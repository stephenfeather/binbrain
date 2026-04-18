"""Shared middleware helpers and FastAPI dependencies.

Imported by route modules; must not import from app.routes.* to avoid circulars.
"""

from fastapi import HTTPException, Request


def require_admin(request: Request) -> None:
    """FastAPI dependency that enforces admin role on a route.

    The auth middleware (main.py) must have already set
    ``request.state.api_key_role`` before this dependency runs.

    Raises:
        HTTPException(403): if the authenticated key does not have the 'admin' role.
    """
    role = getattr(request.state, "api_key_role", None)
    if role != "admin":
        raise HTTPException(
            status_code=403,
            detail="Admin role required",
        )
