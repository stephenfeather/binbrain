from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import repository
from app.deps import (
    get_db, embed_text, canonical_item_text, vec_to_pgvector,
    EMBED_MODEL_NAME, logger,
)
from app.services.upc_lookup import validate_upc, lookup_upc

router = APIRouter()


@router.get("/upc/{upc}")
def upc_lookup(
    upc: str,
    db: Session = Depends(get_db),
):
    upc = (upc or "").strip()
    if not validate_upc(upc):
        raise HTTPException(status_code=400, detail="invalid UPC format (expected 12 or 13 digits)")

    # 1. Local DB first — free and instant
    existing = repository.find_item_by_upc(db, upc)
    if existing:
        logger.info(
            "event=upc_lookup request_id=%s upc=%s source=local item_id=%s",
            db.info.get("request_id"), upc, existing["item_id"],
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
    result = lookup_upc(upc)

    # 3. Cache the result locally if we got a name
    item_id = None
    if result.name:
        try:
            item_id = repository.insert_item(db, result.name, result.category, None, upc=upc)
            vec = embed_text(canonical_item_text(result.name, result.category, None))
            dims = len(vec)
            if dims != 384:
                raise ValueError(f"unexpected embedding dims {dims}")
            repository.upsert_item_embedding(db, item_id, EMBED_MODEL_NAME, dims, vec_to_pgvector(vec))
            db.commit()
        except Exception as e:
            db.rollback()
            logger.warning(
                "event=upc_lookup_cache_failed request_id=%s upc=%s error=%s",
                db.info.get("request_id"), upc, str(e)[:200],
            )
            item_id = None

    logger.info(
        "event=upc_lookup request_id=%s upc=%s source=%s item_id=%s",
        db.info.get("request_id"), upc, result.source, item_id,
    )
    return {
        "version": "1",
        "item_id": item_id,
        "name": result.name,
        "category": result.category,
        "upc": upc,
        "source": result.source,
    }
