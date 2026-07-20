"""
LiveCache — In-memory store for the latest market snapshot.

This is the central bridge between the scheduler (data producer) and the
API/WebSocket layer (data consumer). The scheduler calls update() after
each fetch cycle, and the API reads from it for all live queries.

Design decisions:
- Thread-safe via RLock (scheduler runs in a background thread)
- Stores exactly two snapshots: current + previous (for diff detection)
- Memory footprint: ~10 MB for 6,800 instruments × 100 columns × 2 snapshots
- Column metadata is re-introspected on every update (handles dynamic columns)
- Uses Instrument column as the primary key for row-level diff detection
"""

import threading
import logging
from datetime import datetime
from typing import Any, Optional

import pandas as pd
import pytz

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

# Columns to compare for diff detection. If ANY of these change, the row is "changed".
# Using a small set avoids comparing all 100+ columns every cycle.
DIFF_COLUMNS = {"Last Price", "Volume", "Net Change", "High", "Low"}


class LiveCache:
    """
    Thread-safe in-memory cache holding the latest market snapshot.

    Usage:
        cache = LiveCache()
        changed_rows = cache.update(new_dataframe)  # Called by scheduler
        snapshot = cache.get_snapshot()              # Called by API
        stock = cache.get_stock("NSE_EQ:INFY")      # Called by API
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._current_df: Optional[pd.DataFrame] = None
        self._previous_df: Optional[pd.DataFrame] = None
        self._snapshot_id: int = 0
        self._last_updated: Optional[datetime] = None
        self._column_metadata: list[dict] = []
        self._instrument_index: dict[str, int] = {}  # Instrument -> row index

    @property
    def is_populated(self) -> bool:
        """Check if the cache has been populated with at least one snapshot."""
        with self._lock:
            return self._current_df is not None and not self._current_df.empty

    @property
    def snapshot_id(self) -> int:
        with self._lock:
            return self._snapshot_id

    @property
    def last_updated(self) -> Optional[datetime]:
        with self._lock:
            return self._last_updated

    def update(self, new_df: pd.DataFrame) -> list[dict]:
        """
        Replace the current snapshot with new data.

        Args:
            new_df: The new DataFrame from the scheduler's fetch cycle.

        Returns:
            List of changed rows as dicts (for WebSocket broadcast).
            On first update, returns all rows.
        """
        if new_df is None or new_df.empty:
            logger.warning("LiveCache.update() called with empty DataFrame, skipping")
            return []

        with self._lock:
            self._previous_df = self._current_df
            self._current_df = new_df.copy()
            self._snapshot_id += 1
            self._last_updated = datetime.now(IST)

            # Build instrument index for fast lookups
            if "Instrument" in new_df.columns:
                self._instrument_index = {
                    row["Instrument"]: idx
                    for idx, row in new_df.iterrows()
                }

            # Compute changed rows
            changed = self._compute_diff()

            logger.info(
                f"LiveCache updated: snapshot_id={self._snapshot_id}, "
                f"total_rows={len(new_df)}, changed_rows={len(changed)}"
            )

            return changed

    def _compute_diff(self) -> list[dict]:
        """
        Compare current vs previous snapshot and return changed rows.

        Strategy:
        - First update (no previous): return ALL rows
        - Subsequent updates: compare key columns by Instrument
        - Only rows where price/volume/change differ are returned
        """
        current = self._current_df
        previous = self._previous_df

        if current is None or current.empty:
            return []

        # First snapshot — everything is "changed"
        if previous is None or previous.empty:
            return self._df_to_records(current)

        # Identify columns that exist in both snapshots for comparison
        compare_cols = list(DIFF_COLUMNS & set(current.columns) & set(previous.columns))
        if not compare_cols or "Instrument" not in current.columns:
            return self._df_to_records(current)

        try:
            # Index both by Instrument for aligned comparison
            current_indexed = current.set_index("Instrument")[compare_cols]
            previous_indexed = previous.set_index("Instrument")[compare_cols]

            # Find instruments present in both
            common = current_indexed.index.intersection(previous_indexed.index)

            if common.empty:
                return self._df_to_records(current)

            # Compare only common instruments on diff columns
            curr_common = current_indexed.loc[common]
            prev_common = previous_indexed.loc[common]

            # Rows where any diff column changed
            changed_mask = (curr_common != prev_common).any(axis=1)
            changed_instruments = changed_mask[changed_mask].index.tolist()

            # Also include new instruments not in previous snapshot
            new_instruments = current_indexed.index.difference(previous_indexed.index).tolist()

            all_changed = set(changed_instruments + new_instruments)

            if not all_changed:
                return []

            changed_df = current[current["Instrument"].isin(all_changed)]
            return self._df_to_records(changed_df)

        except Exception as e:
            logger.error(f"Diff computation failed, returning full snapshot: {e}")
            return self._df_to_records(current)

    def get_snapshot(self) -> Optional[pd.DataFrame]:
        """Return a copy of the current snapshot DataFrame."""
        with self._lock:
            if self._current_df is None:
                return None
            return self._current_df.copy()

    def get_snapshot_records(self) -> list[dict]:
        """Return the current snapshot as a list of dicts (JSON-serializable)."""
        with self._lock:
            if self._current_df is None:
                return []
            return self._df_to_records(self._current_df)

    def get_stock(self, instrument: str) -> Optional[dict]:
        """
        Return a single stock's data by Instrument key.

        Args:
            instrument: e.g., "NSE_EQ:INFY" or partial like "INFY"
        """
        with self._lock:
            if self._current_df is None:
                return None

            # Exact match first
            if instrument in self._instrument_index:
                idx = self._instrument_index[instrument]
                row = self._current_df.iloc[idx]
                return self._row_to_dict(row)

            # Partial match on trading_symbol or Instrument
            mask = (
                self._current_df["Instrument"].str.contains(instrument, case=False, na=False)
            )
            if "trading_symbol" in self._current_df.columns:
                mask = mask | self._current_df["trading_symbol"].str.contains(
                    instrument, case=False, na=False
                )

            matches = self._current_df[mask]
            if matches.empty:
                return None

            # Return first match
            return self._row_to_dict(matches.iloc[0])

    def search(self, query: str, limit: int = 50) -> list[dict]:
        """
        Search stocks by partial match on Instrument, trading_symbol, or company_name.

        Args:
            query: Search string (case-insensitive)
            limit: Maximum results to return

        Returns:
            List of matching stock records
        """
        with self._lock:
            if self._current_df is None or not query:
                return []

            query_lower = query.lower()
            df = self._current_df

            mask = df["Instrument"].str.lower().str.contains(query_lower, na=False)

            if "trading_symbol" in df.columns:
                mask = mask | df["trading_symbol"].str.lower().str.contains(
                    query_lower, na=False
                )

            if "company_name" in df.columns:
                mask = mask | df["company_name"].str.lower().str.contains(
                    query_lower, na=False
                )

            results = df[mask].head(limit)
            return self._df_to_records(results)

    def get_columns(self) -> list[str]:
        """Return list of all column names in the current snapshot."""
        with self._lock:
            if self._current_df is None:
                return []
            return self._current_df.columns.tolist()

    def get_total_count(self) -> int:
        """Return total number of instruments in the current snapshot."""
        with self._lock:
            if self._current_df is None:
                return 0
            return len(self._current_df)

    def get_snapshot_info(self) -> dict:
        """Return metadata about the current cache state."""
        with self._lock:
            return {
                "is_populated": self.is_populated,
                "snapshot_id": self._snapshot_id,
                "last_updated": self._last_updated.isoformat() if self._last_updated else None,
                "total_instruments": len(self._current_df) if self._current_df is not None else 0,
                "total_columns": len(self._current_df.columns) if self._current_df is not None else 0,
            }

    # ── Private Helpers ─────────────────────────────────────────────────

    @staticmethod
    def _df_to_records(df: pd.DataFrame) -> list[dict]:
        """Convert a DataFrame to a list of JSON-serializable dicts."""
        records = df.to_dict(orient="records")
        # Convert non-serializable types
        for record in records:
            for key, value in record.items():
                if isinstance(value, pd.Timestamp):
                    record[key] = value.isoformat()
                elif pd.isna(value):
                    record[key] = None
        return records

    @staticmethod
    def _row_to_dict(row: pd.Series) -> dict:
        """Convert a single row to a JSON-serializable dict."""
        record = row.to_dict()
        for key, value in record.items():
            if isinstance(value, pd.Timestamp):
                record[key] = value.isoformat()
            elif pd.isna(value):
                record[key] = None
        return record


# ── Module-level singleton ──────────────────────────────────────────────
# Shared across scheduler thread and API handlers within the same process.
live_cache = LiveCache()
