from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.deps import get_db, EMBED_MODEL_NAME

router = APIRouter()


@router.get("/health")
def health(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
        return {"version": "1", "ok": True, "db_ok": True, "embed_model": EMBED_MODEL_NAME, "expected_dims": 384}
    except Exception:
        raise HTTPException(status_code=503, detail="database unavailable")
