"""
Query Validators — Server-side validation of conditions before execution.

Validates:
    - Column existence against available DataFrame columns
    - Operator compatibility with column dtype
    - Value type correctness
    - Required fields present

Returns structured ValidationError list (non-throwing).
"""

import logging
from typing import Any

from app.schemas.query import QueryValidationError

logger = logging.getLogger(__name__)

# Operators that require a numeric value
NUMERIC_OPERATORS = {">", "<", ">=", "<=", "between"}

# Operators that require a string value
STRING_OPERATORS = {"contains", "starts_with", "ends_with"}

# Operators that require a list value
LIST_OPERATORS = {"in", "not_in"}

# Operators that require no value
NO_VALUE_OPERATORS = {"is_null", "is_not_null"}

# All supported operators
ALL_OPERATORS = (
    NUMERIC_OPERATORS
    | STRING_OPERATORS
    | LIST_OPERATORS
    | NO_VALUE_OPERATORS
    | {"=", "==", "!="}
)


def validate_conditions(
    conditions: list[dict],
    available_columns: set[str],
) -> list[QueryValidationError]:
    """
    Validate a list of conditions against known columns.

    Args:
        conditions: List of condition dicts
        available_columns: Set of valid column names from the DataFrame

    Returns:
        List of validation errors (empty = all valid)
    """
    errors: list[QueryValidationError] = []

    if not conditions:
        errors.append(QueryValidationError(
            field="conditions",
            code="EMPTY_CONDITIONS",
            message="No conditions provided",
            suggestion="Add at least one condition",
        ))
        return errors

    for i, cond in enumerate(conditions):
        if cond.get("type") == "expression" and "ast" in cond:
            # Recursively extract identifiers from the AST
            def _extract_identifiers(node):
                idents = []
                if not isinstance(node, dict):
                    return idents
                if node.get("type") == "Identifier":
                    idents.append(node.get("name"))
                elif node.get("type") == "CallExpression":
                    for arg in node.get("arguments", []):
                        idents.extend(_extract_identifiers(arg))
                    return idents
                
                for key, val in node.items():
                    if isinstance(val, dict):
                        idents.extend(_extract_identifiers(val))
                    elif isinstance(val, list):
                        for item in val:
                            idents.extend(_extract_identifiers(item))
                return idents
            
            for ident in _extract_identifiers(cond["ast"]):
                if ident and ident not in available_columns:
                    suggestion = _find_closest(ident, available_columns)
                    errors.append(QueryValidationError(
                        field=f"conditions[{i}].ast",
                        code="UNKNOWN_COLUMN",
                        message=f"Unknown column: '{ident}'",
                        suggestion=f"Did you mean '{suggestion}'?" if suggestion else None,
                    ))
            continue
            
        column = cond.get("column", "")
        operator = cond.get("operator", "")
        value = cond.get("value")
        logical = cond.get("logical", "AND")

        # ── Column existence ────────────────────────────────────────
        if not column:
            errors.append(QueryValidationError(
                field=f"conditions[{i}].column",
                code="MISSING_COLUMN",
                message=f"Condition {i + 1}: column name is empty",
            ))
            continue

        if column not in available_columns:
            suggestion = _find_closest(column, available_columns)
            errors.append(QueryValidationError(
                field=f"conditions[{i}].column",
                code="UNKNOWN_COLUMN",
                message=f"Unknown column: '{column}'",
                suggestion=f"Did you mean '{suggestion}'?" if suggestion else None,
            ))

        # ── Operator validity ───────────────────────────────────────
        op_lower = operator.lower().strip()
        if op_lower not in ALL_OPERATORS:
            errors.append(QueryValidationError(
                field=f"conditions[{i}].operator",
                code="INVALID_OPERATOR",
                message=f"Unsupported operator: '{operator}'",
                suggestion=f"Valid operators: {', '.join(sorted(ALL_OPERATORS))}",
            ))

        # ── Value presence ──────────────────────────────────────────
        if op_lower not in NO_VALUE_OPERATORS:
            if value is None or (isinstance(value, str) and not value.strip()):
                errors.append(QueryValidationError(
                    field=f"conditions[{i}].value",
                    code="MISSING_VALUE",
                    message=f"Condition {i + 1}: value is required for operator '{operator}'",
                ))

        # ── Between requires list of 2 ──────────────────────────────
        if op_lower == "between":
            if not isinstance(value, list) or len(value) != 2:
                errors.append(QueryValidationError(
                    field=f"conditions[{i}].value",
                    code="INVALID_BETWEEN",
                    message=f"Condition {i + 1}: BETWEEN requires [low, high]",
                    suggestion="Provide value as a list of two numbers",
                ))

        # ── Logical operator ────────────────────────────────────────
        if logical.upper() not in ("AND", "OR"):
            errors.append(QueryValidationError(
                field=f"conditions[{i}].logical",
                code="INVALID_LOGICAL",
                message=f"Invalid logical operator: '{logical}'",
                suggestion="Use 'AND' or 'OR'",
            ))

    return errors


def _find_closest(name: str, candidates: set[str]) -> str | None:
    """Find the closest matching column name using simple substring + Levenshtein."""
    lower = name.lower()

    # Try substring match first
    for c in candidates:
        if lower in c.lower() or c.lower() in lower:
            return c

    # Levenshtein fallback
    best: str | None = None
    best_dist = float("inf")

    for c in candidates:
        dist = _levenshtein(lower, c.lower())
        if dist < best_dist and dist <= max(3, len(name) // 2):
            best_dist = dist
            best = c

    return best


def _levenshtein(a: str, b: str) -> int:
    """Compute Levenshtein edit distance between two strings."""
    if len(a) == 0:
        return len(b)
    if len(b) == 0:
        return len(a)

    matrix = [[0] * (len(a) + 1) for _ in range(len(b) + 1)]

    for i in range(len(b) + 1):
        matrix[i][0] = i
    for j in range(len(a) + 1):
        matrix[0][j] = j

    for i in range(1, len(b) + 1):
        for j in range(1, len(a) + 1):
            cost = 0 if a[j - 1] == b[i - 1] else 1
            matrix[i][j] = min(
                matrix[i - 1][j] + 1,
                matrix[i][j - 1] + 1,
                matrix[i - 1][j - 1] + cost,
            )

    return matrix[len(b)][len(a)]
