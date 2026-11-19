"""Turn engine objects into plain data and plain sentences for reviewers, the API and reports."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
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


def _plain(value: Any) -> str:
    return str(value).replace("_", " ")


def _amount(value: Any) -> str:
    try:
        return f"{Decimal(str(value)):,.2f}"
    except (InvalidOperation, ValueError):
        return _plain(value)


def action_sentence(tool: str, args: dict[str, Any], phrases: dict[str, str]) -> str:
    """One plain sentence for one recorded write, for example: Refund 249.00 USD on order 5003."""
    template = phrases.get(tool)
    if template:
        try:
            sentence = template.format(**{k: _amount(v) if k == "amount" else _plain(v) for k, v in args.items()})
        except (KeyError, IndexError, ValueError):
            sentence = ""
        if sentence:
            if tool == "update_ticket" and args.get("status") in ("closed", "pending", "open"):
                verb = {"closed": "Close", "pending": "Park", "open": "Reopen"}[args["status"]]
                sentence = f"{verb} ticket {args.get('ticket_id')}" + (" and mark it pending" if args["status"] == "pending" else "")
                if args.get("priority"):
                    sentence += f" with {args['priority']} priority"
            elif tool == "update_ticket" and args.get("priority"):
                sentence = f"Set ticket {args.get('ticket_id')} to {args['priority']} priority"
            return sentence
    details = ", ".join(f"{_plain(k)} {_plain(v)}" for k, v in args.items() if v is not None)
    return f"{_plain(tool).capitalize()}" + (f" ({details})" if details else "")


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
        "actions": [action_sentence(c.tool, c.args, r.engine.config.phrases) for c in r.replay_calls()],
        "review": review,
        "outcome": {"state": str(r.outcome.state), "message": r.outcome.message, "verified": r.outcome.verified}
        if r.outcome else None,
    }
