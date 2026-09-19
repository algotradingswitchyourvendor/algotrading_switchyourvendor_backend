"""
Upstox OAuth 2.0 provider.

Upstox supports standard OAuth 2.0 Authorization Code flow.
Multi-user apps are supported — each user individually authorizes.

Identity: fetched from GET /user/profile after token exchange.
Fields: user_id (UCC), user_name, email.

Token validity: Until 3:30 AM IST next day.
The Upstox access token is stored encrypted and can be used
for live market data queries on behalf of the user.

Flow:
  1. get_authorization_url() → redirect user to Upstox login
  2. Upstox redirects to callback with ?code=xxx
  3. exchange_code() → POST to token endpoint → GET /user/profile
  4. Returns UserInfo
"""

import logging
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlencode

import httpx

from app.config.settings import get_settings
from app.auth.providers.google import UserInfo  # reuse UserInfo dataclass

logger = logging.getLogger(__name__)

UPSTOX_AUTH_URL = "https://api.upstox.com/v2/login/authorization/dialog"
UPSTOX_TOKEN_URL = "https://api.upstox.com/v2/login/authorization/token"
UPSTOX_PROFILE_URL = "https://api.upstox.com/v2/user/profile"


def get_authorization_url(state: str) -> str:
    """Build the Upstox OAuth authorization URL."""
    settings = get_settings()

    if not settings.UPSTOX_AUTH_CLIENT_ID:
        raise ValueError(
            "UPSTOX_AUTH_CLIENT_ID is not configured. "
            "Set up an Upstox developer app at https://account.upstox.com/developer/apps"
        )

    params = {
        "client_id": settings.UPSTOX_AUTH_CLIENT_ID,
        "redirect_uri": settings.UPSTOX_AUTH_REDIRECT_URI,
        "response_type": "code",
        "state": state,
    }
    return f"{UPSTOX_AUTH_URL}?{urlencode(params)}"


async def exchange_code(code: str) -> UserInfo:
    """
    Exchange Upstox authorization code for access token.
    Then fetch user profile to get identity information.

    Returns UserInfo with Upstox user_id as provider_account_id.
    """
    settings = get_settings()

    async with httpx.AsyncClient() as client:
        # Step 1: Exchange code for access token
        token_response = await client.post(
            UPSTOX_TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.UPSTOX_AUTH_CLIENT_ID,
                "client_secret": settings.UPSTOX_AUTH_CLIENT_SECRET,
                "redirect_uri": settings.UPSTOX_AUTH_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
            headers={"Accept": "application/json"},
            timeout=15.0,
        )

        if token_response.status_code != 200:
            logger.error(f"Upstox token exchange failed: {token_response.text}")
            raise ValueError(f"Upstox token exchange failed: HTTP {token_response.status_code}")

        token_data = token_response.json()
        access_token = token_data.get("access_token")

        if not access_token:
            raise ValueError("Upstox did not return access_token")

        # Step 2: Fetch user profile to get identity
        profile_response = await client.get(
            UPSTOX_PROFILE_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
            timeout=10.0,
        )

        if profile_response.status_code != 200:
            logger.error(f"Upstox profile fetch failed: {profile_response.text}")
            raise ValueError("Failed to fetch Upstox user profile")

        profile_data = profile_response.json()
        # Upstox wraps data in {"status": "success", "data": {...}}
        profile = profile_data.get("data", profile_data)

    user_id = profile.get("user_id")
    email = profile.get("email")
    user_name = profile.get("user_name") or profile.get("name") or ""

    if not user_id:
        raise ValueError("Upstox profile did not return user_id")
    if not email:
        raise ValueError("Upstox profile did not return email")

    return UserInfo(
        provider="upstox",
        provider_account_id=user_id,  # Upstox UCC (unique client code)
        email=email.lower(),
        name=user_name,
        avatar_url=None,  # Upstox profile does not return avatar
        access_token=access_token,
        refresh_token=None,  # Upstox uses daily token regeneration
    )
