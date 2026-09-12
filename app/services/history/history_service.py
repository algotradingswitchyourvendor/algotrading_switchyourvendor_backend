import asyncio
import logging
import math
import os
import time
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import duckdb
import pandas as pd

from app.config.settings import get_settings
from app.services.history.cache_manager import CacheManager
from app.services.history.metrics import request_metrics, GlobalCacheStats, RequestMetrics
from app.services.column_service import _infer_group, DEFAULT_VISIBLE
from app.services.query_engine.sql_translator import translate_conditions
from app.services.query_engine.engine import resolve_conditions
from app.services.query_engine.adapters import AdapterResult

logger = logging.getLogger(__name__)


# Base columns for explicit projection in historical data
PROJECTION_COLUMNS = [
    "instrument_key", "Symbol", "Instrument", "trading_symbol", "exchange",
    "Open", "High", "Low", "Close", "Last Price", "Average Price", "Volume",
    "Net Change", "day_change_pct", "Total Buy Quantity", "Total Sell Quantity",
    "Fetch Timestamp"
]
PROJECTION_SQL = ", ".join(f'"{c}"' for c in PROJECTION_COLUMNS)

# Shared Concurrency Controls
_heavy_query_semaphore = asyncio.Semaphore(2)
_global_db_conn = None
_init_lock = threading.Lock()

def _create_configured_duckdb():
    temp_dir = '/tmp/duckdb_tmp'
    os.makedirs(temp_dir, exist_ok=True)
    c = duckdb.connect(':memory:')
    c.execute("PRAGMA threads=1")
    c.execute("PRAGMA memory_limit='1.5GB'")
    c.execute(f"PRAGMA temp_directory='{temp_dir}'")
    c.execute("PRAGMA max_temp_directory_size='2GB'")
    c.execute("PRAGMA preserve_insertion_order=false")
    return c

def _init_duckdb_structures():
    global _global_db_conn
    with _init_lock:
        if _global_db_conn is None:
            _global_db_conn = _create_configured_duckdb()
            logger.info("Initialized global DuckDB connection (threads=1, mem=1.5GB, temp=/tmp/duckdb_tmp)")

_init_duckdb_structures()

class DuckDBSession:
    def __init__(self):
        self.cursor = None

    def __enter__(self):
        self.cursor = _global_db_conn.cursor()
        return self.cursor

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.cursor is not None:
            self.cursor.close()

