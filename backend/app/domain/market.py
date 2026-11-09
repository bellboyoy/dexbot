"""Provider-independent market data models. Pure data, no I/O.

Missing data is represented as None, never as zero: a pool with unknown liquidity
must not look like a pool with zero (or plenty of) liquidity.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any

from app.domain.chains import Chain

WINDOWS = ("m5", "m15", "m30", "h1", "h6", "h24")


class DataSource(StrEnum):
    DEXSCREENER = "dexscreener"
    GECKOTERMINAL = "geckoterminal"
    RPC = "rpc"


@dataclass(frozen=True, slots=True)
class TokenRef:
    chain: Chain
    address: str
    symbol: str | None = None
    name: str | None = None


@dataclass(frozen=True, slots=True)
class WindowStats:
    buys: int | None = None
    sells: int | None = None
    buyers: int | None = None
    sellers: int | None = None
    volume_usd: Decimal | None = None
    price_change_pct: Decimal | None = None

    @property
    def buy_sell_ratio(self) -> Decimal | None:
        if self.buys is None or self.sells is None:
            return None
        total = self.buys + self.sells
        return None if total == 0 else Decimal(self.buys) / Decimal(total)


@dataclass(frozen=True, slots=True)
class PoolSnapshot:
    source: DataSource
    chain: Chain
    pool_address: str
    dex_id: str | None
    base_token: TokenRef
    quote_token: TokenRef | None
    observed_at: datetime
    price_usd: Decimal | None
    price_native: Decimal | None
    liquidity_usd: Decimal | None
    fdv_usd: Decimal | None
    market_cap_usd: Decimal | None
    pool_created_at: datetime | None
    windows: dict[str, WindowStats] = field(default_factory=dict)
    locked_liquidity_pct: Decimal | None = None
    pool_fee_pct: Decimal | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def window(self, key: str) -> WindowStats:
        return self.windows.get(key, WindowStats())

    def pool_age(self, now: datetime | None = None) -> timedelta | None:
        if self.pool_created_at is None:
            return None
        return (now or datetime.now(timezone.utc)) - self.pool_created_at


class Timeframe(StrEnum):
    S1 = "1s"
    S15 = "15s"
    S30 = "30s"
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    H1 = "1h"
    H4 = "4h"
    H12 = "12h"
    D1 = "1d"

    @property
    def seconds(self) -> int:
        unit = self.value[-1]
        n = int(self.value[:-1])
        return n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


@dataclass(frozen=True, slots=True)
class Candle:
    open_time: datetime
    timeframe: Timeframe
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume_usd: Decimal
    is_closed: bool

    @property
    def close_time(self) -> datetime:
        return self.open_time + timedelta(seconds=self.timeframe.seconds)


class TradeKind(StrEnum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True, slots=True)
class PoolTrade:
    chain: Chain
    pool_address: str
    tx_hash: str
    block_time: datetime
    kind: TradeKind
    trader: str | None
    volume_usd: Decimal | None
    block_number: int | None = None


@dataclass(frozen=True, slots=True)
class DiscoveredToken:
    """A discovery lead. It still needs pool resolution, screening and security checks."""
    source: DataSource
    chain: Chain
    token_address: str
    pool_address: str | None
    discovered_at: datetime
    reason: str
