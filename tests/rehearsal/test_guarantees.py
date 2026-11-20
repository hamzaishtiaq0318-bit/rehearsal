"""Rehearsal's two promises, checked on random tasks rather than hand picked ones.

1. Nothing reaches live data until a person approves.
2. What gets applied is exactly what was reviewed, and undo puts live back exactly.

Each seed is a different random mix of refunds, store credit, replies and ticket updates, some of which
the app refuses (a refund larger than the order, say). The seeds are fixed, so a failure is reproducible.
"""
import random
import sqlite3

import pytest

from rehearsal.dbdiff import copy_database, diff_databases, meaningful, same_effect
from rehearsal.engine import State

pytestmark = pytest.mark.anyio
SEEDS = range(16)


def random_call(rng: random.Random) -> tuple[str, dict]:
    kind = rng.choice(["refund", "refund", "credit", "reply", "update"])
    if kind == "refund":  # some are larger than what is refundable, so the app refuses them
        return "refund_order", {"order_id": rng.randint(5001, 5120), "amount": rng.choice([5, 19.99, 49, 120, 900]),
                                "reason": "random test"}
    if kind == "credit":
        return "add_store_credit", {"customer_id": rng.randint(1, 30), "amount": rng.choice([2.5, 10, 75]),
                                    "reason": "random test"}
    if kind == "reply":
        return "reply_to_ticket", {"ticket_id": rng.randint(1001, 1006), "body": f"Random reply {rng.random():.6f}"}
    return "update_ticket", {"ticket_id": rng.randint(1001, 1006),
                             "status": rng.choice(["open", "pending", "closed"]),
                             "priority": rng.choice(["low", "normal", "high", "urgent"])}


def changed(engine, before) -> list:
    return meaningful(diff_databases(before, engine.target.live_database), engine.target.volatile_columns)


@pytest.mark.parametrize("seed", SEEDS)
async def test_random_tasks_keep_both_promises(engine, tmp_path, seed):
    rng = random.Random(seed)
    before = tmp_path / "before.db"
    copy_database(engine.target.live_database, before)

    r = await engine.rehearse(f"Random task {seed}")
    for _ in range(rng.randint(1, 10)):
        tool, args = random_call(rng)
        await r.call(tool, args)
    assert changed(engine, before) == [], "live changed during the rehearsal"  # promise 1
    review = await r.finish()
    assert changed(engine, before) == [], "live changed while waiting for review"

    if rng.random() < 0.3:
        await r.reject(by="test")
        assert changed(engine, before) == [], "live changed after a rejection"
        return

    outcome = await r.approve(by="test")
    assert outcome.state == State.APPLIED and outcome.verified
    assert same_effect(changed(engine, before), review.changes, engine.target.volatile_columns)  # promise 2
    with sqlite3.connect(engine.target.live_database) as conn:  # the failed calls really were left out
        assert conn.execute("SELECT COUNT(*) FROM refunds WHERE reason = 'random test'").fetchone()[0] == \
            sum(1 for c in review.changes if c.table == "refunds" and c.kind == "added")
    assert (await r.undo(by="test")).state == State.UNDONE
    assert changed(engine, before) == [], "undo did not put live back exactly"
