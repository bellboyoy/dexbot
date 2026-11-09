"""FastAPI entrypoint. Phase 1 exposes health and read-only market data endpoints."""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

from app.config import get_settings
from app.dashboard import DASHBOARD_HTML
from app.domain.chains import Chain, InvalidAddress
from app.domain.market import Timeframe
from app.logging import configure_logging, get_logger
from app.marketdata.aggregator import MarketDataService, build_market_data_service
from app.marketdata.errors import ProviderUnavailable
from app.paper.repository import build_summary, session_scope
from app.strategy.validation import load_validation

log = get_logger(__name__)


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
    return {
        "status": "ok", "mode": s.trading_mode, "paper_only": True, "execution": "disabled",
        "chains": s.enabled_chains, "providers": _market().health(),
    }


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


async def _paper_ledger() -> dict:
    settings = get_settings()
    try:
        validation = load_validation()
        known = list(validation.passed) + [name for name in validation.rejected if not name.startswith("_")]
        async with session_scope(settings.database_url) as session:
            data = await build_summary(
                session, gas_reserve=settings.risk_gas_reserve_usd, known_strategies=known,
            )
    except Exception as exc:
        log.warning("paper_summary_unavailable", error=type(exc).__name__)
        raise HTTPException(503, "paper ledger database unavailable") from exc
    data["validated_strategies"] = list(validation.passed)
    data["validation_note"] = validation.note
    data["providers"] = _market().health()
    return data


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    return DASHBOARD_HTML


@app.get("/api/paper/summary")
async def paper_summary():
    return await _paper_ledger()


@app.get("/api/paper/positions")
async def paper_positions():
    data = await _paper_ledger()
    return {"mode": "paper", "label": "PAPER", "open_positions": data["open_positions"]}


@app.get("/api/paper/strategies")
async def paper_strategies():
    data = await _paper_ledger()
    return {
        "mode": "paper", "label": "PAPER", "strategies": data["strategies"],
        "validated_strategies": data["validated_strategies"], "validation_note": data["validation_note"],
    }
