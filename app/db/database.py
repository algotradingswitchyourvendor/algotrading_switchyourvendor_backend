"""
Database engine and session management.

Uses SQLAlchemy async with asyncpg driver.
Connection string loaded from DATABASE_URL environment variable.
"""

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from typing import AsyncGenerator

from app.config.settings import get_settings


class Base(DeclarativeBase):
    pass


def _create_engine():
    settings = get_settings()
    if not settings.DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL environment variable is not set. "
            "Please configure a PostgreSQL connection string."
        )
        
    import urllib.parse
    
    db_url = settings.DATABASE_URL
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+asyncpg://", 1)

    parsed = urllib.parse.urlparse(db_url)
    query_params = urllib.parse.parse_qs(parsed.query)
    
    # asyncpg does not support channel_binding
    if "channel_binding" in query_params:
        del query_params["channel_binding"]
    
    # asyncpg expects 'ssl=require' rather than 'sslmode=require'
    if "sslmode" in query_params:
        query_params["ssl"] = query_params.pop("sslmode")
        
    # Rebuild URL
    new_query = urllib.parse.urlencode(query_params, doseq=True)
    parsed = parsed._replace(query=new_query)
    db_url = urllib.parse.urlunparse(parsed)

    return create_async_engine(
        db_url,
        echo=False,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
    )


# Module-level engine and session factory (created lazily)
_engine = None
_async_session_factory = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = _create_engine()
    return _engine


def get_session_factory():
    global _async_session_factory
    if _async_session_factory is None:
        _async_session_factory = async_sessionmaker(
            get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _async_session_factory


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: yields an async database session."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db():
    """
    Validate database connectivity on startup.
    (Alembic manages migrations and schema creation in production).
    """
    engine = get_engine()
    from sqlalchemy import text
    async with engine.begin() as conn:
        await conn.execute(text("SELECT 1"))
