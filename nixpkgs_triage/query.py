"""Filtering, listing and showing PRs; local review marks."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys

from .categorize import load_categorizer
from .db import meta_get, open_db
from .jobs import job_describe, latest_jobs
from .util import age, iso, open_url, pr_url, utcnow

REVIEW_STATUSES = ("todo", "reviewing", "done", "skip")

MERGE_CONFLICT_LABEL = "2.status: merge conflict"
BLOCKING_LABELS = (MERGE_CONFLICT_LABEL, "2.status: needs-changes")
READY_SQL = (
    "COALESCE(p.ci_state, '') NOT IN ('FAILURE', 'ERROR') AND NOT EXISTS "
    f"(SELECT 1 FROM json_each(p.labels) l WHERE l.value IN ({', '.join('?' * len(BLOCKING_LABELS))}))"
)


def add_filter_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("-c", "--category", action="append", help="primary category (repeatable)")
    p.add_argument("--tag", action="append", help="any matching category, not only the primary one")
    p.add_argument("-t", "--topic", action="append", help="6.topic label without prefix, e.g. python")
    p.add_argument("-l", "--label", action="append", help="exact GitHub label")
    p.add_argument("-a", "--author")
    p.add_argument("-b", "--base", help="base branch")
    p.add_argument("--drafts", action="store_true", help="include draft PRs")
    p.add_argument("--ready", action="store_true", help="CI not failing, no merge conflict / needs-changes label")
    p.add_argument("--max-size", type=int, help="max additions+deletions")
    p.add_argument(
        "-s",
        "--status",
        choices=("none", "stale", *REVIEW_STATUSES),
        action="append",
        help="local review status; 'stale' = marked done but PR changed since",
    )
    p.add_argument("--sort", choices=("updated", "oldest", "newest", "size"), default="updated")


def build_query(args: argparse.Namespace) -> tuple[str, list]:
    where = ["p.state = 'OPEN'"]
    params: list = []
    if not args.drafts:
        where.append("p.is_draft = 0")
    if args.category:
        where.append(f"p.category IN ({', '.join('?' for _ in args.category)})")
        params += args.category
    for values, column, match in (
        (args.tag, "tags", "value = ?"),
        (args.topic, "topics", "lower(value) = lower(?)"),
        (args.label, "labels", "value = ?"),
    ):
        for value in values or []:
            where.append(f"EXISTS (SELECT 1 FROM json_each(p.{column}) WHERE {match})")
            params.append(value)
    for column, value in (("author", args.author), ("base_ref", args.base)):
        if value:
            where.append(f"p.{column} = ?")
            params.append(value)
    if args.ready:
        where.append(READY_SQL)
        params += BLOCKING_LABELS
    if args.max_size is not None:
        where.append("p.additions + p.deletions <= ?")
        params.append(args.max_size)
    if args.status:
        ors = []
        for s in args.status:
            if s == "none":
                ors.append("r.status IS NULL")
            elif s == "stale":
                ors.append("(r.status = 'done' AND p.updated_at > r.pr_updated_at)")
            else:
                ors.append("r.status = ?")
                params.append(s)
        where.append(f"({' OR '.join(ors)})")
    order = {
        "updated": "p.updated_at DESC",
        "oldest": "p.created_at ASC",
        "newest": "p.created_at DESC",
        "size": "p.additions + p.deletions ASC",
    }[args.sort]
    sql = (
        "SELECT p.*, r.status AS review_status, r.note AS review_note, r.pr_updated_at AS reviewed_version "
        "FROM prs p LEFT JOIN reviews r ON r.number = p.number "
        f"WHERE {' AND '.join(where)} ORDER BY {order}"
    )
    return sql, params


def review_marker(r: sqlite3.Row) -> str:
    if not r["review_status"]:
        return ""
    stale = r["reviewed_version"] and r["updated_at"] > r["reviewed_version"]
    return r["review_status"] + ("*" if stale else "")


# GitHub's combined check state of the head commit; "none" = no checks reported.
CI_LABEL = {"SUCCESS": "pass", "FAILURE": "FAIL", "ERROR": "error", "PENDING": "pending", "EXPECTED": "pending"}


def ci_label(state: str | None) -> str:
    return CI_LABEL.get(state or "", "none")


def print_rows(rows: list[sqlite3.Row]) -> None:
    width = shutil.get_terminal_size((160, 20)).columns
    fmt = "{:<8} {:<18.18} {:<12.12} {:<14} {:>4} {:<7} {:<10} {}"
    if rows:
        print(fmt.format("PR", "category", "topics", "+added/-del", "age", "CI", "mark", "title")[:width])
    for r in rows:
        topics = ",".join(json.loads(r["topics"]))
        line = fmt.format(
            f"#{r['number']}",
            r["category"],
            topics,
            f"+{r['additions']}/-{r['deletions']}",
            age(r["created_at"]),
            ci_label(r["ci_state"]),
            review_marker(r),
            r["title"],
        )
        print(line[:width])


def cmd_list(args: argparse.Namespace) -> None:
    db = open_db()
    load_categorizer(db)
    sql, params = build_query(args)
    rows = db.execute(sql + " LIMIT ?", [*params, args.limit]).fetchall()
    if args.json:
        out = [dict(r) | {k: json.loads(r[k]) for k in ("labels", "tags", "topics")} for r in rows]
        json.dump(out, sys.stdout, indent=2)
        print()
    else:
        print_rows(rows)


def cmd_next(args: argparse.Namespace) -> None:
    db = open_db()
    load_categorizer(db)
    if not args.status:
        args.status = ["none", "todo", "stale"]
    sql, params = build_query(args)
    row = db.execute(sql + " LIMIT 1", params).fetchone()
    if row is None:
        sys.exit("nothing left in this queue")
    show(db, row["number"])
    if args.open:
        open_url(pr_url(row["number"]))


def show(db: sqlite3.Connection, number: int) -> None:
    r = db.execute(
        "SELECT p.*, r.status AS review_status, r.note AS review_note, r.marked_at, r.pr_updated_at AS reviewed_version "
        "FROM prs p LEFT JOIN reviews r ON r.number = p.number WHERE p.number = ?",
        (number,),
    ).fetchone()
    if r is None:
        sys.exit(f"#{number} is not in the database (run `triage update`)")
    print(f"#{r['number']} {r['title']}")
    print(f"  {pr_url(r['number'])}")
    print(f"  state:     {r['state']}{' (draft)' if r['is_draft'] else ''} -> {r['base_ref']}")
    print(f"  author:    {r['author']} ({r['author_association']})")
    print(f"  created:   {r['created_at']} ({age(r['created_at'])} ago), updated {r['updated_at']}")
    print(f"  category:  {r['category']}  tags: {', '.join(json.loads(r['tags'])) or '-'}")
    print(f"  topics:    {', '.join(json.loads(r['topics'])) or '-'}")
    print(f"  size:      +{r['additions']}/-{r['deletions']} in {r['changed_files']} files, {r['comments']} comments")
    print(f"  CI:        {r['ci_state'] or '-'}   review decision: {r['review_decision'] or '-'}")
    print(f"  labels:    {', '.join(json.loads(r['labels'])) or '-'}")
    if r["review_status"]:
        print(
            f"  local:     {review_marker(r)} (marked {r['marked_at']}){': ' + r['review_note'] if r['review_note'] else ''}"
        )
    jobs = latest_jobs(db, number)
    print(f"  check:     {job_describe(jobs.get((number, 'check')))}")
    print(f"  nixpkgs-review: {job_describe(jobs.get((number, 'review')))}")


def cmd_show(args: argparse.Namespace) -> None:
    db = open_db()
    load_categorizer(db)
    show(db, args.number)
    if args.open:
        open_url(pr_url(args.number))


def cmd_mark(args: argparse.Namespace) -> None:
    db = open_db()
    for number in args.numbers:
        row = db.execute("SELECT updated_at FROM prs WHERE number = ?", (number,)).fetchone()
        if row is None:
            sys.exit(f"#{number} is not in the database (run `triage update`)")
        if args.status == "clear":
            db.execute("DELETE FROM reviews WHERE number = ?", (number,))
        else:
            db.execute(
                "INSERT OR REPLACE INTO reviews(number, status, note, marked_at, pr_updated_at) VALUES (?, ?, ?, ?, ?)",
                (number, args.status, args.note, iso(utcnow()), row["updated_at"]),
            )
    db.commit()


def cmd_stats(args: argparse.Namespace) -> None:
    db = open_db()
    cat = load_categorizer(db)
    last = meta_get(db, "last_sync")
    print(f"last sync: {last or 'never'}")
    if args.by == "topic":
        key_sql = "COALESCE(t.value, '(none)')"
        source = "prs p LEFT JOIN json_each(p.topics) t"
    else:
        key_sql = f"p.{args.by}"
        source = "prs p"
    rows = db.execute(
        f"""
        SELECT {key_sql} AS grp,
               SUM(p.is_draft = 0) AS open,
               SUM(p.is_draft = 1) AS drafts,
               SUM(p.is_draft = 0 AND {READY_SQL}) AS ready,
               SUM(r.status = 'done') AS done,
               SUM(r.status = 'skip') AS skipped
        FROM {source} LEFT JOIN reviews r ON r.number = p.number
        WHERE p.state = 'OPEN'
        GROUP BY grp
        """,
        BLOCKING_LABELS,
    ).fetchall()
    if args.by == "category":
        order = {n: i for i, n in enumerate(cat.names)}
        rows.sort(key=lambda r: order.get(r["grp"], len(order)))
    else:
        rows.sort(key=lambda r: -(r["open"] + r["drafts"]))
        rows = rows[: args.limit]
    print(f"{args.by:<24} {'open':>6} {'drafts':>6} {'ready':>6} {'done':>6} {'skip':>6}")
    totals = [0] * 5
    for r in rows:
        vals = [r["open"], r["drafts"], r["ready"], r["done"] or 0, r["skipped"] or 0]
        totals = [a + b for a, b in zip(totals, vals)]
        print(f"{str(r['grp']):<24.24} " + " ".join(f"{v:>6}" for v in vals))
    if args.by == "category":
        print(f"{'total':<24} " + " ".join(f"{v:>6}" for v in totals))
