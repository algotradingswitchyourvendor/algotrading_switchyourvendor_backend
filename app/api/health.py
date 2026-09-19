"""
Health API — Minimal public health check.

GET /api/v1/health — Safe minimal status for load balancers and uptime monitors.

IMPORTANT: Internal system details (scheduler, cache, S3, snapshot IDs) are
NEVER exposed here. They are admin-only at GET /api/v1/admin/system/health.
"""

from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
async def health_check():
    """
    Minimal public health check.

    Returns only safe status for load balancers and uptime monitors.
    No internal details are exposed to normal users.

    Full internal health is available at /api/v1/admin/system/health (admin only).
    """
    return {"status": "ok"}
