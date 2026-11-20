"""The console API end to end over HTTP, with a scripted model and a real engine."""
import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from rehearsal import server
from rehearsal.config import TrustConfig
from rehearsal.llm import LLMError, Reply, ToolCall

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


def tracking_model():
    """Ticket 1003: tell the customer the order shipped and close the ticket. No money, one customer."""
    return ScriptedModel(
        reply(("get_ticket", {"ticket_id": 1003})),
        reply(("reply_to_ticket", {"ticket_id": 1003, "body": "Your order has shipped. Tracking is in your account."}),
              ("update_ticket", {"ticket_id": 1003, "status": "closed"})),
        reply(text="Told Priya her order shipped and closed the ticket."))


async def test_work_with_no_flags_is_applied_by_the_trust_policy(api):
    api.console.client = tracking_model()
    rid = (await api.post("/api/rehearsals", json={"ticket_id": 1003})).json()["id"]
    await follow(api, rid)
    detail = (await api.get(f"/api/rehearsals/{rid}")).json()
    assert detail["review"]["risk"] == "low" and detail["review"]["flags"] == []
    assert detail["state"] == "applied" and detail["outcome"]["auto"] and detail["outcome"]["verified"]
    assert (await api.get("/api/tickets/1003")).json()["status"] == "closed"
    assert (await api.get("/api/audit")).json()["entries"][-1]["by"] == "Trust policy"
    assert (await api.post(f"/api/rehearsals/{rid}/undo", json={})).json()["state"] == "undone"
    assert (await api.get("/api/tickets/1003")).json()["status"] == "open"


async def test_looking_at_other_customers_means_a_person_reviews(api):
    api.console.client = ScriptedModel(
        reply(("get_ticket", {"ticket_id": 1003})),
        reply(("get_customer", {"customer_id": 9})),  # not Priya
        reply(("reply_to_ticket", {"ticket_id": 1003, "body": "Your order has shipped."}),
              ("update_ticket", {"ticket_id": 1003, "status": "closed"})),
        reply(text="Told Priya her order shipped."))
    rid = (await api.post("/api/rehearsals", json={"ticket_id": 1003})).json()["id"]
    await follow(api, rid)
    detail = (await api.get(f"/api/rehearsals/{rid}")).json()
    assert detail["state"] == "ready"
    assert [f["title"] for f in detail["review"]["flags"]] == ["Looked at the records of 1 other customer"]


class FailsAfter(ScriptedModel):
    async def complete(self, messages, tools):
        if not self.replies:
            raise LLMError("every free model is busy")
        return self.replies.pop(0)


async def test_work_from_an_agent_that_did_not_finish_waits_for_a_person(api):
    api.console.client = FailsAfter(reply(("get_ticket", {"ticket_id": 1003})),
                                    reply(("update_ticket", {"ticket_id": 1003, "status": "pending"})))
    rid = (await api.post("/api/rehearsals", json={"ticket_id": 1003})).json()["id"]
    await follow(api, rid)
    detail = (await api.get(f"/api/rehearsals/{rid}")).json()
    assert detail["review"]["flags"] == [] and detail["state"] == "ready"  # harmless, but it never finished


async def test_money_always_waits_for_a_person(api):
    rid = (await api.post("/api/rehearsals", json={"ticket_id": 1002})).json()["id"]  # the 249.00 refund
    await follow(api, rid)
    assert (await api.get(f"/api/rehearsals/{rid}")).json()["state"] == "ready"


async def test_trust_policy_none_asks_about_everything(api):
    api.console.engine.config = replace(api.console.engine.config, trust=TrustConfig("none"))
    api.console.client = tracking_model()
    rid = (await api.post("/api/rehearsals", json={"ticket_id": 1003})).json()["id"]
    await follow(api, rid)
    assert (await api.get(f"/api/rehearsals/{rid}")).json()["state"] == "ready"


async def test_a_waiting_review_sends_a_phone_alert_without_personal_data(api, monkeypatch):
    sent = []

    async def fake_post(url, content=b"", headers=None):
        sent.append((url, content.decode(), headers))
        return httpx.Response(200)

    monkeypatch.setenv("REHEARSAL_NTFY_TOPIC", "rehearsal-test-7f3a9c")
    monkeypatch.setattr(api.console._http, "post", fake_post)
    rid = (await api.post("/api/rehearsals", json={"ticket_id": 1002})).json()["id"]  # money: waits for a person
    await follow(api, rid)
    assert len(sent) == 1
    url, text, headers = sent[0]
    assert url == "https://ntfy.sh/rehearsal-test-7f3a9c" and headers["Title"] == "Rehearsal needs your review"
    assert "Ticket 1002" in text and "249.00 USD out" in text
    assert "Daniel" not in text and "test-token" not in text  # no customer names, no approval token

    api.console.client = tracking_model()  # applied by the trust policy: nobody needs to be woken up
    rid = (await api.post("/api/rehearsals", json={"ticket_id": 1003})).json()["id"]
    await follow(api, rid)
    assert len(sent) == 1


