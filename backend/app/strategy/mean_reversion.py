"""Buy a cross into RSI < 30 and below the lower Bollinger band. Exit back at the mean or RSI > 50.

This is the strategy most likely to lose on a trending new token. It is included so the
backtest can reject it when the costs say so, not because a dip is assumed to bounce.
"""
from __future__ import annotations

from typing import Sequence

from app.domain.market import Candle
from app.strategy.base import Action, Signal
from app.strategy.indicators import rolling_std, rsi, sma

PERIOD = 20
RSI_PERIOD = 14
ENTRY_RSI = 30.0
EXIT_RSI = 50.0
BANDS = 2.0


class MeanReversion:
    name = "mean_reversion"
    version = "1.0.0"
    max_hold_bars = None
    trail_pct = None
    use_regime = False

    def __init__(self, entry_rsi: float = ENTRY_RSI):
        self.entry_rsi = entry_rsi

    def on_bar(self, candles: Sequence[Candle]) -> Signal:
        closes = [float(c.close) for c in candles]
        if len(closes) < PERIOD + 2:
            return Signal(self.name, Action.HOLD, "warmup")
        mean = sma(closes, PERIOD)
        sigma = rolling_std(closes, PERIOD)
        strength = rsi(closes, RSI_PERIOD)
        m1, s1, r0, r1 = mean[-1], sigma[-1], strength[-2], strength[-1]
        if None in (m1, s1, r0, r1):
            return Signal(self.name, Action.HOLD, "indicators not ready")
        if r1 > EXIT_RSI or closes[-1] >= m1:
            return Signal(self.name, Action.EXIT, "reverted toward the mean" if closes[-1] >= m1 else "rsi recovered")
        lower = m1 - BANDS * s1
        if r0 >= self.entry_rsi and r1 < self.entry_rsi and closes[-1] < lower:
            return Signal(self.name, Action.ENTER, "rsi crossed below 30 under the lower band")
        return Signal(self.name, Action.HOLD, "no stretch")
