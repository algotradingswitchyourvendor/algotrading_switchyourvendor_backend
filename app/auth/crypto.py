"""
Cryptographic utilities for MarketPulse auth system.

- Fernet symmetric encryption for storing OAuth tokens at rest
- SHA-256 hashing for opaque session tokens (never store raw tokens)
- Secure random token generation for session cookies
"""

import hashlib
import os
import secrets
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken


@lru_cache(maxsize=1)
def _get_fernet() -> Fernet:
    """
    Return a cached Fernet instance using ENCRYPTION_KEY from settings.

    The key must be a 32-byte URL-safe base64-encoded string,
    as produced by: Fernet.generate_key().decode()
    """
    from app.config.settings import get_settings
    key = get_settings().ENCRYPTION_KEY
    if not key:
        raise RuntimeError(
            "ENCRYPTION_KEY is not set. "
            "Generate one with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_token(plaintext: str) -> str:
    """Encrypt an OAuth access/refresh token for database storage."""
    fernet = _get_fernet()
    return fernet.encrypt(plaintext.encode()).decode()


def decrypt_token(ciphertext: str) -> str:
    """Decrypt a stored OAuth token. Raises InvalidToken if tampered."""
    fernet = _get_fernet()
    try:
        return fernet.decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        raise ValueError("Token decryption failed — token may be corrupted or key may have changed")


def hash_session_token(raw_token: str) -> str:
    """
    Compute SHA-256 hash of a raw session token.

    Only the hash is stored in the database.
    The raw token lives in the HttpOnly cookie only.
    """
    return hashlib.sha256(raw_token.encode()).hexdigest()


def generate_session_token() -> str:
    """
    Generate a cryptographically secure random session token.

    Returns a 64-character hex string (256-bit entropy).
    """
    return secrets.token_hex(32)


def generate_state_token() -> str:
    """Generate a short-lived CSRF state token for OAuth flows."""
    return secrets.token_urlsafe(32)
