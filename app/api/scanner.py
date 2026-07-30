"""
Scanner API — Unified scanner endpoints.

POST /api/v1/scanner/query  — New unified endpoint (query engine)
POST /api/v1/scanner        — Legacy endpoint (internally routed through query engine)
GET  /api/v1/scanner/presets — Return predefined scanner conditions
"""

from fastapi import APIRouter, Request

from app.config.holidays import get_market_status
from app.schemas.response import success_response, error_response
from app.schemas.scanner import ScannerRequest
from app.schemas.query import UnifiedQueryRequest, QueryCondition
from app.services.query_engine import execute_query
from app.services.scanner_service import get_scanner_presets

router = APIRouter()


@router.post("/scanner/query")
async def query_scanner(request: Request, body: UnifiedQueryRequest):
    """
    Unified scanner query endpoint.

    Accepts either structured conditions or free-text query.
    Supports both live and historical execution targets.
    """
    cache = request.app.state.live_cache

    if body.execution_target == "live" and not cache.is_populated:
        return error_response(
            code="NO_DATA",
            message="Market data not yet available for live scanning.",
        )

    try:
        records, meta = execute_query(request=body, cache=cache)

        return success_response(
            data=records,
            market_status=get_market_status(),
        ) | {"meta": meta}

    except ValueError as e:
        import traceback
        traceback.print_exc()
        return error_response(
            code="QUERY_VALIDATION_ERROR",
            message=str(e),
        )
    except Exception as e:
        return error_response(
            code="SCANNER_ERROR",
            message=f"Scanner evaluation failed: {str(e)}",
        )


@router.post("/scanner")
async def run_scanner(request: Request, body: ScannerRequest):
    """
    Legacy scanner endpoint — internally routes through the unified query engine.

    This preserves backward compatibility for existing MoM scanner calls.
    """
    cache = request.app.state.live_cache

    if body.mode == "live" and not cache.is_populated:
        return error_response(
            code="NO_DATA",
            message="Market data not yet available for live scanning.",
        )

    try:
        # Adapt old format → UnifiedQueryRequest
        unified = UnifiedQueryRequest(
            conditions=[
                QueryCondition(**c.model_dump())
                for c in body.conditions
            ],
            execution_target=body.mode,
            date=body.date,
            start_time=body.start_time,
            end_time=body.end_time,
            sort_by=body.sort_by,
            sort_order=body.sort_order,
            page=body.page,
            page_size=body.page_size,
        )

        records, meta = execute_query(request=unified, cache=cache)

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
