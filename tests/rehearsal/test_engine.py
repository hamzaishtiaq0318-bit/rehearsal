"""The full rehearse, review, decide and verify cycle against real app processes."""
import asyncio
import json
import re
import sqlite3
from dataclasses import replace

import pytest
import requests

from rehearsal import audit
from rehearsal.audit import AuditLog
from rehearsal.config import RulesConfig
from rehearsal.engine import Engine, InvalidState, State, row_id
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
    undone = await r.undo(by="reviewer")
    assert undone.state == State.UNDONE and undone.verified  # checked against the snapshot, like a rollback
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


async def test_a_crash_part_way_through_the_replay_restores_live(engine, monkeypatch):
    r = await engine.rehearse("Resolve ticket 1002")
    await resolve_torn_jacket(r)
    await r.finish()
    real_call, seen = engine.live_server.call, []

    async def dies(name, args):
        seen.append(name)
        if len(seen) == 2:  # the refund has already landed on live
            raise ConnectionResetError("the tool server process died")
        return await real_call(name, args)

    monkeypatch.setattr(engine.live_server, "call", dies)
    outcome = await r.approve(by="reviewer")
    assert outcome.state == State.FAILED and "ConnectionResetError" in outcome.message
    assert "restored and checked" in outcome.message
    assert live(engine, "SELECT COUNT(*) FROM refunds WHERE order_id = 5003") == [(0,)]


async def test_a_snapshot_that_could_not_be_restored_is_kept_outside_the_session(engine, monkeypatch):
    r = await engine.rehearse("Resolve ticket 1002")
    await resolve_torn_jacket(r)
    await r.finish()

    async def fails(name, args):
        return CallResult(False, "503 Server Error")

    async def unconfirmed(snapshot):
        return False

    monkeypatch.setattr(engine.live_server, "call", fails)
    monkeypatch.setattr(r, "_restore_verified", unconfirmed)
    outcome = await r.approve(by="reviewer")
    kept = engine.config.runtime_dir / "recovery" / f"{r.id}-before-approval.db"
    assert outcome.state == State.FAILED and str(kept) in outcome.message and kept.exists()
    assert engine.sessions_dir not in kept.parents  # the session folder is cleared when Rehearsal restarts


async def test_small_payouts_to_one_customer_add_up_to_the_daily_limit(engine):
    engine.config = replace(engine.config, rules=RulesConfig(max_money_per_owner_per_day_cents=25000))
    first = await engine.rehearse("Resolve ticket 1002")
    await first.call("refund_order", {"order_id": 5003, "amount": 249, "reason": "Torn sleeve"})
    review = await first.finish(summary="Refunded 249.00 USD.")
    assert not any("daily limit" in f.title for f in review.flags)  # 249.00 alone is under 250.00
    assert (await first.approve(by="reviewer")).state == State.APPLIED

    second = await engine.rehearse("Goodwill for customer 2")  # a separate task, for the same customer
    await second.call("add_store_credit", {"customer_id": 2, "amount": 20, "reason": "Goodwill"})
    flags = [f for f in (await second.finish(summary="Added 20.00 USD credit.")).flags if "daily limit" in f.title]
    assert flags and flags[0].level == "high" and "269.00 USD today" in flags[0].detail

    await first.undo(by="reviewer")  # undone money no longer counts
    third = await engine.rehearse("Goodwill for customer 2, again")
    await third.call("add_store_credit", {"customer_id": 2, "amount": 20, "reason": "Goodwill"})
    assert not any("daily limit" in f.title for f in (await third.finish(summary="Added 20.00 USD credit.")).flags)


async def restart(engine):
    """Close an engine and start a new one on the same folders and live data, like restarting Rehearsal."""
    await engine.close()
    return await Engine(engine.config).start()


