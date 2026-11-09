"""Round-trips rows through the schema in a real PostgreSQL.

Uses metadata.create_all for speed; test_migrations.py covers the Alembic path."""
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.models import Base, Pool, Position, Token, Transaction
from app.db.session import create_schema, make_engine, make_sessionmaker
from app.domain.chains import Chain

URL = os.environ.get("DEXBOT_TEST_DATABASE_URL")
pytestmark = [pytest.mark.postgres, pytest.mark.skipif(not URL, reason="DEXBOT_TEST_DATABASE_URL not set")]


@pytest.fixture
async def session():
    engine = make_engine(URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await create_schema(engine)
    async with make_sessionmaker(engine)() as s:
        yield s
    await engine.dispose()


async def test_schema_round_trip(session):
    t = Token(chain=Chain.BASE, address="0x" + "a" * 40, symbol="TKN", decimals=18)
    q = Token(chain=Chain.BASE, address="0x" + "b" * 40, symbol="USDC", decimals=6)
    session.add_all([t, q])
    await session.flush()
    p = Pool(chain=Chain.BASE, address="0x" + "c" * 40, dex_id="uniswap", base_token_id=t.id, quote_token_id=q.id)
    session.add(p)
    await session.flush()
    now = datetime.now(timezone.utc)
    pos = Position(mode="paper", chain=Chain.BASE, token_id=t.id, pool_id=p.id, strategy="momentum",
                   status="open", size_usd=Decimal("25"), token_amount_raw=Decimal(2**255),
                   planned_max_loss_usd=Decimal("1"), max_hold_until=now + timedelta(minutes=60))
    session.add(pos)
    await session.commit()
    got = (await session.execute(select(Position))).scalar_one()
    assert got.token_amount_raw == Decimal(2**255) and got.chain is Chain.BASE and got.created_at is not None


async def test_idempotency_key_unique(session):
    for _ in range(2):
        session.add(Transaction(chain=Chain.SOLANA, mode="paper", idempotency_key="k1", status="built"))
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_token_unique_per_chain(session):
    addr = "0x" + "d" * 40
    session.add_all([Token(chain=Chain.BASE, address=addr), Token(chain=Chain.BSC, address=addr)])
    await session.commit()          # same address on two chains is fine
    session.add(Token(chain=Chain.BASE, address=addr))
    with pytest.raises(IntegrityError):
        await session.commit()
