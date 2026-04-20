import math
import os
from typing import Optional

from app.db import repository
from app.deps import (
    EMBED_MODEL_NAME,
    SessionLocal,
    canonical_item_text,
    embed_text,
    fingerprint_for,
    get_db,
    logger,
    vec_to_pgvector,
)
from app.routes.bins import guard_user_bin_name
from app.services.upc_lookup import validate_upc
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

router = APIRouter()

# Dev2_019 PR22-01: SEARCH_DEFAULT_MIN_SCORE is parsed once per invocation.
# If ops deploys a malformed value ("abc", "", "0,35"), or an out-of-range
# value (e.g. 1.5 or a non-finite like "inf"/"nan"), we log a warning and
# fall back to this constant rather than 500'ing every /search.
_SEARCH_DEFAULT_MIN_SCORE_FALLBACK: float = 0.35

# Dev2_019 PR22-02: upper bound on telemetry-persisted ``q`` — prevents
# unbounded rows from buggy or malicious clients pushing arbitrary-length
# strings into ``search_queries.q``. Calibration queries need enough length
# to recover the intent; 1024 chars is orders of magnitude above any real
# user query and still caps worst-case storage.
_SEARCH_Q_TELEMETRY_MAX_LEN: int = 1024


def _resolve_default_min_score() -> float:
    """Parse ``SEARCH_DEFAULT_MIN_SCORE`` defensively.

    Falls back to :data:`_SEARCH_DEFAULT_MIN_SCORE_FALLBACK` on any of:
    ValueError (non-numeric / European decimal), non-finite (nan/inf), or
    out-of-range (outside ``[0.0, 1.0]``). Logs a warning at the point of
    rejection so ops can spot a bad deploy without grepping for 500s.
    """
    raw = os.environ.get("SEARCH_DEFAULT_MIN_SCORE", str(_SEARCH_DEFAULT_MIN_SCORE_FALLBACK))
    try:
        parsed = float(raw)
    except (ValueError, TypeError):
        logger.warning(
            "event=search_default_min_score_invalid value=%r fallback=%s",
            raw,
            _SEARCH_DEFAULT_MIN_SCORE_FALLBACK,
        )
        return _SEARCH_DEFAULT_MIN_SCORE_FALLBACK
    if not math.isfinite(parsed):
        logger.warning(
            "event=search_default_min_score_non_finite value=%r fallback=%s",
            raw,
            _SEARCH_DEFAULT_MIN_SCORE_FALLBACK,
        )
        return _SEARCH_DEFAULT_MIN_SCORE_FALLBACK
    if not 0.0 <= parsed <= 1.0:
        logger.warning(
            "event=search_default_min_score_out_of_range value=%s fallback=%s",
            parsed,
            _SEARCH_DEFAULT_MIN_SCORE_FALLBACK,
        )
        return _SEARCH_DEFAULT_MIN_SCORE_FALLBACK
    return parsed


class CreateItemBody(BaseModel):
    name: str
    category: Optional[str] = None
    notes: Optional[str] = None
    upc: Optional[str] = None
    bin_id: Optional[str] = None
    confidence: Optional[float] = None
    quantity: Optional[float] = None


class AssociateItemBody(BaseModel):
    bin_id: str
    item_id: int
    confidence: Optional[float] = None
    quantity: Optional[float] = None


@router.post("/items")
def create_item(
    payload: CreateItemBody,
    db: Session = Depends(get_db),
):
    name = payload.name
    category = payload.category
    notes = payload.notes
    upc = payload.upc
    bin_id = payload.bin_id
    confidence = payload.confidence
    quantity = payload.quantity
    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")

    upc = (upc or "").strip() or None
    if upc and not validate_upc(upc):
        raise HTTPException(status_code=400, detail="invalid UPC format (expected 12 or 13 digits)")

    try:
        # 1) Insert item (no commit yet)
        item_id = repository.insert_item(db, name, category, notes, upc=upc)

        # 2) Create embedding
        vec = embed_text(canonical_item_text(name, category, notes))
        dims = len(vec)
        if dims != 384:
            raise HTTPException(
                status_code=500, detail=f"unexpected embedding dims {dims}, expected 384"
            )
        vec_str = vec_to_pgvector(vec)

        # 3) Upsert embedding row
        repository.upsert_item_embedding(db, item_id, EMBED_MODEL_NAME, dims, vec_str)

        # 4) Optional association to a bin
        if bin_id:
            # SEC-43-1: format-check + reserved-name reject in one call so a
            # user cannot side-door a "Binless"/"uNaSsIgNeD" bin through
            # /items when /ingest and /add_to_bin already block it.
            bin_id = guard_user_bin_name(bin_id)
            try:
                repository.ensure_bin_active_or_create(db, bin_id)
            except ValueError:
                raise HTTPException(status_code=404, detail="bin not found") from None
            repository.insert_bin_item(db, bin_id, item_id, confidence, quantity)

        db.commit()
        logger.info(
            "event=item_create request_id=%s item_id=%s bin_id=%s",
            db.info.get("request_id"),
            item_id,
            bin_id,
        )
        return {
            "version": "1",
            "item_id": item_id,
            "fingerprint": fingerprint_for(name, category),
            "name": name,
            "category": category,
            "notes": notes,
            "upc": upc,
            "bin_id": bin_id,
        }

    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise HTTPException(status_code=500, detail="internal error") from None


