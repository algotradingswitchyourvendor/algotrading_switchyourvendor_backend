import logging
import os
import duckdb

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
            # Use duckdb's built-in parquet metadata reader. 
            # This avoids pyarrow DLL loading issues on strict Windows environments.
            duckdb.execute(f"SELECT * FROM parquet_metadata('{file_path}') LIMIT 1")
            return True
        except Exception as e:
            logger.error(f"Cache validation failed: Corrupt parquet ({file_path}) - {e}")
            return False
