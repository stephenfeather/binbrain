"""FF-03: Audit `photos.path` rows for entries outside PHOTO_DIR.

Reads every row from the `photos` table, resolves each stored filesystem
path, and reports rows whose path does not lie under the configured
``photo_root``. Exits 0 if none are found, 1 otherwise.

This script is read-only — it does NOT delete or mutate rows. An operator
should review offending rows before taking action (manual quarantine /
schema update / remediation migration).

Run (from repo root):

    uv run python api/scripts/audit_photo_paths.py

Requires the same environment the API uses (``DATABASE_URL``, ``PHOTO_DIR``).
"""

from __future__ import annotations

import sys
from pathlib import Path


def _resolve_under_root(raw: str | None, root: Path) -> tuple[bool, str]:
    """Return (is_under_root, reason). reason is "" when ok."""
    if not raw:
        return False, "empty path"
    fpath = Path(raw)
    try:
        resolved = fpath.resolve()
    except (OSError, RuntimeError) as exc:
        return False, f"resolve failed: {exc!r}"
    try:
        root_resolved = root.resolve()
    except (OSError, RuntimeError) as exc:
        return False, f"photo_root resolve failed: {exc!r}"
    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        return False, f"outside photo_root (resolved={resolved})"
    if not resolved.is_file():
        return False, f"file missing on disk (resolved={resolved})"
    return True, ""


def main() -> int:
    from app.deps import SessionLocal, photo_root
    from sqlalchemy import text

    total = 0
    offenders: list[tuple[int, str, str]] = []

    db = SessionLocal()
    try:
        rows = (
            db.execute(text("SELECT photo_id, path FROM photos ORDER BY photo_id")).mappings().all()
        )
        for row in rows:
            total += 1
            ok, reason = _resolve_under_root(row["path"], photo_root)
            if not ok:
                offenders.append((row["photo_id"], row["path"] or "", reason))
    finally:
        db.close()

    print(f"photo_root = {photo_root.resolve()}")
    print(f"scanned    = {total} rows")
    print(f"offenders  = {len(offenders)}")
    for photo_id, path, reason in offenders:
        print(f"  photo_id={photo_id} path={path!r} reason={reason}")

    if offenders:
        print(
            "\nACTION: review the offending rows above. These rows point "
            "outside PHOTO_DIR and are rejected (404) by read-side handlers. "
            "Quarantine them via a targeted SQL DELETE / UPDATE after review. "
            "This script is read-only and will NOT mutate the database."
        )
        return 1
    print("\nAll photo paths are confined to photo_root.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
