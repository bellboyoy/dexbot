"""FastAPI entrypoint. Phase 1 exposes health and read-only market data endpoints."""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict

from fastapi import FastAPI, HTTPException, Query

from app.config import get_settings
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
    yield
    await app.state.market.aclose()


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
