"""
Payments API — Razorpay webhook handler and subscription checkout.

Routes:
  POST /api/v1/payments/create-subscription  — create Razorpay subscription for upgrade
  POST /api/v1/payments/webhook              — Razorpay webhook (no auth, verified by signature)

Webhook security:
  - Signature verified with HMAC-SHA256 before any processing
  - provider_event_id has UNIQUE constraint in DB (idempotency)
  - Duplicate events are silently acknowledged (HTTP 200)
  - Payment state transitions driven entirely by webhooks (server-authoritative)

Webhook events handled:
  subscription.charged      → ACTIVE + create Payment(SUCCESS)
  subscription.pending      → PAST_DUE
  subscription.halted       → PAST_DUE
  subscription.cancelled    → CANCELLED
  subscription.completed    → EXPIRED
  subscription.activated    → ACTIVE
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_auth
from app.db.database import get_db
from app.db.models import Payment, PaymentEvent, Subscription, SubscriptionPlan, User
from app.redis_client import invalidate_user_plan_cache
from app.schemas.response import error_response, success_response
from app.services.razorpay_service import (
    create_razorpay_subscription,
    map_razorpay_status_to_db,
    verify_webhook_signature,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/payments", tags=["payments"])


# ── POST /payments/create-subscription ───────────────────────────────────────

class CreateSubscriptionRequest(BaseModel):
    plan_name: str  # BASIC | PRO | PREMIUM


@router.post("/create-subscription")
async def create_subscription_checkout(
    body: CreateSubscriptionRequest,
    user: User = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """
    Create a Razorpay subscription for a paid plan upgrade.

    Returns the Razorpay subscription ID and short_url for checkout.
    The subscription is not activated until the webhook confirms payment.
    """
    from app.entitlements.plans import PAID_PLANS

    if body.plan_name not in PAID_PLANS:
        raise HTTPException(400, detail=f"Invalid plan: {body.plan_name}. Must be one of {PAID_PLANS}")

    # Load plan from DB
    result = await db.execute(
        select(SubscriptionPlan)
        .where(SubscriptionPlan.name == body.plan_name)
        .where(SubscriptionPlan.is_active == True)
    )
    plan = result.scalar_one_or_none()
    if not plan:
        raise HTTPException(404, detail=f"Plan {body.plan_name} not found")
    if not plan.razorpay_plan_id:
        raise HTTPException(503, detail="Plan not yet configured in Razorpay. Contact support.")

    # Check if user already has an active subscription
    result = await db.execute(
        select(Subscription)
        .where(Subscription.user_id == user.id)
        .where(Subscription.status == "ACTIVE")
    )
    active_sub = result.scalar_one_or_none()
    if active_sub:
        raise HTTPException(400, detail="You already have an active subscription.")

    try:
        rzp_subscription = await create_razorpay_subscription(
            razorpay_plan_id=plan.razorpay_plan_id,
            notes={"user_id": user.id, "plan_name": plan.name},
        )
    except Exception as e:
        logger.error(f"Failed to create Razorpay subscription for user={user.id}: {e}")
        raise HTTPException(500, detail="Failed to create payment session. Please try again.")

    # Create a pending subscription in our DB
    subscription = Subscription(
        user_id=user.id,
        plan_id=plan.id,
        status="TRIALING",
        razorpay_subscription_id=rzp_subscription.get("id"),
    )
    db.add(subscription)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        logger.warning(f"Concurrent subscription creation prevented for user {user.id}")
        raise HTTPException(400, detail="You already have an active subscription.")

    settings_obj = None
    try:
        from app.config.settings import get_settings
        settings_obj = get_settings()
    except Exception:
        pass

    return success_response(data={
        "subscription_id": rzp_subscription.get("id"),
        "short_url": rzp_subscription.get("short_url"),
        "plan_name": plan.name,
        "amount_inr": plan.price_inr,
        "razorpay_key_id": settings_obj.RAZORPAY_KEY_ID if settings_obj else "",
    })


# ── POST /payments/webhook ─────────────────────────────────────────────────

@router.post("/webhook")
async def razorpay_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Razorpay webhook handler.

    Security:
      1. Verify X-Razorpay-Signature HMAC-SHA256 signature
      2. Check provider_event_id uniqueness (idempotency)
      3. Process event
      4. Return HTTP 200 (Razorpay retries on non-200)

    Duplicate webhooks are silently accepted (HTTP 200) without reprocessing.
    """
    body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")

    # Step 1: Verify signature
    if not verify_webhook_signature(body, signature):
        logger.warning("Razorpay webhook signature verification failed")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid webhook signature",
        )

    payload = await request.json()
    event_type = payload.get("event", "")
    # Razorpay uses event ID from the payload — some have event_id, some use account_id+timestamp
    provider_event_id = payload.get("id")
    if not provider_event_id:
        if event_type and payload.get('created_at'):
            provider_event_id = f"{event_type}:{payload.get('created_at')}"
        else:
            logger.error("Razorpay webhook missing event ID and sufficient fallback data")
            raise HTTPException(400, detail="Missing event ID")

    # Step 2: Idempotency check — insert event record
    event_row = PaymentEvent(
        provider="razorpay",
        provider_event_id=provider_event_id,
        event_type=event_type,
        payload=payload,
    )
    db.add(event_row)
    try:
        await db.flush()
    except IntegrityError:
        # Duplicate event — already processed
        await db.rollback()
        logger.info(f"Duplicate Razorpay webhook ignored: {provider_event_id}")
        return {"status": "ok", "message": "duplicate event ignored"}

    # Step 3: Process event
    try:
        await _process_webhook_event(db, event_type, payload, event_row)
        await db.commit()
    except HTTPException as e:
        # We explicitly raise HTTPExceptions for transient Razorpay API lookup failures
        await db.rollback()
        raise e
    except Exception as e:
        await db.rollback()
        logger.error(f"Webhook processing error for {event_type}: {e}", exc_info=True)
        # Ensure Razorpay retries the webhook by returning 500
        raise HTTPException(500, detail="Internal processing error")

    return {"status": "ok"}


