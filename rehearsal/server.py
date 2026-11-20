"""Rehearsal web server: the review console's API, live agent event streams, and the built console UI.

Run: python -m rehearsal.server   (binds 127.0.0.1:8000 unless REHEARSAL_HOST / REHEARSAL_PORT say otherwise)
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator

from . import config as config_mod
from .agent import SYSTEM_PROMPT, ReplayModel, run_agent
from .apps import seed_database
from .audit import TRUST_POLICY, summarize
from .dbdiff import restore_database
from .engine import Engine, InvalidState, Rehearsal, State
from .guard import PromptGuard
from .llm import ChatClient, LLMError
from .present import rehearsal_dict
from .risk import money
from .scenarios import LABEL as SIMULATION_LABEL
from .scenarios import CompromisedAgent

ROOT = Path(__file__).resolve().parents[1]
WEB_DIST = ROOT / "web" / "dist"
RECORDINGS = ROOT / "demo" / "recordings"
MAX_ACTIVE = int(os.environ.get("REHEARSAL_MAX_ACTIVE", "3"))
SIMULATION_TICKET = 1006
TOKEN_HEADER = "X-Rehearsal-Token"
LOOPBACK = ("127.0.0.1", "localhost", "::1")
NTFY_URL = "https://ntfy.sh/"
# ntfy topics are public by name, so a long random one is the only thing keeping alerts private.
NTFY_TOPIC = re.compile(r"[A-Za-z0-9_-]{8,64}")

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": ("default-src 'self'; img-src 'self' data:; style-src 'self'; font-src 'self'; "
                                "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
                                "form-action 'none'"),
}


@dataclass
class Run:
    """The live event log of one rehearsal, so any number of viewers can follow it."""
    rehearsal: Rehearsal
    mode: str
    model: str
    title: str
    request: str  # what the person asked for, in their words
    ticket_id: int | None
    events: list[dict] = field(default_factory=list)
    done: bool = False
    task: asyncio.Task | None = None
    _changed: asyncio.Event = field(default_factory=asyncio.Event)

    def emit(self, kind: str, data: dict) -> None:
        self.events.append({"id": len(self.events), "kind": kind, "data": data})
        self._changed.set()

    async def wait(self, timeout: float) -> None:
        self._changed.clear()
        try:
            await asyncio.wait_for(self._changed.wait(), timeout)
        except asyncio.TimeoutError:
            pass


class StartRequest(BaseModel):
    ticket_id: int | None = Field(default=None, ge=1, le=10_000_000)
    message: str | None = Field(default=None, min_length=2, max_length=500)
    mode: Literal["agent", "simulated", "replay"] = "agent"

    @field_validator("message")
    @classmethod
    def plain_message(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = " ".join(value.split())
        if any(ord(ch) < 32 for ch in value):
            raise ValueError("control characters are not allowed")
        return value

    @model_validator(mode="after")
    def needs_something_to_do(self) -> "StartRequest":
        if self.ticket_id is None and not self.message:
            raise ValueError("Send a ticket_id, a message, or both")
        if self.mode != "agent" and self.ticket_id is None:
            raise ValueError("Recorded runs and the simulation need a ticket_id")
        return self


RESERVED_NAMES = {TRUST_POLICY.casefold(), "system", "evaluator", "gallery", "console"}


def _plain(value: str) -> str:
    value = " ".join(value.split())
    if any(ord(ch) < 32 for ch in value):
        raise ValueError("control characters are not allowed")
    return value


def _person(value: str) -> str:
    """A reviewer's name, which must not pass for Rehearsal itself in the audit log."""
    value = _plain(value)
    if value.casefold() in RESERVED_NAMES:
        raise ValueError(f"{value!r} is reserved for Rehearsal's own entries in the audit log")
    return value


class Decision(BaseModel):
    reviewer: str = Field(default="Reviewer", min_length=1, max_length=60)
    reason: str = Field(default="", max_length=500)

    @field_validator("reviewer")
    @classmethod
    def person(cls, value: str) -> str:
        return _person(value)

    @field_validator("reason")
    @classmethod
    def plain(cls, value: str) -> str:
        return _plain(value)


class Revision(BaseModel):
    reviewer: str = Field(default="Reviewer", min_length=1, max_length=60)
    feedback: str = Field(min_length=3, max_length=500)

    @field_validator("reviewer")
    @classmethod
    def person(cls, value: str) -> str:
        return _person(value)

    @field_validator("feedback")
    @classmethod
    def plain(cls, value: str) -> str:
        return _plain(value)


