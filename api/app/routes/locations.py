from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import repository
from app.deps import get_db, logger
from app.middleware import require_admin

router = APIRouter(tags=["locations"])


class CreateLocationRequest(BaseModel):
    name: str
    description: Optional[str] = None


@router.get("/locations")
def list_locations(db: Session = Depends(get_db)):
    """List all active locations."""
    locations = repository.list_locations(db)
    return {"version": "1", "locations": locations}


@router.post("/locations", dependencies=[Depends(require_admin)])
def create_location(
    body: CreateLocationRequest,
    db: Session = Depends(get_db),
):
    """Create a new location. Requires admin."""
    name_stripped = (body.name or "").strip()
    if not name_stripped:
        raise HTTPException(status_code=400, detail="name is required")

    loc = repository.create_location(db, name_stripped, body.description)
    if loc is None:
        raise HTTPException(status_code=409, detail="location with this name already exists")

    db.commit()
    logger.info(
        "event=location_create request_id=%s location_id=%s name=%s",
        db.info.get("request_id"),
        loc["location_id"],
        loc["name"],
    )
    return {"version": "1", "location": loc}


@router.delete("/locations/{location_id}", dependencies=[Depends(require_admin)])
def delete_location(
    location_id: int,
    db: Session = Depends(get_db),
):
    """Soft-delete a location and clear references from bins. Requires admin."""
    deleted = repository.soft_delete_location(db, location_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="location not found")

    db.commit()
    logger.info(
        "event=location_delete request_id=%s location_id=%s",
        db.info.get("request_id"),
        location_id,
    )
    return {"version": "1", "deleted": True}
