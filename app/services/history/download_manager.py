import logging
import os
import time
from typing import Dict, Tuple

import boto3
from botocore.config import Config

from app.config.settings import get_settings
from app.services.history.validation_manager import ValidationManager

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

    def head_object(self, s3_key: str) -> Tuple[bool, Dict]:
        """
        Performs a lightweight HEAD request to get ETag and size.
        Returns (is_found, metadata).
        """
        try:
            head = self.s3_client.head_object(
                Bucket=self.settings.S3_BUCKET_NAME,
                Key=s3_key
            )
            return True, {
                "etag": head.get("ETag", "").strip('"'),
                "last_modified": str(head.get("LastModified")),
                "content_length": head.get("ContentLength", 0)
            }
        except self.s3_client.exceptions.ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "404":
                return False, {}
            logger.error(f"S3 HEAD error for {s3_key}: {e}")
            raise

    def stream_download_atomic(self, s3_key: str, dest_path: str) -> Tuple[bool, float]:
        """
        Downloads the S3 object in chunks to a `.tmp` file, validates it, 
        and performs an atomic rename to `dest_path`.
        
        Returns (success_bool, duration_sec).
        """
        tmp_path = f"{dest_path}.tmp"
        start_time = time.time()

        try:
            response = self.s3_client.get_object(
                Bucket=self.settings.S3_BUCKET_NAME,
                Key=s3_key
            )

            with open(tmp_path, "wb") as f:
                for chunk in response["Body"].iter_chunks(chunk_size=self.settings.S3_CHUNK_SIZE):
                    f.write(chunk)

            # Verification
            if not self.validation.verify_cache_integrity(tmp_path):
                logger.error(f"Download corrupted for {s3_key}. Discarding.")
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                return False, 0.0

            # Atomic Rename (POSIX safe, avoids partial reads)
            os.replace(tmp_path, dest_path)
            duration = time.time() - start_time
            return True, duration

        except Exception as e:
            logger.error(f"Failed to stream download {s3_key}: {e}")
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            return False, 0.0
