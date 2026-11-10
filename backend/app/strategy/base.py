"""Strategy interface. `on_bar` sees only closed candles up to and including the decision bar."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, Sequence

from app.domain.market import Candle


class Action(StrEnum):
    ENTER = "enter"
    EXIT = "exit"
    HOLD = "hold"


@dataclass(frozen=True, slots=True)
class Signal:
    strategy: str
    action: Action
    reason: str


class Strategy(Protocol):
    name: str
    version: str
    max_hold_bars: int | None

    def on_bar(self, candles: Sequence[Candle]) -> Signal: ...
