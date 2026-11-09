"""The strategy set. Names here are the ones the backtest is allowed to validate."""
from __future__ import annotations

from app.strategy.base import Strategy
from app.strategy.breakout import Breakout
from app.strategy.mean_reversion import MeanReversion
from app.strategy.momentum import Momentum
from app.strategy.pullback import Pullback
from app.strategy.volume_accel import VolumeAccel
from app.strategy.volume_spike import VolumeSpike

_TYPES = (Momentum, VolumeSpike, Breakout, MeanReversion, Pullback, VolumeAccel)


def build_strategies() -> list[Strategy]:
    return [cls() for cls in _TYPES]


def build_strategy(name: str, params: dict | None = None) -> Strategy:
    """`params` are the train-selected values. Omitted keys keep the class defaults."""
    for cls in _TYPES:
        if cls.name == name:
            return cls(**(params or {}))
    raise KeyError(name)
