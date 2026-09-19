"""
Alembic migration environment for MarketPulse.

Configured for async SQLAlchemy with asyncpg.
DATABASE_URL is read from the same settings as the application.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Load app models so metadata is populated
from app.db.database import Base
import app.db.models  # noqa: F401 — registers all ORM models with Base.metadata

from app.config.settings import get_settings

# Alembic Config object
config = context.config

# Set up logging if defined in alembic.ini
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Metadata for autogenerate
target_metadata = Base.metadata


def get_url() -> str:
    settings = get_settings()
    if not settings.DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is not set. Cannot run Alembic migrations."
        )
    
    import urllib.parse
    db_url = settings.DATABASE_URL
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+asyncpg://", 1)

    parsed = urllib.parse.urlparse(db_url)
    query_params = urllib.parse.parse_qs(parsed.query)
    
    if "channel_binding" in query_params:
        del query_params["channel_binding"]
    if "sslmode" in query_params:
        query_params["ssl"] = query_params.pop("sslmode")
        
    new_query = urllib.parse.urlencode(query_params, doseq=True)
    parsed = parsed._replace(query=new_query)
    return urllib.parse.urlunparse(parsed)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (no DB connection — just generates SQL)."""
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations using the async engine."""
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = get_url()

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (connect to DB)."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
