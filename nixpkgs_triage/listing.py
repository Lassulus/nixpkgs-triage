"""The open-PR list shared by the curses UI and the web dashboard."""

from __future__ import annotations

import sqlite3
from collections import Counter

from .jobs import job_short
from .query import MERGE_CONFLICT_LABEL

# (label, column, descending)
SORTS = (("oldest", "created_at", False), ("newest", "created_at", True), ("updated", "updated_at", True))

# (key, label, options); the first option is the default.
FILTERS = (
    ("drafts", "drafts", ("hide", "show", "only")),
    ("conflicts", "merge conflicts", ("hide", "show", "only")),
    ("ci", "CI", ("any", "not failing", "failing")),
    ("check", "guideline check", ("any", "not run", "pass", "issues", "failed")),
    ("review", "nixpkgs-review", ("any", "not run", "pass", "failed")),
)
DEFAULT_FILTERS = {key: options[0] for key, _, options in FILTERS}
# Filter options -> job_short() values
JOB_FILTER_STATES = {"pass": "pass", "issues": "issues", "failed": "FAIL"}

Jobs = dict[tuple[int, str], sqlite3.Row]


def load_open_rows(db: sqlite3.Connection) -> list[dict]:
    rows = db.execute(
        "SELECT p.number, p.title, p.author, p.category, p.is_draft, p.created_at, p.updated_at, "
        "p.ci_state, p.additions, p.deletions, r.status AS review_status, r.pr_updated_at AS reviewed_version, "
        "EXISTS (SELECT 1 FROM json_each(p.labels) WHERE value = ?) AS conflict "
        "FROM prs p LEFT JOIN reviews r ON r.number = p.number WHERE p.state = 'OPEN'",
        (MERGE_CONFLICT_LABEL,),
    ).fetchall()
    return [dict(r) for r in rows]


def filter_ok(r: dict, jobs: Jobs, key: str, value: str) -> bool:
    if key in ("drafts", "conflicts"):
        flag = bool(r["is_draft"] if key == "drafts" else r["conflict"])
        return {"hide": not flag, "show": True, "only": flag}[value]
    if key == "ci":
        failing = r["ci_state"] in ("FAILURE", "ERROR")
        return {"any": True, "not failing": not failing, "failing": failing}[value]
    job = jobs.get((r["number"], key))
    if value == "any":
        return True
    if value == "not run":
        return job is None
    return job is not None and job_short(job) == JOB_FILTER_STATES[value]


def visible_rows(rows: list[dict], jobs: Jobs, filters: dict[str, str]) -> list[dict]:
    active = [(key, v) for key, v in filters.items() if v not in ("show", "any")]
    return [r for r in rows if all(filter_ok(r, jobs, key, v) for key, v in active)]


def category_counts(names: list[str], visible: list[dict]) -> list[tuple[str, int]]:
    """("all", n), then every configured category plus unknown ones found in the rows."""
    counts = Counter(r["category"] for r in visible)
    names = names + sorted(set(counts) - set(names))
    return [("all", len(visible))] + [(n, counts.get(n, 0)) for n in names]


def sorted_rows(rows: list[dict], sort: str) -> list[dict]:
    """PR number breaks ties so the order is stable for paging."""
    _, column, descending = next(s for s in SORTS if s[0] == sort)
    return sorted(rows, key=lambda r: (r[column], r["number"]), reverse=descending)


def filter_summary(filters: dict[str, str]) -> str:
    active = [f"{label} {filters[key]}" for key, label, _ in FILTERS if filters[key] not in ("show", "any")]
    return ", ".join(active) or "none"
