"""
Unified Query Schemas — Common query model for all scanner operations.

This replaces the fragmented query models. Both MoM condition arrays
and Create Screener text queries serialize into this model.

Consumers:
    POST /api/v1/scanner/query   (new unified endpoint)
    POST /api/v1/scanner         (old endpoint, internally adapted)
    Future: Saved Screeners, Alerts, REST API, AI-generated queries
"""

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator


class QueryCondition(BaseModel):
    """A single filter condition."""
    column: str = Field(..., description="DataFrame column name")
    operator: str = Field(..., description="Comparison operator")
    value: Any = Field(None, description="Value to compare against (None for IS_NULL/IS_NOT_NULL)")
    logical: str = Field("AND", description="Logical connector: AND | OR")


class QueryGroup(BaseModel):
    """Parenthesized group of conditions (for nested logic)."""
    conditions: list[QueryCondition] = Field(default_factory=list)
    logical: str = Field("AND", description="How this group connects to the previous group")


class UnifiedQueryRequest(BaseModel):
    """
    Single request model for all scanner operations.

    Supply ONE of:
        conditions  — Structured condition array (from MoM condition rows)
        query_text  — Free-text query string (from Create Screener)

    Both are translated into the same internal representation
    before execution.
    """
    # ── Query (provide conditions OR query_text, not both) ──────────
    conditions: Optional[list[QueryCondition]] = Field(
        None, description="Structured condition array"
    )
    groups: Optional[list[QueryGroup]] = Field(
        None, description="Grouped conditions with nested logic"
    )
    query_text: Optional[str] = Field(
        None, description="Free-text query from Create Screener"
    )

    # ── Execution ───────────────────────────────────────────────────
    execution_target: Literal["live", "history"] = Field(
        "live", description="Data source: live (LiveCache) or history (Parquet)"
    )

    # ── Historical params (only when execution_target == 'history') ──
    date: Optional[str] = Field(None, description="Target date for historical scan")
    start_time: Optional[str] = Field(None, description="Start time filter")
    end_time: Optional[str] = Field(None, description="End time filter")

    # ── Sorting ─────────────────────────────────────────────────────
    sort_by: Optional[str] = Field(None, description="Column to sort by")
    sort_order: Literal["asc", "desc"] = Field("desc", description="Sort direction")

    # ── Pagination ──────────────────────────────────────────────────
    page: int = Field(1, ge=1, description="Page number")
    page_size: int = Field(50, ge=1, le=5000, description="Results per page")

    # ── Future: saved query metadata ────────────────────────────────
    query_name: Optional[str] = Field(None, description="Name for saved queries")
    query_id: Optional[str] = Field(None, description="ID for saved queries")

    @model_validator(mode="after")
    def check_query_source(self):
        has_conditions = bool(self.conditions) or bool(self.groups)
        has_text = bool(self.query_text and self.query_text.strip())
        if not has_conditions and not has_text:
            raise ValueError("Provide either 'conditions' or 'query_text'")
        return self


class QueryValidationError(BaseModel):
    """Structured validation error returned to the client."""
    field: str = Field(..., description="Column or expression that failed")
    code: str = Field(..., description="Machine-readable error code")
    message: str = Field(..., description="Human-readable message")
    suggestion: Optional[str] = Field(None, description="Recovery suggestion")


class QueryResultMeta(BaseModel):
    """Metadata returned with every query result."""
    total: int = Field(..., description="Total matching rows before pagination")
    total_scanned: int = Field(..., description="Total rows scanned")
    matched_count: int = Field(..., description="Rows matching conditions")
    returned_count: int = Field(..., description="Rows returned in this page")
    truncated: bool = Field(False, description="Whether results were truncated")
    page: int
    page_size: int
    total_pages: int
    conditions_applied: int
    execution_time_ms: float = Field(..., description="Backend execution time in ms")
    execution_target: str = Field(..., description="live or history")
    validation_errors: list[QueryValidationError] = Field(
        default_factory=list, description="Non-fatal validation warnings"
    )
