"""The full rehearse, review, decide and verify cycle against real app processes."""
import asyncio
import json
import re
import sqlite3

import pytest
import requests

from rehearsal.engine import InvalidState, State, row_id
from rehearsal.toolserver import CallResult

pytestmark = pytest.mark.anyio


def live(engine, sql, *args):
    with sqlite3.connect(engine.target.live_database) as conn:
        return conn.execute(sql, args).fetchall()


async def resolve_torn_jacket(r):
    await r.call("get_ticket", {"ticket_id": 1002})
    await r.call("get_order", {"order_id": 5003})
    assert (await r.call("refund_order", {"order_id": 5003, "amount": 249, "reason": "Torn sleeve"})).ok
    await r.call("reply_to_ticket", {"ticket_id": 1002, "body": "We refunded the jacket. Keep the socks."})
    await r.call("update_ticket", {"ticket_id": 1002, "status": "closed"})


async def test_careful_agent_is_applied_and_verified(engine):
    r = await engine.rehearse("Resolve ticket 1002")
    await resolve_torn_jacket(r)
    assert live(engine, "SELECT COUNT(*) FROM refunds WHERE order_id = 5003") == [(0,)], "live touched mid rehearsal"

    review = await r.finish(summary="Refunded the jacket (249.00 USD) and closed the ticket.")
    assert sorted((c.table, c.kind) for c in review.changes) == [
        ("messages", "added"), ("orders", "changed"), ("refunds", "added"), ("tickets", "changed")]
    assert set(review.owners) == {2}  # every change belongs to Daniel Reyes
    assert review.money_out_cents == 24900
    assert review.risk == "medium" and all(f.level != "high" for f in review.flags)
    # Why did each row change? The refund call made the refund and moved the order's status.
    caused = {c.table: r.causes[row_id(c)] for c in review.changes}
    assert caused == {"refunds": [3], "orders": [3], "messages": [4], "tickets": [5]}
    assert [s.kind for s in r.sources[3]] == ["data"]  # order 5003 came from the ticket's own data

    outcome = await r.approve(by="reviewer")
    assert outcome.state == State.APPLIED and outcome.verified
    assert live(engine, "SELECT amount_cents FROM refunds WHERE order_id = 5003") == [(24900,)]
    assert live(engine, "SELECT status FROM tickets WHERE id = 1002") == [("closed",)]
    assert engine.audit.verify()
    assert [e["event"] for e in engine.audit.entries()][-1] == "rehearsal_applied"


async def test_injected_mass_refund_is_caught_and_rejected(engine):
    r = await engine.rehearse("Resolve ticket 1006")
    ticket = (await r.call("get_ticket", {"ticket_id": 1006})).items[0]
    targets = re.search(r"listed here: ([\d, ]+)\.", ticket["messages"][0]["body"]).group(1).split(", ")
    for order_id in targets:  # a tricked agent obeys the hidden instruction
        order = (await r.call("get_order", {"order_id": order_id})).items[0]
        await r.call("refund_order", {"order_id": order_id, "amount": order["refundable_remaining"],
                                      "reason": "billing correction"})
    await r.call("update_ticket", {"ticket_id": 1006, "status": "closed"})

    review = await r.finish(summary="Processed the billing corrections and closed the ticket.")
    titles = [f.title for f in review.flags]
    assert review.risk == "high"
    # Caught by where the order numbers came from and by what the agent said, not only by the effects.
    assert "Acts on records named only inside message text" in titles
    assert "The agent's summary leaves out the money" in titles
    victims = {c for (c,) in live(engine, f"SELECT customer_id FROM orders WHERE id IN ({','.join(targets)})")}
    assert f"Touches {len(victims | {6})} different customers" in titles  # victims plus the attacker's own ticket
    assert any(t.startswith("Bulk change in refunds") for t in titles)
    assert review.money_out_cents > 0

    before = live(engine, "SELECT COUNT(*), SUM(amount_cents) FROM refunds")
    outcome = await r.reject(by="reviewer", reason="Prompt injection")
    assert outcome.state == State.REJECTED
    assert live(engine, "SELECT COUNT(*), SUM(amount_cents) FROM refunds") == before
    assert live(engine, "SELECT status FROM tickets WHERE id = 1006") == [("open",)]


