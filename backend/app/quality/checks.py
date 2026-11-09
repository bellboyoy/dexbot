"""Market-data quality checks.

Each check returns flags (short strings) plus details. Flags are stored on the snapshot,
copied onto derived metrics (which are then `reliable=False`), and written as
`data_quality_events` so problems are visible per chain, provider and pool.

Principles enforced here:
  * missing is flagged, never treated as zero
  * stale data (old fetch, or a provider that stopped updating) is flagged, never passed off as current
  * disagreement between providers is flagged rather than averaged away
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from app.domain.market import Candle, PoolSnapshot

# Flags that make an observation unusable for decisions.
BLOCKING = frozenset({"stale", "frozen", "missing_price", "missing_liquidity", "invalid_values",
                      "missing_activity"})


@dataclass(frozen=True)
class QualityConfig:
    max_age: timedelta = timedelta(seconds=90)              # fetch older than this = stale
    frozen_after: timedelta = timedelta(minutes=3)          # identical activity counters for this long
    liquidity_jump_pct: Decimal = Decimal(50)               # within ~5 min
    divergence_price_pct: Decimal = Decimal(5)
    divergence_liquidity_pct: Decimal = Decimal(30)


@dataclass
class QualityReport:
    flags: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def add(self, flag: str, **details: Any) -> None:
        if flag not in self.flags:
            self.flags.append(flag)
        if details:
            self.details[flag] = details

    @property
    def reliable(self) -> bool:
        return not (set(self.flags) & BLOCKING)


def severity(flag: str) -> str:
    return "error" if flag in BLOCKING else "warn"


def _activity_key(s: PoolSnapshot) -> tuple:
    return tuple((k, w.buys, w.sells, w.volume_usd) for k, w in sorted(s.windows.items()))


def check_snapshot(snap: PoolSnapshot, now: datetime, previous: PoolSnapshot | None = None,
                   cfg: QualityConfig | None = None) -> QualityReport:
    cfg = cfg or QualityConfig()
    r = QualityReport()
    age = now - snap.observed_at
    if age > cfg.max_age:
        r.add("stale", age_s=round(age.total_seconds()))
    if snap.price_usd is None:
        r.add("missing_price")
    if snap.liquidity_usd is None:
        r.add("missing_liquidity")
    m5, h1 = snap.window("m5"), snap.window("h1")
    if None in (h1.buys, h1.sells, h1.volume_usd):
        r.add("missing_activity", window="h1")
    if None in (m5.buys, m5.sells, m5.volume_usd):
        r.add("missing_m5")
    for name in ("price_usd", "liquidity_usd", "fdv_usd", "market_cap_usd"):
        v = getattr(snap, name)
        if v is not None and v < 0:
            r.add("invalid_values", field=name)
    for k, w in snap.windows.items():
        if any(x is not None and x < 0 for x in (w.buys, w.sells, w.buyers, w.sellers, w.volume_usd)):
            r.add("invalid_values", window=k)

    if previous is not None and previous.source == snap.source and previous.pool_address == snap.pool_address:
        gap = snap.observed_at - previous.observed_at
        busy = (h1.buys or 0) + (h1.sells or 0) > 0
        if gap >= cfg.frozen_after and busy and _activity_key(previous) == _activity_key(snap):
            # Rolling-window counters of an active pool cannot stay identical for minutes:
            # the provider has stopped updating this pool.
            r.add("frozen", unchanged_for_s=round(gap.total_seconds()))
        if (snap.liquidity_usd is not None and previous.liquidity_usd and previous.liquidity_usd > 0
                and gap <= timedelta(minutes=6)):
            change = (snap.liquidity_usd - previous.liquidity_usd) / previous.liquidity_usd * 100
            if abs(change) >= cfg.liquidity_jump_pct:
                r.add("liquidity_jump", change_pct=str(round(change, 2)), over_s=round(gap.total_seconds()))
    return r


def check_divergence(a: PoolSnapshot, b: PoolSnapshot, cfg: QualityConfig | None = None) -> QualityReport:
    """Compare two providers' views of the same pool."""
    cfg = cfg or QualityConfig()
    r = QualityReport()
    for name, limit, flag in (("price_usd", cfg.divergence_price_pct, "source_divergence_price"),
                              ("liquidity_usd", cfg.divergence_liquidity_pct, "source_divergence_liquidity")):
        x, y = getattr(a, name), getattr(b, name)
        if x is None or y is None or min(x, y) <= 0:
            continue
        pct = abs(x - y) / min(x, y) * 100
        if pct > limit:
            r.add(flag, pct=str(round(pct, 2)), sources=[a.source.value, b.source.value])
    return r


def check_candles(candles: list[Candle], now: datetime) -> QualityReport:
    """Gaps and invariant violations in a candle series (oldest first)."""
    r = QualityReport()
    if not candles:
        r.add("no_candles")
        return r
    step = timedelta(seconds=candles[0].timeframe.seconds)
    gaps = sum(int((b.open_time - a.open_time) / step) - 1 for a, b in zip(candles, candles[1:])
               if b.open_time - a.open_time > step)
    if gaps:
        # Providers omit intervals with no trades; a gap means "no trades or no data", never zero volume.
        r.add("candle_gaps", missing=gaps, of=gaps + len(candles))
    if any(not (c.low <= min(c.open, c.close) and c.high >= max(c.open, c.close)) for c in candles):
        r.add("invalid_values", kind="ohlc_invariant")
    latest_close = candles[-1].close_time
    if now - latest_close > 3 * step + timedelta(minutes=5):
        r.add("candles_lagging", latest_close=latest_close.isoformat())
    return r
