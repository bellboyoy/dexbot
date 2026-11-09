"""Up-bar whose volume is at least 3x the prior 20-bar average. Exit is a 6-bar time stop."""
from __future__ import annotations

from typing import Sequence

from app.domain.market import Candle
from app.strategy.base import Action, Signal

LOOKBACK = 20
MULTIPLE = 3.0


class VolumeSpike:
    name = "volume_spike"
    version = "1.0.0"
    max_hold_bars = 6

    def on_bar(self, candles: Sequence[Candle]) -> Signal:
        if len(candles) < LOOKBACK + 2:
            return Signal(self.name, Action.HOLD, "warmup")
        vols = [float(c.volume_usd) for c in candles]
        baseline = sum(vols[-(LOOKBACK + 1):-1]) / LOOKBACK
        prev_baseline = sum(vols[-(LOOKBACK + 2):-2]) / LOOKBACK
        last = candles[-1]
        prev = candles[-2]
        spiked = baseline > 0 and vols[-1] > MULTIPLE * baseline
        prev_spiked = prev_baseline > 0 and vols[-2] > MULTIPLE * prev_baseline
        green = last.close > last.open and last.close > prev.close
        if spiked and green and not prev_spiked:
            return Signal(self.name, Action.ENTER, "volume spike on an up bar")
        return Signal(self.name, Action.HOLD, "no new spike")