class HistoryService:
    def __init__(self):
        self.settings = get_settings()
        self.cache_manager = CacheManager()
        self._last_cleanup = 0.0
        self._column_mapping_cache: Dict[str, Dict[str, str]] = {}
        _init_duckdb_structures()

    def _get_logical_column(self, df_columns: list[str], target_group: str) -> Optional[str]:
        schema_key = str(hash(tuple(df_columns)))
        if schema_key not in self._column_mapping_cache:
            self._column_mapping_cache[schema_key] = {}
        if target_group in self._column_mapping_cache[schema_key]:
            return self._column_mapping_cache[schema_key][target_group]
        for col in df_columns:
            if _infer_group(col) == target_group:
                self._column_mapping_cache[schema_key][target_group] = col
                return col
        self._column_mapping_cache[schema_key][target_group] = None
        return None

    async def _trigger_cleanup_if_needed(self):
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

            def _execute_duckdb():
                with DuckDBSession() as cursor:
                    safe_path = str(parquet_path).replace('\\', '/')
                    table_ref = f"read_parquet('{safe_path}')"
                    
                    # Inspect schema without full scan
                    desc = cursor.execute(f"DESCRIBE SELECT * FROM {table_ref}").fetchall()
                    cols = [c[0] for c in desc]
                    symbol_col = self._get_logical_column(cols, "Identity") or "trading_symbol"
                    timestamp_col = "Fetch Timestamp" if "Fetch Timestamp" in cols else self._get_logical_column(cols, "Metadata")
                    
                    where_clauses = []
                    params = []
                    
                    if symbol:
                        where_clauses.append(f'"{symbol_col}" ILIKE ?')
                        params.append(f"%{symbol.strip()}%")
                    
                    if timestamp_col:
                        if start_time:
                            where_clauses.append(f'CAST("{timestamp_col}" AS TIME) >= CAST(? AS TIME)')
                            params.append(f"{start_time}:00")
                        if end_time:
                            where_clauses.append(f'CAST("{timestamp_col}" AS TIME) <= CAST(? AS TIME)')
                            params.append(f"{end_time}:00")
                            
                    where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"
                    
                    # Count Query
                    t_scan = time.perf_counter()
                    count_query = f"SELECT COUNT(*) FROM {table_ref} WHERE {where_sql}"
                    total_records = cursor.execute(count_query, params).fetchone()[0]
                    if rm:
                        rm.t_filtering = (time.perf_counter() - t_scan) * 1000
                    
                    total_pages = math.ceil(total_records / page_size) if total_records > 0 else 1
                    page_safe = max(1, min(page, total_pages))
                    offset = (page_safe - 1) * page_size
                    
                    # Sorting
                    order_sql = ""
                    if sort_by and sort_by in cols:
                        direction = "ASC" if sort_order == "asc" else "DESC"
                        order_sql = f'ORDER BY "{sort_by}" {direction} NULLS LAST'
                    elif timestamp_col:
                        order_sql = f'ORDER BY "{timestamp_col}" ASC NULLS LAST'
                        
                    # Projection
                    available_proj = [c for c in PROJECTION_COLUMNS if c in cols]
                    select_cols = ", ".join(f'"{c}"' for c in available_proj) if available_proj else "*"
                        
                    data_query = f"SELECT {select_cols} FROM {table_ref} WHERE {where_sql} {order_sql} LIMIT ? OFFSET ?"
                    fetch_params = params + [page_size, offset]
                    
                    t_exec = time.perf_counter()
                    rel = cursor.execute(data_query, fetch_params)
                    
                    # Read to arrow / dict
                    t_ser = time.perf_counter()
                    # To dict via Arrow avoids Pandas 3.0 tz issues and saves memory
                    arrow_table = rel.arrow().read_all()
                    records = arrow_table.to_pylist()
                    
                    if rm:
                        rm.t_parquet_read = (t_ser - t_exec) * 1000
                        rm.rows_loaded = len(records)
                        rm.t_serialization = (time.perf_counter() - t_ser) * 1000
                        rm.rows_returned = len(records)
                        
                    return records, total_records, total_pages, page_safe, timestamp_col

            async with _heavy_query_semaphore:
                records, total_records, total_pages, page_safe, timestamp_col = await asyncio.to_thread(_execute_duckdb)

            # Format timestamp strings if needed
            if timestamp_col:
                for record in records:
                    val = record.get(timestamp_col)
                    if hasattr(val, "isoformat"):
                        record[timestamp_col] = val.isoformat() + "Z"
            
            meta = {
                "current_page": page_safe,
                "page_size": page_size,
                "total_pages": total_pages,
                "total_records": total_records,
                "has_next": page_safe < total_pages,
                "has_previous": page_safe > 1,
            }
            return records, meta

        except Exception as e:
            logger.exception(f"Error processing historical data: {e}")
            raise ValueError("Failed to process historical data file.") from e
        finally:
            if rm and token:
                rm.t_total = (time.perf_counter() - req_start) * 1000
                GlobalCacheStats.record_request_completion(rm)
                request_metrics.reset(token)

    async def get_historical_dataframe(self, request: Any = None) -> AdapterResult:
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
            t0 = time.perf_counter()
            conditions = resolve_conditions(request)
            sql_where = translate_conditions(conditions)
            
            time_conditions = []
            if request.start_time:
                time_conditions.append(f'"Fetch Timestamp" >= \'{target_date}T{request.start_time}:00+05:30\'')
            if request.end_time:
                time_conditions.append(f'"Fetch Timestamp" <= \'{target_date}T{request.end_time}:00+05:30\'')
                
            if time_conditions:
                time_sql = " AND ".join(time_conditions)
                sql_where = time_sql if sql_where == "1=1" else f"({sql_where}) AND {time_sql}"
                
            t_sql_trans = (time.perf_counter() - t0) * 1000
            
            order_by = ""
            if request.sort_by:
                col = request.sort_by.replace("'", "''")
                order = "ASC" if request.sort_order == "asc" else "DESC"
                order_by = f'ORDER BY "{col}" {order} NULLS LAST'
                
            page_size = min(request.page_size, 5000)
            offset = (request.page - 1) * page_size
            
            safe_path = str(parquet_path).replace('\\', '/')
            table_ref = f"read_parquet('{safe_path}')"
            
            def _execute_duckdb():
                timings = {}
                with DuckDBSession() as cursor:
                    t_scan = time.perf_counter()
                    total_scanned = cursor.execute(f"SELECT COUNT(*) FROM {table_ref}").fetchone()[0]
                    timings["Total Scanned Query"] = (time.perf_counter() - t_scan) * 1000
                    
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
                    
                    count_result = cursor.execute(count_query).fetchone()
                    total_matched = count_result[0]
                    bullish_count = count_result[1]
                    bearish_count = count_result[2]
                    timings["COUNT Query"] = (time.perf_counter() - t_count) * 1000
                    
                    t_exec = time.perf_counter()
                    where_clause = "" if sql_where == "1=1" else f"WHERE {sql_where}"
                    
                    select_cols = PROJECTION_SQL
                    data_query = f"SELECT {select_cols} FROM {table_ref} {where_clause} {order_by} LIMIT ? OFFSET ?"
                    rel = cursor.execute(data_query, [page_size, offset])
                    timings["DuckDB Query"] = (time.perf_counter() - t_exec) * 1000
                    
                    t_mat = time.perf_counter()
                    df = rel.arrow().read_all().to_pandas(types_mapper=pd.ArrowDtype)
                    timings["Materialization"] = (time.perf_counter() - t_mat) * 1000
                    
                    return total_scanned, total_matched, bullish_count, bearish_count, df, timings

            async with _heavy_query_semaphore:
                total_scanned, total_matched, bullish_count, bearish_count, df, timings = await asyncio.to_thread(_execute_duckdb)
                
            timings["SQL Translation"] = t_sql_trans
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
        if target_date == "today":
            target_date = datetime.now().strftime("%Y-%m-%d")

        parquet_path = await self.cache_manager.get_or_download(target_date)
        if not parquet_path:
            return []

        try:
            def _execute_timeline():
                with DuckDBSession() as cursor:
                    safe_path = str(parquet_path).replace('\\', '/')
                    table_ref = f"read_parquet('{safe_path}')"
                    
                    desc = cursor.execute(f"DESCRIBE SELECT * FROM {table_ref}").fetchall()
                    cols = [c[0] for c in desc]
                    symbol_col = self._get_logical_column(cols, "Identity") or "trading_symbol"
                    timestamp_col = "Fetch Timestamp" if "Fetch Timestamp" in cols else self._get_logical_column(cols, "Metadata")
                    
                    if not symbol_col or not timestamp_col:
                        return pd.DataFrame(), timestamp_col
                        
                    timeline_cols = ["Open", "High", "Low", "Close", "Volume", "day_change_pct", timestamp_col, symbol_col]
                    available_cols = [c for c in timeline_cols if c in cols]
                    select_sql = ", ".join(f'"{c}"' for c in available_cols)
                    
                    data_query = f"""
                        SELECT {select_sql} FROM {table_ref} 
                        WHERE "{symbol_col}" ILIKE ?
                        ORDER BY "{timestamp_col}" ASC
                    """
                    df = cursor.execute(data_query, [f"%{symbol.strip()}%"]).arrow().read_all().to_pandas(types_mapper=pd.ArrowDtype)
                    return df, timestamp_col

            async with _heavy_query_semaphore:
                df, timestamp_col = await asyncio.to_thread(_execute_timeline)

            if df.empty or not timestamp_col:
                return []

            if not pd.api.types.is_datetime64_any_dtype(df[timestamp_col]):
                unit = "ms" if type(df[timestamp_col].iloc[0]) in (int, float) else None
                df[timestamp_col] = pd.to_datetime(df[timestamp_col], unit=unit)

            df_minute = df.set_index(timestamp_col).resample("1Min").last()
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
        return await self._list_dates()

    async def get_schema_dataframe(self, target_date: str) -> Optional[pd.DataFrame]:
        try:
            parquet_path = await self.cache_manager.get_or_download(target_date)
            def _execute_schema():
                with DuckDBSession() as cursor:
                    safe_path = str(parquet_path).replace('\\', '/')
                    table_ref = f"read_parquet('{safe_path}')"
                    return cursor.execute(f"SELECT * FROM {table_ref} LIMIT 1").arrow().read_all().to_pandas(types_mapper=pd.ArrowDtype)
            return await asyncio.to_thread(_execute_schema)
        except Exception as e:
            logger.error(f"Failed to load schema for {target_date}: {e}")
            return None

    async def _list_dates(self) -> List[str]:
        try:
            import boto3
            s3_client = boto3.client("s3")
            response = await asyncio.to_thread(
                s3_client.list_objects_v2,
                Bucket=self.settings.S3_BUCKET_NAME,
                Prefix=f"{self.settings.S3_PARQUET_PREFIX}/",
            )
            dates = set()
            for obj in response.get("Contents", []):
                key = obj["Key"]
                if "/date=" in key and key.endswith(".parquet"):
                    parts = key.split("/")
                    for p in parts:
                        if p.startswith("date="):
                            dates.add(p.replace("date=", ""))
            return sorted(list(dates), reverse=True)
        except Exception as e:
            logger.error(f"Failed to list available dates: {e}")
            return []
