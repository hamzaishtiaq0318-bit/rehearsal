"""Model client behaviour on free-tier realities: rate limits, flaky tool calls, provider dialects."""
import json

import httpx
import pytest

from rehearsal.llm import (GEMINI_FLASH, GEMINI_FOREIGN_SIGNATURE, GROQ_GPT_OSS, ChatClient, LLMError, _for_provider,
                           _seconds)

pytestmark = pytest.mark.anyio

OK = {"choices": [{"message": {"content": None, "tool_calls": [
    {"id": "c1", "type": "function", "function": {"name": "get_ticket", "arguments": "{\"ticket_id\": 1002}"},
     "extra_content": {"google": {"thought_signature": "sig-abc"}}}]}}],
      "usage": {"total_tokens": 42}}


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-groq")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini")


def client(responses, providers=(GROQ_GPT_OSS, GEMINI_FLASH)):
    """A client whose HTTP layer replays the given (host, response) list and records requests."""
    seen, sleeps = [], []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, json.loads(request.content)))
        return responses.pop(0)

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    c = ChatClient(list(providers), transport=httpx.MockTransport(handler), sleep=fake_sleep)
    return c, seen, sleeps


@pytest.mark.parametrize("raw, seconds", [("7", 7), ("1.5", 1.5), ("697ms", 0.697), ("12s", 12), ("1m26.4s", 86.4)])
def test_parses_rate_limit_durations(raw, seconds):
    assert _seconds(raw) == pytest.approx(seconds)


