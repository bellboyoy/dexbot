"""Unusual-momentum detection. Produces momentum events for Phase 3 strategies to study;
it is not a trade signal. Only reliable metrics are considered, and missing inputs never
count as a trigger.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from app.intelligence.metrics import Metrics


@dataclass(frozen=True)
class MomentumThresholds:
    volume_accel_m5: Decimal
    min_volume_m5_usd: Decimal
    volume_accel_h1: Decimal
    buy_ratio_m5: Decimal
    min_txns_m5: int
    price_change_h1_pct: Decimal
    liquidity_change_1h_pct: Decimal


@dataclass(frozen=True)
class MomentumConfig:
    established: MomentumThresholds = MomentumThresholds(
        Decimal(3), Decimal(5_000), Decimal("2.5"), Decimal("0.65"), 20, Decimal(8), Decimal(20))
    new: MomentumThresholds = MomentumThresholds(
        Decimal(3), Decimal(2_000), Decimal(3), Decimal("0.65"), 20, Decimal(20), Decimal(30))
    min_triggers: int = 2
    cooldown: timedelta = timedelta(minutes=15)


VOLUME_TRIGGERS = {"volume_spike", "sustained_volume"}


def triggers(m: Metrics, profile: str, cfg: MomentumConfig) -> list[str]:
    if not m.reliable:
        return []
    t = cfg.new if profile == "new" else cfg.established
    out = []
    if (m.volume_accel_m5 is not None and m.volume_m5_usd is not None
            and m.volume_accel_m5 >= t.volume_accel_m5 and m.volume_m5_usd >= t.min_volume_m5_usd):
        out.append("volume_spike")
    if m.volume_accel_h1 is not None and m.volume_accel_h1 >= t.volume_accel_h1:
        out.append("sustained_volume")
    if (m.buy_ratio_m5 is not None and m.txns_m5 is not None
            and m.buy_ratio_m5 >= t.buy_ratio_m5 and m.txns_m5 >= t.min_txns_m5):
        out.append("buy_pressure")
    if m.price_change_h1_pct is not None and m.price_change_h1_pct >= t.price_change_h1_pct:
        out.append("price_breakout")
    if m.liquidity_change_1h_pct is not None and m.liquidity_change_1h_pct >= t.liquidity_change_1h_pct:
        out.append("liquidity_inflow")
    return out


def is_momentum(trig: list[str], cfg: MomentumConfig) -> bool:
    return len(trig) >= cfg.min_triggers and bool(VOLUME_TRIGGERS & set(trig))


class MomentumDetector:
    def __init__(self, cfg: MomentumConfig | None = None):
        self.cfg = cfg or MomentumConfig()
        self._last_event: dict[tuple, datetime] = {}

    def prime(self, key: tuple, at: datetime) -> None:
        self._last_event[key] = max(at, self._last_event.get(key, at))

    def evaluate(self, m: Metrics, profile: str) -> list[str] | None:
        """Triggers if this is a new momentum event (outside the cooldown), else None."""
        trig = triggers(m, profile, self.cfg)
        if not is_momentum(trig, self.cfg):
            return None
        key = (m.chain, m.pool_address)
        last = self._last_event.get(key)
        if last is not None and m.observed_at - last < self.cfg.cooldown:
            return None
        self._last_event[key] = m.observed_at
        return trig
