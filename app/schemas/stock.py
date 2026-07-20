"""
Stock-related request/response schemas.

These are intentionally lightweight — the actual stock data is dynamic
(columns change as backend adds new metrics), so we don't define a fixed
StockRow model. Instead, stocks are returned as dict records.
The schemas here cover query parameters and structural validation.
"""

from pydantic import BaseModel, Field
from typing import Optional
from enum import Enum


class SortOrder(str, Enum):
    ASC = "asc"
    DESC = "desc"


class StockListParams(BaseModel):
    """Query parameters for the paginated stock list endpoint."""
    page: int = Field(1, ge=1, description="Page number (1-indexed)")
    page_size: int = Field(50, ge=1, le=500, description="Rows per page")
    sort_by: Optional[str] = Field(None, description="Column name to sort by")
    sort_order: SortOrder = Field(SortOrder.ASC, description="Sort direction")
    search: Optional[str] = Field(None, description="Search query (symbol, company name)")
    filters: Optional[str] = Field(
        None,
        description='JSON-encoded filter object, e.g., {"Volume": {"gte": 100000}}'
    )
    columns: Optional[str] = Field(
        None,
        description="Comma-separated list of columns to include in response"
    )
