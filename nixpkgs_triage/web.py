"""Web dashboard: server-rendered pages from the database, plus a periodic `triage update`."""

from __future__ import annotations

import argparse
import gzip
import json
import re
import signal
import socket
import threading
import time
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, NamedTuple
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
from .listing import DEFAULT_FILTERS, FILTERS, Jobs, category_counts, load_open_rows, visible_rows
from .query import ci_label, review_marker
from .sync import SyncJob
from .util import TriageError, age, iso, log, parse_ts, pr_url, since, utcnow

PAGE_SIZE = 100


class Column(NamedTuple):
    name: str
    width: str  # default grid track
    cell: Callable[[dict, Jobs], tuple[str, object]]  # -> (css class, html)
    key: Callable[[dict, Jobs], object]  # sort key
    desc_first: bool  # the first click on the header sorts descending


def filled_first(value: str) -> tuple[bool, str]:
    return (value == "", value)


def job_column(name: str, kind: str) -> Column:
    def short(r: dict, jobs: Jobs) -> str:
        return job_short(jobs.get((r["number"], kind)))

    return Column(name, "4.5em", lambda r, j: (short(r, j), short(r, j)), lambda r, j: filled_first(short(r, j)), False)


def names_column(name: str, field: str, css: str) -> Column:
    def names(r: dict) -> list[str]:
        return json.loads(r[field] or "[]")

    return Column(name, "8.6em", lambda r, j: (css, escape(", ".join(names(r)))), lambda r, j: len(names(r)), True)


COLUMNS = (
    Column("PR", "5.6em", lambda r, j: ("", f"#{r['number']}"), lambda r, j: r["number"], True),
    # Ages sort by time since: ascending is the newest first.
    Column(
        "age",
        "4em",
        lambda r, j: ("", age(r["created_at"])),
        lambda r, j: -parse_ts(r["created_at"]).timestamp(),
        True,
    ),
    Column(
        "updated",
        "5.6em",
        lambda r, j: ("", since(r["updated_at"])),
        lambda r, j: -parse_ts(r["updated_at"]).timestamp(),
        False,
    ),
    Column(
        "+/-",
        "7.6em",
        lambda r, j: ("", f'<span class="add">+{r["additions"]}</span>/<span class="del">-{r["deletions"]}</span>'),
        lambda r, j: r["additions"] + r["deletions"],
        True,
    ),
    Column(
        "CI",
        "5.1em",
        lambda r, j: ("failing" if r["ci_state"] in ("FAILURE", "ERROR") else "", ci_label(r["ci_state"])),
        lambda r, j: ci_label(r["ci_state"]),
        False,
    ),
    Column("conflict", "5.1em", lambda r, j: ("", "yes" if r["conflict"] else ""), lambda r, j: r["conflict"], True),
    Column("draft", "3.6em", lambda r, j: ("", "yes" if r["is_draft"] else ""), lambda r, j: r["is_draft"], True),
    Column(
        "mark", "4.1em", lambda r, j: ("", escape(review_marker(r))), lambda r, j: filled_first(review_marker(r)), False
    ),
    job_column("check", "check"),
    job_column("nixrev", "review"),
    Column("likes", "3.6em", lambda r, j: ("", r["likes"] or ""), lambda r, j: r["likes"], True),
    names_column("approved by", "approvals", "pass"),
    names_column("blocked by", "blocking", "failing"),
    Column(
        "author",
        "8.6em",
        lambda r, j: ("author", escape(r["author"] or "")),
        lambda r, j: (r["author"] or "").lower(),
        False,
    ),
    Column("category", "11em", lambda r, j: ("dim", escape(r["category"])), lambda r, j: r["category"], False),
    Column("title", "minmax(12em, 1fr)", lambda r, j: ("", escape(r["title"])), lambda r, j: r["title"].lower(), False),
)
COLUMN = {c.name: c for c in COLUMNS}
DEFAULTS = {"category": "all", "sort": "-age", **DEFAULT_FILTERS, "q": ""}
OPTIONS = {"sort": [p + c.name for c in COLUMNS for p in ("", "-")], **{key: options for key, _, options in FILTERS}}
# GitHub reaction names
EMOJI = {
    "THUMBS_UP": "👍",
    "HEART": "❤️",
    "HOORAY": "🎉",
    "ROCKET": "🚀",
    "LAUGH": "😄",
    "EYES": "👀",
    "CONFUSED": "😕",
    "THUMBS_DOWN": "👎",
}

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
input[type=search] { width: 20em; }
summary, .head { display: grid; padding: .15em 0; white-space: nowrap;
  border-bottom: 1px solid color-mix(in srgb, GrayText 30%, transparent); }
