import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Body, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.db import repository
from app.deps import (
    get_db, embed_text, canonical_item_text, fingerprint_for,
    vec_to_pgvector, SessionLocal, photo_root,
    get_active_vision_model, get_max_image_px,
    VISION_BASE_URL, VISION_API_KEY, logger,
)
from app.services.detection import detect, get_model_name
from app.services.rate_limiter import require_vision_rate_limit
from app.services.suggest_tracker import get_tracker
from app.services.vision import describe_photo

router = APIRouter()

_SUGGEST_MATCH_THRESHOLD = float(os.environ.get("SUGGEST_MATCH_THRESHOLD", "0.85"))


def _is_path_under_photo_root(fpath: Path) -> bool:
    """Return True iff ``fpath`` resolves to a location under ``photo_root``.

    Uses ``Path.resolve()`` so symlinks are followed — a symlink pointing out
    of the photo root is rejected.
    """
    try:
        resolved = fpath.resolve()
        root_resolved = photo_root.resolve()
    except (OSError, RuntimeError):
        return False
    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        return False
    return True


def _resolve_photo_path_under_root(photo_path: str | None) -> Path:
    """Resolve a DB-stored photo path and confirm it lives under ``photo_root``.

    Returns the resolved ``Path`` on success. Raises ``HTTPException(404)`` for
    any failure mode — missing row, missing file, symlink escape, or path
    outside the configured photo root. 404 (not 403) to avoid leaking row
    existence to unauthenticated probes (FF-03).
    """
    if not photo_path:
        raise HTTPException(status_code=404, detail="photo not found")
    fpath = Path(photo_path)
    try:
        resolved = fpath.resolve(strict=True)
    except (FileNotFoundError, OSError, RuntimeError):
        raise HTTPException(status_code=404, detail="photo not found")
    if not _is_path_under_photo_root(resolved):
        logger.warning(
            "event=photo_path_escape path=%s resolved=%s",
            photo_path, resolved,
        )
        raise HTTPException(status_code=404, detail="photo not found")
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="photo not found")
    return resolved

_MIME_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".heif": "image/heif",
}


@router.get("/photos/{photo_id}/suggest", dependencies=[Depends(require_vision_rate_limit)])
def suggest_for_photo(
    photo_id: int,
    model: Optional[str] = Query(None, description="Override vision model for this request"),
    request: Request = None,
):
    request_id = getattr(request.state, "request_id", None) if request else None

    # Pre-vision validation (not tracked; 404s stay invisible to the status endpoint).
    db1 = SessionLocal()
    try:
        if not repository.photo_exists(db1, photo_id):
            raise HTTPException(status_code=404, detail="photo not found")
        photo_path = repository.fetch_photo_path(db1, photo_id)
    finally:
        db1.close()

    # FF-03: confine to photo_root before reading from disk.
    resolved_path = _resolve_photo_path_under_root(photo_path)

    tracker = get_tracker()
    tracker.start(photo_id)
    try:
        logger.info("event=photo_suggest_vision_start request_id=%s photo_id=%s", request_id, photo_id)
        vision_model = model or get_active_vision_model()
        vision_hits, vision_elapsed_ms = describe_photo(
            str(resolved_path), VISION_BASE_URL, VISION_API_KEY,
            vision_model, get_max_image_px(), photo_id=photo_id,
        )
        logger.info("event=photo_suggest_vision_done request_id=%s photo_id=%s ms=%s hits=%s", request_id, photo_id, vision_elapsed_ms, len(vision_hits))
    except Exception as exc:
        tracker.mark_failed(photo_id, type(exc).__name__.lower())
        logger.exception("event=photo_suggest_crash request_id=%s photo_id=%s", request_id, photo_id)
        raise

    tracker.update_stage(photo_id, "embedding_match")

    # Phase 2: fresh DB connection for embedding search
    # Every vision hit becomes a suggestion. If there's a strong DB match,
    # we annotate it with the existing item_id and bins.
    db2 = SessionLocal()
    try:
        suggestions: list[dict] = []

        for hit in vision_hits:
            name = (hit.get("name") or "").strip()
            category = hit.get("category")
            vision_conf = float(hit.get("confidence") or 0.5)
            if not name:
                continue

            suggestion: dict = {
                "item_id": None,
                "name": name,
                "category": category,
                "confidence": round(vision_conf, 4),
                "bbox": hit.get("bbox"),
                "bins": [],
                "match": None,
            }

            try:
                qvec = embed_text(canonical_item_text(name, category, None))
                matches = repository.search_items_by_embedding(db2, vec_to_pgvector(qvec), limit=1)
                if matches:
                    m = matches[0]
                    score = float(m["score"])
                    if score >= _SUGGEST_MATCH_THRESHOLD:
                        suggestion["match"] = {
                            "item_id": m["item_id"],
                            "name": m["name"],
                            "category": m["category"],
                            "score": round(score, 4),
                            "bins": list(m["bins"]) if m["bins"] else [],
                        }
            except Exception:
                pass

            suggestions.append(suggestion)

        suggestions.sort(key=lambda s: (-s["confidence"], s["name"] or ""))
    finally:
        db2.close()

    tracker.mark_done(photo_id)

    logger.info(
        "event=photo_suggest request_id=%s photo_id=%s model=%s vision_elapsed_ms=%s vision_hits=%s suggestions=%s",
        request_id,
        photo_id,
        vision_model,
        vision_elapsed_ms,
        len(vision_hits),
        len(suggestions),
    )
    return {
        "version": "1",
        "photo_id": photo_id,
        "model": vision_model,
        "vision_elapsed_ms": vision_elapsed_ms,
        "suggestions": suggestions,
    }


