"""
Standard API response envelope.

Every API response follows this structure to ensure consistency.
Frontend can rely on checking `success` to determine response type,
and `meta` for pagination information when applicable.
"""

from pydantic import BaseModel, Field
from typing import Any, Optional
from datetime import datetime


class PaginationMeta(BaseModel):
    """Pagination metadata included in list responses."""
    total: int = Field(..., description="Total number of records")
    page: int = Field(..., description="Current page number (1-indexed)")
    page_size: int = Field(..., description="Number of records per page")
    total_pages: int = Field(..., description="Total number of pages")


class ErrorDetail(BaseModel):
    """Structured error information."""
    code: str = Field(..., description="Machine-readable error code")
    message: str = Field(..., description="Human-readable error message")
    details: Optional[Any] = Field(None, description="Additional error context")


class ApiResponse(BaseModel):
    """
    Standard response envelope for all API endpoints.

    Success: { success: true, timestamp, market_status, data, meta? }
    Error:   { success: false, timestamp, error }
    """
    success: bool = True
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())
    market_status: Optional[str] = None
    data: Optional[Any] = None
    meta: Optional[PaginationMeta] = None
    error: Optional[ErrorDetail] = None


def success_response(
    data: Any,
    market_status: str = "UNKNOWN",
    meta: Optional[PaginationMeta] = None,
) -> dict:
    """Helper to build a successful API response dict."""
    response = {
        "success": True,
        "timestamp": datetime.now().isoformat(),
        "market_status": market_status,
        "data": data,
    }
    if meta:
        response["meta"] = meta.model_dump()
    return response


def error_response(
    code: str,
    message: str,
    details: Any = None,
    status_code: int = 500,
) -> dict:
    """Helper to build an error API response dict."""
    return {
        "success": False,
        "timestamp": datetime.now().isoformat(),
        "error": {
            "code": code,
            "message": message,
            "details": details,
        },
    }
