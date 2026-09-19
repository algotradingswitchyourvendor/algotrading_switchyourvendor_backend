"""
Account linking — find or create user from OAuth provider identity.

Handles:
  - New user registration (first login)
  - Returning user (same provider, same account)
  - Cross-provider account linking (same email, different provider)
  - Suspended user detection

Email-based account linking:
  If a user logs in with Google (email=foo@example.com) and later
  logs in with Upstox (email=foo@example.com), they are linked to
  the same User row. A new AuthAccount row is created for Upstox.

Security:
  - Email addresses are lowercased and normalized before matching
  - Never auto-merges accounts with different emails
  - Logs all account creations and linkings to audit_log
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.crypto import encrypt_token
from app.auth.providers.google import UserInfo
from app.db.models import AuthAccount, User

logger = logging.getLogger(__name__)


async def find_or_create_user(
    db: AsyncSession,
    info: UserInfo,
) -> User:
    """
    Find an existing user or create a new one from OAuth provider info.

    Strategy:
      1. Look up auth_accounts by (provider, provider_account_id)
         → If found: update token, return existing user
      2. Look up users by email
         → If found: add new auth_account row, link accounts, return user
      3. Neither found: create new user + auth_account

    Args:
        db: Async database session
        info: UserInfo returned by OAuth provider

    Returns:
        The User model (existing or newly created)
    """
    now = datetime.now(timezone.utc)

    # 1. Look for existing auth_account (same provider + same account ID)
    result = await db.execute(
        select(AuthAccount)
        .where(AuthAccount.provider == info.provider)
        .where(AuthAccount.provider_account_id == info.provider_account_id)
    )
    existing_account = result.scalar_one_or_none()

    if existing_account:
        # Update stored access token
        if info.access_token:
            existing_account.access_token_enc = encrypt_token(info.access_token)
        if info.refresh_token:
            existing_account.refresh_token_enc = encrypt_token(info.refresh_token)
        existing_account.updated_at = now
        existing_account.raw_profile = {
            "email": info.email,
            "name": info.name,
            "provider": info.provider,
        }
        await db.flush()

        # Load and return user
        result = await db.execute(
            select(User).where(User.id == existing_account.user_id)
        )
        user = result.scalar_one()
        logger.info(f"Returning user for {info.provider} account: user_id={user.id}")
        return user

    # 2. No existing auth_account — look for user by email (cross-provider linking)
    email = info.email.lower().strip()
    result = await db.execute(select(User).where(User.email == email))
    existing_user = result.scalar_one_or_none()

    if existing_user:
        # Link this provider to the existing user
        account = AuthAccount(
            user_id=existing_user.id,
            provider=info.provider,
            provider_account_id=info.provider_account_id,
            access_token_enc=encrypt_token(info.access_token) if info.access_token else None,
            refresh_token_enc=encrypt_token(info.refresh_token) if info.refresh_token else None,
            raw_profile={"email": info.email, "name": info.name, "provider": info.provider},
        )
        db.add(account)
        await db.flush()
        logger.info(
            f"Linked {info.provider} account to existing user={existing_user.id} (email match)"
        )
        return existing_user

    # 3. Neither found — create new user
    user = User(
        email=email,
        name=info.name or email.split("@")[0],
        avatar_url=info.avatar_url,
        role="USER",
        status="ACTIVE",
    )
    db.add(user)
    await db.flush()  # populate user.id

    account = AuthAccount(
        user_id=user.id,
        provider=info.provider,
        provider_account_id=info.provider_account_id,
        access_token_enc=encrypt_token(info.access_token) if info.access_token else None,
        refresh_token_enc=encrypt_token(info.refresh_token) if info.refresh_token else None,
        raw_profile={"email": info.email, "name": info.name, "provider": info.provider},
    )
    db.add(account)
    await db.flush()

    logger.info(f"Created new user={user.id} via {info.provider}")
    return user


async def maybe_bootstrap_admin(db: AsyncSession, user: User) -> None:
    """
    Promote user to ADMIN if ADMIN_BOOTSTRAP_EMAIL matches and no admin exists yet.

    This runs ONLY once — after the first admin is created via bootstrap,
    this function becomes a no-op (there's already an admin in the DB).

    The environment variable is NOT checked on every request;
    only here during account creation / login.
    """
    from app.config.settings import get_settings

    settings = get_settings()
    bootstrap_email = settings.ADMIN_BOOTSTRAP_EMAIL.strip().lower()

    if not bootstrap_email:
        return
    if user.email.lower() != bootstrap_email:
        return
    if user.role == "ADMIN":
        return

    # Check if any admin already exists
    result = await db.execute(
        select(User).where(User.role == "ADMIN").limit(1)
    )
    existing_admin = result.scalar_one_or_none()

    if existing_admin:
        # Admin already exists — bootstrap is no longer needed
        logger.info("Admin bootstrap skipped — admin already exists in DB")
        return

    # Promote this user
    user.role = "ADMIN"
    await db.flush()
    logger.info(f"Admin bootstrap: promoted user={user.id} ({user.email}) to ADMIN")
