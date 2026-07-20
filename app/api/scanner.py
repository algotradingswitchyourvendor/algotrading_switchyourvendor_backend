"""
Scanner API — Real-time condition scanner.

POST /api/v1/scanner — Evaluate conditions and return matching stocks
GET /api/v1/scanner/presets — Return predefined scanner conditions
"""

from fastapi import APIRouter, Request

from app.config.holidays import get_market_status
from app.schemas.response import success_response, error_response
from app.schemas.scanner import ScannerRequest
from app.services.scanner_service import evaluate_scanner, get_scanner_presets

router = APIRouter()


@router.post("/scanner")
async def run_scanner(request: Request, body: ScannerRequest):
    """
    Evaluate scanner conditions and return matching stocks.

    Conditions are evaluated server-side against the live snapshot
    or historical parquet data. Frontend never evaluates conditions.
    """
    cache = request.app.state.live_cache

    if body.mode == "live" and not cache.is_populated:
        return error_response(
            code="NO_DATA",
            message="Market data not yet available for live scanning.",
        )

    try:
        conditions = [c.model_dump() for c in body.conditions]

        historical_params = None
        if body.mode == "historical":
            historical_params = {
                "date": body.date,
                "start_time": body.start_time,
                "end_time": body.end_time,
            }

        records, meta = evaluate_scanner(
            cache=cache,
            conditions=conditions,
            mode=body.mode,
            sort_by=body.sort_by,
            sort_order=body.sort_order,
            page=body.page,
            page_size=body.page_size,
            historical_params=historical_params,
        )

        return success_response(
            data=records,
            market_status=get_market_status(),
        ) | {"meta": meta}

    except Exception as e:
        return error_response(
            code="SCANNER_ERROR",
            message=f"Scanner evaluation failed: {str(e)}",
        )


@router.get("/scanner/presets")
async def get_presets():
    """Return predefined scanner conditions."""
    presets = get_scanner_presets()
    return success_response(
        data=presets,
        market_status=get_market_status(),
    )
