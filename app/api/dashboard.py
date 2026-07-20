"""
Dashboard API — GET /api/v1/dashboard

Returns the full latest market snapshot for initial page load.
Subsequent updates are delivered via WebSocket.
"""

from fastapi import APIRouter, Request

from app.config.holidays import get_market_status
from app.schemas.response import success_response, error_response
from app.services.market_service import get_dashboard_data

router = APIRouter()


@router.get("/dashboard")
async def get_dashboard(request: Request):
    """
    Return complete latest market snapshot.

    Used by the frontend for initial dashboard load.
    After initial load, the frontend connects to WebSocket for live updates.
    """
    cache = request.app.state.live_cache

    if not cache.is_populated:
        return error_response(
            code="NO_DATA",
            message="Market data not yet available. The scheduler may still be starting.",
        )

    data = get_dashboard_data(cache)
    return success_response(
        data=data,
        market_status=get_market_status(),
    )
