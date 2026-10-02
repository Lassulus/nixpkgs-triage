"""Mirroring open PRs into the local database: from GitHub, or from a triage server that does that."""

from __future__ import annotations

import argparse
import fcntl
import gzip
import json
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import timedelta
from urllib.parse import quote

from .categorize import Categorizer, load_categorizer
from .config import DB_PATH, ENTRY, OWNER, REPO
from .db import meta_get, meta_set, open_db
from .github import NODES_QUERY, PAGE_QUERY, GitHub, ServerTimeout, github_token
from .settings import load_settings
from .util import TriageError, iso, log, parse_ts, utcnow

# Incremental syncs re-read this much before the previous watermark to absorb clock skew.
SYNC_OVERLAP = timedelta(minutes=5)

MIN_PAGE_SIZE = 5

# Held by a running `triage update`, so the UI, the web daemon and manual runs never sync concurrently.
SYNC_LOCK_PATH = DB_PATH.with_name(DB_PATH.name + ".sync.lock")


class SyncJob:
    """`triage update ARGS` in a child process; its log lines are collected (and echoed to stderr if asked)."""

    def __init__(self, *args: str, echo: bool = False) -> None:
        self.lines: list[str] = []
        self.echo = echo
        self.proc = subprocess.Popen(
            [sys.executable, str(ENTRY), "update", *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self) -> None:
        for line in self.proc.stdout:
            self.lines.append(line.rstrip("\n"))
            if self.echo:
                print(f"sync: {self.lines[-1]}", file=sys.stderr, flush=True)
        self.proc.wait()

    @property
    def running(self) -> bool:
        return self.thread.is_alive()

    def cancel(self) -> None:
        # SIGINT takes the "interrupted, progress is saved" path; every page is committed.
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)


def author_login(author: dict | None) -> str | None:
    """Bots are written like GitHub does (`name[bot]`); their page is github.com/apps/name, not github.com/name."""
    if author is None:
        return None
    return author["login"] + ("[bot]" if author.get("__typename") == "Bot" else "")


def reviewers(node: dict, state: str) -> str:
    reviews = node["latestOpinionatedReviews"]["nodes"]
    return json.dumps([(r["author"] or {}).get("login", "ghost") for r in reviews if r["state"] == state])


def node_to_row(node: dict) -> dict:
    commits = node["commits"]["nodes"]
    rollup = commits[0]["commit"]["statusCheckRollup"] if commits else None
    return {
        "number": node["number"],
        "node_id": node["id"],
        "title": node["title"],
        "author": author_login(node.get("author")),
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
        # Reviewers by their latest approving-or-requesting-changes review.
        "approvals": reviewers(node, "APPROVED"),
        "blocking": reviewers(node, "CHANGES_REQUESTED"),
        "reactions": json.dumps(
            {g["content"]: g["reactors"]["totalCount"] for g in node["reactionGroups"] if g["reactors"]["totalCount"]}
        ),
    }


def upsert(db: sqlite3.Connection, cat: Categorizer, node: dict, seen_run: str | None = None) -> None:
    row = node_to_row(node)
    row["labels"] = json.dumps(row["labels"])
    row["seen_run"] = seen_run
    row["synced_at"] = iso(utcnow())
    write_row(db, cat, row)


def write_row(db: sqlite3.Connection, cat: Categorizer, row: dict) -> None:
    """Insert or update a PR row (JSON columns as strings), categorized with the local categories.toml."""
    row["category"], tags, topics = cat.classify({**row, "labels": json.loads(row["labels"])})
    row["tags"] = json.dumps(tags)
    row["topics"] = json.dumps(topics)
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
    log(f"full sync started (run {run})" if cursor is None else f"resuming full sync of run {run}")
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


def sync_from_server(db: sqlite3.Connection, cat: Categorizer, server: str) -> None:
    """Copy the PR rows a triage server (`triage serve`) changed since our last pull."""
    since = meta_get(db, "server_since") or ""
    url = f"{server.rstrip('/')}/api/prs?since={quote(since)}"
    request = urllib.request.Request(url, headers={"Accept-Encoding": "gzip"})
    with urllib.request.urlopen(request, timeout=120) as response:
        body = response.read()
        if response.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
    data = json.loads(body)
    columns = {r["name"] for r in db.execute("PRAGMA table_info(prs)")} - {"seen_run", "category", "tags", "topics"}
    for row in data["prs"]:
        write_row(db, cat, {k: v for k, v in row.items() if k in columns})
    # The server commits a sync page after stamping its rows, so re-read a little before its clock.
    meta_set(db, "server_since", iso(parse_ts(data["now"]) - SYNC_OVERLAP))
    meta_set(db, "last_sync", data["last_sync"])
    db.commit()
    log(f"{len(data['prs'])} PRs changed on {server} since {since or 'the beginning'}")


def cmd_update(args: argparse.Namespace) -> None:
    lock = open(SYNC_LOCK_PATH, "w")  # released when the process ends
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise TriageError("another `triage update` is running; not starting a second one") from None
    db = open_db()
    cat = load_categorizer(db)
    started = time.monotonic()
    server = "" if args.github else load_settings(db)["server"]
    if server:
        sync_from_server(db, cat, server)
        requests = "1 request to the server"
    else:
        gh = GitHub(github_token(), delay=args.delay, reserve=args.reserve)
        if args.full and meta_get(db, "full_sync_run") is None:
            meta_set(db, "full_sync_run", iso(utcnow()))
            db.commit()
        if meta_get(db, "full_sync_run") is not None or meta_get(db, "last_sync") is None:
            sync_full(gh, db, cat, args.page_size)
        sync_incremental(gh, db, cat, args.page_size)
        requests = f"{gh.requests} requests, {gh.points_used} points"
    open_count = db.execute("SELECT COUNT(*) FROM prs WHERE state = 'OPEN'").fetchone()[0]
    log(f"done in {time.monotonic() - started:.0f}s: {requests}; {open_count} open PRs in {DB_PATH}")
