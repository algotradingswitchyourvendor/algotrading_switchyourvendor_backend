"""
Zerodha Kite Connect OAuth provider.

⚠️  COMPLIANCE REQUIREMENT — READ BEFORE ENABLING  ⚠️

Standard Kite Connect API is SINGLE-USER ONLY by default.
Using it for multiple unaffiliated users without Zerodha approval
is a violation of Kite Connect Terms of Service.

To enable multi-user Zerodha login for a SaaS platform:
  1. Contact kiteconnect@zerodha.com with a comprehensive proposal
  2. Submit business registration docs (Pvt Ltd or LLP required)
  3. Pass Zerodha's platform security and compliance audit
  4. Receive explicit written approval for multi-user access

Until approval is obtained:
  - ZERODHA_MULTI_USER_ENABLED must remain False (default)
  - The login button shows a "pending compliance approval" notice
  - The callback endpoint returns HTTP 503

Token validity: Until ~6:00 AM IST next day.
Authentication checksum: SHA-256(api_key + request_token + api_secret)

Flow (once enabled):
  1. get_authorization_url() → redirect to Kite login
  2. Kite redirects to callback with ?request_token=xxx&status=success
  3. exchange_request_token() → POST with checksum → access_token
  4. Returns UserInfo
"""

import hashlib
import logging
from typing import Optional
from urllib.parse import urlencode

import httpx

from app.config.settings import get_settings
from app.auth.providers.google import UserInfo

logger = logging.getLogger(__name__)

ZERODHA_LOGIN_URL = "https://kite.zerodha.com/connect/login"
ZERODHA_TOKEN_URL = "https://api.kite.trade/session/token"
ZERODHA_PROFILE_URL = "https://api.kite.trade/user/profile"


def _is_enabled() -> bool:
    return get_settings().ZERODHA_MULTI_USER_ENABLED


def get_authorization_url(state: str) -> str:
    """
    Build the Zerodha Kite Connect authorization URL.

    Raises RuntimeError if multi-user is not enabled.
    This error is caught by the auth route and converted to 503.
    """
    if not _is_enabled():
        raise RuntimeError(
            "Zerodha multi-user login is not enabled. "
            "ZERODHA_MULTI_USER_ENABLED=false. "
            "Contact kiteconnect@zerodha.com to enable multi-user access."
        )

    settings = get_settings()
    if not settings.ZERODHA_API_KEY:
        raise ValueError("ZERODHA_API_KEY is not configured")

    params = {
        "v": "3",
        "api_key": settings.ZERODHA_API_KEY,
    }
    # state parameter is passed via redirect_params if supported, else use redirect URI
    return f"{ZERODHA_LOGIN_URL}?{urlencode(params)}"


async def exchange_request_token(request_token: str) -> UserInfo:
    """
    Exchange a Zerodha request_token for an access_token.

    Kite Connect uses a SHA-256 checksum:
    checksum = SHA-256(api_key + request_token + api_secret)

    NOTE: access_token validity is until ~6:00 AM IST next day.
    Users must re-authenticate each day for Zerodha-specific features.
    """
    if not _is_enabled():
        raise RuntimeError("Zerodha multi-user login is disabled — ZERODHA_MULTI_USER_ENABLED=false")

    settings = get_settings()
    api_key = settings.ZERODHA_API_KEY
    api_secret = settings.ZERODHA_API_SECRET

    if not api_key or not api_secret:
        raise ValueError("ZERODHA_API_KEY and ZERODHA_API_SECRET must be set")

    # Compute SHA-256 checksum as required by Kite Connect
    checksum_input = f"{api_key}{request_token}{api_secret}"
    checksum = hashlib.sha256(checksum_input.encode()).hexdigest()

    async with httpx.AsyncClient() as client:
        # Step 1: Exchange request_token for access_token
        token_response = await client.post(
            ZERODHA_TOKEN_URL,
            data={
                "api_key": api_key,
                "request_token": request_token,
                "checksum": checksum,
            },
            headers={"X-Kite-Version": "3"},
            timeout=15.0,
        )

        if token_response.status_code != 200:
            logger.error(f"Zerodha token exchange failed: {token_response.text}")
            raise ValueError(f"Zerodha token exchange failed: HTTP {token_response.status_code}")

        token_data = token_response.json()
        data = token_data.get("data", token_data)
        access_token = data.get("access_token")

        if not access_token:
            raise ValueError("Zerodha did not return access_token")

        # Step 2: Fetch user profile
        profile_response = await client.get(
            ZERODHA_PROFILE_URL,
            headers={
                "Authorization": f"token {api_key}:{access_token}",
                "X-Kite-Version": "3",
            },
            timeout=10.0,
        )

        if profile_response.status_code != 200:
            raise ValueError("Failed to fetch Zerodha user profile")

        profile_data = profile_response.json()
        profile = profile_data.get("data", profile_data)

    user_id = profile.get("user_id")
    email = profile.get("email")
    user_name = profile.get("user_name") or profile.get("user_shortname") or ""

    if not user_id:
        raise ValueError("Zerodha profile did not return user_id")
    if not email:
        raise ValueError("Zerodha profile did not return email")

    return UserInfo(
        provider="zerodha",
        provider_account_id=user_id,
        email=email.lower(),
        name=user_name,
        avatar_url=None,
        access_token=access_token,
        refresh_token=None,  # Zerodha uses daily regeneration
    )
