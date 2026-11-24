"""Rehearsal as an MCP server, for any MCP client: Claude Desktop, Cursor or Claude Code.

Put Rehearsal in your MCP config where the Synapse server used to be. The agent gets the same tools,
but every call runs on a rehearsal copy of the app, and nothing reaches live data until a person
approves the exact changes in the Rehearsal console. The agent never sees the approval token.

Run: python -m rehearsal.proxy   (MCP on stdin and stdout; the console on 127.0.0.1:8000 or a free port)
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import traceback
import webbrowser
from pathlib import Path
from typing import Any, Awaitable, Callable

import uvicorn
from fastapi import HTTPException
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from . import config as config_mod
from .agent import tool_message
from .apps import free_port
from .engine import State
from .present import rehearsal_dict
from .server import Console, Run, create_app

SUBMIT = "submit_for_review"
STATUS = "review_status"
MAX_STEPS = 40
CLIENTS = {"claude-ai": "Claude Desktop", "claude-code": "Claude Code", "cursor-vscode": "Cursor"}

INSTRUCTIONS = """Every tool here runs on a private rehearsal copy of {target}. Nothing you do reaches live data
until a person approves it. When the task is done, call submit_for_review once with the user's request and a
short summary. A person then sees the exact changes in the Rehearsal console and approves or rejects them.
Call review_status to hear their decision."""

SUBMIT_TOOL = types.Tool(
    name=SUBMIT,
    description=("Call this once when the task is done. A person reviews the exact changes your tool calls would "
                 "make and approves or rejects them. Until then nothing reaches live data."),
    inputSchema={"type": "object", "required": ["request", "summary"], "properties": {
        "request": {"type": "string", "description": "What the user asked you to do, in their words."},
        "summary": {"type": "string", "description": "One or two sentences on what you did and why."}}},
)
STATUS_TOOL = types.Tool(
    name=STATUS,
    description="Says whether the person approved, rejected or is still reviewing your last submission.",
    inputSchema={"type": "object", "properties": {}},
)


class Refused(Exception):
    """A request Rehearsal turns down, with a message meant for the agent."""


def _text(value: Any, limit: int, *, lines: bool = False) -> str:
    """Untrusted text from the agent: printable characters only, one line unless lines is set."""
    raw = value if isinstance(value, str) else ""
    kept = "".join(c if c.isprintable() or (lines and c == "\n") else " " for c in raw)
    return (kept if lines else " ".join(kept.split())).strip()[:limit]


class Bridge:
    """Turns one MCP client's tool calls into console runs that a person reviews."""

    def __init__(self, console: Console, console_url: str, token: str,
                 open_review: Callable[[str], Any] = webbrowser.open):
        self.console = console
        self.console_url = console_url
        self._token = token  # for the person's browser only; never in anything the agent reads
        self._open = open_review
        self.client = "MCP client"
        self.current: Run | None = None
        self.last: Run | None = None
        # One call at a time, so the recorded order is the order the calls ran in, which replay relies on.
        self._lock = asyncio.Lock()

    def tools(self) -> list[types.Tool]:
        note = (f" Runs on a private rehearsal copy of {self.console.engine.target.name}; a person approves "
                "any change before it goes live.")
        return [types.Tool(name=t.name, description=t.description + (note if t.writes else ""),
                           inputSchema=t.agent_schema) for t in self.console.engine.tools.values()] \
            + [SUBMIT_TOOL, STATUS_TOOL]

    async def call(self, name: str, args: Any) -> tuple[str, bool]:
        """Run one tool call for the agent. Returns the text it reads back and whether that is an error."""
        async with self._lock:
            if name == SUBMIT:
                return await self._submit(args if isinstance(args, dict) else {})
            if name == STATUS:
                return self._status(), False
            args = args if isinstance(args, dict) else {}
            if self.current and self.current.done:  # stopped from the console (the pause button)
                self.current = None
                return ("A person stopped this task from the Rehearsal console. Nothing reached live data. "
                        "Tell the user and wait for new instructions."), True
            run = self.current or await self._open_run()
            if len(run.rehearsal.calls) >= MAX_STEPS:
                result = run.rehearsal.record_blocked(name, args, f"step limit of {MAX_STEPS} tool calls reached. "
                                                                  f"Call {SUBMIT} now.")
                return result.text, True
            run.emit("tool_call", {"tool": name, "args": args})
            result = await run.rehearsal.call(name, args)
            text = tool_message(result)
            run.emit("tool_result", {"index": len(run.rehearsal.calls), "tool": name, "ok": result.ok,
                                     "text": text[:1500], "warning": result.warning})
            return text, not result.ok

    async def _open_run(self) -> Run:
        task = f"Task from {self.client}"
        try:
            with self.console.slot():  # shares the console's limit, so runs started there count too
                rehearsal = await self.console.engine.rehearse(task, actor=self.client)
                run = Run(rehearsal, "external", f"{self.client} through MCP", task,
                          f"A request sent from {self.client}", None)
                self.console.runs[rehearsal.id] = run
        except HTTPException as e:
            raise Refused(e.detail) from e
        run.emit("started", {"task": task, "request": run.request, "mode": "external", "model": run.model})
        self.current = run
        return run

    async def _submit(self, args: dict) -> tuple[str, bool]:
        run = self.current
        if run is None:
            return (f"There is nothing to submit: no tool has been called since the last review. "
                    f"Call {STATUS} to hear the decision on that one."), True
        # The agent's account of the request is a claim, not the user's words: it stays in the agent's
        # message, labelled, and never replaces the neutral title the reviewer trusts.
        claimed = _text(args.get("request"), 500)
        said = _text(args.get("summary"), 2000, lines=True)
        summary = f"**Request, as the agent reported it:** {claimed}\n\n{said}" if claimed else said
        run.emit("agent_finished", {"summary": summary, "finished": True, "models": [self.client],
                                    "steps": len(run.rehearsal.calls), "error": None})
        try:
            await run.rehearsal.finish(summary=said)
            trusted = await run.rehearsal.apply_if_trusted()
            run.emit("review", rehearsal_dict(run.rehearsal))
        except Exception:
            run.emit("error", {"message": "The review could not be prepared, so the rehearsal was cancelled."})
            if run.rehearsal.state == State.REHEARSING:
                await run.rehearsal.reject(by="system", reason="The review could not be prepared")
            raise
        finally:
            run.done = True
            run.emit("done", {"state": str(run.rehearsal.state)})
            self.current, self.last = None, run

        details = rehearsal_dict(run.rehearsal)
        link = f"{self.console_url}/#/c/{run.rehearsal.id}"
        if trusted:
            return (f"Applied automatically: nothing in the review raised a flag, and the trust policy lets low "
                    f"risk work through. {trusted.message} A person can undo it in the console: {link}"), False
        if not details["actions"]:
            return f"Submitted. Nothing you did changes live data, so there is nothing to approve. Record: {link}", False
        approve_link = f"{self.console_url}/?token={self._token}#/c/{run.rehearsal.id}"
        if not await asyncio.to_thread(self._open, approve_link):
            # Never log the token: an agent that can read the client's log could approve its own work.
            print(f"Rehearsal {run.rehearsal.id} is waiting for review at {link}. To approve, open "
                  f"{self.console_url}/?token=<your REHEARSAL_TOKEN>", file=sys.stderr)
        review = details["review"]
        lines = ["Submitted for review. Nothing has reached live data yet.", f"Risk: {review['risk']}.",
                 "Waiting for approval:", *(f"- {a}" for a in details["actions"])]
        if review["flags"]:
            lines += ["The reviewer is warned about:", *(f"- {f['title']}" for f in review["flags"])]
        lines.append(f"A person decides in the Rehearsal console ({link}), which has opened in their browser. "
                     f"Tell the user, and call {STATUS} once they say they have decided.")
        return "\n".join(lines), False

    def _status(self) -> str:
        if self.current:
            return (f"A rehearsal is open with {len(self.current.rehearsal.calls)} tool calls. "
                    f"Call {SUBMIT} when the task is done.")
        if self.last is None:
            return "Nothing has been submitted yet."
        r = self.last.rehearsal
        if r.outcome:
            return f"Decision: {r.state}. {r.outcome.message}"
        if not r.replay_calls():
            return "Nothing to decide: the last task did not change live data."
        return "Still waiting for the person to decide in the Rehearsal console."

    async def close(self) -> None:
        run = self.current
        if run and run.rehearsal.state == State.REHEARSING:
            await run.rehearsal.reject(by="system", reason="The agent disconnected before submitting. "
                                                           "Live data was never touched.")
            run.done = True
            run.emit("done", {"state": str(run.rehearsal.state)})
        self.current = None


