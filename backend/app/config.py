"""Environment-based configuration. Every setting is prefixed DEXBOT_."""
from __future__ import annotations

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
    geckoterminal_rpm: int = 3
    coingecko_api_key: SecretStr | None = None

    collector_max_watchlist: int = 300

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
