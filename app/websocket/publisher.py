"""
WebSocket Publisher — Bridges LiveCache updates to WebSocket broadcasts.

When the scheduler updates the LiveCache, the publisher detects changes 
and pushes delta updates to all connected WebSocket clients.

This runs in the async event loop, receiving notifications from the 
synchronous scheduler thread via asyncio.run_coroutine_threadsafe.
"""

import asyncio
import logging
from typing import Optional

from app.cache.live_cache import live_cache
from app.websocket.connection_manager import manager

logger = logging.getLogger(__name__)

# Reference to the running event loop (set during app startup)
_event_loop: Optional[asyncio.AbstractEventLoop] = None


def set_event_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Store reference to the main async event loop."""
    global _event_loop
    _event_loop = loop


def on_cache_updated(changed_rows: list[dict]) -> None:
    """
    Callback invoked by LiveCache.update() from the scheduler thread.
    
    Since the scheduler runs in a background thread but WebSocket 
    operations are async, we bridge the gap using 
    asyncio.run_coroutine_threadsafe.
    """
    if not _event_loop or _event_loop.is_closed():
        return

    if not changed_rows:
        return

    if manager.active_count == 0:
        return

    try:
        snapshot_id = live_cache.snapshot_id
        total = live_cache.get_total_count()

        # Schedule the async broadcast on the event loop
        future = asyncio.run_coroutine_threadsafe(
            _async_publish(changed_rows, snapshot_id, total),
            _event_loop,
        )
        # Don't block waiting for result
    except Exception as e:
        logger.error(f"Failed to schedule WebSocket broadcast: {e}")


async def _async_publish(
    changed_rows: list[dict], snapshot_id: int, total_instruments: int
) -> None:
    """Async coroutine that performs the actual WebSocket broadcast."""
    try:
        await manager.broadcast_snapshot_update(
            changed_rows=changed_rows,
            snapshot_id=snapshot_id,
            total_instruments=total_instruments,
        )

        # Also update scanner subscribers
        await manager.broadcast_scanner_updates()

        logger.debug(
            f"WS broadcast: {len(changed_rows)} changed rows, "
            f"snapshot_id={snapshot_id}, clients={manager.active_count}"
        )
    except Exception as e:
        logger.error(f"WebSocket broadcast failed: {e}")