def conversation(run: Run) -> dict:
    return {"mode": run.mode, "model": run.model, "title": run.title, "request": run.request,
            "ticket_id": run.ticket_id}


class Console:
    """Shared state behind the API: the engine, the model client, and every run."""

    def __init__(self, engine: Engine, client: Any, *, public_demo: bool = False):
        self.engine = engine
        self.client = client
        # In a public demo anyone can type, so free text instructions are turned off: every request
        # must name a ticket and uses the server's own wording.
        self.public_demo = public_demo
        self.runs: dict[str, Run] = {}
        # Scripted agents that act out an attack, by ticket: (name of the attack, label shown, agent factory).
        self.simulations: dict[int, tuple[str, str, Callable[[], Any]]] = {
            SIMULATION_TICKET: ("A polite billing notice", SIMULATION_LABEL,
                                lambda: CompromisedAgent(SIMULATION_TICKET))}
        self._http = httpx.AsyncClient(timeout=10)

    async def aclose(self) -> None:
        for run in self.runs.values():
            if run.task and not run.task.done():
                run.task.cancel()
        await self._http.aclose()

    def active(self) -> int:
        return sum(1 for r in self.runs.values() if not r.done)

    async def live_get(self, path: str) -> Any:
        r = await self._http.get(self.engine.live_app.base_url + path)
        if r.status_code == 404:
            raise HTTPException(404, "Not found in the live app")
        r.raise_for_status()
        return r.json()

    def get_run(self, rehearsal_id: str) -> Run:
        run = self.runs.get(rehearsal_id)
        if run is None:
            raise HTTPException(404, "Unknown rehearsal")
        return run

    async def start(self, req: StartRequest) -> Run:
        if self.active() >= MAX_ACTIVE:
            raise HTTPException(429, f"{MAX_ACTIVE} rehearsals are already running. Try again in a moment.")
        if self.public_demo:
            if req.ticket_id is None:
                raise HTTPException(403, "The public demo only takes requests about a ticket. Mention a ticket number.")
            req = req.model_copy(update={"message": None})
        ticket = await self.live_get(f"/tickets/{req.ticket_id}") if req.ticket_id else None  # 404 if missing
        if ticket:
            title = f"Ticket {ticket['id']}: {ticket['subject']}"
            request = req.message or f"Please resolve ticket {ticket['id']}."
            task = f"Resolve support ticket {ticket['id']}."
            if req.message:
                task += f" The reviewer adds: {req.message}"
        else:
            title = req.message[:80]
            request = task = req.message
        if req.mode == "simulated":
            simulation = self.simulations.get(req.ticket_id)
            if simulation is None:
                raise HTTPException(422, f"There is no attack simulation for ticket {req.ticket_id}")
            _, label, make = simulation
            model = make()
        elif req.mode == "replay":
            path = RECORDINGS / f"ticket-{req.ticket_id}.json"
            if not path.exists():
                raise HTTPException(404, "No recorded run for this ticket")
            recording = json.loads(path.read_text(encoding="utf-8"))
            label = f"Replay of a recorded {recording.get('model', 'model')} run"
            model = ReplayModel(recording["transcript"], label)
            task = f"Resolve support ticket {req.ticket_id}."  # a recording only fits the request it was made for
        else:
            if self.client is None:
                raise HTTPException(503, "No model API key is configured")
            model, label = self.client, "Live model"
        rehearsal = await self.engine.rehearse(task, actor=label)
        run = Run(rehearsal, req.mode, label, title, request, req.ticket_id)
        self.runs[rehearsal.id] = run
        run.emit("started", {"task": task, "request": request, "mode": req.mode, "model": label})
        run.task = asyncio.create_task(self._drive(run, model, task))
        return run

    async def _drive(self, run: Run, model: Any, task: str) -> None:
        async def sink(kind: str, data: dict) -> None:
            run.emit(kind, data)

        try:
            agent = await run_agent(run.rehearsal, model, task, on_event=sink,
                                    max_steps=40 if run.mode == "simulated" else 24,
                                    system_prompt=self.engine.config.agent_prompt or SYSTEM_PROMPT)
            run.emit("agent_finished", {"summary": agent.summary, "finished": agent.finished,
                                        "models": agent.models, "steps": agent.steps, "error": agent.error})
            if run.rehearsal.state == State.REHEARSING:
                await run.rehearsal.finish(summary=agent.summary)
                await run.rehearsal.apply_if_trusted(finished=agent.finished and not agent.error)
            run.emit("review", rehearsal_dict(run.rehearsal))
            await self.notify(run)
        except Exception as e:  # noqa: BLE001 - surface any failure to the viewer instead of hanging the stream
            run.emit("error", {"message": f"The rehearsal stopped: {type(e).__name__}"})
            if run.rehearsal.state == State.REHEARSING:
                await run.rehearsal.reject(by="system", reason="The rehearsal stopped because of an error")
        finally:
            run.done = True
            run.emit("done", {"state": str(run.rehearsal.state)})

    async def revise(self, run: Run, revision: Revision) -> Run:
        """Send a proposal back: cancel it, then let the agent redo the task on a fresh copy with the feedback."""
        if self.public_demo:
            raise HTTPException(403, "The public demo does not take written instructions")
        if run.mode != "agent":
            raise HTTPException(409, "Only a live agent can redo the work. Recorded runs and simulations are fixed.")
        if not run.done or run.rehearsal.state != State.READY:
            raise HTTPException(409, "Only a proposal that is waiting for a decision can be sent back")
        if self.active() >= MAX_ACTIVE:
            raise HTTPException(429, f"{MAX_ACTIVE} rehearsals are already running. Try again in a moment.")
        await run.rehearsal.reject(by=revision.reviewer, reason=f"Sent back for changes: {revision.feedback}")
        # The feedback is the person's own words, so ids in it count as part of the request.
        task = (f"{run.rehearsal.task}\nA person reviewed your earlier attempt and did not apply it. "
                f"They ask for this change: {revision.feedback}")
        rehearsal = await self.engine.rehearse(task, actor="Live model")
        again = Run(rehearsal, "agent", "Live model", run.title, f"Change requested: {revision.feedback}", run.ticket_id)
        self.runs[rehearsal.id] = again
        again.emit("started", {"task": task, "request": again.request, "mode": "agent", "model": again.model})
        again.task = asyncio.create_task(self._drive(again, self.client, task))
        return again

    async def pause_all(self, by: str) -> int:
        """Stop every rehearsal that is still running. Their private copies are discarded, so nothing
        they did reaches live data."""
        stopped = 0
        for run in list(self.runs.values()):
            if run.done:
                continue
            if run.task and not run.task.done():
                run.task.cancel()
                try:
                    await run.task
                except asyncio.CancelledError:
                    pass
            if run.rehearsal.state == State.REHEARSING:
                await run.rehearsal.reject(by=by, reason="Stopped with the pause button. Live data was never touched.")
            run.emit("error", {"message": "Stopped with the pause button. Nothing reached live data."})
            if not run.done:  # an outside agent's run has no task to finish it
                run.done = True
                run.emit("done", {"state": str(run.rehearsal.state)})
            stopped += 1
        self.engine.audit.write("agents_paused", by=by, stopped=stopped)
        return stopped

    async def notify(self, run: Run) -> None:
        """Push a phone alert through ntfy.sh when a review is waiting (REHEARSAL_NTFY_TOPIC, off by default).
        The alert names the ticket, the risk and the totals only: no customer names, no approval link."""
        topic = os.environ.get("REHEARSAL_NTFY_TOPIC", "")
        r = run.rehearsal
        if not topic or r.state != State.READY or not r.review or not r.replay_calls():
            return
        if not NTFY_TOPIC.fullmatch(topic):
            print("REHEARSAL_NTFY_TOPIC must be 8 to 64 letters, digits, - or _. No alert sent.")
            return
        n = len(r.replay_calls())
        text = (f"{run.title}: {r.review.risk} risk, {n} change{'s' if n != 1 else ''} waiting, "
                f"{money(r.review.money_out_cents)} USD out. Nothing is live until you decide.")
        try:
            await self._http.post(NTFY_URL + topic, content=text.encode("utf-8"),
                                  headers={"Title": "Rehearsal needs your review",
                                           "Priority": "high" if r.review.risk == "high" else "default"})
        except httpx.HTTPError:
            pass  # an alert is a convenience; the review itself is never affected

    async def reset(self) -> None:
        if self.active():
            raise HTTPException(409, "Wait for running rehearsals to finish before resetting")
        target = self.engine.target
        with tempfile.TemporaryDirectory() as tmp:
            fresh = Path(tmp) / "fresh.db"
            await asyncio.to_thread(seed_database, target, fresh)
            await asyncio.to_thread(restore_database, fresh, target.live_database)
        self.runs.clear()
        self.engine.rehearsals.clear()
        self.engine.audit.write("demo_reset", by="console")


