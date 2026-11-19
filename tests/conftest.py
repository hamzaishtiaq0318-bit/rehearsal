"""Shared fixtures: a real Kestrel Desk process on a free port with fresh demo data."""
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "legacy" / "kestrel_desk"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(APP_DIR))

import seed  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def running_app(db_path: Path):
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=APP_DIR, env={**os.environ, "KESTREL_DB": str(db_path)},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 20
        while True:
            if proc.poll() is not None:
                raise RuntimeError("Kestrel Desk exited during startup")
            try:
                requests.get(f"{base}/health", timeout=0.5)
                break
            except requests.RequestException:
                if time.monotonic() > deadline:
                    raise RuntimeError("Kestrel Desk did not start in time")
                time.sleep(0.1)
        yield base
    finally:
        proc.terminate()
        proc.wait(10)


@pytest.fixture()
def kestrel(tmp_path):
    db = tmp_path / "kestrel.db"
    seed.seed(str(db))
    with running_app(db) as base:
        yield base, db


@pytest.fixture()
def anyio_backend():
    return "asyncio"
