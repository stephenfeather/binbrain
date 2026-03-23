from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

from sqlalchemy.orm import Session

logger = logging.getLogger("binbrain")

_classes: list[str] = []
_categories: dict[str, str | None] = {}
_lock = threading.Lock()
_on_reload: Callable[[list[str]], None] | None = None


def load_from_db(db: Session) -> None:
    """Load all active classes from the database. Called on startup."""
    from app.db import repository

    rows = repository.fetch_active_classes(db)
    with _lock:
        _classes.clear()
        _categories.clear()
        for row in rows:
            name = row["class_name"]
            _classes.append(name)
            _categories[name] = row.get("category")
    logger.info("event=class_registry_loaded count=%d", len(_classes))


def get_classes() -> list[str]:
    """Thread-safe read of the current class list."""
    with _lock:
        return list(_classes)


def get_category(class_name: str) -> str | None:
    """Look up category for a known class."""
    with _lock:
        return _categories.get(class_name)


def add_class(
    db: Session,
    name: str,
    category: Optional[str],
    source: str,
    confirmed_by: Optional[str] = None,
) -> bool:
    """Persist a class and update the in-memory cache.

    Returns True if the class was newly added, False if it already existed.
    Triggers the reload callback in a background thread on success.
    """
    from app.db import repository

    normalized = name.strip().lower()

    with _lock:
        if normalized in _categories:
            return False

    row = repository.insert_confirmed_class(
        db, name, category, source, confirmed_by
    )
    if row is None:
        return False

    db.commit()

    with _lock:
        if normalized not in _categories:
            _classes.append(normalized)
            _categories[normalized] = category

    _trigger_reload()
    return True


def remove_class(db: Session, name: str) -> bool:
    """Soft-delete a class and remove from cache.

    Returns True if removed, False if not found.
    Triggers the reload callback in a background thread on success.
    """
    from app.db import repository

    normalized = name.strip().lower()
    removed = repository.soft_delete_class(db, name)
    if not removed:
        return False

    db.commit()

    with _lock:
        if normalized in _categories:
            _classes.remove(normalized)
            del _categories[normalized]

    _trigger_reload()
    return True


def set_reload_callback(fn: Callable[[list[str]], None]) -> None:
    """Register a callback invoked when the class list changes.

    Called by the detection module to register its reload_classes function.
    """
    global _on_reload
    _on_reload = fn


def class_count() -> int:
    """Return the number of active classes."""
    with _lock:
        return len(_classes)


def _trigger_reload() -> None:
    """Fire the reload callback in a background thread if registered."""
    if _on_reload is None:
        return
    classes = get_classes()
    t = threading.Thread(target=_on_reload, args=(classes,), daemon=True)
    t.start()
