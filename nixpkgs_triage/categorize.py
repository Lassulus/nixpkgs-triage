"""Rule-based PR categorization from categories.toml."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from pathlib import Path

import tomllib

from .config import CATEGORIES_PATH
from .db import meta_get, meta_set, open_db
from .util import log


class Categorizer:
    """Rules from categories.toml. A category matches if ANY of its criteria matches;
    the first matching category (file order) is the primary one, all matches become tags."""

    def __init__(self, path: Path):
        raw = path.read_bytes()
        self.digest = hashlib.sha256(raw).hexdigest()
        cfg = tomllib.loads(raw.decode())
        self.fallback = cfg.get("fallback", "other")
        self.topic_prefix = cfg.get("topic_label_prefix", "6.topic: ")
        self.categories = []
        for c in cfg["category"]:
            self.categories.append(
                {
                    "name": c["name"],
                    "description": c.get("description", ""),
                    "labels": set(c.get("labels", [])),
                    "authors": set(c.get("authors", [])),
                    **{key: re.compile(c[key]) for key in ("title", "base") if key in c},
                }
            )
        self.names = [c["name"] for c in self.categories] + [self.fallback]

    @staticmethod
    def _matches(c: dict, pr: dict) -> bool:
        if c["labels"] & set(pr["labels"]):
            return True
        if pr["author"] in c["authors"]:
            return True
        if "title" in c and c["title"].search(pr["title"]):
            return True
        if "base" in c and c["base"].search(pr["base_ref"] or ""):
            return True
        return False

    def classify(self, pr: dict) -> tuple[str, list[str], list[str]]:
        tags = [c["name"] for c in self.categories if self._matches(c, pr)]
        topics = sorted(l[len(self.topic_prefix) :] for l in pr["labels"] if l.startswith(self.topic_prefix))
        return (tags[0] if tags else self.fallback), tags, topics


def recategorize(db: sqlite3.Connection, cat: Categorizer) -> int:
    rows = db.execute("SELECT number, title, author, base_ref, labels FROM prs").fetchall()
    for r in rows:
        pr = dict(r)
        pr["labels"] = json.loads(pr["labels"])
        primary, tags, topics = cat.classify(pr)
        db.execute(
            "UPDATE prs SET category = ?, tags = ?, topics = ? WHERE number = ?",
            (primary, json.dumps(tags), json.dumps(topics), r["number"]),
        )
    meta_set(db, "categories_digest", cat.digest)
    db.commit()
    return len(rows)


def load_categorizer(db: sqlite3.Connection) -> Categorizer:
    """Load categories.toml and re-apply it to stored PRs if the file changed since last run."""
    cat = Categorizer(CATEGORIES_PATH)
    if meta_get(db, "categories_digest") != cat.digest:
        n = recategorize(db, cat)
        if n:
            log(f"categories.toml changed; recategorized {n} PRs")
    return cat


def cmd_recategorize(args: argparse.Namespace) -> None:
    db = open_db()
    n = recategorize(db, Categorizer(CATEGORIES_PATH))
    log(f"recategorized {n} PRs")
