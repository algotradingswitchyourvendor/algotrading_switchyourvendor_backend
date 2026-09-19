"""
FastAPI dependencies for authentication and authorization.

These are used as Depends() arguments in route handlers.

Usage:
    @router.get("/me")
    async def get_me(user: User = Depends(require_auth)):
        ...

    @router.get("/admin/stats")
    async def admin_stats(user: User = Depends(require_admin)):
        ...
"""

import logging
from typing import Optional

from fastapi import Cookie, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.session import COOKIE_NAME, validate_session
from app.db.database import get_db
from app.db.models import User

logger = logging.getLogger(__name__)


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
    mp_session: Optional[str] = Cookie(default=None, alias=COOKIE_NAME),
) -> Optional[User]:
    """
    Extract and validate the current user from the session cookie.

    Returns None (not an exception) if no valid session exists.
    Use require_auth() or require_admin() for protected routes.
    """
    if not mp_session:
        return None

    user = await validate_session(db, mp_session)
    return user


async def require_auth(
    user: Optional[User] = Depends(get_current_user),
) -> User:
    """
    Require an authenticated user. Raises 401 if not authenticated.
    Raises 403 if user is suspended.
    """
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "UNAUTHENTICATED", "message": "Authentication required"},
        )
    if user.status == "SUSPENDED":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "ACCOUNT_SUSPENDED", "message": "Your account has been suspended"},
        )
    if user.status == "DELETED":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "ACCOUNT_DELETED", "message": "This account no longer exists"},
        )
    return user


async def require_admin(
    user: User = Depends(require_auth),
) -> User:
    """
    Require an authenticated admin user.
    Raises 403 if the user exists but is not an admin.

    NOTE: Frontend route guards are UX only.
    This dependency enforces authorization server-side on every request.
    """
    if user.role != "ADMIN":
        logger.warning(f"Non-admin user={user.id} attempted admin access")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "INSUFFICIENT_PERMISSIONS",
                "message": "Admin access required",
            },
        )
    return user