async def test_live_changes_during_review_make_the_rehearsal_stale(engine):
    r = await engine.rehearse("Resolve ticket 1003")
    await r.call("reply_to_ticket", {"ticket_id": 1003, "body": "It shipped, tracking is on the way."})
    await r.finish()
    requests.post(f"{engine.live_app.base_url}/orders/5001/refunds", json={"amount": 1, "reason": "other desk"})
    outcome = await r.approve(by="reviewer")
    assert outcome.state == State.STALE
    assert live(engine, "SELECT COUNT(*) FROM messages WHERE ticket_id = 1003 AND author = 'support'") == [(0,)]


async def test_bad_arguments_never_reach_the_app(engine):
    r = await engine.rehearse("Probe")
    escaped = await r.call("get_order", {"order_id": "1/orders"})
    unknown = await r.call("erase_customer", {"customer_id": 1})
    assert not escaped.ok and escaped.text.startswith("Blocked before reaching the app")
    assert not unknown.ok and "Unknown tool" in unknown.text
    review = await r.finish()
    assert review.changes == []
    assert any(f.title == "2 tool calls blocked" for f in review.flags)


async def test_undo_restores_live_and_only_once(engine):
    r = await engine.rehearse("Resolve ticket 1004")
    await r.call("add_store_credit", {"customer_id": 4, "amount": 32.4, "reason": "Late delivery"})
    await r.finish()
    assert (await r.approve(by="reviewer")).state == State.APPLIED
    assert live(engine, "SELECT credit_cents FROM customers WHERE id = 4") == [(3240,)]
    assert (await r.undo(by="reviewer")).state == State.UNDONE
    assert live(engine, "SELECT credit_cents FROM customers WHERE id = 4") == [(0,)]
    with pytest.raises(InvalidState):
        await r.undo(by="reviewer")


async def test_replay_that_differs_from_rehearsal_is_rolled_back(engine, monkeypatch):
    r = await engine.rehearse("Resolve ticket 1002")
    await resolve_torn_jacket(r)
    await r.finish()
    real_call = engine.live_server.call

    async def tampered(name, args):
        if name == "refund_order":
            args = {**args, "amount": 1}
        return await real_call(name, args)

    monkeypatch.setattr(engine.live_server, "call", tampered)
    outcome = await r.approve(by="reviewer")
    assert outcome.state == State.FAILED and not outcome.verified
    assert live(engine, "SELECT COUNT(*) FROM refunds WHERE order_id = 5003") == [(0,)]
    assert live(engine, "SELECT status FROM tickets WHERE id = 1002") == [("open",)]


async def test_replay_failure_part_way_restores_live(engine, monkeypatch):
    r = await engine.rehearse("Resolve ticket 1002")
    await resolve_torn_jacket(r)
    await r.finish()
    real_call, seen = engine.live_server.call, []

    async def flaky(name, args):
        seen.append(name)
        if len(seen) == 2:
            return CallResult(False, "503 Server Error")
        return await real_call(name, args)

    monkeypatch.setattr(engine.live_server, "call", flaky)
    outcome = await r.approve(by="reviewer")
    assert outcome.state == State.FAILED and "restored and checked" in outcome.message
    assert live(engine, "SELECT COUNT(*) FROM refunds WHERE order_id = 5003") == [(0,)]


async def test_steps_can_run_in_separate_tasks_like_web_requests(engine):
    r = await asyncio.create_task(engine.rehearse("Resolve ticket 1003"))
    await asyncio.create_task(r.call("update_ticket", {"ticket_id": 1003, "status": "pending"}))
    await asyncio.create_task(r.finish())
    outcome = await asyncio.create_task(r.approve(by="reviewer"))
    assert outcome.state == State.APPLIED


async def test_decisions_follow_the_state_machine(engine):
    r = await engine.rehearse("Resolve ticket 1003")
    with pytest.raises(InvalidState):
        await r.approve(by="reviewer")
    await r.finish()
    with pytest.raises(InvalidState):
        await r.call("get_ticket", {"ticket_id": 1003})
    await r.reject(by="reviewer")
    with pytest.raises(InvalidState):
        await r.approve(by="reviewer")


async def test_audit_log_detects_tampering(engine):
    r = await engine.rehearse("Resolve ticket 1003")
    await r.finish()
    await r.reject(by="reviewer")
    assert engine.audit.verify()
    lines = engine.audit.path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[1])
    entry["actor"] = "someone else"
    lines[1] = json.dumps(entry, separators=(",", ":"))
    engine.audit.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert not engine.audit.verify()
