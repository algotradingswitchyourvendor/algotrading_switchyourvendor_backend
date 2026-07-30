"""
History API — Historical market data from S3 Parquet.

GET /api/v1/history — Historical data with time range filtering
GET /api/v1/history/dates — List available dates
GET /api/v1/history/timeline/{symbol} — Minute-by-minute stock timeline
"""

from typing import Optional

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from app.config.holidays import get_market_status
from app.schemas.response import success_response, error_response
from app.services.history_service import (
    get_historical_data,
    get_stock_timeline,
    list_available_dates,
)

router = APIRouter()


@router.get("/history")
async def get_history(
    symbol: Optional[str] = Query(None, description="Instrument key or symbol"),
    date: Optional[str] = Query("today", description="Date (YYYY-MM-DD) or 'today'"),
    start_time: Optional[str] = Query(None, description="Start time (HH:MM)"),
    end_time: Optional[str] = Query(None, description="End time (HH:MM)"),
    sort_by: Optional[str] = Query(None, description="Column to sort by"),
    sort_order: Optional[str] = Query("asc", description="Sort order (asc/desc)"),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=1000),
):
    """
    Return historical market data from S3 Parquet.

    Supports filtering by symbol, date, and time range.
    Downloads and caches data synchronously.
    """
    try:
        records, meta = await get_historical_data(
            symbol=symbol,
            target_date=date,
            start_time=start_time,
            end_time=end_time,
            sort_by=sort_by,
            sort_order=sort_order,
            page=page,
            page_size=page_size,
        )

        return success_response(
            data=records,
            market_status=get_market_status(),
        ) | {"meta": meta}

    except Exception as e:
        return error_response(
            code="HISTORY_ERROR",
            message=f"Failed to retrieve historical data: {str(e)}",
        )


@router.get("/history/dates")
async def get_available_dates():
    """Return list of dates that have historical data available."""
    dates = await list_available_dates()
    return success_response(
        data=dates,
        market_status=get_market_status(),
    )


@router.get("/history/timeline/{symbol}")
async def get_timeline(
    symbol: str,
    date: Optional[str] = Query("today", description="Date (YYYY-MM-DD) or 'today'"),
):
    """
    Return minute-by-minute timeline for a single stock.

    Groups all snapshots by minute and returns one row per minute.
    Used by the Stock Analytics page (Screen 2).
    """
    try:
        timeline = await get_stock_timeline(symbol=symbol, target_date=date)

        if not timeline:
            return error_response(
                code="NO_TIMELINE",
                message=f"No timeline data found for '{symbol}' on {date}.",
            )

        return success_response(
            data=timeline,
            market_status=get_market_status(),
        )

    except Exception as e:
        return error_response(
            code="TIMELINE_ERROR",
            message=f"Failed to build timeline: {str(e)}",
        )
