import logging
import os
import pyarrow.parquet as pq

logger = logging.getLogger(__name__)

class ValidationManager:
    """
    Validates cache integrity.
    Ensures parquets are not corrupted or partially written.
    """

    def verify_cache_integrity(self, file_path: str) -> bool:
        """
        Verifies that a parquet file exists, has a size > 0,
        and has a valid parquet schema.
        """
        if not os.path.exists(file_path):
            logger.warning(f"Cache validation failed: File not found ({file_path})")
            return False

        if os.path.getsize(file_path) == 0:
            logger.warning(f"Cache validation failed: File is 0 bytes ({file_path})")
            return False

        try:
            # pq.ParquetFile lazily loads metadata, which quickly verifies 
            # if the file is structurally a valid parquet without reading it fully.
            pq_file = pq.ParquetFile(file_path)
            if not pq_file.schema:
                logger.warning(f"Cache validation failed: No schema found ({file_path})")
                return False
            return True
        except Exception as e:
            logger.error(f"Cache validation failed: Corrupt parquet ({file_path}) - {e}")
            return False
