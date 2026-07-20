import json
import logging
import os
from datetime import datetime
from typing import Dict, Optional

from app.config.settings import get_settings

logger = logging.getLogger(__name__)

class MetadataManager:
    """
    Manages the lifecycle of `.meta.json` files for cached parquets.
    Loads and saves metadata safely.
    """

    def __init__(self):
        self.settings = get_settings()

    def get_meta_path(self, target_date: str) -> str:
        """Returns the full path for a given date's metadata file."""
        return os.path.join(
            self.settings.CACHE_DIRECTORY,
            f"{target_date}_Equity{self.settings.CACHE_METADATA_FILE_EXT}"
        )

    def load_metadata(self, target_date: str) -> Optional[Dict]:
        """Loads metadata for a specific date, returning None if missing/corrupt."""
        meta_path = self.get_meta_path(target_date)
        if not os.path.exists(meta_path):
            return None

        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Failed to read metadata for {target_date}: {e}")
            return None

    def save_metadata(self, target_date: str, s3_key: str, etag: str, last_modified: str, file_size: int, duration_sec: float) -> None:
        """Saves metadata for a downloaded parquet."""
        meta_path = self.get_meta_path(target_date)
        
        metadata = {
            "target_date": target_date,
            "s3_key": s3_key,
            "etag": etag,
            "last_modified_s3": last_modified,
            "file_size_bytes": file_size,
            "cached_timestamp": datetime.utcnow().isoformat(),
            "last_access_timestamp": datetime.utcnow().isoformat(),
            "download_duration_sec": duration_sec,
            "cache_version": "1.0",
        }

        try:
            # Atomic write via temp file
            tmp_meta_path = f"{meta_path}.tmp"
            with open(tmp_meta_path, "w", encoding="utf-8") as f:
                json.dump(metadata, f, indent=2)
            os.replace(tmp_meta_path, meta_path)
        except Exception as e:
            logger.error(f"Failed to save metadata for {target_date}: {e}")
            if os.path.exists(tmp_meta_path):
                os.remove(tmp_meta_path)

    def touch_metadata(self, target_date: str) -> None:
        """Updates the last_access_timestamp to support LRU eviction."""
        metadata = self.load_metadata(target_date)
        if metadata:
            metadata["last_access_timestamp"] = datetime.utcnow().isoformat()
            meta_path = self.get_meta_path(target_date)
            try:
                # Direct write is okay for a simple touch
                with open(meta_path, "w", encoding="utf-8") as f:
                    json.dump(metadata, f, indent=2)
            except Exception as e:
                logger.warning(f"Failed to touch metadata for {target_date}: {e}")

    def delete_metadata(self, target_date: str) -> None:
        """Removes the metadata file."""
        meta_path = self.get_meta_path(target_date)
        try:
            if os.path.exists(meta_path):
                os.remove(meta_path)
        except Exception as e:
            logger.warning(f"Failed to delete metadata for {target_date}: {e}")
