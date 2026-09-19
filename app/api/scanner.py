"""
Scanner API — Unified scanner endpoints with auth + entitlement enforcement.

POST /api/v1/scanner/query  — New unified endpoint (query engine) — requires auth
POST /api/v1/scanner        — Legacy endpoint (internally routed through query engine) — requires auth
GET  /api/v1/scanner/presets — Return predefined scanner conditions — public

Entitlements enforced:
  - Scanner MoM (execution_target=live): all plans with daily limit
  - Scanner LTD (execution_target=history): PRO/PREMIUM only
  - Daily scan limit: checked via Redis counter

All 403 errors use the standardized PLAN_REQUIRED format.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.auth.dependencies import get_current_user, require_auth
from app.config.holidays import get_market_status
from app.db.models import User
from app.entitlements.checker import EntitlementChecker
from app.entitlements.dependencies import get_entitlements
from app.schemas.query import QueryCondition, UnifiedQueryRequest
from app.schemas.response import error_response, success_response
from app.schemas.scanner import ScannerRequest
from app.services.query_engine import execute_query
from app.services.scanner_service import get_scanner_presets

router = APIRouter()


@router.post("/scanner/query")
async def query_scanner(
    request: Request,
    body: UnifiedQueryRequest,
    user: User = Depends(require_auth),
    ent: EntitlementChecker = Depends(get_entitlements),
):
    """
    Unified scanner query endpoint.

    Accepts either structured conditions or free-text query.
    Supports both live and historical execution targets.

    Entitlements:
      - execution_target="live"    → Scanner MoM: all plans (daily limit applies)
      - execution_target="history" → Scanner LTD: PRO/PREMIUM only
    """
    # ── Scanner LTD gate (history target = LTD feature) ────────────────
    if body.execution_target != "live" and not ent.can_use_scanner_ltd():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "PLAN_REQUIRED",
                "feature": "SCANNER_LTD",
                "required_plan": "PRO",
                "current_plan": ent.plan_name,
                "message": "Scanner LTD requires the PRO plan or higher.",
                "upgrade_required": True,
            },
        )

    # ── Scanner MoM: check daily limit (live target) ───────────────────
    if body.execution_target == "live":
        if not ent.can_use_scanner():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "PLAN_REQUIRED",
                    "feature": "SCANNER",
                    "required_plan": "FREE",
                    "current_plan": ent.plan_name,
                    "message": f"Scanner is not available on the {ent.plan_name} plan.",
                    "upgrade_required": True,
                },
            )
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
                        "message": f"Daily scanner limit of {limit} scans reached. Upgrade for more.",
                        "upgrade_required": True,
                    },
                )

    cache = request.app.state.live_cache

    if body.execution_target == "live" and not cache.is_populated:
        return error_response(
            code="NO_DATA",
            message="Market data not yet available for live scanning.",
        )

    try:
        records, meta = await execute_query(request=body, cache=cache)

        return success_response(
            data=records,
            market_status=get_market_status(),
        ) | {"meta": meta}

    except ValueError as e:
        import traceback
        traceback.print_exc()
        return error_response(
            code="QUERY_VALIDATION_ERROR",
            message=str(e),
        )
    except Exception as e:
        return error_response(
            code="SCANNER_ERROR",
            message=f"Scanner evaluation failed: {str(e)}",
        )


@router.post("/scanner")
async def run_scanner(
    request: Request,
    body: ScannerRequest,
    user: User = Depends(require_auth),
    ent: EntitlementChecker = Depends(get_entitlements),
):
    """
    Legacy scanner endpoint — internally routes through the unified query engine.

    Preserves backward compatibility for existing MoM scanner calls.
    Requires auth + scanner entitlement + daily limit.
    """
    # Check scanner access
    if not ent.can_use_scanner():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "PLAN_REQUIRED",
                "feature": "SCANNER",
                "required_plan": "FREE",
                "current_plan": ent.plan_name,
                "message": f"Scanner is not available on the {ent.plan_name} plan.",
                "upgrade_required": True,
            },
        )

    # Check daily limit
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
                    "message": f"Daily scanner limit of {limit} scans reached. Upgrade for more.",
                    "upgrade_required": True,
                },
            )

    cache = request.app.state.live_cache

    if body.mode == "live" and not cache.is_populated:
        return error_response(
            code="NO_DATA",
            message="Market data not yet available for live scanning.",
        )

    try:
        # Adapt old format → UnifiedQueryRequest
        unified = UnifiedQueryRequest(
            conditions=[
                QueryCondition(**c.model_dump())
                for c in body.conditions
            ],
            execution_target=body.mode,
            date=body.date,
            start_time=body.start_time,
            end_time=body.end_time,
            sort_by=body.sort_by,
            sort_order=body.sort_order,
            page=body.page,
            page_size=body.page_size,
        )

        records, meta = await execute_query(request=unified, cache=cache)

        return success_response(
            data=records,
            market_status=get_market_status(),
        ) | {"meta": meta}

    except Exception as e:
        return error_response(
            code="SCANNER_ERROR",
            message=f"Scanner evaluation failed: {str(e)}",
        )


@router.get("/scanner/presets")
async def get_presets():
    """Return predefined scanner conditions. Public — no auth required."""
    presets = get_scanner_presets()
    return success_response(
        data=presets,
        market_status=get_market_status(),
    )
