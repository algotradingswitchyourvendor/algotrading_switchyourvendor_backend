"""
Application settings loaded from environment variables.

Uses Pydantic BaseSettings for type-safe configuration with .env file support.
All credentials and deployment-specific values are externalized here.
"""

from pydantic_settings import BaseSettings
from typing import List, Optional
from functools import lru_cache


class Settings(BaseSettings):
    """
    Central configuration for the MarketPulse backend.
    
    All values can be overridden via environment variables or a .env file.
    The .env file is loaded automatically from the backend/ directory.
    """

    # ── Upstox Market Data API Credentials (scheduler / trading) ────────
    UPSTOX_API_KEY: str = ""
    UPSTOX_SECRET_KEY: str = ""
    UPSTOX_CLIENT_ID: str = ""
    UPSTOX_CLIENT_PIN: str = ""
    UPSTOX_TOTP_SECRET: str = ""
    UPSTOX_REDIRECT_URI: str = "https://127.0.0.1:5000/"
    UPSTOX_ACCESS_TOKEN: Optional[str] = None

    # ── Upstox OAuth (for user login — may use same or separate app) ────
    # These are for the SaaS user authentication flow, not the scheduler.
    UPSTOX_AUTH_CLIENT_ID: str = ""
    UPSTOX_AUTH_CLIENT_SECRET: str = ""
    UPSTOX_AUTH_REDIRECT_URI: str = "http://localhost:8000/api/v1/auth/upstox/callback"

    # ── Google OAuth ─────────────────────────────────────────────────────
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_REDIRECT_URI: str = "http://localhost:8000/api/v1/auth/google/callback"

    # ── Zerodha Kite Connect OAuth ────────────────────────────────────────
    # NOTE: Zerodha multi-user OAuth requires compliance approval from Zerodha.
    # Standard Kite Connect is single-user only. Contact kiteconnect@zerodha.com
    # to enable multi-user support for your platform.
    # Set ZERODHA_MULTI_USER_ENABLED=true only after receiving explicit approval.
    ZERODHA_API_KEY: str = ""
    ZERODHA_API_SECRET: str = ""
    ZERODHA_REDIRECT_URI: str = "http://localhost:8000/api/v1/auth/zerodha/callback"
    ZERODHA_MULTI_USER_ENABLED: bool = False

    # ── Database (Neon PostgreSQL via asyncpg) ───────────────────────────
    DATABASE_URL: str = ""  # postgresql+asyncpg://user:pass@host/dbname

    # ── Redis ─────────────────────────────────────────────────────────────
    REDIS_URL: str = "redis://localhost:6379/0"

    # ── Session Security (opaque server-side sessions — no JWT) ──────────
    SESSION_SECRET: str = ""  # 64-char random hex — signs session cookie
    ENCRYPTION_KEY: str = ""  # Fernet key (base64url 32 bytes) — encrypts tokens at rest

    # ── Razorpay (recurring subscription billing) ─────────────────────────
    RAZORPAY_KEY_ID: str = ""
    RAZORPAY_KEY_SECRET: str = ""
    RAZORPAY_WEBHOOK_SECRET: str = ""

    # ── Admin Bootstrap ────────────────────────────────────────────────────
    # Used ONLY at first startup to promote a newly registered user to ADMIN.
    # After bootstrap, authorization is solely based on the database role.
    # Set to empty string to disable bootstrap.
    ADMIN_BOOTSTRAP_EMAIL: str = ""

    # ── Frontend ──────────────────────────────────────────────────────────
    FRONTEND_URL: str = "http://localhost:3000"

    # ── AWS S3 Configuration ────────────────────────────────────────────
    S3_BUCKET_NAME: str = "rahul-upstox01"
    S3_TICKER_FILE_KEY: str = "Merged_Equities_BSE_NSE.xlsx"
    S3_PARQUET_PREFIX: str = "equitydata"
    S3_VARIABLES_PREFIX: str = "Variables"

    # ── Server Configuration ────────────────────────────────────────────
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000
    CORS_ORIGINS: List[str] = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]

    # ── Market Configuration (IST) ──────────────────────────────────────
    MARKET_OPEN_HOUR: int = 9
    MARKET_OPEN_MINUTE: int = 0
    MARKET_CLOSE_HOUR: int = 15
    MARKET_CLOSE_MINUTE: int = 30

    # ── Scheduler Configuration ─────────────────────────────────────────
    FETCH_CHUNK_SIZE: int = 490
    SCHEDULER_ENABLED: bool = True

    # ── WebSocket Configuration ─────────────────────────────────────────
    WS_HEARTBEAT_INTERVAL: int = 30  # seconds

    # ── Cache Configuration ─────────────────────────────────────────────
    CACHE_DIRECTORY: str = "/tmp/marketpulse_cache"
    CACHE_MAX_SIZE_GB: float = 2.0
    CACHE_MAX_FILE_AGE_DAYS: int = 30
    CACHE_METADATA_FILE_EXT: str = ".meta.json"
    CACHE_CLEANUP_INTERVAL: int = 3600  # 1 hour
    DOWNLOAD_LOCK_TIMEOUT: int = 60  # seconds
    S3_CHUNK_SIZE: int = 8192  # 8KB chunks
    S3_HEAD_TTL_SECONDS: int = 15  # TTL for S3 HEAD response cache
    S3_HEAD_CACHE_MAX_ENTRIES: int = 1000  # Max HEAD cache entries
    DF_CACHE_MAX_ENTRIES: int = 3  # Max parquet DataFrames kept in memory (LRU)
    ENABLE_HISTORY_METRICS: bool = True  # Enable structured performance logging

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": True,
        "extra": "ignore",
    }


@lru_cache()
def get_settings() -> Settings:
    """
    Returns a cached singleton Settings instance.
    
    Using lru_cache ensures the .env file is read only once,
    and the same Settings object is reused across the application.
    """
    return Settings()
