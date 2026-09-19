"""
Admin API — all endpoints require ADMIN role.

Routes:
  GET  /api/v1/admin/stats                 — dashboard metrics
  GET  /api/v1/admin/system/health         — full internal system health
  GET  /api/v1/admin/users                 — paginated user list
  GET  /api/v1/admin/users/{id}            — single user detail
  PATCH /api/v1/admin/users/{id}/status   — suspend / reactivate user
  GET  /api/v1/admin/subscriptions         — subscription list
  GET  /api/v1/admin/payments              — payment list
  GET  /api/v1/admin/audit-logs            — audit log

Security:
  ALL routes depend on require_admin().
  A 403 is returned for any non-admin user.
  Frontend route guards are UX only — all enforcement is here.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_admin
from app.db.database import get_db
from app.db.models import AuditLog, Payment, Subscription, SubscriptionPlan, User
from app.schemas.response import success_response

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])


def _user_to_dict(user: User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "avatar_url": user.avatar_url,
        "role": user.role,
        "status": user.status,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
    }


# ── GET /admin/stats ──────────────────────────────────────────────────────────

@router.get("/stats")
async def admin_stats(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Platform-wide metrics for the admin dashboard."""
    # Total users
    total_users = (await db.execute(select(func.count()).select_from(User))).scalar()
    active_users = (await db.execute(
        select(func.count()).select_from(User).where(User.status == "ACTIVE")
    )).scalar()
    suspended_users = (await db.execute(
        select(func.count()).select_from(User).where(User.status == "SUSPENDED")
    )).scalar()

    # Subscriptions by plan
    sub_stats_result = await db.execute(
        select(SubscriptionPlan.name, func.count(Subscription.id))
        .join(Subscription, Subscription.plan_id == SubscriptionPlan.id)
        .where(Subscription.status == "ACTIVE")
        .group_by(SubscriptionPlan.name)
    )
    sub_stats = dict(sub_stats_result.all())

    # Active subscriptions
    active_subs = (await db.execute(
        select(func.count()).select_from(Subscription).where(Subscription.status == "ACTIVE")
    )).scalar()

    # Payments
    total_payments = (await db.execute(select(func.count()).select_from(Payment))).scalar()
    successful_payments = (await db.execute(
        select(func.count()).select_from(Payment).where(Payment.status == "SUCCESS")
    )).scalar()
    total_revenue = (await db.execute(
        select(func.sum(Payment.amount_inr)).where(Payment.status == "SUCCESS")
    )).scalar() or 0

    return success_response(data={
        "users": {
            "total": total_users,
            "active": active_users,
            "suspended": suspended_users,
        },
        "subscriptions": {
            "active": active_subs,
            "by_plan": {
                "FREE": sub_stats.get("FREE", 0),
                "BASIC": sub_stats.get("BASIC", 0),
                "PRO": sub_stats.get("PRO", 0),
                "ULTRA": sub_stats.get("ULTRA", 0),
            },
        },
        "payments": {
            "total": total_payments,
            "successful": successful_payments,
            "total_revenue_inr": total_revenue,
        },
    })


# ── GET /admin/system/health ─────────────────────────────────────────────────

