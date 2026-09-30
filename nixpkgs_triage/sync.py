"""Mirroring open PRs from GitHub into the local database."""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from datetime import timedelta

from .categorize import Categorizer, load_categorizer
from .config import DB_PATH, OWNER, REPO
from .db import meta_get, meta_set, open_db
from .github import NODES_QUERY, PAGE_QUERY, GitHub, ServerTimeout, github_token
from .util import iso, log, parse_ts, utcnow

# Incremental syncs re-read this much before the previous watermark to absorb clock skew.
SYNC_OVERLAP = timedelta(minutes=5)

MIN_PAGE_SIZE = 5


def node_to_row(node: dict) -> dict:
    commits = node["commits"]["nodes"]
    rollup = commits[0]["commit"]["statusCheckRollup"] if commits else None
    return {
        "number": node["number"],
        "node_id": node["id"],
        "title": node["title"],
        "author": (node.get("author") or {}).get("login"),
        "author_association": node["authorAssociation"],
        "state": node["state"],
        "is_draft": int(node["isDraft"]),
        "created_at": node["createdAt"],
        "updated_at": node["updatedAt"],
        "closed_at": node["closedAt"],
        "merged_at": node["mergedAt"],
        "base_ref": node["baseRefName"],
        "additions": node["additions"],
        "deletions": node["deletions"],
        "changed_files": node["changedFiles"],
        "review_decision": node["reviewDecision"],
        "ci_state": rollup["state"] if rollup else None,
        "comments": node["comments"]["totalCount"],
        "labels": [l["name"] for l in node["labels"]["nodes"]],
        "files": [(f["path"], f["changeType"]) for f in node["files"]["nodes"]],
        "files_total": node["files"]["totalCount"],
    }


def upsert(db: sqlite3.Connection, cat: Categorizer, node: dict, seen_run: str | None = None) -> None:
    row = node_to_row(node)
    row["category"], tags, topics = cat.classify(row)
    row["tags"] = json.dumps(tags)
    row["topics"] = json.dumps(topics)
    row["labels"] = json.dumps(row["labels"])
    row["files"] = json.dumps(row["files"])
    row["seen_run"] = seen_run
    row["synced_at"] = iso(utcnow())
    cols = list(row)
    updates = ", ".join(
        f"{c} = excluded.{c}" if c != "seen_run" else "seen_run = COALESCE(excluded.seen_run, prs.seen_run)"
        for c in cols
        if c != "number"
    )
    db.execute(
        f"INSERT INTO prs ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)}) "
        f"ON CONFLICT(number) DO UPDATE SET {updates}",
        [row[c] for c in cols],
    )


def paginate(gh: GitHub, variables: dict, page_size: int, after: str | None):
    """Yield (connection, cursor_after_page). Halves the page size when GitHub times out."""
    size = page_size
    good_pages = 0
    failures = 0
    while True:
        try:
            data = gh.query(PAGE_QUERY, {**variables, "n": size, "after": after})
        except ServerTimeout as e:
            failures += 1
            if size == MIN_PAGE_SIZE and failures > 5:
                raise RuntimeError("GitHub keeps timing out even at the minimum page size") from e
            size = max(MIN_PAGE_SIZE, size // 2)
            log(f"{e}: server timeout, retrying with page size {size}")
            continue
        failures = 0
        good_pages += 1
        if size < page_size and good_pages >= 3:
            size = min(page_size, size * 2)
            good_pages = 0
        conn = data["repository"]["pullRequests"]
        after = conn["pageInfo"]["endCursor"]
        yield conn, after
        if not conn["pageInfo"]["hasNextPage"]:
            return


def sync_full(gh: GitHub, db: sqlite3.Connection, cat: Categorizer, page_size: int) -> None:
    """Page through all open PRs (oldest first, stable cursor). Resumable after interruption."""
    run = meta_get(db, "full_sync_run")
    cursor = meta_get(db, "full_sync_cursor")
    if run is None:
        run = iso(utcnow())
        meta_set(db, "full_sync_run", run)
        db.commit()
    if cursor is None:
        log(f"full sync started (run {run})")
    else:
        log(f"resuming full sync of run {run}")
    seen = db.execute("SELECT COUNT(*) FROM prs WHERE seen_run = ?", (run,)).fetchone()[0]
    variables = {
        "owner": OWNER,
        "repo": REPO,
        "states": ["OPEN"],
        "order": {"field": "CREATED_AT", "direction": "ASC"},
    }
    for conn, cursor in paginate(gh, variables, page_size, cursor):
        for node in conn["nodes"]:
            upsert(db, cat, node, seen_run=run)
        seen += len(conn["nodes"])
        meta_set(db, "full_sync_cursor", cursor)
        db.commit()
        log(f"full sync: {seen}/{conn['totalCount']} open PRs, {gh.points_used} points used")

    # PRs we still think are open but did not see were closed/merged meanwhile: refresh them.
    missing = [
        r["node_id"] for r in db.execute("SELECT node_id FROM prs WHERE state = 'OPEN' AND seen_run IS NOT ?", (run,))
    ]
    if missing:
        log(f"refreshing {len(missing)} PRs that are no longer in the open list")
    for i in range(0, len(missing), 50):
        data = gh.query(NODES_QUERY, {"ids": missing[i : i + 50]})
        for node in data["nodes"]:
            if node:
                upsert(db, cat, node)
        db.commit()
    # Anything updated after the run started is picked up by the following incremental sync.
    meta_set(db, "last_sync", run)
    meta_set(db, "full_sync_run", None)
    meta_set(db, "full_sync_cursor", None)
    db.commit()
    log("full sync complete")


def sync_incremental(gh: GitHub, db: sqlite3.Connection, cat: Categorizer, page_size: int) -> None:
    """Fetch PRs (any state) ordered by most recently updated, until we pass the previous watermark."""
    since = parse_ts(meta_get(db, "last_sync")) - SYNC_OVERLAP
    run_start = utcnow()
    variables = {
        "owner": OWNER,
        "repo": REPO,
        "states": None,
        "order": {"field": "UPDATED_AT", "direction": "DESC"},
    }
    count = 0
    done = False
    for conn, _ in paginate(gh, variables, page_size, None):
        for node in conn["nodes"]:
            if parse_ts(node["updatedAt"]) < since:
                done = True
                break
            upsert(db, cat, node)
            count += 1
        db.commit()
        log(f"incremental: {count} PRs updated since {iso(since)}, {gh.points_used} points used")
        if done:
            break
    # PRs updated while we were paging moved ahead of our cursor; the next run catches them.
    meta_set(db, "last_sync", iso(run_start))
    db.commit()


def cmd_update(args: argparse.Namespace) -> None:
    db = open_db()
    cat = load_categorizer(db)
    gh = GitHub(github_token(), delay=args.delay, reserve=args.reserve)
    started = time.monotonic()
    if args.full and meta_get(db, "full_sync_run") is None:
        meta_set(db, "full_sync_run", iso(utcnow()))
        db.commit()
    if meta_get(db, "full_sync_run") is not None or meta_get(db, "last_sync") is None:
        sync_full(gh, db, cat, args.page_size)
    sync_incremental(gh, db, cat, args.page_size)
    open_count = db.execute("SELECT COUNT(*) FROM prs WHERE state = 'OPEN'").fetchone()[0]
    log(
        f"done in {time.monotonic() - started:.0f}s: {gh.requests} requests, {gh.points_used} points; "
        f"{open_count} open PRs in {DB_PATH}"
    )
