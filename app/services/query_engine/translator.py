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
import numpy as np
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
        if condition.get("type") == "expression" and "ast" in condition:
            try:
                mask = _evaluate_ast(df, condition["ast"])
                # Fallback in case of unexpected types returning Series
                if not isinstance(mask, pd.Series):
                    mask = pd.Series(mask, index=df.index)
                
                logical = condition.get("logical", "AND").upper()
                if logical == "OR":
                    or_masks.append(mask)
                else:
                    and_mask = and_mask & mask
            except Exception as e:
                logger.warning(f"Translator: AST evaluation failed: {e}")
                # Fail-safe: if evaluation crashes, default to False to prevent false-positives
                mask = pd.Series(False, index=df.index)
                logical = condition.get("logical", "AND").upper()
                if logical == "OR":
                    or_masks.append(mask)
                else:
                    and_mask = and_mask & mask
            continue

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


def _evaluate_ast(df: pd.DataFrame, ast: dict) -> pd.Series | Any:
    """
    Recursively evaluate an AST dict into a Pandas Series mask or value.
    Uses strict vectorized operations (no eval()).
    """
    node_type = ast.get("type")
    
    if node_type == "Identifier":
        name = ast["name"]
        if name not in df.columns:
            raise ValueError(f"Unknown column: {name}")
        # Return the strictly-typed Pandas series
        return df[name]
        
    elif node_type == "Literal":
        return ast["value"]
        
    elif node_type == "UnaryExpression":
        right = _evaluate_ast(df, ast["right"])
        op = ast["operator"]
        if op == "NOT": return ~right
        if op == "-": return -right
        if op == "+": return +right
        raise ValueError(f"Unsupported unary operator: {op}")
        
    elif node_type in ("BinaryExpression", "LogicalExpression"):
        left = _evaluate_ast(df, ast["left"])
        right = _evaluate_ast(df, ast["right"])
        op = ast["operator"]
        
        # Comparisons
        if op == ">": return left > right
        if op == "<": return left < right
        if op == ">=": return left >= right
        if op == "<=": return left <= right
        if op in ("=", "=="): return left == right
        if op == "!=": return left != right
        
        # Math
        if op == "+": return left + right
        if op == "-": return left - right
        if op == "*": return left * right
        if op == "/": return left / right
        if op == "%": return left % right
        
        # Logical
        if op == "AND": return left & right
        if op == "OR": return left | right
        
        raise ValueError(f"Unsupported operator: {op}")
        
    elif node_type == "CallExpression":
        callee = ast["callee"]["name"].upper()
        args = [_evaluate_ast(df, arg) for arg in ast.get("arguments", [])]
        
        if not args:
            raise ValueError(f"Function {callee} requires arguments")
            
        # Math functions via Numpy (perfectly vectorized over Pandas Series)
        if callee == "LOG":
            # DuckDB SQL translates LOG to Natural Log (LN). Handle <= 0 gracefully.
            if isinstance(args[0], pd.Series):
                return np.log(args[0].where(args[0] > 0))
            return np.log(args[0]) if args[0] > 0 else np.nan
            
        elif callee == "SQRT":
            # Handle < 0 gracefully
            if isinstance(args[0], pd.Series):
                return np.sqrt(args[0].where(args[0] >= 0))
            return np.sqrt(args[0]) if args[0] >= 0 else np.nan
            
        elif callee == "ABS": return np.abs(args[0])
        elif callee == "ROUND":
            if len(args) > 1: return np.round(args[0], decimals=args[1])
            return np.round(args[0])
        elif callee == "CEIL": return np.ceil(args[0])
        elif callee == "FLOOR": return np.floor(args[0])
        elif callee == "MIN":
            res = args[0]
            for a in args[1:]:
                res = np.minimum(res, a)
            return res
        elif callee == "MAX":
            res = args[0]
            for a in args[1:]:
                res = np.maximum(res, a)
            return res
        else:
            raise ValueError(f"Unsupported function: {callee}")
            
    raise ValueError(f"Unsupported AST node type: {node_type}")
