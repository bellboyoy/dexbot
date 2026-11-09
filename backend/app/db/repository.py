"""Persistence operations for market data, discovery and the watchlist.

All writes are idempotent where the data has a natural key (tokens, pools, candles,
watchlist entries), so a crash and re-run never duplicates rows.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Iterable

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    CandleRow, CollectorState, DataQualityEvent, DiscoveryLead, MarketSnapshot, MomentumEvent, Pool,
    PoolMetrics, SystemLog, Token, WatchedPool,
)
from app.domain.chains import Chain
from app.domain.market import Candle, PoolSnapshot, TokenRef


def _jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return v


def windows_json(snap: PoolSnapshot) -> dict[str, Any]:
    return {k: _jsonable(asdict(w)) for k, w in snap.windows.items()}


async def upsert_token(session: AsyncSession, ref: TokenRef) -> int:
    stmt = insert(Token).values(chain=ref.chain, address=ref.address, symbol=ref.symbol, name=ref.name)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Token.chain, Token.address],
        set_={"symbol": func.coalesce(stmt.excluded.symbol, Token.symbol),
              "name": func.coalesce(stmt.excluded.name, Token.name), "updated_at": func.now()},
    ).returning(Token.id)
    return (await session.execute(stmt)).scalar_one()


async def upsert_pool(session: AsyncSession, snap: PoolSnapshot) -> int:
    base_id = await upsert_token(session, snap.base_token)
    quote_id = await upsert_token(session, snap.quote_token) if snap.quote_token else None
    stmt = insert(Pool).values(chain=snap.chain, address=snap.pool_address, dex_id=snap.dex_id,
                               base_token_id=base_id, quote_token_id=quote_id,
                               pool_created_at=snap.pool_created_at)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Pool.chain, Pool.address],
        set_={"dex_id": func.coalesce(stmt.excluded.dex_id, Pool.dex_id),
              "pool_created_at": func.coalesce(Pool.pool_created_at, stmt.excluded.pool_created_at),
              "updated_at": func.now()},
    ).returning(Pool.id)
    return (await session.execute(stmt)).scalar_one()


async def insert_snapshot(session: AsyncSession, pool_id: int, snap: PoolSnapshot, *,
                          quality_flags: list[str] | None = None, keep_raw: bool = False) -> None:
    """Every snapshot keeps its provider (`source`) and real fetch time (`observed_at`)."""
    session.add(MarketSnapshot(
        pool_id=pool_id, chain=snap.chain, source=snap.source.value, observed_at=snap.observed_at,
        price_usd=snap.price_usd, liquidity_usd=snap.liquidity_usd, fdv_usd=snap.fdv_usd,
        market_cap_usd=snap.market_cap_usd, windows=windows_json(snap), quality_flags=quality_flags or None,
        raw=_jsonable(snap.raw) if keep_raw else None,
    ))


async def upsert_candles(session: AsyncSession, pool_id: int, chain: Chain, source: str,
                         candles: Iterable[Candle]) -> int:
    rows = [dict(pool_id=pool_id, chain=chain, source=source, timeframe=c.timeframe.value,
                 open_time=c.open_time, open=c.open, high=c.high, low=c.low, close=c.close,
                 volume_usd=c.volume_usd) for c in candles if c.is_closed]
    if not rows:
        return 0
    inserted = 0
    for i in range(0, len(rows), 500):
        stmt = insert(CandleRow).values(rows[i:i + 500]).on_conflict_do_nothing(
            index_elements=[CandleRow.pool_id, CandleRow.source, CandleRow.timeframe, CandleRow.open_time]
        ).returning(CandleRow.id)
        inserted += len((await session.execute(stmt)).all())
    return inserted


async def record_lead(session: AsyncSession, **fields: Any) -> None:
    for k in ("rejections", "metrics"):
        if fields.get(k) is not None:
            fields[k] = _jsonable(fields[k])
    session.add(DiscoveryLead(**fields))


async def watch_pool(session: AsyncSession, *, pool_id: int, chain: Chain, base_token_address: str,
                     profile: str, discovered_via: str, now: datetime, pipeline: str = "new") -> bool:
    """Add a pool to the watchlist. Returns False if it was already there."""
    stmt = insert(WatchedPool).values(
        pool_id=pool_id, chain=chain, base_token_address=base_token_address, profile=profile,
        pipeline=pipeline, status="active", discovered_via=discovered_via, added_at=now, status_since=now,
        consecutive_misses=0, next_ohlcv_at=now, ohlcv_backfilled=False,
    ).on_conflict_do_nothing(index_elements=[WatchedPool.pool_id]).returning(WatchedPool.id)
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def watched_pool_addresses(session: AsyncSession, chain: Chain) -> set[str]:
    q = select(Pool.address).join(WatchedPool, WatchedPool.pool_id == Pool.id).where(WatchedPool.chain == chain)
    return set((await session.execute(q)).scalars())


async def count_watching(session: AsyncSession, statuses: tuple[str, ...] = ("active", "inactive", "dead")) -> int:
    q = select(func.count()).select_from(WatchedPool).where(WatchedPool.status.in_(statuses))
    return (await session.execute(q)).scalar_one()


async def due_for_snapshot(session: AsyncSession, now: datetime, intervals: dict[str, timedelta]
                           ) -> list[tuple[WatchedPool, str]]:
    """Watched pools whose status-specific snapshot interval has elapsed, with pool address."""
    q = (select(WatchedPool, Pool.address).join(Pool, Pool.id == WatchedPool.pool_id)
         .where(WatchedPool.status.in_(list(intervals))))
    out = []
    for wp, addr in (await session.execute(q)).all():
        last = wp.last_snapshot_at
        if last is None or now - last >= intervals[wp.status]:
            out.append((wp, addr))
    return out


async def due_for_ohlcv(session: AsyncSession, now: datetime, limit: int) -> list[tuple[WatchedPool, str]]:
    q = (select(WatchedPool, Pool.address).join(Pool, Pool.id == WatchedPool.pool_id)
         .where(WatchedPool.status.in_(["active", "inactive", "dead"]), WatchedPool.next_ohlcv_at <= now)
         .order_by(WatchedPool.ohlcv_backfilled, WatchedPool.next_ohlcv_at).limit(limit))
    return list((await session.execute(q)).all())


async def update_watch(session: AsyncSession, watch_id: int, **values: Any) -> None:
    await session.execute(update(WatchedPool).where(WatchedPool.id == watch_id).values(**values))


async def log_event(session: AsyncSession, level: str, component: str, event: str,
                    chain: Chain | None = None, **context: Any) -> None:
    session.add(SystemLog(level=level, component=component, event=event, chain=chain,
                          context=_jsonable(context) or None))


async def insert_metrics(session: AsyncSession, pool_id: int, row: dict) -> int:
    obj = PoolMetrics(pool_id=pool_id, **row)
    session.add(obj)
    await session.flush()
    return obj.id


async def record_quality(session: AsyncSession, *, chain: Chain, pool_id: int | None, source: str,
                         observed_at: datetime, flags: list[str], details: dict[str, Any]) -> None:
    from app.quality.checks import severity
    for f in flags:
        session.add(DataQualityEvent(chain=chain, pool_id=pool_id, source=source, check=f, severity=severity(f),
                                     observed_at=observed_at, details=_jsonable(details.get(f)) or None))


async def record_momentum(session: AsyncSession, *, chain: Chain, pool_id: int, profile: str, detected_at: datetime,
                          metrics_id: int | None, triggers: list[str], details: dict[str, Any]) -> None:
    session.add(MomentumEvent(chain=chain, pool_id=pool_id, profile=profile, detected_at=detected_at,
                              metrics_id=metrics_id, triggers=triggers, details=_jsonable(details)))


async def get_state(session: AsyncSession, key: str) -> dict[str, Any] | None:
    row = await session.get(CollectorState, key)
    return None if row is None else row.value


async def set_state(session: AsyncSession, key: str, value: dict[str, Any]) -> None:
    stmt = insert(CollectorState).values(key=key, value=_jsonable(value))
    stmt = stmt.on_conflict_do_update(index_elements=[CollectorState.key],
                                      set_={"value": stmt.excluded.value, "updated_at": func.now()})
    await session.execute(stmt)


async def recent_screened(session: AsyncSession, pipeline: str, since: datetime) -> dict[tuple[Chain, str], datetime]:
    q = (select(DiscoveryLead.chain, DiscoveryLead.token_address, func.max(DiscoveryLead.discovered_at))
         .where(DiscoveryLead.pipeline == pipeline, DiscoveryLead.discovered_at >= since)
         .group_by(DiscoveryLead.chain, DiscoveryLead.token_address))
    return {(c, t): at for c, t, at in (await session.execute(q)).all()}


async def recent_liquidity(session: AsyncSession, since: datetime) -> list[tuple[Chain, str, datetime, Decimal]]:
    """Trustworthy DexScreener liquidity observations of watched pools since `since` (for restart priming)."""
    q = (select(MarketSnapshot.chain, Pool.address, MarketSnapshot.observed_at, MarketSnapshot.liquidity_usd)
         .join(Pool, Pool.id == MarketSnapshot.pool_id)
         .join(WatchedPool, WatchedPool.pool_id == Pool.id)
         .where(MarketSnapshot.observed_at >= since, MarketSnapshot.source == "dexscreener",
                MarketSnapshot.liquidity_usd.is_not(None), MarketSnapshot.quality_flags.is_(None))
         .order_by(MarketSnapshot.observed_at))
    return list((await session.execute(q)).all())


async def recent_momentum(session: AsyncSession, since: datetime) -> list[tuple[Chain, str, datetime]]:
    q = (select(MomentumEvent.chain, Pool.address, func.max(MomentumEvent.detected_at))
         .join(Pool, Pool.id == MomentumEvent.pool_id).where(MomentumEvent.detected_at >= since)
         .group_by(MomentumEvent.chain, Pool.address))
    return list((await session.execute(q)).all())


async def buyers_due(session: AsyncSession, chain: Chain, limit: int) -> list[tuple[WatchedPool, str]]:
    q = (select(WatchedPool, Pool.address).join(Pool, Pool.id == WatchedPool.pool_id)
         .where(WatchedPool.chain == chain, WatchedPool.status == "active")
         .order_by(WatchedPool.buyers_checked_at.asc().nulls_first()).limit(limit))
    return list((await session.execute(q)).all())
