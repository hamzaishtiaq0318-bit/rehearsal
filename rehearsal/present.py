"""Turn engine objects into plain data and plain sentences for reviewers, the API and reports."""
from __future__ import annotations

from typing import Any

from .dbdiff import RowChange
from .engine import Rehearsal
from .risk import money


def _label(table: str) -> str:
    singular = table[:-1] if table.endswith("s") else table
    return singular.replace("_", " ")


def _column(name: str) -> str:
    return name.removesuffix("_cents").replace("_", " ")


def _value(name: str, value: Any) -> str:
    if value is None:
        return "empty"
    if name.endswith("_cents") and isinstance(value, int):
        return f"{money(value)} USD"
    if isinstance(value, str):
        text = value if len(value) <= 90 else value[:87] + "..."
        return f"“{text}”"
    return str(value)


def describe(change: RowChange, volatile: frozenset[str]) -> str:
    key = ", ".join(str(v) for v in change.key.values())
    label = _label(change.table)
    if change.kind == "added":
        details = ", ".join(f"{_column(k)} {_value(k, v)}" for k, v in change.after.items()
                            if k not in change.key and k not in volatile and v is not None)
        return f"New {label} {key}: {details}"
    if change.kind == "removed":
        return f"{label.capitalize()} {key} deleted"
    parts = [f"{_column(k)} from {_value(k, change.before[k])} to {_value(k, change.after[k])}"
             for k in change.changed_columns if k not in volatile]
    return f"{label.capitalize()} {key}: {'; '.join(parts)}"


def rehearsal_dict(r: Rehearsal) -> dict:
    volatile = r.engine.target.volatile_columns
    review = None
    if r.review:
        review = {
            "risk": r.review.risk,
            "money_out": money(r.review.money_out_cents),
            "flags": [f.to_dict() for f in r.review.flags],
            "changes": [{"table": c.table, "key": c.key, "kind": c.kind, "owner": owner,
                         "summary": describe(c, volatile),
                         "columns": c.changed_columns if c.kind == "changed" else [],
                         "before": c.before, "after": c.after}
                        for c, owner in zip(r.review.changes, r.review.owners)],
        }
    return {
        "id": r.id, "task": r.task, "actor": r.actor, "state": str(r.state), "created_at": r.created_at,
        "calls": [c.to_dict() for c in r.calls],
        "replay": [c.index for c in r.replay_calls()],
        "review": review,
        "outcome": {"state": str(r.outcome.state), "message": r.outcome.message, "verified": r.outcome.verified}
        if r.outcome else None,
    }
