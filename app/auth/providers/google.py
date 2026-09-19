"""
Google OAuth 2.0 provider.

Standard OIDC / OAuth 2.0 with full multi-user support.
Identity: verified email, name, avatar_url from Google profile.

Flow:
  1. Redirect user to get_authorization_url()
  2. Google redirects to callback with ?code=xxx&state=yyy
  3. exchange_code() fetches token + user profile
  4. Returns UserInfo for account linking
"""

import logging
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlencode

import httpx

from app.config.settings import get_settings

logger = logging.getLogger(__name__)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"


@dataclass
class UserInfo:
    provider: str
    provider_account_id: str
    email: str
    name: str
    avatar_url: Optional[str]
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None


def get_authorization_url(state: str) -> str:
    """Build the Google OAuth authorization URL."""
    settings = get_settings()
    params = {
        "client_id": settings.GOOGLE_CLIENT_ID,
        "redirect_uri": settings.GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "offline",
        "prompt": "select_account",
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


async def exchange_code(code: str) -> UserInfo:
    """
    Exchange authorization code for access token and fetch user profile.

    Returns UserInfo populated with Google identity data.
    """
    settings = get_settings()

    async with httpx.AsyncClient() as client:
        # Step 1: Exchange code for tokens
        token_response = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
                "redirect_uri": settings.GOOGLE_REDIRECT_URI,
                "code": code,
                "grant_type": "authorization_code",
            },
            timeout=15.0,
        )
        token_response.raise_for_status()
        token_data = token_response.json()

        access_token = token_data.get("access_token")
        refresh_token = token_data.get("refresh_token")

        if not access_token:
            raise ValueError("Google token exchange did not return access_token")

        # Step 2: Fetch user profile
        profile_response = await client.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10.0,
        )
        profile_response.raise_for_status()
        profile = profile_response.json()

    email = profile.get("email")
    if not email:
        raise ValueError("Google profile did not return an email address")

    sub = profile.get("sub")  # Google's unique user identifier
    if not sub:
        raise ValueError("Google profile missing 'sub' identifier")

    return UserInfo(
        provider="google",
        provider_account_id=sub,
        email=email.lower(),
        name=profile.get("name") or email.split("@")[0],
        avatar_url=profile.get("picture"),
        access_token=access_token,
        refresh_token=refresh_token,
    )
