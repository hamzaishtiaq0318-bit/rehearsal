"""Append-only audit log. Each entry carries the hash of the one before, so edits are detectable."""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

GENESIS = "0" * 64


def _digest(record: dict) -> str:
    return hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


class AuditLog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._last = GENESIS
        if self.path.exists():
            lines = self.path.read_text(encoding="utf-8").splitlines()
            if lines:
                self._last = json.loads(lines[-1])["hash"]

    def write(self, event: str, **data: Any) -> dict:
        with self._lock:
            record = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event,
                      **data, "prev": self._last}
            record["hash"] = _digest(record)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")
            self._last = record["hash"]
            return record

    def entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line]

    def verify(self) -> bool:
        prev = GENESIS
        for entry in self.entries():
            claimed = entry.pop("hash", None)
            if entry.get("prev") != prev or _digest(entry) != claimed:
                return False
            prev = claimed
        return True
