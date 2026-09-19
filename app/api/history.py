"""
History API — Historical market data from S3 Parquet.

GET /api/v1/history                    — Historical data with time range filtering
GET /api/v1/history/dates              — List available dates
GET /api/v1/history/timeline/{symbol}  — Minute-by-minute stock timeline

Authentication & Entitlements:
  All routes require authentication.
  Date ranges are clamped to the user's plan entitlement server-side.

  Plan history_days limits:
    FREE    → 30 days
    BASIC   → 180 days
    PRO     → 365 days
    PREMIUM → unlimited (-1)
"""

from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from app.auth.dependencies import require_auth
from app.config.holidays import get_market_status
from app.db.models import User
from app.entitlements.checker import EntitlementChecker
from app.entitlements.dependencies import get_entitlements
from app.schemas.response import success_response, error_response
from app.services.history_service import (
    get_historical_data,
    get_stock_timeline,
    list_available_dates,
)

router = APIRouter()


def _clamp_date(requested_date: Optional[str], max_days: int) -> Optional[str]:
    """
    Clamp a requested date string to within max_days from today.

    If max_days == -1 (unlimited), no clamping is applied.
    If requested_date is None or 'today', no clamping is applied.
    Returns the clamped date as 'YYYY-MM-DD' string.
    """
    if max_days == -1:
        return requested_date

    if not requested_date or requested_date == "today":
        return requested_date

    try:
        req = date.fromisoformat(requested_date)
    except ValueError:
        return requested_date  # let the downstream service handle bad format

    today = date.today()
    earliest_allowed = today - timedelta(days=max_days)

    if req < earliest_allowed:
        return earliest_allowed.isoformat()

    return requested_date


@router.get("/history")
async def get_history(
    user: User = Depends(require_auth),
    ent: EntitlementChecker = Depends(get_entitlements),
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

    The requested date is clamped to the user's plan entitlement.
    FREE users are limited to the last 30 days.
    BASIC users are limited to the last 180 days.
    PRO users are limited to the last 365 days.
    PREMIUM users have access to the full supported history range.
    """
    max_days = ent.get_history_days_limit()
    clamped_date = _clamp_date(date, max_days)

    try:
        records, meta = await get_historical_data(
            symbol=symbol,
            target_date=clamped_date,
            start_time=start_time,
            end_time=end_time,
            sort_by=sort_by,
            sort_order=sort_order,
            page=page,
            page_size=page_size,
        )

        response = success_response(
            data=records,
            market_status=get_market_status(),
        ) | {"meta": meta}

        # Include entitlement context so frontend knows the effective limit
        if max_days != -1 and date and clamped_date != date:
            response["_entitlement"] = {
                "history_days_limit": max_days,
                "requested_date": date,
                "effective_date": clamped_date,
                "clamped": True,
            }

        return response

    except Exception as e:
        return error_response(
            code="HISTORY_ERROR",
            message=f"Failed to retrieve historical data: {str(e)}",
        )


@router.get("/history/dates")
async def get_available_dates(
    user: User = Depends(require_auth),
    ent: EntitlementChecker = Depends(get_entitlements),
):
    """
    Return list of dates that have historical data available.

    Filtered to the user's plan entitlement window.
    """
    dates = await list_available_dates()
    max_days = ent.get_history_days_limit()

    if max_days != -1 and dates:
        today = date.today()
        earliest_allowed = today - timedelta(days=max_days)
        dates = [d for d in dates if d >= earliest_allowed.isoformat()]

    return success_response(
        data=dates,
        market_status=get_market_status(),
    )


@router.get("/history/timeline/{symbol}")
async def get_timeline(
    symbol: str,
    user: User = Depends(require_auth),
    ent: EntitlementChecker = Depends(get_entitlements),
    date: Optional[str] = Query("today", description="Date (YYYY-MM-DD) or 'today'"),
):
    """
    Return minute-by-minute timeline for a single stock.

    Date is clamped to the user's plan entitlement.
    Used by the Stock Analytics page.
    """
    max_days = ent.get_history_days_limit()
    clamped_date = _clamp_date(date, max_days)

    try:
        timeline = await get_stock_timeline(symbol=symbol, target_date=clamped_date)

        if not timeline:
            return error_response(
                code="NO_TIMELINE",
                message=f"No timeline data found for '{symbol}' on {clamped_date}.",
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
