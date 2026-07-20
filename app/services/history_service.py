"""
History Service facade.
Delegates all calls to the production-grade HistoryService in the history package.
"""

from typing import Dict, List, Optional, Tuple

from app.services.history import HistoryService

# Singleton instance of the new history service
_service = HistoryService()

async def get_historical_data(
    symbol: Optional[str] = None,
    target_date: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    page: int = 1,
    page_size: int = 100,
) -> Tuple[List[Dict], Dict]:
    return await _service.get_historical_data(
        symbol=symbol,
        target_date=target_date or "today",
        start_time=start_time,
        end_time=end_time,
        page=page,
        page_size=page_size
    )

async def get_stock_timeline(
    symbol: str,
    target_date: Optional[str] = None,
) -> List[Dict]:
    return await _service.get_stock_timeline(symbol=symbol, target_date=target_date or "today")

async def list_available_dates() -> List[str]:
    return await _service.list_available_dates()

def get_cache_manager():
    """Returns the underlying CacheManager for operations like startup recovery."""
    return _service.cache_manager
