import asyncio
import logging
import math
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import pandas as pd

from app.config.settings import get_settings
from app.services.history.cache_manager import CacheManager
from app.services.column_service import _infer_group

logger = logging.getLogger(__name__)

class HistoryService:
    """
    High-level service for querying historical market data.
    Uses the CacheManager to ensure data is safely and efficiently retrieved.
    """

    def __init__(self):
        self.settings = get_settings()
        self.cache_manager = CacheManager()
        self._last_cleanup = 0.0
        
        # Cache for logical column mapping to avoid rescanning on every request
        self._column_mapping_cache: Dict[str, Dict[str, str]] = {}

    def _get_logical_column(self, df_columns: list[str], target_group: str) -> Optional[str]:
        """Uses the single source of truth (column_service) to resolve dynamic metadata mapping."""
        # Use a hash of the columns as the cache key to detect schema changes
        schema_key = str(hash(tuple(df_columns)))
        
        if schema_key not in self._column_mapping_cache:
            self._column_mapping_cache[schema_key] = {}
            
        if target_group in self._column_mapping_cache[schema_key]:
            return self._column_mapping_cache[schema_key][target_group]

        # Reuse single source of truth from column_service
        for col in df_columns:
            if _infer_group(col) == target_group:
                self._column_mapping_cache[schema_key][target_group] = col
                return col
                
        self._column_mapping_cache[schema_key][target_group] = None
        return None

    def _ensure_datetime(self, df: pd.DataFrame, timestamp_col: str):
        """Helper to safely ensure the column is datetime."""
        if not pd.api.types.is_datetime64_any_dtype(df[timestamp_col]):
            unit = "ms" if type(df[timestamp_col].iloc[0]) in (int, float) else None
            df[timestamp_col] = pd.to_datetime(df[timestamp_col], unit=unit)
        return df

    def _load_and_filter_parquet(
        self,
        parquet_path: str,
        symbol: Optional[str],
        start_time: Optional[str],
        end_time: Optional[str]
    ) -> Tuple[pd.DataFrame, Optional[str]]:
        """
        Runs entirely in a background thread.
        Handles heavy disk I/O and CPU-bound string/datetime filtering.
        """
        pyarrow_start = time.perf_counter()
        df = pd.read_parquet(parquet_path)
        pyarrow_elapsed = (time.perf_counter() - pyarrow_start) * 1000
        logger.info(f"PyArrow Read: {pyarrow_elapsed:.2f} ms")
        
        pandas_start = time.perf_counter()
        
        # Apply symbol filter safely using single source of truth
        symbol_col = self._get_logical_column(df.columns, "Identity")
        if symbol and symbol_col:
            # Production-safe search: trim whitespace, upper case, case=False, regex=False, na=False
            safe_symbol = symbol.strip().upper()
            df = df[df[symbol_col].astype(str).str.contains(safe_symbol, case=False, regex=False, na=False)]

        # Apply time filter using authoritative Fetch Timestamp if available
        timestamp_col = "Fetch Timestamp" if "Fetch Timestamp" in df.columns else self._get_logical_column(df.columns, "Metadata")
        if (start_time or end_time) and timestamp_col:
            df = self._ensure_datetime(df, timestamp_col)
            
            if start_time:
                try:
                    st_dt = datetime.strptime(start_time, "%H:%M").time()
                    df = df[df[timestamp_col].dt.time >= st_dt]
                except ValueError:
                    pass
                    
            if end_time:
                try:
                    et_dt = datetime.strptime(end_time, "%H:%M").time()
                    df = df[df[timestamp_col].dt.time <= et_dt]
                except ValueError:
                    pass
                    
        # Ensure sort by authoritative timestamp
        if timestamp_col:
            df = df.sort_values(by=timestamp_col)
            
        pandas_elapsed = (time.perf_counter() - pandas_start) * 1000
        logger.info(f"Pandas Filtering: {pandas_elapsed:.2f} ms")
        
        return df, timestamp_col

    async def _trigger_cleanup_if_needed(self):
        """Non-blocking asynchronous check to run LRU cleanup."""
        now = time.time()
        if now - self._last_cleanup > self.settings.CACHE_CLEANUP_INTERVAL:
            self._last_cleanup = now
            asyncio.create_task(self.cache_manager.cleanup_lru())

    async def get_historical_data(
        self,
        symbol: Optional[str] = None,
        target_date: str = "today",
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        page: int = 1,
        page_size: int = 100
    ) -> Tuple[List[Dict], Dict]:
        """
        Retrieves historical data with pagination and filtering.
        """
        if target_date == "today":
            target_date = datetime.now().strftime("%Y-%m-%d")

        await self._trigger_cleanup_if_needed()

        parquet_path = await self.cache_manager.get_or_download(target_date)
        if not parquet_path:
            raise ValueError(f"Historical data for {target_date} is unavailable.")

        try:
            # Execute heavy I/O and CPU-bound filtering in a thread pool
            df, timestamp_col = await asyncio.to_thread(
                self._load_and_filter_parquet, parquet_path, symbol, start_time, end_time
            )

            # Pagination (Lightweight: < 1ms, stays on Event Loop)
            total_records = len(df)
            total_pages = math.ceil(total_records / page_size) if total_records > 0 else 1
            page = max(1, min(page, total_pages))
            
            start_idx = (page - 1) * page_size
            end_idx = start_idx + page_size
            paginated_df = df.iloc[start_idx:end_idx]
            
            # Serialization (Lightweight: < 10ms, stays on Event Loop)
            records = paginated_df.fillna("").to_dict(orient="records")
            
            if timestamp_col:
                for record in records:
                    if isinstance(record.get(timestamp_col), pd.Timestamp):
                        record[timestamp_col] = record[timestamp_col].isoformat() + "Z"
            
            meta = {
                "current_page": page,
                "page_size": page_size,
                "total_pages": total_pages,
                "total_records": total_records,
                "has_next": page < total_pages,
                "has_previous": page > 1,
            }
            
            return records, meta

        except Exception as e:
            logger.error(f"Error parsing parquet {parquet_path}: {e}")
            raise ValueError("Failed to process historical data file.") from e

    async def get_stock_timeline(self, symbol: str, target_date: str = "today") -> List[Dict]:
        """
        Extracts a minute-by-minute timeline for a specific symbol.
        """
        if target_date == "today":
            target_date = datetime.now().strftime("%Y-%m-%d")

        parquet_path = await self.cache_manager.get_or_download(target_date)
        if not parquet_path:
            return []

        try:
            # Re-use the background thread logic to read and filter by symbol
            df, timestamp_col = await asyncio.to_thread(
                self._load_and_filter_parquet, parquet_path, symbol, None, None
            )

            if df.empty or not timestamp_col:
                return []

            # Timeline Grouping (Lightweight on small filtered dataframe)
            df = self._ensure_datetime(df, timestamp_col)

            # Group by minute
            df_minute = df.set_index(timestamp_col).resample("1Min").last()
            
            # Fallback to identify Price column for dropna safely
            price_col = self._get_logical_column(df_minute.columns, "Price")
            if price_col:
                df_minute = df_minute.dropna(subset=[price_col])
                
            df_minute = df_minute.reset_index()
            
            records = df_minute.fillna("").to_dict(orient="records")
            for record in records:
                if isinstance(record.get(timestamp_col), pd.Timestamp):
                    record[timestamp_col] = record[timestamp_col].isoformat() + "Z"
                    
            return records

        except Exception as e:
            logger.error(f"Error generating timeline for {symbol}: {e}")
            return []

    async def list_available_dates(self) -> List[str]:
        """List all dates that have historical parquet files."""
        return await asyncio.to_thread(self._list_dates)

    async def get_schema_dataframe(self, target_date: str) -> Optional[pd.DataFrame]:
        """
        Quickly load 1 row of a historical parquet file to introspect its schema,
        without loading the full dataset. Used by metadata generation.
        """
        try:
            parquet_path = await self.cache_manager.get_or_download(target_date)
            # We don't need data, just the schema. Read 1 row to get full dtype/column info.
            # Reading 1 row is extremely fast with Parquet.
            # Using asyncio.to_thread because read_parquet is blocking I/O.
            df = await asyncio.to_thread(pd.read_parquet, parquet_path)
            return df.head(1)
        except Exception as e:
            logger.error(f"Failed to load schema for {target_date}: {e}")
            return None

    async def _list_dates(self) -> List[str]:
        """List all dates that have parquet data in S3."""
        try:
            import boto3
            s3_client = boto3.client("s3")
            response = await asyncio.to_thread(
                s3_client.list_objects_v2,
                Bucket=self.settings.S3_BUCKET_NAME,
                Prefix=f"{self.settings.S3_PARQUET_PREFIX}/",
            )

            dates = []
            for obj in response.get("Contents", []):
                key = obj["Key"]
                filename = key.split("/")[-1]
                if filename.endswith("_Equity.parquet"):
                    date_str = filename.replace("_Equity.parquet", "")
                    dates.append(date_str)

            return sorted(dates, reverse=True)

        except Exception as e:
            logger.error(f"Failed to list available dates: {e}")
            return []
