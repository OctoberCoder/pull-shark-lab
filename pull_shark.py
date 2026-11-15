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


class Pacer:
    """Controls loop timing: jittered interval, periodic cool-down, backoff."""

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

    def delay(self) -> float:
        return self.interval * (1 + random.uniform(-self.jitter, self.jitter))

    def sleep(self, cycles_done: int) -> None:
        if self.cooldown_every and cycles_done and cycles_done % self.cooldown_every == 0:
            self.sleeper(self.cooldown_secs)
            return
        self.sleeper(self.delay())

    def backoff_delays(self):
        delay = 60
        while True:
            yield delay
            delay = min(delay * 2, 240)
