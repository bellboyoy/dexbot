"""Derived market metrics: volume acceleration, buy/sell pressure, unique buyers and
liquidity change. Inputs that are missing produce None, never zero. Values that need
history (liquidity change) are None until enough of our own snapshots exist.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from app.domain.chains import Chain
from app.domain.market import PoolSnapshot
from app.quality.checks import QualityReport

PoolKey = tuple[Chain, str]


@dataclass(frozen=True)
class MetricsConfig:
    history: timedelta = timedelta(hours=2)
    buyers_max_age: timedelta = timedelta(minutes=10)      # older buyer counts are not used
    # Windows (target lag, tolerance) for liquidity change from our own snapshot history.
    lag_5m: tuple[timedelta, timedelta] = (timedelta(minutes=5), timedelta(minutes=2))
    lag_1h: tuple[timedelta, timedelta] = (timedelta(hours=1), timedelta(minutes=10))


@dataclass
class Metrics:
    chain: Chain
    pool_address: str
    observed_at: datetime
    source: str
    price_usd: Decimal | None
    liquidity_usd: Decimal | None
    volume_m5_usd: Decimal | None
    volume_h1_usd: Decimal | None
    volume_accel_m5: Decimal | None
    volume_accel_h1: Decimal | None
    buy_ratio_m5: Decimal | None
    buy_ratio_h1: Decimal | None
    txns_m5: int | None
    buyers_h1: int | None
    sellers_h1: int | None
    buyers_observed_at: datetime | None
    liquidity_change_5m_pct: Decimal | None
    liquidity_change_1h_pct: Decimal | None
    price_change_m5_pct: Decimal | None
    price_change_h1_pct: Decimal | None
    reliable: bool
    quality_flags: list[str] = field(default_factory=list)

    def row(self) -> dict:
        d = {k: getattr(self, k) for k in (
            "chain", "observed_at", "source", "price_usd", "liquidity_usd", "volume_m5_usd", "volume_h1_usd",
            "volume_accel_m5", "volume_accel_h1", "buy_ratio_m5", "buy_ratio_h1", "buyers_h1", "sellers_h1",
            "buyers_observed_at", "liquidity_change_5m_pct", "liquidity_change_1h_pct", "price_change_m5_pct",
            "price_change_h1_pct", "reliable")}
        d["quality_flags"] = self.quality_flags or None
        return d


def _ratio(num: Decimal | None, den: Decimal | None) -> Decimal | None:
    if num is None or den is None or den <= 0:
        return None
    return (num / den).quantize(Decimal("0.000001"))


class MetricsEngine:
    def __init__(self, cfg: MetricsConfig | None = None):
        self.cfg = cfg or MetricsConfig()
        self._liq: dict[PoolKey, deque[tuple[datetime, Decimal]]] = {}
        self._last: dict[PoolKey, PoolSnapshot] = {}
        self._buyers: dict[PoolKey, tuple[datetime, int | None, int | None]] = {}

    # --- state -----------------------------------------------------------------
    def prime_liquidity(self, key: PoolKey, observed_at: datetime, liquidity: Decimal) -> None:
        self._liq.setdefault(key, deque()).append((observed_at, liquidity))

    def last(self, key: PoolKey) -> PoolSnapshot | None:
        return self._last.get(key)

    def observe_buyers(self, snap: PoolSnapshot) -> None:
        w = snap.window("h1")
        if w.buyers is not None or w.sellers is not None:
            self._buyers[(snap.chain, snap.pool_address)] = (snap.observed_at, w.buyers, w.sellers)

    def prime_buyers(self, key: PoolKey, observed_at: datetime, buyers: int | None, sellers: int | None) -> None:
        self._buyers[key] = (observed_at, buyers, sellers)

    def _lookup(self, key: PoolKey, at: datetime, lag: tuple[timedelta, timedelta]) -> Decimal | None:
        target, tol = at - lag[0], lag[1]
        best = None
        for t, v in self._liq.get(key, ()):
            if abs(t - target) <= tol and (best is None or abs(t - target) < abs(best[0] - target)):
                best = (t, v)
        return None if best is None else best[1]

    # --- compute ---------------------------------------------------------------
    def compute(self, snap: PoolSnapshot, quality: QualityReport) -> Metrics:
        key = (snap.chain, snap.pool_address)
        at = snap.observed_at
        m5, h1, h24 = snap.window("m5"), snap.window("h1"), snap.window("h24")
        txns_m5 = None if m5.buys is None or m5.sells is None else m5.buys + m5.sells
        txns_h1 = None if h1.buys is None or h1.sells is None else h1.buys + h1.sells

        buyers = sellers = buyers_at = None
        b = self._buyers.get(key)
        if b is not None and abs(at - b[0]) <= self.cfg.buyers_max_age:
            buyers_at, buyers, sellers = b

        def change(lag):
            prev = self._lookup(key, at, lag)
            if prev is None or snap.liquidity_usd is None or prev <= 0:
                return None
            return ((snap.liquidity_usd - prev) / prev * 100).quantize(Decimal("0.000001"))

        metrics = Metrics(
            chain=snap.chain, pool_address=snap.pool_address, observed_at=at, source=snap.source.value,
            price_usd=snap.price_usd, liquidity_usd=snap.liquidity_usd,
            volume_m5_usd=m5.volume_usd, volume_h1_usd=h1.volume_usd,
            volume_accel_m5=_ratio(m5.volume_usd, None if h1.volume_usd is None else h1.volume_usd / 12),
            volume_accel_h1=_ratio(h1.volume_usd, None if h24.volume_usd is None else h24.volume_usd / 24),
            buy_ratio_m5=None if not txns_m5 else _ratio(Decimal(m5.buys), Decimal(txns_m5)),
            buy_ratio_h1=None if not txns_h1 else _ratio(Decimal(h1.buys), Decimal(txns_h1)),
            txns_m5=txns_m5, buyers_h1=buyers, sellers_h1=sellers, buyers_observed_at=buyers_at,
            liquidity_change_5m_pct=change(self.cfg.lag_5m), liquidity_change_1h_pct=change(self.cfg.lag_1h),
            price_change_m5_pct=m5.price_change_pct, price_change_h1_pct=h1.price_change_pct,
            reliable=quality.reliable, quality_flags=list(quality.flags),
        )
        # Record this observation for future lookups (only trustworthy values).
        if quality.reliable and snap.liquidity_usd is not None:
            dq = self._liq.setdefault(key, deque())
            dq.append((at, snap.liquidity_usd))
            while dq and at - dq[0][0] > self.cfg.history:
                dq.popleft()
        self._last[key] = snap
        return metrics

    def forget(self, key: PoolKey) -> None:
        for d in (self._liq, self._last, self._buyers):
            d.pop(key, None)
