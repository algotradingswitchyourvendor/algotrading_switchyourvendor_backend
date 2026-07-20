import asyncio
import logging
import math
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import pandas as pd

from app.config.settings import get_settings
from app.services.history.cache_manager import CacheManager

logger = logging.getLogger(__name__)

class HistoryService:
    """
    High-level service for querying historical market data.
    Uses the CacheManager to ensure data is safely and efficiently retrieved.
    """

    def __init__(self):
        self.settings = get_settings()
        self.cache_manager = CacheManager()
        
        # We spawn a background task for cleanup every hour, 
        # but since we can't reliably track task lifecycle here 
        # without tying it to FastAPI, we'll let main.py or a scheduler handle it,
        # or we just trigger it asynchronously after reads.
        self._last_cleanup = 0.0

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

        # Automatically check if cleanup is needed
        import time
        await self._trigger_cleanup_if_needed()

        parquet_path = await self.cache_manager.get_or_download(target_date)
        
        if not parquet_path:
            raise ValueError(f"Historical data for {target_date} is unavailable.")

        try:
            # We use pandas to read and filter the parquet.
            # In a very high throughput system, we might use polars or pyarrow directly.
            # For this architecture, pandas is acceptable as we only load the needed columns/rows.
            df = pd.read_parquet(parquet_path)
            
            # Apply symbol filter
            if symbol:
                symbol = symbol.upper()
                df = df[df["symbol"] == symbol]

            # Apply time filter
            if start_time or end_time:
                # Ensure timestamp is parsed properly
                if not pd.api.types.is_datetime64_any_dtype(df["timestamp"]):
                    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms" if type(df["timestamp"].iloc[0]) in (int, float) else None)
                
                if start_time:
                    try:
                        st_dt = datetime.strptime(start_time, "%H:%M").time()
                        df = df[df["timestamp"].dt.time >= st_dt]
                    except ValueError:
                        pass # Ignore malformed time
                        
                if end_time:
                    try:
                        et_dt = datetime.strptime(end_time, "%H:%M").time()
                        df = df[df["timestamp"].dt.time <= et_dt]
                    except ValueError:
                        pass

            # Pagination
            total_records = len(df)
            total_pages = math.ceil(total_records / page_size) if total_records > 0 else 1
            
            # Ensure page is within bounds
            page = max(1, min(page, total_pages))
            
            start_idx = (page - 1) * page_size
            end_idx = start_idx + page_size
            
            paginated_df = df.iloc[start_idx:end_idx]
            
            # Convert to dictionary format
            # Using 'records' format, but we handle NaN/NaT gracefully
            records = paginated_df.fillna("").to_dict(orient="records")
            
            # Formatting timestamps back to ISO strings if needed
            for record in records:
                if isinstance(record.get("timestamp"), pd.Timestamp):
                    record["timestamp"] = record["timestamp"].isoformat() + "Z"
            
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
            raise ValueError("Failed to process historical data file.")

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
            df = pd.read_parquet(parquet_path, filters=[("symbol", "==", symbol.upper())])
            if df.empty:
                return []

            if not pd.api.types.is_datetime64_any_dtype(df["timestamp"]):
                df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms" if type(df["timestamp"].iloc[0]) in (int, float) else None)

            # Group by minute
            df_minute = df.set_index("timestamp").resample("1Min").last().dropna(subset=["last_price"])
            df_minute = df_minute.reset_index()
            
            records = df_minute.fillna("").to_dict(orient="records")
            for record in records:
                if isinstance(record.get("timestamp"), pd.Timestamp):
                    record["timestamp"] = record["timestamp"].isoformat() + "Z"
                    
            return records

        except Exception as e:
            logger.error(f"Error generating timeline for {symbol}: {e}")
            return []

    async def list_available_dates(self) -> List[str]:
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

