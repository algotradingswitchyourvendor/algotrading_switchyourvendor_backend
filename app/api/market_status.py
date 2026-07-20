"""
Market Status API — Market hours, status, and index data.

GET /api/v1/market-status — Current market status (LIVE/CLOSED/HOLIDAY)
GET /api/v1/indices — Market index data (NIFTY, SENSEX, etc.)
"""

from fastapi import APIRouter, Request

from app.config.holidays import get_market_status_detail, get_market_status
from app.schemas.response import success_response
from app.services.index_service import get_index_data

router = APIRouter()


@router.get("/market-status")
async def market_status():
    """
    Return current market status.

    Includes: status (LIVE/CLOSED/WEEKEND/HOLIDAY), 
    market hours, current time, flags.
    """
    detail = get_market_status_detail()
    return success_response(
        data=detail,
        market_status=detail["status"],
    )


@router.get("/indices")
async def indices(request: Request):
    """
    Return current market index values.

    Fetches NIFTY 50, SENSEX, BANK NIFTY, MIDCAP, FINNIFTY, INDIA VIX
    from Upstox API. Results are cached for 60 seconds.

    These are independent of the main equity DataFrame.
    """
    # Get access token from scheduler if available
    access_token = None
    scheduler = request.app.state.scheduler
    if scheduler:
        access_token = scheduler.access_token

    data = get_index_data(access_token=access_token)

    return success_response(
        data=data,
        market_status=get_market_status(),
    )
