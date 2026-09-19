"""
Auth API endpoints.

Routes:
  GET  /api/v1/auth/me                 — current user profile
  POST /api/v1/auth/logout             — revoke session
  GET  /api/v1/auth/google             — start Google OAuth flow
  GET  /api/v1/auth/google/callback    — Google OAuth callback
  GET  /api/v1/auth/upstox             — start Upstox OAuth flow
  GET  /api/v1/auth/upstox/callback    — Upstox OAuth callback
  GET  /api/v1/auth/zerodha            — start Zerodha OAuth (compliance-gated)
  GET  /api/v1/auth/zerodha/callback   — Zerodha OAuth callback

Session cookie: mp_session
  - HttpOnly: prevents JS access
  - Secure: set in production (HTTPS only)
  - SameSite=Lax: allows redirect from OAuth providers
  - Max-Age: 30 days

CSRF protection: state parameter is a signed random token stored in
a short-lived cookie, verified on callback.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.account_linking import find_or_create_user, maybe_bootstrap_admin
from app.auth.crypto import generate_state_token
from app.auth.dependencies import get_current_user, require_auth
from app.auth.session import COOKIE_NAME, SESSION_TTL_DAYS, create_session, revoke_session
import app.auth.providers.google as google_provider
import app.auth.providers.upstox as upstox_provider
import app.auth.providers.zerodha as zerodha_provider
from app.config.settings import get_settings
from app.db.database import get_db
from app.db.models import User
from app.schemas.response import error_response, success_response

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])

OAUTH_STATE_COOKIE = "mp_oauth_state"
OAUTH_STATE_TTL = 600  # 10 minutes


def _set_session_cookie(response: Response, token: str, settings=None) -> None:
    """Set the session cookie on the response."""
    if settings is None:
        settings = get_settings()

    is_production = not settings.FRONTEND_URL.startswith("http://localhost")

    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=SESSION_TTL_DAYS * 86400,
        httponly=True,
        secure=is_production,
        samesite="lax",
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    """Clear the session cookie."""
    response.delete_cookie(key=COOKIE_NAME, path="/")


def _set_state_cookie(response: Response, state: str) -> None:
    """Set the OAuth state cookie (short-lived, for CSRF protection)."""
    response.set_cookie(
        key=OAUTH_STATE_COOKIE,
        value=state,
        max_age=OAUTH_STATE_TTL,
        httponly=True,
        samesite="lax",
        path="/api/v1/auth",
    )


def _validate_state(received_state: str, cookie_state: Optional[str]) -> bool:
    """Validate OAuth state parameter matches stored state cookie."""
    if not cookie_state or not received_state:
        return False
    return received_state == cookie_state


# ── GET /auth/me ──────────────────────────────────────────────────────────────

@router.get("/me")
async def get_me(user: Optional[User] = Depends(get_current_user)):
    """Return current user profile. Returns null data if not authenticated."""
    if user is None:
        return success_response(data=None)

    return success_response(data={
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "avatar_url": user.avatar_url,
        "role": user.role,
        "status": user.status,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
    })


# ── POST /auth/logout ─────────────────────────────────────────────────────────

@router.post("/logout")
async def logout(
    response: Response,
    db: AsyncSession = Depends(get_db),
    mp_session: Optional[str] = Cookie(default=None, alias=COOKIE_NAME),
):
    """Revoke the current session and clear the session cookie."""
    if mp_session:
        await revoke_session(db, mp_session)
    _clear_session_cookie(response)
    return success_response(data={"message": "Logged out successfully"})


# ── Google OAuth ──────────────────────────────────────────────────────────────

@router.get("/google")
async def google_login(response: Response):
    """Start Google OAuth flow. Redirects to Google login page."""
    settings = get_settings()
    if not settings.GOOGLE_CLIENT_ID:
        raise HTTPException(503, detail="Google OAuth is not configured")

    state = generate_state_token()
    auth_url = google_provider.get_authorization_url(state)
    redirect = RedirectResponse(url=auth_url, status_code=302)
    _set_state_cookie(redirect, state)
    return redirect


@router.get("/google/callback")
async def google_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    mp_oauth_state: Optional[str] = Cookie(default=None, alias=OAUTH_STATE_COOKIE),
):
    """Handle Google OAuth callback."""
    settings = get_settings()

    if error:
        logger.warning(f"Google OAuth error: {error}")
        return RedirectResponse(url=f"{settings.FRONTEND_URL}/auth/sign-in?error=oauth_denied")

    if not _validate_state(state, mp_oauth_state):
        logger.warning("Google OAuth state mismatch — possible CSRF")
        return RedirectResponse(url=f"{settings.FRONTEND_URL}/auth/sign-in?error=invalid_state")

    if not code:
        return RedirectResponse(url=f"{settings.FRONTEND_URL}/auth/sign-in?error=no_code")

    try:
        user_info = await google_provider.exchange_code(code)
        user = await find_or_create_user(db, user_info)
        await maybe_bootstrap_admin(db, user)
        ip = request.client.host if request.client else None
        user_agent = request.headers.get("user-agent")
        _, raw_token = await create_session(db, user, ip=ip, user_agent=user_agent)
        await db.commit()

        redirect = RedirectResponse(
            url=f"{settings.FRONTEND_URL}/auth/callback?provider=google",
            status_code=302,
        )
        _set_session_cookie(redirect, raw_token, settings)
        redirect.delete_cookie(OAUTH_STATE_COOKIE, path="/api/v1/auth")
        return redirect

    except Exception as e:
        await db.rollback()
        logger.error(f"Google OAuth callback error: {e}", exc_info=True)
        return RedirectResponse(url=f"{settings.FRONTEND_URL}/auth/sign-in?error=auth_failed")


# ── Upstox OAuth ──────────────────────────────────────────────────────────────

@router.get("/upstox")
async def upstox_login(response: Response):
    """Start Upstox OAuth flow. Redirects to Upstox login page."""
    settings = get_settings()
    if not settings.UPSTOX_AUTH_CLIENT_ID:
        raise HTTPException(503, detail="Upstox OAuth is not configured")

    state = generate_state_token()
    try:
        auth_url = upstox_provider.get_authorization_url(state)
    except ValueError as e:
        raise HTTPException(503, detail=str(e))

    redirect = RedirectResponse(url=auth_url, status_code=302)
    _set_state_cookie(redirect, state)
    return redirect


@router.get("/upstox/callback")
async def upstox_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    mp_oauth_state: Optional[str] = Cookie(default=None, alias=OAUTH_STATE_COOKIE),
):
    """Handle Upstox OAuth callback."""
    settings = get_settings()

    if error:
        logger.warning(f"Upstox OAuth error: {error}")
        return RedirectResponse(url=f"{settings.FRONTEND_URL}/auth/sign-in?error=oauth_denied")

    if not _validate_state(state, mp_oauth_state):
        logger.warning("Upstox OAuth state mismatch — possible CSRF")
        return RedirectResponse(url=f"{settings.FRONTEND_URL}/auth/sign-in?error=invalid_state")

    if not code:
        return RedirectResponse(url=f"{settings.FRONTEND_URL}/auth/sign-in?error=no_code")

    try:
        user_info = await upstox_provider.exchange_code(code)
        user = await find_or_create_user(db, user_info)
        await maybe_bootstrap_admin(db, user)
        ip = request.client.host if request.client else None
        user_agent = request.headers.get("user-agent")
        _, raw_token = await create_session(db, user, ip=ip, user_agent=user_agent)
        await db.commit()

        redirect = RedirectResponse(
            url=f"{settings.FRONTEND_URL}/auth/callback?provider=upstox",
            status_code=302,
        )
        _set_session_cookie(redirect, raw_token, settings)
        redirect.delete_cookie(OAUTH_STATE_COOKIE, path="/api/v1/auth")
        return redirect

    except Exception as e:
        await db.rollback()
        logger.error(f"Upstox OAuth callback error: {e}", exc_info=True)
        return RedirectResponse(url=f"{settings.FRONTEND_URL}/auth/sign-in?error=auth_failed")


# ── Zerodha OAuth ──────────────────────────────────────────────────────────────

@router.get("/zerodha")
async def zerodha_login():
    """
    Start Zerodha OAuth flow.

    Returns 503 if ZERODHA_MULTI_USER_ENABLED=false (default).
    Multi-user access requires Zerodha compliance approval.
    Contact kiteconnect@zerodha.com to enable.
    """
    settings = get_settings()

    if not settings.ZERODHA_MULTI_USER_ENABLED:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "ZERODHA_NOT_ENABLED",
                "message": (
                    "Zerodha multi-user login requires compliance approval from Zerodha. "
                    "Contact kiteconnect@zerodha.com. "
                    "Set ZERODHA_MULTI_USER_ENABLED=true after receiving approval."
                ),
            },
        )

    state = generate_state_token()
    try:
        auth_url = zerodha_provider.get_authorization_url(state)
    except (RuntimeError, ValueError) as e:
        raise HTTPException(503, detail=str(e))

    redirect = RedirectResponse(url=auth_url, status_code=302)
    _set_state_cookie(redirect, state)
    return redirect


@router.get("/zerodha/callback")
async def zerodha_callback(
    request: Request,
    request_token: Optional[str] = None,
    status: Optional[str] = None,
    action: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """Handle Zerodha Kite Connect callback. Note: Kite uses 'request_token', not 'code'."""
    settings = get_settings()

    if not settings.ZERODHA_MULTI_USER_ENABLED:
        raise HTTPException(503, detail="Zerodha multi-user login is not enabled")

    if status != "success" or not request_token:
        logger.warning(f"Zerodha callback failed: status={status}")
        return RedirectResponse(url=f"{settings.FRONTEND_URL}/auth/sign-in?error=oauth_denied")

    try:
        user_info = await zerodha_provider.exchange_request_token(request_token)
        user = await find_or_create_user(db, user_info)
        await maybe_bootstrap_admin(db, user)
        ip = request.client.host if request.client else None
        user_agent = request.headers.get("user-agent")
        _, raw_token = await create_session(db, user, ip=ip, user_agent=user_agent)
        await db.commit()

        redirect = RedirectResponse(
            url=f"{settings.FRONTEND_URL}/auth/callback?provider=zerodha",
            status_code=302,
        )
        _set_session_cookie(redirect, raw_token, settings)
        return redirect

    except Exception as e:
        await db.rollback()
        logger.error(f"Zerodha OAuth callback error: {e}", exc_info=True)
        return RedirectResponse(url=f"{settings.FRONTEND_URL}/auth/sign-in?error=auth_failed")
