"""Provider-independent market data interface.

Providers declare their capabilities; callers (the aggregator) route each request to
a provider that supports it and fall back on failure. Swapping or adding a data
source means implementing this protocol, nothing else.
"""
from __future__ import annotations

from enum import StrEnum
from typing import Protocol, runtime_checkable

from app.domain.chains import Chain
from app.domain.market import Candle, DiscoveredToken, PoolSnapshot, PoolTrade, Timeframe
from app.marketdata.http import ProviderStatus


class Capability(StrEnum):
    POOL_SNAPSHOT = "pool_snapshot"
    TOKEN_POOLS = "token_pools"
    SEARCH = "search"
    NEW_POOLS = "new_pools"
    TRENDING_POOLS = "trending_pools"
    TOKEN_PROFILES = "token_profiles"
    OHLCV = "ohlcv"
    TRADES = "trades"
    UNIQUE_TRADERS = "unique_traders"


@runtime_checkable
class MarketDataProvider(Protocol):
    name: str
    capabilities: frozenset[Capability]

    async def get_pool(self, chain: Chain, pool_address: str) -> PoolSnapshot | None: ...
    async def get_token_pools(self, chain: Chain, token_address: str) -> list[PoolSnapshot]: ...
    async def get_new_pools(self, chain: Chain) -> list[PoolSnapshot]: ...
    async def get_trending_pools(self, chain: Chain) -> list[PoolSnapshot]: ...
    async def discover(self, chains: list[Chain]) -> list[DiscoveredToken]: ...
    async def get_ohlcv(self, chain: Chain, pool_address: str, timeframe: Timeframe,
                        limit: int = 100, before_ts: int | None = None) -> list[Candle]: ...
    async def get_trades(self, chain: Chain, pool_address: str) -> list[PoolTrade]: ...
    def status(self) -> ProviderStatus: ...
    async def aclose(self) -> None: ...
