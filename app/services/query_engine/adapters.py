"""
Data Source Adapters — Abstract the data source from the query engine.

The engine never touches LiveCache or Parquet directly.
Adapters provide a pd.DataFrame and the engine operates on it.

Adapters:
    LiveAdapter     — Reads from the in-memory LiveCache snapshot
    HistoryAdapter  — Reads from Parquet via HistoryService
"""

import logging
from typing import Optional

import pandas as pd

from app.cache.live_cache import LiveCache

logger = logging.getLogger(__name__)


class LiveAdapter:
    """Provides the current live market snapshot as a DataFrame."""

    def get_dataframe(self, cache: LiveCache) -> pd.DataFrame:
        """
        Get the current live snapshot.

        Returns:
            pd.DataFrame with all live instruments, or empty DataFrame.
        """
        if not cache.is_populated:
            logger.warning("LiveAdapter: cache not populated")
            return pd.DataFrame()

        df = cache.get_snapshot()
        if df is None or df.empty:
            return pd.DataFrame()
        return df


class HistoryAdapter:
    """Provides historical data from Parquet files as a DataFrame."""

    def get_dataframe(
        self,
        date: Optional[str] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
    ) -> pd.DataFrame:
        """
        Load historical data for the given date/time range.

        Returns:
            pd.DataFrame with historical records, or empty DataFrame.
        """
        from app.services.history_service import get_historical_data
        import asyncio

        try:
            # history_service may be async; handle both cases
            coro = get_historical_data(
                target_date=date,
                start_time=start_time,
                end_time=end_time,
                page=1,
                page_size=500_000,  # Load all rows for scanning
            )

            # If we're inside an event loop, run directly; otherwise use asyncio.run
            try:
                loop = asyncio.get_running_loop()
                # We're inside an async context — create a task
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    records, _ = pool.submit(asyncio.run, coro).result()
            except RuntimeError:
                # No running loop — safe to use asyncio.run
                records, _ = asyncio.run(coro)

            if not records:
                return pd.DataFrame()

            return pd.DataFrame(records)

        except Exception as e:
            logger.error(f"HistoryAdapter failed: {e}")
            return pd.DataFrame()
