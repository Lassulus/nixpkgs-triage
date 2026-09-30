"""Paths and repository (job settings live in the database, see settings.py)."""

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

# Prompt for the guideline check; edit it to change what counts as blocking.
CHECK_PROMPT_PATH = ROOT / "prompts" / "guideline-check.md"
