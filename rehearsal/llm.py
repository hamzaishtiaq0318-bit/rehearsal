"""A client for OpenAI-compatible chat APIs with a long chain of free models.

Free tiers fail in different ways at different times: per-minute token limits, daily caps,
"high demand" outages, models retired for new accounts. So the client walks an ordered chain
of every free model whose key is configured, and remembers which ones are cooling down so a
busy model is skipped instead of retried on every turn.
"""
from __future__ import annotations

import asyncio
import copy
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

WAIT_BEFORE_SWITCHING = 20  # seconds; longer waits move on to the next model
WAIT_ON_LAST_PROVIDER = 75  # seconds; with nowhere else to go, waiting beats failing
DEFAULT_COOLDOWN = 60  # seconds a busy model is skipped when it gives no retry time
# Gemini 3 needs each earlier tool call's thought signature sent back. Calls that another
# model made have none; Google documents this placeholder for that case.
GEMINI_FOREIGN_SIGNATURE = "skip_thought_signature_validator"

GROQ = "https://api.groq.com/openai/v1"
CEREBRAS = "https://api.cerebras.ai/v1"
OPENROUTER = "https://openrouter.ai/api/v1"
GEMINI = "https://generativelanguage.googleapis.com/v1beta/openai"


@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str
    model: str
    key_env: str
    extra: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"{self.name} {self.model}"


REASONING = {"reasoning_effort": "medium"}
GROQ_GPT_OSS = Provider("Groq", GROQ, "openai/gpt-oss-120b", "GROQ_API_KEY", REASONING)
GROQ_GPT_OSS_20B = Provider("Groq", GROQ, "openai/gpt-oss-20b", "GROQ_API_KEY", REASONING)
GEMINI_FLASH = Provider("Gemini", GEMINI, "gemini-3.7-flash", "GEMINI_API_KEY")

# Best tool callers first; every entry is free. Entries without a configured key are skipped.
DEFAULT_CHAIN = [
    GROQ_GPT_OSS,
    Provider("Cerebras", CEREBRAS, "gpt-oss-120b", "CEREBRAS_API_KEY", REASONING),
    GROQ_GPT_OSS_20B,
    Provider("OpenRouter", OPENROUTER, "openai/gpt-oss-120b:free", "OPENROUTER_API_KEY"),
    GEMINI_FLASH,
    Provider("Gemini", GEMINI, "gemini-3.8-flash", "GEMINI_API_KEY"),
    Provider("Gemini", GEMINI, "gemini-flash-latest", "GEMINI_API_KEY"),
    Provider("Gemini", GEMINI, "gemini-3.5-flash", "GEMINI_API_KEY"),
    Provider("OpenRouter", OPENROUTER, "openai/gpt-oss-20b:free", "OPENROUTER_API_KEY"),
    Provider("Gemini", GEMINI, "gemini-3.5-flash-lite", "GEMINI_API_KEY"),
    Provider("Gemini", GEMINI, "gemini-3.1-flash-lite", "GEMINI_API_KEY"),
    Provider("Groq", GROQ, "qwen/qwen3.8-27b", "GROQ_API_KEY"),
]


class LLMError(RuntimeError):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass
class Reply:
    content: str | None
    tool_calls: list[ToolCall]
    provider: str
    usage: dict[str, Any]
    raw_tool_calls: list[dict] = field(default_factory=list)  # exactly as the provider sent them

    def as_message(self) -> dict:
        msg: dict[str, Any] = {"role": "assistant", "content": self.content or ""}
        if self.raw_tool_calls:
            msg["tool_calls"] = copy.deepcopy(self.raw_tool_calls)
        return msg


def _seconds(value: str) -> float:
    """Parse '7', '1.5', '697ms', '12s' or '1m26.4s'."""
    value = value.strip()
    if value.endswith("ms"):
        return float(value[:-2]) / 1000
    total, number = 0.0, ""
    for ch in value:
        if ch.isdigit() or ch == ".":
            number += ch
        elif ch in "hms" and number:
            total += float(number) * {"h": 3600, "m": 60, "s": 1}[ch]
            number = ""
        else:
            raise ValueError(value)
    return total + (float(number) if number else 0.0)


def _retry_after(r: httpx.Response) -> float | None:
    """retry-after when given; otherwise the longest reset among the rate-limit headers."""
    values = []
    for header in ("retry-after", "x-ratelimit-reset-tokens", "x-ratelimit-reset-requests"):
        raw = r.headers.get(header)
        if raw:
            try:
                values.append(_seconds(raw))
            except ValueError:
                continue
            if header == "retry-after":
                return values[0]
    return max(values) if values else None


