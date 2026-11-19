"""Run a Synapse-generated MCP server against one app instance and call its tools."""
from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@dataclass
class CallResult:
    ok: bool
    text: str  # compact JSON or error text, exactly what the model sees
    items: list[Any] = field(default_factory=list)  # parsed content blocks
    warning: str | None = None  # set by Rehearsal when the result looks like a prompt injection


def _parse(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text


class ToolServer:
    """One generated MCP server process bound to one base URL (a shadow copy or live).

    The MCP client's cancel scopes must be closed by the task that opened them, but a
    rehearsal is started and finished by different web requests. So each server gets a
    dedicated background task that owns the connection from start to stop.
    """

    def __init__(self, server_file: str | Path, base_url: str, *, timeout: float = 30,
                 python: str = sys.executable, errlog_path: Path | None = None):
        self.server_file = Path(server_file)
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.python = python
        self.errlog_path = errlog_path
        self.session: ClientSession | None = None
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> "ToolServer":
        ready = asyncio.Event()
        self._task = asyncio.create_task(self._run(ready), name=f"mcp:{self.base_url}")
        waiter = asyncio.create_task(ready.wait())
        done, _ = await asyncio.wait({self._task, waiter}, timeout=self.timeout,
                                     return_when=asyncio.FIRST_COMPLETED)
        waiter.cancel()
        if self._task in done:
            self._task.result()  # re-raise the startup error
            raise RuntimeError("MCP server stopped during startup")
        if not done:
            await self.stop()
            raise TimeoutError(f"MCP server for {self.base_url} did not start in {self.timeout:.0f} seconds")
        return self

    async def _run(self, ready: asyncio.Event) -> None:
        # Only the base URL is passed on; the MCP SDK adds a minimal safe environment,
        # so model API keys in this process never reach the generated server.
        params = StdioServerParameters(command=self.python, args=[str(self.server_file)],
                                       env={"ENDPOINT_BASE_URL": self.base_url})
        errlog = open(self.errlog_path, "a", encoding="utf-8") if self.errlog_path else None
        try:
            async with stdio_client(params, **({"errlog": errlog} if errlog else {})) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    self.session = session
                    ready.set()
                    await self._stop.wait()
        finally:
            self.session = None
            if errlog:
                errlog.close()

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            except Exception:  # noqa: BLE001 - shutdown must not fail; the error was logged by the server
                pass
            self._task = None

    async def __aenter__(self) -> "ToolServer":
        return await self.start()

    async def __aexit__(self, *exc) -> None:
        await self.stop()

    async def list_tools(self) -> list:
        if not self.session:
            raise RuntimeError("ToolServer is not running")
        return (await self.session.list_tools()).tools

    async def call(self, name: str, args: dict[str, Any]) -> CallResult:
        if not self.session:
            return CallResult(False, "The tool server is not running")
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
