"""FF-03 (Medium): Read-side photo path confinement.

A legacy `photos.path` row, or a row written via DB tampering, must not
become an arbitrary local file-read primitive via ``GET /photos/{id}/file``
or an arbitrary path feed into detection via ``POST /photos/{id}/detect``.

Both handlers must:
  - Resolve the stored path
  - Confirm it lies under ``photo_root``
  - Return 404 (not 200, not 403, not 500) if it does not — 404 to avoid
    leaking row existence.
  - Not invoke the detector / stream bytes when the path escapes the root.

Fixture pattern follows ``test_security_f05_file_validation.py`` and
``test_security_f10_path_disclosure.py``.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import text


@pytest.fixture()
def _seed_bin(client, app_module):
    """Create a bin the photos rows can attach to."""
    from app.deps import SessionLocal
    db = SessionLocal()
    try:
        db.execute(
            text(
                "INSERT INTO bins (bin_id) VALUES (:bin_id) "
                "ON CONFLICT (bin_id) DO NOTHING"
            ),
            {"bin_id": "FF03BIN01"},
        )
        db.commit()
        yield "FF03BIN01"
        db.execute(
            text("DELETE FROM photos WHERE bin_id = :bin_id"),
            {"bin_id": "FF03BIN01"},
        )
        db.execute(
            text("DELETE FROM bins WHERE bin_id = :bin_id"),
            {"bin_id": "FF03BIN01"},
        )
        db.commit()
    finally:
        db.close()


def _insert_photo_row(bin_id: str, path: str) -> int:
    from app.deps import SessionLocal
    db = SessionLocal()
    try:
        photo_id = db.execute(
            text(
                "INSERT INTO photos (bin_id, path) "
                "VALUES (:bin_id, :path) RETURNING photo_id"
            ),
            {"bin_id": bin_id, "path": path},
        ).scalar()
        db.commit()
        return int(photo_id)
    finally:
        db.close()


# ── /photos/{id}/file ─────────────────────────────────────────────────────────

def test_file_rejects_path_outside_photo_root(client, _seed_bin):
    """Row whose path is outside PHOTO_DIR must return 404 from /file."""
    photo_id = _insert_photo_row(_seed_bin, "/etc/passwd")
    resp = client.get(f"/photos/{photo_id}/file")
    assert resp.status_code == 404, (
        f"Expected 404 for out-of-root path, got {resp.status_code}: {resp.text}"
    )
    # No contents of /etc/passwd should leak regardless of body shape.
    assert b"root:" not in resp.content


def test_file_rejects_nonexistent_out_of_root_path(client, _seed_bin):
    """Nonexistent out-of-root path still returns 404 (not 500)."""
    photo_id = _insert_photo_row(_seed_bin, "/tmp/ff03_does_not_exist_xyz.jpg")
    resp = client.get(f"/photos/{photo_id}/file")
    assert resp.status_code == 404, (
        f"Expected 404 for missing path, got {resp.status_code}: {resp.text}"
    )


def test_file_rejects_symlink_escape(client, _seed_bin, tmp_path, valid_jpeg_bytes):
    """A symlink inside photo_root that resolves outside it must be rejected."""
    from app.deps import photo_root

    # Target sits outside photo_root.
    outside_target = tmp_path / "outside.jpg"
    outside_target.write_bytes(valid_jpeg_bytes)

    # Symlink lives inside photo_root but points outside.
    photo_root.mkdir(parents=True, exist_ok=True)
    link = photo_root / "ff03_escape.jpg"
    try:
        if link.exists() or link.is_symlink():
            link.unlink()
        os.symlink(outside_target, link)
    except OSError:
        pytest.skip("symlink creation not supported in this environment")

    photo_id = _insert_photo_row(_seed_bin, str(link))
    try:
        resp = client.get(f"/photos/{photo_id}/file")
        assert resp.status_code == 404, (
            f"Expected 404 for symlink-escape, got {resp.status_code}: {resp.text}"
        )
    finally:
        if link.exists() or link.is_symlink():
            link.unlink()


def test_file_serves_in_root_photo(client, _seed_bin, valid_jpeg_bytes):
    """Positive control: a valid in-root photo still serves normally."""
    from app.deps import photo_root

    photo_root.mkdir(parents=True, exist_ok=True)
    photo_file = photo_root / "ff03_valid.jpg"
    photo_file.write_bytes(valid_jpeg_bytes)

    photo_id = _insert_photo_row(_seed_bin, str(photo_file))
    try:
        resp = client.get(f"/photos/{photo_id}/file")
        assert resp.status_code == 200, (
            f"Expected 200 for in-root photo, got {resp.status_code}: {resp.text}"
        )
        assert resp.content == valid_jpeg_bytes
    finally:
        if photo_file.exists():
            photo_file.unlink()


# ── /photos/{id}/detect ───────────────────────────────────────────────────────

def test_detect_rejects_path_outside_photo_root_and_skips_detector(
    client, _seed_bin, app_module
):
    """Out-of-root path returns 404 AND does NOT invoke the detector."""
    import app.routes.photos as photos_route

    calls: list[str] = []
    original = photos_route.detect
    photos_route.detect = lambda p: calls.append(p) or []
    try:
        photo_id = _insert_photo_row(_seed_bin, "/etc/passwd")
        resp = client.post(f"/photos/{photo_id}/detect")
        assert resp.status_code == 404, (
            f"Expected 404 for out-of-root path, got {resp.status_code}: {resp.text}"
        )
        assert calls == [], (
            f"Detector must not be invoked on out-of-root path; got calls={calls}"
        )
    finally:
        photos_route.detect = original


def test_detect_accepts_in_root_photo(client, _seed_bin, valid_jpeg_bytes):
    """Positive control: in-root photo reaches the (stubbed) detector."""
    from app.deps import photo_root

    photo_root.mkdir(parents=True, exist_ok=True)
    photo_file = photo_root / "ff03_detect_valid.jpg"
    photo_file.write_bytes(valid_jpeg_bytes)

    photo_id = _insert_photo_row(_seed_bin, str(photo_file))
    try:
        resp = client.post(f"/photos/{photo_id}/detect")
        assert resp.status_code == 200, (
            f"Expected 200 for in-root photo, got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert body["photo_id"] == photo_id
    finally:
        if photo_file.exists():
            photo_file.unlink()


# ── Audit script (FF-03 §3) ──────────────────────────────────────────────────

def _load_audit_module():
    """Load api/scripts/audit_photo_paths.py by file path (not a package)."""
    script_path = (
        Path(__file__).resolve().parent.parent / "scripts" / "audit_photo_paths.py"
    )
    spec = importlib.util.spec_from_file_location(
        "audit_photo_paths_ff03", script_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["audit_photo_paths_ff03"] = module
    spec.loader.exec_module(module)
    return module


def test_audit_script_flags_out_of_root_rows(_seed_bin, capsys):
    """The operator audit script flags rows whose path escapes photo_root."""
    audit = _load_audit_module()

    _insert_photo_row(_seed_bin, "/etc/passwd")
    rc = audit.main()
    out = capsys.readouterr().out

    assert rc == 1, f"audit_photo_paths should exit 1 when offenders exist; out:\n{out}"
    assert "/etc/passwd" in out
    assert "offenders" in out
