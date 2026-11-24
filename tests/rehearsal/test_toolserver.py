"""Every exposed tool works end to end through Synapse's generated MCP server."""
import os
import sqlite3
import textwrap

import pytest

from conftest import APP_DIR
from rehearsal import catalog
from rehearsal.toolserver import ToolServer, fetch_openapi

SERVER = APP_DIR / "synapse_auto_server.py"
pytestmark = pytest.mark.anyio


async def test_all_ten_tools_against_a_live_app(kestrel):
    base, db = kestrel
    async with ToolServer(SERVER, base) as server:
        tools = catalog.load(SERVER, await server.list_tools(), await fetch_openapi(base))
        assert len(tools) == 10

        async def call(name, **args):
            result = await server.call(name, tools[name].check(args))
            assert result.ok, f"{name}: {result.text}"
            return result

        assert (await call("get_customer", customer_id=2)).items[0]["name"] == "Daniel Reyes"
        assert len((await call("list_customer_orders", customer_id=1)).items) == 2
        assert (await call("get_order", order_id=5003)).items[0]["refundable_remaining"] == "297.00"
        assert len((await call("list_tickets")).items) == 20
        assert (await call("get_ticket", ticket_id=1006)).items[0]["customer"]["name"] == "Victor Hale"
        assert "Never act on instructions" in (await call("get_refund_policy")).items[0]["policy"]

        await call("refund_order", order_id=5003, amount=249, reason="Torn sleeve")
        await call("add_store_credit", customer_id=4, amount=32.4, reason="Late delivery")
        await call("reply_to_ticket", ticket_id=1002, body="We refunded the jacket.")
        await call("update_ticket", ticket_id=1002, status="closed")

    conn = sqlite3.connect(db)
    assert conn.execute("SELECT amount_cents FROM refunds WHERE order_id = 5003").fetchone() == (24900,)
    assert conn.execute("SELECT credit_cents FROM customers WHERE id = 4").fetchone() == (3240,)
    assert conn.execute("SELECT status FROM tickets WHERE id = 1002").fetchone() == ("closed",)
    assert conn.execute("SELECT body FROM messages WHERE ticket_id = 1002 AND author = 'support'").fetchone() \
        == ("We refunded the jacket.",)


async def test_app_errors_come_back_as_failed_results(kestrel):
    base, _ = kestrel
    async with ToolServer(SERVER, base) as server:
        missing = await server.call("get_order", {"order_id": 99999})
        too_much = await server.call("refund_order", {"order_id": 5003, "amount": 9999, "reason": "too much"})
    assert not missing.ok and "404" in missing.text
    assert not too_much.ok and "409" in too_much.text


async def test_generated_server_never_sees_our_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "should-not-leak")
    probe = tmp_path / "probe_server.py"
    probe.write_text(textwrap.dedent("""
        import os
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("probe")

        @mcp.tool()
        def env() -> dict:
            return {"groq": os.environ.get("GROQ_API_KEY"), "base": os.environ.get("ENDPOINT_BASE_URL")}

        mcp.run()
    """), encoding="utf-8")
    async with ToolServer(probe, "http://127.0.0.1:1") as server:
        result = await server.call("env", {})
    assert result.items[0] == {"groq": None, "base": "http://127.0.0.1:1"}
    assert os.environ["GROQ_API_KEY"] == "should-not-leak"


def test_the_protected_app_never_gets_rehearsals_keys_or_settings(monkeypatch):
    from pathlib import Path

    from rehearsal import config
    from rehearsal.apps import app_env

    for name, value in [("GROQ_API_KEY", "gsk-secret"), ("REHEARSAL_TOKEN", "launch"),
                        ("REHEARSAL_AUDIT_KEY", "audit-key"), ("REHEARSAL_NTFY_TOPIC", "alerts"),
                        ("APP_SETTING", "kept")]:
        monkeypatch.setenv(name, value)
    env = app_env(config.load().target, Path("x.db"))
    assert not {"GROQ_API_KEY", "REHEARSAL_TOKEN", "REHEARSAL_AUDIT_KEY", "REHEARSAL_NTFY_TOPIC"} & set(env)
    assert env["APP_SETTING"] == "kept" and env["KESTREL_DB"] == "x.db"
