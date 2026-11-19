"""Concurrent refunds against a real server must never exceed the order total."""
import os
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

APP_DIR = Path(__file__).resolve().parents[2] / "legacy" / "kestrel_desk"
sys.path.insert(0, str(APP_DIR))
import seed  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_parallel_refunds_are_serialised(tmp_path):
    db = tmp_path / "kestrel.db"
    seed.seed(str(db))
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(port), "--workers", "1"],
        cwd=APP_DIR, env={**os.environ, "KESTREL_DB": str(db)},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                requests.get(f"{base}/health", timeout=0.5)
                break
            except requests.RequestException:
                time.sleep(0.1)
        # Order 5001 totals 189.00; eight parallel 100.00 refunds may only succeed once.
        with ThreadPoolExecutor(8) as pool:
            codes = list(pool.map(lambda _: requests.post(
                f"{base}/orders/5001/refunds", json={"amount": 100, "reason": "race"}, timeout=10).status_code,
                range(8)))
        assert codes.count(201) == 1 and codes.count(409) == 7, codes
        assert requests.get(f"{base}/orders/5001").json()["refunded"] == "100.00"
    finally:
        proc.terminate()
        proc.wait(10)