@router.get("/system/health")
async def admin_system_health(
    request: Request,
    admin: User = Depends(require_admin),
):
    """
    Full internal system health — ADMIN ONLY.

    Normal users MUST NOT see this data.
    Exposes: scheduler, LiveCache internals, S3 status, market status.
    """
    from app.config.holidays import get_market_status

    cache = getattr(request.app.state, "live_cache", None)
    scheduler = getattr(request.app.state, "scheduler", None)

    cache_info = cache.get_snapshot_info() if cache else {}
    scheduler_status = "running" if scheduler and scheduler.scheduler.running else "stopped"

    # S3 check
    s3_status = "unknown"
    try:
        import boto3
        s3 = boto3.client("s3")
        from app.config.settings import get_settings
        s3.head_bucket(Bucket=get_settings().S3_BUCKET_NAME)
        s3_status = "connected"
    except Exception as e:
        s3_status = f"error: {str(e)[:50]}"

    # Database check
    db_status = "unknown"
    try:
        from app.db.database import get_session_factory
        factory = get_session_factory()
        async with factory() as db:
            from sqlalchemy import text
            await db.execute(text("SELECT 1"))
        db_status = "connected"
    except Exception as e:
        db_status = "disconnected"

    # Redis check
    redis_status = "unknown"
    try:
        redis_client = getattr(request.app.state, "redis", None)
        if redis_client:
            await redis_client.ping()
            redis_status = "connected"
        else:
            redis_status = "disconnected"
    except Exception as e:
        redis_status = "disconnected"

    # Upstox WS / API check
    upstox_ws_status = "unknown"
    try:
        if scheduler and scheduler.access_token:
            upstox_ws_status = "connected"
        else:
            upstox_ws_status = "disconnected"
    except Exception:
        upstox_ws_status = "disconnected"

    return success_response(data={
        "backend": "healthy",
        "scheduler": scheduler_status,
        "cache": {
            "populated": cache_info.get("is_populated", False),
            "instruments": cache_info.get("total_instruments", 0),
            "columns": cache_info.get("total_columns", 0),
            "snapshot_id": cache_info.get("snapshot_id", 0),
            "last_updated": cache_info.get("last_updated"),
        },
        "s3": s3_status,
        "database": db_status,
        "redis": redis_status,
        "upstox_ws": upstox_ws_status,
        "market_status": get_market_status(),
    })


# ── GET /admin/system/logs ───────────────────────────────────────────────────

@router.get("/system/logs")
async def get_system_logs(
    admin: User = Depends(require_admin),
    service: Optional[str] = Query(None),
    level: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
):
    """Fetch recent bounded in-memory application logs."""
    try:
        from app.utils.log_capture import log_capture_handler
        logs = log_capture_handler.get_logs(service=service, level=level, limit=limit)
        return success_response(data={"logs": logs})
    except ImportError:
        return success_response(data={"logs": []})


# ── POST /admin/system/cache/clear ───────────────────────────────────────────

@router.post("/system/cache/clear")
async def clear_system_cache(
    request: Request,
    admin: User = Depends(require_admin),
):
    """
    Clears the MarketPulse application cache safely (keys starting with 'mp:').
    Does not touch sessions (which are in PostgreSQL) or trigger a FLUSHALL.
    """
    redis_client = getattr(request.app.state, "redis", None)
    if not redis_client:
        raise HTTPException(500, detail="Redis client not available")

    try:
        cleared_count = 0
        cursor = b"0"
        while cursor:
            cursor, keys = await redis_client.scan(cursor=cursor, match="mp:*", count=100)
            if keys:
                await redis_client.delete(*keys)
                cleared_count += len(keys)
                
        # Also invalidate local live cache to force a fresh pull if requested
        cache = getattr(request.app.state, "live_cache", None)
        if cache:
            # We don't have a clear() method on live_cache, but this logs the intent
            logger.info(f"Admin {admin.id} triggered cache clear. Redis keys cleared: {cleared_count}")

        # Audit log
        from app.db.database import get_session_factory
        factory = get_session_factory()
        async with factory() as db:
            log = AuditLog(
                admin_id=admin.id,
                action="CACHE_CLEARED",
                extra_data={"cleared_keys_count": cleared_count},
                ip=request.client.host if request and request.client else None,
            )
            db.add(log)
            await db.commit()

        return success_response(data={"success": True, "cleared": cleared_count})
    except Exception as e:
        logger.error(f"Failed to clear cache: {e}")
        raise HTTPException(500, detail=f"Failed to clear cache: {str(e)}")


# ── POST /admin/system/services/{service}/restart ────────────────────────────

@router.post("/system/services/{service}/restart")
async def restart_system_service(
    service: str,
    request: Request,
    admin: User = Depends(require_admin),
):
    """
    Restarts a safely restartable internal service.
    Currently ONLY supports 'scheduler'.
    """
    if service.lower() != "scheduler":
        raise HTTPException(
            400, 
            detail=f"Restarting '{service}' is not supported by the deployment architecture."
        )

    scheduler = getattr(request.app.state, "scheduler", None)
    if not scheduler:
        raise HTTPException(500, detail="Scheduler instance not found")

    try:
        # The UpstoxScheduler encapsulates its own APScheduler restart safety
        scheduler.stop()
        scheduler.start()
        
        logger.info(f"Admin {admin.id} successfully restarted the scheduler.")

        # Audit log
        from app.db.database import get_session_factory
        factory = get_session_factory()
        async with factory() as db:
            log = AuditLog(
                admin_id=admin.id,
                action="SERVICE_RESTARTED",
                extra_data={"service": service},
                ip=request.client.host if request and request.client else None,
            )
            db.add(log)
            await db.commit()

        return success_response(data={"success": True, "message": "Scheduler restarted successfully"})
    except Exception as e:
        logger.error(f"Failed to restart scheduler: {e}")
        raise HTTPException(500, detail=f"Failed to restart scheduler: {str(e)}")



