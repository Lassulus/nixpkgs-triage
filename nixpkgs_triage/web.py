"""Web dashboard: server-rendered pages from the database, plus a periodic `triage update`."""

from __future__ import annotations

import argparse
import gzip
import json
import signal
import socket
import threading
import time
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import NamedTuple
from urllib.parse import parse_qs, urlencode, urlsplit

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
from .listing import DEFAULT_FILTERS, FILTERS, SORTS, Jobs, category_counts, load_open_rows, sorted_rows, visible_rows
from .query import ci_label, review_marker
from .sync import SyncJob
from .util import TriageError, age, log, pr_url, since

PAGE_SIZE = 100
DEFAULTS = {"category": "all", "sort": SORTS[0][0], **DEFAULT_FILTERS}
OPTIONS = {"sort": [s[0] for s in SORTS], **{key: options for key, _, options in FILTERS}}
COLUMNS = ("PR", "age", "+/-", "CI", "conflict", "draft", "mark", "check", "nixrev", "category", "title")

CSS = """
:root { color-scheme: light dark; --add: light-dark(#2e7d32, #6cc070); --del: light-dark(#c62828, #ef6b6b); }
body { margin: 0; font: 14px/1.4 system-ui, sans-serif; }
header { position: sticky; top: 0; z-index: 1; display: flex; gap: 1.5em; padding: .5em 1em; background: Canvas;
  border-bottom: 1px solid GrayText; }
header b { margin-right: auto; }
nav { position: fixed; top: 2.6em; bottom: 0; width: 15em; overflow-y: auto; padding: .5em 0; white-space: nowrap; }
nav a { display: flex; justify-content: space-between; padding: 0 1em; color: inherit; text-decoration: none; }
nav a.sel { background: Highlight; color: HighlightText; }
main { margin-left: 15em; padding: 0 1em; }
form { display: flex; flex-wrap: wrap; gap: 1em; padding: .5em 0; }
summary, .head { display: grid; gap: .6em; padding: .15em 0; white-space: nowrap;
  grid-template-columns: 5em 2.5em 7em 4.5em 4.5em 3em 3.5em 3.5em 3.5em 9em 1fr;
  border-bottom: 1px solid color-mix(in srgb, GrayText 30%, transparent); }
summary { cursor: pointer; list-style: none; }
summary > * { overflow: hidden; text-overflow: ellipsis; }
.head { font-weight: bold; }
.draft, .dim { color: GrayText; }
.add, .pass { color: var(--add); }
.del, .FAIL, .issues, .failing { color: var(--del); }
.running { color: orange; }
.pane { padding: .5em 1em 1em; background: color-mix(in srgb, GrayText 10%, Canvas); }
.pane p { margin: .2em 0; }
pre { max-height: 32em; overflow: auto; padding: .5em; border: 1px solid GrayText; white-space: pre-wrap; }
pre.log { white-space: pre; }
"""

# Infinite scroll: a.more links fetch the next rows; opening a PR fetches its detail; detail links reload the pane.
JS = """
const load = async (pane, url) => {
  pane.innerHTML = await (await fetch(url)).text();
  pane.querySelectorAll("pre.log").forEach((p) => (p.scrollTop = p.scrollHeight));
  if (pane.querySelector("[data-live]")) setTimeout(() => pane.parentNode.open && load(pane, url), 3000);
};
document.addEventListener("toggle", (e) => {
  const d = e.target;
  if (d.open && d.dataset.n && !d.dataset.loaded) load(d.lastChild, `/pr/${(d.dataset.loaded = d.dataset.n)}`);
}, true);
document.addEventListener("click", (e) => {
  const a = e.target.closest(".pane a[href^='/']");
  if (a) e.preventDefault(), load(a.closest(".pane"), a.href);
});
const more = new IntersectionObserver(async ([e]) => {
  if (!e.isIntersecting) return;
  more.unobserve(e.target);
  e.target.outerHTML = await (await fetch(e.target.href)).text();
  const next = document.querySelector("a.more");
  if (next) more.observe(next);
}, { rootMargin: "1000px" });
const first = document.querySelector("a.more");
if (first) more.observe(first);
"""


class Data(NamedTuple):
    rows: list[dict]
    jobs: Jobs
    last_sync: str | None
    generation: int


