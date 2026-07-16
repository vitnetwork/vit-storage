import logging
from typing import AsyncGenerator
from sqlalchemy import text, inspect
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from tachyon.core.config import settings

logger = logging.getLogger(__name__)

# Resolve and standardise database URL
db_url = settings.DATABASE_URL
# Convert sqlite:// to sqlite+aiosqlite:// for async compatibility
if db_url.startswith("sqlite://") and not db_url.startswith("sqlite+aiosqlite://"):
    db_url = db_url.replace("sqlite://", "sqlite+aiosqlite://")

logger.info(f"Initializing async database engine with URL: {db_url}")

async_engine = create_async_engine(
    db_url,
    echo=False,
    pool_pre_ping=True
)

AsyncSessionLocal = async_sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    expire_on_commit=False
)

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency injection helper for FastAPI endpoints."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

# ---------------------------------------------------------------------------
# Column-level schema migrations
# Each entry: (table_name, column_name, column_definition)
# These run after create_all() to handle columns added after initial deploy.
# ---------------------------------------------------------------------------
_COLUMN_MIGRATIONS = [
    ("tachyon_manifests", "content_type", "VARCHAR(128)"),
    ("tachyon_manifests", "tags",         "JSON"),
    ("tachyon_manifests", "owner_user_id","INTEGER"),   # safety re-check
]

async def _run_column_migrations() -> None:
    """
    Idempotent per-column ALTER TABLE migrations.

    Works for both SQLite (PRAGMA table_info) and PostgreSQL
    (information_schema.columns).  Silently skips columns that already exist.
    """
    is_sqlite = db_url.startswith("sqlite")

    async with async_engine.begin() as conn:
        for table, col, col_def in _COLUMN_MIGRATIONS:
            try:
                # --- discover existing columns ---
                if is_sqlite:
                    rows = await conn.execute(text(f"PRAGMA table_info({table})"))
                    existing = {row[1] for row in rows}          # row[1] = name
                else:
                    rows = await conn.execute(text(
                        "SELECT column_name "
                        "FROM information_schema.columns "
                        f"WHERE table_name = '{table}'"
                    ))
                    existing = {row[0] for row in rows}

                if col not in existing:
                    await conn.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN {col} {col_def}")
                    )
                    logger.info(f"Schema migration applied: {table}.{col} ({col_def})")
                else:
                    logger.debug(f"Schema migration skipped (already exists): {table}.{col}")

            except Exception as exc:
                # Non-fatal: log and continue — the column may already be present
                # under a race condition or a different DB dialect quirk.
                logger.warning(
                    f"Schema migration check for {table}.{col} encountered: {exc}"
                )


async def init_db() -> None:
    """
    1. Create all tables declared in the ORM models (idempotent).
    2. Apply any pending column-level migrations.
    """
    from tachyon.core.models import Base
    try:
        logger.info("Verifying database schema...")
        async with async_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database schema validated successfully.")
    except Exception as e:
        logger.critical(f"Database schema initialization failed: {e}")
        raise

    # Run column migrations after tables exist
    await _run_column_migrations()
