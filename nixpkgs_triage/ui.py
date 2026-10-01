"""Curses UI."""

from __future__ import annotations

import argparse
import curses
import json
import os
import sqlite3
import textwrap
import time
from collections.abc import Callable

from .categorize import Categorizer, load_categorizer
from .db import meta_get, open_db
from .jobs import (
    ACTIVE_JOB_STATES,
    JOB_TITLES,
    SPAWNED,
    cancel_job,
    job_describe,
    job_output,
    job_short,
    latest_jobs,
    output_lines,
    post_review,
    reap_jobs,
    start_job,
)
from .listing import (
    DEFAULT_FILTERS,
    FILTERS,
    SORTS,
    category_counts,
    filter_summary,
    load_open_rows,
    sorted_rows,
    visible_rows,
)
from .query import ci_label, review_marker
from .settings import SETTINGS, load_settings, save_setting, stored_settings
from .sync import SyncJob
from .util import TriageError, age, open_url, pr_url, since

PR_COLUMNS = ("PR", "age", "+/-", "CI", "conflict", "draft", "mark", "check", "nixrev", "title")
UI_PR_FORMAT = " {:<8} {:>4} {:<13} {:<7} {:<8} {:<5} {:<6.6} {:<7} {:<7} {}"

UI_HELP = (
    "tab/←→ pane  enter open  space details  c check  n nixpkgs-review  f filters  o sort  R refresh  "
    "S settings  q quit"
)
UI_FILTER_HELP = "↑↓ select  ←→/space change  r reset all  q back"
UI_SETTINGS_HELP = "↑↓ select  enter edit  r reset to default  q back"
UI_EDIT_HELP = "enter save  esc cancel  ←→ home end ctrl-u"
UI_DETAIL_HELP = (
    "c check  n nixpkgs-review  tab check/review  l log/report  x cancel  P post review  enter open  q back"
)

JOB_KEYS = {"check": "c", "review": "n"}
JOB_POLL_SECONDS = 2.0

BACK = (ord("q"), 27)
ENTER = (10, 13, curses.KEY_ENTER)
UP = (curses.KEY_UP, ord("k"))
DOWN = (curses.KEY_DOWN, ord("j"))
HOME = (curses.KEY_HOME, ord("g"))
END = (curses.KEY_END, ord("G"))
PAGE = (curses.KEY_PPAGE, curses.KEY_NPAGE)


