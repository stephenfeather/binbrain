from typing import Optional

from app.db import repository
from app.deps import (
    EMBED_MODEL_NAME,
    canonical_item_text,
    embed_text,
    fingerprint_for,
    get_db,
    logger,
    vec_to_pgvector,
)
from app.services.upc_lookup import validate_upc
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

router = APIRouter()


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
            bin_id = bin_id.strip()
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
    bin_id = (payload.bin_id or "").strip()
    item_id = payload.item_id
    confidence = payload.confidence
    quantity = payload.quantity
    if not bin_id:
        raise HTTPException(status_code=400, detail="bin_id is required")

    try:
        repository.ensure_bin_active_or_create(db, bin_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="bin not found") from None
    repository.insert_bin_item(db, bin_id, item_id, confidence, quantity)
    db.commit()

    logger.info(
        "event=item_associate request_id=%s bin_id=%s item_id=%s",
        db.info.get("request_id"),
        bin_id,
        item_id,
    )
    return {"ok": True, "bin_id": bin_id, "item_id": item_id}


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

    rows = repository.search_items(db, qvec_str, limit, offset, min_score)

    logger.info(
        "event=search request_id=%s limit=%s offset=%s min_score=%s results=%s",
        db.info.get("request_id"),
        limit,
        offset,
        min_score,
        len(rows),
    )
    return {
        "version": "1",
        "q": q,
        "limit": limit,
        "offset": offset,
        "min_score": min_score,
        "results": rows,
    }