REVIEWER_NAME = re.compile(r"[A-Za-z][A-Za-z .'-]{0,39}")


def reviewer_tokens(names: str) -> dict[str, str]:
    """A fresh secret link token for each named reviewer (REHEARSAL_REVIEWERS=Amira,Ben)."""
    people = [n.strip() for n in names.split(",") if n.strip()]
    bad = [n for n in people if not REVIEWER_NAME.fullmatch(n)]
    if bad or len({n.casefold() for n in people}) != len(people):
        raise ValueError("REHEARSAL_REVIEWERS takes distinct plain names separated by commas")
    return {name: secrets.token_urlsafe(24) for name in people}


def create_app(console: Console | None = None, *, token: str | None = None, allowed_hosts: list[str] | None = None,
               public_demo: bool | None = None, reviewers: dict[str, str] | None = None) -> FastAPI:
    """Build the app. Tests pass a ready Console; production builds one in the lifespan.

    Every write needs the launch token (like Jupyter) unless public demo mode is switched on, and only
    requests addressed to an allowed host are served, which stops DNS rebinding attacks from a web page.
    Each named reviewer can also have their own token (name -> token). Two person approval trusts only
    those: the approver's identity comes from the token they hold, never from a name typed in a request.
    """
    if public_demo is None:
        public_demo = os.environ.get("REHEARSAL_PUBLIC_DEMO") == "1"
    token = token or os.environ.get("REHEARSAL_TOKEN") or secrets.token_urlsafe(24)
    if reviewers is None:
        reviewers = reviewer_tokens(os.environ.get("REHEARSAL_REVIEWERS", ""))
    if allowed_hosts is None:
        extra = [h.strip() for h in os.environ.get("REHEARSAL_ALLOWED_HOSTS", "").split(",") if h.strip()]
        allowed_hosts = [*LOOPBACK, *extra]

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if console is not None:
            console.public_demo = public_demo
            app.state.console = console
            yield
            return
        config_mod.load_env()
        guard = PromptGuard()
        engine = await Engine(config_mod.load(), guard=guard).start()
        try:
            client = ChatClient()
        except LLMError:
            client = None
        app.state.console = Console(engine, client, public_demo=public_demo)
        try:
            yield
        finally:
            await app.state.console.aclose()
            if client:
                await client.aclose()
            await guard.aclose()
            await engine.close()

    app = FastAPI(title="Rehearsal", version="1.0.0", lifespan=lifespan,
                  docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json")
    app.state.token = token
    app.state.public_demo = public_demo
    app.state.reviewers = reviewers

    @app.middleware("http")
    async def guard_requests(request: Request, call_next):
        request.state.reviewer = None  # set only when the request carries a named reviewer's own token
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            # JSON only: an HTML form on another site cannot send this content type without a CORS preflight.
            if not request.headers.get("content-type", "").startswith("application/json"):
                return JSONResponse({"detail": "Send JSON with Content-Type: application/json"}, status_code=415)
            sent = request.headers.get(TOKEN_HEADER, "")
            # Compare against every token, so the time taken does not reveal which one nearly matched.
            matches = [name for name, t in reviewers.items() if secrets.compare_digest(sent, t)]
            launch = secrets.compare_digest(sent, token)
            if matches:
                request.state.reviewer = matches[0]
            elif not public_demo and not launch:
                return JSONResponse({"detail": "Open the console with the link printed when Rehearsal started."},
                                    status_code=401)
        response = await call_next(request)
        if request.url.path.startswith("/assets/"):  # file names carry a content hash
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        csp_exempt = request.url.path.startswith("/api/docs")
        for name, value in SECURITY_HEADERS.items():
            if not (csp_exempt and name == "Content-Security-Policy"):
                response.headers.setdefault(name, value)
        return response

    def con(request: Request) -> Console:
        return request.app.state.console

    @app.get("/api/status")
    async def status(request: Request) -> dict:
        c = con(request)
        return {
            "target": c.engine.target.name,
            "public_demo": public_demo,
            "server_file": c.engine.server_file.name,
            "tools": [{"name": t.name, "description": t.description, "writes": t.writes, "method": t.method,
                       "path": t.path} for t in c.engine.tools.values()],
            "models": c.client.status() if c.client else [],
            "prompt_guard": bool(c.engine.guard and getattr(c.engine.guard, "available", False)),
            "recordings": sorted(int(p.stem.split("-")[1]) for p in RECORDINGS.glob("ticket-*.json")),
            "simulation_ticket": SIMULATION_TICKET,
            "simulations": [{"ticket_id": t, "name": name} for t, (name, _, _) in sorted(c.simulations.items())],
            "active": c.active(), "max_active": MAX_ACTIVE,
        }

    @app.get("/api/tickets")
    async def tickets(request: Request) -> list[dict]:
        c = con(request)
        latest: dict[int, Run] = {run.ticket_id: run for run in c.runs.values() if run.ticket_id}
        try:
            rows = await c.live_get("/tickets?limit=50")
        except HTTPException:
            return []  # an app without a ticket queue: the console still takes plain requests
        for row in rows:
            run = latest.get(row["id"])
            row["rehearsal"] = {"id": run.rehearsal.id, "state": str(run.rehearsal.state),
                                "risk": run.rehearsal.review.risk if run.rehearsal.review else None} if run else None
        return rows

    @app.get("/api/tickets/{ticket_id}")
    async def ticket(ticket_id: int, request: Request) -> dict:
        if not 1 <= ticket_id <= 10_000_000:
            raise HTTPException(404, "Not found")
        return await con(request).live_get(f"/tickets/{ticket_id}")

    @app.get("/api/rehearsals")
    async def rehearsals(request: Request) -> list[dict]:
        return [{"id": rid, "title": run.title, "ticket_id": run.ticket_id, "mode": run.mode, "model": run.model,
                 "state": str(run.rehearsal.state), "created_at": run.rehearsal.created_at,
                 "risk": run.rehearsal.review.risk if run.rehearsal.review else None, "running": not run.done,
                 "changes": len(run.rehearsal.review.changes) if run.rehearsal.review else None}
                for rid, run in reversed(con(request).runs.items())]

    @app.post("/api/rehearsals", status_code=201)
    async def start(req: StartRequest, request: Request) -> dict:
        run = await con(request).start(req)
        return {"id": run.rehearsal.id, "mode": run.mode, "model": run.model, "title": run.title}

    @app.get("/api/rehearsals/{rehearsal_id}")
    async def detail(rehearsal_id: str, request: Request) -> dict:
        run = con(request).get_run(rehearsal_id)
        return {**rehearsal_dict(run.rehearsal), **conversation(run), "running": not run.done}

    @app.get("/api/rehearsals/{rehearsal_id}/events")
    async def events(rehearsal_id: str, request: Request) -> StreamingResponse:
        run = con(request).get_run(rehearsal_id)

        last_seen = request.headers.get("last-event-id", "")
        resume_from = int(last_seen) + 1 if last_seen.isdigit() else 0

        async def stream():
            sent = min(resume_from, len(run.events))
            while True:
                while sent < len(run.events):
                    event = run.events[sent]
                    sent += 1
                    yield f"id: {event['id']}\nevent: {event['kind']}\ndata: {json.dumps(event['data'], default=str)}\n\n"
                if run.done and sent >= len(run.events):
                    return
                if await request.is_disconnected():
                    return
                await run.wait(15)
                if sent >= len(run.events):
                    yield ": keep-alive\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    async def decide(rehearsal_id: str, request: Request, action: str, decision: Decision) -> dict:
        run = con(request).get_run(rehearsal_id)
        if not run.done:
            raise HTTPException(409, "The agent is still working. Wait for the review.")
        signed_in = request.state.reviewer  # from a reviewer's own token; None for the shared launch link
        by = signed_in or decision.reviewer
        if action == "approve" and run.rehearsal.needs_second_approval() and not signed_in:
            raise HTTPException(403, "High risk changes need two different people, each using their own reviewer "
                                     "link (set REHEARSAL_REVIEWERS). The shared console link cannot approve them.")
        try:
            if action == "approve":
                await run.rehearsal.approve(by=by)
            elif action == "reject":
                await run.rehearsal.reject(by=by, reason=decision.reason)
            else:
                await run.rehearsal.undo(by=by)
        except InvalidState as e:
            raise HTTPException(409, str(e)) from e
        return {**rehearsal_dict(run.rehearsal), **conversation(run), "running": False}

    @app.post("/api/rehearsals/{rehearsal_id}/approve")
    async def approve(rehearsal_id: str, decision: Decision, request: Request) -> dict:
        return await decide(rehearsal_id, request, "approve", decision)

    @app.post("/api/rehearsals/{rehearsal_id}/reject")
    async def reject(rehearsal_id: str, decision: Decision, request: Request) -> dict:
        return await decide(rehearsal_id, request, "reject", decision)

    @app.post("/api/rehearsals/{rehearsal_id}/revise", status_code=201)
    async def revise(rehearsal_id: str, revision: Revision, request: Request) -> dict:
        c = con(request)
        again = await c.revise(c.get_run(rehearsal_id), revision)
        return {"id": again.rehearsal.id, "mode": again.mode, "model": again.model, "title": again.title}

    @app.post("/api/rehearsals/{rehearsal_id}/undo")
    async def undo(rehearsal_id: str, decision: Decision, request: Request) -> dict:
        return await decide(rehearsal_id, request, "undo", decision)

    @app.get("/api/audit")
    async def audit(request: Request, limit: int = 200) -> dict:
        log = con(request).engine.audit
        entries = log.entries()
        return {"verified": log.verify(), "count": len(entries), "entries": entries[-max(1, min(limit, 1000)):]}

    @app.get("/api/overview")
    async def overview(request: Request) -> dict:
        """The protection dashboard, computed from the audit log."""
        return summarize(con(request).engine.audit.entries())

    @app.get("/api/audit/tamper-check")
    async def tamper_check(request: Request) -> dict:
        """Edit one entry on a copy of the log and show where the hash chain breaks. Nothing is written."""
        return con(request).engine.audit.tamper_check()

    @app.post("/api/reset")
    async def reset(request: Request) -> dict:
        await con(request).reset()
        return {"ok": True}

    @app.post("/api/pause")
    async def pause(decision: Decision, request: Request) -> dict:
        return {"stopped": await con(request).pause_all(by=decision.reviewer)}

    @app.post("/api/tools/reload")
    async def reload_tools(request: Request) -> dict:
        """Pick up a fresh Synapse build (after synapse build --discover) without restarting."""
        c = con(request)
        if public_demo:
            raise HTTPException(403, "Reloading tools is turned off in the public demo")
        if c.active():
            raise HTTPException(409, "Wait for running rehearsals to finish before reloading the tools")
        before = set(c.engine.tools)
        names = await c.engine.reload_tools()
        c.engine.audit.write("tools_reloaded", by="console", server=c.engine.server_file.name, tools=names)
        return {"server_file": c.engine.server_file.name, "tools": names,
                "added": sorted(set(names) - before), "removed": sorted(before - set(names))}

    # Added last so it runs first: anything not addressed to an allowed host is refused outright.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

    if WEB_DIST.exists():
        app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str) -> FileResponse:
            if path == "api" or path.startswith("api/"):
                raise HTTPException(404, "Unknown API path")
            candidate = (WEB_DIST / path).resolve()
            if path and candidate.is_file() and WEB_DIST.resolve() in candidate.parents:
                return FileResponse(candidate)
            # index.html must always be revalidated, or a browser keeps pointing at an old build.
            return FileResponse(WEB_DIST / "index.html", headers={"Cache-Control": "no-cache"})

    return app


app = create_app()


def main() -> None:
    import uvicorn

    host = os.environ.get("REHEARSAL_HOST", "127.0.0.1")
    port = int(os.environ.get("REHEARSAL_PORT", os.environ.get("PORT", "8000")))
    if app.state.public_demo:
        print("PUBLIC DEMO MODE: anyone who can reach this server can approve changes to the demo data.")
    else:
        print()
        print(f"  Rehearsal console: http://127.0.0.1:{port}/?token={app.state.token}")
        print("  The token in this link authorises approvals. Do not share it.")
        for name, reviewer_token in app.state.reviewers.items():
            print(f"  Reviewer link for {name}: http://127.0.0.1:{port}/?token={reviewer_token}  (give it to {name} only)")
        if config_mod.load().trust.second_approval == "high" and len(app.state.reviewers) < 2:
            print("  second_approval is on, but REHEARSAL_REVIEWERS names fewer than two people, so high risk "
                  "changes cannot be approved.")
        print()
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
