#!/usr/bin/env python3
"""Pull Shark lab runner.

Opens one small pull request at a time and squash-merges it, paced under
GitHub's secondary rate limits. Token comes from `gh auth token` at runtime and
is never written to disk.

Usage:
    python pull_shark.py --dry-run
    python pull_shark.py --max 320
    python pull_shark.py --max 64 --pair "QueenCy-coder:nkay.cyvy@gmail.com"
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import random
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

API_ROOT = "https://api.github.com"
USER_AGENT = "pull-shark-lab"


@dataclass
class Ledger:
    """Durable progress state. Mutating methods do not persist; call save()."""

    path: Path
    seq: int = 0
    merged: int = 0
    updated_at: str = ""

    def __post_init__(self) -> None:
        self.path = Path(self.path)

    @classmethod
    def load(cls, path: Path) -> "Ledger":
        src = Path(path)
        try:
            data = json.loads(src.read_text())
        except FileNotFoundError:
            return cls(path=src)
        except (json.JSONDecodeError, ValueError):
            cls._quarantine(src)
            return cls(path=src)
        try:
            seq = int(data["seq"])
            merged = int(data["merged"])
            updated_at = str(data.get("updated_at", ""))
        except (KeyError, TypeError, ValueError):
            cls._quarantine(src)
            return cls(path=src)
        return cls(path=src, seq=seq, merged=merged, updated_at=updated_at)

    @staticmethod
    def _quarantine(path: Path) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = path.with_name(f"{path.name}.corrupt-{stamp}")
        try:
            path.replace(target)
        except OSError:
            return
        print(f"warning: corrupt ledger moved to {target}; starting from zero", file=sys.stderr)

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        payload = {"seq": self.seq, "merged": self.merged, "updated_at": self.updated_at}
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w") as fh:
            fh.write(json.dumps(payload, indent=2))
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(self.path)


def backoff_delays() -> Iterator[float]:
    """Exponential backoff schedule for rate-limited retries, capped at 240s."""
    delay = 60.0
    while True:
        yield delay
        delay = min(delay * 2, 240.0)


class Pacer:
    """Controls loop timing: jittered interval and periodic cool-down.

    Tracks its own cycle count so cadence follows elapsed attempts, not
    successes: a stalled loop must not re-trigger cool-downs.
    """

    def __init__(
        self,
        interval: float = 30.0,
        jitter: float = 0.2,
        cooldown_every: int = 50,
        cooldown_secs: float = 300.0,
        sleeper=time.sleep,
    ) -> None:
        self.interval = interval
        self.jitter = jitter
        self.cooldown_every = cooldown_every
        self.cooldown_secs = cooldown_secs
        self.sleeper = sleeper
        self.cycles = 0

    def next_delay(self) -> float:
        return max(0.0, self.interval * (1 + random.uniform(-self.jitter, self.jitter)))

    def sleep(self) -> None:
        self.cycles += 1
        if self.cooldown_every and self.cycles % self.cooldown_every == 0:
            self.sleeper(self.cooldown_secs)
            return
        self.sleeper(self.next_delay())


class GitHubError(RuntimeError):
    """An HTTP error from the GitHub API, with status and retry hints preserved."""

    def __init__(
        self,
        status: int,
        message: str,
        retry_after: float | None = None,
        method: str = "",
        path: str = "",
        retryable: bool = False,
    ) -> None:
        where = f" {method} {path}".rstrip()
        super().__init__(f"HTTP {status}{where}: {message}")
        self.status = status
        self.message = message
        self.retry_after = retry_after
        self.method = method
        self.path = path
        self.retryable = retryable


RETRYABLE = frozenset({403, 429, 500, 502, 503, 504})
TRANSIENT_ERRORS = (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError)
MAX_RETRY_AFTER = 300.0


def is_retryable(status: int, detail: str, retry_after: float | None) -> bool:
    """403 is ambiguous: retry only when GitHub signals a rate limit."""
    if status == 403:
        return retry_after is not None or "rate limit" in detail.lower()
    return status in RETRYABLE


class GitHubAPI:
    """Thin REST wrapper. Every call goes through request() for uniform retry.

    Retrying a non-idempotent write can surface as a duplicate (422 from
    POST /pulls, 409 from PUT /contents after the sha moved). That is
    detectable rather than silent: the Runner records only merged PRs, so
    a duplicate write fails one cycle and burns nothing further.
    """

    def __init__(self, token: str, repo: str, sleeper=time.sleep) -> None:
        self.token = token
        self.repo = repo
        self.sleeper = sleeper

    def _raw(self, method: str, path: str, payload: dict | None = None) -> dict:
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(f"{API_ROOT}{path}", data=data, method=method)
        req.add_unredirected_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", USER_AGENT)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            hint = exc.headers.get("retry-after") if exc.headers else None
            try:
                retry_after = float(hint) if hint is not None else None
            except (TypeError, ValueError):
                retry_after = None
            try:
                detail = exc.read().decode(errors="replace")[:300]
            except Exception:
                detail = ""
            raise GitHubError(
                exc.code,
                detail,
                retry_after,
                method=method,
                path=path,
                retryable=is_retryable(exc.code, detail, retry_after),
            ) from exc

    def request(self, method: str, path: str, payload: dict | None = None, attempts: int = 3) -> dict:
        if attempts < 1:
            raise ValueError("attempts must be at least 1")
        delay_gen = backoff_delays()
        for attempt in range(1, attempts + 1):
            try:
                return self._raw(method, path, payload)
            except GitHubError as exc:
                if not exc.retryable or attempt == attempts:
                    raise
                wait = exc.retry_after if exc.retry_after is not None else next(delay_gen)
                self.sleeper(min(wait, MAX_RETRY_AFTER))
            except TRANSIENT_ERRORS:
                if attempt == attempts:
                    raise
                self.sleeper(next(delay_gen))
        raise AssertionError("unreachable")

    def default_branch_sha(self, branch: str) -> str:
        data = self.request("GET", f"/repos/{self.repo}/git/ref/heads/{branch}")
        return data["object"]["sha"]

    def create_ref(self, branch: str, sha: str) -> dict:
        return self.request(
            "POST", f"/repos/{self.repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": sha}
        )

    def get_contents(self, path: str, branch: str) -> dict | None:
        try:
            return self.request("GET", f"/repos/{self.repo}/contents/{path}?ref={branch}")
        except GitHubError as exc:
            if exc.status == 404:
                return None
            raise

    def put_contents(self, path: str, branch: str, text: str, message: str, sha: str | None) -> dict:
        payload = {
            "message": message,
            "content": base64.b64encode(text.encode()).decode(),
            "branch": branch,
        }
        if sha:
            payload["sha"] = sha
        return self.request("PUT", f"/repos/{self.repo}/contents/{path}", payload)

    def open_pr(self, head: str, base: str, title: str, body: str = "") -> dict:
        return self.request(
            "POST",
            f"/repos/{self.repo}/pulls",
            {"title": title, "body": body, "head": head, "base": base},
        )

    def merge_pr(self, number: int) -> dict:
        return self.request(
            "PUT", f"/repos/{self.repo}/pulls/{number}/merge", {"merge_method": "squash"}
        )

    def merged_pr_count(self, login: str) -> int:
        data = self.request(
            "GET", f"/search/issues?q=is:pr+is:merged+author:{login}&per_page=1"
        )
        return int(data.get("total_count", 0))

    def whoami(self) -> str:
        return self.request("GET", "/user")["login"]
