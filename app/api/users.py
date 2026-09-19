import logging
from typing import List, Optional
import zoneinfo

from fastapi import APIRouter, Depends, HTTPException, Request, Cookie
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_auth
from app.auth.session import revoke_session, COOKIE_NAME
from app.db.database import get_db
from app.db.models import User, UserPreferences, AuthAccount, Session, Payment
from app.schemas.response import success_response
from app.schemas.user import (
    ProfileUpdate,
    UserPreferencesResponse,
    UserPreferencesUpdate,
    SessionResponse,
    ConnectionResponse,
    BillingHistoryResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/users", tags=["users"])

# ── Profile ───────────────────────────────────────────────────────────────────

@router.patch("/profile")
async def update_profile(
    update: ProfileUpdate,
    user: User = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """Update user profile (only name is allowed)."""
    user.name = update.name
    await db.commit()
    return success_response(data={"message": "Profile updated successfully"})

# ── Preferences ───────────────────────────────────────────────────────────────

@router.get("/preferences", response_model=None)
async def get_preferences(
    user: User = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """Get user preferences. Initializes defaults if missing."""
    stmt = select(UserPreferences).where(UserPreferences.user_id == user.id)
    result = await db.execute(stmt)
    prefs = result.scalar_one_or_none()

    if not prefs:
        prefs = UserPreferences(user_id=user.id)
        db.add(prefs)
        await db.commit()
        await db.refresh(prefs)

    return success_response(data=UserPreferencesResponse(
        default_exchange=prefs.default_exchange,
        default_page_size=prefs.default_page_size,
        timezone=prefs.timezone,
    ).model_dump())

@router.patch("/preferences")
async def update_preferences(
    update: UserPreferencesUpdate,
    user: User = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """Update user preferences."""
    stmt = select(UserPreferences).where(UserPreferences.user_id == user.id)
    result = await db.execute(stmt)
    prefs = result.scalar_one_or_none()

    if not prefs:
        prefs = UserPreferences(user_id=user.id)
        db.add(prefs)

    if update.default_exchange is not None:
        if update.default_exchange not in ["NSE", "BSE", "NSE + BSE"]:
            raise HTTPException(422, detail="Invalid exchange")
        prefs.default_exchange = update.default_exchange

    if update.default_page_size is not None:
        if update.default_page_size not in [10, 20, 50, 100]:
            raise HTTPException(422, detail="Invalid page size")
        prefs.default_page_size = update.default_page_size

    if update.timezone is not None:
        if update.timezone not in zoneinfo.available_timezones():
            raise HTTPException(422, detail="Invalid timezone identifier")
        prefs.timezone = update.timezone

    await db.commit()
    return success_response(data={"message": "Preferences updated"})

# ── Connections ───────────────────────────────────────────────────────────────

@router.get("/connections")
async def get_connections(
    user: User = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """Get connected OAuth providers."""
    stmt = select(AuthAccount).where(AuthAccount.user_id == user.id)
    result = await db.execute(stmt)
    accounts = result.scalars().all()

    connections = [
        ConnectionResponse(
            provider=acc.provider,
            provider_account_id=acc.provider_account_id,
            created_at=acc.created_at,
        ).model_dump()
        for acc in accounts
    ]
    return success_response(data=connections)

# ── Sessions ──────────────────────────────────────────────────────────────────

@router.get("/sessions")
async def get_sessions(
    request: Request,
    user: User = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
    mp_session: str | None = Cookie(default=None, alias=COOKIE_NAME)
):
    """Get all active sessions for the user."""
    stmt = select(Session).where(Session.user_id == user.id)
    result = await db.execute(stmt)
    sessions = result.scalars().all()
    
    # We need to hash the cookie to check which session is current
    import hashlib
    current_token_hash = None
    if mp_session:
        current_token_hash = hashlib.sha256(mp_session.encode()).hexdigest()

    data = [
        SessionResponse(
            id=s.id,
            ip=s.ip,
            user_agent=s.user_agent,
            created_at=s.created_at,
            expires_at=s.expires_at,
            is_current=(s.token_hash == current_token_hash),
        ).model_dump()
        for s in sessions
    ]
    return success_response(data=data)

@router.delete("/sessions/{session_id}")
async def revoke_other_session(
    session_id: str,
    user: User = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
    mp_session: str | None = Cookie(default=None, alias=COOKIE_NAME)
):
    """Revoke a specific session."""
    import hashlib
    current_token_hash = None
    if mp_session:
        current_token_hash = hashlib.sha256(mp_session.encode()).hexdigest()

    stmt = select(Session).where(Session.id == session_id, Session.user_id == user.id)
    result = await db.execute(stmt)
    session_to_delete = result.scalar_one_or_none()

    if not session_to_delete:
        raise HTTPException(404, detail="Session not found")
        
    if current_token_hash and session_to_delete.token_hash == current_token_hash:
        raise HTTPException(400, detail="Cannot revoke current active session from here")

    await db.delete(session_to_delete)
    await db.commit()
    return success_response(data={"message": "Session revoked"})

# ── Billing History ───────────────────────────────────────────────────────────

@router.get("/billing/history")
async def get_billing_history(
    user: User = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """Get billing history."""
    stmt = select(Payment).where(Payment.user_id == user.id).order_by(Payment.created_at.desc())
    result = await db.execute(stmt)
    payments = result.scalars().all()

    data = [
        BillingHistoryResponse(
            id=p.id,
            amount_inr=p.amount_inr,
            currency=p.currency,
            status=p.status,
            razorpay_payment_id=p.razorpay_payment_id,
            created_at=p.created_at,
        ).model_dump()
        for p in payments
    ]
    return success_response(data=data)
