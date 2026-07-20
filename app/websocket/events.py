"""
WebSocket event types and message models.

Defines all message types exchanged between server and client.
Using string constants and factory functions instead of Pydantic 
for WebSocket messages to minimize serialization overhead.
"""

from datetime import datetime
from typing import Any, Optional
import pytz

IST = pytz.timezone("Asia/Kolkata")


# ── Server → Client Message Types ──────────────────────────────────────

class WSMessageType:
    CONNECTED = "connected"
    SNAPSHOT_UPDATE = "snapshot_update"
    MARKET_CLOSED = "market_closed"
    SCANNER_UPDATE = "scanner_update"
    HEARTBEAT = "heartbeat"
    ERROR = "error"


# ── Client → Server Message Types ──────────────────────────────────────

class WSClientAction:
    SUBSCRIBE_SCANNER = "subscribe_scanner"
    UNSUBSCRIBE_SCANNER = "unsubscribe_scanner"
    PING = "ping"


# ── Message Factories ──────────────────────────────────────────────────

def msg_connected(market_status: str, total_instruments: int) -> dict:
    """Initial connection acknowledgment."""
    return {
        "type": WSMessageType.CONNECTED,
        "market_status": market_status,
        "total_instruments": total_instruments,
        "timestamp": _now(),
    }


def msg_snapshot_update(
    changed_rows: list[dict],
    snapshot_id: int,
    total_instruments: int,
) -> dict:
    """Delta update containing only changed rows."""
    return {
        "type": WSMessageType.SNAPSHOT_UPDATE,
        "changed_rows": changed_rows,
        "snapshot_id": snapshot_id,
        "total_changed": len(changed_rows),
        "total_instruments": total_instruments,
        "timestamp": _now(),
    }


def msg_market_closed(last_snapshot_time: Optional[str] = None) -> dict:
    """Market has closed, no more updates will be sent."""
    return {
        "type": WSMessageType.MARKET_CLOSED,
        "last_snapshot_time": last_snapshot_time or _now(),
        "timestamp": _now(),
    }


def msg_scanner_update(
    added: list[dict],
    removed: list[str],
    updated: list[dict],
) -> dict:
    """Scanner results have changed."""
    return {
        "type": WSMessageType.SCANNER_UPDATE,
        "added": added,
        "removed": removed,
        "updated": updated,
        "timestamp": _now(),
    }


def msg_heartbeat() -> dict:
    """Keepalive heartbeat."""
    return {
        "type": WSMessageType.HEARTBEAT,
        "timestamp": _now(),
    }


def msg_error(code: str, message: str) -> dict:
    """Error message."""
    return {
        "type": WSMessageType.ERROR,
        "code": code,
        "message": message,
        "timestamp": _now(),
    }


def _now() -> str:
    return datetime.now(IST).isoformat()
