import hashlib

from app.db import repository
from app.deps import EMBED_MODEL_NAME, get_db
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

router = APIRouter()


@router.get("/health")
def health(request: Request, db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        raise HTTPException(status_code=503, detail="database unavailable") from None

    body: dict = {
        "version": "1",
        "ok": True,
        "db_ok": True,
        "embed_model": EMBED_MODEL_NAME,
        "expected_dims": 384,
    }

    # Finding #3: surface the last YOLOE class reload status so silent
    # background-thread failures (e.g. mkdir permission errors) are visible
    # to ops and, eventually, the iOS client.
    from app.services import detection

    body["model_reload"] = detection.get_reload_status()

    # Optional auth probe: if the caller supplied X-API-Key, report whether
    # it's valid so clients can verify their credentials without hitting a
    # protected endpoint. /health itself stays unauthenticated.
    raw_key = request.headers.get("x-api-key")
    if raw_key:
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        key_row = repository.validate_api_key(db, key_hash)
        if key_row:
            body["auth_ok"] = True
            body["role"] = key_row.get("role", "user")
        else:
            body["auth_ok"] = False

    return body
