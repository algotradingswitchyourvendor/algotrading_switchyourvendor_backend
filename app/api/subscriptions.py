"""
Subscriptions API.

Routes:
  GET  /api/v1/subscriptions/plans   — public plan listing
  GET  /api/v1/subscriptions/me      — current user's subscription + entitlements
  POST /api/v1/subscriptions/cancel  — cancel current subscription
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import joinedload
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_auth
from app.db.database import get_db
from app.db.models import Subscription, SubscriptionPlan, User
from app.entitlements.checker import EntitlementChecker
from app.entitlements.plans import PLAN_FEATURES
from app.redis_client import invalidate_user_plan_cache
from app.schemas.response import error_response, success_response
from app.services.razorpay_service import cancel_razorpay_subscription

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])


# ── GET /subscriptions/plans ──────────────────────────────────────────────────

@router.get("/plans")
async def list_plans(db: AsyncSession = Depends(get_db)):
    """Return all active subscription plans. Public endpoint."""
    result = await db.execute(
        select(SubscriptionPlan)
        .where(SubscriptionPlan.is_active == True)
        .order_by(SubscriptionPlan.price_inr)
    )
    plans = result.scalars().all()

    return success_response(data=[
        {
            "id": p.id,
            "name": p.name,
            "price_inr": p.price_inr,
            "razorpay_plan_id": p.razorpay_plan_id,
            "features": PLAN_FEATURES.get(p.name, {}),
        }
        for p in plans
    ])


# ── GET /subscriptions/me ─────────────────────────────────────────────────────

@router.get("/me")
async def get_my_subscription(
    user: User = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """Return the current user's subscription and entitlements."""
    result = await db.execute(
        select(Subscription)
        .options(joinedload(Subscription.plan))
        .join(SubscriptionPlan, Subscription.plan_id == SubscriptionPlan.id)
        .where(Subscription.user_id == user.id)
        .order_by(Subscription.created_at.desc())
        .limit(1)
    )
    subscription = result.scalar_one_or_none()

    checker = EntitlementChecker.from_subscription(subscription)

    sub_data = None
    if subscription:
        plan_result = await db.execute(
            select(SubscriptionPlan).where(SubscriptionPlan.id == subscription.plan_id)
        )
        plan = plan_result.scalar_one_or_none()
        sub_data = {
            "id": subscription.id,
            "plan": plan.name if plan else "FREE",
            "price_inr": plan.price_inr if plan else 0,
            "status": subscription.status,
            "period_start": subscription.period_start.isoformat() if subscription.period_start else None,
            "period_end": subscription.period_end.isoformat() if subscription.period_end else None,
            "razorpay_subscription_id": subscription.razorpay_subscription_id,
        }

    return success_response(data={
        "subscription": sub_data,
        "entitlements": checker.to_dict(),
    })


# ── POST /subscriptions/cancel ────────────────────────────────────────────────

@router.post("/cancel")
async def cancel_subscription(
    user: User = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """
    Cancel the user's active subscription.

    Cancellation takes effect at the end of the current billing period
    (cancel_at_cycle_end=True).
    """
    result = await db.execute(
        select(Subscription)
        .where(Subscription.user_id == user.id)
        .where(Subscription.status == "ACTIVE")
        .limit(1)
    )
    subscription = result.scalar_one_or_none()

    if not subscription:
        raise HTTPException(404, detail="No active subscription found")

    if subscription.razorpay_subscription_id:
        success = await cancel_razorpay_subscription(
            subscription.razorpay_subscription_id,
            cancel_at_cycle_end=True,
        )
        if not success:
            raise HTTPException(500, detail="Failed to cancel subscription with payment provider")

    # Status will be updated to CANCELLED by webhook
    # For immediate UI feedback, mark as cancelling
    # Webhook will confirm final state
    logger.info(f"Cancellation requested for subscription={subscription.id} user={user.id}")
    await invalidate_user_plan_cache(user.id)

    return success_response(data={
        "message": "Subscription cancellation requested. Active until end of current billing period.",
        "subscription_id": subscription.id,
        "period_end": subscription.period_end.isoformat() if subscription.period_end else None,
    })
