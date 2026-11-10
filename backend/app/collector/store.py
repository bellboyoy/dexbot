"""Postgres access for launch watches. Rows are written one pool at a time."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.collector.schedule import due_at, is_usable, partition, snapshot_ages
from app.db.models import LaunchSnapshot, LaunchWatch
from app.domain.chains import Chain


@dataclass(frozen=True, slots=True)
class Candidate:
    chain: Chain
    pool_address: str
    token_address: str
    quote_address: str | None
    symbol: str | None
    dex_id: str | None
    pool_created_at: datetime | None


async def active_count(session: AsyncSession) -> int:
    value = await session.scalar(select(func.count()).select_from(LaunchWatch).where(LaunchWatch.status == "watching"))
    return int(value or 0)


async def enrolled_since(session: AsyncSession, start: datetime) -> int:
    value = await session.scalar(
        select(func.count()).select_from(LaunchWatch).where(LaunchWatch.first_seen_at >= start)
    )
    return int(value or 0)


async def enroll(session: AsyncSession, candidate: Candidate, *, now: datetime, max_active: int,
                 max_per_day: int, max_age: timedelta) -> bool:
    """Start following a pool. Too-old launches and pools over the caps are ignored."""
    existing = await session.scalar(
        select(LaunchWatch.id).where(LaunchWatch.chain == candidate.chain, LaunchWatch.pool_address == candidate.pool_address)
    )
    if existing is not None:
        return False
    launch = candidate.pool_created_at or now
    if now - launch > max_age:
        return False
    if await active_count(session) >= max_active:
        return False
    day_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    if await enrolled_since(session, day_start) >= max_per_day:
        return False
    missed, nxt = partition(launch, now)
    if nxt is None:
        return False
    watch = LaunchWatch(
        chain=candidate.chain, pool_address=candidate.pool_address, token_address=candidate.token_address,
        quote_address=candidate.quote_address, symbol=candidate.symbol, dex_id=candidate.dex_id,
        pool_created_at=candidate.pool_created_at, first_seen_at=now, launch_at=launch, status="watching",
        next_due_at=due_at(launch, nxt), snapshots_taken=0, last_error=None,
    )
    session.add(watch)
    await session.flush()
    for age in missed:
        session.add(_missed(watch, age))
    await session.flush()
    return True


def _missed(watch: LaunchWatch, age: int) -> LaunchSnapshot:
    return LaunchSnapshot(
        watch_id=watch.id, chain=watch.chain, age_minutes=age, due_at=due_at(watch.launch_at, age),
        observed_at=None, missed=True, not_found=False, source=None,
    )


async def due_watches(session: AsyncSession, now: datetime, limit: int) -> list[LaunchWatch]:
    rows = await session.scalars(
        select(LaunchWatch)
        .where(LaunchWatch.status == "watching", LaunchWatch.next_due_at.is_not(None), LaunchWatch.next_due_at <= now)
        .order_by(LaunchWatch.next_due_at)
        .limit(limit)
    )
    return list(rows)


async def taken_ages(session: AsyncSession, watch_id: int) -> set[int]:
    rows = await session.scalars(select(LaunchSnapshot.age_minutes).where(LaunchSnapshot.watch_id == watch_id))
    return set(rows)


async def add_missed(session: AsyncSession, watch: LaunchWatch, age: int) -> None:
    session.add(_missed(watch, age))


async def add_observation(session: AsyncSession, watch: LaunchWatch, *, age: int, now: datetime, source: str | None,
                          not_found: bool, fields: dict, screen: dict | None) -> None:
    session.add(LaunchSnapshot(
        watch_id=watch.id, chain=watch.chain, age_minutes=age, due_at=due_at(watch.launch_at, age),
        observed_at=now, missed=False, not_found=not_found, source=source,
        price_usd=fields.get("price_usd"), liquidity_usd=fields.get("liquidity_usd"),
        fdv_usd=fields.get("fdv_usd"), market_cap_usd=fields.get("market_cap_usd"),
        base_reserve=fields.get("base_reserve"), quote_reserve=fields.get("quote_reserve"),
        volume_m5=fields.get("volume_m5"), volume_h1=fields.get("volume_h1"), volume_h24=fields.get("volume_h24"),
        buys_m5=fields.get("buys_m5"), sells_m5=fields.get("sells_m5"),
        buys_h1=fields.get("buys_h1"), sells_h1=fields.get("sells_h1"),
        buyers_m5=fields.get("buyers_m5"), sellers_m5=fields.get("sellers_m5"),
        buyers_h1=fields.get("buyers_h1"), sellers_h1=fields.get("sellers_h1"),
        screen_allowed=None if screen is None else screen.get("allowed"),
        screen_reason=None if screen is None else screen.get("reason"),
        screen_checks=None if screen is None else {"checks": screen.get("checks") or []},
    ))
    if not not_found:
        watch.snapshots_taken += 1


async def advance(session: AsyncSession, watch: LaunchWatch, now: datetime) -> None:
    await session.flush()
    done = await taken_ages(session, watch.id)
    missed, nxt = partition(watch.launch_at, now, done)
    for age in missed:
        if age not in done:
            await add_missed(session, watch, age)
            done.add(age)
    if nxt is None:
        watch.status = "completed"
        watch.next_due_at = None
    else:
        watch.next_due_at = due_at(watch.launch_at, nxt)


@dataclass(frozen=True, slots=True)
class Inventory:
    watches: int
    snapshots: int
    real_snapshots: int
    oldest: datetime | None
    pools_with_history: int
    observed_coverage: float | None = None
    observed_slots: int = 0
    elapsed_slots: int = 0
    unsupported_slots: int = 0

    @property
    def days(self) -> float:
        if self.oldest is None:
            return 0.0
        return (datetime.now(timezone.utc) - self.oldest).total_seconds() / 86400


def _market_observation():
    """A real look at the pool: a price, or a confirmed not-found. Misses and unsupported ids are not this."""
    return or_(LaunchSnapshot.price_usd.is_not(None), LaunchSnapshot.not_found.is_(True))


async def inventory(session: AsyncSession, min_real: int = 8) -> Inventory:
    watches = int(await session.scalar(select(func.count()).select_from(LaunchWatch)) or 0)
    snapshots = int(await session.scalar(select(func.count()).select_from(LaunchSnapshot)) or 0)
    real = int(await session.scalar(
        select(func.count()).select_from(LaunchSnapshot).where(
            LaunchSnapshot.missed.is_(False), _market_observation(),
        )
    ) or 0)
    unsupported = int(await session.scalar(
        select(func.count()).select_from(LaunchSnapshot).where(LaunchSnapshot.source == "unsupported_pool_id")
    ) or 0)
    oldest = await session.scalar(select(func.min(LaunchWatch.first_seen_at)))
    rows = (await session.execute(select(LaunchWatch.id, LaunchWatch.launch_at))).all()
    counts = dict((await session.execute(
        select(LaunchSnapshot.watch_id, func.count())
        .where(LaunchSnapshot.missed.is_(False), _market_observation())
        .group_by(LaunchSnapshot.watch_id)
    )).all())
    now = datetime.now(timezone.utc)
    ages = snapshot_ages()
    usable = 0
    observed_total = 0
    elapsed_total = 0
    for watch_id, launch in rows:
        elapsed = sum(1 for age in ages if due_at(launch, age) <= now)
        observed = int(counts.get(watch_id, 0))
        observed_total += observed
        elapsed_total += elapsed
        if is_usable(observed, elapsed, min_observed=min_real):
            usable += 1
    coverage = (observed_total / elapsed_total) if elapsed_total else None
    return Inventory(watches, snapshots, real, oldest, usable, coverage, observed_total, elapsed_total, unsupported)


def ready(stats: Inventory, *, min_days: float = 14, min_pools: int = 30) -> tuple[bool, str]:
    if stats.days < min_days:
        return False, f"only {stats.days:.1f} days of snapshots; need {min_days:.0f}"
    if stats.pools_with_history < min_pools:
        return False, f"{stats.pools_with_history} pools have enough observed coverage; need {min_pools}"
    return True, "enough point-in-time history to rerun validation"
