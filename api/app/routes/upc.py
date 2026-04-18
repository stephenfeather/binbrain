from app.db import repository
from app.deps import (
    EMBED_MODEL_NAME,
    SessionLocal,
    canonical_item_text,
    embed_text,
    get_db,
    logger,
    vec_to_pgvector,
)
from app.services.rate_limiter import require_upc_rate_limit
from app.services.upc_lookup import UPCResult, lookup_upc, validate_upc
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

router = APIRouter()


def _record_upc_lookup_provenance(
    *,
    upc: str,
    item_id: int | None,
    source: str,
    raw_response: dict | None,
    elapsed_ms: int | None,
    request_id: str | None,
) -> None:
    """Best-effort append to ``item_upc_lookups`` (Gap #7).

    Runs in its own short-lived session so a rollback on the caller's
    request session (e.g. item-cache insert failure) can't take the
    provenance row with it. Failure to write provenance MUST NOT affect
    the /upc/{upc} response — the caller already has its result and the
    user is waiting. A failing write is logged at WARN and swallowed.
    """
    tel_db = SessionLocal()
    try:
        repository.insert_upc_lookup(
            tel_db,
            upc=upc,
            item_id=item_id,
            source=source,
            raw_response=raw_response,
            elapsed_ms=elapsed_ms,
        )
        tel_db.commit()
    except Exception as tel_exc:
        logger.warning(
            "event=upc_lookup_provenance_write_failed request_id=%s "
            "upc=%s source=%s item_id=%s err=%s",
            request_id,
            upc,
            source,
            item_id,
            tel_exc,
        )
    finally:
        tel_db.close()


@router.get("/upc/{upc}", dependencies=[Depends(require_upc_rate_limit)])
def upc_lookup(
    upc: str,
    db: Session = Depends(get_db),
):
    upc = (upc or "").strip()
    if not validate_upc(upc):
        raise HTTPException(status_code=400, detail="invalid UPC format (expected 12 or 13 digits)")

    request_id = db.info.get("request_id")

    # 1. Local DB first — free and instant
    existing = repository.find_item_by_upc(db, upc)
    if existing:
        logger.info(
            "event=upc_lookup request_id=%s upc=%s source=local item_id=%s",
            request_id,
            upc,
            existing["item_id"],
        )
        # ApiDev2_002 (Gap #7): local-hit provenance row — no raw_response,
        # no elapsed_ms (no external call was made).
        _record_upc_lookup_provenance(
            upc=upc,
            item_id=existing["item_id"],
            source="local",
            raw_response=None,
            elapsed_ms=None,
            request_id=request_id,
        )
        return {
            "version": "1",
            "item_id": existing["item_id"],
            "name": existing["name"],
            "category": existing["category"],
            "upc": upc,
            "source": "local",
        }

    # 2. External lookup — degrades gracefully to source="unknown"
    result: UPCResult = lookup_upc(upc)

    # 3. Cache the result locally if we got a name
    item_id = None
    if result.name:
        try:
            item_id = repository.insert_item(db, result.name, result.category, None, upc=upc)
            vec = embed_text(canonical_item_text(result.name, result.category, None))
            dims = len(vec)
            if dims != 384:
                raise ValueError(f"unexpected embedding dims {dims}")
            repository.upsert_item_embedding(
                db, item_id, EMBED_MODEL_NAME, dims, vec_to_pgvector(vec)
            )
            db.commit()
        except Exception as e:
            db.rollback()
            logger.warning(
                "event=upc_lookup_cache_failed request_id=%s upc=%s error=%s",
                request_id,
                upc,
                str(e)[:200],
            )
            item_id = None

    logger.info(
        "event=upc_lookup request_id=%s upc=%s source=%s item_id=%s",
        request_id,
        upc,
        result.source,
        item_id,
    )
    # ApiDev2_002 (Gap #7): external-path provenance row. We write this
    # AFTER the item commit (success or rollback) so a cache-insert failure
    # doesn't take the provenance with it. Every return path writes exactly
    # one row — upcitemdb/go-upc success with raw body + latency, or the
    # ``unknown`` fallback with NULL raw_response and whatever elapsed_ms
    # we got from the last upstream attempt (None if all services stubbed).
    _record_upc_lookup_provenance(
        upc=upc,
        item_id=item_id,
        source=result.source,
        raw_response=result.raw_response,
        elapsed_ms=result.elapsed_ms,
        request_id=request_id,
    )
    return {
        "version": "1",
        "item_id": item_id,
        "name": result.name,
        "category": result.category,
        "upc": upc,
        "source": result.source,
    }
