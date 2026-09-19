"""
Server-side opaque session management.

Sessions are stored in PostgreSQL with a hashed token.
The raw token lives in an HttpOnly cookie only.
No JWT — sessions are fully server-controlled and revocable.

Session lifecycle:
  create_session()    → generates token, stores hash in DB, returns raw token
  validate_session()  → hash lookup, expiry check, returns User
  revoke_session()    → deletes session row from DB
  revoke_all()        → deletes all sessions for a user (logout all devices)
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.crypto import generate_session_token, hash_session_token
from app.db.models import Session, User

logger = logging.getLogger(__name__)

SESSION_TTL_DAYS = 30
COOKIE_NAME = "mp_session"


async def create_session(
    db: AsyncSession,
    user: User,
    ip: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> tuple[Session, str]:
    """
    Create a new session for the given user.

    Returns:
        (session_row, raw_token) — caller sets raw_token as HttpOnly cookie.
    """
    raw_token = generate_session_token()
    token_hash = hash_session_token(raw_token)
    expires_at = datetime.now(timezone.utc) + timedelta(days=SESSION_TTL_DAYS)

    session = Session(
        user_id=user.id,
        token_hash=token_hash,
        ip=ip,
        user_agent=user_agent,
        expires_at=expires_at,
    )
    db.add(session)
    await db.flush()  # get session.id without committing

    logger.info(f"Session created for user={user.id} ip={ip}")
    return session, raw_token


async def validate_session(
    db: AsyncSession,
    raw_token: str,
) -> Optional[User]:
    """
    Validate a raw session token from the cookie.

    Returns the User if the session is valid and not expired.
    Returns None if session is invalid, expired, or not found.
    """
    token_hash = hash_session_token(raw_token)
    now = datetime.now(timezone.utc)

    result = await db.execute(
        select(Session)
        .where(Session.token_hash == token_hash)
        .where(Session.expires_at > now)
    )
    session = result.scalar_one_or_none()

    if session is None:
        return None

    # Load user
    result = await db.execute(
        select(User).where(User.id == session.user_id)
    )
    user = result.scalar_one_or_none()

    if user is None or user.status != "ACTIVE":
        return None

    return user


async def revoke_session(db: AsyncSession, raw_token: str) -> None:
    """Revoke a single session by raw token."""
    token_hash = hash_session_token(raw_token)
    await db.execute(delete(Session).where(Session.token_hash == token_hash))
    logger.info(f"Session revoked (hash={token_hash[:8]}...)")


async def revoke_all_sessions(db: AsyncSession, user_id: str) -> int:
    """Revoke all sessions for a user (logout all devices)."""
    result = await db.execute(delete(Session).where(Session.user_id == user_id))
    count = result.rowcount
    logger.info(f"Revoked {count} sessions for user={user_id}")
    return count


async def cleanup_expired_sessions(db: AsyncSession) -> int:
    """Remove expired sessions. Should be called periodically."""
    now = datetime.now(timezone.utc)
    result = await db.execute(delete(Session).where(Session.expires_at < now))
    count = result.rowcount
    if count > 0:
        logger.info(f"Cleaned up {count} expired sessions")
    return count