# ── GET /admin/users ──────────────────────────────────────────────────────────

@router.get("/users")
async def list_users(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    search: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    role: Optional[str] = Query(None),
):
    """Paginated user list with search and filters."""
    query = select(User)

    if search:
        query = query.where(
            User.email.ilike(f"%{search}%") | User.name.ilike(f"%{search}%")
        )
    if status:
        query = query.where(User.status == status.upper())
    if role:
        query = query.where(User.role == role.upper())

    # Count
    count_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = count_result.scalar()

    # Paginate
    query = query.order_by(User.created_at.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    users = result.scalars().all()

    return success_response(data={
        "users": [_user_to_dict(u) for u in users],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": (total + page_size - 1) // page_size,
    })


# ── GET /admin/users/{id} ─────────────────────────────────────────────────────

@router.get("/users/{user_id}")
async def get_user(
    user_id: str,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Get single user detail with subscription info."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(404, detail="User not found")

    # Load subscription
    sub_result = await db.execute(
        select(Subscription, SubscriptionPlan)
        .join(SubscriptionPlan, Subscription.plan_id == SubscriptionPlan.id)
        .where(Subscription.user_id == user_id)
        .order_by(Subscription.created_at.desc())
        .limit(1)
    )
    sub_row = sub_result.first()

    sub_data = None
    if sub_row:
        sub, plan = sub_row
        sub_data = {
            "status": sub.status,
            "plan": plan.name,
            "price_inr": plan.price_inr,
            "period_end": sub.period_end.isoformat() if sub.period_end else None,
        }

    return success_response(data={**_user_to_dict(user), "subscription": sub_data})


# ── PATCH /admin/users/{id}/status ───────────────────────────────────────────

class UpdateUserStatusRequest(BaseModel):
    status: str  # ACTIVE | SUSPENDED


@router.patch("/users/{user_id}/status")
async def update_user_status(
    user_id: str,
    body: UpdateUserStatusRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    request: Request = None,
):
    """Suspend or reactivate a user. Admin cannot suspend themselves."""
    if user_id == admin.id:
        raise HTTPException(400, detail="Cannot change your own status")

    new_status = body.status.upper()
    if new_status not in ("ACTIVE", "SUSPENDED"):
        raise HTTPException(400, detail="Status must be ACTIVE or SUSPENDED")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(404, detail="User not found")

    old_status = user.status
    user.status = new_status

    # Audit log
    log = AuditLog(
        admin_id=admin.id,
        action=f"USER_STATUS_CHANGED:{old_status}→{new_status}",
        target_user_id=user_id,
        extra_data={"old_status": old_status, "new_status": new_status},
        ip=request.client.host if request and request.client else None,
    )
    db.add(log)
    await db.commit()

    logger.info(f"Admin {admin.id} changed user {user_id} status: {old_status} → {new_status}")
    return success_response(data=_user_to_dict(user))


# ── GET /admin/subscriptions ──────────────────────────────────────────────────

@router.get("/subscriptions")
async def list_subscriptions(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    search: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    plan: Optional[str] = Query(None),
):
    """List all subscriptions across users."""
    query = select(Subscription, User, SubscriptionPlan).join(
        User, Subscription.user_id == User.id
    ).join(SubscriptionPlan, Subscription.plan_id == SubscriptionPlan.id)

    if search:
        query = query.where(
            User.email.ilike(f"%{search}%") | User.name.ilike(f"%{search}%")
        )
    if status:
        query = query.where(Subscription.status == status.upper())
    if plan:
        query = query.where(SubscriptionPlan.name == plan.upper())

    count_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = count_result.scalar()

    query = query.order_by(Subscription.created_at.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    rows = result.all()

    return success_response(data={
        "subscriptions": [
            {
                "id": sub.id,
                "user": {"id": user.id, "email": user.email, "name": user.name},
                "plan": plan.name,
                "price_inr": plan.price_inr,
                "status": sub.status,
                "period_start": sub.period_start.isoformat() if sub.period_start else None,
                "period_end": sub.period_end.isoformat() if sub.period_end else None,
                "razorpay_subscription_id": sub.razorpay_subscription_id,
                "created_at": sub.created_at.isoformat(),
            }
            for sub, user, plan in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    })


# ── POST /admin/subscriptions/{id}/cancel ─────────────────────────────────────

@router.post("/subscriptions/{subscription_id}/cancel")
async def admin_cancel_subscription(
    subscription_id: str,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Cancel a subscription from the admin dashboard."""
    from app.services.razorpay_service import cancel_razorpay_subscription
    from app.redis_client import invalidate_user_plan_cache

    result = await db.execute(
        select(Subscription).where(Subscription.id == subscription_id)
    )
    subscription = result.scalar_one_or_none()
    
    if not subscription:
        raise HTTPException(404, detail="Subscription not found")

    if subscription.status != "ACTIVE":
        raise HTTPException(400, detail=f"Cannot cancel subscription in {subscription.status} status")

    if subscription.razorpay_subscription_id:
        success = await cancel_razorpay_subscription(
            subscription.razorpay_subscription_id,
            cancel_at_cycle_end=True,
        )
        if not success:
            raise HTTPException(500, detail="Failed to cancel subscription with payment provider")

    # Audit log
    log = AuditLog(
        admin_id=admin.id,
        action=f"SUBSCRIPTION_CANCELLED",
        target_user_id=subscription.user_id,
        extra_data={"subscription_id": subscription_id, "razorpay_id": subscription.razorpay_subscription_id},
        ip=request.client.host if request and getattr(request, "client", None) else None,
    )
    db.add(log)
    await db.commit()
    
    await invalidate_user_plan_cache(subscription.user_id)
    logger.info(f"Admin {admin.id} canceled subscription {subscription_id}")
    return success_response(data={"message": "Subscription cancelled successfully."})


# ── GET /admin/payments ───────────────────────────────────────────────────────

@router.get("/payments")
async def list_payments(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    search: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
):
    """List all payment records."""
    query = select(Payment, User).join(User, Payment.user_id == User.id)

    if search:
        query = query.where(
            User.email.ilike(f"%{search}%") | 
            User.name.ilike(f"%{search}%") |
            Payment.razorpay_payment_id.ilike(f"%{search}%") |
            Payment.id.ilike(f"%{search}%")
        )
    
    if status:
        query = query.where(Payment.status == status.upper())
        
    count_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = count_result.scalar()

    query = query.order_by(Payment.created_at.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    rows = result.all()

    return success_response(data={
        "payments": [
            {
                "id": p.id,
                "user": {"id": u.id, "email": u.email, "name": u.name},
                "amount_inr": p.amount_inr,
                "currency": p.currency,
                "status": p.status,
                "razorpay_payment_id": p.razorpay_payment_id,
                "razorpay_order_id": p.razorpay_order_id,
                "created_at": p.created_at.isoformat(),
            }
            for p, u in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    })


# ── GET /admin/audit-logs ─────────────────────────────────────────────────────

@router.get("/audit-logs")
async def list_audit_logs(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    search: Optional[str] = Query(None),
    action: Optional[str] = Query(None),
):
    """List audit log entries."""
    query = select(AuditLog, User).join(User, AuditLog.admin_id == User.id)

    if search:
        query = query.where(
            AuditLog.action.ilike(f"%{search}%") | 
            User.email.ilike(f"%{search}%")
        )
        
    if action:
        query = query.where(AuditLog.action.ilike(f"{action}%"))
        
    count_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = count_result.scalar()

    query = query.order_by(AuditLog.created_at.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    rows = result.all()

    return success_response(data={
        "logs": [
            {
                "id": log.id,
                "admin_id": log.admin_id,
                "admin_email": admin_user.email,
                "action": log.action,
                "target_user_id": log.target_user_id,
                "metadata": log.extra_data,
                "ip": log.ip,
                "created_at": log.created_at.isoformat(),
            }
            for log, admin_user in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    })
