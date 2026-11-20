"""Plain rules, not AI guesses, that tell a reviewer what to look at before approving."""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

from .config import RiskConfig, Target
from .dbdiff import RowChange

LEVELS = {"high": 3, "medium": 2, "low": 1}


@dataclass(frozen=True)
class Flag:
    level: str  # high, medium or low
    title: str
    detail: str
    rule: str = "effect"  # effect (what would change), source (where targets came from) or say_do

    def to_dict(self) -> dict:
        return asdict(self)


def money(cents: int) -> str:
    return f"{Decimal(cents) / 100:,.2f}"


def money_out(changes: list[RowChange], target: Target) -> list[tuple[RowChange, int]]:
    return [(c, int(c.after[target.money[c.table]])) for c in changes
            if c.kind == "added" and c.table in target.money and c.after.get(target.money[c.table])]


def assess(changes: list[RowChange], owners: list[Any], failed_calls: int, blocked_calls: int,
           target: Target, cfg: RiskConfig, injections: list[tuple] = ()) -> list[Flag]:
    flags: list[Flag] = []

    if injections:
        index, tool, score, excerpt = max(injections, key=lambda i: i[2])
        flags.append(Flag("high", "Agent read text that looks like a prompt injection",
                          f"Call {index} ({tool}), Prompt Guard score {score:.2f}: \"{excerpt}\""))

    payments = money_out(changes, target)
    if payments:
        total = sum(cents for _, cents in payments)
        flags.append(Flag("medium", f"Moves {money(total)} USD out of the business",
                          f"{len(payments)} new payment{'s' if len(payments) != 1 else ''} "
                          f"({', '.join(sorted({c.table for c, _ in payments}))})."))
        big = [cents for _, cents in payments if cents > cfg.single_payment_limit_cents]
        if big:
            flags.append(Flag("high", f"Payment above the {money(cfg.single_payment_limit_cents)} USD approval limit",
                              f"Largest single payment is {money(max(big))} USD."))

    distinct = sorted({o for o in owners if o is not None},
                      key=lambda o: (0, o, "") if isinstance(o, int) else (1, 0, str(o)))
    if len(distinct) > 1:
        label = target.owner_table or "owners"
        flags.append(Flag("high", f"Touches {len(distinct)} different {label}",
                          f"One task normally concerns one {label.rstrip('s')}. "
                          f"Affected ids: {', '.join(str(o) for o in distinct[:12])}"
                          f"{' and more' if len(distinct) > 12 else ''}."))

    per_table = Counter(c.table for c in changes)
    for table, n in sorted(per_table.items()):
        if n > cfg.bulk_rows:
            flags.append(Flag("medium", f"Bulk change in {table}", f"{n} rows would change in one task."))

    removed = [c for c in changes if c.kind == "removed"]
    if removed:
        flags.append(Flag("high", f"Deletes {len(removed)} record{'s' if len(removed) != 1 else ''}",
                          "Deleted records cannot be recovered from inside the app."))

    if blocked_calls:
        flags.append(Flag("medium", f"{blocked_calls} tool call{'s' if blocked_calls != 1 else ''} blocked",
                          "Arguments did not fit the tool and never reached the app."))
    if failed_calls:
        flags.append(Flag("low", f"{failed_calls} tool call{'s' if failed_calls != 1 else ''} failed in the app",
                          "Failed calls changed nothing and will not be replayed."))

    return sorted(flags, key=lambda f: -LEVELS[f.level])


def verdict(flags: list[Flag]) -> str:
    top = max((LEVELS[f.level] for f in flags), default=0)
    return {3: "high", 2: "medium"}.get(top, "low")
