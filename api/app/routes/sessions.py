"""/sessions routes — explicit session lifecycle (ApiDev_008, Q-session-id).

All routes require an authenticated api_key (``api_key_auth_middleware`` sets
``request.state.api_key_id``). Row ownership is always scoped by that id;
cross-owner lookups return 404 so existence is not leaked.

Plan doc: thoughts/shared/plans/2026-04-19-session-id-explicit-boundary.md
"""

from __future__ import annotations

from app.db import repository
from app.deps import get_db, logger
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

router = APIRouter(tags=["sessions"])


MAX_LABEL_LEN = 120
MAX_OPEN_SESSIONS_PER_KEY = 20


class CreateSessionRequest(BaseModel):
    # Optional human-readable label. None / omitted is fine.
    label: str | None = Field(default=None)


def _validate_label(label: str | None) -> str | None:
    """Return the normalized label or raise HTTPException(400).

    Rules:
    - None or empty string -> None (no label).
    - ≤ 120 chars after stripping leading/trailing whitespace.
    - No control characters (<0x20, 0x7F). Tabs and newlines blocked so the
      label stays safe for single-line Settings UI + log lines.
    """
    if label is None:
        return None
    label = label.strip()
    if label == "":
        return None
    if len(label) > MAX_LABEL_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"label exceeds {MAX_LABEL_LEN}-char limit",
        )
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in label):
        raise HTTPException(
            status_code=400,
            detail="label must not contain control characters",
        )
    return label


def _require_api_key_id(request: Request) -> int:
    api_key_id = getattr(request.state, "api_key_id", None)
    if api_key_id is None:
        # Defense in depth — the auth middleware should have already rejected
        # unauthenticated requests before a route handler runs.
        raise HTTPException(status_code=401, detail="Missing API key")
    return int(api_key_id)


@router.post("/sessions", status_code=201)
def create_session(
    request: Request,
    body: CreateSessionRequest,
    db: Session = Depends(get_db),
):
    api_key_id = _require_api_key_id(request)
    label = _validate_label(body.label)

    open_count = repository.count_open_sessions(db, api_key_id)
    if open_count >= MAX_OPEN_SESSIONS_PER_KEY:
        raise HTTPException(
            status_code=429,
            detail=(
                f"too many open sessions "
                f"(limit {MAX_OPEN_SESSIONS_PER_KEY}); close existing sessions first"
            ),
        )

    session = repository.create_session(db, api_key_id, label)
    db.commit()
    logger.info(
        "event=session_create request_id=%s api_key_id=%s session_id=%s",
        db.info.get("request_id"),
        api_key_id,
        session["session_id"],
    )
    return {"version": "1", "session": session}


@router.delete("/sessions/{session_id}")
def delete_session(
    request: Request,
    session_id: str,
    db: Session = Depends(get_db),
):
    api_key_id = _require_api_key_id(request)

    try:
        result = repository.end_session(db, session_id, api_key_id)
    except Exception:
        # Malformed UUID — collapse to 404 (don't leak "not a UUID" vs "not
        # yours" vs "not found").
        db.rollback()
        raise HTTPException(status_code=404, detail="session not found") from None

    if result is None:
        raise HTTPException(status_code=404, detail="session not found")
    if result == "already_closed":
        raise HTTPException(status_code=410, detail="session already closed")

    db.commit()
    logger.info(
        "event=session_end request_id=%s api_key_id=%s session_id=%s",
        db.info.get("request_id"),
        api_key_id,
        session_id,
    )
    return {"version": "1", "session": result}


@router.get("/sessions/{session_id}")
def get_session(
    request: Request,
    session_id: str,
    db: Session = Depends(get_db),
):
    api_key_id = _require_api_key_id(request)

    try:
        session = repository.find_session(db, session_id, api_key_id)
    except Exception:
        db.rollback()
        raise HTTPException(status_code=404, detail="session not found") from None

    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return {"version": "1", "session": session}


@router.get("/sessions")
def list_sessions_route(
    request: Request,
    state: str = Query("all", pattern="^(open|closed|all)$"),
    limit: int = Query(20, ge=1),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    if limit > 100:
        # Could let Query(le=100) do this, but FastAPI returns 422 — we want
        # 400 so it maps cleanly to our ErrorResponse envelope.
        raise HTTPException(status_code=400, detail="limit must be <= 100")

    api_key_id = _require_api_key_id(request)
    sessions = repository.list_sessions(db, api_key_id, state, limit, offset)
    return {"version": "1", "sessions": sessions}
