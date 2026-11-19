"""The console API end to end over HTTP, with a scripted model and a real engine."""
import json

import httpx
import pytest

from rehearsal import server
from rehearsal.llm import Reply, ToolCall

pytestmark = pytest.mark.anyio


def reply(*calls, text=None):
    raw = [{"id": f"id-{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a)}}
           for i, (n, a) in enumerate(calls)]
    return Reply(text, [ToolCall(r["id"], n, r["function"]["arguments"]) for r, (n, a) in zip(raw, calls)],
                 "Scripted model", {}, raw)


class ScriptedModel:
    def __init__(self, *replies):
        self.replies = list(replies)

    async def complete(self, messages, tools):
        return self.replies.pop(0)

    def status(self):
        return [{"model": "Scripted model", "state": "ready"}]


def jacket_model():
    return ScriptedModel(
        reply(("get_ticket", {"ticket_id": 1002})),
        reply(("refund_order", {"order_id": 5003, "amount": 249, "reason": "Torn sleeve"})),
        reply(("reply_to_ticket", {"ticket_id": 1002, "body": "Refunded the jacket."}),
              ("update_ticket", {"ticket_id": 1002, "status": "closed"})),
        reply(text="Refunded the jacket and closed the ticket."))


@pytest.fixture()
async def api(engine):
    console = server.Console(engine, jacket_model())
    app = server.create_app(console, token="test-token", allowed_hosts=["test"], public_demo=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                     headers={server.TOKEN_HEADER: "test-token"}) as client:
            client.console = console
            yield client
    await console.aclose()


async def follow(api, rehearsal_id):
    r = await api.get(f"/api/rehearsals/{rehearsal_id}/events")
    events = [line.split(": ", 1)[1] for line in r.text.splitlines() if line.startswith("event: ")]
    return events


async def test_full_review_flow_over_http(api):
    status = (await api.get("/api/status")).json()
    assert status["target"] == "Kestrel Desk" and len(status["tools"]) == 10
    queue = (await api.get("/api/tickets")).json()
    assert {t["id"] for t in queue if t["status"] == "open"} == {1001, 1002, 1003, 1004, 1005, 1006}

    started = await api.post("/api/rehearsals", json={"ticket_id": 1002})
    assert started.status_code == 201
    rid = started.json()["id"]
    events = await follow(api, rid)
    assert events[0] == "started" and "tool_call" in events and events[-2:] == ["review", "done"]

    review = (await api.get(f"/api/rehearsals/{rid}")).json()
    assert review["state"] == "ready" and review["review"]["money_out"] == "249.00"
    assert any(c["summary"].startswith("New refund") for c in review["review"]["changes"])

    approved = await api.post(f"/api/rehearsals/{rid}/approve", json={"reviewer": "Amira"})
    assert approved.status_code == 200 and approved.json()["outcome"]["verified"] is True
    assert (await api.get("/api/tickets/1002")).json()["status"] == "closed"
    assert (await api.post(f"/api/rehearsals/{rid}/approve", json={})).status_code == 409

    audit = (await api.get("/api/audit")).json()
    assert audit["verified"] and audit["entries"][-1]["event"] == "rehearsal_applied"
    assert audit["entries"][-1]["by"] == "Amira"


async def test_simulated_attack_is_reviewed_and_rejected(api):
    rid = (await api.post("/api/rehearsals", json={"ticket_id": 1006, "mode": "simulated"})).json()["id"]
    await follow(api, rid)
    review = (await api.get(f"/api/rehearsals/{rid}")).json()
    assert review["review"]["risk"] == "high" and review["model"].startswith("Simulated compromised agent")
    rejected = await api.post(f"/api/rehearsals/{rid}/reject", json={"reason": "Prompt injection"})
    assert rejected.json()["state"] == "rejected"
    assert (await api.get("/api/tickets/1006")).json()["status"] == "open"


async def test_writes_must_be_json(api):
    r = await api.post("/api/rehearsals", content="ticket_id=1002",
                       headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 415


async def test_security_headers_are_set(api):
    headers = (await api.get("/api/status")).headers
    assert headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["x-frame-options"] == "DENY"


@pytest.mark.parametrize("body, code", [
    ({"ticket_id": 1002, "mode": "simulated"}, 422),  # the simulation only targets the injected ticket
    ({"ticket_id": 99999}, 404),
    ({"ticket_id": -1}, 422),
    ({"ticket_id": 1002, "mode": "replay"}, 404),  # no recording for this ticket
    ({"ticket_id": 1002, "mode": "shell"}, 422),
])
async def test_start_validates_input(api, body, code, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "RECORDINGS", tmp_path)  # independent of recordings in the repo
    assert (await api.post("/api/rehearsals", json=body)).status_code == code


async def test_decision_input_is_validated(api):
    rid = (await api.post("/api/rehearsals", json={"ticket_id": 1002})).json()["id"]
    await follow(api, rid)
    assert (await api.post(f"/api/rehearsals/{rid}/reject", json={"reviewer": "x" * 61})).status_code == 422
    assert (await api.post(f"/api/rehearsals/{rid}/reject", json={"reviewer": ""})).status_code == 422
    assert (await api.post("/api/rehearsals/nope/approve", json={})).status_code == 404


async def test_concurrency_cap(api, monkeypatch):
    monkeypatch.setattr(server, "MAX_ACTIVE", 0)
    assert (await api.post("/api/rehearsals", json={"ticket_id": 1002})).status_code == 429


async def test_reset_restores_fresh_demo_data(api):
    rid = (await api.post("/api/rehearsals", json={"ticket_id": 1002})).json()["id"]
    await follow(api, rid)
    await api.post(f"/api/rehearsals/{rid}/approve", json={})
    assert (await api.get("/api/tickets/1002")).json()["status"] == "closed"
    assert (await api.post("/api/reset", json={})).status_code == 200
    assert (await api.get("/api/tickets/1002")).json()["status"] == "open"
    assert (await api.get("/api/rehearsals")).json() == []


async def test_writes_need_the_launch_token(api):
    r = await api.post("/api/rehearsals", json={"ticket_id": 1002}, headers={server.TOKEN_HEADER: "wrong"})
    assert r.status_code == 401
    del api.headers[server.TOKEN_HEADER]
    assert (await api.post("/api/reset", json={})).status_code == 401
    assert (await api.get("/api/status")).status_code == 200  # reads stay open on the allowed host


async def test_requests_for_other_hosts_are_refused(api):
    r = await api.get("/api/status", headers={"host": "attacker.example"})
    assert r.status_code == 400


async def test_public_demo_mode_needs_no_token(engine):
    console = server.Console(engine, jacket_model())
    app = server.create_app(console, allowed_hosts=["test"], public_demo=True)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.post("/api/reset", json={})).status_code == 200
            assert (await client.get("/api/status")).json()["public_demo"] is True
    await console.aclose()


async def test_event_stream_resumes_after_a_reconnect(api):
    rid = (await api.post("/api/rehearsals", json={"ticket_id": 1002})).json()["id"]
    full = await follow(api, rid)
    r = await api.get(f"/api/rehearsals/{rid}/events", headers={"last-event-id": "2"})
    resumed = [line.split(": ", 1)[1] for line in r.text.splitlines() if line.startswith("event: ")]
    assert resumed == full[3:]