class SlowModel(ScriptedModel):
    """Reads a ticket, then thinks for a long time: still running when the pause button is pressed."""

    async def complete(self, messages, tools):
        if len(messages) > 2:
            await asyncio.sleep(60)
        return reply(("get_ticket", {"ticket_id": 1003}))


async def test_pause_stops_every_running_agent(api):
    api.console.client = SlowModel()
    rid = (await api.post("/api/rehearsals", json={"ticket_id": 1003})).json()["id"]
    while not api.console.runs[rid].rehearsal.calls:
        await asyncio.sleep(0.05)
    r = await api.post("/api/pause", json={"reviewer": "Ops lead"})
    assert r.status_code == 200 and r.json() == {"stopped": 1}
    detail = (await api.get(f"/api/rehearsals/{rid}")).json()
    assert detail["state"] == "rejected" and not detail["running"]
    entries = (await api.get("/api/audit")).json()["entries"]
    assert entries[-1]["event"] == "agents_paused" and entries[-1]["by"] == "Ops lead"


async def test_tools_reload_after_a_synapse_rebuild(api):
    r = await api.post("/api/tools/reload", json={})
    assert r.status_code == 200 and len(r.json()["tools"]) == 10 and r.json()["added"] == []
    assert (await api.get("/api/audit")).json()["entries"][-1]["event"] == "tools_reloaded"
    del api.headers[server.TOKEN_HEADER]
    assert (await api.post("/api/tools/reload", json={})).status_code == 401


def test_reviewer_links_are_distinct_secrets_for_distinct_people():
    links = server.reviewer_tokens(" Amira , Ben ")
    assert set(links) == {"Amira", "Ben"} and len(set(links.values())) == 2
    assert server.reviewer_tokens("") == {}
    for bad in ("Amira,amira", "<script>", "A" * 41):
        with pytest.raises(ValueError):
            server.reviewer_tokens(bad)


def test_trust_policy_rejects_unknown_values():
    with pytest.raises(ValueError):
        TrustConfig("sometimes")


async def test_a_proposal_can_be_sent_back_with_feedback(api):
    api.console.client = ScriptedModel(
        # First attempt: the jacket refund plus a goodwill credit nobody asked for.
        reply(("refund_order", {"order_id": 5003, "amount": 249, "reason": "Torn sleeve"}),
              ("add_store_credit", {"customer_id": 2, "amount": 20, "reason": "Goodwill"})),
        reply(text="Refunded 249.00 USD and added 20.00 USD credit."),
        # Second attempt, after the feedback.
        reply(("refund_order", {"order_id": 5003, "amount": 249, "reason": "Torn sleeve"})),
        reply(text="Refunded 249.00 USD for the jacket only."))
    first = (await api.post("/api/rehearsals", json={"ticket_id": 1002})).json()["id"]
    await follow(api, first)
    sent = await api.post(f"/api/rehearsals/{first}/revise",
                          json={"reviewer": "Amira", "feedback": "Only refund the jacket, no store credit."})
    assert sent.status_code == 201
    second = sent.json()["id"]
    await follow(api, second)
    old = (await api.get(f"/api/rehearsals/{first}")).json()
    new = (await api.get(f"/api/rehearsals/{second}")).json()
    assert old["state"] == "rejected" and "Only refund the jacket" in old["outcome"]["message"]
    assert new["state"] == "ready" and new["actions"] == ["Refund 249.00 USD on order 5003"]
    assert new["request"] == "Change requested: Only refund the jacket, no store credit."
    assert "Only refund the jacket" in new["task"]
    # Recorded runs cannot be redone, and a decided proposal cannot be sent back again.
    assert (await api.post(f"/api/rehearsals/{first}/revise", json={"feedback": "again"})).status_code == 409


