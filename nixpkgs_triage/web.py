"""Web dashboard: a read-only JSON API and a static page over the database, plus a periodic sync."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import signal
import socket
import sqlite3
import threading
import time
from datetime import timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import NamedTuple
from urllib.parse import parse_qs, urlsplit

from .categorize import load_categorizer
from .db import meta_get, open_db
from .jobs import (
    ACTIVE_JOB_STATES,
    JOB_TITLES,
    job_describe,
    job_output,
    job_short,
    latest_jobs,
    output_lines,
    reap_jobs,
)
from .listing import (
    DEFAULT_FILTERS,
    FILTERS,
    SORTS,
    Jobs,
    category_counts,
    load_open_rows,
    sorted_rows,
    visible_rows,
)
from .query import ci_label, review_marker
from .sync import SyncJob
from .util import TriageError, iso, log, pr_url, utcnow

STATIC_DIR = Path(__file__).resolve().parent / "static"
# URL path -> (file in STATIC_DIR, content type)
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}


def load_static() -> dict[str, tuple[bytes, bytes, str, str]]:
    """URL path -> (body, gzipped body, content type, ETag), read once at startup."""
    static = {}
    for path, (name, content_type) in STATIC_FILES.items():
        body = (STATIC_DIR / name).read_bytes()
        static[path] = (body, gzip.compress(body, 9), content_type, f'"{hashlib.sha256(body).hexdigest()[:16]}"')
    return static


PAGE_SIZE = 100
MAX_PAGE_SIZE = 500
# How often requests look for database changes (syncs, job runners, the curses UI all write to it).
SNAPSHOT_CHECK_SECONDS = 2.0


class BadRequest(Exception):
    pass


class NotFound(Exception):
    pass


class Data(NamedTuple):
    rows: list[dict]
    jobs: Jobs
    last_sync: str | None
    generation: int  # bumped on every reload; the page compares it to notice new data


class Snapshot:
    """Open PRs and the latest jobs, kept in memory and reloaded when the database changed.

    One connection serves all request threads (under a lock), so PRAGMA data_version tells
    whether another connection committed since the last look."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.db = open_db(check_same_thread=False)
        self.category_names = load_categorizer(self.db).names
        self.data_version: int | None = None
        self.checked = 0.0
        self.data = Data([], {}, None, 0)

    def current(self) -> Data:
        with self.lock:
            if time.monotonic() - self.checked >= SNAPSHOT_CHECK_SECONDS:
                self.checked = time.monotonic()
                changes = self.db.total_changes
                reap_jobs(self.db)  # a dead runner's job shows as failed instead of running forever
                version = self.db.execute("PRAGMA data_version").fetchone()[0]
                if version != self.data_version or self.db.total_changes != changes:
                    self.data_version = version
                    self.data = Data(
                        load_open_rows(self.db),
                        latest_jobs(self.db),
                        meta_get(self.db, "last_sync"),
                        self.data.generation + 1,
                    )
            return self.data

    def fetchone(self, sql: str, params: tuple) -> sqlite3.Row | None:
        with self.lock:
            return self.db.execute(sql, params).fetchone()


class SyncLoop:
    """Runs `triage update` now and then every `interval` seconds after the previous run ended."""

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self.job: SyncJob | None = None
        self.last_exit: int | None = None
        self.last_finished: str | None = None
        self.next_at: str | None = None
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self) -> None:
        while True:
            self.job = SyncJob(echo=True)
            self.job.thread.join()
            self.last_exit = self.job.proc.returncode
            finished = utcnow()
            self.last_finished = iso(finished)
            self.next_at = iso(finished + timedelta(seconds=self.interval))
            time.sleep(self.interval)

    def state(self) -> dict:
        job = self.job
        running = job is not None and job.running
        return {
            "interval": self.interval,
            "running": running,
            "line": job.lines[-1] if job and job.lines else None,
            "last_exit": self.last_exit,
            "last_finished": self.last_finished,
            "next": None if running else self.next_at,
        }

    def stop(self) -> None:
        if self.job and self.job.running:
            self.job.cancel()
            self.job.thread.join(timeout=10)


def row_json(r: dict, jobs: Jobs) -> dict:
    return {
        "number": r["number"],
        "title": r["title"],
        "author": r["author"],
        "category": r["category"],
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
        "additions": r["additions"],
        "deletions": r["deletions"],
        "ci": ci_label(r["ci_state"]),
        "ci_failing": r["ci_state"] in ("FAILURE", "ERROR"),
        "conflict": bool(r["conflict"]),
        "draft": bool(r["is_draft"]),
        "mark": review_marker(r),
        "check": job_short(jobs.get((r["number"], "check"))),
        "review": job_short(jobs.get((r["number"], "review"))),
    }