def _port() -> int:
    wanted = int(os.environ.get("REHEARSAL_PORT", "8000"))
    with socket.socket() as s:
        return wanted if s.connect_ex(("127.0.0.1", wanted)) != 0 else free_port()


def build_mcp(ready: Callable[[], Awaitable[Bridge]], target: str) -> Server:
    """The MCP face of Rehearsal. ready() waits for the console and returns the bridge."""
    mcp = Server("rehearsal", version="1.0.0", instructions=INSTRUCTIONS.format(target=target))

    @mcp.list_tools()
    async def list_tools() -> list[types.Tool]:
        return (await ready()).tools()

    @mcp.call_tool(validate_input=False)  # Rehearsal checks arguments itself and records blocked attempts
    async def call_tool(name: str, arguments: dict) -> types.CallToolResult:
        try:
            b = await ready()
            params = mcp.request_context.session.client_params
            if params:
                b.client = CLIENTS.get(params.clientInfo.name, _text(params.clientInfo.name, 40) or b.client)
            text, error = await b.call(name, arguments)
        except Refused as e:
            text, error = str(e), True
        except Exception:  # noqa: BLE001 - the agent gets a plain message; the details go to the log
            traceback.print_exc(file=sys.stderr)
            text, error = "Rehearsal hit an unexpected error. Nothing reached live data.", True
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)], isError=error)

    return mcp


