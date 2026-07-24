"""
Scanner Service — Legacy condition evaluation engine.

Most logic has moved to the unified query_engine package.
This file now contains only:
1. evaluate_scanner() — A backward-compatibility wrapper around execute_query()
2. get_scanner_presets() — Loads saved presets
"""

import json
import logging
import os
from typing import Optional

from app.cache.live_cache import LiveCache
from app.schemas.query import QueryCondition, UnifiedQueryRequest
from app.services.query_engine import execute_query

logger = logging.getLogger(__name__)


def evaluate_scanner(
    cache: LiveCache,
    conditions: list[dict],
    mode: str = "live",
    sort_by: Optional[str] = None,
    sort_order: str = "desc",
    page: int = 1,
    page_size: int = 50,
    historical_params: Optional[dict] = None,
) -> tuple[list[dict], dict]:
    """
    Evaluate scanner conditions (Legacy Wrapper).

    This delegates to the new query_engine for actual execution.
    Preserved so that existing websocket connections don't break.
    """
    try:
        req = UnifiedQueryRequest(
            conditions=[QueryCondition(**c) for c in conditions],
            execution_target=mode,
            date=historical_params.get("date") if historical_params else None,
            start_time=historical_params.get("start_time") if historical_params else None,
            end_time=historical_params.get("end_time") if historical_params else None,
            sort_by=sort_by,
            sort_order=sort_order,
            page=page,
            page_size=page_size,
        )

        records, meta = execute_query(request=req, cache=cache)
        return records, meta
    except Exception as e:
        logger.error(f"Legacy evaluate_scanner failed: {e}")
        return [], _empty_meta(page, page_size)


def get_scanner_presets() -> list[dict]:
    """Load predefined scanner presets from JSON config."""
    presets_path = os.path.join(
        os.path.dirname(__file__), "..", "config", "scanner_presets.json"
    )
    try:
        if os.path.exists(presets_path):
            with open(presets_path, "r") as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"Could not load scanner presets: {e}")
    return []


def _empty_meta(page: int, page_size: int, total_scanned: int = 0) -> dict:
    return {
        "total": 0,
        "total_scanned": total_scanned,
        "matched_count": 0,
        "returned_count": 0,
        "truncated": False,
        "page": page,
        "page_size": page_size,
        "total_pages": 0,
        "conditions_applied": 0,
        "execution_time_ms": 0.0,
        "execution_target": "live",
        "validation_errors": [],
    }
