"""The detached job runner (`triage job-run ID`) and the work each job kind does."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import traceback
import urllib.error
from pathlib import Path

from .config import CHECK_PROMPT_PATH, JOBS_DIR, OWNER, REPO
from .db import open_db
from .github import PR_DETAIL_QUERY, GitHub, github_token
from .jobs import JOB_TITLES, reap_jobs
from .settings import agent_argv, job_slots, load_settings, nixpkgs_dir, review_argv
from .util import TriageError, iso, log, pr_url, read_tail, utcnow

# Copied from the PR's target branch for the guideline check.
GUIDELINE_FILES = (
    "CONTRIBUTING.md",
    ".github/PULL_REQUEST_TEMPLATE.md",
    "pkgs/README.md",
    "nixos/README.md",
    "lib/README.md",
    "doc/README.md",
    "maintainers/README.md",
)

MAX_DIFF_BYTES = 4 * 1024 * 1024

MAX_PR_FILE_BYTES = 256 * 1024

# File contents fetched per GraphQL query.
BLOB_BATCH = 25


class JobCancelled(Exception):
    pass


def run_tool(cmd: list[str], *, cwd: Path, env: dict | None = None, stdout=None) -> int:
    """Run a job's tool in its own process group so a cancel can interrupt it and everything it spawned."""
    log(f"$ {shlex.join(cmd)}")
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=stdout, start_new_session=True)
    try:
        return proc.wait()
    except JobCancelled:
        log("cancelling: sending SIGINT")
        os.killpg(proc.pid, signal.SIGINT)
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        raise


def fetch_blobs(gh: GitHub, specs: list[str]) -> dict[str, str | None]:
    """Contents of `rev:path` expressions; None if missing, binary or larger than MAX_PR_FILE_BYTES."""
    out: dict[str, str | None] = {}
    for i in range(0, len(specs), BLOB_BATCH):
        batch = specs[i : i + BLOB_BATCH]
        params = ", ".join(f"$e{j}: String!" for j in range(len(batch)))
        fields = " ".join(
            f"f{j}: object(expression: $e{j}) {{ ... on Blob {{ text byteSize }} }}" for j in range(len(batch))
        )
        data = gh.query(
            f"query($owner: String!, $repo: String!, {params}) "
            f"{{ repository(owner: $owner, name: $repo) {{ {fields} }} }}",
            {"owner": OWNER, "repo": REPO, **{f"e{j}": spec for j, spec in enumerate(batch)}},
        )["repository"]
        for j, spec in enumerate(batch):
            blob = data[f"f{j}"]
            ok = blob and blob.get("text") is not None and blob["byteSize"] <= MAX_PR_FILE_BYTES
            out[spec] = blob["text"] if ok else None
    return out


def failure_line(jobdir: Path) -> str:
    """The most telling line of a failed tool's output: its last `error:` line, else its last line."""
    lines = [line.strip() for line in read_tail(jobdir / "job.log", 8192).splitlines() if line.strip()]
    errors = [line for line in lines if line.lower().startswith("error")]
    return (errors or lines or [""])[-1][:200]


