"""Curses UI."""

from __future__ import annotations

import argparse
import curses
import json
import os
import signal
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from .categorize import Categorizer, load_categorizer
from .config import ENTRY
from .db import meta_get, open_db
from .jobs import (
    ACTIVE_JOB_STATES,
    JOB_TITLES,
    SPAWNED,
    cancel_job,
    job_describe,
    job_short,
    latest_jobs,
    post_review,
    reap_jobs,
    review_report,
    start_job,
)
from .query import ci_label, review_marker
from .util import TriageError, age, open_url, pr_url, since


class SyncJob:
    """`triage update` in a child process; its log lines are collected for the refresh view."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.proc = subprocess.Popen(
            [sys.executable, str(ENTRY), "update"],
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
        self.proc.wait()

    @property
    def running(self) -> bool:
        return self.thread.is_alive()

    def cancel(self) -> None:
        # SIGINT takes the "interrupted, progress is saved" path; every page is committed.
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)


# (label, column, descending)
UI_SORTS = (("oldest", "created_at", False), ("newest", "created_at", True), ("updated", "updated_at", True))

# PR, age, CI, draft, mark, check, nixrev, title
UI_PR_FORMAT = " {:<8} {:>4} {:<7} {:<5} {:<6.6} {:<7} {:<7} {}"

UI_HELP = "tab/←→ pane  enter open  space details  c check  n nixpkgs-review  d drafts  o sort  R refresh  q quit"

UI_DETAIL_HELP = (
    "c check  n nixpkgs-review  tab switch output  l log/report  x cancel  P post review  enter open  q back"
)

JOB_POLL_SECONDS = 2.0

LOG_TAIL_BYTES = 512 * 1024


def read_tail(path: Path, limit: int) -> str:
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - limit))
        return f.read().decode(errors="replace")


class TriageUI:
    def __init__(self, scr: curses.window, db: sqlite3.Connection, cat: Categorizer):
        self.scr = scr
        self.db = db
        self.cat = cat
        self.focus = "cats"
        self.view = "list"
        self.show_drafts = False
        self.sort_idx = 0
        self.cat_idx = 0
        self.cat_top = 0
        self.pr_idx = 0
        self.pr_top = 0
        self.sync: SyncJob | None = None
        self.sync_reloaded = True
        self.message = ""
        self.all_rows: list[dict] = []
        self.categories: list[tuple[str, int]] = []
        self.prs: list[dict] = []
        self.visible_count = 0
        self.last_sync = None
        self.jobs: dict[tuple[int, str], sqlite3.Row] = {}
        self.jobs_polled = 0.0
        # detail view state: pr row, output tab ("check"/"review"), log instead of report, scroll (None = auto)
        self.detail: dict | None = None
        self.confirm: tuple[str, Callable[[], None]] | None = None

        curses.curs_set(0)
        scr.keypad(True)
        scr.timeout(250)
        self.red = self.green = self.yellow = curses.A_NORMAL
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_RED, -1)
            curses.init_pair(2, curses.COLOR_GREEN, -1)
            curses.init_pair(3, curses.COLOR_YELLOW, -1)
            self.red = curses.color_pair(1)
            self.green = curses.color_pair(2)
            self.yellow = curses.color_pair(3)
        self.dim = curses.A_DIM
        self.load()

    # data

    def load(self) -> None:
        rows = self.db.execute(
            "SELECT p.number, p.title, p.author, p.category, p.is_draft, p.created_at, p.updated_at, "
            "p.ci_state, p.additions, p.deletions, r.status AS review_status, r.pr_updated_at AS reviewed_version "
            "FROM prs p LEFT JOIN reviews r ON r.number = p.number WHERE p.state = 'OPEN'"
        ).fetchall()
        self.all_rows = [dict(r) for r in rows]
        self.last_sync = meta_get(self.db, "last_sync")
        if self.detail:
            self.detail["pr"] = self.load_pr(self.detail["pr"]["number"])
        self.poll_jobs(force=True)
        self.apply()

    def load_pr(self, number: int) -> sqlite3.Row:
        return self.db.execute(
            "SELECT p.*, r.status AS review_status, r.pr_updated_at AS reviewed_version "
            "FROM prs p LEFT JOIN reviews r ON r.number = p.number WHERE p.number = ?",
            (number,),
        ).fetchone()

    def poll_jobs(self, force: bool = False) -> None:
        if not force and time.monotonic() - self.jobs_polled < JOB_POLL_SECONDS:
            return
        self.jobs_polled = time.monotonic()
        for proc in SPAWNED[:]:
            if proc.poll() is not None:
                SPAWNED.remove(proc)
        reap_jobs(self.db)
        self.jobs = latest_jobs(self.db)

    def apply(self) -> None:
        """Recompute category counts and the PR list, keeping the selected PR if it is still listed."""
        keep = self.prs[self.pr_idx]["number"] if self.prs else None
        visible = [r for r in self.all_rows if self.show_drafts or not r["is_draft"]]
        self.visible_count = len(visible)
        counts = Counter(r["category"] for r in visible)
        names = self.cat.names + sorted(set(counts) - set(self.cat.names))
        self.categories = [("all", len(visible))] + [(n, counts.get(n, 0)) for n in names]
        self.cat_idx = min(self.cat_idx, len(self.categories) - 1)
        selected = self.categories[self.cat_idx][0]
        prs = visible if selected == "all" else [r for r in visible if r["category"] == selected]
        _, column, descending = UI_SORTS[self.sort_idx]
        prs.sort(key=lambda r: r[column], reverse=descending)
        self.prs = prs
        numbers = [r["number"] for r in prs]
        self.pr_idx = numbers.index(keep) if keep in numbers else min(self.pr_idx, max(len(prs) - 1, 0))

    # drawing

    def put(self, y: int, x: int, text: str, width: int, attr: int = curses.A_NORMAL) -> None:
        if width <= 0:
            return
        try:
            self.scr.addnstr(y, x, text.ljust(width), width, attr)
        except curses.error:
            pass  # writing the bottom-right cell always "fails"

    def job_attr(self, job: sqlite3.Row | None) -> int:
        if job is None or job["status"] in ("pending", "cancelled"):
            return self.dim
        if job["status"] == "running":
            return self.yellow
        if job["status"] == "failed" or (job["kind"] == "check" and job["summary"] != "PASS"):
            return self.red
        return self.green

    def draw(self) -> None:
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        if self.view == "sync":
            self.draw_sync(h, w)
        elif self.view == "detail":
            self.draw_detail(h, w)
        else:
            self.draw_list(h, w)
        if self.confirm:
            self.put(h - 1, 0, f" {self.confirm[0]} [y/N]", w, curses.A_REVERSE | curses.A_BOLD)
        self.scr.refresh()

    def footer(self, h: int, w: int, help_text: str) -> None:
        text = help_text
        if self.sync and self.sync.running:
            text = f"syncing: {self.sync.lines[-1] if self.sync.lines else 'starting'}"
        if self.message:
            text = self.message
        self.put(h - 1, 0, " " + text, w, curses.A_REVERSE)

    def draw_list(self, h: int, w: int) -> None:
        body = h - 3  # title bar, column header, footer
        header = (
            f" nixpkgs-triage  {self.visible_count} open  sort: {UI_SORTS[self.sort_idx][0]}  "
            f"drafts: {'shown' if self.show_drafts else 'hidden'}  last sync: {self.last_sync or 'never'}"
        )
        self.put(0, 0, header, w, curses.A_REVERSE)

        left_w = min(max(len(n) for n, _ in self.categories) + 9, w // 3)
        self.put(1, 0, f" {'category':<{left_w - 9}} {'PRs':>6} ", left_w, curses.A_BOLD | curses.A_UNDERLINE)
        self.cat_top = min(max(self.cat_top, self.cat_idx - body + 1), self.cat_idx)
        for i, (name, count) in enumerate(self.categories[self.cat_top : self.cat_top + body]):
            idx = self.cat_top + i
            attr = curses.A_NORMAL
            if idx == self.cat_idx:
                attr = curses.A_REVERSE if self.focus == "cats" else curses.A_BOLD
            elif count == 0:
                attr = self.dim
            self.put(2 + i, 0, f" {name:<{left_w - 9}.{left_w - 9}} {count:>6} ", left_w, attr)
        try:
            self.scr.vline(1, left_w, curses.ACS_VLINE, body + 1)
        except curses.error:
            pass

        x = left_w + 1
        pw = w - x
        if pw > 0:
            self.put(
                1,
                x,
                UI_PR_FORMAT.format("PR", "age", "CI", "draft", "mark", "check", "nixrev", "title"),
                pw,
                curses.A_BOLD | curses.A_UNDERLINE,
            )
            self.pr_top = min(max(self.pr_top, self.pr_idx - body + 1), self.pr_idx)
            if not self.prs:
                self.put(2, x, " no PRs in this category", pw, self.dim)
            for i, r in enumerate(self.prs[self.pr_top : self.pr_top + body]):
                idx = self.pr_top + i
                line = UI_PR_FORMAT.format(
                    f"#{r['number']}",
                    age(r["created_at"]),
                    ci_label(r["ci_state"]),
                    "yes" if r["is_draft"] else "",
                    review_marker(r),
                    job_short(self.jobs.get((r["number"], "check"))),
                    job_short(self.jobs.get((r["number"], "review"))),
                    r["title"],
                )
                attr = curses.A_NORMAL
                if r["is_draft"]:
                    attr = self.dim
                if r["ci_state"] in ("FAILURE", "ERROR"):
                    attr = self.red
                if idx == self.pr_idx:
                    attr = curses.A_REVERSE if self.focus == "prs" else curses.A_BOLD
                self.put(2 + i, x, line, pw, attr)
        self.footer(h, w, UI_HELP)

    def detail_output(self, job: sqlite3.Row | None, width: int) -> tuple[str, list[str], bool]:
        """Title, lines and whether it is a log: the report once finished, otherwise the live log."""
        kind = self.detail["tab"]
        if job is None:
            key = "c" if kind == "check" else "n"
            return f"{JOB_TITLES[kind]}: no output", ["", f"not run yet, press {key} to start it"], False
        jobdir = Path(job["dir"])
        report, logfile = jobdir / "report.md", jobdir / "job.log"
        active = job["status"] in ACTIVE_JOB_STATES
        use_log = self.detail["log"] or active or not report.exists() or report.stat().st_size == 0
        path = logfile if use_log else report
        if not path.exists():
            return f"{JOB_TITLES[kind]}: {path}", ["", "(no output yet)"], use_log
        lines: list[str] = []
        for raw in read_tail(path, LOG_TAIL_BYTES).splitlines():
            raw = raw.rsplit("\r", 1)[-1].replace("\t", "    ")  # progress output: keep the final state
            if use_log:
                lines.append(raw)
            else:
                lines.extend(textwrap.wrap(raw, width, drop_whitespace=False) or [""])
        what = ("live log" if active else "log") if use_log else "report"
        return f"{JOB_TITLES[kind]} {what}: {path}", lines, use_log

    def draw_detail(self, h: int, w: int) -> None:
        d = self.detail
        pr = d["pr"]
        n = pr["number"]
        self.put(0, 0, f" #{n}  {pr['title']}", w, curses.A_REVERSE)
        info = [
            pr_url(n),
            f"{pr['state'].lower()}{' draft' if pr['is_draft'] else ''} → {pr['base_ref']}   "
            f"by {pr['author']} ({pr['author_association'].lower()})   "
            f"opened {age(pr['created_at'])} ago, updated {since(pr['updated_at'])} ago",
            f"category: {pr['category']}   tags: {', '.join(json.loads(pr['tags'])) or '-'}   "
            f"topics: {', '.join(json.loads(pr['topics'])) or '-'}",
            f"size: +{pr['additions']}/-{pr['deletions']} in {pr['files_total']} files   "
            f"CI: {ci_label(pr['ci_state'])}   comments: {pr['comments']}   mark: {review_marker(pr) or '-'}",
            f"labels: {', '.join(json.loads(pr['labels'])) or '-'}",
        ]
        y = 1
        for line in info:
            self.put(y, 0, " " + line, w)
            y += 1
        y += 1
        for kind, key in (("check", "c"), ("review", "n")):
            job = self.jobs.get((n, kind))
            selected = d["tab"] == kind
            line = f"{'▶' if selected else ' '} [{key}] {JOB_TITLES[kind]:<22} {job_describe(job)}"
            self.put(y, 0, line, w, self.job_attr(job) | (curses.A_BOLD if selected else 0))
            y += 1
        y += 1

        title, lines, is_log = self.detail_output(self.jobs.get((n, d["tab"])), w - 2)
        self.put(y, 0, f" {title}", w, curses.A_BOLD | curses.A_UNDERLINE)
        y += 1
        height = max(1, h - 1 - y)
        max_scroll = max(0, len(lines) - height)
        if d["scroll"] is None:  # follow logs, start reports at the top
            scroll = max_scroll if is_log else 0
        else:
            scroll = min(d["scroll"], max_scroll)
        d["scroll_now"], d["scroll_max"], d["height"] = scroll, max_scroll, height
        for i, line in enumerate(lines[scroll : scroll + height]):
            self.put(y + i, 0, " " + line, w)
        self.footer(h, w, UI_DETAIL_HELP)

    def draw_sync(self, h: int, w: int) -> None:
        job = self.sync
        if job is None:
            state = "no sync started"
        elif job.running:
            state = "running"
        else:
            state = f"finished (exit {job.proc.returncode})"
        self.put(0, 0, f" refresh: triage update  {state}", w, curses.A_REVERSE)
        lines = job.lines if job else []
        for i, line in enumerate(lines[-(h - 2) :]):
            attr = self.red if ("rror" in line or "Traceback" in line) else curses.A_NORMAL
            if "done in" in line:
                attr = self.green
            self.put(1 + i, 0, line, w, attr)
        keys = "esc/q back (sync keeps running)  c cancel" if job and job.running else "esc/q back  R run again"
        self.put(h - 1, 0, " " + keys, w, curses.A_REVERSE)

    # actions

    def start_sync(self) -> None:
        if self.sync and self.sync.running:
            return
        self.sync = SyncJob()
        self.sync_reloaded = False
        self.message = ""

    def start_job(self, number: int, kind: str) -> None:
        try:
            start_job(self.db, number, kind)
            self.message = f"started {JOB_TITLES[kind]} for #{number}"
        except TriageError as e:
            self.message = str(e)
        self.poll_jobs(force=True)

    def open_detail(self, number: int) -> None:
        jobs = latest_jobs(self.db, number)
        # Show the review output if that is the only thing that ran.
        tab = "review" if (number, "review") in jobs and (number, "check") not in jobs else "check"
        self.detail = {"pr": self.load_pr(number), "tab": tab, "log": False, "scroll": None}
        self.view = "detail"

    def ask_cancel(self, number: int, kind: str) -> None:
        job = self.jobs.get((number, kind))
        if job is None or job["status"] not in ACTIVE_JOB_STATES:
            self.message = f"no {JOB_TITLES[kind]} running for #{number}"
            return

        def do() -> None:
            try:
                cancel_job(self.db, job["id"])
                self.message = f"cancelling {JOB_TITLES[kind]} for #{number}"
            except TriageError as e:
                self.message = str(e)
            self.poll_jobs(force=True)

        self.confirm = (f"Cancel the {job['status']} {JOB_TITLES[kind]} for #{number}?", do)

    def ask_post(self, number: int) -> None:
        try:
            job, _ = review_report(self.db, number)
        except TriageError as e:
            self.message = str(e)
            return

        def do() -> None:
            self.message = "posting…"
            self.draw()
            try:
                self.message = f"posted {post_review(self.db, number)}"
            except Exception as e:  # network / GitHub errors are shown, not fatal for the UI
                self.message = f"posting failed: {e}"
            self.poll_jobs(force=True)

        again = f" (already posted {since(job['posted_at'])} ago)" if job["posted_at"] else ""
        self.confirm = (f"Post the nixpkgs-review report as a comment on #{number} on GitHub{again}?", do)

    # input

    def move(self, delta) -> None:
        h, _ = self.scr.getmaxyx()
        if delta in ("page_up", "page_down"):
            delta = (h - 3) * (1 if delta == "page_down" else -1)
        if self.focus == "cats":
            self.cat_idx = max(0, min(len(self.categories) - 1, self.cat_idx + delta))
            self.pr_idx = self.pr_top = 0
            self.prs = []
            self.apply()
        else:
            self.pr_idx = max(0, min(len(self.prs) - 1, self.pr_idx + delta))

    def handle_list_key(self, key: int) -> bool:
        selected = self.prs[self.pr_idx]["number"] if self.prs else None
        if key in (ord("q"), 27):
            return False
        if key in (curses.KEY_UP, ord("k")):
            self.move(-1)
        elif key in (curses.KEY_DOWN, ord("j")):
            self.move(1)
        elif key == curses.KEY_PPAGE:
            self.move("page_up")
        elif key == curses.KEY_NPAGE:
            self.move("page_down")
        elif key in (curses.KEY_HOME, ord("g")):
            self.move(-(10**9))
        elif key in (curses.KEY_END, ord("G")):
            self.move(10**9)
        elif key in (9, curses.KEY_BTAB):
            self.focus = "prs" if self.focus == "cats" else "cats"
        elif key in (curses.KEY_RIGHT, ord("l")):
            self.focus = "prs"
        elif key in (curses.KEY_LEFT, ord("h")):
            self.focus = "cats"
        elif key in (10, 13, curses.KEY_ENTER):
            if self.focus == "cats":
                self.focus = "prs"
            elif selected:
                open_url(pr_url(selected))
                self.message = f"opened {pr_url(selected)}"
        elif key in (ord(" "), ord("v")) and selected:
            self.open_detail(selected)
        elif key == ord("c") and selected:
            self.start_job(selected, "check")
        elif key == ord("n") and selected:
            self.start_job(selected, "review")
        elif key == ord("d"):
            self.show_drafts = not self.show_drafts
            self.apply()
        elif key == ord("o"):
            self.sort_idx = (self.sort_idx + 1) % len(UI_SORTS)
            # A new order starts at the top instead of scrolling to the previous selection.
            self.prs, self.pr_idx, self.pr_top = [], 0, 0
            self.apply()
        elif key == ord("R"):
            self.start_sync()
            self.view = "sync"
        return True

    def handle_detail_key(self, key: int) -> None:
        d = self.detail
        number = d["pr"]["number"]
        if key in (ord("q"), 27):
            self.view = "list"
            self.detail = None
        elif key in (10, 13, curses.KEY_ENTER):
            open_url(pr_url(number))
            self.message = f"opened {pr_url(number)}"
        elif key in (ord("c"), ord("n")):
            kind = "check" if key == ord("c") else "review"
            self.start_job(number, kind)
            d.update(tab=kind, log=False, scroll=None)
        elif key in (9, curses.KEY_BTAB):
            d.update(tab="review" if d["tab"] == "check" else "check", log=False, scroll=None)
        elif key == ord("l"):
            d.update(log=not d["log"], scroll=None)
        elif key == ord("x"):
            self.ask_cancel(number, d["tab"])
        elif key == ord("P"):
            self.ask_post(number)
        elif key in (curses.KEY_UP, ord("k"), curses.KEY_DOWN, ord("j"), curses.KEY_PPAGE, curses.KEY_NPAGE):
            step = {curses.KEY_PPAGE: -d["height"], curses.KEY_NPAGE: d["height"]}.get(key, 1)
            if key in (curses.KEY_UP, ord("k")):
                step = -1
            scroll = min(max(0, d["scroll_now"] + step), d["scroll_max"])
            # Scrolling down to the bottom resumes following a live log.
            d["scroll"] = None if step > 0 and scroll == d["scroll_max"] else scroll
        elif key in (curses.KEY_HOME, ord("g")):
            d["scroll"] = 0
        elif key in (curses.KEY_END, ord("G")):
            d["scroll"] = None

    def handle_sync_key(self, key: int) -> None:
        if key in (ord("q"), 27):
            self.view = "list"
        elif key == ord("c") and self.sync:
            self.sync.cancel()
        elif key == ord("R"):
            self.start_sync()

    def run(self) -> None:
        while True:
            if self.sync and not self.sync.running and not self.sync_reloaded:
                self.sync_reloaded = True
                self.load()
                code = self.sync.proc.returncode
                self.message = "refresh finished" if code == 0 else f"refresh failed (exit {code}), see R"
            self.poll_jobs()
            self.draw()
            key = self.scr.getch()
            if key in (-1, curses.KEY_RESIZE):
                continue
            self.message = ""
            if self.confirm:
                _, action = self.confirm
                self.confirm = None
                if key in (ord("y"), ord("Y")):
                    action()
                continue
            if self.view == "sync":
                self.handle_sync_key(key)
            elif self.view == "detail":
                self.handle_detail_key(key)
            elif not self.handle_list_key(key):
                break
        if self.sync and self.sync.running:
            self.sync.cancel()
            self.sync.thread.join(timeout=10)


def cmd_ui(args: argparse.Namespace) -> None:
    db = open_db()
    cat = load_categorizer(db)
    os.environ.setdefault("ESCDELAY", "25")
    curses.wrapper(lambda scr: TriageUI(scr, db, cat).run())