class Snapshot:
    """Open PRs and latest jobs in memory, reloaded when another connection committed (PRAGMA data_version)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.db = open_db(check_same_thread=False)
        self.category_names = load_categorizer(self.db).names
        self.version = None
        self.checked = 0.0
        self.data = Data([], {}, None, 0)

    def current(self) -> Data:
        with self.lock:
            if time.monotonic() - self.checked >= 2:
                self.checked = time.monotonic()
                changes = self.db.total_changes
                reap_jobs(self.db)
                version = self.db.execute("PRAGMA data_version").fetchone()[0]
                if version != self.version or self.db.total_changes != changes:
                    self.version = version
                    rows, jobs = load_open_rows(self.db), latest_jobs(self.db)
                    self.data = Data(rows, jobs, meta_get(self.db, "last_sync"), self.data.generation + 1)
            return self.data

    def pr(self, number: int):
        with self.lock:
            return self.db.execute("SELECT * FROM prs WHERE number = ?", (number,)).fetchone()


class SyncLoop(threading.Thread):
    """`triage update` every `interval` seconds after the previous run ended."""

    def __init__(self, interval: float) -> None:
        super().__init__(daemon=True)
        self.interval = interval
        self.job: SyncJob | None = None
        self.start()

    def run(self) -> None:
        while True:
            self.job = SyncJob(echo=True)
            self.job.thread.join()
            time.sleep(self.interval)

    def status(self) -> str:
        job = self.job
        if job and job.running:
            return f"syncing: {job.lines[-1] if job.lines else 'starting'}"
        if job and job.proc.returncode:
            return f"last sync failed (exit {job.proc.returncode})"
        return ""


def parse_state(params: dict[str, str]) -> dict:
    state = dict(DEFAULTS)
    for k, v in params.items():
        if k == "category" or v in OPTIONS.get(k, ()):
            state[k] = v
    return state


def query(state: dict, **change) -> str:
    return urlencode({k: v for k, v in {**state, **change}.items() if v != DEFAULTS.get(k)})


def row_html(r: dict, jobs: Jobs) -> str:
    n = r["number"]
    check, review = job_short(jobs.get((n, "check"))), job_short(jobs.get((n, "review")))
    failing = "failing" if r["ci_state"] in ("FAILURE", "ERROR") else ""
    return (
        f'<details data-n="{n}"><summary class="{"draft" if r["is_draft"] else ""}">'
        f"<span>#{n}</span><span>{age(r['created_at'])}</span>"
        f'<span><span class="add">+{r["additions"]}</span>/<span class="del">-{r["deletions"]}</span></span>'
        f'<span class="{failing}">{ci_label(r["ci_state"])}</span><span>{"yes" if r["conflict"] else ""}</span>'
        f"<span>{'yes' if r['is_draft'] else ''}</span><span>{escape(review_marker(r))}</span>"
        f'<span class="{check}">{check}</span><span class="{review}">{review}</span>'
        f'<span class="dim">{escape(r["category"])}</span>'
        f'<span>{escape(r["title"])} <span class="dim">{escape(r["author"] or "")}</span></span>'
        f'</summary><div class="pane">loading…</div></details>'
    )


class Dashboard(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], snapshot: Snapshot, sync: SyncLoop | None) -> None:
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(address, Handler)
        self.snapshot = snapshot
        self.sync = sync
        self.lists: dict = {}  # (generation, filters, sort) -> (visible, sorted, category counts)

    def listing(self, state: dict) -> tuple[Data, list, list, list]:
        data = self.snapshot.current()
        filters = {k: state[k] for k in DEFAULT_FILTERS}
        key = (data.generation, tuple(filters.items()), state["sort"])
        if key not in self.lists:
            if len(self.lists) > 32:
                self.lists.clear()
            visible = visible_rows(data.rows, data.jobs, filters)
            counts = category_counts(self.snapshot.category_names, visible)
            self.lists[key] = (visible, sorted_rows(visible, state["sort"]), counts)
        return data, *self.lists[key]

    def rows(self, state: dict, after: str = "") -> str:
        """The next PAGE_SIZE rows after the cursor (sort value|number), plus the link to the following page."""
        data, _, prs, _ = self.listing(state)
        if state["category"] != "all":
            prs = [r for r in prs if r["category"] == state["category"]]
        _, column, descending = next(s for s in SORTS if s[0] == state["sort"])
        start = 0
        if after:
            value, _, number = after.rpartition("|")
            cursor = (value, int(number or 0))

            def past(r: dict) -> bool:
                key = (r[column], r["number"])
                return key < cursor if descending else key > cursor

            start = next((i for i, r in enumerate(prs) if past(r)), len(prs))
        page = prs[start : start + PAGE_SIZE]
        html = "".join(row_html(r, data.jobs) for r in page)
        if start + PAGE_SIZE < len(prs):
            last = page[-1]
            html += f'<a class="more" href="/rows?{query(state, after=f"{last[column]}|{last["number"]}")}">more</a>'
        return html

    def page(self, state: dict) -> str:
        data, visible, _, counts = self.listing(state)
        cats = "".join(
            f'<a class="{"sel" if name == state["category"] else ""}" href="/?{query(state, category=name)}">'
            f"{escape(name)}<span>{n}</span></a>"
            for name, n in counts
        )
        selects = "".join(
            f'<label>{label} <select name="{key}" onchange="this.form.submit()">'
            + "".join(f"<option{' selected' if o == state[key] else ''}>{o}</option>" for o in options)
            + "</select></label>"
            for key, label, options in (("sort", "sort", OPTIONS["sort"]), *FILTERS)
        )
        status = [f"{len(visible)} of {len(data.rows)} open PRs match"]
        status.append(f"last sync {since(data.last_sync)} ago" if data.last_sync else "never synced")
        if self.sync and self.sync.status():
            status.append(escape(self.sync.status()))
        return (
            f'<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
            f'<title>nixpkgs-triage</title><link rel="icon" href="data:,"><style>{CSS}</style>'
            f"<header><b>nixpkgs-triage</b>{''.join(f'<span>{s}</span>' for s in status)}</header>"
            f"<nav>{cats}</nav><main>"
            f'<form><input type="hidden" name="category" value="{escape(state["category"])}">{selects}</form>'
            f'<div class="head">{"".join(f"<span>{c}</span>" for c in COLUMNS)}</div>'
            f"{self.rows(state) or '<p class=dim>no PRs match</p>'}</main><script>{JS}</script>"
        )

    def detail(self, number: int, tab: str, want_log: bool) -> str:
        pr = self.snapshot.pr(number)
        if pr is None:
            return "<p>not in the database</p>"
        jobs = self.snapshot.current().jobs

        def joined(column: str) -> str:
            return escape(", ".join(json.loads(pr[column]))) or "-"

        lines = [
            f'<a href="{pr_url(number)}" target="_blank">{pr_url(number)}</a>',
            f"{pr['state'].lower()}{' draft' if pr['is_draft'] else ''} → {escape(pr['base_ref'])}, "
            f"by {escape(pr['author'] or '')}, opened {age(pr['created_at'])} ago, "
            f"updated {since(pr['updated_at'])} ago",
            f"tags: {joined('tags')} · topics: {joined('topics')}",
            f"+{pr['additions']} -{pr['deletions']} in {pr['changed_files']} files · {pr['comments']} comments",
            f"labels: {joined('labels')}",
        ]
        for kind, title in JOB_TITLES.items():
            job = jobs.get((number, kind))
            link = f'<a href="/pr/{number}?tab={kind}">{title}</a>'
            lines.append(f"{'<b>' + link + '</b>' if kind == tab else link}: {escape(job_describe(job))}")
        job = jobs.get((number, tab))
        if job is None:
            return "".join(f"<p>{line}</p>" for line in lines)
        path, use_log = job_output(job, want_log)
        lines.append(
            f'<a href="/pr/{number}?tab={tab}{"" if use_log else "&log=1"}">show {"report" if use_log else "log"}</a>'
        )
        text = "\n".join(output_lines(path)) if path.exists() else "(no output yet)"
        live = " data-live" if job["status"] in ACTIVE_JOB_STATES else ""
        return (
            "".join(f"<p>{line}</p>" for line in lines)
            + f'<pre class="{"log" if use_log else ""}"{live}>{escape(text)}</pre>'
        )


class Handler(BaseHTTPRequestHandler):
    server: Dashboard
    protocol_version = "HTTP/1.1"  # keep-alive; every response has a Content-Length

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        params = {k: v[-1] for k, v in parse_qs(url.query).items()}
        state = parse_state(params)
        parts = url.path.strip("/").split("/")
        if url.path == "/":
            self.send(200, self.server.page(state))
        elif url.path == "/rows":
            self.send(200, self.server.rows(state, params.get("after", "")))
        elif len(parts) == 2 and parts[0] == "pr" and parts[1].isdigit():
            tab = params.get("tab") if params.get("tab") in JOB_TITLES else "check"
            self.send(200, self.server.detail(int(parts[1]), tab, params.get("log") == "1"))
        else:
            self.send(404, "not found")

    def send(self, status: int, html: str) -> None:
        body = html.encode()
        gzipped = "gzip" in self.headers.get("Accept-Encoding", "")
        if gzipped:
            body = gzip.compress(body, 5)
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        if gzipped:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        if isinstance(code, int) and code >= 400:
            super().log_request(code, size)


def cmd_serve(args: argparse.Namespace) -> None:
    host, sep, port = args.listen.rpartition(":")
    if not sep or not port.isdigit():
        raise TriageError(f"--listen must be HOST:PORT, got {args.listen!r}")
    address = (host.strip("[]") or "0.0.0.0", int(port))
    snapshot = Snapshot()
    snapshot.current()
    sync = SyncLoop(args.sync_every) if args.sync_every > 0 else None
    server = Dashboard(address, snapshot, sync)

    def stop(*_) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    log(f"serving the dashboard on http://{args.listen}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("stopping")
    finally:
        server.server_close()
        if sync and sync.job and sync.job.running:
            sync.job.cancel()
            sync.job.thread.join(timeout=10)
