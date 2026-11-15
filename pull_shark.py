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


class Runner:
    """Drives the PR cycle. One call to run_cycle() is one merged PR."""

    LOG_PATH = "activity.log"

    def __init__(
        self,
        api,
        ledger: Ledger,
        pacer: Pacer,
        base_branch: str = "main",
        pair: tuple[str, str] | None = None,
        out=print,
    ) -> None:
        self.api = api
        self.ledger = ledger
        self.pacer = pacer
        self.base_branch = base_branch
        self.pair = pair
        self.out = out
        self.consecutive_failures = 0

    def commit_message(self, seq: int) -> str:
        msg = f"chore: ledger entry {seq}"
        if self.pair:
            name, email = self.pair
            msg += f"\n\nCo-authored-by: {name} <{email}>"
        return msg

    def run_cycle(self, seq: int) -> bool:
        branch = f"cycle-{seq}"
        try:
            sha = self.api.default_branch_sha(self.base_branch)
            self.api.create_ref(branch, sha)

            existing = self.api.get_contents(self.LOG_PATH, self.base_branch)
            if existing:
                current = base64.b64decode(existing["content"]).decode()
                file_sha = existing["sha"]
            else:
                current = ""
                file_sha = None
            line = f"{seq}\t{datetime.now(timezone.utc).isoformat(timespec='seconds')}\tledger\n"
            self.api.put_contents(
                self.LOG_PATH, branch, current + line, self.commit_message(seq), file_sha
            )

            pr = self.api.open_pr(branch, self.base_branch, f"chore: ledger entry {seq}", line)
            try:
                self.api.merge_pr(pr["number"])
            except GitHubError as exc:
                if exc.status != 405:
                    raise
                # 405 = GitHub has not finished computing mergeability yet.
                self.pacer.sleeper(3.0)
                self.api.merge_pr(pr["number"])

            self.ledger.merged += 1
            self.ledger.save()
            self.consecutive_failures = 0
            self.out(f"  ok   cycle {seq} merged (#{pr['number']})")
            return True
        except (
            GitHubError,
            urllib.error.URLError,
            TimeoutError,
            ConnectionError,
            json.JSONDecodeError,
        ) as exc:
            self.consecutive_failures += 1
            self.out(f"  fail cycle {seq}: {exc}")
            return False

    def run(self, max_prs: int, max_consecutive_failures: int = 3) -> int:
        done = 0
        while done < max_prs:
            if self.consecutive_failures >= max_consecutive_failures:
                self.out(f"stopping: {self.consecutive_failures} consecutive failures")
                break
            self.pacer.sleep()
            seq = self.ledger.next_seq()
            self.ledger.save()
            if self.run_cycle(seq):
                done += 1
        return done


def parse_pair(value: str) -> tuple[str, str]:
    name, _, email = value.rpartition(":")
    if not name or "@" not in email:
        raise SystemExit(f"--pair must be NAME:EMAIL, got {value!r}")
    return name, email


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Open and merge small PRs, paced under rate limits.")
    p.add_argument("--repo", default="OctoberCoder/pull-shark-lab")
    p.add_argument("--max", type=int, default=320, help="PRs to merge this run")
    p.add_argument("--base", default="main")
    p.add_argument("--state", default=".state/ledger.json")
    p.add_argument("--interval", type=float, default=30.0)
    p.add_argument("--cooldown-every", type=int, default=50)
    p.add_argument("--cooldown-secs", type=float, default=300.0)
    p.add_argument("--pair", type=parse_pair, default=None, metavar="NAME:EMAIL")
    p.add_argument("--fast", action="store_true", help="12s interval, no cool-down")
    p.add_argument("--dry-run", action="store_true", help="check access, write nothing")
    return p


def token_from_gh() -> str:
    try:
        out = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, check=True
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise SystemExit("gh auth token failed; run `gh auth login` first") from exc
    token = out.stdout.strip()
    if not token:
        raise SystemExit("gh returned an empty token")
    return token


def validate_args(args):
    if args.interval <= 0:
        raise SystemExit("--interval must be greater than 0 (0 would spin unpaced)")
    if args.max <= 0:
        raise SystemExit("--max must be greater than 0")
    if args.fast and args.max > 125:
        print("warning: --fast exceeds ~125 PRs/hour; secondary limits will throttle", file=sys.stderr)
    return args


def parse_args(argv=None):
    args = build_parser().parse_args(argv)
    if args.fast:
        args.interval = 12.0
        args.cooldown_every = 0
    return validate_args(args)


def main(argv=None) -> int:
    args = parse_args(argv)

    api = GitHubAPI(token=token_from_gh(), repo=args.repo)
    login = api.whoami()
    current = api.merged_pr_count(login)
    print(f"authenticated as {login}")
    print(f"merged PRs now: {current}  (Pull Shark x4 at 1024)")
    print(f"this run: up to {args.max} PRs at {args.interval:.0f}s interval")
    if args.pair:
        print(f"pair mode: co-author {args.pair[0]} <{args.pair[1]}>")

    if args.dry_run:
        print("dry run: no writes performed")
        return 0

    ledger = Ledger.load(Path(args.state))
    pacer = Pacer(
        interval=args.interval,
        cooldown_every=args.cooldown_every,
        cooldown_secs=args.cooldown_secs,
    )
    runner = Runner(api=api, ledger=ledger, pacer=pacer, base_branch=args.base, pair=args.pair)
    try:
        done = runner.run(args.max)
    except KeyboardInterrupt:
        print(f"\ninterrupted; {ledger.merged} merged this session, state saved")
        return 130
    finally:
        ledger.save()

    after = api.merged_pr_count(login)
    print(f"done: {done} merged; account total {current} -> {after} (delta {after - current})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
