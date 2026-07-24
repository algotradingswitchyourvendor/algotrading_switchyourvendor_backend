"""
Query Engine — Unified execution engine for all scanner operations.

Public API:
    from app.services.query_engine import execute_query
    records, meta = execute_query(request, cache)
"""

from app.services.query_engine.engine import execute_query

__all__ = ["execute_query"]
