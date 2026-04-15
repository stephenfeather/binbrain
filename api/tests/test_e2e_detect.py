"""E2E test: upload photo -> detect -> verify YOLO-World detections use seed classes.

Requires:
  - binbrain_api container running at localhost:8000
  - binbrain_db container running at localhost:5434
  - A real image at samples/image_01-1200x900.jpg

Run:
  cd api && uv run python -m pytest tests/test_e2e_detect.py -v -s
"""
from __future__ import annotations

import hashlib
import os
import secrets

import httpx
import psycopg
import pytest

API_BASE = os.environ.get("BINBRAIN_API_URL", "http://localhost:8000")
DB_DSN = os.environ.get(
    "BINBRAIN_DB_DSN",
    "postgresql://binbrain:claude_dev@localhost:5434/binbrain",
)
SAMPLE_IMAGE = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "samples", "image_01-1200x900.jpg",
)


@pytest.fixture(scope="module")
def api_key() -> str:
    """Create a temporary API key directly in the database."""
    raw_key = "bb_" + secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    try:
        with psycopg.connect(DB_DSN) as conn:
            conn.execute(
                "INSERT INTO api_keys (key_hash, name) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (key_hash, "e2e-test"),
            )
            conn.commit()
    except psycopg.OperationalError:
        pytest.skip(f"E2E database not reachable at {DB_DSN}")
    return raw_key


@pytest.fixture(scope="module")
def seed_classes() -> list[str]:
    """Fetch the active confirmed classes from the database."""
    try:
        with psycopg.connect(DB_DSN) as conn:
            rows = conn.execute(
                "SELECT lower(trim(class_name)) FROM confirmed_classes WHERE removed_at IS NULL"
            ).fetchall()
    except psycopg.OperationalError:
        pytest.skip(f"E2E database not reachable at {DB_DSN}")
    if not rows:
        pytest.skip("No confirmed classes in DB -- seed classes first")
    return [r[0] for r in rows]


@pytest.fixture(scope="module")
def client(api_key: str) -> httpx.Client:
    return httpx.Client(
        base_url=API_BASE,
        headers={"X-API-Key": api_key},
        timeout=120,  # model load can be slow on first call
    )


def _require_sample_image():
    if not os.path.isfile(SAMPLE_IMAGE):
        pytest.skip(f"Sample image not found: {SAMPLE_IMAGE}")


class TestE2EDetect:
    """Upload a real photo, run YOLO-World detection, verify results."""

    @pytest.fixture(autouse=True)
    def _check_api(self):
        try:
            resp = httpx.get(f"{API_BASE}/health", timeout=5)
            if not resp.is_success:
                pytest.skip("API not reachable at " + API_BASE)
        except httpx.ConnectError:
            pytest.skip("API not reachable at " + API_BASE)

    def test_upload_detect_verify(self, client, seed_classes):
        _require_sample_image()

        # 1. Upload photo via /ingest
        bin_id = f"BIN-E2E-{secrets.token_hex(4).upper()}"
        with open(SAMPLE_IMAGE, "rb") as f:
            resp = client.post(
                "/ingest",
                data={"bin_id": bin_id},
                files={"photos": ("test.jpg", f, "image/jpeg")},
            )
        assert resp.status_code == 200, f"Ingest failed: {resp.text}"
        body = resp.json()
        photo_id = body["photos"][0]["photo_id"]

        # 2. Call detect
        resp = client.post(f"/photos/{photo_id}/detect")
        assert resp.status_code == 200, f"Detect failed: {resp.text}"
        detect_body = resp.json()

        assert detect_body["photo_id"] == photo_id
        assert "model" in detect_body
        detections = detect_body["detections"]

        # 3. Verify detections structure
        assert isinstance(detections, list)
        for det in detections:
            assert "label" in det
            assert "confidence" in det
            assert "bbox" in det
            assert isinstance(det["bbox"], list)
            assert len(det["bbox"]) == 4
            assert 0 < det["confidence"] <= 1.0

        # 4. Verify detection labels come from confirmed_classes vocabulary
        if detections:
            labels = {d["label"].lower() for d in detections}
            seed_set = set(seed_classes)
            unknown = labels - seed_set
            assert not unknown, (
                f"Detections used labels outside seed classes: {unknown}. "
                f"Seed classes: {seed_set}. Detection labels: {labels}"
            )

        # 5. Verify detections were persisted to DB
        with psycopg.connect(DB_DSN) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM photo_detections WHERE photo_id = %s",
                (photo_id,),
            ).fetchone()[0]
        assert count == len(detections), (
            f"DB has {count} detections but API returned {len(detections)}"
        )
