"""
Column Service — Dynamic column metadata introspection.

This is the engine behind the "no hardcoded columns" philosophy.
It inspects the live DataFrame's dtypes and column names, then generates
structured metadata the frontend uses to build:
- Column selectors
- Filter panels
- Indicator cards
- Scanner condition builders

Column names are auto-classified into groups (Price, Volume, Indicator, etc.)
using pattern matching. Display names and descriptions can be overridden
via column_overrides.json.
"""

import json
import logging
import os
from typing import Optional

import pandas as pd

from app.cache.live_cache import LiveCache
from app.schemas.metadata import ColumnMetadata

logger = logging.getLogger(__name__)

# ── Default Visible Columns ─────────────────────────────────────────────
# These columns are visible by default in the table.
# All other columns are available but hidden until the user enables them.
DEFAULT_VISIBLE = {
    "Instrument",
    "trading_symbol",
    "Last Price",
    "Net Change",
    "day_change_pct",
    "Volume",
    "Open",
    "High",
    "Low",
    "Close",
    "Average Price",
    "Total Buy Quantity",
    "Total Sell Quantity",
}

# ── Columns to exclude from metadata (internal/redundant) ──────────────
EXCLUDED_COLUMNS = {
    "instrument_key",  # Internal ID, same as Instrument
    "Prev_Day_Close",  # Redundant with Close (both = previous day close)
    "Symbol",          # Raw API key, trading_symbol is the clean display name
}

# ── Group Inference Patterns ────────────────────────────────────────────
# Order matters: first match wins.
GROUP_PATTERNS = [
    ({"Open", "High", "Low", "Close", "Last Price", "Average Price"}, "Price"),
    ({"Net Change", "day_change_pct"}, "Performance"),
    ({"Volume", "Total Buy Quantity", "Total Sell Quantity"}, "Volume"),
    ({"Lower Circuit Limit", "Upper Circuit Limit"}, "Limits"),
    ({"Open Interest", "OI Day High", "OI Day Low"}, "Derivatives"),
    ({"Last Trade Time", "Fetch Timestamp"}, "Metadata"),
    ({"Instrument", "trading_symbol", "exchange", "instrument_key"}, "Identity"),
]

GROUP_NAME_PATTERNS = {
    "price": "Price",
    "gain": "Performance",
    "change": "Performance",
    "return": "Performance",
    "pct": "Performance",
    "movement": "Performance",
    "52w": "Performance",
    "week_52": "Performance",
    "volume": "Volume",
    "quantity": "Volume",
    "qty": "Volume",
    "dlv": "Volume",
    "vol_": "Volume",
    "tbq": "Volume",
    "tsq": "Volume",
    "calculated": "Indicator",
    "signal": "Indicator",
    "sentiment": "Indicator",
    "score": "Indicator",
    "momentum": "Indicator",
    "vwap": "Indicator",
    "delta": "Indicator",
    "mom": "Indicator",
    "premarket": "Pre-Market",
    "prev_daily": "Historical",
    "prev_premarket": "Historical",
    "prev_day": "Historical",
    "oi": "Derivatives",
    "interest": "Derivatives",
    "circuit": "Limits",
    "limit": "Limits",
    "time": "Metadata",
    "timestamp": "Metadata",
    "date": "Metadata",
    "hour": "Metadata",
    "minute": "Metadata",
    "second": "Metadata",
    "symbol": "Identity",
    "instrument": "Identity",
    "exchange": "Identity",
    "company": "Identity",
    "sector": "Identity",
    "industry": "Identity",
    "name": "Identity",
}

# ── Unit Inference ──────────────────────────────────────────────────────
UNIT_PATTERNS = {
    "price": "₹",
    "open": "₹",
    "high": "₹",
    "low": "₹",
    "close": "₹",
    "last price": "₹",
    "average price": "₹",
    "net change": "₹",
    "circuit": "₹",
    "limit": "₹",
    "vwap": "₹",
    "delta_average_price": "₹",
    "traded_value": "₹ Cr",
    "pct": "%",
    "percent": "%",
    "gain": "%",
    "change_pct": "%",
    "return": "%",
    "movement_from": "%",
}

# ── Operator Sets by Type ───────────────────────────────────────────────
NUMERIC_OPERATORS = [">", "<", ">=", "<=", "=", "!=", "between"]
STRING_OPERATORS = ["=", "!=", "contains", "starts_with", "ends_with", "in", "not_in"]
DATETIME_OPERATORS = ["=", "!=", ">", "<", "between"]
BOOLEAN_OPERATORS = ["=", "!="]


