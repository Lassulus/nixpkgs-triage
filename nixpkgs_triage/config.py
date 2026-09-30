"""Paths, repository and environment settings."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# The executable script; background processes (sync, job runners) are started through it.
ENTRY = ROOT / "triage"

DB_PATH = Path(os.environ.get("TRIAGE_DB", ROOT / "triage.db"))

CATEGORIES_PATH = Path(os.environ.get("TRIAGE_CATEGORIES", ROOT / "categories.toml"))

OWNER, REPO = "NixOS", "nixpkgs"

JOBS_DIR = Path(os.environ.get("TRIAGE_JOBS_DIR", ROOT / "jobs"))

NIXPKGS_DIR = Path(os.environ.get("TRIAGE_NIXPKGS", Path.home() / "src" / "nixpkgs")).expanduser()

# How many jobs of each kind run at once; the rest wait as "pending" in FIFO order.
JOB_SLOTS = {
    "check": int(os.environ.get("TRIAGE_MAX_CHECKS", "3")),
    "review": int(os.environ.get("TRIAGE_MAX_REVIEWS", "1")),
}
