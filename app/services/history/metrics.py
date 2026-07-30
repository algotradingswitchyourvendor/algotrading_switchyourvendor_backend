import threading
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class RequestMetrics:
    """Per-request timings and cache state."""
    request_id: str = ""
    target_date: str = ""
    rows_loaded: int = 0
    rows_returned: int = 0
    
    # Cache states
    head_cache_hit: Optional[bool] = None
    df_cache_hit: Optional[bool] = None
    
    # Coalescing state
    request_coalesced: Optional[bool] = None
    coalesce_wait_time: float = 0.0
    duplicate_read_prevented: Optional[bool] = None
    
    # Timings in ms
    t_head_val: float = 0.0
    t_parquet_download: float = 0.0
    t_parquet_read: float = 0.0
    t_filtering: float = 0.0
    t_sorting: float = 0.0
    t_pagination: float = 0.0
    t_serialization: float = 0.0
    t_total: float = 0.0

# Context variable to hold metrics for the current request
request_metrics: ContextVar[Optional[RequestMetrics]] = ContextVar("request_metrics", default=None)

class _GlobalCacheStats:
    """Thread-safe singleton for global cache statistics."""
    def __init__(self):
        self._lock = threading.Lock()
        
        self.head_hits = 0
        self.head_misses = 0
        
        self.df_hits = 0
        self.df_misses = 0
        self.df_evictions = 0
        
        # Coalescing counters
        self.coalesced_requests = 0
        self.duplicate_reads_prevented = 0
        self.total_wait_time_ms = 0.0
        self.peak_concurrent = 0
        
        self.total_requests = 0
        self.total_response_time_ms = 0.0
        self.total_cold_time_ms = 0.0
        self.cold_requests = 0
        self.total_warm_time_ms = 0.0
        self.warm_requests = 0
        self.total_sort_time_ms = 0.0
        self.sort_requests = 0
        self.total_filter_time_ms = 0.0
        self.filter_requests = 0

    def record_head_result(self, hit: bool):
        with self._lock:
            if hit:
                self.head_hits += 1
            else:
                self.head_misses += 1

    def record_df_result(self, hit: bool):
        with self._lock:
            if hit:
                self.df_hits += 1
            else:
                self.df_misses += 1

    def record_df_eviction(self):
        with self._lock:
            self.df_evictions += 1

    def record_coalesce_result(self, coalesced: bool, wait_time_ms: float = 0.0):
        with self._lock:
            if coalesced:
                self.coalesced_requests += 1
                self.total_wait_time_ms += wait_time_ms
                self.duplicate_reads_prevented += 1

    def update_peak_concurrent(self, current: int):
        with self._lock:
            self.peak_concurrent = max(self.peak_concurrent, current)

    def record_request_completion(self, metrics: RequestMetrics):
        with self._lock:
            self.total_requests += 1
            self.total_response_time_ms += metrics.t_total
            
            # Cold vs Warm request logic
            if metrics.df_cache_hit:
                self.warm_requests += 1
                self.total_warm_time_ms += metrics.t_total
            else:
                self.cold_requests += 1
                self.total_cold_time_ms += metrics.t_total

            if metrics.t_sorting > 0:
                self.sort_requests += 1
                self.total_sort_time_ms += metrics.t_sorting

            if metrics.t_filtering > 0:
                self.filter_requests += 1
                self.total_filter_time_ms += metrics.t_filtering

    def get_stats(self) -> dict:
        with self._lock:
            head_total = self.head_hits + self.head_misses
            head_ratio = (self.head_hits / head_total * 100) if head_total > 0 else 0.0

            df_total = self.df_hits + self.df_misses
            df_ratio = (self.df_hits / df_total * 100) if df_total > 0 else 0.0

            avg_resp = (self.total_response_time_ms / self.total_requests) if self.total_requests > 0 else 0.0
            avg_cold = (self.total_cold_time_ms / self.cold_requests) if self.cold_requests > 0 else 0.0
            avg_warm = (self.total_warm_time_ms / self.warm_requests) if self.warm_requests > 0 else 0.0
            avg_sort = (self.total_sort_time_ms / self.sort_requests) if self.sort_requests > 0 else 0.0
            avg_filter = (self.total_filter_time_ms / self.filter_requests) if self.filter_requests > 0 else 0.0
            avg_wait = (self.total_wait_time_ms / self.coalesced_requests) if self.coalesced_requests > 0 else 0.0

            return {
                "head_hit_ratio_pct": head_ratio,
                "df_hit_ratio_pct": df_ratio,
                "total_requests": self.total_requests,
                "avg_response_ms": avg_resp,
                "avg_cold_ms": avg_cold,
                "avg_warm_ms": avg_warm,
                "avg_sort_ms": avg_sort,
                "avg_filter_ms": avg_filter,
                "df_evictions": self.df_evictions,
                "coalesced_requests": self.coalesced_requests,
                "duplicate_reads_prevented": self.duplicate_reads_prevented,
                "avg_wait_ms": avg_wait,
                "peak_concurrent": self.peak_concurrent
            }

GlobalCacheStats = _GlobalCacheStats()
