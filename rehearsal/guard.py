"""Scan text the agent reads for prompt injections, using Llama Prompt Guard 2 on Groq.

This is an extra signal for the reviewer and a warning for the agent. It is never the gate:
the gate is the human review of the rehearsal diff, which catches harm however it was caused.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx

MODEL = "meta-llama/llama-prompt-guard-2-86m"
THRESHOLD = 0.9
MIN_CHARS = 40
MAX_CHARS = 2000  # about the model's 512 token window
MAX_STRINGS = 10


@dataclass(frozen=True)
class Detection:
    score: float
    excerpt: str


def strings_in(value: Any) -> list[str]:
    """Every string inside a parsed tool result, longest first."""
    found: list[str] = []
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            found.append(item)
        elif isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return sorted({s for s in found if len(s) >= MIN_CHARS}, key=len, reverse=True)[:MAX_STRINGS]


class PromptGuard:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        # Offline mode (OLLAMA_MODEL) promises nothing leaves the machine, and this scan calls Groq.
        self.key = None if os.environ.get("OLLAMA_MODEL") else os.environ.get("GROQ_API_KEY")
        self._http = httpx.AsyncClient(timeout=15, transport=transport)

    @property
    def available(self) -> bool:
        return bool(self.key)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def score(self, text: str) -> float | None:
        try:
            r = await self._http.post("https://api.groq.com/openai/v1/chat/completions",
                                      headers={"Authorization": f"Bearer {self.key}"},
                                      json={"model": MODEL, "messages": [{"role": "user", "content": text[:MAX_CHARS]}]})
            r.raise_for_status()
            return float(r.json()["choices"][0]["message"]["content"])
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError):
            return None  # the scan is advisory; an outage must not block work

    async def scan(self, items: list[Any]) -> Detection | None:
        """The highest scoring string in a tool result, or None when nothing could be scored."""
        if not self.available:
            return None
        best: Detection | None = None
        for text in strings_in(items):
            score = await self.score(text)
            if score is not None and (best is None or score > best.score):
                best = Detection(score, text[:280])
        return best
