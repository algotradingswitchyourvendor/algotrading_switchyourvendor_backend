"""
EntitlementChecker — centralized feature access control.

Usage:
    checker = EntitlementChecker.from_user_subscription(user, subscription)
    if not checker.can_use_scanner_ltd():
        raise HTTPException(403, ...)
    limit = checker.get_scanner_daily_limit()

The checker is constructed once per request and is immutable.
All plan-based decisions go through this class.
No other code checks plan names directly.
"""

import logging
from dataclasses import dataclass
from datetime import date
from typing import Optional, Any

from app.entitlements.plans import PLAN_FEATURES, get_plan_features
from app.db.models import User, Subscription

logger = logging.getLogger(__name__)

# Redis key templates for usage counters
REDIS_SCANNER_USAGE_KEY = "mp:usage:scanner:{user_id}:{date}"  # value = count, TTL = 86400


@dataclass(frozen=True)
class EntitlementChecker:
    """
    Immutable entitlement checker for a specific user + subscription state.

    Attributes:
        plan_name: The effective plan name (FREE/BASIC/PRO/PREMIUM)
        features: The resolved feature dict for this plan
    """
    plan_name: str
    features: dict

    @classmethod
    def from_admin(cls) -> "EntitlementChecker":
        """
        Create a checker for an admin user.

        Admins receive ULTRA-equivalent entitlements regardless of their
        actual subscription, so they can access and test all features.
        """
        features = get_plan_features("ULTRA")
        return cls(plan_name="ULTRA", features=features)

    @classmethod
    def from_subscription(cls, subscription: Optional[Subscription]) -> "EntitlementChecker":
        """
        Create a checker from an optional subscription.

        If subscription is None or not ACTIVE, defaults to FREE.
        """
        plan_name = "FREE"
        if subscription and subscription.status == "ACTIVE":
            plan_name = subscription.plan.name if subscription.plan else "FREE"

        features = get_plan_features(plan_name)
        return cls(plan_name=plan_name, features=features)

    def _get(self, key: str, default: Any = False) -> Any:
        return self.features.get(key, default)

    # ── Dashboard ──────────────────────────────────────────────────────────

    def can_use_dashboard(self) -> bool:
        return bool(self._get("dashboard", True))

    # ── Live Market Data ───────────────────────────────────────────────────

    def get_live_data_limit(self) -> int:
        """Max rows from live snapshot. -1 = unlimited."""
        return int(self._get("live_data_rows", 100))

    def has_unlimited_live_data(self) -> bool:
        return self.get_live_data_limit() == -1

    # ── Scanner MoM ───────────────────────────────────────────────────────

    def can_use_scanner(self) -> bool:
        return bool(self._get("scanner", True))

    def get_scanner_daily_limit(self) -> int:
        """Max scanner queries per day. -1 = unlimited."""
        return int(self._get("scanner_daily_limit", 10))

    def has_unlimited_scanner(self) -> bool:
        return self.get_scanner_daily_limit() == -1

    async def check_scanner_daily_limit(self, redis_client, user_id: str) -> tuple[bool, int]:
        """
        Check if user has exceeded their daily scanner limit.

        Returns (allowed: bool, current_count: int).
        If allowed, increments the counter atomically.
        """
        limit = self.get_scanner_daily_limit()
        if limit == -1:
            return True, 0

        today = date.today().isoformat()
        key = REDIS_SCANNER_USAGE_KEY.format(user_id=user_id, date=today)

        current = await redis_client.get(key)
        current_count = int(current) if current else 0

        if current_count >= limit:
            return False, current_count

        # Increment and set TTL (expires at midnight + buffer)
        pipe = redis_client.pipeline()
        pipe.incr(key)
        pipe.expire(key, 86400 + 3600)  # 25 hours
        await pipe.execute()

        return True, current_count + 1

    # ── Scanner LTD ───────────────────────────────────────────────────────

    def can_use_scanner_ltd(self) -> bool:
        return bool(self._get("scanner_ltd", False))

    # ── History ───────────────────────────────────────────────────────────

    def can_use_history(self) -> bool:
        return bool(self._get("history", True))

    def get_history_days_limit(self) -> int:
        """Max history days. -1 = full history."""
        return int(self._get("history_days", 30))

    def has_full_history(self) -> bool:
        return self.get_history_days_limit() == -1

    # ── Analytics ─────────────────────────────────────────────────────────

    def can_use_advanced_analytics(self) -> bool:
        return bool(self._get("advanced_analytics", False))

    def can_use_fii_analytics(self) -> bool:
        return bool(self._get("fii_analytics", False))

    def can_use_advanced_sentiment(self) -> bool:
        return bool(self._get("advanced_sentiment", False))

    def can_use_premarket(self) -> bool:
        return bool(self._get("premarket", True))

    # ── Dynamic Columns ────────────────────────────────────────────────────

    def get_max_columns(self) -> int:
        """Max visible columns. -1 = unlimited."""
        return int(self._get("max_columns", 10))

    def has_unlimited_columns(self) -> bool:
        return self.get_max_columns() == -1

    # ── Presets ────────────────────────────────────────────────────────────

    def get_max_presets(self) -> int:
        """Max saved presets. -1 = unlimited."""
        return int(self._get("max_presets", 3))

    def has_unlimited_presets(self) -> bool:
        return self.get_max_presets() == -1

    # ── Export ─────────────────────────────────────────────────────────────

    def can_export_csv(self) -> bool:
        return bool(self._get("csv_export", False))

    # ── Support ────────────────────────────────────────────────────────────

    def has_priority_support(self) -> bool:
        return bool(self._get("priority_support", False))

    # ── Serialization ──────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        """Serialize for API response (sent to frontend for UX gating)."""
        return {
            "plan": self.plan_name,
            "dashboard": self.can_use_dashboard(),
            "live_data_limit": self.get_live_data_limit(),
            "scanner": self.can_use_scanner(),
            "scanner_daily_limit": self.get_scanner_daily_limit(),
            "scanner_ltd": self.can_use_scanner_ltd(),
            "history": self.can_use_history(),
            "history_days": self.get_history_days_limit(),
            "advanced_analytics": self.can_use_advanced_analytics(),
            "fii_analytics": self.can_use_fii_analytics(),
            "advanced_sentiment": self.can_use_advanced_sentiment(),
            "max_columns": self.get_max_columns(),
            "max_presets": self.get_max_presets(),
            "csv_export": self.can_export_csv(),
            "priority_support": self.has_priority_support(),
        }
