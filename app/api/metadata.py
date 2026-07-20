"""
Metadata API — Dynamic column metadata and column list.

GET /api/v1/metadata — Full column metadata for frontend dynamic rendering
GET /api/v1/columns — Simple column name list
"""

from fastapi import APIRouter, Request

from app.config.holidays import get_market_status
from app.schemas.response import success_response, error_response
from app.services.column_service import get_column_metadata, get_column_groups

router = APIRouter()


@router.get("/metadata")
async def get_metadata(request: Request):
    """
    Return dynamic column metadata.

    The frontend uses this to build:
    - Column selectors (which columns to show/hide)
    - Filter panels (which filter type per column)
    - Indicator cards (grouped by category)
    - Scanner condition builders (available columns + operators)

    This endpoint is the foundation of the "no hardcoded columns" architecture.
    Whenever the backend adds a new DataFrame column, this endpoint 
    automatically includes it in the response.
    """
    cache = request.app.state.live_cache

    if not cache.is_populated:
        return error_response(
            code="NO_DATA",
            message="Metadata not available yet. Waiting for first data fetch.",
        )

    metadata = get_column_metadata(cache)
    groups = get_column_groups(metadata)

    return success_response(
        data={
            "columns": [m.model_dump() for m in metadata],
            "groups": groups,
        },
        market_status=get_market_status(),
    )


@router.get("/columns")
async def get_columns(request: Request):
    """Return simple list of all available column names."""
    cache = request.app.state.live_cache
    columns = cache.get_columns()

    return success_response(
        data=columns,
        market_status=get_market_status(),
    )