def run_check(job: sqlite3.Row, settings: dict[str, str]) -> tuple[str, str, str | None]:
    """Collect the PR's commits, diff, changed files and the guideline docs from the API, then run omp on them.
    Nothing comes from the local checkout: it may be shallow, and the API view is also right for merged PRs."""
    number, jobdir = job["number"], Path(job["dir"])
    gh = GitHub(github_token(), delay=1.0, reserve=100)
    pr = gh.query(PR_DETAIL_QUERY, {"owner": OWNER, "repo": REPO, "number": number})["repository"]["pullRequest"]
    head, commits, files = pr["headRefOid"], pr["commits"], pr["files"]
    if commits["totalCount"] == 0:
        raise TriageError("the PR has no commits")

    ctx = jobdir / "context"
    ctx.mkdir(parents=True, exist_ok=True)
    labels = ", ".join(label["name"] for label in pr["labels"]["nodes"]) or "none"
    (ctx / "pr.md").write_text(
        f"# #{number}: {pr['title']}\n\n"
        f"- URL: {pr_url(number)}\n"
        f"- Author: {(pr['author'] or {}).get('login')}\n"
        f"- Target branch: {pr['baseRefName']}\n"
        f"- State: {pr['state'].lower()}\n"
        f"- Head commit: {head}\n"
        f"- Commits: {commits['totalCount']}, changed files: {files['totalCount']}\n"
        f"- Labels: {labels}\n\n"
        f"## Description\n\n{pr['body'] or '(empty)'}\n"
    )

    entries = []
    for node in commits["nodes"]:
        c = node["commit"]
        authors = ", ".join(f"{a['name']} <{a['email']}>" for a in c["authors"]["nodes"])
        merge = "  (merge commit)" if c["parents"]["totalCount"] > 1 else ""
        entries.append(f"commit {c['oid']}{merge}\nAuthor: {authors}\n\n{c['message']}\n")
    if commits["totalCount"] > len(commits["nodes"]):
        entries.append(f"[only the first {len(commits['nodes'])} of {commits['totalCount']} commits]\n")
    (ctx / "commits.txt").write_text("\n".join(entries))

    stat = [f"{f['changeType'].lower():<8} +{f['additions']}/-{f['deletions']}  {f['path']}" for f in files["nodes"]]
    if files["totalCount"] > len(files["nodes"]):
        stat.append(f"[only the first {len(files['nodes'])} of {files['totalCount']} files]")
    try:
        diff = gh.get(f"/repos/{OWNER}/{REPO}/pulls/{number}", "application/vnd.github.diff").decode(errors="replace")
    except urllib.error.HTTPError as e:  # 406 when the diff is too large for the API
        diff = f"[GitHub did not return the diff (HTTP {e.code}); use pr-files/ instead]\n"
    if len(diff) > MAX_DIFF_BYTES:
        diff = diff[:MAX_DIFF_BYTES] + "\n[diff truncated]\n"
    (ctx / "diff.patch").write_text("\n".join(stat) + "\n\n" + diff)

    # spec -> path in the context dir (".github" is renamed so glob tools don't skip it as hidden)
    guideline_specs = {
        f"{pr['baseRefName']}:{p}": f"guidelines/{p.replace('.github/', 'github/')}" for p in GUIDELINE_FILES
    }
    file_specs = {
        f"{head}:{f['path']}": f"pr-files/{f['path']}" for f in files["nodes"] if f["changeType"] != "DELETED"
    }
    blobs = fetch_blobs(gh, [*guideline_specs, *file_specs])
    for spec, rel in {**guideline_specs, **file_specs}.items():
        if (text := blobs[spec]) is not None:
            target = ctx / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)

    prompt = jobdir / "prompt.md"
    # Read at run time so edits to the prompt file apply to the next check; `{number}` is the PR number.
    prompt.write_text(CHECK_PROMPT_PATH.read_text().replace("{number}", str(number)))
    report = jobdir / "report.md"
    with open(report, "w") as out:
        code = run_tool(
            [
                *agent_argv(settings),
                "-p",
                "--no-session",
                "--no-title",
                "--tools=read,grep,glob",
                "--max-time=20m",
                "--cwd",
                str(ctx),
                f"@{prompt}",
            ],
            cwd=ctx,
            stdout=out,
        )
    text = report.read_text()
    if code != 0 or not text.strip():
        return "failed", f"agent exited with {code}: {failure_line(jobdir)}", head
    verdicts = re.findall(r"^\W*VERDICT:\W*(PASS|ISSUES)\b", text, re.M | re.I)
    return "success", verdicts[-1].upper() if verdicts else "no verdict", head


# nixpkgs-review output when GitHub CI's evaluation can't be used (none after 10 min of polling, or expired).
EVAL_UNAVAILABLE = ("No evaluation seems to be available on GitHub", "has expired or been removed")


def github_eval_state(gh: GitHub, number: int) -> str:
    """Whether nixpkgs-review can use GitHub CI's evaluation of the PR head: "ready", "running" or "missing".
    Mirrors what nixpkgs-review looks for: a non-expired `comparison` artifact of an "Eval"/"PR" workflow run."""
    head = json.loads(gh.get(f"/repos/{OWNER}/{REPO}/pulls/{number}"))["head"]["sha"]
    runs = json.loads(gh.get(f"/repos/{OWNER}/{REPO}/actions/runs?head_sha={head}"))["workflow_runs"]
    state = "missing"
    for run in runs:
        if run["name"] not in ("Eval", "PR"):
            continue
        if run["status"] != "completed":
            state = "running"
            continue
        artifacts = json.loads(gh.get(run["artifacts_url"]))["artifacts"]
        if any(a["name"] == "comparison" and not a["expired"] for a in artifacts):
            return "ready"
    return state


