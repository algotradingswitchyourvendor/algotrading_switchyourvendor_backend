"""
Subscription plan feature matrix.

This is the single source of truth for what each plan can do.
No plan checks are scattered elsewhere in the codebase.

Plans:
  FREE    — ₹0/month
  BASIC   — ₹99/month
  PRO     — ₹149/month
  PREMIUM — ₹199/month

Values:
  -1 means unlimited
  False means not available
  True means available
"""

from typing import Any

# ── Plan definitions ──────────────────────────────────────────────────────────

PLAN_FEATURES: dict[str, dict[str, Any]] = {
    "FREE": {
        # Dashboard
        "dashboard": True,

        # Live market data
        "live_data_rows": 100,           # max rows returned from live snapshot

        # Scanner MoM
        "scanner": True,
        "scanner_daily_limit": 10,       # max scans per day

        # Scanner LTD
        "scanner_ltd": False,

        # History
        "history": True,
        "history_days": 30,              # how far back user can query

        # Analytics
        "advanced_analytics": False,
        "fii_analytics": False,
        "advanced_sentiment": False,
        "premarket": True,               # premarket data is available to all

        # Dynamic columns
        "max_columns": 10,               # max visible columns in table

        # Presets
        "max_presets": 3,

        # Export
        "csv_export": False,

        # Support
        "priority_support": False,
    },

    "BASIC": {
        "dashboard": True,
        "live_data_rows": -1,            # unlimited
        "scanner": True,
        "scanner_daily_limit": 100,
        "scanner_ltd": False,
        "history": True,
        "history_days": 180,
        "advanced_analytics": True,
        "fii_analytics": False,
        "advanced_sentiment": False,
        "premarket": True,
        "max_columns": 30,
        "max_presets": 15,
        "csv_export": True,
        "priority_support": False,
    },

    "PRO": {
        "dashboard": True,
        "live_data_rows": -1,
        "scanner": True,
        "scanner_daily_limit": 500,
        "scanner_ltd": True,
        "history": True,
        "history_days": 365,
        "advanced_analytics": True,
        "fii_analytics": True,
        "advanced_sentiment": True,
        "premarket": True,
        "max_columns": -1,              # all columns
        "max_presets": 50,
        "csv_export": True,
        "priority_support": False,
    },

    "ULTRA": {
        "dashboard": True,
        "live_data_rows": -1,
        "scanner": True,
        "scanner_daily_limit": -1,      # unlimited
        "scanner_ltd": True,
        "history": True,
        "history_days": -1,             # full history
        "advanced_analytics": True,
        "fii_analytics": True,
        "advanced_sentiment": True,
        "premarket": True,
        "max_columns": -1,
        "max_presets": -1,              # unlimited
        "csv_export": True,
        "priority_support": True,
    },
}

# Plan pricing (in INR)
PLAN_PRICES: dict[str, int] = {
    "FREE": 0,
    "BASIC": 99,
    "PRO": 149,
    "ULTRA": 199,
}

# Ordered plan names (for upgrade/downgrade comparison)
PLAN_ORDER = ["FREE", "BASIC", "PRO", "ULTRA"]

# Plans that require a Razorpay subscription (paid plans)
PAID_PLANS = {"BASIC", "PRO", "ULTRA"}


def get_plan_features(plan_name: str) -> dict[str, Any]:
    """Return feature dict for a plan name. Falls back to FREE if unknown."""
    return PLAN_FEATURES.get(plan_name, PLAN_FEATURES["FREE"])


def is_upgrade(from_plan: str, to_plan: str) -> bool:
    """Return True if moving from_plan to to_plan is an upgrade."""
    from_idx = PLAN_ORDER.index(from_plan) if from_plan in PLAN_ORDER else 0
    to_idx = PLAN_ORDER.index(to_plan) if to_plan in PLAN_ORDER else 0
    return to_idx > from_idx
