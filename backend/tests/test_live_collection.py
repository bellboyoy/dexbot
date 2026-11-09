"""Opt-in live checks for Phase 2 assumptions: pytest -m live"""
import pytest

from app.config import Settings
from app.discovery.validation import validate_quote_assets
from app.domain.assets import QUOTE_ASSETS
from app.domain.chains import Chain
from app.marketdata.aggregator import build_market_data_service

pytestmark = pytest.mark.live


@pytest.fixture
async def ds():
    svc = build_market_data_service(Settings(_env_file=None, enabled_chains="solana,base,bsc,arbitrum"))
    yield svc.providers[0]
    await svc.aclose()


async def test_all_quote_assets_verify_live(ds):
    chains = [Chain.SOLANA, Chain.BASE, Chain.BSC, Chain.ARBITRUM]
    got = await validate_quote_assets(ds, chains)
    for c in chains:
        assert got[c] == set(QUOTE_ASSETS[c]), c


async def test_multi_pair_batch_of_30(ds):
    pools = [p.pool_address for p in await ds.search("SOL USDC") if p.chain is Chain.SOLANA]
    pools += [p.pool_address for p in await ds.search("BONK SOL") if p.chain is Chain.SOLANA]
    pools = list(dict.fromkeys(pools))[:30]
    got = await ds.get_pools(Chain.SOLANA, pools)
    assert len(pools) >= 10 and len(got) == len(pools)