async def serve(open_review: Callable[[str], Any] = webbrowser.open) -> None:
    config_mod.load_env()
    target = config_mod.load().target.name
    port = _port()
    url = f"http://127.0.0.1:{port}"
    app = create_app()
    web = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="on",
                                        log_config=None, access_log=False))  # stdout belongs to MCP
    async def run_web() -> None:
        try:
            await web.serve()
        except SystemExit:  # uvicorn exits this way when the console cannot start; the tools then say why
            pass

    web_task = asyncio.create_task(run_web())
    bridge: Bridge | None = None

    async def ready() -> Bridge:
        nonlocal bridge
        while bridge is None:
            if web.started:
                bridge = Bridge(app.state.console, url, app.state.token, open_review)
                print(f"Rehearsal console: {url} (approval links open in your browser)", file=sys.stderr)
            elif web_task.done():
                raise Refused("Rehearsal could not start. Another copy may already be running; stop it and "
                              "restart your MCP client.")
            else:
                await asyncio.sleep(0.1)
        return bridge

    mcp = build_mcp(ready, target)
    try:
        async with stdio_server() as (read, write):
            await mcp.run(read, write, mcp.create_initialization_options())
    finally:
        if bridge:
            await bridge.close()
        web.should_exit = True
        await web_task


def main() -> None:
    if "--print-config" in sys.argv:  # the snippet to paste into Claude Desktop, Cursor or another client
        entry = {"command": sys.executable, "args": ["-m", "rehearsal.proxy"],
                 "env": {"PYTHONPATH": str(config_mod.ROOT)}}
        if os.environ.get("REHEARSAL_CONFIG"):  # protecting another app: the client must start it on that config
            entry["env"]["REHEARSAL_CONFIG"] = str(Path(os.environ["REHEARSAL_CONFIG"]).resolve())
        print(json.dumps({"mcpServers": {"rehearsal": entry}}, indent=2))
        return
    # REHEARSAL_OPEN_BROWSER=0 (headless machines): the log names the rehearsal waiting for review, and the
    # person opens the console with a REHEARSAL_TOKEN they chose. The token itself is never logged.
    quiet = os.environ.get("REHEARSAL_OPEN_BROWSER") == "0"
    asyncio.run(serve(open_review=(lambda url: False) if quiet else webbrowser.open))


if __name__ == "__main__":
    main()
