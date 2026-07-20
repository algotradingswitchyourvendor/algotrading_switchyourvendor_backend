"""
Scanner request/response schemas.
"""

from pydantic import BaseModel, Field
from typing import Any, Optional


class ScannerCondition(BaseModel):
    """A single scanner condition."""
    column: str = Field(..., description="DataFrame column name")
    operator: str = Field(..., description="Comparison operator")
    value: Any = Field(..., description="Value to compare against")
    logical: str = Field("AND", description="Logical operator: AND or OR")


class ScannerRequest(BaseModel):
    """Request body for POST /api/v1/scanner."""
    mode: str = Field("live", description="'live' or 'historical'")
    conditions: list[ScannerCondition] = Field(
        ..., description="List of conditions to evaluate"
    )
    sort_by: Optional[str] = Field(None, description="Column to sort results by")
    sort_order: str = Field("desc", description="'asc' or 'desc'")
    page: int = Field(1, ge=1, description="Page number")
    page_size: int = Field(50, ge=1, le=500, description="Results per page")
    # Historical mode params
    date: Optional[str] = Field(None, description="Date for historical scan")
    start_time: Optional[str] = Field(None, description="Start time for historical scan")
    end_time: Optional[str] = Field(None, description="End time for historical scan")
