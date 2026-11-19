"""The Rehearsal engine.

Every task gets its own shadow copy of the app. The agent's tool calls run there and are
recorded. A reviewer sees exactly which rows would change, with plain risk flags. Approving
replays the recorded writes on the live app, then checks live changed exactly as rehearsed.
"""
from __future__ import annotations

import asyncio
import shutil
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

from . import catalog as catalog_mod
from .apps import AppProcess, seed_database
from .audit import AuditLog
from .config import Config
from .dbdiff import (OwnerResolver, RowChange, copy_database, diff_databases, meaningful, restore_database,
                     same_effect)
from .risk import Flag, assess, money_out, verdict
from .toolserver import CallResult, ToolServer, fetch_openapi


class State(StrEnum):
    REHEARSING = "rehearsing"
    READY = "ready"  # waiting for a decision
    APPLIED = "applied"
    REJECTED = "rejected"
    STALE = "stale"  # live data changed during review; must rehearse again
    FAILED = "failed"  # replay did not match; live was rolled back
    UNDONE = "undone"


class InvalidState(RuntimeError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class RecordedCall:
    index: int
    tool: str
    args: dict[str, Any]
    writes: bool
    ok: bool
    blocked: bool
    result: str
    at: str = field(default_factory=now)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Review:
    changes: list[RowChange]
    owners: list[Any]
    flags: list[Flag]
    risk: str
    money_out_cents: int


@dataclass
class Outcome:
    state: State
    message: str
    verified: bool = False


class Engine:
    """Owns the live app, the live tool server and every rehearsal session."""

    def __init__(self, config: Config):
        self.config = config
        self.target = config.target
        self.sessions_dir = config.runtime_dir / "sessions"
        self.audit = AuditLog(config.runtime_dir / "audit.jsonl")
        self.live_app: AppProcess | None = None
        self.live_server: ToolServer | None = None
        self.tools: dict[str, catalog_mod.Tool] = {}
        self.server_file: Path | None = None
        self.rehearsals: dict[str, Rehearsal] = {}
        self._apply_lock = asyncio.Lock()
        self._logs = config.runtime_dir / "logs"

    async def start(self) -> "Engine":
        self._logs.mkdir(parents=True, exist_ok=True)
        if self.sessions_dir.exists():
            shutil.rmtree(self.sessions_dir, ignore_errors=True)  # shadows from a previous run are stale
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        seed_database(self.target, self.target.live_database)
        self.live_app = await AppProcess(self.target, self.target.live_database, port=self.target.live_port,
                                         log_path=self._logs / "live-app.log").start()
        await self.reload_tools()
        self.audit.write("engine_started", target=self.target.name, server=self.server_file.name,
                         tools=sorted(self.tools))
        return self

    async def reload_tools(self) -> list[str]:
        """Pick up a fresh Synapse build (for example after synapse build --discover)."""
        server_file = self.target.server_file()
        server = await ToolServer(server_file, self.live_app.base_url,
                                  errlog_path=self._logs / "live-mcp.log").start()
        try:
            tools = catalog_mod.load(server_file, await server.list_tools(), await fetch_openapi(self.live_app.base_url))
        except BaseException:
            await server.stop()
            raise
        old, self.live_server, self.tools, self.server_file = self.live_server, server, tools, server_file
        if old:
            await old.stop()
        return sorted(tools)

    async def close(self) -> None:
        for r in list(self.rehearsals.values()):
            await r._stop_shadow()
        if self.live_server:
            await self.live_server.stop()
            self.live_server = None
        if self.live_app:
            await self.live_app.stop()
            self.live_app = None

    async def rehearse(self, task: str, *, actor: str = "agent") -> "Rehearsal":
        r = Rehearsal(self, task, actor)
        await r._start()
        self.rehearsals[r.id] = r
        return r

    def get(self, rehearsal_id: str) -> "Rehearsal":
        return self.rehearsals[rehearsal_id]


class Rehearsal:
    def __init__(self, engine: Engine, task: str, actor: str):
        self.engine = engine
        self.id = uuid.uuid4().hex[:12]
        self.task = task
        self.actor = actor
        self.created_at = now()
        self.state = State.REHEARSING
        self.calls: list[RecordedCall] = []
        self.review: Review | None = None
        self.outcome: Outcome | None = None
        self.dir = engine.sessions_dir / self.id
        self.base_db = self.dir / "base.db"  # live, frozen at the start of the rehearsal
        self.shadow_db = self.dir / "shadow.db"  # what the agent actually changed
        self.pre_apply_db = self.dir / "pre_apply.db"
        self.post_apply_db = self.dir / "post_apply.db"
        self._app: AppProcess | None = None
        self._server: ToolServer | None = None
        self._server_file = engine.server_file

    # ------------------------------------------------------------ rehearsing

    async def _start(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        copy_database(self.engine.target.live_database, self.base_db)
        copy_database(self.base_db, self.shadow_db)
        self._app = await AppProcess(self.engine.target, self.shadow_db,
                                     log_path=self.dir / "shadow-app.log").start()
        try:
            self._server = await ToolServer(self._server_file, self._app.base_url,
                                            errlog_path=self.dir / "shadow-mcp.log").start()
        except BaseException:
            await self._app.stop()
            raise
        self.engine.audit.write("rehearsal_started", rehearsal=self.id, task=self.task, actor=self.actor)

    async def call(self, tool: str, args: dict[str, Any] | None = None) -> CallResult:
        """Run one tool call against the shadow copy and record it."""
        if self.state != State.REHEARSING:
            raise InvalidState(f"Rehearsal {self.id} is {self.state}, not rehearsing")
        spec = self.engine.tools.get(tool)
        if spec is None:
            return self._record(tool, args or {}, False, CallResult(False, f"Unknown tool {tool}"), blocked=True)
        try:
            clean = spec.check(args)
        except catalog_mod.ArgumentError as e:
            return self._record(tool, args or {}, spec.writes,
                                CallResult(False, f"Blocked before reaching the app: {e}"), blocked=True)
        return self._record(tool, clean, spec.writes, await self._server.call(tool, clean))

    def _record(self, tool: str, args: dict, writes: bool, result: CallResult, blocked: bool = False) -> CallResult:
        call = RecordedCall(len(self.calls) + 1, tool, args, writes, result.ok, blocked, result.text)
        self.calls.append(call)
        self.engine.audit.write("call", rehearsal=self.id, index=call.index, tool=tool, args=args,
                                writes=writes, ok=result.ok, blocked=blocked)
        return result

    async def _stop_shadow(self) -> None:
        if self._server:
            await self._server.stop()
            self._server = None
        if self._app:
            await self._app.stop()
            self._app = None

    async def finish(self) -> Review:
        """Stop the shadow and work out exactly what the agent changed."""
        if self.state != State.REHEARSING:
            raise InvalidState(f"Rehearsal {self.id} is {self.state}, not rehearsing")
        await self._stop_shadow()
        target, cfg = self.engine.target, self.engine.config.risk
        changes = meaningful(diff_databases(self.base_db, self.shadow_db), target.volatile_columns)
        after, before = OwnerResolver(self.shadow_db, target.owner_table), OwnerResolver(self.base_db, target.owner_table)
        try:
            owners = [(before if c.kind == "removed" else after).owner_of(c.table, c.after or c.before)
                      for c in changes]
        finally:
            after.close()
            before.close()
        failed = sum(1 for c in self.calls if not c.ok and not c.blocked)
        blocked = sum(1 for c in self.calls if c.blocked)
        flags = assess(changes, owners, failed, blocked, target, cfg)
        self.review = Review(changes, owners, flags, verdict(flags),
                             sum(cents for _, cents in money_out(changes, target)))
        self.state = State.READY
        self.engine.audit.write("review_ready", rehearsal=self.id, rows_changed=len(changes),
                                risk=self.review.risk, flags=[f.title for f in flags],
                                money_out_cents=self.review.money_out_cents)
        return self.review

    # ------------------------------------------------------------ decisions

    def replay_calls(self) -> list[RecordedCall]:
        return [c for c in self.calls if c.writes and c.ok]

    async def approve(self, *, by: str) -> Outcome:
        target = self.engine.target
        async with self.engine._apply_lock:
            # Known limit: assumes Rehearsal is the only writer during the replay window (well under a second).
            # Production would add an app-side lock or idempotency keys.
            if self.state != State.READY:
                raise InvalidState(f"Rehearsal {self.id} is {self.state}; only a ready rehearsal can be approved")
            if self.engine.server_file != self._server_file:
                return self._decide(State.STALE, by, "The tool server was rebuilt since this rehearsal. "
                                                     "Rehearse the task again.")
            live = target.live_database
            drift = meaningful(diff_databases(self.base_db, live), target.volatile_columns)
            if drift:
                return self._decide(State.STALE, by, f"Live data changed in {len(drift)} rows since this rehearsal "
                                                     "started. Nothing was applied. Rehearse the task again.")
            copy_database(live, self.pre_apply_db)
            for call in self.replay_calls():
                result = await self.engine.live_server.call(call.tool, call.args)
                if not result.ok:
                    restore_database(self.pre_apply_db, live)
                    return self._decide(State.FAILED, by, f"Replay of call {call.index} ({call.tool}) failed: "
                                                          f"{result.text}. Live data was restored.")
            applied = diff_databases(self.pre_apply_db, live)
            if not same_effect(applied, self.review.changes, target.volatile_columns):
                restore_database(self.pre_apply_db, live)
                return self._decide(State.FAILED, by, "Live changes did not match the rehearsal, so live data "
                                                      "was restored. Nothing was kept.")
            copy_database(live, self.post_apply_db)
            n = len(self.replay_calls())
            return self._decide(State.APPLIED, by, f"Applied {n} change{'s' if n != 1 else ''} to live data. "
                                                   "Live now matches the rehearsal exactly.", verified=True)

    async def reject(self, *, by: str, reason: str = "") -> Outcome:
        if self.state not in (State.READY, State.REHEARSING):
            raise InvalidState(f"Rehearsal {self.id} is {self.state} and cannot be rejected")
        await self._stop_shadow()
        return self._decide(State.REJECTED, by, reason or "Rejected. Live data was never touched.")

    async def undo(self, *, by: str) -> Outcome:
        target = self.engine.target
        async with self.engine._apply_lock:
            if self.state != State.APPLIED:
                raise InvalidState(f"Rehearsal {self.id} is {self.state}; only applied changes can be undone")
            since = meaningful(diff_databases(self.post_apply_db, target.live_database), target.volatile_columns)
            if since:
                raise InvalidState(f"Live data changed in {len(since)} rows after this was applied, "
                                   "so an automatic undo could erase other work.")
            restore_database(self.pre_apply_db, target.live_database)
            return self._decide(State.UNDONE, by, "Undone. Live data is back to how it was before approval.")

    def _decide(self, state: State, by: str, message: str, verified: bool = False) -> Outcome:
        self.state = state
        self.outcome = Outcome(state, message, verified)
        self.engine.audit.write(f"rehearsal_{state}", rehearsal=self.id, by=by, message=message, verified=verified)
        return self.outcome
