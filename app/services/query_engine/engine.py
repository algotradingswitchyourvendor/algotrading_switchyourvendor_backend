"""
Query Engine — Core orchestrator for all scanner operations.

This is the SINGLE execution entry point. Every scanner operation
(MoM conditions, Create Screener text queries, future alerts)
flows through execute_query().

Pipeline:
    1. Resolve conditions (from conditions array OR parsed query_text)
    2. Validate conditions against available columns
    3. Select data source adapter (Live or History)
    4. Load DataFrame via adapter
    5. Apply conditions via Pandas translator
    6. Sort results
    7. Paginate and enforce limits
    8. Build result metadata
    9. Return (records, meta)
"""

import logging
import math
import time
from typing import Optional, Any

import pandas as pd

from app.cache.live_cache import LiveCache
from app.schemas.query import (
    QueryResultMeta,
    QueryValidationError,
    UnifiedQueryRequest,
)
from app.services.query_engine.adapters import HistoryAdapter, LiveAdapter
from app.services.query_engine.parser import ParseError, parse_query_text
from app.services.query_engine.translator import apply_conditions
from app.services.query_engine.validators import validate_conditions

logger = logging.getLogger(__name__)

# Hard limit — never return more than this
MAX_RESULT_ROWS = 5000


def execute_query(
    request: UnifiedQueryRequest,
    cache: LiveCache,
) -> tuple[list[dict], dict]:
    """
    Execute a unified query and return results.

    Args:
        request: The unified query request
        cache: LiveCache instance (for live queries)

    Returns:
        Tuple of (list of record dicts, meta dict)

    Raises:
        ValueError: If the query is invalid and cannot be executed
    """
    start_time = time.perf_counter()

    # ── Step 1: Resolve conditions ──────────────────────────────────
    conditions = resolve_conditions(request)

    # ── Step 2: Load DataFrame via adapter ──────────────────────────
    adapter_result = _load_dataframe(request, cache)
    
    # Check if we got an AdapterResult or a raw DataFrame
    if hasattr(adapter_result, "is_pre_processed"):
        df = adapter_result.df
        is_pre_processed = adapter_result.is_pre_processed
        matched_count = adapter_result.matched_count
        total_scanned = adapter_result.total_scanned or len(df)
    else:
        df = adapter_result
        is_pre_processed = False
        matched_count = None
        total_scanned = len(df)

    if df.empty and not is_pre_processed:
        return [], _build_meta(
            total=0,
            total_scanned=0,
            matched=0,
            returned=0,
            request=request,
            conditions_count=len(conditions),
            start_time=start_time,
        )

    # ── Step 3: Validate conditions ─────────────────────────────────
    available_columns = set(df.columns)
    validation_errors = validate_conditions(conditions, available_columns)

    # Filter out fatal errors (unknown columns, etc.) but continue with warnings
    fatal_errors = [e for e in validation_errors if e.code in ("UNKNOWN_COLUMN", "EMPTY_CONDITIONS")]
    if fatal_errors:
        meta = _build_meta(
            total=0,
            total_scanned=total_scanned,
            matched=0,
            returned=0,
            request=request,
            conditions_count=len(conditions),
            start_time=start_time,
            validation_errors=validation_errors,
        )
        return [], meta

    # ── Step 4: Apply conditions ────────────────────────────────────
    if is_pre_processed:
        filtered = df
        matched = matched_count if matched_count is not None else len(filtered)
    else:
        filtered = apply_conditions(df, conditions)
        matched = len(filtered)

    if filtered.empty and not is_pre_processed:
        return [], _build_meta(
            total=0,
            total_scanned=total_scanned,
            matched=0,
            returned=0,
            request=request,
            conditions_count=len(conditions),
            start_time=start_time,
            validation_errors=validation_errors,
        )

    # ── Step 5: Sort ────────────────────────────────────────────────
    if not is_pre_processed and request.sort_by and request.sort_by in filtered.columns:
        try:
            ascending = request.sort_order == "asc"
            filtered = filtered.sort_values(
                by=request.sort_by,
                ascending=ascending,
                na_position="last",
            )
        except Exception as e:
            logger.warning(f"Sort failed on '{request.sort_by}': {e}")

    # ── Step 6: Paginate ────────────────────────────────────────────
    page_size = min(request.page_size, MAX_RESULT_ROWS)
    total_pages = math.ceil(matched / page_size) if matched > 0 else 0
    
    if is_pre_processed:
        paginated = filtered
    else:
        start_idx = (request.page - 1) * page_size
        end_idx = start_idx + page_size
        paginated = filtered.iloc[start_idx:end_idx]
        
    truncated = matched > MAX_RESULT_ROWS

    # ── Step 7: Convert to records ──────────────────────────────────
    logger.info(f"DataFrame shape before serialization: {paginated.shape}")
    t_json = time.perf_counter()
    records = _df_to_records(paginated)
    t_json_time = (time.perf_counter() - t_json) * 1000

    meta = _build_meta(
        total=matched,
        total_scanned=total_scanned,
        matched=matched,
        returned=len(records),
        request=request,
        conditions_count=len(conditions),
        start_time=start_time,
        truncated=truncated,
        total_pages=total_pages,
        validation_errors=validation_errors,
    )
    
    if is_pre_processed and hasattr(adapter_result, "bullish_count"):
        meta["bullish_count"] = adapter_result.bullish_count
        meta["bearish_count"] = adapter_result.bearish_count
    
    total_time = (time.perf_counter() - start_time) * 1000
    
    # Log detailed timings for Historical queries
    if is_pre_processed and hasattr(adapter_result, "timings") and adapter_result.timings:
        adapter_result.timings["JSON Serialization"] = t_json_time
        adapter_result.timings["Total Query Time"] = total_time
        meta["timings"] = adapter_result.timings
        timings_str = " | ".join(f"{k}: {v:.2f}ms" for k, v in adapter_result.timings.items())
        logger.info(f"[Historical Scanner] {timings_str}")

    return records, meta


