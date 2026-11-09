"""FastAPI entrypoint. Phase 1 exposes health and read-only market data endpoints."""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict

from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Query
from sqlalchemy import func, select

from app.config import get_settings
from app.db.models import (
    CandleRow, DataQualityEvent, DiscoveryLead, MarketSnapshot, MomentumEvent, Pool, PoolMetrics, WatchedPool,
)
from app.domain.candles import resample
from app.domain.market import Candle
from app.quality.checks import check_candles
from app.db.session import make_engine, make_sessionmaker
from app.domain.chains import Chain, InvalidAddress
from app.domain.market import Timeframe
from app.logging import configure_logging
from app.marketdata.aggregator import MarketDataService, build_market_data_service
from app.marketdata.errors import ProviderUnavailable


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    app.state.market = build_market_data_service(settings)
    engine = make_engine(settings.database_url)
    app.state.sessionmaker = make_sessionmaker(engine)
    yield
    await app.state.market.aclose()
    await engine.dispose()


app = FastAPI(title="DexBot", version="0.1.0", lifespan=lifespan)


def _market() -> MarketDataService:
    return app.state.market


def _serialize(obj):
    d = asdict(obj)
    d.pop("raw", None)
    return d


@app.get("/health")
async def health():
    s = get_settings()
    return {"status": "ok", "mode": s.trading_mode, "chains": s.enabled_chains, "providers": _market().health()}


@app.get("/market/{chain}/pools/{address}")
async def pool(chain: Chain, address: str, enrich: bool = False):
    try:
        snap = await _market().get_pool(chain, address, enrich=enrich)
    except InvalidAddress as exc:
        raise HTTPException(422, str(exc))
    except ProviderUnavailable as exc:
        raise HTTPException(503, str(exc))
    if snap is None:
        raise HTTPException(404, "pool not found")
    return _serialize(snap)


@app.get("/market/{chain}/pools/{address}/ohlcv")
async def ohlcv(chain: Chain, address: str, timeframe: Timeframe = Timeframe.M5,
                limit: int = Query(100, ge=1, le=1000)):
    try:
        candles = await _market().get_ohlcv(chain, address, timeframe, limit)
    except InvalidAddress as exc:
        raise HTTPException(422, str(exc))
    except ProviderUnavailable as exc:
        raise HTTPException(503, str(exc))
    return [asdict(c) for c in candles]


@app.get("/market/{chain}/new-pools")
async def new_pools(chain: Chain):
    try:
        return [_serialize(s) for s in await _market().get_new_pools(chain)]
    except ProviderUnavailable as exc:
        raise HTTPException(503, str(exc))


@app.get("/collector/status")
async def collector_status():
    """Read-only view of what the collector (separate process) has recorded, per chain."""
    now = datetime.now(timezone.utc)
    try:
        async with app.state.sessionmaker() as s:
            by = (await s.execute(select(WatchedPool.chain, WatchedPool.pipeline, WatchedPool.status, func.count())
                                  .group_by(WatchedPool.chain, WatchedPool.pipeline, WatchedPool.status))).all()
            per_chain = (await s.execute(
                select(MarketSnapshot.chain, MarketSnapshot.source, func.count(), func.max(MarketSnapshot.observed_at))
                .group_by(MarketSnapshot.chain, MarketSnapshot.source))).all()
            stale = (await s.execute(
                select(WatchedPool.chain, func.count()).where(
                    WatchedPool.status == "active",
                    (WatchedPool.last_snapshot_at.is_(None)) | (WatchedPool.last_snapshot_at < now - timedelta(minutes=3)))
                .group_by(WatchedPool.chain))).all()
            candles = (await s.execute(select(CandleRow.chain, CandleRow.timeframe, func.count())
                                       .group_by(CandleRow.chain, CandleRow.timeframe))).all()
            leads = (await s.execute(select(DiscoveryLead.chain, DiscoveryLead.pipeline, DiscoveryLead.passed, func.count())
                                     .group_by(DiscoveryLead.chain, DiscoveryLead.pipeline, DiscoveryLead.passed))).all()
            momentum = (await s.execute(select(MomentumEvent.chain, func.count()).group_by(MomentumEvent.chain))).all()
    except Exception as exc:
        raise HTTPException(503, f"database unavailable: {type(exc).__name__}")
    out: dict = {}

    def chain(c):
        return out.setdefault(c.value, {"watchlist": [], "snapshots": {}, "stale_active_pools": 0, "candles": {},
                                        "leads": [], "momentum_events": 0})
    for c, pipe, st, n in by:
        chain(c)["watchlist"].append({"pipeline": pipe, "status": st, "pools": n})
    for c, src, n, latest in per_chain:
        chain(c)["snapshots"][src] = {"total": n, "latest_at": latest}
    for c, n in stale:
        chain(c)["stale_active_pools"] = n
    for c, tf, n in candles:
        chain(c)["candles"][tf] = n
    for c, pipe, passed, n in leads:
        chain(c)["leads"].append({"pipeline": pipe, "outcome": "passed" if passed else "rejected" if passed is False
                                  else "unresolved", "count": n})
    for c, n in momentum:
        chain(c)["momentum_events"] = n
    return {"chains": out, "as_of": now}


