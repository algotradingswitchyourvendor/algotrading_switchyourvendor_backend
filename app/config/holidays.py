"""
Market hours and holiday calendar for NSE/BSE.

Provides utility functions to determine whether the market is currently open,
accounting for weekdays, trading hours (09:00–15:30 IST), and NSE holidays.
"""

from datetime import datetime, date, time
from typing import Optional
import pytz

IST = pytz.timezone("Asia/Kolkata")

# ── Market Trading Hours ────────────────────────────────────────────────
MARKET_OPEN_TIME = time(9, 0)
MARKET_CLOSE_TIME = time(15, 30)

# ── NSE Holidays 2026 ──────────────────────────────────────────────────
# Source: NSE publishes this list annually.
# Update this set at the start of each calendar year.
NSE_HOLIDAYS_2026: frozenset[date] = frozenset({
    date(2026, 1, 26),   # Republic Day
    date(2026, 2, 17),   # Mahashivratri (tentative)
    date(2026, 3, 10),   # Holi
    date(2026, 3, 30),   # Id-Ul-Fitr (Ramadan) (tentative)
    date(2026, 3, 31),   # Id-Ul-Fitr (Ramadan) (tentative)
    date(2026, 4, 2),    # Ram Navami (tentative)
    date(2026, 4, 3),    # Good Friday
    date(2026, 4, 14),   # Dr. Ambedkar Jayanti
    date(2026, 5, 1),    # Maharashtra Day
    date(2026, 5, 25),   # Buddha Purnima (tentative)
    date(2026, 6, 6),    # Bakri Id (tentative)
    date(2026, 7, 6),    # Muharram (tentative)
    date(2026, 8, 15),   # Independence Day
    date(2026, 8, 16),   # Parsi New Year (tentative)
    date(2026, 9, 4),    # Milad-un-Nabi (tentative)
    date(2026, 10, 2),   # Mahatma Gandhi Jayanti
    date(2026, 10, 20),  # Dussehra
    date(2026, 11, 9),   # Diwali (Laxmi Pujan)
    date(2026, 11, 10),  # Diwali (Balipratipada)
    date(2026, 11, 27),  # Guru Nanak Jayanti (tentative)
    date(2026, 12, 25),  # Christmas
})


def get_ist_now() -> datetime:
    """Returns the current datetime in IST timezone."""
    return datetime.now(IST)


def is_weekend(dt: Optional[datetime] = None) -> bool:
    """Check if the given datetime (or now) falls on a weekend."""
    if dt is None:
        dt = get_ist_now()
    return dt.weekday() >= 5  # Saturday=5, Sunday=6


def is_holiday(dt: Optional[datetime] = None) -> bool:
    """Check if the given date is an NSE holiday."""
    if dt is None:
        dt = get_ist_now()
    return dt.date() in NSE_HOLIDAYS_2026


def is_within_market_hours(dt: Optional[datetime] = None) -> bool:
    """Check if the given time is within 09:00–15:30 IST."""
    if dt is None:
        dt = get_ist_now()
    current_time = dt.time()
    return MARKET_OPEN_TIME <= current_time <= MARKET_CLOSE_TIME


def is_market_open(dt: Optional[datetime] = None) -> bool:
    """
    Determine if the stock market is currently open.
    
    Returns True only when ALL of:
    - It's a weekday (Monday–Friday)
    - It's not an NSE holiday
    - Current time is between 09:00 and 15:30 IST
    """
    if dt is None:
        dt = get_ist_now()
    return (
        not is_weekend(dt)
        and not is_holiday(dt)
        and is_within_market_hours(dt)
    )


def get_market_status(dt: Optional[datetime] = None) -> str:
    """
    Returns a human-readable market status string.
    
    Returns one of: "LIVE", "CLOSED", "HOLIDAY", "WEEKEND"
    """
    if dt is None:
        dt = get_ist_now()

    if is_weekend(dt):
        return "WEEKEND"
    if is_holiday(dt):
        return "HOLIDAY"
    if is_within_market_hours(dt):
        return "LIVE"
    return "CLOSED"


def get_market_status_detail(dt: Optional[datetime] = None) -> dict:
    """
    Returns detailed market status information for the API response.
    """
    if dt is None:
        dt = get_ist_now()

    return {
        "status": get_market_status(dt),
        "is_open": is_market_open(dt),
        "current_time": dt.isoformat(),
        "market_open": MARKET_OPEN_TIME.isoformat(),
        "market_close": MARKET_CLOSE_TIME.isoformat(),
        "is_weekend": is_weekend(dt),
        "is_holiday": is_holiday(dt),
    }
