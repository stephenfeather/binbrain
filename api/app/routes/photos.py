from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Body, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.db import repository
from app.deps import (
    get_db, embed_text, canonical_item_text, fingerprint_for,
    vec_to_pgvector, SessionLocal,
    get_active_vision_model, get_max_image_px,
    OLLAMA_URL, logger,
)
from app.services.detection import detect
from app.services.vision import describe_photo

router = APIRouter()

_SUGGEST_MATCH_THRESHOLD = 0.5

_MIME_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".heif": "image/heif",
}


@router.get("/photos/{photo_id}/suggest")
def suggest_for_photo(
    photo_id: int,
    model: Optional[str] = Query(None, description="Override vision model for this request"),
    request: Request = None,
):
    request_id = getattr(request.state, "request_id", None) if request else None

    try:
        # Phase 1: fetch photo path, then close DB to avoid idle connection timeout
        # during the potentially long vision call
        db1 = SessionLocal()
        try:
            if not repository.photo_exists(db1, photo_id):
                raise HTTPException(status_code=404, detail="photo not found")
            photo_path = repository.fetch_photo_path(db1, photo_id)
        finally:
            db1.close()

        logger.info("event=photo_suggest_vision_start request_id=%s photo_id=%s", request_id, photo_id)
        vision_model = model or get_active_vision_model()
        vision_hits, vision_elapsed_ms = (
            describe_photo(photo_path, OLLAMA_URL, vision_model, get_max_image_px()) if photo_path else ([], 0)
        )
        logger.info("event=photo_suggest_vision_done request_id=%s photo_id=%s ms=%s hits=%s", request_id, photo_id, vision_elapsed_ms, len(vision_hits))
    except HTTPException:
        raise
    except Exception:
        logger.exception("event=photo_suggest_crash request_id=%s photo_id=%s", request_id, photo_id)
        raise

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


@router.get("/photos/{photo_id}/file")
def get_photo_file(
    photo_id: int,
    w: Optional[int] = Query(None, ge=16, le=4096, description="Resize to this width (aspect ratio preserved). Omit for original."),
    db: Session = Depends(get_db),
):
    if not repository.photo_exists(db, photo_id):
        raise HTTPException(status_code=404, detail="photo not found")

    photo_path = repository.fetch_photo_path(db, photo_id)
    if not photo_path:
        raise HTTPException(status_code=404, detail="photo not found")

    fpath = Path(photo_path)
    if not fpath.is_file():
        raise HTTPException(status_code=404, detail="photo file missing from disk")

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

    # Remove file from disk
    fpath = Path(deleted_path)
    if fpath.is_file():
        fpath.unlink()

    logger.info(
        "event=photo_delete request_id=%s photo_id=%s path=%s",
        db.info.get("request_id"),
        photo_id,
        deleted_path,
    )
    return {"version": "1", "photo_id": photo_id, "deleted": True}


@router.post("/photos/{photo_id}/detect")
def detect_for_photo(
    photo_id: int,
    db: Session = Depends(get_db),
):
    if not repository.photo_exists(db, photo_id):
        raise HTTPException(status_code=404, detail="photo not found")

    photo_path = repository.fetch_photo_path(db, photo_id)
    if not photo_path:
        raise HTTPException(status_code=404, detail="photo not found")

    detections = detect(photo_path)
    model = "stub"
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

    model = "stub"
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

    model = "stub"
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
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"confirm failed: {e}")

    return {"version": "1", "photo_id": photo_id, "bin_id": bin_id, "results": results}