@app.get("/quality")
async def quality(hours: int = Query(24, ge=1, le=24 * 30)):
    """Data-quality events by chain, provider and check over the last `hours`."""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    try:
        async with app.state.sessionmaker() as s:
            rows = (await s.execute(
                select(DataQualityEvent.chain, DataQualityEvent.source, DataQualityEvent.check,
                       DataQualityEvent.severity, func.count(), func.max(DataQualityEvent.observed_at))
                .where(DataQualityEvent.observed_at >= since)
                .group_by(DataQualityEvent.chain, DataQualityEvent.source, DataQualityEvent.check,
                          DataQualityEvent.severity))).all()
    except Exception as exc:
        raise HTTPException(503, f"database unavailable: {type(exc).__name__}")
    return [{"chain": c, "source": src, "check": chk, "severity": sev, "count": n, "latest_at": t}
            for c, src, chk, sev, n, t in rows]


@app.get("/history/{chain}/pools/{address}/candles")
async def stored_candles(chain: Chain, address: str, timeframe: Timeframe = Timeframe.M5,
                         limit: int = Query(300, ge=1, le=5000)):
    """Persisted candles. Timeframes not fetched directly are resampled from stored 1m candles."""
    try:
        from app.domain.chains import normalize_pool_id
        addr = normalize_pool_id(chain, address)
    except InvalidAddress as exc:
        raise HTTPException(422, str(exc))
    async with app.state.sessionmaker() as s:
        pool_id = (await s.execute(select(Pool.id).where(Pool.chain == chain, Pool.address == addr))).scalar_one_or_none()
        if pool_id is None:
            raise HTTPException(404, "pool not recorded")
        async def load(tf: Timeframe, n: int):
            rows = (await s.execute(select(CandleRow).where(CandleRow.pool_id == pool_id, CandleRow.timeframe == tf.value)
                                    .order_by(CandleRow.open_time.desc()).limit(n))).scalars().all()
            return [Candle(r.open_time, tf, r.open, r.high, r.low, r.close, r.volume_usd, True) for r in reversed(rows)]
        candles = await load(timeframe, limit)
        derived = False
        if not candles and timeframe.seconds > 60 and timeframe.seconds % 60 == 0:
            ones = await load(Timeframe.M1, min(limit * timeframe.seconds // 60, 20000))
            candles = resample(ones, timeframe)[-limit:]
            derived = True
    q = check_candles(candles, datetime.now(timezone.utc))
    return {"chain": chain, "pool": addr, "timeframe": timeframe, "source": "geckoterminal",
            "derived_from_1m": derived, "quality": {"flags": q.flags, "details": q.details},
            "candles": [asdict(c) for c in candles]}
