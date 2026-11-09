"""Enter after a burst of range has contracted and price has dipped off the local high.

This is the post-launch pattern when the candle series actually covers the launch: the first
bars are wide, later bars are quieter, and the entry is a bounce off a dip rather than a chase.
On older series it is just a volatility-contraction pullback. The trail, when set, is applied
by the backtest engine.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Sequence

from app.domain.market import Candle
from app.strategy.base import Action, Signal


class Pullback:
    name = "pullback"
    version = "1.0.0"
    max_hold_bars = 8

    def __init__(self, dip: float = 0.05, settle_bars: int = 8, lookback: int = 12,
                 hold: int = 8, trail_pct: float | None = None, use_regime: bool = False):
        self.dip = dip
        self.settle_bars = settle_bars
        self.lookback = lookback
        self.max_hold_bars = hold
        self.trail_pct = trail_pct
        self.use_regime = use_regime

    def on_bar(self, candles: Sequence[Candle]) -> Signal:
        need = max(self.settle_bars, self.lookback, 8) + 1
        if len(candles) < need:
            return Signal(self.name, Action.HOLD, "warmup")
        window = list(candles[-self.lookback:])
        early, late = window[:4], window[-4:]
        early_range = max(c.high for c in early) - min(c.low for c in early)
        late_range = max(c.high for c in late) - min(c.low for c in late)
        high = max(c.high for c in window)
        last, prev = candles[-1], candles[-2]
        settled = early_range > 0 and late_range < early_range
        dip = Decimal(str(self.dip))
        dipped = high > 0 and last.close <= high * (Decimal(1) - dip)
        bounce = last.close > last.open and last.close > prev.close
        if settled and dipped and bounce:
            return Signal(self.name, Action.ENTER, "range contracted and price bounced off a dip")
        if last.close < min(c.low for c in late):
            return Signal(self.name, Action.EXIT, "failed the recent low")
        return Signal(self.name, Action.HOLD, "no contraction dip")
