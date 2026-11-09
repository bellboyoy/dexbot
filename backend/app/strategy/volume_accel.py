"""Enter when volume is both rising bar to bar and above its recent baseline.

The hold is short on purpose: a 15-minute strategy that needs a large move to pay the
round trip should not sit in the position. The engine also applies the stop and, when
configured, a trailing stop.
"""
from __future__ import annotations

from typing import Sequence

from app.domain.market import Candle
from app.strategy.base import Action, Signal


class VolumeAccel:
    name = "volume_accel"
    version = "1.0.0"

    def __init__(self, span: int = 3, multiple: float = 1.8, hold: int = 4, trail_pct: float | None = 0.05,
                 use_regime: bool = False):
        self.span = span
        self.multiple = multiple
        self.max_hold_bars = hold
        self.trail_pct = trail_pct
        self.use_regime = use_regime

    def on_bar(self, candles: Sequence[Candle]) -> Signal:
        if len(candles) < self.span + 9:
            return Signal(self.name, Action.HOLD, "warmup")
        vols = [float(c.volume_usd) for c in candles]
        rising = all(vols[-i] > vols[-i - 1] > 0 for i in range(1, self.span))
        baseline_window = vols[-(self.span + 8):-self.span]
        baseline = sum(baseline_window) / len(baseline_window)
        last = candles[-1]
        if rising and baseline > 0 and vols[-1] > self.multiple * baseline and last.close > last.open:
            return Signal(self.name, Action.ENTER, "volume accelerating on an up bar")
        return Signal(self.name, Action.HOLD, "no acceleration")
