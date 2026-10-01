"""Command line entry point."""

from __future__ import annotations

import argparse
import signal
import sys

import nixpkgs_triage

from .categorize import cmd_recategorize
from .jobs import cmd_cancel, cmd_jobs, cmd_post, cmd_start_jobs
from .query import REVIEW_STATUSES, add_filter_args, cmd_list, cmd_mark, cmd_next, cmd_show, cmd_stats
from .runner import cmd_job_run
from .settings import cmd_settings
from .sync import cmd_update
from .ui import cmd_ui
from .util import TriageError
from .web import cmd_serve


def main() -> None:
    # Piping into `head` etc. should end quietly, not with a BrokenPipeError traceback.
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    ap = argparse.ArgumentParser(prog="triage", description=nixpkgs_triage.__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("update", help="sync the local database with GitHub")
    p.add_argument("--full", action="store_true", help="re-walk all open PRs (first run does this automatically)")
    p.add_argument("--page-size", type=int, default=50, help="PRs per request; shrinks automatically on timeouts")
    p.add_argument("--delay", type=float, default=1.0, help="seconds between requests (secondary rate limit)")
    p.add_argument("--reserve", type=int, default=500, help="stop and wait for reset below this many points")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("stats", help="counts per category / topic")
    p.add_argument("--by", choices=("category", "topic", "author", "base_ref"), default="category")
    p.add_argument("--limit", type=int, default=40)
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("list", help="list open PRs")
    add_filter_args(p)
    p.add_argument("-n", "--limit", type=int, default=50)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("next", help="show the next PR to review in a queue")
    add_filter_args(p)
    p.add_argument("-o", "--open", action="store_true", help="open it in the browser")
    p.set_defaults(func=cmd_next)

    p = sub.add_parser("show", help="show one PR")
    p.add_argument("number", type=int)
    p.add_argument("-o", "--open", action="store_true", help="open it in the browser")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("mark", help="set local review status")
    p.add_argument("status", choices=(*REVIEW_STATUSES, "clear"))
    p.add_argument("numbers", type=int, nargs="+")
    p.add_argument("-m", "--note")
    p.set_defaults(func=cmd_mark)

    p = sub.add_parser("recategorize", help="re-apply categories.toml to all stored PRs")
    p.set_defaults(func=cmd_recategorize)

    p = sub.add_parser("ui", help="interactive curses browser (categories, open in browser, refresh)")
    p.set_defaults(func=cmd_ui)

    p = sub.add_parser("serve", help="web dashboard (read-only) with a background sync")
    p.add_argument("--listen", default="127.0.0.1:8080", help="HOST:PORT to listen on (default 127.0.0.1:8080)")
    p.add_argument(
        "--sync-every",
        type=float,
        default=300,
        help="seconds between `triage update` runs (default 300; 0 = don't sync, e.g. when a timer does it)",
    )
    p.set_defaults(func=cmd_serve)

    for kind, what in (("check", "guideline check with omp"), ("review", "nixpkgs-review")):
        p = sub.add_parser(kind, help=f"start a background {what} for PRs")
        p.add_argument("numbers", type=int, nargs="+")
        p.set_defaults(func=cmd_start_jobs, kind=kind)

    p = sub.add_parser("jobs", help="list background jobs (active ones unless --all)")
    p.add_argument("-a", "--all", action="store_true")
    p.set_defaults(func=cmd_jobs)

    p = sub.add_parser("cancel", help="cancel background jobs by id")
    p.add_argument("job_ids", type=int, nargs="+")
    p.set_defaults(func=cmd_cancel)

    p = sub.add_parser("post", help="post the latest nixpkgs-review report as a PR comment")
    p.add_argument("number", type=int)
    p.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    p.set_defaults(func=cmd_post)

    p = sub.add_parser("settings", help="show or change job settings (agent, model, nixpkgs-review command, …)")
    p.add_argument("key", nargs="?", help="setting to show or change; all are listed without one")
    p.add_argument("value", nargs="?", help="new value")
    p.add_argument("--reset", action="store_true", help="go back to the default")
    p.set_defaults(func=cmd_settings)

    p = sub.add_parser("job-run")  # internal: the detached runner started by check/review
    p.add_argument("job_id", type=int)
    p.set_defaults(func=cmd_job_run)

    args = ap.parse_args()
    try:
        args.func(args)
    except TriageError as e:
        sys.exit(str(e))
    except KeyboardInterrupt:
        sys.exit("interrupted (progress is saved; rerun to resume)")
