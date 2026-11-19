"""Start and stop instances of the protected app: the live one and short-lived shadow copies."""
from __future__ import annotations

import asyncio
import os
import socket
import subprocess
from pathlib import Path

import httpx

from .config import Target


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def app_env(target: Target, database: Path) -> dict[str, str]:
    # Model and Synapse keys belong to Rehearsal, not to the app it protects.
    env = {k: v for k, v in os.environ.items() if not k.upper().endswith(("_API_KEY", "_TOKEN", "_SECRET"))}
    env[target.database_env] = str(database)
    return env


class AppProcess:
    """One running copy of the app, bound to one database file and one local port."""

    def __init__(self, target: Target, database: Path, *, port: int | None = None, log_path: Path | None = None):
        self.target = target
        self.database = Path(database)
        self.port = port or free_port()
        self.log_path = log_path
        self._proc: subprocess.Popen | None = None
        self._log = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def start(self, timeout: float = 20) -> "AppProcess":
        command = self.target.render(self.target.command, port=self.port, database=self.database)
        self._log = open(self.log_path, "ab") if self.log_path else subprocess.DEVNULL
        self._proc = subprocess.Popen(command, cwd=self.target.app_dir, env=app_env(self.target, self.database),
                                      stdout=self._log, stderr=subprocess.STDOUT)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        async with httpx.AsyncClient(timeout=1) as client:
            while True:
                if self._proc.poll() is not None:
                    await self.stop()
                    raise RuntimeError(f"{self.target.name} exited during startup (port {self.port}); "
                                       f"see {self.log_path or 'the app log'}")
                try:
                    r = await client.get(self.base_url + self.target.health_path)
                    if r.status_code < 500:
                        return self
                except httpx.HTTPError:
                    pass
                if loop.time() > deadline:
                    await self.stop()
                    raise RuntimeError(f"{self.target.name} did not become ready on port {self.port}")
                await asyncio.sleep(0.1)

    async def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                await asyncio.to_thread(self._proc.wait, 10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                await asyncio.to_thread(self._proc.wait, 10)
        if self._log not in (None, subprocess.DEVNULL):
            self._log.close()
        self._log = None


def seed_database(target: Target, database: Path) -> None:
    """Create the live database with the app's own seed command if it does not exist yet."""
    if database.exists() or not target.seed_command:
        return
    database.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(target.render(target.seed_command, database=database), cwd=target.app_dir,
                   env=app_env(target, database), check=True, capture_output=True)
