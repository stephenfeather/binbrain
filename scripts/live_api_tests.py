#!/usr/bin/env python3
import os
import sys
import uuid

import httpx


def fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    sys.exit(1)


def main() -> None:
    base = os.environ.get("API_BASE_URL", "http://localhost:8000").rstrip("/")
    bin_id = os.environ.get("BIN_ID", f"BIN-LIVE-{uuid.uuid4().hex[:8]}")

    with httpx.Client(timeout=15.0) as client:
        r = client.get(f"{base}/health")
        if r.status_code != 200:
            fail(f"/health status {r.status_code}")

        item_payload = {
            "name": "Live Test Item",
            "category": "live-test",
            "bin_id": bin_id,
            "quantity": 1,
        }
        r = client.post(f"{base}/items", data=item_payload)
        if r.status_code != 200:
            fail(f"/items status {r.status_code}: {r.text}")
        item_id = r.json().get("item_id")
        if not item_id:
            fail("/items missing item_id")

        r = client.get(f"{base}/search", params={"q": "Live Test Item", "limit": 5, "offset": 0})
        if r.status_code != 200:
            fail(f"/search status {r.status_code}: {r.text}")

        r = client.get(f"{base}/bins/{bin_id}")
        if r.status_code != 200:
            fail(f"/bins/{{bin_id}} status {r.status_code}: {r.text}")
        body = r.json()
        if body.get("bin_id") != bin_id:
            fail("/bins/{bin_id} returned wrong bin_id")

        r = client.get(f"{base}/bins")
        if r.status_code != 200:
            fail(f"/bins status {r.status_code}: {r.text}")
        if not isinstance(r.json().get("bins"), list):
            fail("/bins response missing 'bins' list")

    print("OK")


if __name__ == "__main__":
    main()
