"""Run a Synapse-generated MCP server against one app instance and call its tools."""
from __future__ import annotations

import json
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

import anyio
import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@dataclass
class CallResult:
    ok: bool
    text: str  # compact JSON or error text, exactly what the model sees
    items: list[Any] = field(default_factory=list)  # parsed content blocks


def _parse(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text


class ToolServer:
    """One generated MCP server process bound to one base URL (shadow or live)."""

    def __init__(self, server_file: str | Path, base_url: str, *, timeout: float = 30,
                 python: str = sys.executable, errlog: TextIO | None = None):
        self.server_file = Path(server_file)
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.python = python
        self.errlog = errlog
        self._stack: AsyncExitStack | None = None
        self.session: ClientSession | None = None

    async def __aenter__(self) -> "ToolServer":
        self._stack = AsyncExitStack()
        # Only the base URL is passed on; the MCP SDK adds a minimal safe environment,
        # so model API keys in this process never reach the generated server.
        params = StdioServerParameters(command=self.python, args=[str(self.server_file)],
                                       env={"ENDPOINT_BASE_URL": self.base_url})
        kwargs = {"errlog": self.errlog} if self.errlog else {}
        try:
            read, write = await self._stack.enter_async_context(stdio_client(params, **kwargs))
            self.session = await self._stack.enter_async_context(ClientSession(read, write))
            with anyio.fail_after(self.timeout):
                await self.session.initialize()
        except BaseException:
            await self._stack.aclose()
            raise
        return self

    async def __aexit__(self, *exc) -> None:
        if self._stack:
            await self._stack.aclose()

    async def list_tools(self) -> list:
        assert self.session, "ToolServer is not started"
        return (await self.session.list_tools()).tools

    async def call(self, name: str, args: dict[str, Any]) -> CallResult:
        assert self.session, "ToolServer is not started"
        try:
            with anyio.fail_after(self.timeout):
                res = await self.session.call_tool(name, args)
        except TimeoutError:
            return CallResult(False, f"{name} timed out after {self.timeout:.0f} seconds")
        texts = [c.text for c in res.content if getattr(c, "type", "") == "text"]
        if res.isError:
            message = " ".join(texts).replace(f"Error executing tool {name}: ", "").strip()
            return CallResult(False, message or f"{name} failed")
        items = [_parse(t) for t in texts]
        payload = items[0] if len(items) == 1 else items
        return CallResult(True, json.dumps(payload, separators=(",", ":"), ensure_ascii=False), items)


async def fetch_openapi(base_url: str) -> dict | None:
    """The app's OpenAPI spec if it publishes one; Rehearsal works without it."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{base_url.rstrip('/')}/openapi.json")
            r.raise_for_status()
            return r.json()
    except (httpx.HTTPError, ValueError):
        return None
