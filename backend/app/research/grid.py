"""Pre-registered parameter grid. The count is whatever `build_grid()` returns, not a tuned subset.

Neighbours differ in exactly one of: parameter dict, position size, slippage cap.
A config with no neighbour cannot pass the robustness check.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.risk.limits import RiskLimits
from app.strategy.registry import build_strategy


@dataclass(frozen=True, slots=True)
class Config:
    family: str
    label: str
    params: dict
    size: Decimal
    slippage_bps: int = 100

    def strategy(self):
        return build_strategy(self.family, self.params)

    def limits(self) -> RiskLimits:
        return RiskLimits(max_position_usd=self.size, max_slippage_bps=self.slippage_bps)


def _add(grid: list[Config], family: str, label: str, params: dict, size: int, slip: int = 100) -> None:
    grid.append(Config(family, label, params, Decimal(size), slip))


def build_grid() -> list[Config]:
    grid: list[Config] = []
    for size in (10, 25, 50):
        _add(grid, "momentum", f"m_f8_s{size}", {"fast": 8, "slow": 21}, size)
    for size in (10, 25):
        _add(grid, "momentum", f"m_f12_s{size}", {"fast": 12, "slow": 26}, size)
    _add(grid, "momentum", "m_f8_s10_reg", {"fast": 8, "slow": 21, "use_regime": True}, 10)

    for size in (10, 25, 50):
        _add(grid, "volume_spike", f"vs_m25_s{size}", {"multiple": 2.5, "hold": 6}, size)
    for size in (10, 25):
        _add(grid, "volume_spike", f"vs_m40_s{size}", {"multiple": 4.0, "hold": 6}, size)

    for size in (10, 25, 50):
        _add(grid, "breakout", f"bo_e12_s{size}", {"entry_bars": 12, "exit_bars": 8}, size)
    for size in (10, 25):
        _add(grid, "breakout", f"bo_e30_s{size}", {"entry_bars": 30, "exit_bars": 10}, size)
    _add(grid, "breakout", "bo_e12_s10_trail", {"entry_bars": 12, "exit_bars": 8, "trail_pct": 0.06}, 10)

    for size in (10, 25, 50):
        _add(grid, "mean_reversion", f"mr_r25_s{size}", {"entry_rsi": 25}, size)
    for size in (10, 25):
        _add(grid, "mean_reversion", f"mr_r35_s{size}", {"entry_rsi": 35}, size)

    for size in (10, 25, 50):
        _add(grid, "pullback", f"pb_d4_s{size}", {"dip": 0.04, "hold": 8}, size)
    for size in (10, 25):
        _add(grid, "pullback", f"pb_d8_s{size}", {"dip": 0.08, "hold": 8}, size)
    _add(grid, "pullback", "pb_d4_s10_trail", {"dip": 0.04, "hold": 8, "trail_pct": 0.05}, 10)
    _add(grid, "pullback", "pb_d4_s10_reg", {"dip": 0.04, "hold": 8, "use_regime": True}, 10)

    for size in (10, 25, 50):
        _add(grid, "volume_accel", f"va_k3_s{size}", {"span": 3, "hold": 4, "trail_pct": 0.05}, size)
    for size in (10, 25):
        _add(grid, "volume_accel", f"va_k5_s{size}", {"span": 5, "hold": 4, "trail_pct": 0.05}, size)
    _add(grid, "volume_accel", "va_k3_s10_tight", {"span": 3, "hold": 4, "trail_pct": 0.05}, 10, slip=50)
    return grid


def is_neighbor(left: Config, right: Config) -> bool:
    if left.family != right.family or left.label == right.label:
        return False
    diffs = (left.params != right.params) + (left.size != right.size) + (left.slippage_bps != right.slippage_bps)
    return diffs == 1
