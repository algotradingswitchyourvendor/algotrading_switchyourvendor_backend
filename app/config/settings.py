"""
Application settings loaded from environment variables.

Uses Pydantic BaseSettings for type-safe configuration with .env file support.
All credentials and deployment-specific values are externalized here.
"""

from pydantic_settings import BaseSettings
from typing import List
from functools import lru_cache


class Settings(BaseSettings):
    """
    Central configuration for the MarketPulse backend.
    
    All values can be overridden via environment variables or a .env file.
    The .env file is loaded automatically from the backend/ directory.
    """

    # ── Upstox API Credentials ──────────────────────────────────────────
    UPSTOX_API_KEY: str = ""
    UPSTOX_SECRET_KEY: str = ""
    UPSTOX_CLIENT_ID: str = ""
    UPSTOX_CLIENT_PIN: str = ""
    UPSTOX_TOTP_SECRET: str = ""
    UPSTOX_REDIRECT_URI: str = "https://127.0.0.1:5000/"

    # ── AWS S3 Configuration ────────────────────────────────────────────
    S3_BUCKET_NAME: str = "rahul-upstox01"
    S3_TICKER_FILE_KEY: str = "Merged_Equities_BSE_NSE.xlsx"
    S3_PARQUET_PREFIX: str = "equitydata"

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