def run_review(job: sqlite3.Row, settings: dict[str, str]) -> tuple[str, str, str | None]:
    number, jobdir = job["number"], Path(job["dir"])
    # nixpkgs-review puts its builddir (worktree, logs, report.md/json) under $NIXPKGS_REVIEW_CACHE_DIR.
    env = {**os.environ, "NIXPKGS_REVIEW_CACHE_DIR": str(jobdir), "GITHUB_TOKEN": github_token()}
    argv = review_argv(settings, number)
    local = ["--eval", "local"]
    eval_mode = "configured"  # an explicit --eval in the settings wins
    if not any(arg == "--eval" or arg.startswith("--eval=") for arg in argv):
        state = github_eval_state(GitHub(github_token(), delay=1.0, reserve=100), number)
        # "missing" would make nixpkgs-review poll for 10 minutes and give up; evaluate locally right away.
        eval_mode = "local" if state == "missing" else "github"
        log(f"GitHub CI evaluation of the PR head: {state} -> {eval_mode} evaluation")
        if eval_mode == "local":
            argv += local

    logfile = jobdir / "job.log"
    log_start = logfile.stat().st_size
    code = run_tool(argv, cwd=nixpkgs_dir(settings), env=env)
    reports = sorted(jobdir.glob("nixpkgs-review/pr-*/report.json"), key=lambda p: p.stat().st_mtime)
    if not reports and eval_mode == "github":
        with open(logfile, "rb") as f:
            f.seek(log_start)
            output = f.read().decode(errors="replace")
        if any(marker in output for marker in EVAL_UNAVAILABLE):
            log("GitHub CI evaluation turned out to be unavailable; retrying with --eval local")
            eval_mode = "local"
            code = run_tool(argv + local, cwd=nixpkgs_dir(settings), env=env)
            reports = sorted(jobdir.glob("nixpkgs-review/pr-*/report.json"), key=lambda p: p.stat().st_mtime)
    if not reports:
        return "failed", f"no report (nixpkgs-review exited with {code}): {failure_line(jobdir)}", None
    data = json.loads(reports[-1].read_text())
    shutil.copyfile(reports[-1].with_name("report.md"), jobdir / "report.md")
    failed = 0
    parts = []
    for system, result in data["result"].items():
        failed += len(result.get("failed", []))
        counts = ", ".join(f"{len(attrs)} {kind}" for kind, attrs in result.items() if attrs)
        parts.append(f"{system}: {counts or 'nothing to build'}")
    if eval_mode == "local":
        parts.append("local eval")
    # nixpkgs-review --no-shell exits 1 when a package failed to build.
    status = "success" if code == 0 and failed == 0 else "failed"
    return status, "; ".join(parts), data.get("commit")


@contextlib.contextmanager
def job_slot(db: sqlite3.Connection, job: sqlite3.Row):
    """Wait until this is the oldest pending job of its kind and a slot is free.
    Slots are flock()ed files, so a slot frees itself when its runner dies."""
    slots = JOBS_DIR / ".slots"
    slots.mkdir(parents=True, exist_ok=True)
    announced = False
    while True:
        reap_jobs(db)
        oldest = db.execute(
            "SELECT MIN(id) FROM jobs WHERE kind = ? AND status = 'pending'", (job["kind"],)
        ).fetchone()[0]
        if oldest == job["id"]:
            for i in range(job_slots(load_settings(db), job["kind"])):
                lock = open(slots / f"{job['kind']}-{i}.lock", "w")
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    lock.close()
                    continue
                try:
                    yield
                finally:
                    lock.close()
                return
        if not announced:
            log("pending: waiting for a free slot")
            announced = True
        time.sleep(3)


def cmd_job_run(args: argparse.Namespace) -> None:
    db = open_db()
    job = db.execute("SELECT * FROM jobs WHERE id = ?", (args.job_id,)).fetchone()
    if job is None:
        sys.exit(f"no job {args.job_id}")

    def cancel(signum, frame):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        raise JobCancelled()

    signal.signal(signal.SIGTERM, cancel)
    signal.signal(signal.SIGINT, cancel)
    db.execute("UPDATE jobs SET pid = ? WHERE id = ?", (os.getpid(), job["id"]))
    db.commit()

    status, summary, head = "failed", None, None
    try:
        with job_slot(db, job):
            db.execute("UPDATE jobs SET status = 'running', started_at = ? WHERE id = ?", (iso(utcnow()), job["id"]))
            db.commit()
            log(f"running {JOB_TITLES[job['kind']]} for #{job['number']}")
            # Settings are read when the job starts running, so changes apply to queued jobs too.
            settings = load_settings(db)
            status, summary, head = (run_check if job["kind"] == "check" else run_review)(job, settings)
    except JobCancelled:
        status, summary = "cancelled", "cancelled"
    except Exception as e:
        traceback.print_exc()
        status, summary = "failed", f"error: {e}"
    finally:
        db.execute(
            "UPDATE jobs SET status = ?, summary = ?, head_sha = ?, finished_at = ? WHERE id = ?",
            (status, summary, head, iso(utcnow()), job["id"]),
        )
        db.commit()
        log(f"{status}: {summary}")
