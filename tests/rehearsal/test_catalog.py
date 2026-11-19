"""Catalogue rules: read/write classification, real types, and argument guards."""
from types import SimpleNamespace

import pytest

import main
from conftest import APP_DIR
from rehearsal.catalog import ArgumentError, load

SERVER = APP_DIR / "synapse_auto_server.py"

TOOLS = ["get_customer", "list_customer_orders", "add_store_credit", "get_order", "refund_order",
         "list_tickets", "get_ticket", "reply_to_ticket", "update_ticket", "get_refund_policy"]
ARGS = {"get_customer": ["customer_id"], "list_customer_orders": ["customer_id"],
        "add_store_credit": ["customer_id", "amount", "reason"], "get_order": ["order_id"],
        "refund_order": ["order_id", "amount", "reason"], "get_ticket": ["ticket_id"],
        "reply_to_ticket": ["ticket_id", "body"], "update_ticket": ["ticket_id", "status", "priority"]}


def fake_mcp_tools(names=TOOLS):
    return [SimpleNamespace(name=n, description=f"{n} tool",
                            inputSchema={"properties": {a: {} for a in ARGS.get(n, [])}}) for n in names]


@pytest.fixture(scope="module")
def catalog():
    return load(SERVER, fake_mcp_tools(), main.app.openapi())


def test_classifies_reads_and_writes_from_synapse_output(catalog):
    assert {n for n, t in catalog.items() if t.writes} == {
        "add_store_credit", "refund_order", "reply_to_ticket", "update_ticket"}
    assert catalog["refund_order"].method == "POST" and catalog["refund_order"].path == "/orders/{order_id}/refunds"


def test_agent_schema_has_real_types(catalog):
    s = catalog["refund_order"].agent_schema
    assert s["properties"]["order_id"]["type"] == "integer"
    assert s["properties"]["amount"]["type"] == "number"
    assert s["required"] == ["amount", "order_id", "reason"]
    assert catalog["update_ticket"].agent_schema["properties"]["status"]["enum"] == ["open", "pending", "closed"]


@pytest.mark.parametrize("bad", ["1/orders", "5003?x=1", "../admin", "5003#", " 5003", "", "%2e%2e", True, 0, -1,
                                 "abc", 5003.5, "５００３"])
def test_path_arguments_cannot_escape_their_route(catalog, bad):
    with pytest.raises(ArgumentError):
        catalog["get_order"].check({"order_id": bad})


def test_valid_arguments_are_cleaned(catalog):
    assert catalog["get_order"].check({"order_id": "5003"}) == {"order_id": 5003}
    assert catalog["refund_order"].check({"order_id": 5003, "amount": 24.99, "reason": "Torn sleeve"})["amount"] == 24.99
    assert catalog["update_ticket"].check({"ticket_id": 1003, "status": "pending"})["status"] == "pending"


@pytest.mark.parametrize("args, message", [
    ({"order_id": 5003, "amount": 10}, "reason"),
    ({"order_id": 5003, "amount": 10, "reason": "ok reason", "admin": True}, "does not take admin"),
    ({"order_id": 5003, "amount": -4, "reason": "negative"}, "amount"),
])
def test_bad_arguments_get_a_clear_message(catalog, args, message):
    with pytest.raises(ArgumentError, match=message):
        catalog["refund_order"].check(args)


def test_ticket_status_must_be_a_known_value(catalog):
    with pytest.raises(ArgumentError):
        catalog["update_ticket"].check({"ticket_id": 1003, "status": "archived"})


def test_path_guard_works_without_openapi():
    tools = load(SERVER, fake_mcp_tools(["get_order"]), None)
    with pytest.raises(ArgumentError):
        tools["get_order"].check({"order_id": "1/orders"})


def test_unknown_tool_fails_closed():
    with pytest.raises(ValueError, match="Could not find the route"):
        load(SERVER, fake_mcp_tools(["drop_database"]), None)
