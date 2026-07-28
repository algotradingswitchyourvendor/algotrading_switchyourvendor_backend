import json
import os
import uuid
import hashlib
import tempfile
import threading
from abc import ABC, abstractmethod
from typing import List, Optional, Any
from datetime import datetime, timezone

from app.schemas.preset import PresetCreate, PresetUpdate, PresetResponse

class BasePresetRepository(ABC):
    @abstractmethod
    def get_all(self, include_deleted: bool = False) -> List[PresetResponse]:
        pass

    @abstractmethod
    def get_by_id(self, preset_id: str) -> Optional[PresetResponse]:
        pass

    @abstractmethod
    def create(self, preset: PresetCreate) -> PresetResponse:
        pass

    @abstractmethod
    def update(self, preset_id: str, updates: PresetUpdate) -> Optional[PresetResponse]:
        pass

    @abstractmethod
    def delete(self, preset_id: str) -> bool:
        pass

    @abstractmethod
    def increment_usage(self, preset_id: str) -> Optional[PresetResponse]:
        pass
        
    @abstractmethod
    def find_duplicate(self, name: str, request: Any) -> Optional[PresetResponse]:
        pass


class FilePresetRepository(BasePresetRepository):
    """
    JSON file-backed repository for User Presets.
    
    IMPORTANT: This repository is intended for single-instance, single-worker deployments only.
    For multi-worker or cloud deployments, this must be replaced with a PostgreSQL repository
    to prevent file locking bottlenecks and ensure proper concurrent state management.
    """
    def __init__(self, storage_path: str = None):
        if not storage_path:
            storage_path = os.path.join(
                os.path.dirname(__file__), "..", "storage", "user_presets.json"
            )
        self.storage_path = storage_path
        self._lock = threading.Lock()
        self._ensure_storage_exists()

    def _ensure_storage_exists(self):
        os.makedirs(os.path.dirname(self.storage_path), exist_ok=True)
        if not os.path.exists(self.storage_path):
            with open(self.storage_path, "w") as f:
                json.dump([], f)

    def _read_data(self) -> List[dict]:
        try:
            with open(self.storage_path, "r") as f:
                return json.load(f)
        except Exception:
            return []

    def _write_data(self, data: List[dict]):
        # Write atomically using a temporary file to avoid corruption
        dir_name = os.path.dirname(self.storage_path)
        fd, temp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(temp_path, self.storage_path)
        except Exception:
            os.remove(temp_path)
            raise

    def _normalize_dict(self, d):
        if isinstance(d, dict):
            return {k: self._normalize_dict(v) for k, v in sorted(d.items())}
        if isinstance(d, list):
            # Try to sort the list if elements are comparable, else keep original order
            try:
                # recursively normalize items first
                normalized_list = [self._normalize_dict(i) for i in d]
                # we can't easily sort lists of dicts directly in python, but we can sort by string representation
                return sorted(normalized_list, key=lambda x: json.dumps(x, sort_keys=True))
            except Exception:
                return [self._normalize_dict(i) for i in d]
        return d

    def _hash_conditions(self, payload: Any) -> str:
        """Generate a deterministic hash for a payload dictionary or object."""
        data = payload.model_dump() if hasattr(payload, "model_dump") else payload
        
        # Deep normalize to ensure identical content generates identical hashes regardless of order
        normalized = self._normalize_dict(data)
        
        json_str = json.dumps(normalized, sort_keys=True)
        return hashlib.sha256(json_str.encode('utf-8')).hexdigest()

    def get_all(self, include_deleted: bool = False) -> List[PresetResponse]:
        with self._lock:
            data = self._read_data()
            presets = []
            for item in data:
                if not include_deleted and item.get("is_deleted", False):
                    continue
                presets.append(PresetResponse(**item))
            return presets

    def get_by_id(self, preset_id: str) -> Optional[PresetResponse]:
        with self._lock:
            data = self._read_data()
            for item in data:
                if item.get("id") == preset_id:
                    return PresetResponse(**item)
            return None

    def find_duplicate(self, name: str, request: Any) -> Optional[PresetResponse]:
        with self._lock:
            data = self._read_data()
            target_hash = self._hash_conditions(request.model_dump() if hasattr(request, "model_dump") else request)
            
            for item in data:
                if item.get("is_deleted", False):
                    continue
                
                # Match by name OR payload
                item_hash = self._hash_conditions(item.get("request", {}))
                
                if item.get("name") == name or item_hash == target_hash:
                    return PresetResponse(**item)
                    
            return None

    def create(self, preset: PresetResponse) -> PresetResponse:
        with self._lock:
            data = self._read_data()
            data.append(preset.model_dump(mode="json"))
            self._write_data(data)
            return preset

    def update(self, preset_id: str, updates: PresetUpdate, now_iso: str) -> Optional[PresetResponse]:
        with self._lock:
            data = self._read_data()
            for i, item in enumerate(data):
                if item.get("id") == preset_id:
                    if updates.name is not None:
                        item["name"] = updates.name
                    if updates.description is not None:
                        item["description"] = updates.description
                    if updates.request is not None:
                        item["request"] = updates.request.model_dump()
                    if updates.version is not None:
                        item["version"] = updates.version
                    if updates.favorite is not None:
                        item["favorite"] = updates.favorite
                    if updates.sorting is not None:
                        item["sorting"] = updates.sorting
                    if updates.page_size is not None:
                        item["page_size"] = updates.page_size
                    if updates.selected_columns is not None:
                        item["selected_columns"] = updates.selected_columns
                        
                    item["updated_at"] = now_iso
                    
                    updated_preset = PresetResponse(**item)
                    data[i] = updated_preset.model_dump(mode="json")
                    self._write_data(data)
                    return updated_preset
            return None

    def delete(self, preset_id: str, now_iso: str) -> bool:
        with self._lock:
            data = self._read_data()
            for i, item in enumerate(data):
                if item.get("id") == preset_id:
                    item["is_deleted"] = True
                    item["updated_at"] = now_iso
                    self._write_data(data)
                    return True
            return False

    def increment_usage(self, preset_id: str, now_iso: str) -> Optional[PresetResponse]:
        with self._lock:
            data = self._read_data()
            for i, item in enumerate(data):
                if item.get("id") == preset_id:
                    item["usage_count"] = item.get("usage_count", 0) + 1
                    item["last_used"] = now_iso
                    
                    updated_preset = PresetResponse(**item)
                    data[i] = updated_preset.model_dump(mode="json")
                    self._write_data(data)
                    return updated_preset
            return None