def job_json(job: sqlite3.Row | None) -> dict | None:
    if job is None:
        return None
    return {
        "id": job["id"],
        "status": job["status"],
        "short": job_short(job),
        "describe": job_describe(job),
        "active": job["status"] in ACTIVE_JOB_STATES,
    }


def sort_cursor(r: dict, column: str) -> str:
    return f"{r[column]}|{r['number']}"


class Dashboard(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], snapshot: Snapshot, sync: SyncLoop | None) -> None:
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(address, Handler)
        self.snapshot = snapshot
        self.sync = sync
        self.static = load_static()
        # (filters, sort) -> (visible rows, sorted rows, category counts) for the current data generation
        self.lists: dict = {}
        self.lists_generation = -1

    def sorted_list(self, data: Data, filters: dict[str, str], sort: str) -> tuple[list, list, list]:
        """Filtered and sorted open PRs, cached until the data changes, so scrolling and category switches are cheap."""
        if self.lists_generation != data.generation or len(self.lists) > 32:
            self.lists, self.lists_generation = {}, data.generation
        key = (tuple(filters.items()), sort)
        if key not in self.lists:
            visible = visible_rows(data.rows, data.jobs, filters)
            counts = category_counts(self.snapshot.category_names, visible)
            self.lists[key] = (visible, sorted_rows(visible, sort), counts)
        return self.lists[key]

    def status(self, data: Data | None = None) -> dict:
        data = data or self.snapshot.current()
        return {
            "generation": data.generation,
            "last_sync": data.last_sync,
            "total_open": len(data.rows),
            "sync": self.sync.state() if self.sync else None,
        }

    def prs(self, params: dict[str, str]) -> dict:
        data = self.snapshot.current()
        filters = {}
        for key, _, options in FILTERS:
            filters[key] = params.get(key, DEFAULT_FILTERS[key])
            if filters[key] not in options:
                raise BadRequest(f"{key} must be one of {', '.join(options)}")
        sort = params.get("sort", SORTS[0][0])
        if sort not in [s[0] for s in SORTS]:
            raise BadRequest(f"unknown sort {sort}")
        try:
            limit = min(MAX_PAGE_SIZE, max(1, int(params.get("limit", PAGE_SIZE))))
        except ValueError:
            raise BadRequest("limit must be a number") from None
        category = params.get("category", "all")

        visible, prs, counts = self.sorted_list(data, filters, sort)
        if category != "all":
            prs = [r for r in prs if r["category"] == category]
        _, column, descending = next(s for s in SORTS if s[0] == sort)
        start = 0
        if params.get("after"):
            # Keyset paging: continue after the last row's sort key, even if rows moved or vanished meanwhile.
            value, _, number = params["after"].rpartition("|")
            if not number.isdigit():
                raise BadRequest("bad cursor")
            cursor = (value, int(number))
            start = next(
                (
                    i
                    for i, r in enumerate(prs)
                    if ((r[column], r["number"]) < cursor if descending else (r[column], r["number"]) > cursor)
                ),
                len(prs),
            )
        page = prs[start : start + limit]
        more = start + limit < len(prs)
        return {
            **self.status(data),
            # The page builds its controls from these, so its first load is a single API request.
            "filters": [{"key": k, "label": label, "options": options} for k, label, options in FILTERS],
            "sorts": [s[0] for s in SORTS],
            "state": {"category": category, "sort": sort, "filters": filters},
            "matching": len(visible),
            "categories": counts,
            "count": len(prs),
            "prs": [row_json(r, data.jobs) for r in page],
            "next": sort_cursor(page[-1], column) if more else None,
        }

    def pr(self, number: int) -> dict:
        pr = self.snapshot.fetchone(
            "SELECT p.*, r.status AS review_status, r.note AS review_note, r.pr_updated_at AS reviewed_version "
            "FROM prs p LEFT JOIN reviews r ON r.number = p.number WHERE p.number = ?",
            (number,),
        )
        if pr is None:
            raise NotFound(f"PR #{number} is not in the database")
        jobs = self.snapshot.current().jobs
        return {
            "number": number,
            "url": pr_url(number),
            "title": pr["title"],
            "author": pr["author"],
            "author_association": pr["author_association"],
            "state": pr["state"],
            "draft": bool(pr["is_draft"]),
            "base_ref": pr["base_ref"],
            "created_at": pr["created_at"],
            "updated_at": pr["updated_at"],
            "category": pr["category"],
            "tags": json.loads(pr["tags"]),
            "topics": json.loads(pr["topics"]),
            "labels": json.loads(pr["labels"]),
            "ci": ci_label(pr["ci_state"]),
            "comments": pr["comments"],
            "mark": review_marker(pr),
            "note": pr["review_note"],
            "additions": pr["additions"],
            "deletions": pr["deletions"],
            "changed_files": pr["changed_files"],
            "jobs": {kind: job_json(jobs.get((number, kind))) for kind in JOB_TITLES},
        }

    def output(self, number: int, kind: str, want_log: bool) -> dict:
        job = self.snapshot.current().jobs.get((number, kind))
        if job is None:
            raise NotFound(f"no {JOB_TITLES[kind]} for #{number}")
        path, use_log = job_output(job, want_log)
        active = job["status"] in ACTIVE_JOB_STATES
        return {
            "job": job_json(job),
            "what": ("live log" if active else "log") if use_log else "report",
            "is_log": use_log,
            "text": "\n".join(output_lines(path)) if path.exists() else None,
        }


