"""Rehearsal web server: the review console's API, live agent event streams, and the built console UI.

Run: python -m rehearsal.server   (binds 127.0.0.1:8000 unless REHEARSAL_HOST / REHEARSAL_PORT say otherwise)
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator

from . import config as config_mod
from .agent import ReplayModel, run_agent
from .apps import seed_database
from .dbdiff import restore_database
from .engine import Engine, InvalidState, Rehearsal, State
from .guard import PromptGuard
from .llm import ChatClient, LLMError
from .present import rehearsal_dict
from .scenarios import LABEL as SIMULATION_LABEL
from .scenarios import CompromisedAgent

ROOT = Path(__file__).resolve().parents[1]
WEB_DIST = ROOT / "web" / "dist"
RECORDINGS = ROOT / "demo" / "recordings"
MAX_ACTIVE = int(os.environ.get("REHEARSAL_MAX_ACTIVE", "3"))
SIMULATION_TICKET = 1006
TOKEN_HEADER = "X-Rehearsal-Token"
LOOPBACK = ("127.0.0.1", "localhost", "::1")

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


class Decision(BaseModel):
    reviewer: str = Field(default="Reviewer", min_length=1, max_length=60)
    reason: str = Field(default="", max_length=500)

    @field_validator("reviewer", "reason")
    @classmethod
    def plain_text(cls, value: str) -> str:
        value = " ".join(value.split())
        if any(ord(ch) < 32 for ch in value):
            raise ValueError("control characters are not allowed")
        return value


def conversation(run: Run) -> dict:
    return {"mode": run.mode, "model": run.model, "title": run.title, "request": run.request,
            "ticket_id": run.ticket_id}


class Console:
    """Shared state behind the API: the engine, the model client, and every run."""

    def __init__(self, engine: Engine, client: Any):
        self.engine = engine
        self.client = client
        self.runs: dict[str, Run] = {}
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
            if req.ticket_id != SIMULATION_TICKET:
                raise HTTPException(422, f"The simulated compromised agent only runs on ticket {SIMULATION_TICKET}")
            model, label = CompromisedAgent(req.ticket_id), SIMULATION_LABEL
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
                                    max_steps=40 if run.mode == "simulated" else 24)
            run.emit("agent_finished", {"summary": agent.summary, "finished": agent.finished,
                                        "models": agent.models, "steps": agent.steps, "error": agent.error})
            if run.rehearsal.state == State.REHEARSING:
                await run.rehearsal.finish()
            run.emit("review", rehearsal_dict(run.rehearsal))
        except Exception as e:  # noqa: BLE001 - surface any failure to the viewer instead of hanging the stream
            run.emit("error", {"message": f"The rehearsal stopped: {type(e).__name__}"})
            if run.rehearsal.state == State.REHEARSING:
                await run.rehearsal.reject(by="system", reason="The rehearsal stopped because of an error")
        finally:
            run.done = True
            run.emit("done", {"state": str(run.rehearsal.state)})

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


def create_app(console: Console | None = None, *, token: str | None = None, allowed_hosts: list[str] | None = None,
               public_demo: bool | None = None) -> FastAPI:
    """Build the app. Tests pass a ready Console; production builds one in the lifespan.

    Every write needs the launch token (like Jupyter) unless public demo mode is switched on, and only
    requests addressed to an allowed host are served, which stops DNS rebinding attacks from a web page.
    """
    if public_demo is None:
        public_demo = os.environ.get("REHEARSAL_PUBLIC_DEMO") == "1"
    token = token or os.environ.get("REHEARSAL_TOKEN") or secrets.token_urlsafe(24)
    if allowed_hosts is None:
        extra = [h.strip() for h in os.environ.get("REHEARSAL_ALLOWED_HOSTS", "").split(",") if h.strip()]
        allowed_hosts = [*LOOPBACK, *extra]

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if console is not None:
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
        app.state.console = Console(engine, client)
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

    @app.middleware("http")
    async def guard_requests(request: Request, call_next):
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            # JSON only: an HTML form on another site cannot send this content type without a CORS preflight.
            if not request.headers.get("content-type", "").startswith("application/json"):
                return JSONResponse({"detail": "Send JSON with Content-Type: application/json"}, status_code=415)
            if not public_demo and not secrets.compare_digest(request.headers.get(TOKEN_HEADER, ""), token):
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
            "active": c.active(), "max_active": MAX_ACTIVE,
        }

    @app.get("/api/tickets")
    async def tickets(request: Request) -> list[dict]:
        c = con(request)
        latest: dict[int, Run] = {run.ticket_id: run for run in c.runs.values() if run.ticket_id}
        rows = await c.live_get("/tickets?limit=50")
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
        try:
            if action == "approve":
                await run.rehearsal.approve(by=decision.reviewer)
            elif action == "reject":
                await run.rehearsal.reject(by=decision.reviewer, reason=decision.reason)
            else:
                await run.rehearsal.undo(by=decision.reviewer)
        except InvalidState as e:
            raise HTTPException(409, str(e)) from e
        return {**rehearsal_dict(run.rehearsal), **conversation(run), "running": False}

    @app.post("/api/rehearsals/{rehearsal_id}/approve")
    async def approve(rehearsal_id: str, decision: Decision, request: Request) -> dict:
        return await decide(rehearsal_id, request, "approve", decision)

    @app.post("/api/rehearsals/{rehearsal_id}/reject")
    async def reject(rehearsal_id: str, decision: Decision, request: Request) -> dict:
        return await decide(rehearsal_id, request, "reject", decision)

    @app.post("/api/rehearsals/{rehearsal_id}/undo")
    async def undo(rehearsal_id: str, decision: Decision, request: Request) -> dict:
        return await decide(rehearsal_id, request, "undo", decision)

    @app.get("/api/audit")
    async def audit(request: Request, limit: int = 200) -> dict:
        log = con(request).engine.audit
        entries = log.entries()
        return {"verified": log.verify(), "count": len(entries), "entries": entries[-max(1, min(limit, 1000)):]}

    @app.post("/api/reset")
    async def reset(request: Request) -> dict:
        await con(request).reset()
        return {"ok": True}

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
        print()
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