# ── Private Helpers ──────────────────────────────────────────────────────


def resolve_conditions(request: UnifiedQueryRequest) -> list[dict]:
    """Convert request into a flat list of condition dicts."""
    # Text query takes priority if provided
    if request.query_text and request.query_text.strip():
        try:
            return parse_query_text(request.query_text)
        except ParseError as e:
            raise ValueError(f"Query parse error: {e}")

    # Structured conditions
    if request.conditions:
        return [c.model_dump() for c in request.conditions]

    # Groups (flatten)
    if request.groups:
        conditions = []
        for group in request.groups:
            for c in group.conditions:
                conditions.append({
                    **c.model_dump(),
                    "logical": group.logical if not conditions else c.logical,
                })
        return conditions

    raise ValueError("No conditions or query_text provided")


def _load_dataframe(
    request: UnifiedQueryRequest,
    cache: LiveCache,
) -> Any:
    """Select the appropriate adapter and load data. Returns DataFrame or AdapterResult."""
    if request.execution_target == "history":
        adapter = HistoryAdapter()
        return adapter.get_dataframe(request=request)
    else:
        adapter = LiveAdapter()
        return adapter.get_dataframe(cache)


def _build_meta(
    total: int,
    total_scanned: int,
    matched: int,
    returned: int,
    request: UnifiedQueryRequest,
    conditions_count: int,
    start_time: float,
    truncated: bool = False,
    total_pages: int = 0,
    validation_errors: Optional[list[QueryValidationError]] = None,
) -> dict:
    """Build the result metadata dict."""
    execution_ms = round((time.perf_counter() - start_time) * 1000, 2)
    page_size = min(request.page_size, MAX_RESULT_ROWS)

    return {
        "total": total,
        "total_scanned": total_scanned,
        "matched_count": matched,
        "returned_count": returned,
        "truncated": truncated,
        "page": request.page,
        "page_size": page_size,
        "total_pages": total_pages,
        "conditions_applied": conditions_count,
        "execution_time_ms": execution_ms,
        "execution_target": request.execution_target,
        "validation_errors": [e.model_dump() for e in (validation_errors or [])],
    }


def _df_to_records(df: pd.DataFrame) -> list[dict]:
    """Convert DataFrame to JSON-serializable list of dicts."""
    import numpy as np
    
    if df.empty:
        return []
        
    df_copy = df.copy()
    
    # Precompute datetime columns for fast membership testing
    dt_cols = set(df_copy.select_dtypes(include=['datetime64', 'datetimetz', '<M8[ns]', 'datetime64[ns, UTC]', 'datetime64[ns]']).columns)
    
    records = df_copy.to_dict(orient="records")
    import math
    for r in records:
        for k, v in r.items():
            if type(v) is float and math.isnan(v):
                r[k] = None
            elif k in dt_cols:
                if v is None or v is pd.NaT:
                    r[k] = None
                else:
                    try:
                        r[k] = v.isoformat()
                    except AttributeError:
                        pass
                
    return records
