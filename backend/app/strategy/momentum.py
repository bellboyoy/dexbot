"""EMA cross with an RSI band. Parameters were fixed before any backtest run."""
from __future__ import annotations

from typing import Sequence

from app.domain.market import Candle
from app.strategy.base import Action, Signal
from app.strategy.indicators import ema, rsi

FAST = 9
SLOW = 21
RSI_PERIOD = 14
RSI_MIN = 50.0
RSI_MAX = 70.0
RSI_EXIT = 80.0


class Momentum:
    name = "momentum"
    version = "1.0.0"
    max_hold_bars = None

    def on_bar(self, candles: Sequence[Candle]) -> Signal:
        closes = [float(c.close) for c in candles]
        if len(closes) < SLOW + 2:
            return Signal(self.name, Action.HOLD, "warmup")
        fast = ema(closes, FAST)
        slow = ema(closes, SLOW)
        strength = rsi(closes, RSI_PERIOD)
        f0, f1 = fast[-2], fast[-1]
        s0, s1 = slow[-2], slow[-1]
        r1 = strength[-1]
        if None in (f0, f1, s0, s1) or r1 is None:
            return Signal(self.name, Action.HOLD, "indicators not ready")
        crossed_up = f0 <= s0 and f1 > s1
        crossed_down = f0 >= s0 and f1 < s1
        if crossed_down or r1 > RSI_EXIT:
            return Signal(self.name, Action.EXIT, "ema cross down" if crossed_down else "rsi extended")
        if crossed_up and RSI_MIN <= r1 <= RSI_MAX and closes[-1] > f1:
            return Signal(self.name, Action.ENTER, "ema cross up inside rsi band")
        return Signal(self.name, Action.HOLD, "no cross")
