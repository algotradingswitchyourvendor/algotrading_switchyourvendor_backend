from typing import List, Optional, Any
from datetime import datetime
from pydantic import BaseModel, Field
from app.schemas.query import UnifiedQueryRequest

class PresetCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = None
    scanner_type: str = Field(default="any") 
    version: int = 2
    request: UnifiedQueryRequest
    is_public: bool = False
    
    # New state fields
    sorting: Optional[List[dict]] = None
    page_size: Optional[int] = None
    selected_columns: Optional[List[str]] = None
    ui_state: Optional[dict] = Field(default_factory=dict)

class PresetUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    request: Optional[UnifiedQueryRequest] = None
    version: Optional[int] = None
    favorite: Optional[bool] = None
    sorting: Optional[List[dict]] = None
    page_size: Optional[int] = None
    selected_columns: Optional[List[str]] = None
    ui_state: Optional[dict] = None

class PresetResponse(PresetCreate):
    id: str
    usage_count: int = 0
    favorite: bool = False
    created_at: datetime
    updated_at: datetime
    last_used: Optional[datetime] = None
    is_deleted: bool = False