async def test_parses_tool_calls_and_keeps_provider_metadata():
    c, _, _ = client([httpx.Response(200, json=OK)])
    reply = await c.complete([{"role": "user", "content": "hi"}], [])
    assert reply.tool_calls[0].name == "get_ticket" and reply.usage["total_tokens"] == 42
    assert reply.as_message()["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == "sig-abc"


def test_history_is_adapted_to_each_provider():
    history = [{"role": "assistant", "content": "", "tool_calls": [
        {"id": "a", "type": "function", "function": {"name": "x", "arguments": "{}"}},
        {"id": "b", "type": "function", "function": {"name": "y", "arguments": "{}"},
         "extra_content": {"google": {"thought_signature": "real"}}}]}]
    gemini = _for_provider(history, GEMINI_FLASH)[0]["tool_calls"]
    assert gemini[0]["extra_content"]["google"]["thought_signature"] == GEMINI_FOREIGN_SIGNATURE
    assert gemini[1]["extra_content"]["google"]["thought_signature"] == "real"
    assert all("extra_content" not in call for call in _for_provider(history, GROQ_GPT_OSS)[0]["tool_calls"])
    assert "extra_content" not in history[0]["tool_calls"][0], "the shared history must not be mutated"


async def test_short_rate_limit_waits_then_succeeds():
    c, seen, sleeps = client([httpx.Response(429, headers={"retry-after": "3"}), httpx.Response(200, json=OK)])
    await c.complete([], [])
    assert sleeps == [pytest.approx(3.25)] and [host for host, _ in seen] == ["api.groq.com"] * 2


async def test_long_rate_limit_switches_provider_instead_of_waiting():
    c, seen, sleeps = client([httpx.Response(429, headers={"retry-after": "60"}), httpx.Response(200, json=OK)])
    reply = await c.complete([], [])
    assert sleeps == [] and reply.provider == GEMINI_FLASH.label
    assert [host for host, _ in seen] == ["api.groq.com", "generativelanguage.googleapis.com"]


async def test_last_provider_waits_rather_than_failing():
    c, _, sleeps = client([httpx.Response(429, headers={"retry-after": "60"}), httpx.Response(200, json=OK)],
                          providers=(GROQ_GPT_OSS,))
    await c.complete([], [])
    assert sleeps == [pytest.approx(60.25)]


async def test_daily_limit_uses_the_longest_reset_header():
    headers = {"x-ratelimit-reset-tokens": "697ms", "x-ratelimit-reset-requests": "1m26.4s"}
    c, seen, sleeps = client([httpx.Response(429, headers=headers), httpx.Response(200, json=OK)])
    await c.complete([], [])
    assert sleeps == [] and seen[1][0] == "generativelanguage.googleapis.com"


async def test_malformed_tool_call_is_retried_on_the_same_provider():
    bad = httpx.Response(400, json={"error": {"message": "Parsing failed. See 'failed_generation'"}})
    c, seen, _ = client([bad, httpx.Response(200, json=OK)])
    await c.complete([], [])
    assert [host for host, _ in seen] == ["api.groq.com"] * 2


async def test_other_request_errors_move_to_the_next_provider():
    c, seen, _ = client([httpx.Response(400, json={"error": "bad schema"}), httpx.Response(200, json=OK)])
    await c.complete([], [])
    assert seen[1][0] == "generativelanguage.googleapis.com"


async def test_reports_every_failure_when_all_providers_fail():
    c, _, _ = client([httpx.Response(401, json={"error": "nope"}), httpx.Response(401, json={"error": "nope"})])
    with pytest.raises(LLMError, match="Every model provider failed"):
        await c.complete([], [])


def test_needs_at_least_one_key(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY")
    monkeypatch.delenv("GEMINI_API_KEY")
    with pytest.raises(LLMError, match="No model API key"):
        ChatClient([GROQ_GPT_OSS, GEMINI_FLASH])


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def breaker_client(responses, providers=(GROQ_GPT_OSS, GEMINI_FLASH)):
    seen, sleeps, clock = [], [], Clock()

    def handler(request):
        seen.append(request.url.host)
        return responses.pop(0)

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        clock.t += seconds

    c = ChatClient(list(providers), transport=httpx.MockTransport(handler), sleep=fake_sleep, clock=clock)
    return c, seen, sleeps, clock


async def test_busy_model_is_skipped_on_the_next_turn_then_retried_after_cooldown():
    c, seen, _, clock = breaker_client([httpx.Response(429, headers={"retry-after": "60"}),
                                        httpx.Response(200, json=OK), httpx.Response(200, json=OK),
                                        httpx.Response(200, json=OK)])
    await c.complete([], [])
    await c.complete([], [])
    assert seen == ["api.groq.com", "generativelanguage.googleapis.com", "generativelanguage.googleapis.com"]
    assert [s["state"] for s in c.status()] == ["cooling down", "ready"]
    clock.t += 61
    await c.complete([], [])
    assert seen[-1] == "api.groq.com"


async def test_overloaded_model_switches_at_once():
    c, seen, sleeps, _ = breaker_client([httpx.Response(503), httpx.Response(200, json=OK)])
    reply = await c.complete([], [])
    assert sleeps == [] and reply.provider == GEMINI_FLASH.label


async def test_retired_or_forbidden_model_is_disabled_for_the_session():
    c, seen, _, _ = breaker_client([httpx.Response(404, json={"error": "no longer available"}),
                                    httpx.Response(200, json=OK), httpx.Response(200, json=OK)])
    await c.complete([], [])
    await c.complete([], [])
    assert seen == ["api.groq.com", "generativelanguage.googleapis.com", "generativelanguage.googleapis.com"]
    assert c.status()[0]["state"] == "unavailable"


async def test_when_everything_cools_down_it_waits_for_the_first_to_free_up():
    c, seen, sleeps, _ = breaker_client([httpx.Response(429, headers={"retry-after": "40"}),
                                         httpx.Response(429, headers={"retry-after": "90"}),
                                         httpx.Response(200, json=OK)])
    with pytest.raises(LLMError):
        await c.complete([], [])
    reply = await c.complete([], [])
    assert reply.provider == GROQ_GPT_OSS.label and sleeps and sleeps[0] == pytest.approx(40)


def test_default_chain_only_uses_configured_keys(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY")
    c = ChatClient()
    assert c.providers and all(p.key_env == "GROQ_API_KEY" for p in c.providers)


async def test_offline_mode_talks_only_to_the_local_model(monkeypatch):
    from rehearsal.guard import PromptGuard

    monkeypatch.setenv("OLLAMA_MODEL", "qwen3:14b")
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("authorization"), json.loads(request.content)["model"]))
        return httpx.Response(200, json=OK)

    c = ChatClient(transport=httpx.MockTransport(handler))  # no provider list: the default chain would go online
    assert [p.label for p in c.providers] == ["Ollama qwen3:14b"]
    reply = await c.complete([{"role": "user", "content": "hi"}], [])
    await c.aclose()
    assert seen == [("http://127.0.0.1:11434/v1/chat/completions", None, "qwen3:14b")]  # local, and no key sent
    assert reply.tool_calls[0].name == "get_ticket"
    assert not PromptGuard().available  # the injection scan calls Groq, so it stays off offline


def test_prompt_guard_can_be_switched_off(monkeypatch):
    from rehearsal.guard import PromptGuard

    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    assert PromptGuard().available  # on by default when a Groq key is set: it sends tool results to Groq
    monkeypatch.setenv("REHEARSAL_PROMPT_GUARD", "0")
    assert not PromptGuard().available
