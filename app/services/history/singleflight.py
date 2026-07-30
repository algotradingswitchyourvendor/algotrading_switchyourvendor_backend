"""
SingleFlight: Request Coalescing for Concurrent Identical Requests

Implements the SingleFlight pattern (inspired by Go's singleflight package)
for both async (asyncio) and sync (threading) contexts.

When multiple callers request the same key simultaneously:
- The FIRST caller (leader) executes the expensive work.
- All SUBSEQUENT callers (followers) await the same result.
- On completion, the result is shared and the registry entry is cleaned up.
- On failure, the exception is propagated to ALL waiters.

Design Principles:
- Locks are ONLY held during registry dict manipulation (nanoseconds).
- Locks are NEVER held during expensive I/O (S3, PyArrow, Pandas).
- Cleanup is ALWAYS in a finally block — no stale entries possible.
- Per-key granularity — different keys execute independently.
"""

import asyncio
import logging
import threading
import time
from typing import Any, Callable, Coroutine, Dict, Optional, Tuple, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class SingleFlight:
    """
    Async-native SingleFlight for use within asyncio event loops.

    Used by CacheManager.get_or_download to coalesce concurrent
    S3 HEAD + download requests for the same target_date.
    """

    def __init__(self):
        # Registry of in-flight operations: key → asyncio.Future
        self._inflight: Dict[str, asyncio.Future] = {}
        # Lightweight lock — only protects dict reads/writes, never held during I/O
        self._lock = asyncio.Lock()
        # Observability
        self._current_inflight = 0
        self._peak_concurrent = 0
        self._obs_lock = threading.Lock()

    @property
    def current_inflight(self) -> int:
        return self._current_inflight

    @property
    def peak_concurrent(self) -> int:
        return self._peak_concurrent

    async def do(self, key: str, fn: Callable[[], Coroutine[Any, Any, T]]) -> Tuple[T, bool]:
        """
        Execute fn() for the given key, coalescing concurrent calls.

        Returns:
            (result, was_coalesced):
                result — the return value of fn()
                was_coalesced — True if this caller waited on another's result

        Guarantees:
            - Only one fn() executes per key at any time.
            - All concurrent callers for the same key receive the same result.
            - On exception, all waiters receive the same exception.
            - The registry entry is always cleaned up in a finally block.
        """
        # Step 1: Check registry (lock held only for dict lookup)
        existing_future = None
        async with self._lock:
            if key in self._inflight:
                existing_future = self._inflight[key]

        # Follower path: a leader is already running — await its result
        if existing_future is not None:
            wait_start = time.perf_counter()
            try:
                result = await asyncio.shield(existing_future)
                wait_ms = (time.perf_counter() - wait_start) * 1000
                logger.debug(f"SingleFlight coalesced: key={key}, wait={wait_ms:.1f}ms")
                return result, True
            except Exception:
                # Leader failed — propagate the same exception to this follower
                raise

        # Leader path: create a Future and register it
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        async with self._lock:
            # Double-check: another coroutine may have registered between our check and now
            if key in self._inflight:
                existing_future = self._inflight[key]
            else:
                self._inflight[key] = future
                with self._obs_lock:
                    self._current_inflight += 1
                    self._peak_concurrent = max(self._peak_concurrent, self._current_inflight)

        # If we lost the race, become a follower
        if existing_future is not None:
            wait_start = time.perf_counter()
            try:
                result = await asyncio.shield(existing_future)
                wait_ms = (time.perf_counter() - wait_start) * 1000
                logger.debug(f"SingleFlight coalesced (race): key={key}, wait={wait_ms:.1f}ms")
                return result, True
            except Exception:
                raise

        # We are the leader — execute fn() WITHOUT holding any lock
        try:
            result = await fn()
            future.set_result(result)
            return result, False
        except Exception as exc:
            future.set_exception(exc)
            raise
        finally:
            # Guaranteed cleanup — no stale futures
            async with self._lock:
                self._inflight.pop(key, None)
            with self._obs_lock:
                self._current_inflight -= 1


class SingleFlightSync:
    """
    Thread-safe SingleFlight for use within synchronous thread pools.

    Used by HistoryService._load_and_filter_parquet to coalesce concurrent
    pd.read_parquet calls for the same parquet file.

    Implementation uses threading.Event for zero-spin waiting.
    """

    def __init__(self):
        # Registry: key → _SyncFlight
        self._inflight: Dict[str, "_SyncFlight"] = {}
        # Lightweight lock — only protects dict reads/writes
        self._lock = threading.Lock()
        # Observability
        self._current_inflight = 0
        self._peak_concurrent = 0

    @property
    def current_inflight(self) -> int:
        return self._current_inflight

    @property
    def peak_concurrent(self) -> int:
        return self._peak_concurrent

    def do(self, key: str, fn: Callable[[], T]) -> Tuple[T, bool]:
        """
        Execute fn() for the given key, coalescing concurrent calls.

        Returns:
            (result, was_coalesced):
                result — the return value of fn()
                was_coalesced — True if this caller waited on another's result
        """
        with self._lock:
            if key in self._inflight:
                flight = self._inflight[key]
                is_follower = True
            else:
                flight = _SyncFlight()
                self._inflight[key] = flight
                self._current_inflight += 1
                self._peak_concurrent = max(self._peak_concurrent, self._current_inflight)
                is_follower = False

        if is_follower:
            # Follower path: wait for leader WITHOUT holding any lock
            wait_start = time.perf_counter()
            flight.event.wait()  # Blocks until leader completes (zero-spin)
            wait_ms = (time.perf_counter() - wait_start) * 1000

            if flight.exception is not None:
                logger.debug(f"SingleFlightSync follower received exception: key={key}")
                raise flight.exception

            logger.debug(f"SingleFlightSync coalesced: key={key}, wait={wait_ms:.1f}ms")
            return flight.result, True

        # Leader path: execute fn() WITHOUT holding any lock
        try:
            result = fn()
            flight.result = result
            return result, False
        except Exception as exc:
            flight.exception = exc
            raise
        finally:
            # Guaranteed cleanup — no stale entries
            flight.event.set()  # Wake all followers
            with self._lock:
                self._inflight.pop(key, None)
                self._current_inflight -= 1


class _SyncFlight:
    """Internal state holder for a single in-flight synchronous operation."""
    __slots__ = ("event", "result", "exception")

    def __init__(self):
        self.event = threading.Event()
        self.result: Any = None
        self.exception: Optional[Exception] = None