async def test_a_waiting_review_survives_a_restart(engine):
    waiting = await engine.rehearse("Resolve ticket 1002")
    await resolve_torn_jacket(waiting)
    review = await waiting.finish(summary="Refunded the jacket (249.00 USD) and closed the ticket.")
    decided = await engine.rehearse("Resolve ticket 1003")
    await decided.call("reply_to_ticket", {"ticket_id": 1003, "body": "It shipped."})
    await decided.finish()
    await decided.reject(by="reviewer")

    again = await restart(engine)
    try:
        assert set(again.rehearsals) == {waiting.id} and not decided.dir.exists()  # decided work is not kept
        back = again.rehearsals[waiting.id]
        assert back.state == State.READY and back.review.risk == review.risk
        assert [f.title for f in back.review.flags] == [f.title for f in review.flags]
        assert len(back.review.changes) == len(review.changes) and back.causes == waiting.causes
        assert back.sources == waiting.sources and back.summary == waiting.summary
        outcome = await back.approve(by="reviewer")  # the recorded calls replay as if nothing happened
        assert outcome.state == State.APPLIED and outcome.verified
        assert live(again, "SELECT amount_cents FROM refunds WHERE order_id = 5003") == [(24900,)]
    finally:
        await again.close()


async def test_applied_work_can_still_be_undone_after_a_restart(engine):
    r = await engine.rehearse("Resolve ticket 1004")
    await r.call("add_store_credit", {"customer_id": 4, "amount": 32.4, "reason": "Late delivery"})
    await r.finish()
    assert (await r.approve(by="reviewer")).state == State.APPLIED

    again = await restart(engine)
    try:
        undone = await again.rehearsals[r.id].undo(by="reviewer")
        assert undone.state == State.UNDONE and undone.verified
        assert live(again, "SELECT credit_cents FROM customers WHERE id = 4") == [(0,)]
    finally:
        await again.close()


async def test_applied_work_that_can_no_longer_be_undone_is_not_kept(engine):
    r = await engine.rehearse("Resolve ticket 1004")
    await r.call("add_store_credit", {"customer_id": 4, "amount": 32.4, "reason": "Late delivery"})
    await r.finish()
    await r.approve(by="reviewer")
    requests.post(f"{engine.live_app.base_url}/orders/5001/refunds", json={"amount": 1, "reason": "other desk"})
    again = await restart(engine)
    try:
        assert again.rehearsals == {} and not r.dir.exists()
    finally:
        await again.close()


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
    before = engine.audit.path.read_text(encoding="utf-8")
    check = engine.audit.tamper_check()  # the console's demonstration works on a copy
    assert check["event"] == "rehearsal_rejected" and check["was"] == "reviewer"
    assert check["broken_at"] == check["edited"] and check["real_log_intact"]
    assert engine.audit.path.read_text(encoding="utf-8") == before
    lines = engine.audit.path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[1])
    entry["actor"] = "someone else"
    lines[1] = json.dumps(entry, separators=(",", ":"))
    engine.audit.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert not engine.audit.verify()


def test_a_half_written_last_entry_does_not_stop_rehearsal_starting(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl", key="")
    log.write("engine_started")
    with log.path.open("a", encoding="utf-8") as f:
        f.write('{"at":"2026-11-24T10:00:00+00:00","ev')  # the process died part way through a write
    again = AuditLog(log.path, key="")
    again.write("engine_started")
    assert [e["event"] for e in again.entries()] == ["engine_started", "unreadable", "engine_started"]
    assert not again.verify()  # the console shows where the log was damaged


def test_a_keyed_chain_cannot_be_recomputed_without_the_key(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl", key="kept-away-from-the-log")
    log.write("rehearsal_applied", by="Amira")
    assert log.verify()
    entry = log.entries()[0]
    entry["by"] = "someone else"  # edit the entry, then recompute its hash as anyone could without the key
    entry["hash"] = audit._digest({k: v for k, v in entry.items() if k != "hash"})
    log.path.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    assert audit.first_break(log.entries()) is None  # a chain without a key would have been fooled
    assert not log.verify()
