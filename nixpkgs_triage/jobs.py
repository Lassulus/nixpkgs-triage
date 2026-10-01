"""Background jobs per PR (guideline check, nixpkgs-review): starting, tracking, cancelling, posting."""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

from .config import ENTRY, JOBS_DIR, ROOT
from .db import open_db
from .github import ADD_COMMENT_MUTATION, GitHub, github_token
from .settings import agent_argv, load_settings, nixpkgs_dir, review_argv
from .util import TriageError, fmt_duration, iso, parse_ts, pr_url, read_tail, since, utcnow

JOB_TITLES = {"check": "guideline check", "review": "nixpkgs-review"}

ACTIVE_JOB_STATES = ("pending", "running")

# Logs can be long (nix build output); views show their end.
LOG_TAIL_BYTES = 512 * 1024

# Runners started by this process; polled so finished ones don't linger as zombies.
SPAWNED: list[subprocess.Popen] = []


def runner_alive(pid: int) -> bool:
    proc = Path(f"/proc/{pid}")
    if proc.exists():
        try:
            state = (proc / "stat").read_text().rsplit(")", 1)[1].split()[0]
            cmdline = (proc / "cmdline").read_bytes()
        except OSError:
            return False
        # A reused pid runs something else; a zombie is dead.
        return state != "Z" and b"job-run" in cmdline
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def reap_jobs(db: sqlite3.Connection) -> None:
    """Fail jobs whose runner vanished (killed, reboot). New jobs get a grace minute to record their pid."""
    grace = iso(utcnow() - timedelta(minutes=1))
    for job in db.execute("SELECT id, pid, created_at FROM jobs WHERE status IN ('pending', 'running')").fetchall():
        if job["pid"] is None and job["created_at"] > grace:
            continue
        if job["pid"] is None or not runner_alive(job["pid"]):
            db.execute(
                "UPDATE jobs SET status = 'failed', summary = 'runner died', finished_at = ? "
                "WHERE id = ? AND status IN ('pending', 'running')",
                (iso(utcnow()), job["id"]),
            )
    db.commit()


def latest_jobs(db: sqlite3.Connection, number: int | None = None) -> dict[tuple[int, str], sqlite3.Row]:
    where, params = ("WHERE number = ?", [number]) if number is not None else ("", [])
    rows = db.execute(
        f"SELECT * FROM jobs WHERE id IN (SELECT MAX(id) FROM jobs {where} GROUP BY number, kind)", params
    ).fetchall()
    return {(r["number"], r["kind"]): r for r in rows}


def job_output(job: sqlite3.Row, want_log: bool) -> tuple[Path, bool]:
    """The file to show for a job and whether it is the log: the report once finished, otherwise the live log."""
    jobdir = Path(job["dir"])
    report = jobdir / "report.md"
    use_log = want_log or job["status"] in ACTIVE_JOB_STATES or not report.exists() or report.stat().st_size == 0
    return (jobdir / "job.log" if use_log else report), use_log


def output_lines(path: Path) -> list[str]:
    """The (tail of a) job output file as lines; carriage-return progress output keeps its final state."""
    return [raw.rsplit("\r", 1)[-1].replace("\t", "    ") for raw in read_tail(path, LOG_TAIL_BYTES).splitlines()]


def job_short(job: sqlite3.Row | None) -> str:
    """7-character status for list columns."""
    if job is None:
        return ""
    if job["status"] == "success":
        return {"PASS": "pass", "ISSUES": "issues"}.get(job["summary"], "done") if job["kind"] == "check" else "pass"
    return {"pending": "pending", "running": "running", "failed": "FAIL", "cancelled": "cancel"}[job["status"]]


def job_describe(job: sqlite3.Row | None) -> str:
    if job is None:
        return "not run"
    status = job["status"]
    if status == "pending":
        return f"pending: queued {since(job['created_at'])} ago, waiting for a free slot"
    if status == "running":
        return f"running for {since(job['started_at'])}"
    if status == "cancelled":
        text = "cancelled"
    elif job["kind"] == "check" and status == "success":
        text = {
            "PASS": "PASS: no blocking guideline issues",
            "ISSUES": "ISSUES: blocking guideline issues",
        }.get(job["summary"], f"done: {job['summary']}")
    else:
        text = f"{status}: {job['summary']}"
    parts = [text]
    if job["finished_at"]:
        took = ""
        if job["started_at"]:
            took = (
                f", took {fmt_duration((parse_ts(job['finished_at']) - parse_ts(job['started_at'])).total_seconds())}"
            )
        parts.append(f"(finished {since(job['finished_at'])} ago{took})")
    if job["head_sha"]:
        parts.append(f"at {job['head_sha'][:10]}")
    if job["posted_at"]:
        parts.append(f"posted: {job['comment_url']}")
    return "  ".join(parts)


