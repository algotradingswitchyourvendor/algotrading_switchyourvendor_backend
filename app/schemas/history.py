"""
History request schemas.
"""

from pydantic import BaseModel, Field
from typing import Optional


class HistoryParams(BaseModel):
    """Query parameters for the history endpoint."""
    symbol: Optional[str] = Field(None, description="Instrument key or trading symbol")
    date: Optional[str] = Field("today", description="Date (YYYY-MM-DD) or 'today'")
    start_time: Optional[str] = Field(None, description="Start time filter (HH:MM)")
    end_time: Optional[str] = Field(None, description="End time filter (HH:MM)")
    page: int = Field(1, ge=1, description="Page number")
    page_size: int = Field(100, ge=1, le=1000, description="Rows per page")
