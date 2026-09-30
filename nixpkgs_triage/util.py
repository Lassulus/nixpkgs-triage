"""Logging, time formatting and small shared helpers."""

from __future__ import annotations

import shutil
import subprocess
import sys
import webbrowser
from datetime import datetime, timezone

from .config import OWNER, REPO


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", file=sys.stderr, flush=True)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def age(ts: str) -> str:
    days = (utcnow() - parse_ts(ts)).days
    if days >= 365:
        return f"{days // 365}y"
    if days >= 30:
        return f"{days // 30}mo"
    return f"{days}d"


class TriageError(Exception):
    """Expected failure with a message for the user (CLI error / UI footer)."""


def fmt_duration(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h{s % 3600 // 60:02d}m"
    return f"{s // 86400}d"


def since(ts: str) -> str:
    return fmt_duration((utcnow() - parse_ts(ts)).total_seconds())


def pr_url(number: int) -> str:
    return f"https://github.com/{OWNER}/{REPO}/pull/{number}"


def open_url(url: str) -> None:
    """Open in the browser without letting the browser write into our terminal."""
    if shutil.which("xdg-open"):
        subprocess.Popen(
            ["xdg-open", url],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    else:
        webbrowser.open(url)
