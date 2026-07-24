"""
Market Status API — Market hours, status, and index data.

GET /api/v1/market-status — Current market status (LIVE/CLOSED/HOLIDAY)
GET /api/v1/indices — Market index data (NIFTY, SENSEX, etc.)
"""

from fastapi import APIRouter, Request

from app.config.holidays import get_market_status_detail, get_market_status
from app.schemas.response import success_response

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
