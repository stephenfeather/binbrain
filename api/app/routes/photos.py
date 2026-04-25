import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Optional

from app.db import repository
from app.deps import (
    VISION_API_KEY,
    VISION_BASE_URL,
    SessionLocal,
    canonical_item_text,
    embed_text,
    fingerprint_for,
    get_active_vision_model,
    get_db,
    get_max_image_px,
    get_suggest_match_threshold,
    logger,
    photo_root,
    require_api_key_id,
    vec_to_pgvector,
)
from app.routes.bins import guard_user_bin_name
from app.services.detection import detect, get_model_name
from app.services.rate_limiter import require_vision_rate_limit
from app.services.suggest_tracker import get_tracker
from app.services.upc_lookup import extract_upc_from_device_metadata
from app.services.vision import PROMPT_VERSION, describe_photo
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, model_validator
from pydantic_core import PydanticCustomError
from sqlalchemy.orm import Session

router = APIRouter()


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
        raise HTTPException(status_code=404, detail="photo not found") from None
    if not _is_path_under_photo_root(resolved):
        logger.warning(
            "event=photo_path_escape path=%s resolved=%s",
            photo_path,
            resolved,
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


def _hit_to_detection_row(hit: dict) -> Optional[dict]:
    """Convert a vision hit ({name, category, confidence, bbox}) into the
    shape expected by ``repository.insert_photo_detections``
    ({label, category, confidence, bbox[4]}). Returns None when the hit lacks
    a usable name or bbox — those rows can't be persisted safely.
    """
    name = (hit.get("name") or "").strip()
    bbox = hit.get("bbox")
    if not name or not bbox or len(bbox) != 4:
        return None
    return {
        "label": name,
        "category": hit.get("category"),
        "confidence": float(hit.get("confidence") or 0.0),
        "bbox": list(bbox),
    }


def _detection_row_to_hit(row: dict) -> dict:
    """Convert a persisted photo_detections row back into the vision-hit shape
    the suggest builder downstream expects ({name, category, confidence, bbox})."""
    return {
        "name": row["label"],
        "category": row["category"],
        "confidence": row["confidence"],
        "bbox": row["bbox"],
    }


@router.get("/photos/{photo_id}/suggest", dependencies=[Depends(require_vision_rate_limit)])
def suggest_for_photo(
    photo_id: int,
    model: Optional[str] = Query(None, description="Override vision model for this request"),
    refresh: bool = Query(
        False, description="Bypass the photo_detections cache and force a fresh vision call."
    ),
    request: Request = None,
):
    request_id = getattr(request.state, "request_id", None) if request else None

    # Pre-vision validation (not tracked; 404s stay invisible to the status
    # endpoint and are NOT recorded in vision_calls because the vision_model
    # is not yet resolved — see Dev2_018 migration comment).
    db1 = SessionLocal()
    try:
        if not repository.photo_exists(db1, photo_id):
            raise HTTPException(status_code=404, detail="photo not found")
        photo_path = repository.fetch_photo_path(db1, photo_id)
    finally:
        db1.close()

    # FF-03: confine to photo_root before reading from disk.
    resolved_path = _resolve_photo_path_under_root(photo_path)

    vision_model = model or get_active_vision_model()

    # Dev2_018: telemetry bookkeeping. Initialised before any branch that may
    # raise so the finally-block write has a consistent row to emit.
    started_at = datetime.now(UTC)
    flags: dict = {"stages": []}
    outcome = "ok"
    error_code: str | None = None
    hits_count: int | None = None
    cached_flag = False
    # Dev2_016b: on a cache hit we echo the prompt_version stamped at write
    # time (historical lineage), not the live PROMPT_VERSION constant.
    response_prompt_version: str | None = PROMPT_VERSION
    # Dev2_018: hit_idx -> photo_detections.id for wiring match-telemetry FKs.
    detection_id_by_hit_idx: dict[int, int] = {}
    vision_hits: list[dict] = []
    vision_elapsed_ms = 0

    tracker = get_tracker()
    tracker.start(photo_id)
    flags["stages"].append("resolve")

    try:
        # -- cache path ------------------------------------------------------
        if not refresh:
            db_read = SessionLocal()
            try:
                cached_rows = repository.get_photo_detections(db_read, photo_id, vision_model)
            finally:
                db_read.close()
            if cached_rows:
                vision_hits = [_detection_row_to_hit(r) for r in cached_rows]
                detection_id_by_hit_idx = {i: r["id"] for i, r in enumerate(cached_rows)}
                vision_elapsed_ms = 0
                cached_flag = True
                response_prompt_version = cached_rows[0]["prompt_version"]
                logger.info(
                    "event=photo_suggest_cache_hit request_id=%s photo_id=%s model=%s hits=%s",
                    request_id,
                    photo_id,
                    vision_model,
                    len(vision_hits),
                )

        # -- fresh VLM path --------------------------------------------------
        if not cached_flag:
            flags["stages"].append("vlm")
            logger.info(
                "event=photo_suggest_vision_start request_id=%s photo_id=%s",
                request_id,
                photo_id,
            )
            vision_hits, vision_elapsed_ms = describe_photo(
                str(resolved_path),
                VISION_BASE_URL,
                VISION_API_KEY,
                vision_model,
                get_max_image_px(),
                photo_id=photo_id,
                flags_out=flags,
            )
            logger.info(
                "event=photo_suggest_vision_done request_id=%s photo_id=%s ms=%s hits=%s",
                request_id,
                photo_id,
                vision_elapsed_ms,
                len(vision_hits),
            )

            # Persist detections for future cache hits. Clear first so re-runs
            # replace rather than accumulate (decision 1: "latest vision
            # answer wins"). RETURNING ids lets us FK match-telemetry rows.
            db_write = SessionLocal()
            try:
                repository.clear_photo_detections(db_write, photo_id, vision_model)
                persistable: list[tuple[int, dict]] = []
                for i, h in enumerate(vision_hits):
                    row = _hit_to_detection_row(h)
                    if row is not None:
                        persistable.append((i, row))
                if persistable:
                    detection_ids = repository.insert_photo_detections(
                        db_write,
                        photo_id,
                        vision_model,
                        [r for _, r in persistable],
                        prompt_version=PROMPT_VERSION,
                    )
                    detection_id_by_hit_idx = {
                        hit_idx: det_id
                        for (hit_idx, _), det_id in zip(persistable, detection_ids, strict=True)
                    }
                db_write.commit()
            finally:
                db_write.close()

        tracker.update_stage(photo_id, "embedding_match")
        flags["stages"].append("embed")

        # -- embedding match + per-match telemetry ---------------------------
        # Decision 7: read SUGGEST_MATCH_THRESHOLD exactly once per invocation
        # and write the same value on every match row for this call. S-01
        # promoted this knob into the runtime settings store; the DB is the
        # source of truth, fed through get_suggest_match_threshold().
        threshold_at_compute = get_suggest_match_threshold()

        db2 = SessionLocal()
        try:
            suggestions: list[dict] = []
            match_rows: list[dict] = []

            for hit_idx, hit in enumerate(vision_hits):
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
                    matches = repository.search_items_by_embedding(
                        db2, vec_to_pgvector(qvec), limit=1
                    )
                    if matches:
                        m = matches[0]
                        score = float(m["score"])
                        above = score >= threshold_at_compute
                        # Dev2_018: persist the match (score, threshold) per
                        # detection so threshold tuning is answerable from
                        # data. NULL matched_item_id when below threshold —
                        # that rejection signal is exactly what makes the
                        # table useful for tuning.
                        det_id = detection_id_by_hit_idx.get(hit_idx)
                        if det_id is not None:
                            match_rows.append(
                                {
                                    "photo_detection_id": det_id,
                                    "matched_item_id": int(m["item_id"]) if above else None,
                                    "score": score,
                                    "threshold_at_compute": threshold_at_compute,
                                }
                            )
                        if above:
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

            # Dev2_018 (PR#21 review follow-up): match-telemetry is as
            # best-effort as the vision_calls row. A DB failure here must not
            # break the /suggest response contract.
            if match_rows:
                try:
                    repository.insert_photo_suggestion_matches(db2, rows=match_rows)
                    db2.commit()
                except Exception as m_exc:
                    logger.warning(
                        "event=photo_suggestion_matches_write_failed photo_id=%s rows=%s err=%s",
                        photo_id,
                        len(match_rows),
                        m_exc,
                    )
        finally:
            db2.close()

        flags["stages"].append("match")
        tracker.mark_done(photo_id)

        hits_count = len(suggestions)
        logger.info(
            "event=photo_suggest request_id=%s photo_id=%s model=%s "
            "vision_elapsed_ms=%s vision_hits=%s suggestions=%s",
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
            "cached": cached_flag,
            "prompt_version": response_prompt_version,
            "suggestions": suggestions,
        }
    except Exception as exc:
        # Dev2_018 (PR#21 review follow-up): any exception reaching the outer
        # try — VLM call, detection persistence, embedding match — updates the
        # telemetry fields before the finally block writes the row. Without
        # this, a late-stage DB failure would return HTTP 500 while
        # vision_calls recorded outcome=ok, corrupting the error-rate metric.
        outcome = "error"
        error_code = type(exc).__name__
        tracker.mark_failed(photo_id, type(exc).__name__.lower())
        logger.exception(
            "event=photo_suggest_crash request_id=%s photo_id=%s stage=%s error_code=%s",
            request_id,
            photo_id,
            flags.get("stages", [])[-1] if flags.get("stages") else None,
            error_code,
        )
        raise
    finally:
        # Dev2_018: best-effort vision_calls telemetry. Runs on every terminal
        # state (success, cache hit, or error). NEVER lets its own failure
        # affect the /suggest response contract — a raising writer is logged
        # and swallowed so the user-facing behaviour is unchanged.
        try:
            elapsed_ms = int((datetime.now(UTC) - started_at).total_seconds() * 1000)
            # Telemetry uses a fresh session intentionally: if the main
            # /suggest session rolled back due to an exception, the
            # vision_calls row still needs to persist. Pool checkout cost is
            # microseconds versus the value of preserving the error row.
            db_tel = SessionLocal()
            try:
                repository.insert_vision_call(
                    db_tel,
                    photo_id=photo_id,
                    model=vision_model,
                    prompt_version=response_prompt_version,
                    base_url=VISION_BASE_URL,
                    started_at=started_at,
                    elapsed_ms=elapsed_ms,
                    hits_count=hits_count,
                    cached=cached_flag,
                    outcome=outcome,
                    error_code=error_code,
                    flags=flags,
                )
                db_tel.commit()
            finally:
                db_tel.close()
        except Exception as tel_exc:
            logger.warning(
                "event=vision_call_telemetry_write_failed photo_id=%s err=%s",
                photo_id,
                tel_exc,
            )


@router.get("/photos/{photo_id}/suggest/status")
def suggest_status(photo_id: int):
    """Finding #18: lightweight liveness probe for an in-flight or recently-completed
    /suggest job. Safe to poll at ~5s intervals. 404 when no recent job exists."""
    from datetime import datetime

    tracker = get_tracker()
    entry = tracker.get(photo_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="no suggest job for this photo")
    started_at = (
        datetime.fromtimestamp(entry.started_at, tz=UTC)
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
    request: Request,
    w: Optional[int] = Query(
        None,
        ge=16,
        le=4096,
        description="Resize to this width (aspect ratio preserved). Omit for original.",
    ),
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
    etag = f'"{photo_id}-{w or 0}"'
    cache_headers = {
        "Cache-Control": "public, max-age=31536000, immutable",
        "ETag": etag,
    }

    inm = request.headers.get("if-none-match")
    if inm is not None and (inm.strip() == "*" or etag in (t.strip() for t in inm.split(","))):
        return Response(status_code=304, headers=cache_headers)

    if w is None:
        return Response(
            content=fpath.read_bytes(),
            media_type=content_type,
            headers=cache_headers,
        )

    import io

    from PIL import Image

    with Image.open(fpath) as img:
        img = img.convert("RGB")
        ratio = w / img.width
        h = int(img.height * ratio)
        img = img.resize((w, h), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return Response(
            content=buf.getvalue(),
            media_type="image/jpeg",
            headers=cache_headers,
        )


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
            photo_id,
            deleted_path,
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
    # SEC-43-1: validate bin_id (format + reserved) BEFORE hitting the DB.
    # Cheap input validation beats a 404 roundtrip when both are wrong, and
    # lets the cross-route reserved-rejection tests run without needing a
    # seeded photo row.
    raw_bin_id = (payload.get("bin_id") or "").strip()
    if not raw_bin_id:
        raise HTTPException(status_code=400, detail="bin_id is required")
    bin_id = guard_user_bin_name(raw_bin_id)

    if not repository.photo_exists(db, photo_id):
        raise HTTPException(status_code=404, detail="photo not found")

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
        raise HTTPException(status_code=404, detail="bin not found") from None

    model = get_model_name()
    metadata_upc = extract_upc_from_device_metadata(
        repository.fetch_photo_device_metadata(db, photo_id)
    )
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

            item_id, inserted = repository.insert_item_with_status(
                db, label, category, None, upc=metadata_upc
            )
            linked = repository.insert_bin_item(db, bin_id, item_id, None, quantity)
            repository.insert_photo_group_item(db, photo_id, model, label, category, item_id)
            repository.link_suggestion_outcomes_to_item(
                db,
                photo_id=photo_id,
                label=label,
                category=category,
                item_id=item_id,
            )

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
        logger.exception(
            "event=confirm_failed request_id=%s photo_id=%s", db.info.get("request_id"), photo_id
        )
        raise HTTPException(status_code=500, detail="internal error") from None

    return {"version": "1", "photo_id": photo_id, "bin_id": bin_id, "results": results}


class SuggestionOutcome(BaseModel):
    """Per-suggestion decision row for POST /photos/{id}/outcomes.

    Dev2_017 (Phase 2 data capture). ``bbox`` is optional — some presented
    suggestions legitimately have no bbox (e.g. whole-image fallbacks); when
    present it MUST have exactly 4 elements. ``edited_to_label`` is required
    iff ``decision == 'edited'``. ``shown_at`` MUST be tz-aware — naive
    datetimes would be interpreted in the Postgres server timezone for the
    ``timestamptz`` column and skew analytics.
    """

    label: str
    category: Optional[str] = None
    confidence: Optional[float] = None
    bbox: Optional[list[float]] = None
    shown_at: datetime
    decision: Literal["accepted", "rejected", "edited", "ignored"]
    edited_to_label: Optional[str] = None
    # S-PROV-02: iOS knows the item_id at outcomes-POST time (it just
    # called /items → /associate). Previously the server dropped it on
    # the floor and relied on /photos/{id}/confirm to stitch item_id
    # via link_suggestion_outcomes_to_item — but iOS never calls
    # /confirm, so every outcome row landed with item_id=NULL. Accept
    # it here and persist directly. Optional for backwards compat with
    # older clients; FK (photo_suggestion_outcomes_item_id_fkey,
    # ON DELETE SET NULL) rejects ids that don't reference an item.
    item_id: Optional[int] = None

    @model_validator(mode="after")
    def _check_shape(self) -> "SuggestionOutcome":
        # PydanticCustomError serializes cleanly in RequestValidationError.errors();
        # a raw ValueError embeds the exception instance in ctx and breaks
        # the app's JSON error handler.
        self.label = self.label.strip() if self.label else ""
        if not self.label:
            raise PydanticCustomError("label_empty", "label must be non-empty")
        if self.category is not None:
            trimmed = self.category.strip()
            self.category = trimmed or None
        if self.edited_to_label is not None:
            trimmed = self.edited_to_label.strip()
            self.edited_to_label = trimmed or None
        if self.bbox is not None and len(self.bbox) != 4:
            raise PydanticCustomError(
                "bbox_length",
                "bbox must have exactly 4 elements [x1,y1,x2,y2]",
            )
        if self.decision == "edited" and not self.edited_to_label:
            raise PydanticCustomError(
                "edited_requires_label",
                "edited_to_label is required and non-empty when decision is 'edited'",
            )
        if self.shown_at.tzinfo is None or self.shown_at.tzinfo.utcoffset(self.shown_at) is None:
            raise PydanticCustomError(
                "shown_at_naive",
                "shown_at must be timezone-aware (e.g. ISO-8601 with 'Z' or offset)",
            )
        return self


class SuggestionOutcomesRequest(BaseModel):
    vision_model: str
    prompt_version: Optional[str] = None
    decisions: list[SuggestionOutcome]


# Postgres ``int`` (int4) upper bound. Values above this would raise
# ``numeric_value_out_of_range`` at INSERT time and the blanket
# exception handler would turn that into a 500. Telemetry must never
# 5xx on input — clamp at the parser (SEC-33-1).
_INT32_MAX = 2_147_483_647


def _parse_client_retry_count(raw: str | None) -> int:
    """Parse the ``X-Client-Retry-Count`` header.

    ApiDev2_005 (Swift2b-gamma). This is telemetry, not validation — a missing
    or malformed value MUST NOT 400/500. Missing → 0 (first-attempt success).
    Malformed → 0 (absorb client bugs rather than rejecting real outcomes).
    Negative values clamped to 0. Values above ``_INT32_MAX`` clamped to
    ``_INT32_MAX`` so the int4 column never rejects the INSERT (SEC-33-1).
    """
    if raw is None:
        return 0
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    if value < 0:
        return 0
    if value > _INT32_MAX:
        return _INT32_MAX
    return value


# RFC 4122 UUID: 8-4-4-4-12 hex digits, case-insensitive. We normalize to
# lowercase before storing/comparing so the client's ``.uuidString.lowercased()``
# round-trips exactly. No trailing whitespace allowed — the regex is anchored.
_IDEMPOTENCY_KEY_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _validate_idempotency_key(raw: str) -> str:
    """Return the normalized (lowercase) UUID string, or raise 400.

    SEC-26-3 / ApiDev_idempotency_outcomes. Malformed keys never reach the
    DB — they are a client bug and we surface them loudly via
    ``400 invalid_idempotency_key`` so a bad SDK build does not silently
    lose dedup across an app update.
    """
    normalized = raw.lower()
    if not _IDEMPOTENCY_KEY_RE.match(normalized):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_idempotency_key",
                "message": "Idempotency-Key must be an RFC 4122 UUID",
            },
        )
    return normalized


def _coerce_stored_body(response_body) -> dict:
    """``response_body`` column is jsonb; psycopg typically returns it as a
    dict, but fixture-inserted rows (see TTL cleanup test) land as strings.
    Normalize so the route always emits a dict-shaped body.
    """
    if isinstance(response_body, str):
        return json.loads(response_body)
    return response_body


def _maybe_idempotent_replay(
    existing: dict | None,
    *,
    body_sha256: bytes,
    response: Response,
    request_id: str | None,
    api_key_id: int,
    idempotency_key: str,
) -> dict | None:
    """SEC-26-3 / SEC-42-2: handle a hit on the idempotency-key store.

    Returns ``None`` when ``existing`` is ``None`` so the caller proceeds
    with the domain write. On a hash match, sets ``X-Idempotent-Replay``
    plus the stored status code on ``response`` and returns the decoded
    body. On a hash mismatch, logs and raises ``409`` — the outer
    ``except HTTPException`` block in the route owns the rollback (do
    NOT add a rollback here).

    The caller is responsible for ``db.commit()`` in the under-lock
    branch to release the advisory lock; this helper does not commit.
    """
    if existing is None:
        return None
    if bytes(existing["body_sha256"]) != body_sha256:
        logger.warning(
            "event=idempotency_key_mismatch request_id=%s api_key_id=%s key=%s",
            request_id,
            api_key_id,
            idempotency_key,
        )
        raise HTTPException(
            status_code=409,
            detail={
                "code": "idempotency_key_mismatch",
                "message": (
                    "request body differs from prior submission " "under this Idempotency-Key"
                ),
            },
        )
    response.headers["X-Idempotent-Replay"] = "true"
    response.status_code = int(existing["response_status"])
    return _coerce_stored_body(existing["response_body"])


@router.post("/photos/{photo_id}/outcomes")
async def post_photo_suggestion_outcomes(
    photo_id: int,
    body: SuggestionOutcomesRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    """Fire-and-forget: record the full user-decision list for a photo's
    VLM suggestions.

    Decoupled from /confirm so telemetry failures cannot break the
    catalogue-write path. Idempotent per (photo_id, vision_model):
    the server DELETEs prior outcomes for that pair and INSERTs the new
    batch atomically. Other vision_model rows on the same photo are
    preserved.

    ``X-Client-Retry-Count`` (ApiDev2_005, Swift2b-gamma): iOS offline-queue
    telemetry. Persisted to every row in the batch. Missing or malformed
    → 0 (never 400 — this is telemetry, not validation).

    ``Idempotency-Key`` (ApiDev_idempotency_outcomes, SEC-26-3): optional
    RFC 4122 UUID. When present, the server binds the key to
    ``SHA-256(raw request body)`` and returns the stored response on a
    matching replay (with ``X-Idempotent-Replay: true``). Replays with the
    same key but a different body return ``409 idempotency_key_mismatch``
    — never silently overwrite. TTL 24h, cleanup lazy-on-write. Absent
    header → endpoint behaves exactly as pre-feature (no storage, no
    lookup). See ``repository.store_idempotent_response`` for the
    race-serialization contract.
    """
    if not repository.photo_exists(db, photo_id):
        raise HTTPException(status_code=404, detail="photo not found")

    client_retry_count = _parse_client_retry_count(request.headers.get("x-client-retry-count"))

    raw_idempotency_key = request.headers.get("idempotency-key")
    idempotency_key: str | None = None
    body_sha256: bytes | None = None
    api_key_id: int | None = None

    if raw_idempotency_key is not None:
        idempotency_key = _validate_idempotency_key(raw_idempotency_key)
        api_key_id = require_api_key_id(request)
        # Hash the raw bytes FastAPI already consumed. request.body() is
        # cached internally so this second call does not hang the stream.
        raw_bytes = await request.body()
        body_sha256 = repository.hash_canonical_body(raw_bytes)

    decisions = [d.model_dump() for d in body.decisions]
    # ApiDev2_014 (SEC-42-2 / QA-42-O-1): every path below that raises
    # HTTPException — including both the pre-lock and under-lock 409
    # mismatch branches — relies on the outer ``except HTTPException``
    # handler for ``db.rollback()``. Do NOT reintroduce inline rollbacks
    # or hoist a raise out of this try: block; the symmetric rollback
    # discipline makes the three exit points (pre-lock 409, under-lock
    # 409, generic 500) share one cleanup codepath.
    try:
        if idempotency_key is not None:
            assert api_key_id is not None and body_sha256 is not None
            request_id = db.info.get("request_id") if db.info else None
            # Pre-lock fast path — steady-state crash-reclaim replays avoid
            # every single advisory lock acquisition.
            existing = repository.fetch_idempotent_record(db, api_key_id, idempotency_key)
            replay = _maybe_idempotent_replay(
                existing,
                body_sha256=body_sha256,
                response=response,
                request_id=request_id,
                api_key_id=api_key_id,
                idempotency_key=idempotency_key,
            )
            if replay is not None:
                return replay

            # Serialize concurrent first-sighting attempts: only one txn
            # per api_key can be mid-insert on the same key.
            repository.acquire_idempotency_lock(db, api_key_id)
            # Under the lock, re-check: a racing peer may have committed
            # between the pre-check and the lock wait.
            existing = repository.fetch_idempotent_record(db, api_key_id, idempotency_key)
            replay = _maybe_idempotent_replay(
                existing,
                body_sha256=body_sha256,
                response=response,
                request_id=request_id,
                api_key_id=api_key_id,
                idempotency_key=idempotency_key,
            )
            if replay is not None:
                # Commit releases the advisory lock; no domain write ran.
                db.commit()
                return replay

        metadata_upc = extract_upc_from_device_metadata(
            repository.fetch_photo_device_metadata(db, photo_id)
        )
        if metadata_upc is not None:
            for d in body.decisions:
                if d.item_id is not None and d.decision in ("accepted", "edited"):
                    repository.backfill_item_upc_if_missing(db, d.item_id, metadata_upc)

        repository.replace_photo_suggestion_outcomes(
            db,
            photo_id=photo_id,
            vision_model=body.vision_model,
            prompt_version=body.prompt_version,
            decisions=decisions,
            client_retry_count=client_retry_count,
        )
        response_body = {
            "version": "1",
            "photo_id": photo_id,
            "outcomes_recorded": len(decisions),
        }
        if idempotency_key is not None:
            assert api_key_id is not None and body_sha256 is not None
            repository.store_idempotent_response(
                db,
                api_key_id=api_key_id,
                key=idempotency_key,
                body_sha256=body_sha256,
                response_status=200,
                response_body=response_body,
            )
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        logger.exception(
            "event=outcomes_failed request_id=%s photo_id=%s",
            db.info.get("request_id") if db.info else None,
            photo_id,
        )
        raise HTTPException(status_code=500, detail="internal error") from None

    logger.info(
        "event=photo_outcomes request_id=%s photo_id=%s model=%s count=%s "
        "client_retry_count=%s idempotency_key=%s",
        db.info.get("request_id") if db.info else None,
        photo_id,
        body.vision_model,
        len(decisions),
        client_retry_count,
        idempotency_key,
    )
    return response_body
