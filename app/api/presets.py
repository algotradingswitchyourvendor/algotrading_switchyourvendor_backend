from fastapi import APIRouter, HTTPException, Depends, Query
from typing import List
import uuid
from datetime import datetime, timezone

from app.schemas.preset import PresetCreate, PresetUpdate, PresetResponse
from app.services.preset_repository import FilePresetRepository, BasePresetRepository

router = APIRouter(tags=["presets"])

# Dependency to inject repository
def get_preset_repository() -> BasePresetRepository:
    return FilePresetRepository()

@router.get("/presets", response_model=List[PresetResponse])
async def list_presets(
    scanner_type: str = Query(None, description="Filter by scanner type"),
    repo: BasePresetRepository = Depends(get_preset_repository)
):
    """Get all saved user presets"""
    presets = repo.get_all(include_deleted=False)
    if scanner_type and scanner_type != "any":
        presets = [p for p in presets if p.scanner_type in (scanner_type, "any")]
    
    # Sort by created_at desc
    presets.sort(key=lambda x: x.created_at, reverse=True)
    return presets

@router.post("/presets", response_model=PresetResponse)
async def create_preset(
    preset: PresetCreate,
    repo: BasePresetRepository = Depends(get_preset_repository)
):
    """Save a new query preset"""
    # Check for duplicate
    duplicate = repo.find_duplicate(preset.name, preset.request)
    if duplicate:
        # 409 Conflict allows the frontend to ask for "Overwrite"
        raise HTTPException(
            status_code=409, 
            detail={
                "message": "A preset with this name or identical conditions already exists.",
                "existing_preset_id": duplicate.id,
                "reason": "name_match" if duplicate.name == preset.name else "payload_match"
            }
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
        is_deleted=False
    )
    
    return repo.create(new_preset)

@router.get("/presets/{preset_id}", response_model=PresetResponse)
async def get_preset(
    preset_id: str,
    repo: BasePresetRepository = Depends(get_preset_repository)
):
    """Get a specific preset by ID"""
    preset = repo.get_by_id(preset_id)
    if not preset or preset.is_deleted:
        raise HTTPException(status_code=404, detail="Preset not found")
    return preset

@router.put("/presets/{preset_id}", response_model=PresetResponse)
async def update_preset(
    preset_id: str,
    updates: PresetUpdate,
    repo: BasePresetRepository = Depends(get_preset_repository)
):
    """Update a saved preset (or Overwrite)"""
    # Ensure it exists
    preset = repo.get_by_id(preset_id)
    if not preset or preset.is_deleted:
        raise HTTPException(status_code=404, detail="Preset not found")
        
    now_iso = datetime.now(timezone.utc).isoformat()
    updated = repo.update(preset_id, updates, now_iso)
    return updated

@router.delete("/presets/{preset_id}")
async def delete_preset(
    preset_id: str,
    repo: BasePresetRepository = Depends(get_preset_repository)
):
    """Soft delete a preset"""
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
    repo: BasePresetRepository = Depends(get_preset_repository)
):
    """Mark a preset as used to increment its usage count"""
    preset = repo.get_by_id(preset_id)
    if not preset or preset.is_deleted:
        raise HTTPException(status_code=404, detail="Preset not found")
        
    now_iso = datetime.now(timezone.utc).isoformat()
    updated = repo.increment_usage(preset_id, now_iso)
    return updated
