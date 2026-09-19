"""
PostgreSQL-backed preset repository.

Implements the same BasePresetRepository interface as FilePresetRepository,
so it can be swapped in as a drop-in replacement.

Presets are user-scoped:
  - user_id = NULL: system/public preset (visible to all users)
  - user_id = <id>: private user preset (only owner can modify)
  - is_public = True: other users can view but not modify

Access control enforced at repository level:
  - CRUD operations require matching user_id (or NULL for public)
  - Cross-user access returns 403 at the API layer
"""

import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Preset
from app.schemas.preset import PresetCreate, PresetResponse, PresetUpdate

logger = logging.getLogger(__name__)


def _model_to_response(p: Preset) -> PresetResponse:
    """Convert Preset ORM model to PresetResponse Pydantic schema."""
    return PresetResponse(
        id=p.id,
        name=p.name,
        description=p.description or "",
        scanner_type=p.scanner_type or "live",
        version=p.version,
        request=p.request or {},
        is_public=p.is_public,
        favorite=p.favorite,
        sorting=p.ui_state.get("sorting") if p.ui_state else None,
        page_size=p.ui_state.get("page_size") if p.ui_state else None,
        selected_columns=p.ui_state.get("selected_columns") if p.ui_state else None,
        usage_count=p.usage_count,
        created_at=p.created_at,
        updated_at=p.updated_at,
        last_used=p.last_used,
        is_deleted=p.is_deleted,
    )


class PostgresPresetRepository:
    """PostgreSQL preset repository scoped to a specific user."""

    def __init__(self, db: AsyncSession, user_id: Optional[str] = None):
        self.db = db
        self.user_id = user_id

    def _user_filter(self):
        """Filter: user's own presets OR public presets."""
        if self.user_id is None:
            # Unauthenticated: only public presets
            return Preset.is_public == True
        return or_(
            Preset.user_id == self.user_id,
            Preset.is_public == True,
            Preset.user_id == None,  # system presets
        )

    async def get_all(
        self, include_deleted: bool = False, scanner_type: Optional[str] = None
    ) -> list[PresetResponse]:
        """Return all accessible presets for the current user."""
        query = select(Preset).where(self._user_filter())

        if not include_deleted:
            query = query.where(Preset.is_deleted == False)

        if scanner_type and scanner_type != "any":
            query = query.where(Preset.scanner_type == scanner_type)

        query = query.order_by(Preset.updated_at.desc())
        result = await self.db.execute(query)
        presets = result.scalars().all()
        return [_model_to_response(p) for p in presets]

    async def get_by_id(self, preset_id: str) -> Optional[PresetResponse]:
        """Get a single preset, checking access."""
        result = await self.db.execute(
            select(Preset)
            .where(Preset.id == preset_id)
            .where(self._user_filter())
        )
        p = result.scalar_one_or_none()
        return _model_to_response(p) if p else None

    async def _get_model(self, preset_id: str) -> Optional[Preset]:
        """Get raw ORM model (for mutations)."""
        result = await self.db.execute(
            select(Preset)
            .where(Preset.id == preset_id)
            .where(self._user_filter())
        )
        return result.scalar_one_or_none()

    def _user_owns(self, preset: Preset) -> bool:
        """Check if current user owns this preset."""
        if self.user_id is None:
            return False
        return preset.user_id == self.user_id

    async def create(self, data: PresetCreate) -> PresetResponse:
        """Create a new preset for the current user."""
        now = datetime.now(timezone.utc)
        ui_state = {}
        if hasattr(data, "sorting") and data.sorting:
            ui_state["sorting"] = data.sorting
        if hasattr(data, "page_size") and data.page_size:
            ui_state["page_size"] = data.page_size
        if hasattr(data, "selected_columns") and data.selected_columns:
            ui_state["selected_columns"] = data.selected_columns

        p = Preset(
            id=str(uuid4()),
            user_id=self.user_id,
            name=data.name,
            description=data.description,
            scanner_type=data.scanner_type or "live",
            version=getattr(data, "version", None),
            request=data.request.model_dump() if data.request else {},
            ui_state=ui_state,
            is_public=data.is_public if hasattr(data, "is_public") else False,
            favorite=False,
            usage_count=0,
            is_deleted=False,
            created_at=now,
            updated_at=now,
        )
        self.db.add(p)
        await self.db.flush()
        await self.db.refresh(p)
        return _model_to_response(p)

    async def update(self, preset_id: str, updates: PresetUpdate) -> Optional[PresetResponse]:
        """Update a preset. Only owner can update."""
        p = await self._get_model(preset_id)
        if not p or p.is_deleted:
            return None
        if not self._user_owns(p):
            raise PermissionError(f"User {self.user_id} does not own preset {preset_id}")

        now = datetime.now(timezone.utc)
        if updates.name is not None:
            p.name = updates.name
        if updates.description is not None:
            p.description = updates.description
        if updates.request is not None:
            p.request = updates.request.model_dump()
        if updates.is_public is not None:
            p.is_public = updates.is_public
        if updates.favorite is not None:
            p.favorite = updates.favorite

        # Merge UI state fields
        ui_state = p.ui_state or {}
        if hasattr(updates, "sorting") and updates.sorting is not None:
            ui_state["sorting"] = updates.sorting
        if hasattr(updates, "page_size") and updates.page_size is not None:
            ui_state["page_size"] = updates.page_size
        if hasattr(updates, "selected_columns") and updates.selected_columns is not None:
            ui_state["selected_columns"] = updates.selected_columns
        p.ui_state = ui_state
        p.updated_at = now

        await self.db.flush()
        await self.db.refresh(p)
        return _model_to_response(p)

    async def delete(self, preset_id: str) -> bool:
        """Soft-delete a preset. Only owner can delete."""
        p = await self._get_model(preset_id)
        if not p or p.is_deleted:
            return False
        if not self._user_owns(p):
            raise PermissionError(f"User {self.user_id} does not own preset {preset_id}")
        p.is_deleted = True
        p.updated_at = datetime.now(timezone.utc)
        await self.db.flush()
        return True

    async def increment_usage(self, preset_id: str) -> Optional[PresetResponse]:
        """Increment usage_count and update last_used."""
        p = await self._get_model(preset_id)
        if not p or p.is_deleted:
            return None
            
        if p.user_id is not None and not self._user_owns(p):
            raise PermissionError(f"User {self.user_id} does not own preset {preset_id}")
            
        # Anyone with access can increment usage (for public system presets)
        p.usage_count = (p.usage_count or 0) + 1
        p.last_used = datetime.now(timezone.utc)
        await self.db.flush()
        await self.db.refresh(p)
        return _model_to_response(p)

    async def find_duplicate(
        self, name: str, request: dict
    ) -> Optional[PresetResponse]:
        """Check for duplicate preset by name within user's presets."""
        result = await self.db.execute(
            select(Preset)
            .where(Preset.user_id == self.user_id)
            .where(Preset.name == name)
            .where(Preset.is_deleted == False)
        )
        p = result.scalar_one_or_none()
        return _model_to_response(p) if p else None

    async def get_count_for_user(self) -> int:
        """Count non-deleted presets for the current user (for limit checking)."""
        from sqlalchemy import func
        result = await self.db.execute(
            select(func.count()).select_from(Preset)
            .where(Preset.user_id == self.user_id)
            .where(Preset.is_deleted == False)
        )
        return result.scalar() or 0
