"""Risk limits. Averaging down and martingale are not settings: they are refused in code."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.config import Settings


@dataclass(frozen=True, slots=True)
class RiskLimits:
    max_position_usd: Decimal = Decimal("50")
    max_total_exposure_usd: Decimal = Decimal("300")
    max_concurrent_positions: int = 5
    max_daily_loss_usd: Decimal = Decimal("50")
    gas_reserve_usd: Decimal = Decimal("5")
    stop_loss_pct: Decimal = Decimal("0.08")
    take_profit_pct: Decimal = Decimal("0.15")
    max_hold_minutes: int = 240
    cooldown_losses: int = 2
    cooldown_seconds: int = 900
    max_slippage_bps: int = 200

    def __post_init__(self) -> None:
        if self.max_position_usd <= 0 or self.max_total_exposure_usd < self.max_position_usd:
            raise ValueError("exposure limit must cover at least one position")
        if self.max_concurrent_positions < 1:
            raise ValueError("max concurrent positions must be >= 1")
        if self.stop_loss_pct <= 0 or self.take_profit_pct <= 0:
            raise ValueError("stop and take-profit must be positive")
        if self.max_daily_loss_usd <= 0 or self.gas_reserve_usd < 0:
            raise ValueError("daily loss must be positive and gas reserve cannot be negative")
        if self.cooldown_losses < 1 or self.cooldown_seconds < 0:
            raise ValueError("cooldown is misconfigured")


def limits_from_settings(settings: Settings) -> RiskLimits:
    return RiskLimits(
        max_position_usd=settings.risk_max_position_usd,
        max_total_exposure_usd=settings.risk_max_exposure_usd,
        max_concurrent_positions=settings.risk_max_concurrent_positions,
        max_daily_loss_usd=settings.risk_max_daily_loss_usd,
        gas_reserve_usd=settings.risk_gas_reserve_usd,
        stop_loss_pct=settings.risk_stop_loss_pct,
        take_profit_pct=settings.risk_take_profit_pct,
        max_hold_minutes=settings.risk_max_hold_minutes,
        cooldown_losses=settings.risk_cooldown_losses,
        cooldown_seconds=settings.risk_cooldown_seconds,
        max_slippage_bps=settings.risk_max_slippage_bps,
    )
