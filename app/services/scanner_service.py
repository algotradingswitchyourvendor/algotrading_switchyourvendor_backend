"""
Scanner Service — Server-side condition evaluation engine.

Evaluates user-defined conditions against the live snapshot or historical data.
The frontend NEVER evaluates conditions — it only renders results.

Supports:
- Numeric operators: >, <, >=, <=, =, !=, between
- String operators: =, !=, contains, starts_with, ends_with, in, not_in
- Boolean logic: AND, OR
- Multiple condition groups
"""

import json
import logging
import math
import os
from typing import Any, Optional

import pandas as pd

from app.cache.live_cache import LiveCache

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
    Evaluate scanner conditions and return matching stocks.

    Args:
        cache: LiveCache instance
        conditions: List of condition dicts, each with:
            - column: str
            - operator: str (>, <, >=, <=, =, !=, between, contains, etc.)
            - value: Any
            - logical: str ("AND" or "OR", default "AND")
        mode: "live" (use cache) or "historical" (use parquet)
        sort_by: Column to sort results by
        sort_order: "asc" or "desc"
        page: Page number
        page_size: Results per page
        historical_params: dict with date, start_time, end_time for historical mode

    Returns:
        Tuple of (matching_records, meta)
    """
    # Get source data
    if mode == "historical" and historical_params:
        from app.services.history_service import get_historical_data
        records, _ = get_historical_data(
            target_date=historical_params.get("date"),
            start_time=historical_params.get("start_time"),
            end_time=historical_params.get("end_time"),
            page=1,
            page_size=100000,  # Get all for scanning
        )
        if not records:
            return [], _empty_meta(page, page_size)
        df = pd.DataFrame(records)
    else:
        df = cache.get_snapshot()
        if df is None or df.empty:
            return [], _empty_meta(page, page_size)

    # Apply conditions
    df = _apply_conditions(df, conditions)

    if df.empty:
        return [], _empty_meta(page, page_size)

    # Sort results
    if sort_by and sort_by in df.columns:
        try:
            ascending = sort_order == "asc"
            df = df.sort_values(by=sort_by, ascending=ascending, na_position="last")
        except Exception as e:
            logger.warning(f"Sort failed on {sort_by}: {e}")

    # Paginate
    total = len(df)
    total_pages = math.ceil(total / page_size) if total > 0 else 0
    start = (page - 1) * page_size
    end = start + page_size
    paginated = df.iloc[start:end]

    meta = {
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "conditions_applied": len(conditions),
    }

    return _df_to_records(paginated), meta


def _apply_conditions(df: pd.DataFrame, conditions: list[dict]) -> pd.DataFrame:
    """
    Apply a list of conditions to the DataFrame with AND/OR logic.

    Default logic between conditions is AND.
    OR conditions create separate masks that are unioned.
    """
    if not conditions:
        return df

    and_mask = pd.Series([True] * len(df), index=df.index)
    or_masks = []

    for condition in conditions:
        column = condition.get("column", "")
        operator = condition.get("operator", "=")
        value = condition.get("value")
        logical = condition.get("logical", "AND").upper()

        if column not in df.columns:
            logger.warning(f"Scanner: column '{column}' not found, skipping")
            continue

        condition_mask = _evaluate_single_condition(df, column, operator, value)

        if logical == "OR":
            or_masks.append(condition_mask)
        else:
            and_mask = and_mask & condition_mask

    # Combine AND and OR masks
    if or_masks:
        combined_or = pd.Series([False] * len(df), index=df.index)
        for m in or_masks:
            combined_or = combined_or | m
        final_mask = and_mask & combined_or
    else:
        final_mask = and_mask

    return df[final_mask]


def _evaluate_single_condition(
    df: pd.DataFrame, column: str, operator: str, value: Any
) -> pd.Series:
    """Evaluate a single condition and return a boolean mask."""
    try:
        series = df[column]

        if operator in (">", "<", ">=", "<=", "between"):
            numeric_series = pd.to_numeric(series, errors="coerce")

            if operator == ">":
                return numeric_series > float(value)
            elif operator == "<":
                return numeric_series < float(value)
            elif operator == ">=":
                return numeric_series >= float(value)
            elif operator == "<=":
                return numeric_series <= float(value)
            elif operator == "between":
                if isinstance(value, list) and len(value) == 2:
                    return (numeric_series >= float(value[0])) & (
                        numeric_series <= float(value[1])
                    )

        elif operator == "=":
            # Try numeric comparison first, fall back to string
            try:
                return pd.to_numeric(series, errors="raise") == float(value)
            except (ValueError, TypeError):
                return series.astype(str) == str(value)

        elif operator == "!=":
            try:
                return pd.to_numeric(series, errors="raise") != float(value)
            except (ValueError, TypeError):
                return series.astype(str) != str(value)

        elif operator == "contains":
            return series.astype(str).str.contains(str(value), case=False, na=False)

        elif operator == "starts_with":
            return series.astype(str).str.startswith(str(value), na=False)

        elif operator == "ends_with":
            return series.astype(str).str.endswith(str(value), na=False)

        elif operator == "in":
            if isinstance(value, list):
                return series.isin(value)

        elif operator == "not_in":
            if isinstance(value, list):
                return ~series.isin(value)

    except Exception as e:
        logger.warning(f"Condition evaluation failed ({column} {operator} {value}): {e}")

    # Default: include all rows (condition failed gracefully)
    return pd.Series([True] * len(df), index=df.index)


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


def _empty_meta(page: int, page_size: int) -> dict:
    return {
        "total": 0,
        "page": page,
        "page_size": page_size,
        "total_pages": 0,
        "conditions_applied": 0,
    }


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
