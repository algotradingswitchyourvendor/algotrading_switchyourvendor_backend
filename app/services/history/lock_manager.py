import asyncio
from typing import Dict

class LockManager:
    """
    Manages per-file asyncio locks to prevent Thundering Herd race conditions.
    Ensures that if 20 users request the same missing cache file simultaneously,
    only 1 performs the S3 HEAD/GET while the rest wait.
    """

    def __init__(self):
        self._locks: Dict[str, asyncio.Lock] = {}
        # Global lock for mutating the _locks dictionary safely
        self._global_lock = asyncio.Lock()

    async def acquire_lock(self, target_date: str) -> asyncio.Lock:
        """
        Retrieves or creates a lock for the specific date and acquires it.
        Returns the lock instance so it can be released later.
        """
        async with self._global_lock:
            if target_date not in self._locks:
                self._locks[target_date] = asyncio.Lock()
            
            lock = self._locks[target_date]
        
        await lock.acquire()
        return lock

    def release_lock(self, target_date: str) -> None:
        """
        Releases the lock for the specific date.
        """
        if target_date in self._locks:
            try:
                self._locks[target_date].release()
            except RuntimeError:
                pass  # Lock was not acquired