class TriageUI:
    def __init__(self, scr: curses.window, db: sqlite3.Connection, cat: Categorizer):
        self.scr = scr
        self.db = db
        self.cat = cat
        self.focus = "cats"
        self.view = "list"
        self.filters = dict(DEFAULT_FILTERS)
        self.filter_idx = 0
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
        # pr, tab, log (instead of report), scroll (None = auto); draw_detail adds scroll_now, scroll_max, height
        self.detail: dict | None = None
        self.confirm: tuple[str, Callable[[], None]] | None = None
        self.settings_idx = 0
        # {"key", "label", "buffer", "pos"} while a setting is being edited
        self.edit: dict | None = None

        curses.curs_set(0)
        scr.keypad(True)
        scr.timeout(250)
        self.red = self.green = self.yellow = curses.A_NORMAL
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            for pair, color in enumerate((curses.COLOR_RED, curses.COLOR_GREEN, curses.COLOR_YELLOW), 1):
                curses.init_pair(pair, color, -1)
            self.red, self.green, self.yellow = (curses.color_pair(pair) for pair in (1, 2, 3))
        self.load()

    def load(self) -> None:
        self.all_rows = load_open_rows(self.db)
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
        if self.all_rows and (self.filters["check"] != "any" or self.filters["review"] != "any"):
            self.apply()  # job filters may now match different PRs

    def apply(self) -> None:
        """Recompute category counts and the PR list, keeping the selected PR if it is still listed."""
        keep = self.prs[self.pr_idx]["number"] if self.prs else None
        visible = visible_rows(self.all_rows, self.jobs, self.filters)
        self.visible_count = len(visible)
        self.categories = category_counts(self.cat.names, visible)
        self.cat_idx = min(self.cat_idx, len(self.categories) - 1)
        selected = self.categories[self.cat_idx][0]
        prs = visible if selected == "all" else [r for r in visible if r["category"] == selected]
        self.prs = sorted_rows(prs, SORTS[self.sort_idx][0])
        numbers = [r["number"] for r in self.prs]
        self.pr_idx = numbers.index(keep) if keep in numbers else min(self.pr_idx, max(len(self.prs) - 1, 0))

    def put(self, y: int, x: int, text: str, width: int, attr: int = curses.A_NORMAL) -> None:
        if width <= 0:
            return
        try:
            self.scr.addnstr(y, x, text.ljust(width), width, attr)
        except curses.error:
            pass  # writing the bottom-right cell always "fails"

    def job_attr(self, job: sqlite3.Row | None) -> int:
        if job is None or job["status"] in ("pending", "cancelled"):
            return curses.A_DIM
        if job["status"] == "running":
            return self.yellow
        if job["status"] == "failed" or (job["kind"] == "check" and job["summary"] != "PASS"):
            return self.red
        return self.green

    def draw(self) -> None:
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        getattr(self, f"draw_{self.view}")(h, w)
        if self.confirm:
            self.put(h - 1, 0, f" {self.confirm[0]} [y/N]", w, curses.A_REVERSE | curses.A_BOLD)
        self.draw_edit(h, w)
        self.scr.refresh()

    def draw_filters(self, h: int, w: int) -> None:
        self.put(0, 0, " filters  (for the PR list and the category counts)", w, curses.A_REVERSE)
        label_w = max(len(label) for _, label, _ in FILTERS) + 2
        for i, (key, label, options) in enumerate(FILTERS):
            selected = i == self.filter_idx
            y = 2 + i
            self.put(y, 0, f"{'▶' if selected else ' '} {label:<{label_w}}", w, curses.A_BOLD if selected else 0)
            x = 3 + label_w
            for option in options:
                text = f" {option} "
                self.put(y, x, text, len(text), curses.A_REVERSE if option == self.filters[key] else curses.A_NORMAL)
                x += len(text) + 1
        footer = f"   {self.visible_count} of {len(self.all_rows)} open PRs match"
        self.put(3 + len(FILTERS), 0, footer, w, curses.A_DIM)
        self.footer(h, w, UI_FILTER_HELP)

    def draw_settings(self, h: int, w: int) -> None:
        self.put(0, 0, " settings  (stored in triage.db; jobs read them when they start running)", w, curses.A_REVERSE)
        values, stored = load_settings(self.db), stored_settings(self.db)
        label_w = max(len(s.label) for s in SETTINGS) + 2
        for i, s in enumerate(SETTINGS):
            value = values[s.key] or "(empty)"
            origin = "" if s.key in stored else "   (default)"
            selected = i == self.settings_idx
            line = f"{'▶' if selected else ' '} {s.label:<{label_w}} {value}{origin}"
            self.put(2 + i, 0, line, w, curses.A_REVERSE if selected else curses.A_NORMAL)
        self.put(3 + len(SETTINGS), 0, f"   {SETTINGS[self.settings_idx].help}", w, curses.A_DIM)
        self.footer(h, w, UI_EDIT_HELP if self.edit else UI_SETTINGS_HELP)

    def draw_edit(self, h: int, w: int) -> None:
        if not self.edit:
            curses.curs_set(0)
            return
        e = self.edit
        prefix = f" {e['label']}: "
        avail = max(1, w - len(prefix) - 1)
        start = max(0, e["pos"] - avail + 1)  # scroll long values so the cursor stays visible
        self.put(h - 2, 0, prefix + e["buffer"][start : start + avail], w, curses.A_BOLD)
        curses.curs_set(1)
        self.scr.move(h - 2, len(prefix) + e["pos"] - start)

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
            f" nixpkgs-triage  {self.visible_count} of {len(self.all_rows)} open  sort: {SORTS[self.sort_idx][0]}  "
            f"filters: {filter_summary(self.filters)}  last sync: {self.last_sync or 'never'}"
        )
        self.put(0, 0, header, w, curses.A_REVERSE)

        left_w = min(max(len(n) for n, _ in self.categories) + 9, w // 3)
        self.put(1, 0, f" {'category':<{left_w - 9}} {'PRs':>6} ", left_w, curses.A_BOLD | curses.A_UNDERLINE)
        self.cat_top = min(max(self.cat_top, self.cat_idx - body + 1), self.cat_idx)
        for i, (name, count) in enumerate(self.categories[self.cat_top : self.cat_top + body]):
            attr = curses.A_NORMAL
            if self.cat_top + i == self.cat_idx:
                attr = curses.A_REVERSE if self.focus == "cats" else curses.A_BOLD
            elif count == 0:
                attr = curses.A_DIM
            self.put(2 + i, 0, f" {name:<{left_w - 9}.{left_w - 9}} {count:>6} ", left_w, attr)
        try:
            self.scr.vline(1, left_w, curses.ACS_VLINE, body + 1)
        except curses.error:
            pass

        x = left_w + 1
        pw = w - x
        if pw > 0:
            self.put(1, x, UI_PR_FORMAT.format(*PR_COLUMNS), pw, curses.A_BOLD | curses.A_UNDERLINE)
            self.pr_top = min(max(self.pr_top, self.pr_idx - body + 1), self.pr_idx)
            if not self.prs:
                self.put(2, x, " no PRs in this category", pw, curses.A_DIM)
            for i, r in enumerate(self.prs[self.pr_top : self.pr_top + body]):
                line = UI_PR_FORMAT.format(
                    f"#{r['number']}",
                    age(r["created_at"]),
                    f"+{r['additions']}/-{r['deletions']}",
                    ci_label(r["ci_state"]),
                    "yes" if r["conflict"] else "",
                    "yes" if r["is_draft"] else "",
                    review_marker(r),
                    job_short(self.jobs.get((r["number"], "check"))),
                    job_short(self.jobs.get((r["number"], "review"))),
                    r["title"],
                )
                attr = curses.A_NORMAL
                if r["is_draft"]:
                    attr = curses.A_DIM
                if r["ci_state"] in ("FAILURE", "ERROR"):
                    attr = self.red
                if self.pr_top + i == self.pr_idx:
                    attr = curses.A_REVERSE if self.focus == "prs" else curses.A_BOLD
                self.put(2 + i, x, line, pw, attr)
        self.footer(h, w, UI_HELP)

    def detail_output(self, job: sqlite3.Row | None, width: int) -> tuple[str, list[str], bool]:
        """Title, lines and whether it is a log: the report once finished, otherwise the live log."""
        kind = self.detail["tab"]
        if job is None:
            return f"{JOB_TITLES[kind]}: no output", ["", f"not run yet, press {JOB_KEYS[kind]} to start it"], False
        path, use_log = job_output(job, self.detail["log"])
        if not path.exists():
            return f"{JOB_TITLES[kind]}: {path}", ["", "(no output yet)"], use_log
        lines = output_lines(path)
        if not use_log:
            lines = [part for raw in lines for part in textwrap.wrap(raw, width, drop_whitespace=False) or [""]]
        what = ("live log" if job["status"] in ACTIVE_JOB_STATES else "log") if use_log else "report"
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
            f"size: +{pr['additions']}/-{pr['deletions']} in {pr['changed_files']} files   "
            f"CI: {ci_label(pr['ci_state'])}   comments: {pr['comments']}   mark: {review_marker(pr) or '-'}",
            f"labels: {', '.join(json.loads(pr['labels'])) or '-'}",
        ]
        for y, line in enumerate(info, 1):
            self.put(y, 0, " " + line, w)
        y = len(info) + 2
        for kind, key in JOB_KEYS.items():
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
        # scroll None follows logs and starts reports at the top
        scroll = (max_scroll if is_log else 0) if d["scroll"] is None else min(d["scroll"], max_scroll)
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

    def post(self, number: int) -> None:
        self.message = "posting…"
        self.draw()
        try:
            self.message = f"posted {post_review(self.db, number)}"
        except TriageError as e:
            self.message = str(e)
        except Exception as e:  # network / GitHub errors are shown, not fatal for the UI
            self.message = f"posting failed: {e}"
        self.poll_jobs(force=True)

    def handle_filters_key(self, key: int) -> None:
        fkey, _, options = FILTERS[self.filter_idx]
        if key in (*BACK, ord("f")):
            self.view = "list"
        elif key in UP + DOWN:
            self.filter_idx = max(0, min(len(FILTERS) - 1, self.filter_idx + (1 if key in DOWN else -1)))
        elif key in (curses.KEY_LEFT, ord("h"), curses.KEY_RIGHT, ord("l"), ord(" "), *ENTER):
            step = -1 if key in (curses.KEY_LEFT, ord("h")) else 1
            self.filters[fkey] = options[(options.index(self.filters[fkey]) + step) % len(options)]
            self.apply()
        elif key == ord("r"):
            self.filters = dict(DEFAULT_FILTERS)
            self.apply()

    def handle_settings_key(self, key: int) -> None:
        setting = SETTINGS[self.settings_idx]
        if key in BACK:
            self.view = "list"
        elif key in UP + DOWN:
            self.settings_idx = max(0, min(len(SETTINGS) - 1, self.settings_idx + (1 if key in DOWN else -1)))
        elif key in ENTER:
            value = load_settings(self.db)[setting.key]
            self.edit = {"key": setting.key, "label": setting.label, "buffer": value, "pos": len(value)}
        elif key == ord("r"):
            save_setting(self.db, setting.key, None)
            self.message = f"{setting.label} reset to default"

    def handle_edit_key(self, key: str | int) -> None:
        e = self.edit
        buf, pos = e["buffer"], e["pos"]
        if key in ("\n", "\r", curses.KEY_ENTER):
            try:
                warning = save_setting(self.db, e["key"], buf)
            except TriageError as err:
                self.message = str(err)  # keep editing so it can be fixed
                return
            self.message = f"saved {e['label']}" + (f", but {warning}" if warning else "")
            self.edit = None
            return
        if key == "\x1b":
            self.edit = None
        elif key in ("\x7f", "\b", curses.KEY_BACKSPACE) and pos > 0:
            buf, pos = buf[: pos - 1] + buf[pos:], pos - 1
        elif key == curses.KEY_DC:
            buf = buf[:pos] + buf[pos + 1 :]
        elif key == curses.KEY_LEFT:
            pos = max(0, pos - 1)
        elif key == curses.KEY_RIGHT:
            pos = min(len(buf), pos + 1)
        elif key in (curses.KEY_HOME, "\x01"):
            pos = 0
        elif key in (curses.KEY_END, "\x05"):
            pos = len(buf)
        elif key == "\x15":  # ctrl-u
            buf, pos = buf[pos:], 0
        elif isinstance(key, str) and key.isprintable():
            buf, pos = buf[:pos] + key + buf[pos:], pos + 1
        if self.edit:
            e["buffer"], e["pos"] = buf, pos

    def move(self, delta: int) -> None:
        if self.focus == "cats":
            self.cat_idx = max(0, min(len(self.categories) - 1, self.cat_idx + delta))
            self.prs, self.pr_idx, self.pr_top = [], 0, 0
            self.apply()
        else:
            self.pr_idx = max(0, min(len(self.prs) - 1, self.pr_idx + delta))

    def handle_list_key(self, key: int) -> None:
        selected = self.prs[self.pr_idx]["number"] if self.prs else None
        if key in UP + DOWN:
            self.move(1 if key in DOWN else -1)
        elif key in PAGE:
            page = self.scr.getmaxyx()[0] - 3
            self.move(page if key == curses.KEY_NPAGE else -page)
        elif key in HOME + END:
            self.move(10**9 if key in END else -(10**9))
        elif key in (9, curses.KEY_BTAB):
            self.focus = "prs" if self.focus == "cats" else "cats"
        elif key in (curses.KEY_RIGHT, ord("l")):
            self.focus = "prs"
        elif key in (curses.KEY_LEFT, ord("h")):
            self.focus = "cats"
        elif key in ENTER:
            if self.focus == "cats":
                self.focus = "prs"
            elif selected:
                open_url(pr_url(selected))
                self.message = f"opened {pr_url(selected)}"
        elif key in (ord(" "), ord("v")) and selected:
            self.open_detail(selected)
        elif key in (ord("c"), ord("n")) and selected:
            self.start_job(selected, "check" if key == ord("c") else "review")
        elif key == ord("f"):
            self.view = "filters"
        elif key == ord("o"):
            self.sort_idx = (self.sort_idx + 1) % len(SORTS)
            self.prs, self.pr_idx, self.pr_top = [], 0, 0  # a new order starts at the top
            self.apply()
        elif key == ord("R"):
            self.start_sync()
            self.view = "sync"
        elif key == ord("S"):
            self.view = "settings"

    def handle_detail_key(self, key: int) -> None:
        d = self.detail
        number = d["pr"]["number"]
        if key in BACK:
            self.view = "list"
            self.detail = None
        elif key in ENTER:
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
            self.post(number)
        elif key in UP + DOWN + PAGE:
            step = {curses.KEY_PPAGE: -d["height"], curses.KEY_NPAGE: d["height"]}.get(key, 1 if key in DOWN else -1)
            scroll = min(max(0, d["scroll_now"] + step), d["scroll_max"])
            # Scrolling down to the bottom resumes following a live log.
            d["scroll"] = None if step > 0 and scroll == d["scroll_max"] else scroll
        elif key in HOME:
            d["scroll"] = 0
        elif key in END:
            d["scroll"] = None

    def handle_sync_key(self, key: int) -> None:
        if key in BACK:
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
            if self.edit:
                try:
                    wide_key = self.scr.get_wch()  # str for text (incl. non-ASCII), int for special keys
                except curses.error:
                    continue
                if wide_key != curses.KEY_RESIZE:
                    self.message = ""
                    self.handle_edit_key(wide_key)
                continue
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
            if self.view == "list" and key in BACK:
                break
            getattr(self, f"handle_{self.view}_key")(key)
        if self.sync and self.sync.running:
            self.sync.cancel()
            self.sync.thread.join(timeout=10)


def cmd_ui(args: argparse.Namespace) -> None:
    db = open_db()
    cat = load_categorizer(db)
    os.environ.setdefault("ESCDELAY", "25")
    curses.wrapper(lambda scr: TriageUI(scr, db, cat).run())