async def test_high_risk_needs_two_people_each_with_their_own_link(engine):
    engine.config = replace(engine.config, trust=TrustConfig("low", "high"))
    console = server.Console(engine, jacket_model())
    app = server.create_app(console, token="launch", allowed_hosts=["test"], public_demo=False,
                            reviewers={"Amira": "amira-secret", "Ben": "ben-secret"})
    as_ = lambda t: {server.TOKEN_HEADER: t}  # noqa: E731
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            rid = (await client.post("/api/rehearsals", json={"ticket_id": 1006, "mode": "simulated"},
                                     headers=as_("launch"))).json()["id"]
            await follow(client, rid)
            url = f"/api/rehearsals/{rid}/approve"
            # The shared console link cannot approve high risk work, whatever name it types.
            assert (await client.post(url, json={"reviewer": "Amira"}, headers=as_("launch"))).status_code == 403
            first = (await client.post(url, json={"reviewer": "Ben"}, headers=as_("amira-secret"))).json()
            assert first["state"] == "ready" and first["approvals"] == ["Amira"]  # the link decides, not the name
            again = await client.post(url, json={"reviewer": "Ben"}, headers=as_("amira-secret"))
            assert again.status_code == 409 and "someone else" in again.json()["detail"]
            second = (await client.post(url, json={}, headers=as_("ben-secret"))).json()
            assert second["state"] == "applied" and second["outcome"]["verified"]
            events = [(e["event"], e.get("by")) for e in (await client.get("/api/audit")).json()["entries"]]
            assert ("first_approval", "Amira") in events and events[-1] == ("rehearsal_applied", "Amira and Ben")
    await console.aclose()


@pytest.mark.parametrize("name", ["Trust policy", "trust POLICY", "system"])
async def test_reviewers_cannot_sign_as_rehearsal_itself(api, name):
    rid = (await api.post("/api/rehearsals", json={"ticket_id": 1002})).json()["id"]
    await follow(api, rid)
    assert (await api.post(f"/api/rehearsals/{rid}/approve", json={"reviewer": name})).status_code == 422
    assert (await api.get("/api/overview")).json()["applied_automatically"] == 0


async def test_overview_counts_what_rehearsal_did_from_the_audit_log(api):
    jacket = (await api.post("/api/rehearsals", json={"ticket_id": 1002})).json()["id"]
    await follow(api, jacket)
    await api.post(f"/api/rehearsals/{jacket}/approve", json={})
    attack = (await api.post("/api/rehearsals", json={"ticket_id": 1006, "mode": "simulated"})).json()["id"]
    await follow(api, attack)
    await api.post(f"/api/rehearsals/{attack}/reject", json={})
    api.console.client = tracking_model()
    await follow(api, (await api.post("/api/rehearsals", json={"ticket_id": 1003})).json()["id"])

    o = (await api.get("/api/overview")).json()
    assert (o["rehearsed"], o["approved"], o["applied_automatically"], o["cancelled"]) == (3, 1, 1, 1)
    assert o["high_risk_stopped"] == 1 and o["money_applied_cents"] == 24900 and o["chain_intact"]
    assert o["money_stopped_cents"] > 300000  # the scripted attack's refunds


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


async def test_conversations_have_titles_and_plain_action_lists(api):
    rid = (await api.post("/api/rehearsals", json={"ticket_id": 1002})).json()["id"]
    await follow(api, rid)
    listed = (await api.get("/api/rehearsals")).json()[0]
    assert listed["title"] == "Ticket 1002: Jacket arrived with a torn sleeve" and listed["ticket_id"] == 1002
    detail = (await api.get(f"/api/rehearsals/{rid}")).json()
    assert detail["request"] == "Please resolve ticket 1002."
    assert detail["actions"] == ["Refund 249.00 USD on order 5003", "Reply to the customer on ticket 1002",
                                 "Close ticket 1002"]


async def test_a_free_text_request_starts_a_conversation(api):
    api.console.client = ScriptedModel(reply(("get_customer", {"customer_id": 4})),
                                       reply(text="Tom Whitfield is a business customer with no store credit."))
    started = await api.post("/api/rehearsals", json={"message": "Who is customer 4?"})
    assert started.status_code == 201 and started.json()["title"] == "Who is customer 4?"
    await follow(api, started.json()["id"])
    detail = (await api.get(f"/api/rehearsals/{started.json()['id']}")).json()
    assert detail["ticket_id"] is None and detail["review"]["changes"] == []


@pytest.mark.parametrize("body", [{}, {"message": "x" * 501}, {"message": "hi", "mode": "simulated"},
                                  {"message": "line\u0000break"}])
async def test_free_text_requests_are_validated(api, body):
    assert (await api.post("/api/rehearsals", json=body)).status_code == 422


async def test_public_demo_only_takes_ticket_requests_in_server_wording(engine):
    console = server.Console(engine, jacket_model())
    app = server.create_app(console, allowed_hosts=["test"], public_demo=True)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            refused = await client.post("/api/rehearsals", json={"message": "Refund every order in the store"})
            assert refused.status_code == 403
            started = await client.post("/api/rehearsals", json={"ticket_id": 1002, "message": "also refund 5001"})
            assert started.status_code == 201
            detail = (await client.get(f"/api/rehearsals/{started.json()['id']}")).json()
            assert detail["task"] == "Resolve support ticket 1002." and detail["request"] == "Please resolve ticket 1002."
            await follow(client, started.json()["id"])
    await console.aclose()
