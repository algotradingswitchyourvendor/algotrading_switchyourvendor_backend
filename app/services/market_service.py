"""
Market Service — Business logic for live market data queries.

All operations work on the in-memory LiveCache DataFrame.
Supports pagination, sorting, filtering, and search.

Design decision: Filtering is done via pandas operations on the in-memory 
DataFrame (~6,800 rows). This is fast enough (< 5ms) that we don't need 
a database. If the dataset grows to 50K+ instruments, consider SQLite/Postgres.
"""

import json
import logging
import math
from typing import Any, Optional

import pandas as pd

from app.cache.live_cache import LiveCache
from app.schemas.stock import SortOrder

logger = logging.getLogger(__name__)


def get_dashboard_data(cache: LiveCache) -> dict:
    """
    Return full dashboard data including snapshot metadata.
    Used for initial page load — subsequent updates come via WebSocket.
    """
    return {
        "stocks": cache.get_snapshot_records(),
        "info": cache.get_snapshot_info(),
    }


def get_stocks_paginated(
    cache: LiveCache,
    page: int = 1,
    page_size: int = 50,
    sort_by: Optional[str] = None,
    sort_order: SortOrder = SortOrder.ASC,
    search: Optional[str] = None,
    filters: Optional[str] = None,
    columns: Optional[str] = None,
) -> tuple[list[dict], dict]:
    """
    Return a paginated, sorted, filtered, and optionally column-selected 
    subset of the current market snapshot.

    Args:
        cache: LiveCache instance
        page: 1-indexed page number
        page_size: Number of rows per page
        sort_by: Column name to sort by
        sort_order: asc or desc
        search: Search query (partial match on symbol/name)
        filters: JSON string of filter conditions
        columns: Comma-separated column names to include

    Returns:
        Tuple of (records, pagination_meta)
    """
    df = cache.get_snapshot()
    if df is None or df.empty:
        return [], {"total": 0, "page": page, "page_size": page_size, "total_pages": 0}

    # ── Search ──────────────────────────────────────────────────────
    if search:
        search_lower = search.lower()
        mask = df["Instrument"].str.lower().str.contains(search_lower, na=False)
        if "trading_symbol" in df.columns:
            mask = mask | df["trading_symbol"].str.lower().str.contains(search_lower, na=False)
        if "company_name" in df.columns:
            mask = mask | df["company_name"].str.lower().str.contains(search_lower, na=False)
        df = df[mask]

    # ── Filters ─────────────────────────────────────────────────────
    if filters:
        df = _apply_filters(df, filters)

    # ── Sort ────────────────────────────────────────────────────────
    if sort_by and sort_by in df.columns:
        ascending = sort_order == SortOrder.ASC
        # Handle mixed types gracefully
        try:
            df = df.sort_values(
                by=sort_by,
                ascending=ascending,
                na_position="last",
            )
        except TypeError:
            logger.warning(f"Could not sort by {sort_by}, mixed types")

    # ── Column Selection ────────────────────────────────────────────
    if columns:
        requested_cols = [c.strip() for c in columns.split(",")]
        # Always include Instrument as identifier
        if "Instrument" not in requested_cols:
            requested_cols.insert(0, "Instrument")
        valid_cols = [c for c in requested_cols if c in df.columns]
        df = df[valid_cols]

    # ── Pagination ──────────────────────────────────────────────────
    total = len(df)
    total_pages = math.ceil(total / page_size) if total > 0 else 0
    start = (page - 1) * page_size
    end = start + page_size

    paginated_df = df.iloc[start:end]

    meta = {
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
    }

    return _df_to_records(paginated_df), meta


def get_stock_detail(cache: LiveCache, symbol: str) -> Optional[dict]:
    """Return a single stock's data by symbol."""
    return cache.get_stock(symbol)


def search_stocks(cache: LiveCache, query: str, limit: int = 20) -> list[dict]:
    """Search stocks by partial match."""
    return cache.search(query, limit=limit)


# ── Private Helpers ─────────────────────────────────────────────────────


def _apply_filters(df: pd.DataFrame, filters_json: str) -> pd.DataFrame:
    """
    Apply dynamic filters to the DataFrame.

    Filter format (JSON string):
    {
        "Volume": {"gte": 100000},
        "day_change_pct": {"gte": -2, "lte": 5},
        "exchange": {"eq": "NSE"},
        "trading_symbol": {"contains": "BANK"}
    }

    Supported operators: gt, gte, lt, lte, eq, ne, contains, in, not_in, between
    """
    try:
        filter_dict = json.loads(filters_json) if isinstance(filters_json, str) else filters_json
    except (json.JSONDecodeError, TypeError):
        logger.warning(f"Invalid filter JSON: {filters_json}")
        return df

    for column, conditions in filter_dict.items():
        if column not in df.columns:
            continue

        if not isinstance(conditions, dict):
            continue

        for operator, value in conditions.items():
            try:
                if operator == "gt":
                    df = df[pd.to_numeric(df[column], errors="coerce") > float(value)]
                elif operator == "gte":
                    df = df[pd.to_numeric(df[column], errors="coerce") >= float(value)]
                elif operator == "lt":
                    df = df[pd.to_numeric(df[column], errors="coerce") < float(value)]
                elif operator == "lte":
                    df = df[pd.to_numeric(df[column], errors="coerce") <= float(value)]
                elif operator == "eq":
                    df = df[df[column] == value]
                elif operator == "ne":
                    df = df[df[column] != value]
                elif operator == "contains":
                    df = df[df[column].astype(str).str.contains(str(value), case=False, na=False)]
                elif operator == "in":
                    if isinstance(value, list):
                        df = df[df[column].isin(value)]
                elif operator == "not_in":
                    if isinstance(value, list):
                        df = df[~df[column].isin(value)]
                elif operator == "between":
                    if isinstance(value, list) and len(value) == 2:
                        col_numeric = pd.to_numeric(df[column], errors="coerce")
                        df = df[(col_numeric >= float(value[0])) & (col_numeric <= float(value[1]))]
            except (ValueError, TypeError) as e:
                logger.warning(f"Filter error on {column}.{operator}: {e}")

    return df


def _df_to_records(df: pd.DataFrame) -> list[dict]:
    """Convert DataFrame to JSON-serializable list of dicts."""
    records = df.to_dict(orient="records")
    for record in records:
        for key, value in record.items():
            if isinstance(value, pd.Timestamp):
                record[key] = value.isoformat()
            elif pd.isna(value):
                record[key] = None
    return records
