"""
History Service facade.
Delegates all calls to the production-grade HistoryService in the history package.
"""

from typing import Dict, List, Optional, Tuple

import pandas as pd

from app.services.history import HistoryService

# Singleton instance of the new history service
_service = HistoryService()

async def get_historical_data(
    symbol: Optional[str] = None,
    target_date: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    sort_by: Optional[str] = None,
    sort_order: Optional[str] = "asc",
    page: int = 1,
    page_size: int = 100,
) -> Tuple[List[Dict], Dict]:
    return await _service.get_historical_data(
        symbol=symbol,
        target_date=target_date or "today",
        start_time=start_time,
        end_time=end_time,
        sort_by=sort_by,
        sort_order=sort_order,
        page=page,
        page_size=page_size
    )

from typing import Optional, Any

async def get_historical_dataframe(
    request: Any = None,
) -> Any:
    return await _service.get_historical_dataframe(
        request=request
    )

async def get_stock_timeline(
    symbol: str,
    target_date: Optional[str] = None,
) -> List[Dict]:
    return await _service.get_stock_timeline(symbol=symbol, target_date=target_date or "today")

async def list_available_dates() -> List[str]:
    return await _service.list_available_dates()

async def get_schema_dataframe(target_date: str) -> Optional[pd.DataFrame]:
    """Get the schema DataFrame for a historical date."""
    return await _service.get_schema_dataframe(target_date)

def get_cache_manager():
    """Returns the underlying CacheManager for operations like startup recovery."""
    return _service.cache_manager
