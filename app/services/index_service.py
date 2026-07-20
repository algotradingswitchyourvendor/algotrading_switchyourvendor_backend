"""
Index Service — Fetches market index data (NIFTY, SENSEX, etc.)

These values are NOT part of the main equity DataFrame.
They are fetched independently via Upstox API for the Market Summary Bar.
Results are cached for 60 seconds to avoid excessive API calls.
"""

import logging
import time
from datetime import datetime
from typing import Optional

import requests
import pytz

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

# ── Index Instrument Keys ───────────────────────────────────────────────
INDEX_INSTRUMENTS = {
    "NIFTY 50": "NSE_INDEX|Nifty 50",
    "SENSEX": "BSE_INDEX|SENSEX",
    "BANK NIFTY": "NSE_INDEX|Nifty Bank",
    "MIDCAP": "NSE_INDEX|NIFTY MIDCAP 100",
    "FINNIFTY": "NSE_INDEX|Nifty Fin Service",
    "INDIA VIX": "NSE_INDEX|India VIX",
}

# ── Cache ───────────────────────────────────────────────────────────────
_index_cache: Optional[list[dict]] = None
_cache_timestamp: float = 0
CACHE_TTL = 60  # seconds


def get_index_data(access_token: Optional[str] = None) -> list[dict]:
    """
    Fetch current values for market indices.

    Returns cached data if within TTL (60s).
    Falls back to last known values if API call fails.
    """
    global _index_cache, _cache_timestamp

    # Return cache if fresh
    if _index_cache and (time.time() - _cache_timestamp) < CACHE_TTL:
        return _index_cache

    if not access_token:
        logger.warning("No access token for index data, returning cache or empty")
        return _index_cache or _get_fallback_indices()

    try:
        import urllib.parse

        keys = ",".join(
            urllib.parse.quote(v) for v in INDEX_INSTRUMENTS.values()
        )
        url = f"https://api.upstox.com/v2/market-quote/quotes?instrument_key={keys}"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
        }

        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json().get("data", {})

        # Build a normalized lookup: decode URL-encoded keys from response
        # Upstox may return "NSE_INDEX|Nifty 50" or "NSE_INDEX%7CNifty+50"
        normalized_data: dict = {}
        for key, value in data.items():
            decoded_key = urllib.parse.unquote(key).replace("+", " ")
            normalized_data[decoded_key] = value
            normalized_data[key] = value  # also keep original

        # Build a previous-cache map for fallback values
        prev_cache_map: dict = {}
        if _index_cache:
            for item in _index_cache:
                prev_cache_map[item["name"]] = item

        indices = []
        for display_name, instrument_key in INDEX_INSTRUMENTS.items():
            # Try original key, then decoded key
            decoded_instrument_key = urllib.parse.unquote(instrument_key).replace("+", " ")
            details = (
                normalized_data.get(instrument_key)
                or normalized_data.get(decoded_instrument_key)
                or {}
            )

            if not details:
                # Use last known cached value to prevent index from disappearing
                prev = prev_cache_map.get(display_name)
                if prev:
                    logger.debug(
                        f"Index '{display_name}' missing from API response, "
                        f"using cached value"
                    )
                    indices.append(prev)
                    continue
                else:
                    logger.warning(
                        f"Index '{display_name}' missing from API response and no cache"
                    )
                    indices.append({
                        "name": display_name,
                        "instrument_key": instrument_key,
                        "value": None,
                        "change": None,
                        "change_pct": None,
                        "direction": "flat",
                        "last_updated": datetime.now(IST).isoformat(),
                    })
                    continue

            last_price = details.get("last_price", 0) or 0
            net_change = details.get("net_change", 0) or 0
            prev_close = last_price - net_change if last_price else 0
            change_pct = (net_change / prev_close * 100) if prev_close else 0

            indices.append({
                "name": display_name,
                "instrument_key": instrument_key,
                "value": last_price,
                "change": round(net_change, 2),
                "change_pct": round(change_pct, 2),
                "direction": "up" if net_change > 0 else "down" if net_change < 0 else "flat",
                "last_updated": datetime.now(IST).isoformat(),
            })

        _index_cache = indices
        _cache_timestamp = time.time()
        logger.info(f"Index data refreshed: {len(indices)} indices")
        return indices

    except Exception as e:
        logger.error(f"Failed to fetch index data: {e}")
        return _index_cache or _get_fallback_indices()


def _get_fallback_indices() -> list[dict]:
    """Return placeholder index data when API is unavailable."""
    return [
        {
            "name": name,
            "instrument_key": key,
            "value": None,
            "change": None,
            "change_pct": None,
            "direction": "flat",
            "last_updated": None,
        }
        for name, key in INDEX_INSTRUMENTS.items()
    ]
