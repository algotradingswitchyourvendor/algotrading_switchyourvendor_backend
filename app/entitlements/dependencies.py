"""
FastAPI dependencies for entitlement enforcement.

All plan-enforcement dependencies produce a consistent 403 error body:

    {
        "code": "PLAN_REQUIRED",
        "feature": "SCANNER_LTD",
        "required_plan": "PRO",
        "current_plan": "FREE",
        "message": "Scanner LTD requires the PRO plan."
    }

Usage:
    @router.post("/scanner/query")
    async def query_scanner(
        body: UnifiedQueryRequest,
        request: Request,
        user: User = Depends(require_auth),
        ent: EntitlementChecker = Depends(get_entitlements),
        _: None = Depends(require_scanner_access),
    ):
        ...
"""

import logging
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_auth
from app.db.database import get_db
from app.db.models import Subscription, SubscriptionPlan, User
from app.entitlements.checker import EntitlementChecker

logger = logging.getLogger(__name__)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _plan_required_error(
    feature: str,
    required_plan: str,
    current_plan: str,
    message: str,
) -> HTTPException:
    """Return a standardized 403 PLAN_REQUIRED HTTPException."""
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "code": "PLAN_REQUIRED",
            "feature": feature,
            "required_plan": required_plan,
            "current_plan": current_plan,
            "message": message,
            "upgrade_required": True,
        },
    )


async def get_active_subscription(
    user: User,
    db: AsyncSession,
) -> Optional[Subscription]:
    """Load the user's active subscription with plan eagerly."""
    result = await db.execute(
        select(Subscription)
        .join(SubscriptionPlan, Subscription.plan_id == SubscriptionPlan.id)
        .where(Subscription.user_id == user.id)
        .where(Subscription.status == "ACTIVE")
        .order_by(Subscription.created_at.desc())
        .limit(1)
    )
    sub = result.scalar_one_or_none()

    # Eagerly load the plan relationship
    if sub:
        plan_result = await db.execute(
            select(SubscriptionPlan).where(SubscriptionPlan.id == sub.plan_id)
        )
        sub.plan = plan_result.scalar_one_or_none()

    return sub


# ── Core dependency ───────────────────────────────────────────────────────────

async def get_entitlements(
    user: User = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> EntitlementChecker:
    """
    Build an EntitlementChecker for the current user.

    - ADMIN users receive PREMIUM-equivalent entitlements.
    - Otherwise, loads the active subscription from DB.
    - Falls back to FREE if no active subscription.
    """
    # Admin bypass: admins always get the highest entitlements
    if user.role == "ADMIN":
        return EntitlementChecker.from_admin()

    subscription = await get_active_subscription(user, db)
    return EntitlementChecker.from_subscription(subscription)


# ── Feature-specific enforcement dependencies ─────────────────────────────────

async def require_scanner_access(
    request: Request,
    user: User = Depends(require_auth),
    ent: EntitlementChecker = Depends(get_entitlements),
) -> EntitlementChecker:
    """
    Require scanner access and enforce daily scan limit.

    Raises 403 PLAN_REQUIRED if scanner not available on plan.
    Raises 429 SCANNER_DAILY_LIMIT_EXCEEDED if daily limit exceeded.
    """
    if not ent.can_use_scanner():
        raise _plan_required_error(
            feature="SCANNER",
            required_plan="FREE",  # scanner is free for all plans
            current_plan=ent.plan_name,
            message=f"Scanner is not available on the {ent.plan_name} plan.",
        )

    # Check daily limit via Redis
    redis_client = getattr(request.app.state, "redis", None)
    if redis_client and not ent.has_unlimited_scanner():
        allowed, current = await ent.check_scanner_daily_limit(redis_client, user.id)
        if not allowed:
            limit = ent.get_scanner_daily_limit()
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "code": "PLAN_LIMIT_REACHED",
                    "feature": "SCANNER",
                    "limit": limit,
                    "current": current,
                    "required_plan": "PRO",
                    "current_plan": ent.plan_name,
                    "message": f"Daily scanner limit of {limit} scans reached. Resets at midnight IST.",
                    "upgrade_required": True,
                },
            )

    return ent


async def require_scanner_ltd_access(
    ent: EntitlementChecker = Depends(get_entitlements),
) -> EntitlementChecker:
    """
    Require Scanner LTD access (PRO/PREMIUM only).

    Raises 403 PLAN_REQUIRED for FREE and BASIC users.
    """
    if not ent.can_use_scanner_ltd():
        raise _plan_required_error(
            feature="SCANNER_LTD",
            required_plan="PRO",
            current_plan=ent.plan_name,
            message="Scanner LTD requires the PRO plan or higher.",
        )
    return ent


async def require_history_access(
    ent: EntitlementChecker = Depends(get_entitlements),
) -> EntitlementChecker:
    """
    Require history access.

    All plans have history access — this guards the feature boolean.
    Actual day-range clamping is handled in the endpoint.
    """
    if not ent.can_use_history():
        raise _plan_required_error(
            feature="HISTORY",
            required_plan="FREE",
            current_plan=ent.plan_name,
            message=f"Historical data is not available on the {ent.plan_name} plan.",
        )
    return ent


async def require_csv_export(
    ent: EntitlementChecker = Depends(get_entitlements),
) -> EntitlementChecker:
    """
    Require CSV export access (BASIC/PRO/PREMIUM only).

    Raises 403 PLAN_REQUIRED for FREE users.
    """
    if not ent.can_export_csv():
        raise _plan_required_error(
            feature="CSV_EXPORT",
            required_plan="BASIC",
            current_plan=ent.plan_name,
            message="CSV export requires the BASIC plan or higher.",
        )
    return ent


async def require_advanced_analytics(
    ent: EntitlementChecker = Depends(get_entitlements),
) -> EntitlementChecker:
    """Require advanced analytics access (BASIC/PRO/PREMIUM only)."""
    if not ent.can_use_advanced_analytics():
        raise _plan_required_error(
            feature="ADVANCED_ANALYTICS",
            required_plan="BASIC",
            current_plan=ent.plan_name,
            message="Advanced analytics requires the BASIC plan or higher.",
        )
    return ent


async def require_fii_analytics(
    ent: EntitlementChecker = Depends(get_entitlements),
) -> EntitlementChecker:
    """Require FII analytics access (PRO/PREMIUM only)."""
    if not ent.can_use_fii_analytics():
        raise _plan_required_error(
            feature="FII_ANALYTICS",
            required_plan="PRO",
            current_plan=ent.plan_name,
            message="FII analytics requires the PRO plan or higher.",
        )
    return ent


async def require_advanced_sentiment(
    ent: EntitlementChecker = Depends(get_entitlements),
) -> EntitlementChecker:
    """Require advanced sentiment access (PRO/PREMIUM only)."""
    if not ent.can_use_advanced_sentiment():
        raise _plan_required_error(
            feature="ADVANCED_SENTIMENT",
            required_plan="PRO",
            current_plan=ent.plan_name,
            message="Advanced sentiment analysis requires the PRO plan or higher.",
        )
    return ent
