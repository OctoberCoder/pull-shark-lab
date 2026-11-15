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
    """Durable progress state, written atomically after every cycle."""

    path: Path
    seq: int = 0
    merged: int = 0
    updated_at: str = ""

    @classmethod
    def load(cls, path: Path) -> "Ledger":
        led = cls(path=Path(path))
        try:
            data = json.loads(led.path.read_text())
            led.seq = int(data["seq"])
            led.merged = int(data["merged"])
            led.updated_at = str(data.get("updated_at", ""))
        except (FileNotFoundError, json.JSONDecodeError, KeyError, ValueError, TypeError):
            pass
        return led

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        payload = {"seq": self.seq, "merged": self.merged, "updated_at": self.updated_at}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(self.path)
