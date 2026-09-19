"""
Razorpay subscription billing service.

Handles:
  - Plan creation in Razorpay (one-time setup)
  - Subscription creation (recurring billing)
  - Subscription cancellation
  - Webhook signature verification
  - Subscription status sync

Razorpay subscription lifecycle:
  created → authenticated → active → (charged each cycle) → (cancelled | expired)

Webhook events handled:
  subscription.authenticated  — first authorization payment completed
  subscription.activated      — subscription is now active
  subscription.charged        — recurring payment successful (ACTIVE)
  subscription.pending        — payment failed, in retry (PAST_DUE)
  subscription.halted         — payment failed after all retries (PAST_DUE)
  subscription.cancelled      — user or admin cancelled (CANCELLED)
  subscription.completed      — all billing cycles finished (EXPIRED)
"""

import hashlib
import hmac
import logging
from typing import Optional

from app.config.settings import get_settings

logger = logging.getLogger(__name__)


def _get_client():
    """Get Razorpay client. Lazily initialized."""
    import razorpay
    settings = get_settings()
    if not settings.RAZORPAY_KEY_ID or not settings.RAZORPAY_KEY_SECRET:
        raise RuntimeError(
            "RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET must be configured for payments"
        )
    return razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))


async def create_razorpay_plan(
    name: str,
    amount_inr: int,
    description: str = "",
) -> str:
    """
    Create a recurring billing plan in Razorpay.

    Returns the Razorpay plan_id (e.g., 'plan_xxxxx').
    This is called once per plan during initial setup, not per user.

    Amount is in INR (not paise). Razorpay requires paise internally.
    """
    client = _get_client()
    amount_paise = amount_inr * 100  # INR to paise

    plan_data = {
        "period": "monthly",
        "interval": 1,
        "item": {
            "name": name,
            "amount": amount_paise,
            "currency": "INR",
            "description": description or f"MarketPulse {name} Plan",
        },
    }

    plan = client.plan.create(plan_data)
    plan_id = plan.get("id")
    if not plan_id:
        raise ValueError(f"Razorpay did not return plan ID for {name}")

    logger.info(f"Created Razorpay plan: {plan_id} for {name} @ ₹{amount_inr}/month")
    return plan_id


async def create_razorpay_subscription(
    razorpay_plan_id: str,
    total_count: int = 120,  # 10 years — effectively unlimited
    quantity: int = 1,
    customer_notify: int = 1,
    notes: Optional[dict] = None,
) -> dict:
    """
    Create a Razorpay subscription instance for a user.

    Returns the full subscription object including short_url for checkout.

    total_count: number of billing cycles (120 = 10 years)
    """
    client = _get_client()

    sub_data = {
        "plan_id": razorpay_plan_id,
        "total_count": total_count,
        "quantity": quantity,
        "customer_notify": customer_notify,
    }
    if notes:
        sub_data["notes"] = notes

    subscription = client.subscription.create(sub_data)
    logger.info(f"Created Razorpay subscription: {subscription.get('id')}")
    return subscription


async def cancel_razorpay_subscription(
    razorpay_subscription_id: str,
    cancel_at_cycle_end: bool = True,
) -> bool:
    """
    Cancel a Razorpay subscription.

    cancel_at_cycle_end=True: subscription active until end of current billing period.
    cancel_at_cycle_end=False: cancel immediately.

    Returns True on success.
    """
    client = _get_client()
    try:
        client.subscription.cancel(
            razorpay_subscription_id,
            {"cancel_at_cycle_end": 1 if cancel_at_cycle_end else 0},
        )
        logger.info(f"Cancelled Razorpay subscription: {razorpay_subscription_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to cancel Razorpay subscription {razorpay_subscription_id}: {e}")
        return False


def verify_webhook_signature(payload_body: bytes, signature: str) -> bool:
    """
    Verify Razorpay webhook signature.

    Razorpay signs webhooks with HMAC-SHA256 using the webhook secret.
    signature comes from 'X-Razorpay-Signature' header.

    Returns True if valid, False if invalid.
    """
    settings = get_settings()
    webhook_secret = settings.RAZORPAY_WEBHOOK_SECRET

    if not webhook_secret:
        logger.error("RAZORPAY_WEBHOOK_SECRET not set — cannot verify webhook signature")
        return False

    expected = hmac.new(
        webhook_secret.encode(),
        payload_body,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected, signature)


async def fetch_razorpay_subscription(razorpay_subscription_id: str) -> dict:
    """Fetch subscription details from Razorpay API."""
    client = _get_client()
    return client.subscription.fetch(razorpay_subscription_id)


async def fetch_razorpay_invoice(invoice_id: str) -> dict:
    """Fetch invoice details from Razorpay API."""
    client = _get_client()
    return client.invoice.fetch(invoice_id)


def map_razorpay_status_to_db(razorpay_status: str) -> str:
    """
    Map Razorpay subscription status to our DB subscription status.

    Razorpay statuses: created, authenticated, active, pending, halted, cancelled, completed, expired
    Our statuses: TRIALING, ACTIVE, PAST_DUE, CANCELLED, EXPIRED
    """
    mapping = {
        "created": "TRIALING",
        "authenticated": "TRIALING",
        "active": "ACTIVE",
        "pending": "PAST_DUE",
        "halted": "PAST_DUE",
        "cancelled": "CANCELLED",
        "completed": "EXPIRED",
        "expired": "EXPIRED",
    }
    return mapping.get(razorpay_status, "PAST_DUE")
