import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Optional, Tuple

from app.config.settings import get_settings
from app.services.history.download_manager import DownloadManager
from app.services.history.lock_manager import LockManager
from app.services.history.metadata_manager import MetadataManager
from app.services.history.validation_manager import ValidationManager
from app.services.history.singleflight import SingleFlight

logger = logging.getLogger(__name__)

class CacheManager:
    """
    Orchestrates the entire history disk cache lifecycle.
    Provides safe concurrent access, startup recovery, and LRU cleanup.
    """

    def __init__(self):
        self.settings = get_settings()
        self.metadata = MetadataManager()
        self.locks = LockManager()
        self.validation = ValidationManager()
        self.downloader = DownloadManager(self.validation)
        self._flight = SingleFlight()
        
        # Ensure cache directory exists
        self.cache_dir = Path(self.settings.CACHE_DIRECTORY)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def get_parquet_path(self, target_date: str) -> str:
        """Returns the base directory path for a given date's partitions."""
        return str(self.cache_dir / f"date={target_date}")

    def get_parquet_glob(self, target_date: str) -> str:
        """Returns the glob pattern for DuckDB to query."""
        return str(self.cache_dir / f"date={target_date}" / "**" / "*.parquet")

    def _get_date_cache_size(self, date_dir: str) -> int:
        """Safely calculates the total size of all .parquet chunks in a date directory."""
        total_size = 0
        if not os.path.exists(date_dir):
            return 0
        try:
            for root, _, files in os.walk(date_dir):
                for f in files:
                    if f.endswith('.parquet'):
                        try:
                            total_size += os.path.getsize(os.path.join(root, f))
                        except OSError:
                            pass
        except OSError:
            pass
        return total_size

    def _has_local_parquet(self, date_dir: str) -> bool:
        """Returns True if at least one .parquet file exists recursively."""
        if not os.path.exists(date_dir):
            return False
        for root, _, files in os.walk(date_dir):
            if any(f.endswith('.parquet') for f in files):
                return True
        return False

    def startup_recovery(self) -> None:
        """
        Runs on backend startup. Scans cache directory.
        Removes `.tmp` orphans anywhere in the directory tree.
        """
        logger.info("Starting CacheManager startup recovery...")
        deleted_count = 0

        if not os.path.exists(self.cache_dir):
            return

        for root, dirs, files in os.walk(self.cache_dir):
            for filename in files:
                if filename.endswith(".tmp"):
                    file_path = os.path.join(root, filename)
                    try:
                        os.remove(file_path)
                        deleted_count += 1
                    except OSError as e:
                        logger.warning(f"Failed to remove temp file {file_path}: {e}")

        logger.info(f"Cache recovery complete. Deleted orphans: {deleted_count}")

    async def cleanup_lru(self) -> None:
        """
        Checks total cache size and deletes oldest accessed files 
        until below CACHE_MAX_SIZE_GB.
        """
        try:
            files = os.listdir(self.cache_dir)
        except FileNotFoundError:
            return

        total_bytes = 0
        access_records = []

        for filename in files:
            if filename.startswith("date="):
                target_date = filename.split("=")[1]
                date_dir = str(self.cache_dir / filename)
                meta = self.metadata.load_metadata(target_date)
                size = self._get_date_cache_size(date_dir)
                
                if not meta:
                    if size > 0:
                        total_bytes += size
                        access_records.append({
                            "target_date": target_date,
                            "last_access": "",  # Missing metadata -> oldest priority
                            "size": size
                        })
                    continue
                    
                total_bytes += size
                access_records.append({
                    "target_date": target_date,
                    "last_access": meta.get("last_access_timestamp", ""),
                    "size": size
                })

        max_bytes = self.settings.CACHE_MAX_SIZE_GB * 1024 * 1024 * 1024
        
        if total_bytes <= max_bytes:
            return

        logger.info(f"Cache size ({total_bytes / 1024**3:.2f}GB) exceeds max ({self.settings.CACHE_MAX_SIZE_GB}GB). Running LRU cleanup.")
        
        # Sort by oldest access first
        access_records.sort(key=lambda x: x["last_access"])

        for record in access_records:
            if total_bytes <= max_bytes:
                break
                
            target_date = record["target_date"]
            
            # Acquire lock to safely delete
            try:
                # Use a fast timeout for cleanup. If it's locked, skip it.
                lock = await asyncio.wait_for(self.locks.acquire_lock(target_date), timeout=0.1)
                try:
                    dir_path = self.get_parquet_path(target_date)
                    success = True
                    if os.path.exists(dir_path):
                        import shutil
                        try:
                            shutil.rmtree(dir_path)
                        except Exception as e:
                            logger.error(f"Failed to delete {dir_path}: {e}")
                            success = False
                            
                    if success:
                        self.metadata.delete_metadata(target_date)
                        total_bytes -= record["size"]
                        logger.info(f"LRU Evicted: {target_date} ({record['size'] / 1024**2:.2f}MB)")
                except Exception as e:
                    logger.error(f"Unexpected error during LRU eviction for {target_date}: {e}")
                finally:
                    self.locks.release_lock(target_date)
            except asyncio.TimeoutError:
                # File is actively being used/downloaded, skip eviction
                continue

    async def get_or_download(self, target_date: str) -> Optional[str]:
        """
        Core flow: SingleFlight → Lock → Check Meta → Head Object → Download → Unlock.
        Returns the local path to the valid parquet file, or None if unavailable.
        
        SingleFlight ensures that if 50 requests arrive for the same date
        simultaneously, only one performs the S3 HEAD + download pipeline.
        The remaining 49 await the same result.
        """
        result, was_coalesced = await self._flight.do(
            key=target_date,
            fn=lambda: self._do_get_or_download(target_date)
        )
        if was_coalesced:
            logger.info(f"Request coalesced for {target_date} — skipped S3 pipeline")
        return result

    async def _do_get_or_download(self, target_date: str) -> Optional[str]:
        """
        The actual S3 listing + download pipeline for all missing chunks of a date.
        """
        prefix = f"{self.settings.S3_PARQUET_PREFIX}/date={target_date}/"
        date_dir = self.get_parquet_path(target_date)

        # Acquire per-date lock
        try:
            lock = await asyncio.wait_for(
                self.locks.acquire_lock(target_date), 
                timeout=self.settings.DOWNLOAD_LOCK_TIMEOUT
            )
        except asyncio.TimeoutError:
            logger.error(f"Timeout waiting for lock on {target_date}")
            return None

        try:
            # 1. Ensure directory exists
            os.makedirs(date_dir, exist_ok=True)
            
            # 2. Check S3 state
            try:
                s3_objects = await asyncio.to_thread(self.downloader.list_objects, prefix)
            except Exception as e:
                logger.error(f"S3 listing failed for {target_date}: {e}")
                # Fall back to existing local cache if available.
                # DO NOT perform stale-chunk cleanup.
                if self._has_local_parquet(date_dir):
                    logger.info(f"Using local cache fallback for {target_date}: {date_dir}")
                    return self.get_parquet_glob(target_date)
                return None

            if not s3_objects:
                logger.warning(f"No data available in S3 for {target_date}")
                # Fallback to local files if S3 is genuinely empty
                if self._has_local_parquet(date_dir):
                    logger.info(f"Using local cache fallback for {target_date}: {date_dir}")
                    return self.get_parquet_glob(target_date)
                return None

            # Remove stale chunks locally that are no longer in S3
            s3_keys = {obj["key"] for obj in s3_objects}
            if os.path.exists(date_dir):
                for root, dirs, files in os.walk(date_dir):
                    for f in files:
                        if f.endswith('.parquet'):
                            local_path = os.path.join(root, f)
                            rel_path = os.path.relpath(local_path, date_dir).replace('\\', '/')
                            expected_s3_key = f"{prefix}{rel_path}"
                            if expected_s3_key not in s3_keys:
                                logger.info(f"Removing stale local chunk: {rel_path}")
                                try:
                                    os.remove(local_path)
                                except Exception as e:
                                    logger.warning(f"Failed to remove stale chunk {local_path}: {e}")

            # 3. Compare Cache vs S3
            download_count = 0
            total_duration = 0.0

            for obj in s3_objects:
                # Key looks like: prefix/date=YYYY-MM-DD/hour=HH/part-HHMM-SS-<unique-id>.parquet
                # We need to mirror this structure locally
                rel_path = obj["key"].replace(f"{self.settings.S3_PARQUET_PREFIX}/date={target_date}/", "")
                local_path = os.path.join(date_dir, rel_path)
                os.makedirs(os.path.dirname(local_path), exist_ok=True)
                
                is_valid = False
                if os.path.exists(local_path):
                    try:
                        if os.path.getsize(local_path) == obj["content_length"]:
                            is_valid = True
                        else:
                            os.remove(local_path)
                    except OSError:
                        pass
                
                if not is_valid:
                    logger.info(f"Downloading missing chunk: {rel_path}")
                    success, duration = await asyncio.to_thread(
                        self.downloader.stream_download_atomic, obj["key"], local_path
                    )
                    if success:
                        download_count += 1
                        total_duration += duration
                
            # Recalculate actual local Parquet size after stale cleanup & downloads
            actual_local_size = self._get_date_cache_size(date_dir)

            # 4. Save metadata for LRU tracking
            if download_count > 0 or not self.metadata.load_metadata(target_date):
                self.metadata.save_metadata(
                    target_date=target_date,
                    s3_key=prefix,
                    etag=f"chunks-{len(s3_objects)}",
                    last_modified="",
                    file_size=actual_local_size,
                    duration_sec=total_duration
                )
            else:
                existing_meta = self.metadata.load_metadata(target_date)
                if existing_meta:
                    self.metadata.save_metadata(
                        target_date=target_date,
                        s3_key=existing_meta.get("s3_key", prefix),
                        etag=existing_meta.get("etag", f"chunks-{len(s3_objects)}"),
                        last_modified=existing_meta.get("last_modified_s3", ""),
                        file_size=actual_local_size,
                        duration_sec=existing_meta.get("download_duration_sec", 0.0)
                    )

            if not self._has_local_parquet(date_dir):
                return None
            return self.get_parquet_glob(target_date)

        finally:
            self.locks.release_lock(target_date)
