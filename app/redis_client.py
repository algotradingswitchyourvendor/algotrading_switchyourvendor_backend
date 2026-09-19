"""
Redis client for MarketPulse.

Used for:
  - Rate limiting (scanner daily usage counters)
  - Subscription entitlement cache (fast plan lookup)
  - Session cache (optional — sessions are primarily in PostgreSQL)
  - WebSocket pub/sub coordination

Key namespacing:
  mp:usage:scanner:{user_id}:{YYYY-MM-DD}   — daily scanner count
  mp:sub:{user_id}                           — cached plan name (TTL=300s)
  mp:rate:{user_id}:{endpoint}               — general rate limiter

Market tick data is NOT stored in Redis.
Durable market history remains in S3/DuckDB.
PostgreSQL is the source of truth for all business data.
"""

import logging
from typing import Optional

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

_redis_client: Optional[aioredis.Redis] = None


async def init_redis() -> aioredis.Redis:
    """Initialize the Redis connection pool. Called once on startup."""
    global _redis_client
    from app.config.settings import get_settings
    settings = get_settings()

    if not settings.REDIS_URL:
        logger.warning("REDIS_URL not set — Redis features (rate limiting, caching) will be disabled")
        return None

    try:
        _redis_client = aioredis.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            max_connections=20,
        )
        await _redis_client.ping()
        logger.info("Redis connection established")
        return _redis_client
    except Exception as e:
        logger.warning(f"Redis unavailable ({e}), continuing without rate limiting")
        _redis_client = None
        return None


async def get_redis() -> Optional[aioredis.Redis]:
    """Return the Redis client. May be None if Redis is not configured."""
    return _redis_client


async def close_redis() -> None:
    """Close the Redis connection pool. Called on shutdown."""
    global _redis_client
    if _redis_client:
        await _redis_client.aclose()
        _redis_client = None
        logger.info("Redis connection closed")


# ── Subscription Cache ─────────────────────────────────────────────────────

SUBSCRIPTION_CACHE_TTL = 300  # 5 minutes
SUBSCRIPTION_CACHE_KEY = "mp:sub:{user_id}"


async def cache_user_plan(user_id: str, plan_name: str) -> None:
    """Cache the user's plan name in Redis for fast entitlement lookups."""
    if _redis_client is None:
        return
    key = SUBSCRIPTION_CACHE_KEY.format(user_id=user_id)
    await _redis_client.setex(key, SUBSCRIPTION_CACHE_TTL, plan_name)


async def get_cached_plan(user_id: str) -> Optional[str]:
    """Get the user's cached plan name from Redis. Returns None if not cached."""
    if _redis_client is None:
        return None
    key = SUBSCRIPTION_CACHE_KEY.format(user_id=user_id)
    return await _redis_client.get(key)


async def invalidate_user_plan_cache(user_id: str) -> None:
    """Invalidate the subscription cache for a user. Called on plan change."""
    if _redis_client is None:
        return
    key = SUBSCRIPTION_CACHE_KEY.format(user_id=user_id)
    await _redis_client.delete(key)
    logger.info(f"Invalidated subscription cache for user={user_id}")
