"""
Pandas Translator — Converts query conditions into vectorized Pandas masks.

This is the ONLY place where conditions become DataFrame operations.
Extracted and enhanced from the original scanner_service._apply_conditions().

Supports:
    Numeric:  >, <, >=, <=, =, !=, between
    String:   =, !=, contains, starts_with, ends_with
    Set:      in, not_in
    Null:     is_null, is_not_null
    Logic:    AND, OR (flat list with left-to-right evaluation)

Security: No eval(). All operations are explicit Pandas vectorized calls.
"""

import logging
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)


def apply_conditions(
    df: pd.DataFrame,
    conditions: list[dict],
) -> pd.DataFrame:
    """
    Apply a list of conditions to the DataFrame with AND/OR logic.

    Args:
        df: Source DataFrame
        conditions: List of condition dicts, each with:
            column, operator, value, logical

    Returns:
        Filtered DataFrame
    """
    if not conditions or df.empty:
        return df

    and_mask = pd.Series(True, index=df.index)
    or_masks: list[pd.Series] = []

    for condition in conditions:
        column = condition.get("column", "")
        operator = condition.get("operator", "=")
        value = condition.get("value")
        logical = condition.get("logical", "AND").upper()

        if column not in df.columns:
            logger.warning(f"Translator: column '{column}' not found, skipping")
            continue

        mask = _evaluate_single(df, column, operator, value)

        if logical == "OR":
            or_masks.append(mask)
        else:
            and_mask = and_mask & mask

    # Combine AND and OR masks
    if or_masks:
        combined_or = pd.Series(False, index=df.index)
        for m in or_masks:
            combined_or = combined_or | m
        final_mask = and_mask & combined_or
    else:
        final_mask = and_mask

    return df[final_mask]


def _evaluate_single(
    df: pd.DataFrame,
    column: str,
    operator: str,
    value: Any,
) -> pd.Series:
    """
    Evaluate a single condition and return a boolean Series mask.

    Never raises — returns all-True mask on failure (graceful degradation).
    """
    try:
        series = df[column]
        op = operator.lower().strip()

        # ── Null checks ─────────────────────────────────────────────
        if op == "is_null":
            return series.isna()
        if op == "is_not_null":
            return series.notna()

        # ── Numeric operators ────────────────────────────────────────
        if op in (">", "<", ">=", "<=", "between"):
            numeric = pd.to_numeric(series, errors="coerce")

            if op == ">":
                return numeric > float(value)
            if op == "<":
                return numeric < float(value)
            if op == ">=":
                return numeric >= float(value)
            if op == "<=":
                return numeric <= float(value)
            if op == "between":
                if isinstance(value, list) and len(value) == 2:
                    return (numeric >= float(value[0])) & (numeric <= float(value[1]))
                return pd.Series(True, index=df.index)

        # ── Equality (auto-detect numeric vs string) ─────────────────
        if op in ("=", "=="):
            try:
                return pd.to_numeric(series, errors="raise") == float(value)
            except (ValueError, TypeError):
                return series.astype(str).str.lower() == str(value).lower()

        if op == "!=":
            try:
                return pd.to_numeric(series, errors="raise") != float(value)
            except (ValueError, TypeError):
                return series.astype(str).str.lower() != str(value).lower()

        # ── String operators ─────────────────────────────────────────
        if op == "contains":
            return series.astype(str).str.contains(str(value), case=False, na=False)

        if op == "starts_with":
            return series.astype(str).str.startswith(str(value), na=False)

        if op == "ends_with":
            return series.astype(str).str.endswith(str(value), na=False)

        # ── Set operators ────────────────────────────────────────────
        if op == "in":
            if isinstance(value, list):
                return series.isin(value)
            return pd.Series(True, index=df.index)

        if op == "not_in":
            if isinstance(value, list):
                return ~series.isin(value)
            return pd.Series(True, index=df.index)

        # ── Unknown operator — log and skip ──────────────────────────
        logger.warning(f"Translator: unknown operator '{operator}', skipping")

    except Exception as e:
        logger.warning(
            f"Translator: condition failed ({column} {operator} {value}): {e}"
        )

    # Default: include all rows (graceful degradation)
    return pd.Series(True, index=df.index)