def _load_overrides() -> dict:
    """Load column overrides from JSON file if it exists."""
    overrides_path = os.path.join(
        os.path.dirname(__file__), "..", "config", "column_overrides.json"
    )
    try:
        if os.path.exists(overrides_path):
            with open(overrides_path, "r") as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"Could not load column overrides: {e}")
    return {}


def _infer_group(column_name: str) -> str:
    """Infer the column group from the column name."""
    # Check exact match first
    for col_set, group in GROUP_PATTERNS:
        if column_name in col_set:
            return group

    # Check name pattern match
    name_lower = column_name.lower()
    for pattern, group in GROUP_NAME_PATTERNS.items():
        if pattern in name_lower:
            return group

    return "Other"


def _infer_unit(column_name: str) -> str:
    """Infer the display unit from the column name."""
    name_lower = column_name.lower()
    for pattern, unit in UNIT_PATTERNS.items():
        if pattern in name_lower:
            return unit
    return ""


def _infer_display_name(column_name: str) -> str:
    """Generate a human-readable display name from column name."""
    # Common transformations
    name = column_name.replace("_", " ").replace("-", " ")
    # Title case but preserve known acronyms
    words = name.split()
    result = []
    for word in words:
        if word.upper() in {"OI", "VWAP", "ID", "EQ", "BSE", "NSE"}:
            result.append(word.upper())
        elif word.lower() in {"pct"}:
            result.append("%")
        else:
            result.append(word.capitalize())
    return " ".join(result)


def _infer_filter_type(column_name: str, dtype, unique_count: int) -> str:
    """Infer the best filter UI type for a column."""
    if pd.api.types.is_bool_dtype(dtype):
        return "checkbox"
    if pd.api.types.is_datetime64_any_dtype(dtype):
        return "date_range"
    if pd.api.types.is_numeric_dtype(dtype):
        return "range"
    # String columns: use select if few unique values, search otherwise
    if pd.api.types.is_string_dtype(dtype) or pd.api.types.is_object_dtype(dtype):
        if unique_count <= 50:
            return "select"
        return "search"
    return "text"


def get_column_metadata(cache: LiveCache) -> list[ColumnMetadata]:
    """
    Generate dynamic column metadata by introspecting the current Live DataFrame.
    """
    df = cache.get_snapshot()
    return generate_metadata_from_df(df)


def generate_metadata_from_df(df: Optional[pd.DataFrame]) -> list[ColumnMetadata]:
    """
    Generate dynamic column metadata by introspecting a DataFrame.
    
    This is the core function that makes the entire frontend dynamic.
    Called by GET /api/v1/metadata.
    """
    if df is None or df.empty:
        return []

    overrides = _load_overrides()
    metadata = []

    for col in df.columns:
        if col in EXCLUDED_COLUMNS:
            continue

        series = df[col]
        dtype = series.dtype
        unique_count = series.nunique()
        override = overrides.get(col, {})

        # Determine type string
        if pd.api.types.is_bool_dtype(dtype):
            type_str = "boolean"
            operators = BOOLEAN_OPERATORS
        elif pd.api.types.is_datetime64_any_dtype(dtype):
            type_str = "datetime"
            operators = DATETIME_OPERATORS
        elif pd.api.types.is_numeric_dtype(dtype):
            type_str = "number"
            operators = NUMERIC_OPERATORS
        else:
            type_str = "string"
            operators = STRING_OPERATORS

        # Check if column contains Timestamp objects (object dtype but datetime values)
        if dtype == object:
            sample = series.dropna().head(5)
            if len(sample) > 0 and all(isinstance(v, pd.Timestamp) for v in sample):
                type_str = "datetime"
                operators = DATETIME_OPERATORS

        meta = ColumnMetadata(
            column=col,
            display_name=override.get("display_name", _infer_display_name(col)),
            type=type_str,
            unit=override.get("unit", _infer_unit(col)),
            description=override.get("description", ""),
            group=override.get("group", _infer_group(col)),
            sortable=type_str in ("number", "string", "datetime"),
            filterable=type_str in ("number", "string", "boolean"),
            filter_type=_infer_filter_type(col, dtype, unique_count),
            visible_default=col in DEFAULT_VISIBLE,
            operators=operators,
        )
        metadata.append(meta)

    return metadata


def get_column_groups(metadata: list[ColumnMetadata]) -> list[str]:
    """Extract ordered unique group names from metadata."""
    # Preserve a meaningful order
    preferred_order = [
        "Identity", "Price", "Performance", "Volume",
        "Indicator", "Pre-Market", "Historical",
        "Derivatives", "Limits", "Metadata", "Other",
    ]
    found_groups = {m.group for m in metadata}
    ordered = [g for g in preferred_order if g in found_groups]
    # Add any groups not in preferred order
    ordered.extend(sorted(found_groups - set(ordered)))
    return ordered