def _for_provider(messages: list[dict], provider: Provider) -> list[dict]:
    """Adapt the shared history to one provider's dialect of the OpenAI format."""
    out = copy.deepcopy(messages)
    for msg in out:
        for call in msg.get("tool_calls") or []:
            if provider.name == "Gemini":
                google = call.setdefault("extra_content", {}).setdefault("google", {})
                google.setdefault("thought_signature", GEMINI_FOREIGN_SIGNATURE)
            else:
                call.pop("extra_content", None)
    return out


def _transient_400(r: httpx.Response) -> bool:
    text = r.text
    return "failed_generation" in text or "Parsing failed" in text


class ChatClient:
    def __init__(self, providers: list[Provider] | None = None, *, timeout: float = 90,
                 transport: httpx.AsyncBaseTransport | None = None, sleep=asyncio.sleep, clock=time.monotonic):
        self.providers = [p for p in (providers or DEFAULT_CHAIN) if os.environ.get(p.key_env)]
        if not self.providers:
            raise LLMError("No model API key found. Set GROQ_API_KEY or GEMINI_API_KEY in .env")
        self._http = httpx.AsyncClient(timeout=timeout, transport=transport)
        self._sleep = sleep
        self._clock = clock
        self._cooldown: dict[str, float] = {}  # label -> time it may be tried again
        self._disabled: set[str] = set()  # models this account cannot use at all

    async def aclose(self) -> None:
        await self._http.aclose()

    def available(self) -> list[Provider]:
        now = self._clock()
        return [p for p in self.providers if p.label not in self._disabled and self._cooldown.get(p.label, 0) <= now]

    def status(self) -> list[dict]:
        now = self._clock()
        return [{"model": p.label, "state": "unavailable" if p.label in self._disabled else
                 "cooling down" if self._cooldown.get(p.label, 0) > now else "ready"} for p in self.providers]

    async def complete(self, messages: list[dict], tools: list[dict]) -> Reply:
        errors: list[str] = []
        candidates = self.available()
        if not candidates:  # everything is cooling down: try the one that frees up first
            usable = [p for p in self.providers if p.label not in self._disabled]
            if not usable:
                raise LLMError("No configured model is usable with these API keys")
            candidates = [min(usable, key=lambda p: self._cooldown.get(p.label, 0))]
            await self._sleep(max(0.0, min(self._cooldown.get(candidates[0].label, 0) - self._clock(),
                                           WAIT_ON_LAST_PROVIDER)))
        for i, provider in enumerate(candidates):
            last = i == len(candidates) - 1
            body = {"model": provider.model, "messages": _for_provider(messages, provider), "tools": tools,
                    "tool_choice": "auto", "temperature": 0.2, **provider.extra}
            for attempt in range(5):
                try:
                    r = await self._http.post(f"{provider.base_url}/chat/completions", json=body,
                                              headers={"Authorization": f"Bearer {os.environ[provider.key_env]}"})
                except httpx.HTTPError as e:
                    errors.append(f"{provider.label}: {type(e).__name__}")
                    if last:
                        await self._sleep(min(2 ** attempt, 8))
                        continue
                    self._cooldown[provider.label] = self._clock() + DEFAULT_COOLDOWN
                    break
                if r.status_code == 200:
                    try:
                        return self._parse(r.json(), provider)
                    except (LLMError, ValueError) as e:
                        errors.append(f"{provider.label}: {e}")
                        break
                if r.status_code == 429 or r.status_code >= 500:
                    wait = _retry_after(r)
                    errors.append(f"{provider.label}: HTTP {r.status_code}")
                    limit = WAIT_ON_LAST_PROVIDER if last else WAIT_BEFORE_SWITCHING
                    if wait is None:
                        wait = 2 ** attempt if last else DEFAULT_COOLDOWN
                    if wait > limit or (not last and r.status_code >= 500):
                        self._cooldown[provider.label] = self._clock() + max(wait, 1)
                        break
                    await self._sleep(wait + 0.25)
                    continue
                if r.status_code == 400 and _transient_400(r) and attempt < 2:
                    errors.append(f"{provider.label}: malformed tool call, retrying")
                    continue
                if r.status_code in (401, 403, 404):
                    self._disabled.add(provider.label)  # wrong key, no access, or model retired
                errors.append(f"{provider.label}: HTTP {r.status_code} {r.text[:160]}")
                break
        raise LLMError("Every model provider failed: " + "; ".join(errors[-6:]))

    @staticmethod
    def _parse(data: dict, provider: Provider) -> Reply:
        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"{provider.label} returned an unexpected response") from e
        raw = []
        for i, call in enumerate(msg.get("tool_calls") or []):
            call = copy.deepcopy(call)
            call["id"] = call.get("id") or f"call_{i}"
            call.setdefault("type", "function")
            call["function"]["arguments"] = call["function"].get("arguments") or "{}"
            raw.append(call)
        calls = [ToolCall(c["id"], c["function"]["name"], c["function"]["arguments"]) for c in raw]
        return Reply(msg.get("content"), calls, provider.label, data.get("usage") or {}, raw)
