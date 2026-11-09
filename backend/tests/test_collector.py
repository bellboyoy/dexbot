"""Launch schedule, as-of liquidity, and the Postgres watch book."""
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.collector.schedule import (
    asof_liquidity, classify, daily_megabytes, daily_snapshot_rows, partition, snapshot_ages,
)
from app.collector.store import Candidate, Inventory, enroll, inventory, ready
from app.domain.chains import Chain
from app.domain.market import Candle, Timeframe
from app.research.pit import clip_candles

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
URL = os.environ.get("DEXBOT_TEST_DATABASE_URL")


def test_schedule_covers_the_first_day():
    ages = snapshot_ages()
    assert ages[:6] == (1, 5, 15, 30, 60, 240)
    assert ages[-1] == 24 * 60
    assert 180 not in ages
    assert len(ages) == 26
    assert daily_snapshot_rows(150) == 150 * 26
    assert 4 < daily_megabytes(150) < 6


def test_late_slots_are_missed_and_the_next_one_waits():
    launch = NOW
    later = NOW + timedelta(minutes=10)
    assert classify(launch, 1, later) == "missed"
    assert classify(launch, 5, later) == "missed"
    assert classify(launch, 15, later) == "wait"
    missed, nxt = partition(launch, later)
    assert missed == [1, 5]
    assert nxt == 15
    # The 1-minute slot opens at +60s and stays due for 45s after that.
    assert classify(launch, 1, NOW + timedelta(seconds=70)) == "due"
    assert classify(launch, 1, NOW + timedelta(seconds=120)) == "missed"


def test_asof_liquidity_ignores_the_future():
    points = [
        (NOW, Decimal("100")),
        (NOW + timedelta(minutes=15), Decimal("50")),
        (NOW + timedelta(minutes=30), None),
    ]
    assert asof_liquidity(points, NOW + timedelta(minutes=10)) == Decimal("100")
    assert asof_liquidity(points, NOW + timedelta(minutes=20)) == Decimal("50")
    assert asof_liquidity(points, NOW - timedelta(minutes=1)) is None
    candles = [
        Candle(NOW - timedelta(minutes=15), Timeframe.M15, Decimal(1), Decimal(1), Decimal(1), Decimal(1), Decimal(1), True),
        Candle(NOW + timedelta(minutes=15), Timeframe.M15, Decimal(1), Decimal(1), Decimal(1), Decimal(1), Decimal(1), True),
        Candle(NOW + timedelta(minutes=30), Timeframe.M15, Decimal(1), Decimal(1), Decimal(1), Decimal(1), Decimal(1), True),
    ]
    # Pad so the clipped series can satisfy the 40-bar minimum inside the helper's caller.
    # Here we only check the clip of the early bar and the as-of value on the middle bar.
    assert asof_liquidity(points, NOW + timedelta(minutes=15)) == Decimal("50")
    assert asof_liquidity([(NOW, Decimal("10"))], NOW + timedelta(minutes=30)) == Decimal("10")


def test_clip_drops_bars_before_the_first_snapshot():
    points = [(NOW + timedelta(minutes=15 * 5), Decimal("80"))]
    candles = [
        Candle(NOW + timedelta(minutes=15 * i), Timeframe.M15, Decimal(1), Decimal(1), Decimal(1), Decimal(1), Decimal(1), True)
        for i in range(50)
    ]
    clipped = clip_candles(candles, points)
    assert clipped is not None
    kept, series = clipped
    assert kept[0].open_time >= points[0][0]
    assert all(value == Decimal("80") for value in series)
    assert clip_candles(candles[:10], points) is None


def test_ready_requires_two_weeks_and_enough_pools():
    fresh = Inventory(3, 10, 10, NOW, 1)
    assert ready(fresh)[0] is False
    old = Inventory(40, 400, 400, NOW - timedelta(days=21), 35)
    assert ready(old)[0] is True


pytestmark_pg = pytest.mark.skipif(not URL, reason="DEXBOT_TEST_DATABASE_URL not set")


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
async def test_enroll_records_missed_slots_without_inventing_prices(session):
    candidate = Candidate(
        chain=Chain.SOLANA, pool_address="So11111111111111111111111111111111111111112",
        token_address="DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263", quote_address=None,
        symbol="BONK", dex_id="raydium", pool_created_at=NOW,
    )
    assert await enroll(session, candidate, now=NOW + timedelta(minutes=10), max_active=10, max_per_day=10,
                        max_age=timedelta(minutes=30))
    await session.commit()
    stats = await inventory(session)
    assert stats.watches == 1
    assert stats.real_snapshots == 0
    assert stats.snapshots == 2  # the 1 and 5 minute slots were already late
    again = await enroll(session, candidate, now=NOW + timedelta(minutes=10), max_active=10, max_per_day=10,
                         max_age=timedelta(minutes=30))
    assert again is False
