"""Close above the prior 20-bar high, with volume at least the prior 20-bar average.

Exit when the close falls through the prior 10-bar low. The current bar is not part of either window.
"""
from __future__ import annotations

from typing import Sequence

from app.domain.market import Candle
from app.strategy.base import Action, Signal

ENTRY_BARS = 20
EXIT_BARS = 10


class Breakout:
    name = "breakout"
    version = "1.0.0"
    max_hold_bars = None
    use_regime = False

    def __init__(self, entry_bars: int = ENTRY_BARS, exit_bars: int = EXIT_BARS, trail_pct: float | None = None):
        self.entry_bars = entry_bars
        self.exit_bars = exit_bars
        self.trail_pct = trail_pct

    def on_bar(self, candles: Sequence[Candle]) -> Signal:
        if len(candles) < self.entry_bars + 2:
            return Signal(self.name, Action.HOLD, "warmup")
        prior = candles[-(self.entry_bars + 1):-1]
        last = candles[-1]
        prior_high = max(c.high for c in prior)
        vols = [float(c.volume_usd) for c in candles]
        prev_avg = sum(vols[-(self.entry_bars + 1):-1]) / self.entry_bars
        exit_window = candles[-(self.exit_bars + 1):-1]
        prior_low = min(c.low for c in exit_window)
        if last.close < prior_low:
            return Signal(self.name, Action.EXIT, "close through the prior low")
        if last.close > prior_high and prev_avg > 0 and float(last.volume_usd) > prev_avg:
            return Signal(self.name, Action.ENTER, "close through the prior high")
        return Signal(self.name, Action.HOLD, "inside the channel")
