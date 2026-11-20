"""Append-only audit log. Each entry carries the hash of the one before, so edits are detectable."""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

GENESIS = "0" * 64
TRUST_POLICY = "Trust policy"  # who the audit log names when work is applied without asking


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
        return first_break(self.entries()) is None

    def tamper_check(self) -> dict:
        """Show the chain catching an edit, on a copy: change who decided the latest decision and find
        where the check fails. The real log is never written."""
        entries = self.entries()
        if not entries:
            return {"edited": None}
        edited = next((i for i in range(len(entries) - 1, -1, -1) if "by" in entries[i]), len(entries) - 1)
        copy = [dict(e) for e in entries]
        before = copy[edited].get("by")
        copy[edited]["by"] = "someone else"
        return {"edited": edited, "event": entries[edited]["event"], "field": "by", "was": before,
                "broken_at": first_break(copy), "count": len(entries), "real_log_intact": first_break(entries) is None}


def summarize(entries: list[dict]) -> dict:
    """What Rehearsal has done, from the audit log alone: the numbers on the console's overview."""
    reviews: dict[str, tuple[str | None, int]] = {}  # rehearsal -> (risk, money it would pay out)
    counts = {"rehearsed": 0, "applied_automatically": 0, "approved": 0, "cancelled": 0, "undone": 0,
              "held_or_rolled_back": 0, "high_risk_stopped": 0}
    money_stopped = money_applied = 0
    for e in entries:
        event, rid = e.get("event"), e.get("rehearsal")
        risk, cents = reviews.get(rid, (None, 0))
        if event == "rehearsal_started":
            counts["rehearsed"] += 1
        elif event == "review_ready":
            reviews[rid] = (e.get("risk"), int(e.get("money_out_cents") or 0))
        elif event == "rehearsal_applied":
            counts["applied_automatically" if e.get("auto") is True else "approved"] += 1  # a field, not a name
            money_applied += cents
        elif event == "rehearsal_undone":
            counts["undone"] += 1
            money_applied -= cents
        elif event == "rehearsal_rejected" and rid in reviews:  # stopped after a person saw the review
            counts["cancelled"] += 1
            money_stopped += cents
            counts["high_risk_stopped"] += risk == "high"
        elif event in ("rehearsal_stale", "rehearsal_failed"):
            counts["held_or_rolled_back"] += 1
    return {**counts, "money_stopped_cents": money_stopped, "money_applied_cents": money_applied,
            "chain_intact": first_break(entries) is None}


def first_break(entries: list[dict]) -> int | None:
    """The position of the first entry whose hash or link does not check out, or None if the chain holds."""
    prev = GENESIS
    for i, entry in enumerate(entries):
        record = {k: v for k, v in entry.items() if k != "hash"}
        if record.get("prev") != prev or _digest(record) != entry.get("hash"):
            return i
        prev = entry["hash"]
    return None
