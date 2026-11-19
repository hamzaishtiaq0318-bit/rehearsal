"""Concurrent refunds against a real server must never exceed the order total."""
from concurrent.futures import ThreadPoolExecutor

import requests


def test_parallel_refunds_are_serialised(kestrel):
    base, _ = kestrel
    # Order 5001 totals 189.00; eight parallel 100.00 refunds may only succeed once.
    with ThreadPoolExecutor(8) as pool:
        codes = list(pool.map(lambda _: requests.post(
            f"{base}/orders/5001/refunds", json={"amount": 100, "reason": "race"}, timeout=10).status_code,
            range(8)))
    assert codes.count(201) == 1 and codes.count(409) == 7, codes
    assert requests.get(f"{base}/orders/5001").json()["refunded"] == "100.00"