summary { list-style: none; }
summary:hover { background: color-mix(in srgb, GrayText 12%, Canvas); }
summary > a { color: inherit; text-decoration: none; overflow: hidden; text-overflow: ellipsis; padding-right: .6em; }
summary > a.author:hover { text-decoration: underline; }
.head { position: sticky; top: 2.6em; z-index: 1; background: Canvas; font-weight: bold; }
.head > a { position: relative; color: inherit; text-decoration: none; overflow: hidden; text-overflow: ellipsis;
  padding-right: .6em; }
.grip { position: absolute; top: 0; right: 0; width: 5px; height: 100%; cursor: col-resize; }
.grip:hover { background: GrayText; }
.fold { cursor: pointer; text-align: center; color: GrayText; }
.fold::before { content: "▸"; }
details[open] .fold::before { content: "▾"; }
.draft, .dim { color: GrayText; }
.add, .pass { color: var(--add); }
.del, .FAIL, .issues, .failing { color: var(--del); }
.running { color: orange; }
.pane { padding: .5em 1em 1em; background: color-mix(in srgb, GrayText 10%, Canvas); }
.pane p { margin: .2em 0; }
pre { max-height: 32em; overflow: auto; padding: .5em; border: 1px solid GrayText; white-space: pre-wrap; }
pre.log { white-space: pre; }
"""

# Column order and widths live in localStorage; applied in <head>, before the rows render.
LAYOUT_JS = (
    f"const COLS = {json.dumps([[c.name, c.width] for c in COLUMNS])}, NAMES = COLS.map((c) => c[0]);"
    + """