class Handler(BaseHTTPRequestHandler):
    server: Dashboard
    server_version = "nixpkgs-triage"
    protocol_version = "HTTP/1.1"  # keep-alive; every response has a Content-Length

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        params = {k: v[-1] for k, v in parse_qs(url.query).items()}
        parts = url.path.strip("/").split("/")
        try:
            if url.path in self.server.static:
                self.send_static(*self.server.static[url.path])
            elif url.path == "/api/status":
                self.send_json(self.server.status())
            elif url.path == "/api/prs":
                self.send_json(self.server.prs(params))
            elif len(parts) == 3 and parts[:2] == ["api", "pr"] and parts[2].isdigit():
                self.send_json(self.server.pr(int(parts[2])))
            elif len(parts) == 4 and parts[:2] == ["api", "pr"] and parts[2].isdigit() and parts[3] in JOB_TITLES:
                self.send_json(self.server.output(int(parts[2]), parts[3], params.get("log") == "1"))
            else:
                raise NotFound("not found")
        except BadRequest as e:
            self.send_json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
        except NotFound as e:
            self.send_json({"error": str(e)}, HTTPStatus.NOT_FOUND)

    def send_static(self, body: bytes, gzipped: bytes, content_type: str, etag: str) -> None:
        if self.headers.get("If-None-Match") == etag:
            self.send_response(HTTPStatus.NOT_MODIFIED)
            self.send_header("ETag", etag)
            self.end_headers()
            return
        self.send(HTTPStatus.OK, body, content_type, "no-cache", etag=etag, gzipped=gzipped)

    def send_json(self, value: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(value, separators=(",", ":")).encode()
        self.send(status, body, "application/json", "no-store", gzipped=gzip.compress(body, 5))

    def send(
        self, status: HTTPStatus, body: bytes, content_type: str, cache: str, etag: str = "", gzipped: bytes = b""
    ) -> None:
        compress = bool(gzipped) and "gzip" in self.headers.get("Accept-Encoding", "")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", cache)
        self.send_header("Vary", "Accept-Encoding")
        if etag:
            self.send_header("ETag", etag)
        if compress:
            self.send_header("Content-Encoding", "gzip")
            body = gzipped
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        # The page polls; only failures are worth a log line.
        if isinstance(code, int) and code >= 400:
            super().log_request(code, size)


def parse_listen(listen: str) -> tuple[str, int]:
    host, sep, port = listen.rpartition(":")
    if not sep or not port.isdigit():
        raise TriageError(f"--listen must be HOST:PORT, got {listen!r}")
    return host.strip("[]") or "0.0.0.0", int(port)


def cmd_serve(args: argparse.Namespace) -> None:
    address = parse_listen(args.listen)
    snapshot = Snapshot()
    snapshot.current()  # load the PRs now rather than in the first request
    sync = SyncLoop(args.sync_every) if args.sync_every > 0 else None
    server = Dashboard(address, snapshot, sync)

    def stop(*_) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    host, port = address
    log(f"serving the dashboard on http://{f'[{host}]' if ':' in host else host}:{port}/")
    if sync:
        log(f"syncing every {args.sync_every:g}s")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("stopping")
    finally:
        server.server_close()
        if sync:
            sync.stop()
