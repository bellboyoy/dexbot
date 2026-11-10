"""Hosted launch rows replace local rows, including gaps, without duplicating them."""
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select

from app.db.models import LaunchSnapshot, LaunchWatch, ProcessHeartbeat
from app.domain.chains import Chain
from app.research.sync import (
    read_collector_heartbeat, read_launches, write_collector_heartbeat, write_launches,
)

URL = os.environ.get("DEXBOT_TEST_DATABASE_URL")
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
POOL = "0x" + "ab" * 32


@pytest.fixture
async def session():
    if not URL:
        pytest.skip("DEXBOT_TEST_DATABASE_URL not set")
    from app.db.models import Base
    from app.db.session import create_schema, make_engine, make_sessionmaker
    engine = make_engine(URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await create_schema(engine)
    async with make_sessionmaker(engine)() as made:
        yield made
    await engine.dispose()


@pytest.mark.postgres
async def test_sync_round_trip_keeps_misses_and_drops_local_only_prices(session):
    watch = LaunchWatch(
        chain=Chain.BASE, pool_address=POOL, token_address="0x" + "cd" * 20,
        quote_address="0x" + "11" * 20, symbol="V4", dex_id="uniswap-v4",
        pool_created_at=NOW, first_seen_at=NOW, launch_at=NOW, status="watching",
        next_due_at=NOW + timedelta(minutes=15), snapshots_taken=1, last_error=None,
    )
    session.add(watch)
    await session.flush()
    session.add(LaunchSnapshot(
        watch_id=watch.id, chain=Chain.BASE, age_minutes=1, due_at=NOW + timedelta(minutes=1),
        observed_at=None, missed=True, not_found=False, source=None, price_usd=None,
    ))
    session.add(LaunchSnapshot(
        watch_id=watch.id, chain=Chain.BASE, age_minutes=5, due_at=NOW + timedelta(minutes=5),
        observed_at=NOW + timedelta(minutes=5), missed=False, not_found=False, source="dexscreener",
        price_usd=Decimal("1.25"), liquidity_usd=Decimal("100"),
    ))
    session.add(ProcessHeartbeat(
        component="collector", last_success_at=NOW, last_attempt_at=NOW, last_error=None,
        consecutive_failures=0, updated_at=NOW,
    ))
    await session.commit()

    payload = await read_launches(session)
    beat = await read_collector_heartbeat(session)
    assert len(payload.watches) == 1 and payload.watches[0].pool_address == POOL
    assert beat is not None and beat["last_success_at"] == NOW

    await session.execute(delete(LaunchSnapshot))
    await session.execute(delete(LaunchWatch))
    await session.execute(delete(ProcessHeartbeat))
    await session.commit()
    session.expunge_all()

    async with session.begin():
        first = await write_launches(session, payload)
        await write_collector_heartbeat(session, beat)
    assert first["watches_inserted"] == 1
    assert first["snapshots_upserted"] == 2
    copied = await session.scalar(select(LaunchWatch).where(LaunchWatch.pool_address == POOL))
    assert copied is not None and copied.id != watch.id
    missed = await session.scalar(select(LaunchSnapshot).where(LaunchSnapshot.age_minutes == 1))
    priced = await session.scalar(select(LaunchSnapshot).where(LaunchSnapshot.age_minutes == 5))
    assert missed.missed is True and missed.price_usd is None and missed.observed_at is None
    assert priced.missed is False and priced.price_usd == Decimal("1.25")

    session.add(LaunchSnapshot(
        watch_id=copied.id, chain=Chain.BASE, age_minutes=15, due_at=NOW + timedelta(minutes=15),
        observed_at=NOW, missed=False, not_found=False, source="local-only", price_usd=Decimal("9"),
    ))
    await session.commit()
    async with session.begin():
        second = await write_launches(session, payload)
    assert second["watches_inserted"] == 0 and second["watches_updated"] == 1
    assert second["snapshots_removed"] == 1
    ages = set(await session.scalars(select(LaunchSnapshot.age_minutes)))
    assert ages == {1, 5}
    assert await session.scalar(select(func.count()).select_from(LaunchWatch)) == 1
    restored = await read_collector_heartbeat(session)
    assert restored["last_success_at"] == NOW
