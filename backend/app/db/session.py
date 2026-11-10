from __future__ import annotations

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.db.models import Base

# libpq query keys that asyncpg.connect() does not accept. sslmode is translated below.
_DROP_QUERY_KEYS = ("sslmode", "channel_binding")


def split_database_url(url: str) -> tuple[str, dict]:
    """Return an asyncpg SQLAlchemy URL and connect args.

    Hosted Postgres URIs use libpq's `sslmode=require`. asyncpg rejects that
    keyword, so it is removed and SSL is requested explicitly. The password
    stays in the URL and is never logged by this function.
    """
    parsed = make_url(url)
    if parsed.drivername in {"postgresql", "postgres"}:
        parsed = parsed.set(drivername="postgresql+asyncpg")
    elif parsed.drivername != "postgresql+asyncpg":
        raise ValueError(f"unsupported database URL scheme {parsed.drivername}")
    query = dict(parsed.query)
    sslmode = str(query.get("sslmode") or "")
    for key in _DROP_QUERY_KEYS:
        query.pop(key, None)
    parsed = parsed.set(query=query)
    connect_args: dict = {"timeout": 5, "command_timeout": 10}
    if sslmode and sslmode != "disable":
        connect_args["ssl"] = True
    return parsed.render_as_string(hide_password=False), connect_args


def make_engine(url: str) -> AsyncEngine:
    # Connect and command timeouts keep a dead socket from hanging the process after a suspend.
    sqlalchemy_url, connect_args = split_database_url(url)
    return create_async_engine(
        sqlalchemy_url, pool_pre_ping=True, pool_timeout=5, connect_args=connect_args,
    )


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)


async def create_schema(engine: AsyncEngine) -> None:
    """Development helper. Production migrations will use Alembic (Phase 2)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
