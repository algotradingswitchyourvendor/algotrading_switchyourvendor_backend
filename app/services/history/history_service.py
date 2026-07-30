import asyncio
import logging
import math
import os
import time
import threading
from collections import OrderedDict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import duckdb
import pandas as pd

from app.config.settings import get_settings
from app.services.history.cache_manager import CacheManager
from app.services.history.metrics import request_metrics, GlobalCacheStats, RequestMetrics
from app.services.history.singleflight import SingleFlightSync
from app.services.column_service import _infer_group
from app.services.query_engine.sql_translator import translate_conditions
from app.services.query_engine.engine import resolve_conditions
from app.services.query_engine.adapters import AdapterResult

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

        # Thread-safe bounded LRU cache for parsed DataFrames
        # Key: (parquet_path, mtime) → Value: pd.DataFrame (immutable base copy)
        self._df_cache: OrderedDict[Tuple[str, float], pd.DataFrame] = OrderedDict()
        self._df_cache_lock = threading.Lock()

        # SingleFlight for parquet reads — prevents duplicate pd.read_parquet calls
        self._parquet_flight = SingleFlightSync()

    def _get_cached_df(self, parquet_path: str) -> Optional[pd.DataFrame]:
        """
        Returns a cached DataFrame if the file hasn't changed on disk.
        Uses file mtime as the invalidation signal.
        Returns None on cache miss or if the file has been updated.
        """
        try:
            mtime = os.path.getmtime(parquet_path)
        except OSError:
            return None

        key = (parquet_path, mtime)
        with self._df_cache_lock:
            if key in self._df_cache:
                self._df_cache.move_to_end(key)
                if self.settings.ENABLE_HISTORY_METRICS:
                    GlobalCacheStats.record_df_result(True)
                    metrics = request_metrics.get()
                    if metrics:
                        metrics.df_cache_hit = True
                logger.info(f"DataFrame Cache Hit: {parquet_path}")
                return self._df_cache[key]
        
        if self.settings.ENABLE_HISTORY_METRICS:
            GlobalCacheStats.record_df_result(False)
            metrics = request_metrics.get()
            if metrics:
                metrics.df_cache_hit = False
                
        return None

    def _put_cached_df(self, parquet_path: str, df: pd.DataFrame) -> None:
        """
        Stores a DataFrame in the bounded LRU cache.
        Evicts the least recently used entry if at capacity.
        """
        try:
            mtime = os.path.getmtime(parquet_path)
        except OSError:
            return

        key = (parquet_path, mtime)
        max_entries = self.settings.DF_CACHE_MAX_ENTRIES

        with self._df_cache_lock:
            if key in self._df_cache:
                self._df_cache.move_to_end(key)
                return

            while len(self._df_cache) >= max_entries:
                evicted_key, _ = self._df_cache.popitem(last=False)
                if self.settings.ENABLE_HISTORY_METRICS:
                    GlobalCacheStats.record_df_eviction()
                logger.info(f"DataFrame Cache Evicted: {evicted_key[0]}")

            self._df_cache[key] = df
            logger.info(f"DataFrame Cache Stored: {parquet_path} ({len(df)} rows, cache size: {len(self._df_cache)}/{max_entries})")

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
        Uses an in-memory LRU cache to skip repeated PyArrow reads.
        """
        metrics = None
        if self.settings.ENABLE_HISTORY_METRICS:
            metrics = request_metrics.get()

        # Check DataFrame cache first
        cached_df = self._get_cached_df(parquet_path)
        if cached_df is not None:
            # Use the cached base — work on a copy for filtering
            df = cached_df.copy()
        else:
            # SingleFlight: only one thread reads the parquet; followers wait then use cache
            def _read_and_cache():
                """Leader function: reads parquet and populates the DF cache."""
                _start = time.perf_counter()
                _df = pd.read_parquet(parquet_path, engine="pyarrow", dtype_backend="pyarrow")
                _elapsed = (time.perf_counter() - _start) * 1000
                if metrics:
                    metrics.t_parquet_read = _elapsed
                logger.info(f"PyArrow Read: {_elapsed:.2f} ms")
                # Store the raw, unfiltered DataFrame in cache (immutable base)
                self._put_cached_df(parquet_path, _df)
                return _df

            base_df, was_coalesced = self._parquet_flight.do(parquet_path, _read_and_cache)

            if was_coalesced:
                # Follower: the leader already populated the cache.
                # Read from cache to get our own reference (avoids holding the leader's object)
                cached_df = self._get_cached_df(parquet_path)
                if cached_df is not None:
                    df = cached_df.copy()
                else:
                    # Extremely unlikely: cache was evicted between leader finish and follower read.
                    # Fall back to using the leader's returned DataFrame.
                    df = base_df.copy()

                if metrics:
                    metrics.request_coalesced = True
                    metrics.duplicate_read_prevented = True
                if self.settings.ENABLE_HISTORY_METRICS:
                    GlobalCacheStats.record_coalesce_result(True)
            else:
                # Leader: we just read it — work on a copy
                df = base_df.copy()
                if metrics:
                    metrics.request_coalesced = False
                    metrics.duplicate_read_prevented = False

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
        if metrics:
            metrics.t_filtering = pandas_elapsed
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
        sort_by: Optional[str] = None,
        sort_order: Optional[str] = "asc",
        page: int = 1,
        page_size: int = 100
    ) -> Tuple[List[Dict], Dict]:
        """
        Retrieves historical data with pagination and filtering.
        """
        req_start = time.perf_counter()
        import uuid
        rm = None
        token = None
        if self.settings.ENABLE_HISTORY_METRICS:
            rm = RequestMetrics(request_id=str(uuid.uuid4())[:8], target_date=target_date)
            token = request_metrics.set(rm)

        try:
            if target_date == "today":
                target_date = datetime.now().strftime("%Y-%m-%d")

            await self._trigger_cleanup_if_needed()

            parquet_path = await self.cache_manager.get_or_download(target_date)
            if not parquet_path:
                raise ValueError(f"Historical data for {target_date} is unavailable.")

            # Execute heavy I/O and CPU-bound filtering in a thread pool
            df, timestamp_col = await asyncio.to_thread(
                self._load_and_filter_parquet, parquet_path, symbol, start_time, end_time
            )
            
            if rm:
                rm.rows_loaded = len(df)

            # Sort before Pagination
            t_sort_start = time.perf_counter()
            if sort_by and sort_by in df.columns:
                try:
                    df = df.sort_values(
                        by=sort_by,
                        ascending=(sort_order == "asc"),
                        na_position="last"
                    )
                except Exception as e:
                    logger.warning(f"Failed to sort historical data by {sort_by}: {e}")
            if rm:
                rm.t_sorting = (time.perf_counter() - t_sort_start) * 1000

            # Pagination (Lightweight: < 1ms, stays on Event Loop)
            t_pag_start = time.perf_counter()
            total_records = len(df)
            total_pages = math.ceil(total_records / page_size) if total_records > 0 else 1
            page = max(1, min(page, total_pages))
            
            start_idx = (page - 1) * page_size
            end_idx = start_idx + page_size
            paginated_df = df.iloc[start_idx:end_idx]
            if rm:
                rm.t_pagination = (time.perf_counter() - t_pag_start) * 1000

            # Serialization (Lightweight: < 10ms, stays on Event Loop)
            t_ser_start = time.perf_counter()
            records = paginated_df.to_dict(orient="records")
            
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
            if rm:
                rm.t_serialization = (time.perf_counter() - t_ser_start) * 1000
                rm.rows_returned = len(records)
            
            return records, meta

        except Exception as e:
            logger.exception(f"Error processing historical data: {e}")
            raise ValueError("Failed to process historical data file.") from e
        finally:
            if rm and token:
                rm.t_total = (time.perf_counter() - req_start) * 1000
                GlobalCacheStats.record_request_completion(rm)
                
                # Structured Logging
                log_level = logging.INFO
                if rm.t_total > 500:
                    log_level = logging.WARNING
                    slow_reason = max([
                        ("Sorting", rm.t_sorting),
                        ("Filtering", rm.t_filtering),
                        ("HEAD Validation", rm.t_head_val),
                        ("Parquet Read", rm.t_parquet_read)
                    ], key=lambda x: x[1])
                    logger.warning(f"WARNING\nSlow History Request\nTotal Time: {rm.t_total:.0f} ms\nLargest Contributor:\n{slow_reason[0]}\n{slow_reason[1]:.0f} ms")
                elif logger.isEnabledFor(logging.DEBUG):
                    log_level = logging.DEBUG
                
                if log_level >= logging.INFO:
                    head_hit = "HIT" if rm.head_cache_hit else ("MISS" if rm.head_cache_hit is False else "N/A")
                    df_hit = "HIT" if rm.df_cache_hit else ("MISS" if rm.df_cache_hit is False else "N/A")
                    coalesced = "YES" if rm.request_coalesced else ("NO" if rm.request_coalesced is False else "N/A")
                    dup_prevented = "YES" if rm.duplicate_read_prevented else ("NO" if rm.duplicate_read_prevented is False else "N/A")
                    summary = (
                        f"\n==================================================\n"
                        f"History Performance\n"
                        f"==================================================\n"
                        f"Request ID: {rm.request_id}\n"
                        f"History Date: {rm.target_date}\n"
                        f"Rows Loaded: {rm.rows_loaded}\n"
                        f"Rows Returned: {rm.rows_returned}\n\n"
                        f"HEAD Cache:\n{head_hit}\n\n"
                        f"DataFrame Cache:\n{df_hit}\n\n"
                        f"Request Coalesced:\n{coalesced}\n\n"
                        f"Wait Time:\n{rm.coalesce_wait_time:.0f} ms\n\n"
                        f"Duplicate Read Prevented:\n{dup_prevented}\n\n"
                        f"HEAD Validation:\n{rm.t_head_val:.0f} ms\n\n"
                        f"Parquet Read:\n{rm.t_parquet_read:.0f} ms\n\n"
                        f"Filtering:\n{rm.t_filtering:.0f} ms\n\n"
                        f"Sorting:\n{rm.t_sorting:.0f} ms\n\n"
                        f"Pagination:\n{rm.t_pagination:.0f} ms\n\n"
                        f"Serialization:\n{rm.t_serialization:.0f} ms\n\n"
                        f"Total:\n{rm.t_total:.0f} ms\n"
                        f"=================================================="
                    )
                    logger.log(log_level, summary)
                request_metrics.reset(token)

    async def get_historical_dataframe(
        self,
        request: Any = None,
    ) -> AdapterResult:
        """
        Executes a historical query via DuckDB for optimal performance.
        Returns an AdapterResult containing the paginated DataFrame and total_matched count.
        """
        if not request:
            raise ValueError("request is required for historical dataframe")

        target_date = request.date
        if not target_date or target_date == "today":
            target_date = datetime.now().strftime("%Y-%m-%d")

        await self._trigger_cleanup_if_needed()

        parquet_path = await self.cache_manager.get_or_download(target_date)
        if not parquet_path:
            raise ValueError(f"Historical data for {target_date} is unavailable.")

        try:
            # 1. Resolve conditions and generate SQL WHERE clause
            t0 = time.perf_counter()
            conditions = resolve_conditions(request)
            sql_where = translate_conditions(conditions)
            
            # Additional filtering for start/end time
            time_conditions = []
            if request.start_time:
                time_conditions.append(f'"Fetch Timestamp" >= \'{target_date}T{request.start_time}:00+05:30\'')
            if request.end_time:
                time_conditions.append(f'"Fetch Timestamp" <= \'{target_date}T{request.end_time}:00+05:30\'')
                
            if time_conditions:
                time_sql = " AND ".join(time_conditions)
                if sql_where == "1=1":
                    sql_where = time_sql
                else:
                    sql_where = f"({sql_where}) AND {time_sql}"
                
            t_sql_trans = (time.perf_counter() - t0) * 1000
                
            # 2. Sort parameters
            order_by = ""
            if request.sort_by:
                col = request.sort_by.replace("'", "''")
                order = "ASC" if request.sort_order == "asc" else "DESC"
                order_by = f'ORDER BY "{col}" {order} NULLS LAST'
                
            # 3. Pagination parameters
            page_size = min(request.page_size, 5000)
            offset = (request.page - 1) * page_size
            
            # Windows path handling for DuckDB
            safe_path = str(parquet_path).replace('\\', '/')
            table_ref = f"read_parquet('{safe_path}')"
            
            # Execute in a background thread to avoid blocking the asyncio loop
            def _execute_duckdb():
                timings = {}
                
                t_conn = time.perf_counter()
                conn = duckdb.connect(':memory:')
                timings["DuckDB Connect"] = (time.perf_counter() - t_conn) * 1000
                
                try:
                    # Get total rows in parquet
                    t_scan = time.perf_counter()
                    total_scanned = conn.execute(f"SELECT COUNT(*) FROM {table_ref}").fetchone()[0]
                    timings["Total Scanned Query"] = (time.perf_counter() - t_scan) * 1000
                    
                    # Get total matched for pagination metadata
                    t_count = time.perf_counter()
                    if sql_where == "1=1":
                        count_query = f"""
                            SELECT 
                                {total_scanned} as total_matched,
                                COUNT(CASE WHEN day_change_pct > 0 THEN 1 END) as bullish_count,
                                COUNT(CASE WHEN day_change_pct < 0 THEN 1 END) as bearish_count
                            FROM {table_ref}
                        """
                    else:
                        count_query = f"""
                            SELECT 
                                COUNT(*) as total_matched,
                                COUNT(CASE WHEN day_change_pct > 0 THEN 1 END) as bullish_count,
                                COUNT(CASE WHEN day_change_pct < 0 THEN 1 END) as bearish_count
                            FROM {table_ref}
                            WHERE {sql_where}
                        """
                    
                    count_result = conn.execute(count_query).fetchone()
                    total_matched = count_result[0]
                    bullish_count = count_result[1]
                    bearish_count = count_result[2]
                    timings["COUNT Query"] = (time.perf_counter() - t_count) * 1000
                    
                    # Fetch paginated dataframe (DuckDB Query + Materialization)
                    t_exec = time.perf_counter()
                    where_clause = "" if sql_where == "1=1" else f"WHERE {sql_where}"
                    data_query = f"SELECT * FROM {table_ref} {where_clause} {order_by} LIMIT {page_size} OFFSET {offset}"
                    logger.info(f"Generated DuckDB Query: {data_query}")
                    rel = conn.execute(data_query)
                    timings["DuckDB Query"] = (time.perf_counter() - t_exec) * 1000
                    
                    t_mat = time.perf_counter()
                    # Bypasses Pandas 3.0 pyarrow timezone conversion crash
                    df = rel.arrow().read_all().to_pandas(types_mapper=pd.ArrowDtype)
                    timings["Materialization"] = (time.perf_counter() - t_mat) * 1000
                    
                    return total_scanned, total_matched, bullish_count, bearish_count, df, timings
                finally:
                    conn.close()
                    
            total_scanned, total_matched, bullish_count, bearish_count, df, timings = await asyncio.to_thread(_execute_duckdb)
            
            # Inject translation time
            timings["SQL Translation"] = t_sql_trans
            
            # Return wrapped result so QueryEngine skips Pandas execution
            # We'll pass timings inside AdapterResult so engine can log the full picture
            return AdapterResult(
                df=df,
                is_pre_processed=True,
                matched_count=total_matched,
                total_scanned=total_scanned,
                timings=timings,
                bullish_count=bullish_count,
                bearish_count=bearish_count
            )
            
        except Exception as e:
            logger.exception(f"Error parsing parquet {parquet_path}: {e}")
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
            
            records = df_minute.to_dict(orient="records")
            for record in records:
                if isinstance(record.get(timestamp_col), pd.Timestamp):
                    record[timestamp_col] = record[timestamp_col].isoformat() + "Z"
                    
            return records

        except Exception as e:
            logger.error(f"Error generating timeline for {symbol}: {e}")
            return []

    async def list_available_dates(self) -> List[str]:
        """List all dates that have historical parquet files."""
        return await self._list_dates()

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
            # dtype_backend="pyarrow" avoids Pandas 3.0 tz_standardize crashes with PyArrow PyTZ conversion
            df = await asyncio.to_thread(pd.read_parquet, parquet_path, engine="pyarrow", dtype_backend="pyarrow")
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
