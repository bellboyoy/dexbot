import json
import pathlib

import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture
def load_fixture():
    def _load(name: str):
        return json.loads((FIXTURES / name).read_text())
    return _load


class FakeClock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
async def pg_sessionmaker():
    """Fresh schema in the test PostgreSQL (DEXBOT_TEST_DATABASE_URL)."""
    import os

    from sqlalchemy import text

    from app.db.session import create_schema, make_engine, make_sessionmaker

    url = os.environ.get("DEXBOT_TEST_DATABASE_URL")
    if not url:
        pytest.skip("DEXBOT_TEST_DATABASE_URL not set")
    engine = make_engine(url)
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await create_schema(engine)
    yield make_sessionmaker(engine)
    await engine.dispose()