@router.post("/associate")
def associate_item(
    payload: AssociateItemBody,
    db: Session = Depends(get_db),
):
    raw_bin_id = payload.bin_id or ""
    item_id = payload.item_id
    confidence = payload.confidence
    quantity = payload.quantity
    if not raw_bin_id.strip():
        raise HTTPException(status_code=400, detail="bin_id is required")
    # SEC-43-1: previously this route called neither _check_bin_id nor the
    # reserved-name reject, so a user could both bypass the path-safety
    # regex (SEC) and create "Binless"/"uNaSsIgNeD". Composed guard fixes
    # both at once.
    bin_id = guard_user_bin_name(raw_bin_id)

    try:
        repository.ensure_bin_active_or_create(db, bin_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="bin not found") from None
    # ApiDev2_013: ``insert_bin_item`` already uses ``ON CONFLICT DO
    # NOTHING RETURNING id`` and returns True on first insert, False on a
    # unique-constraint hit. Surface that flag so iOS can render a
    # "already in bin" signal instead of a silent drop. No schema or
    # behavior change — the on-conflict branch still updates nothing.
    inserted = repository.insert_bin_item(db, bin_id, item_id, confidence, quantity)
    db.commit()

    logger.info(
        "event=item_associate request_id=%s bin_id=%s item_id=%s inserted=%s",
        db.info.get("request_id"),
        bin_id,
        item_id,
        inserted,
    )
    return {"ok": True, "bin_id": bin_id, "item_id": item_id, "inserted": inserted}


@router.get("/search")
def search(
    q: str = Query(..., min_length=1),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    min_score: Optional[float] = Query(None, ge=0.0, le=1.0),
    db: Session = Depends(get_db),
):
    try:
        qvec = embed_text(q)
    except Exception:
        raise HTTPException(status_code=400, detail="search unavailable") from None

    if len(qvec) != 384:
        raise HTTPException(
            status_code=500, detail=f"unexpected query embedding dims {len(qvec)}, expected 384"
        )

    qvec_str = vec_to_pgvector(qvec)

    # Dev2_019: apply the server-side default floor when the client omits
    # ``min_score``. Env is resolved per-invocation (so ops can flip the
    # default without restart, and tests can monkeypatch) and defensively
    # parsed — a malformed or out-of-range deploy falls back to the
    # documented constant rather than 500'ing the endpoint (PR22-01).
    # Explicit client values (including 0.0) still win.
    effective_min_score = _resolve_default_min_score() if min_score is None else min_score

    rows = repository.search_items(db, qvec_str, limit, offset, effective_min_score)

    request_id = db.info.get("request_id")
    logger.info(
        "event=search request_id=%s limit=%s offset=%s min_score=%s results=%s",
        request_id,
        limit,
        offset,
        effective_min_score,
        len(rows),
    )

    # Dev2_019 PR22-02: ``q`` is persisted verbatim (up to the telemetry
    # length cap). Retention / purge policy for ``search_queries`` is a
    # deferred follow-up — the Gap #11 closeout task is populate now,
    # retain-forever, and revisit once we have a few months of volume to
    # decide on a retention window. Until then this table grows
    # append-only. Mirrored in the migration header.
    q_for_telemetry = q[:_SEARCH_Q_TELEMETRY_MAX_LEN]

    # Dev2_019: telemetry write is best-effort. A failure here must never
    # regress the /search response contract. Uses a fresh session so a
    # rolled-back main session (future exception paths) would still persist
    # the row.
    try:
        db_tel = SessionLocal()
        try:
            repository.insert_search_query(
                db_tel,
                request_id=request_id,
                q=q_for_telemetry,
                qvec_dims=len(qvec),
                min_score_effective=effective_min_score,
                result_count=len(rows),
            )
            db_tel.commit()
        finally:
            db_tel.close()
    except Exception as tel_exc:
        logger.warning(
            "event=search_telemetry_write_failed request_id=%s err=%s",
            request_id,
            tel_exc,
        )

    return {
        "version": "1",
        "q": q,
        "limit": limit,
        "offset": offset,
        "min_score": effective_min_score,
        "results": rows,
    }
