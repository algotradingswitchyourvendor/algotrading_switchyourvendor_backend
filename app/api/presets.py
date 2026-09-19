"""
Presets API — User-scoped scanner presets backed by PostgreSQL.

All endpoints require authentication.
Users can only read/write their own presets.
Public/system presets (user_id=NULL) are readable by all authenticated users.

If DATABASE_URL is not configured, falls back to FilePresetRepository
for backward compatibility during local development without a DB.

Routes:
  GET    /api/v1/presets              — list user's presets
  POST   /api/v1/presets              — create preset
  GET    /api/v1/presets/{id}         — get single preset
  PUT    /api/v1/presets/{id}         — update preset
  DELETE /api/v1/presets/{id}         — soft delete preset
  POST   /api/v1/presets/{id}/use     — increment usage
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth.dependencies import require_auth
from app.config.settings import get_settings
from app.db.models import User
from app.schemas.preset import PresetCreate, PresetResponse, PresetUpdate

logger = logging.getLogger(__name__)
router = APIRouter(tags=["presets"])



@router.get("/presets", response_model=List[PresetResponse])
async def list_presets(
    scanner_type: str = Query(None, description="Filter by scanner type"),
    user: User = Depends(require_auth),
):
    """Get all presets accessible to the current user."""
    settings = get_settings()

    if settings.DATABASE_URL and user:
        from app.db.database import get_session_factory
        from app.services.preset_db_repository import PostgresPresetRepository
        factory = get_session_factory()
        async with factory() as db:
            repo = PostgresPresetRepository(db, user.id)
            presets = await repo.get_all(include_deleted=False, scanner_type=scanner_type)
            return presets
    else:
        # File fallback
        from app.services.preset_repository import FilePresetRepository
        repo = FilePresetRepository(user_id=user.id if user else None)
        presets = repo.get_all(include_deleted=False)
        if scanner_type and scanner_type != "any":
            presets = [p for p in presets if p.scanner_type in (scanner_type, "any")]
        presets.sort(key=lambda x: x.created_at, reverse=True)
        return presets


@router.post("/presets", response_model=PresetResponse)
async def create_preset(
    preset: PresetCreate,
    user: User = Depends(require_auth),
):
    """Create a new preset."""
    settings = get_settings()

    if settings.DATABASE_URL and user:
        from app.db.database import get_session_factory
        from app.services.preset_db_repository import PostgresPresetRepository
        from app.entitlements.checker import EntitlementChecker
        from app.db.models import Subscription, SubscriptionPlan
        from sqlalchemy import select

        factory = get_session_factory()
        async with factory() as db:
            # Check preset limit
            sub_result = await db.execute(
                select(Subscription, SubscriptionPlan)
                .join(SubscriptionPlan, Subscription.plan_id == SubscriptionPlan.id)
                .where(Subscription.user_id == user.id)
                .where(Subscription.status == "ACTIVE")
                .limit(1)
            )
            sub_row = sub_result.first()
            subscription = sub_row[0] if sub_row else None

            # Temporarily set plan on subscription for checker
            if subscription and sub_row:
                subscription.plan = sub_row[1]

            checker = EntitlementChecker.from_subscription(subscription)
            repo = PostgresPresetRepository(db, user.id)
            count = await repo.get_count_for_user()
            max_presets = checker.get_max_presets()

            if max_presets != -1 and count >= max_presets:
                raise HTTPException(
                    status_code=403,
                    detail={
                        "code": "PRESET_LIMIT_REACHED",
                        "message": f"Preset limit of {max_presets} reached for {checker.plan_name} plan. Upgrade to save more presets.",
                        "upgrade_required": True,
                    },
                )

            duplicate = await repo.find_duplicate(preset.name, preset.request.model_dump() if preset.request else {})
            if duplicate:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "A preset with this name already exists.",
                        "existing_preset_id": duplicate.id,
                        "reason": "name_match",
                    },
                )

            created = await repo.create(preset)
            await db.commit()
            return created
    else:
        import uuid
        from datetime import datetime, timezone
        from app.services.preset_repository import FilePresetRepository
        repo = FilePresetRepository(user_id=user.id if user else None)
        duplicate = repo.find_duplicate(preset.name, preset.request)
        if duplicate:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "A preset with this name or identical conditions already exists.",
                    "existing_preset_id": duplicate.id,
                    "reason": "name_match" if duplicate.name == preset.name else "payload_match",
                },
            )
        now = datetime.now(timezone.utc)
        new_preset = PresetResponse(
            id=str(uuid.uuid4()),
            name=preset.name,
            description=preset.description,
            scanner_type=preset.scanner_type,
            version=preset.version,
            request=preset.request,
            is_public=preset.is_public,
            favorite=False,
            sorting=preset.sorting,
            page_size=preset.page_size,
            selected_columns=preset.selected_columns,
            usage_count=0,
            created_at=now,
            updated_at=now,
            last_used=None,
            is_deleted=False,
        )
        return repo.create(new_preset)


@router.get("/presets/{preset_id}", response_model=PresetResponse)
async def get_preset(
    preset_id: str,
    user: User = Depends(require_auth),
):
    """Get a specific preset."""
    settings = get_settings()

    if settings.DATABASE_URL and user:
        from app.db.database import get_session_factory
        from app.services.preset_db_repository import PostgresPresetRepository
        factory = get_session_factory()
        async with factory() as db:
            repo = PostgresPresetRepository(db, user.id)
            preset = await repo.get_by_id(preset_id)
            if not preset or preset.is_deleted:
                raise HTTPException(status_code=404, detail="Preset not found")
            return preset
    else:
        from app.services.preset_repository import FilePresetRepository
        repo = FilePresetRepository(user_id=user.id if user else None)
        preset = repo.get_by_id(preset_id)
        if not preset or preset.is_deleted:
            raise HTTPException(status_code=404, detail="Preset not found")
        return preset


@router.put("/presets/{preset_id}", response_model=PresetResponse)
async def update_preset(
    preset_id: str,
    updates: PresetUpdate,
    user: User = Depends(require_auth),
):
    """Update a preset. Only the owner can update."""
    settings = get_settings()

    if settings.DATABASE_URL and user:
        from app.db.database import get_session_factory
        from app.services.preset_db_repository import PostgresPresetRepository
        factory = get_session_factory()
        async with factory() as db:
            repo = PostgresPresetRepository(db, user.id)
            try:
                updated = await repo.update(preset_id, updates)
            except PermissionError:
                raise HTTPException(status_code=403, detail="You do not have permission to modify this preset")
            if not updated:
                raise HTTPException(status_code=404, detail="Preset not found")
            await db.commit()
            return updated
    else:
        from datetime import datetime, timezone
        from app.services.preset_repository import FilePresetRepository
        repo = FilePresetRepository(user_id=user.id if user else None)
        preset = repo.get_by_id(preset_id)
        if not preset or preset.is_deleted:
            raise HTTPException(status_code=404, detail="Preset not found")
        now_iso = datetime.now(timezone.utc).isoformat()
        return repo.update(preset_id, updates, now_iso)


@router.delete("/presets/{preset_id}")
async def delete_preset(
    preset_id: str,
    user: User = Depends(require_auth),
):
    """Soft-delete a preset. Only the owner can delete."""
    settings = get_settings()

    if settings.DATABASE_URL and user:
        from app.db.database import get_session_factory
        from app.services.preset_db_repository import PostgresPresetRepository
        factory = get_session_factory()
        async with factory() as db:
            repo = PostgresPresetRepository(db, user.id)
            try:
                success = await repo.delete(preset_id)
            except PermissionError:
                raise HTTPException(status_code=403, detail="You do not have permission to delete this preset")
            if not success:
                raise HTTPException(status_code=404, detail="Preset not found")
            await db.commit()
            return {"message": "Preset deleted successfully"}
    else:
        from datetime import datetime, timezone
        from app.services.preset_repository import FilePresetRepository
        repo = FilePresetRepository(user_id=user.id if user else None)
        preset = repo.get_by_id(preset_id)
        if not preset or preset.is_deleted:
            raise HTTPException(status_code=404, detail="Preset not found")
        now_iso = datetime.now(timezone.utc).isoformat()
        success = repo.delete(preset_id, now_iso)
        if not success:
            raise HTTPException(status_code=500, detail="Failed to delete preset")
        return {"message": "Preset deleted successfully"}


@router.post("/presets/{preset_id}/use", response_model=PresetResponse)
async def use_preset(
    preset_id: str,
    user: User = Depends(require_auth),
):
    """Increment usage count for a preset."""
    settings = get_settings()

    if settings.DATABASE_URL and user:
        from app.db.database import get_session_factory
        from app.services.preset_db_repository import PostgresPresetRepository
        factory = get_session_factory()
        async with factory() as db:
            repo = PostgresPresetRepository(db, user.id)
            updated = await repo.increment_usage(preset_id)
            if not updated:
                raise HTTPException(status_code=404, detail="Preset not found")
            await db.commit()
            return updated
    else:
        from datetime import datetime, timezone
        from app.services.preset_repository import FilePresetRepository
        repo = FilePresetRepository(user_id=user.id if user else None)
        preset = repo.get_by_id(preset_id)
        if not preset or preset.is_deleted:
            raise HTTPException(status_code=404, detail="Preset not found")
        now_iso = datetime.now(timezone.utc).isoformat()
        return repo.increment_usage(preset_id, now_iso)
