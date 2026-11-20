"""MCP proxy mode: an outside agent's calls run on a rehearsal copy and wait for a person."""
import httpx
import pytest
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

from rehearsal import proxy, server
from rehearsal.engine import State

pytestmark = pytest.mark.anyio


@pytest.fixture()
async def bridge(engine):
    console = server.Console(engine, None)
    opened: list[str] = []
    b = proxy.Bridge(console, "http://127.0.0.1:9", "secret-token", lambda url: opened.append(url) or True)
    b.opened = opened
    yield b
    await b.close()


async def live_order(engine, order_id):
    async with httpx.AsyncClient() as client:
        return (await client.get(f"{engine.live_app.base_url}/orders/{order_id}")).json()


async def test_outside_agent_works_on_a_copy_until_a_person_approves(bridge, engine):
    before = await live_order(engine, 5003)
    text, error = await bridge.call("get_ticket", {"ticket_id": 1002})
    assert not error and "1002" in text
    text, error = await bridge.call("refund_order", {"order_id": 5003, "amount": 249, "reason": "Torn sleeve"})
    assert not error
    assert await live_order(engine, 5003) == before  # only the copy changed

    text, error = await bridge.call("submit_for_review", {"request": "Refund the torn jacket on ticket 1002",
                                                          "summary": "Refunded 249.00 on order 5003."})
    assert not error and "Waiting for approval" in text and "5003" in text
    assert "secret-token" not in text  # the agent never gets the approval token
    assert bridge.opened == [f"http://127.0.0.1:9/?token=secret-token#/c/{bridge.last.rehearsal.id}"]

    run = bridge.last
    assert run.done and run.mode == "external" and run.rehearsal.state == State.READY
    # The agent's claim about the request never takes the place of trusted text the reviewer relies on.
    assert run.title == "Task from MCP client" and "torn jacket" not in run.request
    finished = next(e["data"] for e in run.events if e["kind"] == "agent_finished")
    assert finished["summary"].startswith("**Request, as the agent reported it:** Refund the torn jacket")
    assert [e["kind"] for e in run.events][-3:] == ["agent_finished", "review", "done"]
    assert "Still waiting" in (await bridge.call("review_status", {}))[0]

    await run.rehearsal.approve(by="Test reviewer")
    assert (await bridge.call("review_status", {}))[0].startswith("Decision: applied")
    assert await live_order(engine, 5003) != before


async def test_outside_work_with_no_flags_is_applied_by_the_trust_policy(bridge):
    await bridge.call("get_ticket", {"ticket_id": 1003})
    await bridge.call("update_ticket", {"ticket_id": 1003, "status": "pending"})
    text, error = await bridge.call("submit_for_review", {"request": "Park ticket 1003",
                                                          "summary": "Marked ticket 1003 as pending."})
    assert not error and text.startswith("Applied automatically") and bridge.opened == []
    assert bridge.last.rehearsal.state == State.APPLIED and bridge.last.rehearsal.outcome.auto


async def test_token_never_reaches_the_log_when_no_browser_opens(engine, capsys):
    b = proxy.Bridge(server.Console(engine, None), "http://127.0.0.1:9", "secret-token", lambda url: False)
    await b.call("refund_order", {"order_id": 5003, "amount": 249, "reason": "Torn sleeve"})
    text, error = await b.call("submit_for_review", {"request": "r", "summary": "s"})
    log = capsys.readouterr().err
    assert not error and b.last.rehearsal.id in log
    assert "secret-token" not in log and "secret-token" not in text


async def test_blocked_arguments_are_recorded_for_the_reviewer(bridge):
    text, error = await bridge.call("refund_order", {"order_id": "1/orders", "amount": 5, "reason": "x"})
    assert error and text.startswith("Blocked")
    assert bridge.current.rehearsal.calls[-1].blocked


async def test_read_only_work_needs_no_approval(bridge):
    text, error = await bridge.call("submit_for_review", {"request": "x", "summary": "y"})
    assert error and "nothing to submit" in text
    await bridge.call("get_ticket", {"ticket_id": 1001})
    text, error = await bridge.call("submit_for_review", {"request": "What is ticket 1001 about?",
                                                          "summary": "A double charge."})
    assert not error and "nothing to approve" in text and bridge.opened == []


async def test_step_limit_holds_for_outside_agents(bridge, monkeypatch):
    monkeypatch.setattr(proxy, "MAX_STEPS", 2)
    await bridge.call("get_ticket", {"ticket_id": 1001})
    await bridge.call("get_ticket", {"ticket_id": 1002})
    text, error = await bridge.call("get_ticket", {"ticket_id": 1003})
    assert error and "step limit" in text


async def test_disconnecting_cancels_the_open_rehearsal(bridge):
    await bridge.call("get_ticket", {"ticket_id": 1001})
    run = bridge.current
    await bridge.close()
    assert run.rehearsal.state == State.REJECTED and run.done


async def test_agent_text_is_flattened_and_capped():
    assert proxy._text("a\x07b\n c", 10) == "a b c"
    assert proxy._text("line one\nline two", 100, lines=True) == "line one\nline two"
    assert proxy._text(42, 10) == "" and len(proxy._text("x" * 900, 500)) == 500


async def test_speaks_mcp_to_a_real_client(bridge):
    async def ready():
        return bridge

    mcp = proxy.build_mcp(ready, "Kestrel Desk")
    info = types.Implementation(name="claude-ai", version="1.0")
    async with create_connected_server_and_client_session(mcp, client_info=info) as client:
        names = [t.name for t in (await client.list_tools()).tools]
        assert "refund_order" in names and names[-2:] == ["submit_for_review", "review_status"]
        ok = await client.call_tool("get_ticket", {"ticket_id": 1002})
        assert not ok.isError and "1002" in ok.content[0].text
        # A bad argument reaches Rehearsal (not just the MCP layer), so the reviewer sees the attempt.
        bad = await client.call_tool("get_order", {"order_id": "../admin"})
        assert bad.isError and bad.content[0].text.startswith("Blocked")
    assert bridge.client == "Claude Desktop"
    assert bridge.current.model == "Claude Desktop through MCP" and len(bridge.current.rehearsal.calls) == 2
