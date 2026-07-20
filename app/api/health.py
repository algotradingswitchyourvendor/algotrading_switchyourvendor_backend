"""
Health API — System health check.

GET /api/v1/health — Backend, scheduler, cache, S3 health status
"""

from fastapi import APIRouter, Request

from app.config.holidays import get_market_status
from app.schemas.response import success_response

router = APIRouter()


@router.get("/health")
async def health_check(request: Request):
    """
    Return system health status.

    Reports on: backend, scheduler, cache, S3 connectivity.
    """
    cache = request.app.state.live_cache
    scheduler = request.app.state.scheduler

    cache_info = cache.get_snapshot_info()

    scheduler_status = "disabled"
    if scheduler:
        scheduler_status = "running" if scheduler.is_running else "stopped"

    # S3 connectivity check (lightweight — just checks if client exists)
    s3_status = "unknown"
    if scheduler and scheduler.s3_client:
        try:
            scheduler.s3_client.head_bucket(
                Bucket=scheduler.settings.S3_BUCKET_NAME
            )
            s3_status = "connected"
        except Exception:
            s3_status = "error"

    market_status_str = get_market_status()

    health_data = {
        "status": "healthy",
        "backend": "healthy",  # If we're responding, backend is healthy
        "scheduler": scheduler_status,
        "cache": {
            "populated": cache_info["is_populated"],
            "snapshot_id": cache_info["snapshot_id"],
            "last_updated": cache_info["last_updated"],
            "instruments": cache_info["total_instruments"],
            "columns": cache_info["total_columns"],
        },
        "s3": s3_status,
        "market_status": market_status_str,
    }

    # Mark degraded only if scheduler is down AND market is LIVE
    # (scheduler being disabled/stopped during closed hours is normal)
    market_is_live = market_status_str in ("LIVE",)
    if market_is_live and scheduler_status not in ("running", "disabled"):
        health_data["status"] = "degraded"

    return success_response(
        data=health_data,
        market_status=get_market_status(),
    )
