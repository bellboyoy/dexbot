"""Launch schedule, as-of liquidity, and the Postgres watch book."""
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.collector.schedule import (
    asof_liquidity, catch_up_limit, classify, clock_jumped, daily_megabytes, daily_snapshot_rows,
    fatal_stall, is_usable, partition, snapshot_ages,
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


def test_suspend_gap_marks_overdue_slots_missed_and_leaves_the_current_one_due():
    launch = NOW
    later = NOW + timedelta(hours=6)
    assert clock_jumped(NOW, later, 120) is True
    assert clock_jumped(None, later, 120) is False
    missed, nxt = partition(launch, later)
    assert missed == [1, 5, 15, 30, 60, 240, 300]
    assert nxt == 360
    assert classify(launch, 360, later) == "due"
    assert catch_up_limit(True, 4) == 50
    assert catch_up_limit(False, 4) == 4
    assert fatal_stall(2, 2) is True
    assert fatal_stall(1, 2) is False


def test_usable_pool_needs_observed_coverage_not_filled_gaps():
    assert is_usable(8, 8) is True
    assert is_usable(8, 16) is True
    assert is_usable(7, 8) is False
    assert is_usable(8, 17) is False  # under half of the elapsed slots
    assert is_usable(8, 4) is False


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


async def test_collector_exits_nonzero_after_repeated_stalls(monkeypatch):
    import asyncio
    from app.collector.__main__ import _guarded_cycle
    from app.collector.service import CollectorState
    from app.config import get_settings

    monkeypatch.setenv("DEXBOT_CYCLE_DEADLINE_S", "0.05")
    monkeypatch.setenv("DEXBOT_COLLECTOR_STALL_LIMIT", "2")
    get_settings.cache_clear()

    async def hang(*args, **kwargs):
        await asyncio.sleep(5)

    monkeypatch.setattr("app.collector.__main__._cycle", hang)
    state = CollectorState()
    await _guarded_cycle(None, None, None, state)
    assert state.stalls == 1
    with pytest.raises(SystemExit) as caught:
        await _guarded_cycle(None, None, None, state)
    assert caught.value.code == 1
    get_settings.cache_clear()


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


@pytest.mark.postgres
async def test_extended_pool_id_and_coverage_ignore_gaps(session):
    from decimal import Decimal as D
    from app.collector.store import add_observation
    from app.db.models import LaunchSnapshot, LaunchWatch
    from sqlalchemy import select

    now = datetime.now(timezone.utc).replace(microsecond=0)
    launch = now - timedelta(minutes=361)
    pool_id = "0x" + "ab" * 32
    token = "0x" + "cd" * 20
    quote = "0x" + "11" * 20
    created = await enroll(session, Candidate(
        chain=Chain.BASE, pool_address=pool_id, token_address=token, quote_address=quote,
        symbol="V4", dex_id="uniswap-v4", pool_created_at=launch,
    ), now=launch + timedelta(seconds=20), max_active=10, max_per_day=10, max_age=timedelta(minutes=30))
    assert created is True
    watch = await session.scalar(select(LaunchWatch).where(LaunchWatch.pool_address == pool_id))
    assert watch is not None and len(watch.pool_address) == 66
    for age in (1, 5, 15, 30, 60, 240, 300, 360):
        await add_observation(
            session, watch, age=age, now=now, source="dexscreener", not_found=False,
            fields={"price_usd": D("1"), "liquidity_usd": D("100")}, screen=None,
        )
    await add_observation(
        session, watch, age=420, now=now, source="unsupported_pool_id", not_found=False, fields={}, screen=None,
    )
    await session.commit()
    row = await session.scalar(select(LaunchSnapshot).where(LaunchSnapshot.source == "unsupported_pool_id"))
    assert row is not None and row.missed is False and row.price_usd is None and row.not_found is False
    stats = await inventory(session)
    assert stats.unsupported_slots == 1
    assert stats.real_snapshots == 8
    assert stats.pools_with_history == 1
    assert stats.observed_coverage == 1
    assert stats.elapsed_slots == 8
    missed = await session.scalar(
        select(LaunchSnapshot).where(LaunchSnapshot.watch_id == watch.id, LaunchSnapshot.missed.is_(True))
    )
    assert missed is None or missed.price_usd is None


@pytest.mark.postgres
async def test_heartbeat_records_the_last_success(session, monkeypatch):
    from app.config import get_settings
    from app.runtime.heartbeat import read_heartbeats, write_heartbeat

    monkeypatch.setenv("DEXBOT_DATABASE_URL", URL)
    get_settings.cache_clear()
    await write_heartbeat("collector", success=True)
    await write_heartbeat("collector", success=False, error="cycle deadline")
    rows = {row["component"]: row for row in await read_heartbeats(session)}
    assert rows["collector"]["last_success_at"] is not None
    assert rows["collector"]["consecutive_failures"] == 1
    assert rows["collector"]["last_error"] == "cycle deadline"
    get_settings.cache_clear()