def start_job(db: sqlite3.Connection, number: int, kind: str) -> int:
    """Record a pending job and start its detached runner (`triage job-run ID`). Returns the job id."""
    reap_jobs(db)
    if db.execute("SELECT 1 FROM prs WHERE number = ?", (number,)).fetchone() is None:
        raise TriageError(f"#{number} is not in the database (run `triage update`)")
    active = db.execute(
        "SELECT status FROM jobs WHERE number = ? AND kind = ? AND status IN ('pending', 'running')", (number, kind)
    ).fetchone()
    if active:
        raise TriageError(f"{JOB_TITLES[kind]} for #{number} is already {active['status']}")
    settings = load_settings(db)
    checkout = nixpkgs_dir(settings)
    if kind == "review" and not (checkout / ".git").exists():
        raise TriageError(f"nixpkgs-review needs a nixpkgs git checkout at {checkout} (see settings)")
    tool = (agent_argv(settings) if kind == "check" else review_argv(settings, number))[0]
    if not shutil.which(tool):
        raise TriageError(f"`{tool}` not found in PATH (see settings)")

    job_id = db.execute(
        "INSERT INTO jobs (number, kind, status, dir, created_at) VALUES (?, ?, 'pending', '', ?)",
        (number, kind, iso(utcnow())),
    ).lastrowid
    jobdir = JOBS_DIR / str(number) / f"{job_id}-{kind}"
    jobdir.mkdir(parents=True)
    db.execute("UPDATE jobs SET dir = ? WHERE id = ?", (str(jobdir), job_id))
    db.commit()
    with open(jobdir / "job.log", "ab") as logf:
        proc = subprocess.Popen(
            [sys.executable, str(ENTRY), "job-run", str(job_id)],
            stdin=subprocess.DEVNULL,
            stdout=logf,
            stderr=subprocess.STDOUT,
            cwd=ROOT,
            start_new_session=True,
        )
    SPAWNED.append(proc)
    db.execute("UPDATE jobs SET pid = ? WHERE id = ? AND pid IS NULL", (proc.pid, job_id))
    db.commit()
    return job_id


def cancel_job(db: sqlite3.Connection, job_id: int) -> None:
    job = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None:
        raise TriageError(f"no job {job_id}")
    if job["status"] not in ACTIVE_JOB_STATES:
        raise TriageError(f"job {job_id} is already {job['status']}")
    if job["pid"] and runner_alive(job["pid"]):
        # The runner interrupts its tool (nixpkgs-review then removes its worktree) and records "cancelled".
        os.kill(job["pid"], signal.SIGTERM)
    else:
        db.execute(
            "UPDATE jobs SET status = 'cancelled', summary = 'cancelled', finished_at = ? WHERE id = ?",
            (iso(utcnow()), job_id),
        )
        db.commit()


def review_report(db: sqlite3.Connection, number: int) -> tuple[sqlite3.Row, Path]:
    job = db.execute(
        "SELECT * FROM jobs WHERE number = ? AND kind = 'review' AND status IN ('success', 'failed') "
        "ORDER BY id DESC LIMIT 1",
        (number,),
    ).fetchone()
    report = Path(job["dir"]) / "report.md" if job else None
    if report is None or not report.exists():
        raise TriageError(f"no finished nixpkgs-review report for #{number}")
    return job, report


def post_review(db: sqlite3.Connection, number: int) -> str:
    """Post the latest nixpkgs-review report as a PR comment (what `nixpkgs-review post-result` does)."""
    job, report = review_report(db, number)
    node_id = db.execute("SELECT node_id FROM prs WHERE number = ?", (number,)).fetchone()["node_id"]
    data = GitHub(github_token(), delay=0, reserve=0).query(
        ADD_COMMENT_MUTATION, {"subject": node_id, "body": report.read_text()}
    )
    url = data["addComment"]["commentEdge"]["node"]["url"]
    db.execute("UPDATE jobs SET posted_at = ?, comment_url = ? WHERE id = ?", (iso(utcnow()), url, job["id"]))
    db.commit()
    return url


def cmd_start_jobs(args: argparse.Namespace) -> None:
    db = open_db()
    failed = False
    for number in args.numbers:
        try:
            job_id = start_job(db, number, args.kind)
        except TriageError as e:
            print(f"#{number}: {e}", file=sys.stderr)
            failed = True
            continue
        jobdir = db.execute("SELECT dir FROM jobs WHERE id = ?", (job_id,)).fetchone()["dir"]
        print(f"#{number}: started {JOB_TITLES[args.kind]} (job {job_id}, log {jobdir}/job.log)")
    sys.exit(1 if failed else 0)


def cmd_jobs(args: argparse.Namespace) -> None:
    db = open_db()
    reap_jobs(db)
    where = "" if args.all else "WHERE status IN ('pending', 'running')"
    for job in db.execute(f"SELECT * FROM jobs {where} ORDER BY id DESC").fetchall():
        print(f"{job['id']:>5}  #{job['number']:<7} {job['kind']:<7} {job_describe(job)}")


def cmd_cancel(args: argparse.Namespace) -> None:
    db = open_db()
    for job_id in args.job_ids:
        cancel_job(db, job_id)


def cmd_post(args: argparse.Namespace) -> None:
    db = open_db()
    job, report = review_report(db, args.number)
    if not args.yes:
        print(report.read_text())
        if job["posted_at"]:
            print(f"already posted {job['posted_at']}: {job['comment_url']}")
        if input(f"Post this as a comment on {pr_url(args.number)}? [y/N] ").strip().lower() != "y":
            sys.exit("not posted")
    print(post_review(db, args.number))
