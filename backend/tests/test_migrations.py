"""Alembic migrations from a clean database: upgrade, no drift vs models, full downgrade, re-upgrade."""
import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

URL = os.environ.get("DEXBOT_TEST_DATABASE_URL")
pytestmark = [pytest.mark.postgres, pytest.mark.skipif(not URL, reason="DEXBOT_TEST_DATABASE_URL not set")]
BACKEND = Path(__file__).resolve().parents[1]


def alembic(*args: str) -> str:
    env = {**os.environ, "DEXBOT_DATABASE_URL": URL}
    res = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=BACKEND, env=env,
                         capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, res.stderr[-2000:]
    return res.stdout + res.stderr


async def _reset_schema() -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    engine = create_async_engine(URL)
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await engine.dispose()


def test_migrations_clean_cycle():
    asyncio.run(_reset_schema())          # truly clean database
    alembic("upgrade", "head")
    assert "No new upgrade operations detected" in alembic("check")
    alembic("downgrade", "base")
    alembic("upgrade", "head")            # must succeed again (enum types dropped on downgrade)
    assert "(head)" in alembic("current")
