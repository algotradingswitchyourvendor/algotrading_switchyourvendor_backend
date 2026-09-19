import logging
from collections import deque
from typing import List, Dict, Optional
from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

class LogCaptureHandler(logging.Handler):
    """
    A thread-safe logging handler that keeps the most recent N log records in memory.
    Used for exposing recent application logs to the admin UI.
    """
    def __init__(self, maxlen: int = 500):
        super().__init__()
        import threading
        self.log_records = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord):
        try:
            # Map standard logger names to services for the UI
            service = "api"
            if "scheduler" in record.name or "upstox" in record.name:
                service = "scheduler"
            elif "worker" in record.name:
                service = "worker"
            elif "uvicorn" in record.name or "fastapi" in record.name:
                service = "api"
            elif record.name.startswith("app."):
                service = "api"

            # Avoid storing extremely large exception traces or sensitive data
            msg = self.format(record)
            if len(msg) > 1000:
                msg = msg[:1000] + "... [truncated]"

            log_entry = {
                "timestamp": datetime.fromtimestamp(record.created, tz=IST).isoformat(),
                "level": record.levelname,
                "service": service,
                "message": msg,
                "logger_name": record.name
            }
            
            with self._lock:
                self.log_records.appendleft(log_entry)
        except Exception:
            self.handleError(record)

    def get_logs(self, service: Optional[str] = None, level: Optional[str] = None, limit: int = 50) -> List[Dict]:
        """Returns the most recent logs, optionally filtered."""
        results = []
        with self._lock:
            # log_records is a deque populated using appendleft, so it is already newest-first
            for record in self.log_records:
                if service and service.lower() != "all":
                    if record["service"] != service.lower():
                        continue
                if level and level.lower() != "all":
                    if record["level"] != level.upper():
                        continue
                
                results.append(record)
                if len(results) >= limit:
                    break
                    
        return results

# Singleton instance to be attached to the root logger
log_capture_handler = LogCaptureHandler(maxlen=500)
# Simple formatter for the message
log_capture_handler.setFormatter(logging.Formatter("%(message)s"))