@router.get("/photos/{photo_id}/suggest/status")
def suggest_status(photo_id: int):
    """Finding #18: lightweight liveness probe for an in-flight or recently-completed
    /suggest job. Safe to poll at ~5s intervals. 404 when no recent job exists."""
    from datetime import datetime, timezone

    tracker = get_tracker()
    entry = tracker.get(photo_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="no suggest job for this photo")
    started_at = (
        datetime.fromtimestamp(entry.started_at, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    return {
        "version": "1",
        "photo_id": photo_id,
        "state": entry.state,
        "stage": entry.stage,
        "started_at": started_at,
        "elapsed_ms": tracker.elapsed_ms(entry),
        "error_code": entry.error_code,
    }


@router.get("/photos/{photo_id}/file")
def get_photo_file(
    photo_id: int,
    w: Optional[int] = Query(None, ge=16, le=4096, description="Resize to this width (aspect ratio preserved). Omit for original."),
    db: Session = Depends(get_db),
):
    if not repository.photo_exists(db, photo_id):
        raise HTTPException(status_code=404, detail="photo not found")

    photo_path = repository.fetch_photo_path(db, photo_id)
    # FF-03: confine to photo_root (rejects symlink escapes and legacy rows
    # whose path points outside PHOTO_DIR). 404 to avoid leaking row existence.
    fpath = _resolve_photo_path_under_root(photo_path)

    ext = fpath.suffix.lower()
    content_type = _MIME_TYPES.get(ext, "application/octet-stream")

    if w is None:
        return Response(content=fpath.read_bytes(), media_type=content_type)

    from PIL import Image
    import io
    with Image.open(fpath) as img:
        img = img.convert("RGB")
        ratio = w / img.width
        h = int(img.height * ratio)
        img = img.resize((w, h), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return Response(content=buf.getvalue(), media_type="image/jpeg")


@router.delete("/photos/{photo_id}")
def delete_photo(
    photo_id: int,
    db: Session = Depends(get_db),
):
    deleted_path = repository.delete_photo(db, photo_id)
    if not deleted_path:
        raise HTTPException(status_code=404, detail="photo not found")

    db.commit()

    # F-01: verify stored path is still within photo_root before unlinking.
    # Uses the shared _is_path_under_photo_root helper (FF-03).
    fpath = Path(deleted_path)
    if not _is_path_under_photo_root(fpath):
        logger.error(
            "event=photo_delete_path_escape photo_id=%s path=%s",
            photo_id, deleted_path,
        )
        raise HTTPException(status_code=400, detail="photo path outside photo root")
    if fpath.is_file():
        fpath.unlink()

    logger.info(
        "event=photo_delete request_id=%s photo_id=%s path=%s",
        db.info.get("request_id"),
        photo_id,
        deleted_path,
    )
    return {"version": "1", "photo_id": photo_id, "deleted": True}


@router.post("/photos/{photo_id}/detect", dependencies=[Depends(require_vision_rate_limit)])
def detect_for_photo(
    photo_id: int,
    db: Session = Depends(get_db),
):
    if not repository.photo_exists(db, photo_id):
        raise HTTPException(status_code=404, detail="photo not found")

    photo_path = repository.fetch_photo_path(db, photo_id)
    # FF-03: confine to photo_root before feeding into detection.
    resolved_path = _resolve_photo_path_under_root(photo_path)

    detections = detect(str(resolved_path))
    model = get_model_name()
    repository.insert_photo_detections(db, photo_id, model, detections)
    repository.clear_detection_groups(db, photo_id, model)
    db.commit()

    logger.info(
        "event=photo_detect request_id=%s photo_id=%s model=%s detections=%s",
        db.info.get("request_id"),
        photo_id,
        model,
        len(detections),
    )
    return {
        "version": "1",
        "photo_id": photo_id,
        "model": model,
        "detections": detections,
    }


@router.get("/photos/{photo_id}/groups")
def groups_for_photo(
    photo_id: int,
    db: Session = Depends(get_db),
):
    if not repository.photo_exists(db, photo_id):
        raise HTTPException(status_code=404, detail="photo not found")

    model = get_model_name()
    groups = repository.fetch_cached_groups(db, photo_id, model)
    if not groups:
        groups = repository.compute_groups_from_detections(db, photo_id, model)
        repository.clear_detection_groups(db, photo_id, model)
        repository.insert_detection_groups(db, photo_id, model, groups)
        db.commit()
    logger.info(
        "event=photo_groups request_id=%s photo_id=%s model=%s groups=%s",
        db.info.get("request_id"),
        photo_id,
        model,
        len(groups),
    )
    return {"version": "1", "photo_id": photo_id, "model": model, "groups": groups}


@router.post("/photos/{photo_id}/confirm")
def confirm_photo_groups(
    photo_id: int,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
):
    if not repository.photo_exists(db, photo_id):
        raise HTTPException(status_code=404, detail="photo not found")

    bin_id = (payload.get("bin_id") or "").strip()
    if not bin_id:
        raise HTTPException(status_code=400, detail="bin_id is required")

    version = payload.get("version")
    if version != "1":
        raise HTTPException(status_code=400, detail="version must be '1'")

    selected_groups = payload.get("selected_groups") or []
    if not isinstance(selected_groups, list):
        raise HTTPException(status_code=400, detail="selected_groups must be a list")
    if not selected_groups:
        raise HTTPException(status_code=400, detail="selected_groups must not be empty")

    try:
        repository.ensure_bin_active_or_create(db, bin_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="bin not found")

    model = get_model_name()
    results = []
    try:
        for g in selected_groups:
            group_key = (g.get("group_key") or "").strip()
            label = (g.get("label") or "").strip()
            category = (g.get("category") or "").strip()
            if not group_key:
                raise HTTPException(status_code=400, detail="group_key is required")
            if not label:
                raise HTTPException(status_code=400, detail="label is required")
            if not category:
                raise HTTPException(status_code=400, detail="category is required")
            quantity = g.get("quantity")

            item_id, inserted = repository.insert_item_with_status(db, label, category, None)
            linked = repository.insert_bin_item(db, bin_id, item_id, None, quantity)
            repository.insert_photo_group_item(db, photo_id, model, label, category, item_id)

            status = "created" if inserted else "updated"
            if not linked:
                status = "linked"

            results.append(
                {
                    "group_key": group_key or fingerprint_for(label, category),
                    "item_id": item_id,
                    "fingerprint": fingerprint_for(label, category),
                    "status": status,
                }
            )

        db.commit()
        logger.info(
            "event=photo_confirm request_id=%s photo_id=%s bin_id=%s item_ids=%s",
            db.info.get("request_id"),
            photo_id,
            bin_id,
            [i["item_id"] for i in results],
        )
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        logger.exception("event=confirm_failed request_id=%s photo_id=%s", db.info.get("request_id"), photo_id)
        raise HTTPException(status_code=500, detail="internal error") from None

    return {"version": "1", "photo_id": photo_id, "bin_id": bin_id, "results": results}