const layout = () => {
  const saved = JSON.parse(localStorage.getItem("cols") || "{}");
  const order = (saved.order || []).filter((n) => NAMES.includes(n));
  NAMES.forEach((n, i) => order.includes(n) || order.splice(i, 0, n));
  return { order, widths: saved.widths || {} };
};
const applyLayout = ({ order, widths }) => {
  const width = (n) => widths[n] || COLS[NAMES.indexOf(n)][1];
  document.getElementById("cols").textContent =
    `summary, .head { grid-template-columns: 1.6em ${order.map(width).join(" ")}; }` +
    order.map((n, i) => `.c${NAMES.indexOf(n)} { order: ${i + 1}; }`).join("");
};
const saveLayout = (l) => (localStorage.setItem("cols", JSON.stringify(l)), applyLayout(l));
applyLayout(layout());
"""
)

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
// Live search: replace the rows as you type; the newest response wins.
const search = document.querySelector("input[name=q]");
let searchTimer, searchSeq = 0;
search.addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(async () => {
    const seq = ++searchSeq, params = new URLSearchParams(new FormData(search.form));
    history.replaceState(null, "", "?" + params);
    const html = await (await fetch("/rows?" + params)).text();
    if (seq !== searchSeq) return;
    document.getElementById("rows").innerHTML = html || "<p class=dim>no PRs match</p>";
    const next = document.querySelector("a.more");
    if (next) more.observe(next);
  }, 150);
});
// Header: drag a column onto another to reorder, drag its right edge to resize; the order and widths are saved.
const head = document.querySelector(".head");
let dragged = null;
head.addEventListener("dragstart", (e) => {
  if (e.target.closest(".grip")) return e.preventDefault();
  dragged = e.target.closest("[data-col]").dataset.col;
  e.dataTransfer.effectAllowed = "move";
  e.dataTransfer.setData("text/plain", dragged);
});
document.addEventListener("dragover", (e) => dragged && e.preventDefault());
document.addEventListener("drop", (e) => {
  if (!dragged) return;
  e.preventDefault();
  const target = e.target.closest(".head [data-col]");
  if (!target || target.dataset.col === dragged) return;
  const l = layout(), box = target.getBoundingClientRect();
  l.order.splice(l.order.indexOf(dragged), 1);
  l.order.splice(l.order.indexOf(target.dataset.col) + (e.clientX > box.left + box.width / 2), 0, dragged);
  saveLayout(l);
});
document.addEventListener("dragend", () => (dragged = null));
head.addEventListener("pointerdown", (e) => {
  const grip = e.target.closest(".grip");
  if (!grip) return;
  e.preventDefault();
  grip.setPointerCapture(e.pointerId);
  const l = layout(), name = grip.parentNode.dataset.col, x = e.clientX;
  const width = grip.parentNode.getBoundingClientRect().width;
  grip.onpointermove = (m) => ((l.widths[name] = Math.max(24, width + m.clientX - x) + "px"), applyLayout(l));
  grip.onpointerup = () => ((grip.onpointermove = grip.onpointerup = null), saveLayout(l));
});
head.addEventListener("click", (e) => e.target.closest(".grip") && e.preventDefault());
head.addEventListener("dblclick", (e) => {
  const grip = e.target.closest(".grip");
  if (grip) {
    const l = layout();
    delete l.widths[grip.parentNode.dataset.col];
    saveLayout(l);
  }
});
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

    def changed_since(self, since: str) -> dict:
        """PR rows synced at or after `since` (all when empty), for `triage update` on clients."""
        with self.lock:
            now = iso(utcnow())
            rows = self.db.execute("SELECT * FROM prs WHERE synced_at >= ?", (since,)).fetchall()
            last_sync = meta_get(self.db, "last_sync")
        prs = [{k: r[k] for k in r.keys() if k != "seen_run"} for r in rows]
        return {"now": now, "last_sync": last_sync, "prs": prs}


class SyncLoop(threading.Thread):
    """`triage update` every `interval` seconds after the previous run ended."""

    def __init__(self, interval: float) -> None:
        super().__init__(daemon=True)
        self.interval = interval
        self.job: SyncJob | None = None
        self.start()

    def run(self) -> None:
        while True:
            self.job = SyncJob("--github", echo=True)  # the server is where the data comes from
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
        if k in ("category", "q") or v in OPTIONS.get(k, ()):
            state[k] = v
    return state


def query(state: dict, **change) -> str:
    return urlencode({k: v for k, v in {**state, **change}.items() if v != DEFAULTS.get(k)})


def fuzzy(q: str):
    """Each word of q must match one word of '#number title author': its letters in order, gaps allowed."""
    patterns = [re.compile(r"\S*?".join(map(re.escape, word)), re.I) for word in q.split()]
    return lambda r: all(p.search(f"#{r['number']} {r['title']} {r['author']}") for p in patterns)


def row_html(r: dict, jobs: Jobs) -> str:
    n, author = r["number"], r["author"] or ""
    # Every cell links to the PR, except the author's, which links to their profile.
    profile = f"https://github.com/apps/{author[:-5]}" if author.endswith("[bot]") else f"https://github.com/{author}"
    cells = "".join(
        f'<a class="c{i} {css}" href="{escape(profile) if css == "author" and author else pr_url(n)}" '
        f'target="_blank">{html}</a>'
        for i, (css, html) in enumerate(column.cell(r, jobs) for column in COLUMNS)
    )
    return (
        f'<details data-n="{n}"><summary class="{"draft" if r["is_draft"] else ""}">'
        f'<span class="fold" title="details"></span>{cells}</summary><div class="pane">loading…</div></details>'
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

    def listing(self, state: dict, generation: int | None = None) -> tuple[Data, int, list, list, list]:
        """Filtered and sorted open PRs of `generation` while it is cached (so paging stays consistent), else current."""
        data = self.snapshot.current()
        filters = tuple((k, state[k]) for k in DEFAULT_FILTERS)
        key = (generation, filters, state["sort"])
        if key not in self.lists:
            key = (data.generation, filters, state["sort"])
        if key not in self.lists:
            if len(self.lists) > 32:
                self.lists.clear()
            visible = visible_rows(data.rows, data.jobs, dict(filters))
            column = COLUMN[state["sort"].lstrip("-")]
            ordered = sorted(
                visible, key=lambda r: (column.key(r, data.jobs), r["number"]), reverse=state["sort"].startswith("-")
            )
            self.lists[key] = (visible, ordered, category_counts(self.snapshot.category_names, visible))
        return data, key[0], *self.lists[key]

    def rows(self, state: dict, offset: int = 0, generation: int | None = None) -> str:
        """PAGE_SIZE rows from offset, plus the link to the next page (of the same data generation)."""
        data, generation, _, prs, _ = self.listing(state, generation)
        if state["category"] != "all":
            prs = [r for r in prs if r["category"] == state["category"]]
        if state["q"].strip():
            prs = list(filter(fuzzy(state["q"]), prs))
        html = "".join(row_html(r, data.jobs) for r in prs[offset : offset + PAGE_SIZE])
        if state["q"].strip() and not offset:
            html = f'<p class="dim">{len(prs)} matching “{escape(state["q"])}”</p>' + html
        if offset + PAGE_SIZE < len(prs):
            html += f'<a class="more" href="/rows?{query(state, offset=offset + PAGE_SIZE, gen=generation)}">more</a>'
        return html

    def page(self, state: dict) -> str:
        data, _, visible, _, counts = self.listing(state)
        cats = "".join(
            f'<a class="{"sel" if name == state["category"] else ""}" href="/?{query(state, category=name)}">'
            f"{escape(name)}<span>{n}</span></a>"
            for name, n in counts
        )
        selects = "".join(
            f'<label>{label} <select name="{key}" onchange="this.form.submit()">'
            + "".join(f"<option{' selected' if o == state[key] else ''}>{o}</option>" for o in options)
            + "</select></label>"
            for key, label, options in FILTERS
        )
        sort, desc = state["sort"].lstrip("-"), state["sort"].startswith("-")

        def header(i: int, c: Column) -> str:
            # A click sorts by the column, a second click reverses.
            if c.name == sort:
                new, arrow = ("" if desc else "-") + c.name, " ▼" if desc else " ▲"
            else:
                new, arrow = ("-" if c.desc_first else "") + c.name, ""
            return (
                f'<a class="c{i}" data-col="{escape(c.name)}" draggable="true" href="/?{query(state, sort=new)}">'
                f'{escape(c.name)}{arrow}<i class="grip" title="drag to resize, double-click to reset"></i></a>'
            )

        status = [f"{len(visible)} of {len(data.rows)} open PRs match"]
        status.append(f"last sync {since(data.last_sync)} ago" if data.last_sync else "never synced")
        if self.sync and self.sync.status():
            status.append(escape(self.sync.status()))
        return (
            f'<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
            f'<title>nixpkgs-triage</title><link rel="icon" href="data:,"><style>{CSS}</style>'
            f'<style id="cols"></style><script>{LAYOUT_JS}</script>'
            f"<header><b>nixpkgs-triage</b>{''.join(f'<span>{s}</span>' for s in status)}</header>"
            f"<nav>{cats}</nav><main>"
            f'<form><input type="hidden" name="category" value="{escape(state["category"])}">'
            f'<input type="hidden" name="sort" value="{escape(state["sort"])}">'
            f'<input type="search" name="q" value="{escape(state["q"])}" placeholder="fuzzy search" autofocus>'
            f'{selects}<button type="button" onclick="localStorage.removeItem(\'cols\'), applyLayout(layout())">'
            f"reset columns</button></form>"
            f'<div class="head"><span></span>{"".join(header(i, c) for i, c in enumerate(COLUMNS))}</div>'
            f'<div id="rows">{self.rows(state) or "<p class=dim>no PRs match</p>"}</div></main><script>{JS}</script>'
        )

    def detail(self, number: int, tab: str, want_log: bool) -> str:
        pr = self.snapshot.pr(number)
        if pr is None:
            return "<p>not in the database</p>"
        jobs = self.snapshot.current().jobs

        def joined(column: str) -> str:
            return escape(", ".join(json.loads(pr[column] or "[]"))) or "-"

        lines = [
            f'<a href="{pr_url(number)}" target="_blank">{pr_url(number)}</a>',
            f"{pr['state'].lower()}{' draft' if pr['is_draft'] else ''} → {escape(pr['base_ref'])}, "
            f"by {escape(pr['author'] or '')}, opened {age(pr['created_at'])} ago, "
            f"updated {since(pr['updated_at'])} ago",
            f"tags: {joined('tags')} · topics: {joined('topics')}",
            f"+{pr['additions']} -{pr['deletions']} in {pr['changed_files']} files · {pr['comments']} comments",
            f"approved by: {joined('approvals')} · changes requested by: {joined('blocking')}",
            "reactions: "
            + (
                " ".join(f"{EMOJI[k]} {n}" for k, n in json.loads(pr["reactions"] or "{}").items() if k in EMOJI) or "-"
            ),
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
        elif url.path == "/api/prs":
            self.send(200, json.dumps(self.server.snapshot.changed_since(params.get("since", ""))), "application/json")
        elif url.path == "/rows":
            offset, generation = params.get("offset", ""), params.get("gen", "")
            rows = self.server.rows(
                state, int(offset) if offset.isdigit() else 0, int(generation) if generation.isdigit() else None
            )
            self.send(200, rows)
        elif len(parts) == 2 and parts[0] == "pr" and parts[1].isdigit():
            tab = params.get("tab") if params.get("tab") in JOB_TITLES else "check"
            self.send(200, self.server.detail(int(parts[1]), tab, params.get("log") == "1"))
        else:
            self.send(404, "not found")

    def send(self, status: int, text: str, content_type: str = "text/html; charset=utf-8") -> None:
        body = text.encode()
        gzipped = "gzip" in self.headers.get("Accept-Encoding", "")
        if gzipped:
            body = gzip.compress(body, 5)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
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
