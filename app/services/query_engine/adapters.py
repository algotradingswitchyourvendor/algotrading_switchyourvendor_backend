"""
Data Source Adapters — Abstract the data source from the query engine.

The engine never touches LiveCache or Parquet directly.
Adapters provide a pd.DataFrame and the engine operates on it.

Adapters:
    LiveAdapter     — Reads from the in-memory LiveCache snapshot
    HistoryAdapter  — Reads from Parquet via HistoryService
"""

import logging
from typing import Optional, Dict
from abc import ABC, abstractmethod
from dataclasses import dataclass

import pandas as pd

from app.cache.live_cache import LiveCache
from app.schemas.query import UnifiedQueryRequest

logger = logging.getLogger(__name__)

@dataclass
class AdapterResult:
    df: pd.DataFrame
    is_pre_processed: bool = False
    matched_count: Optional[int] = None
    total_scanned: Optional[int] = None
    timings: Optional[Dict[str, float]] = None
    bullish_count: Optional[int] = None
    bearish_count: Optional[int] = None

class BaseAdapter(ABC):
    """Provides the current live market snapshot as a DataFrame."""

    @abstractmethod
    async def get_dataframe(
        self,
        cache: Optional[LiveCache] = None,
        request: Optional[UnifiedQueryRequest] = None,
    ) -> pd.DataFrame | AdapterResult:
        """
        Subclasses should return a Pandas DataFrame or an AdapterResult.
        """
        pass


class LiveAdapter(BaseAdapter):
    """Provides the current live market snapshot as a DataFrame."""

    async def get_dataframe(self, cache: LiveCache) -> pd.DataFrame:
        """
        Get the current live snapshot.

        Returns:
            pd.DataFrame with all live instruments, or empty DataFrame.
        """
        if not cache or not cache.is_populated:
            logger.warning("LiveAdapter: cache not populated or missing")
            return pd.DataFrame()

        df = cache.get_snapshot()
        if df is None or df.empty:
            return pd.DataFrame()
        return df


class HistoryAdapter(BaseAdapter):
    """Provides historical data from Parquet files as a DataFrame."""

    async def get_dataframe(
        self,
        request: UnifiedQueryRequest,
    ) -> AdapterResult:
        """
        Load historical data for the given date/time range.

        Returns:
            AdapterResult with historical records, or empty DataFrame.
        """
        from app.services.history_service import get_historical_dataframe
        import asyncio

        try:
            result = await get_historical_dataframe(
                request=request
            )

            if result is None:
                return pd.DataFrame()

            return result

        except Exception as e:
            logger.error(f"HistoryAdapter failed: {e}")
            return pd.DataFrame()
