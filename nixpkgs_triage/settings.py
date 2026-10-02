"""User settings for background jobs, stored as JSON in the database (edited in the UI or with `triage settings`)."""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .db import meta_get, meta_set, open_db
from .util import TriageError


def default_review_command() -> str:
    return "nixpkgs-review" if shutil.which("nixpkgs-review") else "nix run nixpkgs#nixpkgs-review --"


@dataclass(frozen=True)
class Setting:
    key: str
    label: str
    help: str
    default: str | Callable[[], str]
    kind: str = "text"  # text | command | args | path | count | url

    def default_value(self) -> str:
        return self.default() if callable(self.default) else self.default


SETTINGS = (
    Setting(
        "server",
        "sync server",
        "`triage update` copies PR data from this triage server instead of GitHub; empty syncs from GitHub",
        "https://review.lassul.us",
        "url",
    ),
    Setting("agent_command", "agent command", "omp command used for guideline checks", "s omp", "command"),
    Setting(
        "agent_model",
        "agent model",
        "passed as `--model` to the agent (e.g. opus, gpt-5.2); empty uses the agent's default",
        "",
    ),
    Setting(
        "review_command",
        "nixpkgs-review command",
        "runs as `<command> pr N --no-shell --build-graph nix <extra arguments>`",
        default_review_command,
        "command",
    ),
    Setting(
        "review_args",
        "nixpkgs-review arguments",
        "extra arguments, e.g. --systems 'x86_64-linux aarch64-linux' or --tests",
        "",
        "args",
    ),
    Setting("nixpkgs_dir", "nixpkgs checkout", "git checkout that nixpkgs-review runs in", "~/src/nixpkgs", "path"),
    Setting("max_checks", "parallel checks", "guideline checks running at once; others wait as pending", "3", "count"),
    Setting("max_reviews", "parallel reviews", "nixpkgs-reviews running at once; others wait as pending", "1", "count"),
)
BY_KEY = {s.key: s for s in SETTINGS}


def stored_settings(db: sqlite3.Connection) -> dict[str, str]:
    return json.loads(meta_get(db, "settings") or "{}")


def load_settings(db: sqlite3.Connection) -> dict[str, str]:
    stored = stored_settings(db)
    return {s.key: stored.get(s.key, s.default_value()) for s in SETTINGS}


def validate(key: str, value: str) -> str | None:
    """Raise TriageError if a value is unusable; return a warning if it is usable but suspicious."""
    setting = BY_KEY[key]
    if setting.kind in ("command", "args"):
        try:
            words = shlex.split(value)
        except ValueError as e:
            raise TriageError(f"{setting.label}: {e}") from None
        if setting.kind == "command":
            if not words:
                raise TriageError(f"{setting.label} must not be empty")
            if not shutil.which(words[0]):
                return f"`{words[0]}` is not in PATH"
    elif setting.kind == "count":
        if not value.isdigit() or int(value) < 1:
            raise TriageError(f"{setting.label} must be a number ≥ 1")
    elif setting.kind == "path" and not (Path(value).expanduser() / ".git").exists():
        return f"{value} is not a git checkout"
    elif setting.kind == "url" and value and not value.startswith(("http://", "https://")):
        raise TriageError(f"{setting.label} must be an http(s) URL or empty")
    return None


def save_setting(db: sqlite3.Connection, key: str, value: str | None) -> str | None:
    """Store a value (None resets to the default). Returns a warning, if any."""
    stored = stored_settings(db)
    warning = None
    if value is None:
        stored.pop(key, None)
    else:
        warning = validate(key, value)
        stored[key] = value
    meta_set(db, "settings", json.dumps(stored))
    db.commit()
    return warning


def agent_argv(settings: dict[str, str]) -> list[str]:
    model = settings["agent_model"].strip()
    return [*shlex.split(settings["agent_command"]), *(["--model", model] if model else [])]


def review_argv(settings: dict[str, str], number: int) -> list[str]:
    return [
        *shlex.split(settings["review_command"]),
        "pr",
        str(number),
        "--no-shell",
        "--build-graph",
        "nix",
        *shlex.split(settings["review_args"]),
    ]


def nixpkgs_dir(settings: dict[str, str]) -> Path:
    return Path(settings["nixpkgs_dir"]).expanduser()


def job_slots(settings: dict[str, str], kind: str) -> int:
    return int(settings["max_checks" if kind == "check" else "max_reviews"])


def cmd_settings(args: argparse.Namespace) -> None:
    db = open_db()
    if args.key is None:
        stored = stored_settings(db)
        values = load_settings(db)
        for s in SETTINGS:
            origin = "" if s.key in stored else "  (default)"
            print(f"{s.key:<16} {values[s.key]!r}{origin}\n{'':<16} {s.help}")
        return
    if args.key not in BY_KEY:
        raise TriageError(f"unknown setting {args.key!r} (known: {', '.join(BY_KEY)})")
    if args.reset:
        save_setting(db, args.key, None)
    elif args.value is None:
        print(load_settings(db)[args.key])
    elif warning := save_setting(db, args.key, args.value):
        print(f"saved, but {warning}")
