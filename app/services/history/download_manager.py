import logging
import os
import time
import threading
from typing import Dict, Tuple

import boto3
from botocore.config import Config

from app.config.settings import get_settings
from app.services.history.validation_manager import ValidationManager
from app.services.history.metrics import request_metrics, GlobalCacheStats

logger = logging.getLogger(__name__)

class DownloadManager:
    """
    Handles streaming S3 downloads with atomic file replacement to prevent corrupted caches.
    """

    def __init__(self, validation_manager: ValidationManager):
        self.settings = get_settings()
        self.validation = validation_manager
        
        # Boto3 client with optimized retries and connection pooling
        s3_config = Config(
            retries={"max_attempts": 3, "mode": "standard"},
            max_pool_connections=10
        )
        self.s3_client = boto3.client("s3", config=s3_config)

        # Thread-safe TTL cache for HEAD responses: {s3_key: (timestamp, (found, meta))}
        self._head_cache: Dict[str, Tuple[float, Tuple[bool, Dict]]] = {}
        self._head_cache_lock = threading.Lock()

    def _get_cached_head(self, s3_key: str) -> Tuple[bool, Dict] | None:
        """Returns cached HEAD result if within TTL, otherwise None."""
        with self._head_cache_lock:
            entry = self._head_cache.get(s3_key)
            if entry is None:
                return None
            cached_at, result = entry
            if (time.monotonic() - cached_at) < self.settings.S3_HEAD_TTL_SECONDS:
                return result
            # Expired — remove stale entry
            del self._head_cache[s3_key]
            return None

    def _set_cached_head(self, s3_key: str, result: Tuple[bool, Dict]) -> None:
        """Stores a HEAD result in the TTL cache."""
        with self._head_cache_lock:
            self._head_cache[s3_key] = (time.monotonic(), result)

    def head_object(self, s3_key: str) -> Tuple[bool, Dict]:
        """
        Performs a lightweight HEAD request to get ETag and size.
        Returns (is_found, metadata).
        Uses a configurable TTL cache to avoid repeated network calls.
        """
        cached = self._get_cached_head(s3_key)
        if cached is not None:
            if self.settings.ENABLE_HISTORY_METRICS:
                GlobalCacheStats.record_head_result(True)
                metrics = request_metrics.get()
                if metrics:
                    metrics.head_cache_hit = True
            logger.debug(f"HEAD Cache Hit: {s3_key}")
            return cached

        start_time = time.perf_counter()
        try:
            head = self.s3_client.head_object(
                Bucket=self.settings.S3_BUCKET_NAME,
                Key=s3_key
            )
            elapsed = (time.perf_counter() - start_time) * 1000
            
            if self.settings.ENABLE_HISTORY_METRICS:
                GlobalCacheStats.record_head_result(False)
                metrics = request_metrics.get()
                if metrics:
                    metrics.head_cache_hit = False
                    metrics.t_head_val = elapsed
                    
            logger.info(f"HEAD Object ({s3_key}): {elapsed:.2f} ms")
            result = (True, {
                "etag": head.get("ETag", "").strip('"'),
                "last_modified": str(head.get("LastModified")),
                "content_length": head.get("ContentLength", 0)
            })
            self._set_cached_head(s3_key, result)
            return result
        except self.s3_client.exceptions.ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "404":
                result = (False, {})
                self._set_cached_head(s3_key, result)
                return result
            logger.error(f"S3 HEAD error for {s3_key}: {e}")
            raise

    def stream_download_atomic(self, s3_key: str, dest_path: str) -> Tuple[bool, float]:
        """
        Downloads the S3 object in chunks to a `.tmp` file, validates it, 
        and performs an atomic rename to `dest_path`.
        
        Returns (success_bool, duration_sec).
        """
        tmp_path = f"{dest_path}.tmp"
        start_time = time.perf_counter()

        try:
            get_start = time.perf_counter()
            response = self.s3_client.get_object(
                Bucket=self.settings.S3_BUCKET_NAME,
                Key=s3_key
            )
            get_elapsed = (time.perf_counter() - get_start) * 1000
            logger.info(f"GET Object ({s3_key}): {get_elapsed:.2f} ms")

            download_start = time.perf_counter()
            with open(tmp_path, "wb") as f:
                for chunk in response["Body"].iter_chunks(chunk_size=self.settings.S3_CHUNK_SIZE):
                    f.write(chunk)
            download_elapsed = (time.perf_counter() - download_start) * 1000
            logger.info(f"Download ({s3_key}): {download_elapsed:.2f} ms")

            # Verification
            val_start = time.perf_counter()
            is_valid = self.validation.verify_cache_integrity(tmp_path)
            val_elapsed = (time.perf_counter() - val_start) * 1000
            logger.info(f"Validation ({s3_key}): {val_elapsed:.2f} ms")

            if not is_valid:
                logger.error(f"Download corrupted for {s3_key}. Discarding.")
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                return False, 0.0

            # Atomic Rename (POSIX safe, avoids partial reads)
            os.replace(tmp_path, dest_path)
            duration = time.perf_counter() - start_time
            return True, duration

        except Exception as e:
            logger.error(f"Failed to stream download {s3_key}: {e}")
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            return False, 0.0
