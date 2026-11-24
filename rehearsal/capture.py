"""Capture real engine runs for the website's browser demo (web/src/demo-data.json).

Usage: python -m rehearsal.capture

Each scenario runs through the real engine and the real console API on a fresh copy of the demo
store: the recorded model runs and the scripted attack, so no API key is needed. Every review is
then approved and undone on one rehearsal and cancelled on a second, so the demo can show the
engine's real answer to each choice.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import httpx

from . import config, gallery, server
from .apps import free_port, seed_database
from .engine import Engine

OUT = config.ROOT / "web" / "src" / "demo-data.json"
HOST = "capture"
MAX_TEXT = 1500


async def _run(client: httpx.AsyncClient, console: server.Console, ticket_id: int, mode: str) -> dict:
    started = await client.post("/api/rehearsals", json={"ticket_id": ticket_id, "mode": mode})
    started.raise_for_status()
    rid = started.json()["id"]
    await console.runs[rid].task
    detail = (await client.get(f"/api/rehearsals/{rid}")).json()
    return {"id": rid, "detail": detail, "events": console.runs[rid].events}


def _event(e: dict) -> dict:
    data = {} if e["kind"] == "review" else e["data"]  # the console re-reads the review itself
    if isinstance(data.get("text"), str):
        data = {**data, "text": data["text"][:MAX_TEXT]}
    return {"kind": e["kind"], "data": data}


async def _decide(client: httpx.AsyncClient, rid: str, action: str) -> dict:
    r = await client.post(f"/api/rehearsals/{rid}/{action}", json={"reviewer": "Demo reviewer"})
    r.raise_for_status()
    return r.json()["outcome"]


async def capture() -> dict:
    config.load_env()
    cfg = config.load()
    tmp = Path(tempfile.mkdtemp(prefix="rehearsal-capture-"))
    target = replace(cfg.target, live_database=tmp / "live.db", live_port=free_port())
    seed_database(target, target.live_database)
    installed = gallery.install(target.live_database)  # the attack gallery's tickets, as if sent by customers
    engine = await Engine(replace(cfg, target=target, runtime_dir=tmp)).start()
    console = server.Console(engine, None, public_demo=True)
    gallery.register(console, installed)
    app = server.create_app(console, allowed_hosts=[HOST], public_demo=True)
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"http://{HOST}",
                                         timeout=120) as client:
                status = (await client.get("/api/status")).json()
                tickets = (await client.get("/api/tickets")).json()
                scenarios_wanted = [(s["ticket_id"], "simulated") for s in status["simulations"]] + \
                    [(t, "replay") for t in status["recordings"]]
                details = {str(t): (await client.get(f"/api/tickets/{t}")).json()
                           for t in sorted({t for t, _ in scenarios_wanted})}
                scenarios = []
                for ticket_id, mode in scenarios_wanted:
                    first = await _run(client, console, ticket_id, mode)
                    ready = first["detail"]
                    for call in ready["calls"]:
                        call["result"] = call["result"][:MAX_TEXT]
                    outcomes = {"applied": None, "rejected": None, "undone": None}
                    if ready["state"] == "applied":  # the trust policy applied it without asking
                        outcomes["applied"] = ready["outcome"]
                        outcomes["undone"] = await _decide(client, first["id"], "undo")
                    else:
                        if ready["actions"]:
                            outcomes["applied"] = await _decide(client, first["id"], "approve")
                            outcomes["undone"] = await _decide(client, first["id"], "undo")
                        second = await _run(client, console, ticket_id, mode)
                        outcomes["rejected"] = await _decide(client, second["id"], "reject")
                    scenarios.append({"ticket_id": ticket_id, "mode": mode, "title": ready["title"],
                                      "request": ready["request"], "model": ready["model"],
                                      "events": [_event(e) for e in first["events"]],
                                      "ready": ready, "outcomes": outcomes})
                    print(f"ticket {ticket_id} {mode}: {len(ready['actions'])} actions, "
                          f"risk {(ready['review'] or {}).get('risk')}, {ready['state']}", flush=True)
                audit = (await client.get("/api/audit")).json()
                tamper = (await client.get("/api/audit/tamper-check")).json()
    finally:
        await console.aclose()
        await engine.close()
        shutil.rmtree(tmp, ignore_errors=True)  # the scratch copies of the store
    return {"captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "status": status,
            "tickets": tickets, "ticket_details": details, "audit": audit, "tamper": tamper, "scenarios": scenarios}


def main() -> None:
    data = asyncio.run(capture())
    OUT.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
    print(f"Wrote {OUT} ({OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
