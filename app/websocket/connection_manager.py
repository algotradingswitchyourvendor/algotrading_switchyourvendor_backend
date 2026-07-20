"""
WebSocket Connection Manager.

Manages all active WebSocket connections, handles client messages,
and provides broadcast capabilities. Integrates with the LiveCache
to push delta updates when new market data arrives.

Design decisions:
- Single global manager instance (module-level singleton)
- asyncio.Lock for thread-safe connection tracking
- JSON serialization for messages (sufficient for single-server, ~16 KB/s)
- Heartbeat via background task to keep connections alive through Nginx
"""

import asyncio
import json
import logging
from typing import Optional

from fastapi import WebSocket
from starlette.websockets import WebSocketState

from app.config.holidays import get_market_status, is_market_open
from app.websocket.events import (
    WSClientAction,
    msg_connected,
    msg_heartbeat,
    msg_error,
    msg_snapshot_update,
    msg_market_closed,
)

logger = logging.getLogger(__name__)


class ConnectionManager:
    """
    Manages WebSocket client connections and message broadcasting.
    
    Lifecycle:
    1. Client connects → added to _connections set
    2. During market hours → receives snapshot_update messages
    3. Market closes → receives market_closed message
    4. Client disconnects → removed from _connections set
    """

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._scanner_subscriptions: dict[WebSocket, list[dict]] = {}
        self._lock = asyncio.Lock()
        self._heartbeat_task: Optional[asyncio.Task] = None

    @property
    def active_count(self) -> int:
        return len(self._connections)

    async def connect(self, websocket: WebSocket) -> None:
        """Accept a new WebSocket connection."""
        await websocket.accept()

        async with self._lock:
            self._connections.add(websocket)

        # Send connection acknowledgment
        from app.cache.live_cache import live_cache
        total = live_cache.get_total_count()
        status = get_market_status()

        await self._send(websocket, msg_connected(status, total))
        logger.info(
            f"WebSocket client connected. Active: {self.active_count}"
        )

        # Start heartbeat if not running
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    def disconnect(self, websocket: WebSocket) -> None:
        """Remove a disconnected client."""
        self._connections.discard(websocket)
        self._scanner_subscriptions.pop(websocket, None)
        logger.info(
            f"WebSocket client disconnected. Active: {self.active_count}"
        )

    async def handle_client_message(self, websocket: WebSocket, raw: str) -> None:
        """Process incoming message from a client."""
        try:
            data = json.loads(raw)
            action = data.get("type", "")

            if action == WSClientAction.PING:
                await self._send(websocket, {"type": "pong"})

            elif action == WSClientAction.SUBSCRIBE_SCANNER:
                conditions = data.get("conditions", [])
                self._scanner_subscriptions[websocket] = conditions
                logger.info(
                    f"Client subscribed to scanner with {len(conditions)} conditions"
                )

            elif action == WSClientAction.UNSUBSCRIBE_SCANNER:
                self._scanner_subscriptions.pop(websocket, None)
                logger.info("Client unsubscribed from scanner")

        except json.JSONDecodeError:
            await self._send(
                websocket,
                msg_error("INVALID_MESSAGE", "Message must be valid JSON"),
            )
        except Exception as e:
            logger.error(f"Error handling client message: {e}")

    async def broadcast_snapshot_update(
        self, changed_rows: list[dict], snapshot_id: int, total_instruments: int
    ) -> None:
        """
        Broadcast delta update to all connected clients.
        
        Called by the publisher when LiveCache is updated.
        Only sends during market hours.
        """
        if not self._connections:
            return

        if not is_market_open():
            # Send market_closed once, then stop broadcasting
            from app.cache.live_cache import live_cache
            last_updated = live_cache.last_updated
            last_time = last_updated.isoformat() if last_updated else None
            message = msg_market_closed(last_time)
        else:
            message = msg_snapshot_update(
                changed_rows=changed_rows,
                snapshot_id=snapshot_id,
                total_instruments=total_instruments,
            )

        await self._broadcast(message)

    async def broadcast_scanner_updates(self) -> None:
        """
        Evaluate scanner conditions for subscribed clients and push delta changes.
        
        This runs after each snapshot update for clients with active scanner subscriptions.
        """
        if not self._scanner_subscriptions:
            return

        from app.cache.live_cache import live_cache
        from app.services.scanner_service import evaluate_scanner

        for ws, conditions in list(self._scanner_subscriptions.items()):
            if ws not in self._connections:
                self._scanner_subscriptions.pop(ws, None)
                continue

            try:
                records, meta = evaluate_scanner(
                    cache=live_cache,
                    conditions=conditions,
                    mode="live",
                    page=1,
                    page_size=500,
                )
                # Full result set is sent each cycle. Scanner conditions can produce
                # entirely different result sets between snapshots, making row-level
                # delta tracking unreliable without per-client state caching.
                # For scanner result sets (typically <500 rows), this is optimal.
                await self._send(ws, {
                    "type": "scanner_update",
                    "data": records,
                    "meta": meta,
                })
            except Exception as e:
                logger.error(f"Scanner broadcast error: {e}")

    async def _broadcast(self, message: dict) -> None:
        """Send a message to all connected clients."""
        disconnected = set()

        for ws in self._connections.copy():
            try:
                await self._send(ws, message)
            except Exception:
                disconnected.add(ws)

        # Clean up disconnected clients
        for ws in disconnected:
            self.disconnect(ws)

    async def _send(self, websocket: WebSocket, message: dict) -> None:
        """Send a message to a single client."""
        try:
            if websocket.client_state == WebSocketState.CONNECTED:
                await websocket.send_json(message)
        except Exception as e:
            logger.debug(f"Failed to send to client: {e}")
            raise

    async def _heartbeat_loop(self) -> None:
        """Send periodic heartbeats to keep connections alive through Nginx."""
        while self._connections:
            await asyncio.sleep(30)
            if self._connections:
                await self._broadcast(msg_heartbeat())

    async def close_all(self) -> None:
        """Close all active connections (used during shutdown)."""
        for ws in self._connections.copy():
            try:
                await ws.close()
            except Exception:
                pass
        self._connections.clear()
        self._scanner_subscriptions.clear()


# ── Module-level singleton ──────────────────────────────────────────────
manager = ConnectionManager()