async def _process_webhook_event(
    db: AsyncSession,
    event_type: str,
    payload: dict,
    event_row: PaymentEvent,
) -> None:
    """Route webhook event to appropriate handler."""

    now = datetime.now(timezone.utc)

    # Extract subscription entity from payload
    subscription_entity = (
        payload.get("payload", {})
        .get("subscription", {})
        .get("entity", {})
    )
    payment_entity = (
        payload.get("payload", {})
        .get("payment", {})
        .get("entity", {})
    )

    razorpay_subscription_id = subscription_entity.get("id")

    # --- FALLBACK LOGIC FOR payment.captured ---
    if event_type == "payment.captured" and not razorpay_subscription_id:
        invoice_id = payment_entity.get("invoice_id")
        if not invoice_id:
            logger.info("payment.captured ignored: no invoice_id found.")
            event_row.processed_at = now
            return

        from app.services.razorpay_service import fetch_razorpay_invoice, fetch_razorpay_subscription
        
        try:
            invoice_data = await fetch_razorpay_invoice(invoice_id)
        except Exception as e:
            logger.error(f"Transient error fetching invoice {invoice_id}: {e}")
            raise HTTPException(500, detail="Razorpay API failure during invoice fetch")

        razorpay_subscription_id = invoice_data.get("subscription_id")
        if not razorpay_subscription_id:
            logger.info(f"payment.captured ignored: invoice {invoice_id} is not linked to a subscription.")
            event_row.processed_at = now
            return

        try:
            rzp_subscription = await fetch_razorpay_subscription(razorpay_subscription_id)
        except Exception as e:
            logger.error(f"Transient error fetching subscription {razorpay_subscription_id}: {e}")
            raise HTTPException(500, detail="Razorpay API failure during subscription fetch")
        
        rzp_status = rzp_subscription.get("status")
        if rzp_status != "active":
            logger.info(f"payment.captured ignored: Razorpay subscription {razorpay_subscription_id} status is {rzp_status}, not active.")
            event_row.processed_at = now
            return
        
        subscription_entity = rzp_subscription

    # ---------------------------------------------

    if not razorpay_subscription_id:
        logger.warning(f"No subscription ID could be resolved for webhook event {event_type}")
        event_row.processed_at = now
        return

    # Load our subscription
    result = await db.execute(
        select(Subscription)
        .where(Subscription.razorpay_subscription_id == razorpay_subscription_id)
    )
    subscription = result.scalar_one_or_none()

    if not subscription:
        logger.warning(f"No local subscription found for Razorpay ID: {razorpay_subscription_id}")
        event_row.processed_at = now
        return

    if event_type in ("subscription.charged", "subscription.activated", "payment.captured"):
        if event_type == "payment.captured":
            logger.info(f"Razorpay payment.captured received. Resolving subscription: {razorpay_subscription_id}")
            logger.info(f"Verified Razorpay subscription status: active")
            logger.info(f"Activating local subscription: {subscription.id} for user_id: {subscription.user_id}")

        # Recurring payment success → ACTIVE
        subscription.status = "ACTIVE"

        # Update period from Razorpay data
        current_start = subscription_entity.get("current_start")
        current_end = subscription_entity.get("current_end")
        if current_start:
            subscription.period_start = datetime.fromtimestamp(current_start, tz=timezone.utc)
        if current_end:
            subscription.period_end = datetime.fromtimestamp(current_end, tz=timezone.utc)

        subscription.updated_at = now

        # Create Payment record
        if payment_entity:
            payment_id = payment_entity.get("id")
            
            # Idempotency check for payment record itself
            payment_check_result = await db.execute(
                select(Payment).where(Payment.razorpay_payment_id == payment_id)
            )
            existing_payment = payment_check_result.scalar_one_or_none()
            
            if not existing_payment:
                plan_result = await db.execute(
                    select(SubscriptionPlan).where(SubscriptionPlan.id == subscription.plan_id)
                )
                plan = plan_result.scalar_one_or_none()

                payment = Payment(
                    user_id=subscription.user_id,
                    subscription_id=subscription.id,
                    amount_inr=plan.price_inr if plan else 0,
                    currency="INR",
                    status="SUCCESS",
                    razorpay_order_id=payment_entity.get("order_id"),
                    razorpay_payment_id=payment_id,
                )
                db.add(payment)
                logger.info(f"Created new local payment record for {payment_id}")

        logger.info(f"Subscription {subscription.id} → ACTIVE (event: {event_type})")

    elif event_type in ("subscription.pending", "subscription.halted"):
        # Payment failure → PAST_DUE
        subscription.status = "PAST_DUE"
        subscription.updated_at = now
        logger.info(f"Subscription {subscription.id} → PAST_DUE (event: {event_type})")

    elif event_type == "subscription.cancelled":
        subscription.status = "CANCELLED"
        subscription.updated_at = now
        logger.info(f"Subscription {subscription.id} → CANCELLED")

    elif event_type in ("subscription.completed", "subscription.expired"):
        subscription.status = "EXPIRED"
        subscription.updated_at = now
        logger.info(f"Subscription {subscription.id} → EXPIRED")

    # Invalidate Redis subscription cache so next request re-reads from DB
    await invalidate_user_plan_cache(subscription.user_id)

    # Mark event as processed
    event_row.processed_at = now
