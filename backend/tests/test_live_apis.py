"""Opt-in smoke tests against the real APIs: pytest -m live"""
import pytest

from app.config import Settings
from app.domain.chains import Chain
from app.domain.market import Timeframe
from app.marketdata.aggregator import build_market_data_service

pytestmark = pytest.mark.live
WSOL = "So11111111111111111111111111111111111111112"


@pytest.fixture
async def svc():
    s = build_market_data_service(Settings(_env_file=None))
    yield s
    await s.aclose()


async def test_live_end_to_end(svc):
    pools = await svc.get_token_pools(Chain.SOLANA, WSOL)
    assert pools and pools[0].liquidity_usd and pools[0].price_usd
    top = max(pools, key=lambda p: p.liquidity_usd or 0)
    enriched = await svc.get_pool(Chain.SOLANA, top.pool_address, enrich=True)
    assert enriched.window("h1").buyers is not None      # filled from GeckoTerminal
    candles = await svc.get_ohlcv(Chain.SOLANA, top.pool_address, Timeframe.M5, limit=20)
    assert candles and all(c.is_closed for c in candles)
    leads = await svc.discover([Chain.SOLANA, Chain.BASE, Chain.BSC])
    assert leads
