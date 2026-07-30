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
        """Returns the full path to the parquet cache file."""
        return str(self.cache_dir / f"{target_date}_Equity.parquet")

    def startup_recovery(self) -> None:
        """
        Runs on backend startup. Scans cache directory.
        Removes `.tmp` orphans, missing metadata orphans, and corrupted files.
        """
        logger.info("Starting CacheManager startup recovery...")
        valid_count = 0
        deleted_count = 0

        try:
            files = os.listdir(self.cache_dir)
        except FileNotFoundError:
            return

        for filename in files:
            file_path = str(self.cache_dir / filename)
            
            # 1. Clean tmp orphans from crashed downloads
            if filename.endswith(".tmp"):
                os.remove(file_path)
                deleted_count += 1
                continue

            # 2. Check parquet files
            if filename.endswith(".parquet"):
                target_date = filename.split("_")[0]
                meta_path = self.metadata.get_meta_path(target_date)
                
                # Missing metadata?
                if not os.path.exists(meta_path):
                    logger.warning(f"Orphaned parquet found (no meta): {filename}")
                    os.remove(file_path)
                    deleted_count += 1
                    continue
                
                # Corrupt parquet?
                if not self.validation.verify_cache_integrity(file_path):
                    logger.warning(f"Corrupt parquet found on startup: {filename}")
                    os.remove(file_path)
                    if os.path.exists(meta_path):
                        os.remove(meta_path)
                    deleted_count += 1
                    continue
                
                valid_count += 1

            # 3. Check meta files without parquets
            elif filename.endswith(self.settings.CACHE_METADATA_FILE_EXT):
                target_date = filename.split("_")[0]
                parquet_path = self.get_parquet_path(target_date)
                if not os.path.exists(parquet_path):
                    logger.warning(f"Orphaned meta found (no parquet): {filename}")
                    os.remove(file_path)
                    deleted_count += 1

        logger.info(f"Cache recovery complete. Valid files: {valid_count}, Deleted orphans: {deleted_count}")

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
            if filename.endswith(".parquet"):
                target_date = filename.split("_")[0]
                parquet_path = str(self.cache_dir / filename)
                meta = self.metadata.load_metadata(target_date)
                
                if not meta:
                    continue
                
                size = meta.get("file_size_bytes", os.path.getsize(parquet_path))
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
                
                parquet_path = self.get_parquet_path(target_date)
                if os.path.exists(parquet_path):
                    os.remove(parquet_path)
                self.metadata.delete_metadata(target_date)
                
                total_bytes -= record["size"]
                logger.info(f"LRU Evicted: {target_date} ({record['size'] / 1024**2:.2f}MB)")
                
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
        The actual S3 HEAD + download pipeline. Only one instance of this
        runs per target_date at any given time (enforced by SingleFlight).
        """
        s3_key = f"{self.settings.S3_PARQUET_PREFIX}/{target_date}_Equity.parquet"
        parquet_path = self.get_parquet_path(target_date)

        # Acquire per-file lock to prevent concurrent downloads of the same date
        try:
            lock = await asyncio.wait_for(
                self.locks.acquire_lock(target_date), 
                timeout=self.settings.DOWNLOAD_LOCK_TIMEOUT
            )
        except asyncio.TimeoutError:
            logger.error(f"Timeout waiting for lock on {target_date}")
            return None

        try:
            # 1. Check local metadata
            local_meta = self.metadata.load_metadata(target_date)
            
            # 2. Check S3 state
            found, s3_meta = await asyncio.to_thread(self.downloader.head_object, s3_key)

            if not found:
                logger.warning(f"Data not available in S3 for {target_date}")
                return None

            s3_etag = s3_meta["etag"]

            # 3. Compare Cache vs S3
            is_valid_cache = False
            if local_meta and local_meta.get("etag") == s3_etag:
                if os.path.exists(parquet_path):
                    if self.validation.verify_cache_integrity(parquet_path):
                        is_valid_cache = True
                        self.metadata.touch_metadata(target_date)
                        logger.debug(f"Cache Hit: {target_date}")
                    else:
                        logger.warning(f"Cached parquet {target_date} corrupted. Deleting.")
                        os.remove(parquet_path)
                        self.metadata.delete_metadata(target_date)

            if is_valid_cache:
                return parquet_path

            # 4. Cache Miss or Invalid → Download
            logger.info(f"Cache Miss: {target_date}. Starting download...")
            success, duration = await asyncio.to_thread(
                self.downloader.stream_download_atomic, s3_key, parquet_path
            )

            if success:
                logger.info(f"Download complete for {target_date} in {duration:.2f}s")
                self.metadata.save_metadata(
                    target_date=target_date,
                    s3_key=s3_key,
                    etag=s3_etag,
                    last_modified=s3_meta["last_modified"],
                    file_size=s3_meta["content_length"],
                    duration_sec=duration
                )
                return parquet_path
            else:
                return None

        finally:
            self.locks.release_lock(target_date)
