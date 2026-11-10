from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.db.models import Base


def make_engine(url: str) -> AsyncEngine:
    # Connect and command timeouts keep a dead socket from hanging the process after a suspend.
    return create_async_engine(
        url, pool_pre_ping=True, pool_timeout=5,
        connect_args={"timeout": 5, "command_timeout": 10},
    )


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)


async def create_schema(engine: AsyncEngine) -> None:
    """Development helper. Production migrations will use Alembic (Phase 2)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
