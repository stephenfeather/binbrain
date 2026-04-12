from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.deps import get_db, logger
from app.middleware import require_admin
from app.services import class_registry

router = APIRouter(prefix="/classes", tags=["classes"])


class ConfirmRequest(BaseModel):
    version: str = "1"
    class_name: str
    category: str | None = None
    source: str = "vision_llm"


@router.get("")
def list_classes(request: Request = None):
    """List all active confirmed classes."""
    from app.db import repository
    db = next(get_db(request))
    rows = repository.fetch_active_classes(db)
    return {
        "version": "1",
        "classes": rows,
        "count": len(rows),
    }


@router.post("/confirm", dependencies=[Depends(require_admin)])
def confirm_class(body: ConfirmRequest, request: Request = None):
    """Confirm a class name, adding it to the YOLO-World vocabulary. Requires admin."""
    request_id = getattr(request.state, "request_id", None) if request else None
    if not body.class_name.strip():
        raise HTTPException(status_code=400, detail="class_name cannot be empty")

    db = next(get_db(request))
    added = class_registry.add_class(
        db,
        name=body.class_name,
        category=body.category,
        source=body.source,
    )
    logger.info(
        "event=class_confirm request_id=%s class=%s added=%s",
        request_id, body.class_name, added,
    )
    return {
        "version": "1",
        "class_name": body.class_name.strip().lower(),
        "added": added,
        "active_class_count": class_registry.class_count(),
        "reload_triggered": added,
    }


@router.delete("/{class_name}", dependencies=[Depends(require_admin)])
def delete_class(class_name: str, request: Request = None):
    """Soft-delete a class from the active list. Requires admin."""
    request_id = getattr(request.state, "request_id", None) if request else None
    db = next(get_db(request))
    removed = class_registry.remove_class(db, class_name)
    if not removed:
        raise HTTPException(status_code=404, detail=f"class '{class_name}' not found")
    logger.info(
        "event=class_delete request_id=%s class=%s",
        request_id, class_name,
    )
    return {
        "version": "1",
        "class_name": class_name,
        "removed": True,
        "active_class_count": class_registry.class_count(),
    }


@router.post("/reload", dependencies=[Depends(require_admin)])
def reload_classes(request: Request = None):
    """Force a YOLO-World set_classes() reload from the current class list. Requires admin."""
    request_id = getattr(request.state, "request_id", None) if request else None
    classes = class_registry.get_classes()
    from app.services.detection import reload_classes as _reload
    _reload(classes)
    logger.info(
        "event=class_reload_forced request_id=%s count=%d",
        request_id, len(classes),
    )
    return {
        "version": "1",
        "reloaded": True,
        "active_class_count": len(classes),
    }
