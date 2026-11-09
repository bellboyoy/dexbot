"""Environment-based configuration. Every setting is prefixed DEXBOT_."""
from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from typing import Annotated

from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.domain.chains import Chain


class TradingMode(StrEnum):
    PAPER = "paper"
    LIVE = "live"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DEXBOT_", env_file=".env", extra="ignore")

    env: str = "development"
    log_level: str = "INFO"
    log_json: bool = True

    enabled_chains: Annotated[list[Chain], NoDecode] = Field(default_factory=lambda: [Chain.SOLANA, Chain.BASE, Chain.BSC])

    database_url: str = "postgresql+asyncpg://dexbot:dexbot@localhost:5432/dexbot"
    redis_url: str = "redis://localhost:6379/0"

    dexscreener_base_url: str = "https://api.dexscreener.com"
    dexscreener_pairs_rpm: int = 200
    dexscreener_profiles_rpm: int = 55
    geckoterminal_base_url: str = "https://api.geckoterminal.com/api/v2"
    geckoterminal_rpm: int = 5
    coingecko_api_key: SecretStr | None = None

    http_timeout_s: float = 10.0
    http_max_retries: int = 3
    breaker_failure_threshold: int = 5
    breaker_cooldown_s: float = 60.0

    solana_rpc_url: SecretStr | None = None
    solana_ws_url: SecretStr | None = None
    base_rpc_url: SecretStr | None = None
    bsc_rpc_url: SecretStr | None = None
    arbitrum_rpc_url: SecretStr | None = None

    zerox_api_key: SecretStr | None = None
    jupiter_api_key: SecretStr | None = None
    goplus_api_key: SecretStr | None = None

    # Paper ledger and risk limits. These cannot enable live trading.
    paper_starting_balance_usd: Decimal = Decimal("1000")
    paper_scan_interval_s: float = 90.0
    paper_max_security_checks: int = 3
    paper_max_ohlcv_per_cycle: int = 1
    risk_max_position_usd: Decimal = Decimal("50")
    risk_max_exposure_usd: Decimal = Decimal("300")
    risk_max_concurrent_positions: int = 5
    risk_max_daily_loss_usd: Decimal = Decimal("50")
    risk_gas_reserve_usd: Decimal = Decimal("5")
    risk_stop_loss_pct: Decimal = Decimal("0.08")
    risk_take_profit_pct: Decimal = Decimal("0.15")
    risk_max_hold_minutes: int = 240
    risk_cooldown_losses: int = 2
    risk_cooldown_seconds: int = 900
    risk_max_slippage_bps: int = 200
    screen_min_liquidity_usd: Decimal = Decimal("20000")
    screen_min_volume_h1_usd: Decimal = Decimal("5000")
    screen_min_age_minutes: int = 30
    screen_max_tax: Decimal = Decimal("0.10")
    screen_lp_lock_young_hours: int = 72
    screen_min_lp_locked_pct: Decimal = Decimal("80")

    # Point-in-time launch collector. GT calls are kept far below the shared ~5/min IP budget.
    collector_poll_s: float = 20.0
    collector_gt_interval_s: float = 180.0
    collector_max_active: int = 200
    collector_max_new_per_day: int = 150
    collector_snapshots_per_cycle: int = 4
    collector_screens_per_cycle: int = 2

    @field_validator("enabled_chains", mode="before")
    @classmethod
    def _split_chains(cls, v: object) -> object:
        if isinstance(v, str):
            return [c.strip() for c in v.split(",") if c.strip()]
        return v

    @property
    def trading_mode(self) -> TradingMode:
        # Deliberately not configurable from the environment. Live activation is a
        # separate, explicit runtime flow (Phase 9) gated by the risk engine.
        return TradingMode.PAPER


@lru_cache
def get_settings() -> Settings:
    return Settings()
