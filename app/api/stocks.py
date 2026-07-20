"""
Stocks API — Paginated stock list and individual stock details.

GET /api/v1/stocks — Paginated, sorted, filtered, searchable stock list
GET /api/v1/stocks/{symbol} — Single stock latest snapshot
"""

from typing import Optional

from fastapi import APIRouter, Query, Request

from app.config.holidays import get_market_status
from app.schemas.response import success_response, error_response
from app.schemas.stock import SortOrder
from app.services.market_service import get_stocks_paginated, get_stock_detail

router = APIRouter()


@router.get("/stocks")
async def list_stocks(
    request: Request,
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(50, ge=1, le=500, description="Rows per page"),
    sort_by: Optional[str] = Query(None, description="Column to sort by"),
    sort_order: SortOrder = Query(SortOrder.ASC, description="Sort direction"),
    search: Optional[str] = Query(None, description="Search query"),
    filters: Optional[str] = Query(None, description="JSON filter string"),
    columns: Optional[str] = Query(None, description="Comma-separated columns"),
):
    """
    Return paginated stock list from live cache.

    Supports full-text search, dynamic column filtering, sorting, 
    column selection, and pagination.
    """
    cache = request.app.state.live_cache

    if not cache.is_populated:
        return error_response(
            code="NO_DATA",
            message="Market data not yet available.",
        )

    records, meta = get_stocks_paginated(
        cache=cache,
        page=page,
        page_size=page_size,
        sort_by=sort_by,
        sort_order=sort_order,
        search=search,
        filters=filters,
        columns=columns,
    )

    return success_response(
        data=records,
        market_status=get_market_status(),
        meta=None,  # We'll pass meta directly
    ) | {"meta": meta}


@router.get("/stocks/{symbol}")
async def get_stock(request: Request, symbol: str):
    """
    Return latest snapshot for a single stock.

    The symbol can be:
    - Full instrument key: "NSE_EQ:INFY"
    - Trading symbol: "INFY"
    - Partial match: "INF"
    """
    cache = request.app.state.live_cache

    if not cache.is_populated:
        return error_response(
            code="NO_DATA",
            message="Market data not yet available.",
        )

    stock = get_stock_detail(cache, symbol)

    if stock is None:
        return error_response(
            code="STOCK_NOT_FOUND",
            message=f"Stock '{symbol}' not found in current snapshot.",
        )

    return success_response(
        data=stock,
        market_status=get_market_status(),
    )
