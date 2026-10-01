"""GitHub API client (GraphQL + REST GET)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from email.message import Message

from .util import log, parse_ts

API_URL = "https://api.github.com/graphql"

PR_FRAGMENT = """
fragment PR on PullRequest {
  id number title state isDraft createdAt updatedAt closedAt mergedAt
  baseRefName additions deletions changedFiles reviewDecision authorAssociation
  author { login __typename }
  labels(first: 50) { nodes { name } }
  commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
  comments { totalCount }
  latestOpinionatedReviews(first: 30) { nodes { state author { login } } }
  reactionGroups { content reactors { totalCount } }
}
"""

PAGE_QUERY = (
    """
query($owner: String!, $repo: String!, $n: Int!, $after: String, $states: [PullRequestState!], $order: IssueOrder!) {
  rateLimit { cost remaining resetAt }
  repository(owner: $owner, name: $repo) {
    pullRequests(first: $n, after: $after, states: $states, orderBy: $order) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes { ...PR }
    }
  }
}
"""
    + PR_FRAGMENT
)

NODES_QUERY = (
    """
query($ids: [ID!]!) {
  rateLimit { cost remaining resetAt }
  nodes(ids: $ids) { ...PR }
}
"""
    + PR_FRAGMENT
)


class ServerTimeout(Exception):
    """GitHub aborted the query (502/504); the caller should shrink the page."""


class GitHub:
    """Serial client that stays below GitHub's rate limits.

    - primary (5000 points/h): every query asks for `rateLimit`; below `reserve` we sleep until reset.
    - secondary (concurrency / CPU time): requests are strictly serial with `delay` seconds between
      them; 403/429 responses honour `Retry-After`, otherwise back off exponentially.
    """

    def __init__(self, token: str, delay: float, reserve: int):
        self.token = token
        self.delay = delay
        self.reserve = reserve
        self.last_request = 0.0
        self.points_used = 0
        self.requests = 0

    def _sleep(self, seconds: float, why: str) -> None:
        seconds = max(1.0, seconds)
        log(f"{why}; sleeping {seconds:.0f}s")
        time.sleep(seconds)

    def _sleep_until_epoch(self, reset: float, why: str) -> None:
        self._sleep(reset - time.time() + 5, why)

    def _send(self, url: str, body: bytes | None, accept: str) -> tuple[Message, bytes]:
        """One paced request with rate-limit sleeps and retries; returns headers and raw body."""
        backoff = 0
        network_failures = 0
        while True:
            wait = self.last_request + self.delay - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self.last_request = time.monotonic()
            self.requests += 1
            req = urllib.request.Request(
                url,
                data=body,
                headers={
                    "Authorization": f"bearer {self.token}",
                    "Content-Type": "application/json",
                    "Accept": accept,
                    "User-Agent": "nixpkgs-triage",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=90) as resp:
                    return resp.headers, resp.read()
            except urllib.error.HTTPError as e:
                if e.code in (502, 504):
                    raise ServerTimeout(f"HTTP {e.code}") from None
                if e.code == 401:
                    sys.exit("GitHub rejected the token (401). Run `gh auth login` or set GITHUB_TOKEN.")
                if e.code in (403, 429):
                    retry_after = e.headers.get("retry-after")
                    if retry_after:
                        self._sleep(int(retry_after) + 1, f"HTTP {e.code} with Retry-After")
                    elif e.headers.get("x-ratelimit-remaining") == "0":
                        self._sleep_until_epoch(int(e.headers["x-ratelimit-reset"]), "primary rate limit exhausted")
                    else:
                        self._sleep(min(60 * 2**backoff, 900), f"secondary rate limit (HTTP {e.code})")
                        backoff += 1
                    continue
                if e.code >= 500 and network_failures < 5:
                    network_failures += 1
                    self._sleep(10 * network_failures, f"HTTP {e.code}")
                    continue
                raise
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if network_failures >= 5:
                    raise
                network_failures += 1
                self._sleep(10 * network_failures, f"network error: {e}")
                continue

    def query(self, query: str, variables: dict) -> dict:
        body = json.dumps({"query": query, "variables": variables}).encode()
        while True:
            headers, raw = self._send(API_URL, body, "application/json")
            payload = json.loads(raw)
            errors = payload.get("errors") or []
            if any(err.get("type") == "RATE_LIMITED" for err in errors):
                self._sleep_until_epoch(int(headers.get("x-ratelimit-reset", time.time() + 60)), "GraphQL RATE_LIMITED")
                continue
            data = payload.get("data")
            # NOT_FOUND only means a requested node is gone (closed PRs looked up by id); anything else is fatal.
            fatal = [err.get("message") for err in errors if err.get("type") != "NOT_FOUND"]
            if data is None or fatal:
                raise RuntimeError(f"GraphQL error: {fatal or errors}")
            rl = data.get("rateLimit")
            if rl:
                self.points_used += rl["cost"]
                if rl["remaining"] < self.reserve:
                    reset = parse_ts(rl["resetAt"]).timestamp()
                    self._sleep_until_epoch(reset, f"only {rl['remaining']} points left (reserve {self.reserve})")
            return data

    def get(self, path: str, accept: str = "application/vnd.github+json") -> bytes:
        """REST GET of an API path or full API URL."""
        url = path if path.startswith("https://") else f"https://api.github.com{path}"
        return self._send(url, None, accept)[1]


def github_token() -> str:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return token
    if shutil.which("gh"):
        out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    sys.exit("No GitHub token: set GITHUB_TOKEN or log in with `gh auth login`.")


PR_DETAIL_QUERY = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      title body state baseRefName headRefOid author { login }
      commits(first: 100) {
        totalCount
        nodes { commit { oid message parents { totalCount } authors(first: 5) { nodes { name email } } } }
      }
      files(first: 100) { totalCount nodes { path changeType additions deletions } }
      labels(first: 50) { nodes { name } }
    }
  }
}
"""

ADD_COMMENT_MUTATION = """
mutation($subject: ID!, $body: String!) {
  addComment(input: {subjectId: $subject, body: $body}) { commentEdge { node { url } } }
}
"""
