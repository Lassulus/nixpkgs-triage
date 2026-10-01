"""SQLite schema and access helpers."""

from __future__ import annotations

import sqlite3

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS prs (
  number INTEGER PRIMARY KEY,
  node_id TEXT NOT NULL,
  title TEXT NOT NULL,
  author TEXT,
  author_association TEXT,
  state TEXT NOT NULL,
  is_draft INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  closed_at TEXT,
  merged_at TEXT,
  base_ref TEXT,
  additions INTEGER,
  deletions INTEGER,
  changed_files INTEGER,
  review_decision TEXT,
  ci_state TEXT,
  comments INTEGER,
  labels TEXT NOT NULL,
  category TEXT,
  tags TEXT,
  topics TEXT,
  seen_run TEXT,
  synced_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS prs_state_category ON prs(state, category);
CREATE TABLE IF NOT EXISTS reviews (
  number INTEGER PRIMARY KEY,
  status TEXT NOT NULL,
  note TEXT,
  marked_at TEXT NOT NULL,
  pr_updated_at TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  number INTEGER NOT NULL,
  kind TEXT NOT NULL,       -- 'check' (omp guideline check) or 'review' (nixpkgs-review)
  status TEXT NOT NULL,     -- pending, running, success, failed, cancelled
  summary TEXT,
  pid INTEGER,              -- the `triage job-run` process
  dir TEXT NOT NULL,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  head_sha TEXT,
  posted_at TEXT,
  comment_url TEXT
);
CREATE INDEX IF NOT EXISTS jobs_number_kind ON jobs(number, kind);
"""


def open_db(check_same_thread: bool = True) -> sqlite3.Connection:
    # Job runners write from their own processes: WAL keeps readers unblocked, the timeout waits out writers.
    db = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=check_same_thread)
    db.execute("PRAGMA journal_mode=WAL")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    # Older databases still have these dropped columns.
    for column in {"files", "files_total"}.intersection(r["name"] for r in db.execute("PRAGMA table_info(prs)")):
        db.execute(f"ALTER TABLE prs DROP COLUMN {column}")
    return db


def meta_get(db: sqlite3.Connection, key: str) -> str | None:
    row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def meta_set(db: sqlite3.Connection, key: str, value: str | None) -> None:
    if value is None:
        db.execute("DELETE FROM meta WHERE key = ?", (key,))
    else:
        db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))
