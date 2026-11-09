"""SQLAlchemy models for the PostgreSQL schema (see docs/ARCHITECTURE.md §5)."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger, Boolean, DateTime, Enum, ForeignKey, Index, Integer, Numeric, String, Text,
    UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.domain.chains import Chain

USD = Numeric(38, 18)
RAW = Numeric(78, 0)          # fits uint256
CHAIN = Enum(Chain, name="chain", values_callable=lambda e: [m.value for m in e])


class Base(DeclarativeBase):
    # none_as_null: a Python None is stored as SQL NULL (not JSON null), so IS NULL queries work.
    type_annotation_map = {dict[str, Any]: JSONB(none_as_null=True), datetime: DateTime(timezone=True)}


class Timestamps:
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class Token(Timestamps, Base):
    __tablename__ = "tokens"
    __table_args__ = (UniqueConstraint("chain", "address"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    chain: Mapped[Chain] = mapped_column(CHAIN)
    address: Mapped[str] = mapped_column(String(64))
    symbol: Mapped[str | None] = mapped_column(String(64))
    name: Mapped[str | None] = mapped_column(String(256))
    decimals: Mapped[int | None] = mapped_column(Integer)
    is_validated: Mapped[bool] = mapped_column(Boolean, default=False)
    first_seen_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Pool(Timestamps, Base):
    __tablename__ = "pools"
    __table_args__ = (UniqueConstraint("chain", "address"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    chain: Mapped[Chain] = mapped_column(CHAIN)
    address: Mapped[str] = mapped_column(String(80))     # contract address or 32-byte pool id
    dex_id: Mapped[str | None] = mapped_column(String(64))
    base_token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id"))
    quote_token_id: Mapped[int | None] = mapped_column(ForeignKey("tokens.id"))
    pool_created_at: Mapped[datetime | None]
    first_seen_at: Mapped[datetime] = mapped_column(server_default=func.now())


class MarketSnapshot(Base):
    __tablename__ = "market_snapshots"
    __table_args__ = (Index("ix_snapshots_pool_time", "pool_id", "observed_at"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    pool_id: Mapped[int] = mapped_column(ForeignKey("pools.id"))
    chain: Mapped[Chain] = mapped_column(CHAIN)
    source: Mapped[str] = mapped_column(String(32))
    observed_at: Mapped[datetime]
    price_usd: Mapped[Decimal | None] = mapped_column(USD)
    liquidity_usd: Mapped[Decimal | None] = mapped_column(USD)
    fdv_usd: Mapped[Decimal | None] = mapped_column(USD)
    market_cap_usd: Mapped[Decimal | None] = mapped_column(USD)
    windows: Mapped[dict[str, Any]]          # {"m5": {"buys":..,"buyers":..,"volume_usd":..}, ...}
    quality_flags: Mapped[list[str] | None] = mapped_column(JSONB(none_as_null=True))    # see app/quality/checks.py
    raw: Mapped[dict[str, Any] | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class CandleRow(Base):
    __tablename__ = "candles"
    __table_args__ = (UniqueConstraint("pool_id", "source", "timeframe", "open_time"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    pool_id: Mapped[int] = mapped_column(ForeignKey("pools.id"))
    chain: Mapped[Chain] = mapped_column(CHAIN)
    source: Mapped[str] = mapped_column(String(32))
    timeframe: Mapped[str] = mapped_column(String(8))
    open_time: Mapped[datetime]
    open: Mapped[Decimal] = mapped_column(USD)
    high: Mapped[Decimal] = mapped_column(USD)
    low: Mapped[Decimal] = mapped_column(USD)
    close: Mapped[Decimal] = mapped_column(USD)
    volume_usd: Mapped[Decimal] = mapped_column(USD)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Signal(Timestamps, Base):
    __tablename__ = "signals"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    chain: Mapped[Chain] = mapped_column(CHAIN)
    pool_id: Mapped[int] = mapped_column(ForeignKey("pools.id"))
    strategy: Mapped[str] = mapped_column(String(64), index=True)
    strategy_version: Mapped[str] = mapped_column(String(32))
    score: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    score_components: Mapped[dict[str, Any] | None]
    features: Mapped[dict[str, Any] | None]
    decision: Mapped[str] = mapped_column(String(32))          # accepted / rejected_score / rejected_security / rejected_risk
    decision_reason: Mapped[str | None] = mapped_column(Text)
    generated_at: Mapped[datetime]


class SecurityCheck(Base):
    __tablename__ = "security_checks"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id"), index=True)
    chain: Mapped[Chain] = mapped_column(CHAIN)
    provider: Mapped[str] = mapped_column(String(32))
    check_name: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(8))               # PASS / FAIL / UNKNOWN
    critical: Mapped[bool] = mapped_column(Boolean)
    details: Mapped[dict[str, Any] | None]
    checked_at: Mapped[datetime]


class Wallet(Timestamps, Base):
    """Trading wallet metadata only. Keys never touch this database."""
    __tablename__ = "wallets"
    __table_args__ = (UniqueConstraint("chain_family", "address"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    chain_family: Mapped[str] = mapped_column(String(16))
    address: Mapped[str] = mapped_column(String(64))
    label: Mapped[str] = mapped_column(String(64))
    max_trade_usd: Mapped[Decimal] = mapped_column(USD)
    max_daily_spend_usd: Mapped[Decimal] = mapped_column(USD)
    active: Mapped[bool] = mapped_column(Boolean, default=False)


class Position(Timestamps, Base):
    __tablename__ = "positions"
    __table_args__ = (Index("ix_positions_status", "status"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    mode: Mapped[str] = mapped_column(String(8))                  # paper / live
    chain: Mapped[Chain] = mapped_column(CHAIN)
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id"))
    pool_id: Mapped[int] = mapped_column(ForeignKey("pools.id"))
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"))
    wallet_id: Mapped[int | None] = mapped_column(ForeignKey("wallets.id"))
    strategy: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))               # pending_entry/open/pending_exit/closed/failed/needs_reconcile
    size_usd: Mapped[Decimal] = mapped_column(USD)
    token_amount_raw: Mapped[Decimal | None] = mapped_column(RAW)
    entry_price_usd: Mapped[Decimal | None] = mapped_column(USD)
    exit_price_usd: Mapped[Decimal | None] = mapped_column(USD)
    planned_max_loss_usd: Mapped[Decimal] = mapped_column(USD)
    stop_loss_price_usd: Mapped[Decimal | None] = mapped_column(USD)
    take_profit_price_usd: Mapped[Decimal | None] = mapped_column(USD)
    trailing_stop_pct: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    max_hold_until: Mapped[datetime]
    opened_at: Mapped[datetime | None]
    closed_at: Mapped[datetime | None]
    exit_reason: Mapped[str | None] = mapped_column(String(32))
    fees_usd: Mapped[Decimal] = mapped_column(USD, default=0)
    gas_usd: Mapped[Decimal] = mapped_column(USD, default=0)
    net_pnl_usd: Mapped[Decimal | None] = mapped_column(USD)


class Trade(Base):
    __tablename__ = "trades"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    position_id: Mapped[int] = mapped_column(ForeignKey("positions.id"), index=True)
    chain: Mapped[Chain] = mapped_column(CHAIN)
    mode: Mapped[str] = mapped_column(String(8))
    side: Mapped[str] = mapped_column(String(4))
    amount_in_raw: Mapped[Decimal] = mapped_column(RAW)
    amount_out_raw: Mapped[Decimal] = mapped_column(RAW)
    price_usd: Mapped[Decimal] = mapped_column(USD)
    fees_usd: Mapped[Decimal] = mapped_column(USD)
    gas_usd: Mapped[Decimal] = mapped_column(USD)
    slippage_bps: Mapped[int | None] = mapped_column(Integer)
    price_impact_bps: Mapped[int | None] = mapped_column(Integer)
    executed_at: Mapped[datetime]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Transaction(Timestamps, Base):
    __tablename__ = "transactions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    chain: Mapped[Chain] = mapped_column(CHAIN)
    mode: Mapped[str] = mapped_column(String(8))
    trade_id: Mapped[int | None] = mapped_column(ForeignKey("trades.id"))
    position_id: Mapped[int | None] = mapped_column(ForeignKey("positions.id"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    tx_id: Mapped[str | None] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(16))
    nonce: Mapped[int | None] = mapped_column(BigInteger)
    error: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any] | None]


class PortfolioBalance(Base):
    __tablename__ = "portfolio_balances"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    mode: Mapped[str] = mapped_column(String(8))
    wallet_id: Mapped[int | None] = mapped_column(ForeignKey("wallets.id"))
    chain: Mapped[Chain] = mapped_column(CHAIN)
    token_id: Mapped[int | None] = mapped_column(ForeignKey("tokens.id"))
    amount_raw: Mapped[Decimal] = mapped_column(RAW)
    value_usd: Mapped[Decimal | None] = mapped_column(USD)
    observed_at: Mapped[datetime]


class StrategyPerformance(Timestamps, Base):
    __tablename__ = "strategy_performance"
    __table_args__ = (UniqueConstraint("strategy", "mode", "period_start", "period_end"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    strategy: Mapped[str] = mapped_column(String(64))
    mode: Mapped[str] = mapped_column(String(8))
    chain: Mapped[Chain | None] = mapped_column(CHAIN)
    period_start: Mapped[datetime]
    period_end: Mapped[datetime]
    trades: Mapped[int] = mapped_column(Integer)
    metrics: Mapped[dict[str, Any]]      # win_rate, profit_factor, expectancy, max_dd, sharpe, sortino, fees...


class RiskEvent(Base):
    __tablename__ = "risk_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    severity: Mapped[str] = mapped_column(String(16))
    chain: Mapped[Chain | None] = mapped_column(CHAIN)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"))
    position_id: Mapped[int | None] = mapped_column(ForeignKey("positions.id"))
    details: Mapped[dict[str, Any] | None]
    occurred_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ConfigEntry(Base):
    __tablename__ = "config"
    __table_args__ = (UniqueConstraint("key", "version"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    key: Mapped[str] = mapped_column(String(128))
    value: Mapped[dict[str, Any]]
    version: Mapped[int] = mapped_column(Integer)
    changed_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class SystemLog(Base):
    __tablename__ = "system_logs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    level: Mapped[str] = mapped_column(String(8))
    component: Mapped[str] = mapped_column(String(64))
    event: Mapped[str] = mapped_column(String(128))
    chain: Mapped[Chain | None] = mapped_column(CHAIN)
    context: Mapped[dict[str, Any] | None]
    logged_at: Mapped[datetime] = mapped_column(server_default=func.now(), index=True)


class DiscoveryLead(Base):
    """Every discovery lead and its screening outcome, including rejections.

    Kept so later analysis sees the full population of tokens that were visible at the
    time, not just the survivors (survivorship bias).
    """
    __tablename__ = "discovery_leads"
    __table_args__ = (Index("ix_leads_chain_token", "chain", "token_address"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    chain: Mapped[Chain] = mapped_column(CHAIN)
    token_address: Mapped[str] = mapped_column(String(64))
    pool_address: Mapped[str | None] = mapped_column(String(80))
    pool_id: Mapped[int | None] = mapped_column(ForeignKey("pools.id"))
    source: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(String(64))
    pipeline: Mapped[str | None] = mapped_column(String(16))         # new / established / momentum
    profile: Mapped[str | None] = mapped_column(String(16))          # new / established
    passed: Mapped[bool | None] = mapped_column(Boolean)              # None = could not be resolved
    rejections: Mapped[dict[str, Any] | None]
    metrics: Mapped[dict[str, Any] | None]
    discovered_at: Mapped[datetime] = mapped_column(index=True)


class WatchedPool(Timestamps, Base):
    """Pools the collector records history for. Status moves active -> inactive/dead -> retired;
    dead pools keep being recorded for a while so their failure is in the data."""
    __tablename__ = "watchlist"
    __table_args__ = (Index("ix_watchlist_status", "status"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    pool_id: Mapped[int] = mapped_column(ForeignKey("pools.id"), unique=True)
    chain: Mapped[Chain] = mapped_column(CHAIN)
    base_token_address: Mapped[str] = mapped_column(String(64))
    profile: Mapped[str] = mapped_column(String(16))
    pipeline: Mapped[str] = mapped_column(String(16), server_default="new")  # discovery pipeline that added it
    status: Mapped[str] = mapped_column(String(16))                   # active / inactive / dead / retired
    discovered_via: Mapped[str] = mapped_column(String(64))
    added_at: Mapped[datetime]
    status_since: Mapped[datetime]
    last_snapshot_at: Mapped[datetime | None]
    consecutive_misses: Mapped[int] = mapped_column(Integer, default=0)
    next_ohlcv_at: Mapped[datetime | None]
    ohlcv_backfilled: Mapped[bool] = mapped_column(Boolean, default=False)
    buyers_checked_at: Mapped[datetime | None]


class PoolMetrics(Base):
    """Derived market metrics per pool per collection cycle.

    Every value is None when its inputs are missing (never zero). `reliable` is False when
    any input observation carried a quality flag; consumers must not act on unreliable rows.
    """
    __tablename__ = "pool_metrics"
    __table_args__ = (Index("ix_pool_metrics_pool_time", "pool_id", "observed_at"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    pool_id: Mapped[int] = mapped_column(ForeignKey("pools.id"))
    chain: Mapped[Chain] = mapped_column(CHAIN)
    observed_at: Mapped[datetime]                     # time of the snapshot the metrics derive from
    source: Mapped[str] = mapped_column(String(32))   # primary snapshot source
    price_usd: Mapped[Decimal | None] = mapped_column(USD)
    liquidity_usd: Mapped[Decimal | None] = mapped_column(USD)
    volume_m5_usd: Mapped[Decimal | None] = mapped_column(USD)
    volume_h1_usd: Mapped[Decimal | None] = mapped_column(USD)
    volume_accel_m5: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))   # m5 volume vs h1 average 5m
    volume_accel_h1: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))   # h1 volume vs h24 average hour
    buy_ratio_m5: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))      # buys / (buys + sells)
    buy_ratio_h1: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    buyers_h1: Mapped[int | None] = mapped_column(Integer)                    # unique buyers (GeckoTerminal)
    sellers_h1: Mapped[int | None] = mapped_column(Integer)
    buyers_observed_at: Mapped[datetime | None]
    liquidity_change_5m_pct: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    liquidity_change_1h_pct: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    price_change_m5_pct: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    price_change_h1_pct: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    reliable: Mapped[bool] = mapped_column(Boolean)
    quality_flags: Mapped[list[str] | None] = mapped_column(JSONB(none_as_null=True))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class DataQualityEvent(Base):
    __tablename__ = "data_quality_events"
    __table_args__ = (Index("ix_dq_chain_check_time", "chain", "check", "observed_at"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    chain: Mapped[Chain] = mapped_column(CHAIN)
    pool_id: Mapped[int | None] = mapped_column(ForeignKey("pools.id"))
    source: Mapped[str] = mapped_column(String(32))
    check: Mapped[str] = mapped_column(String(48))
    severity: Mapped[str] = mapped_column(String(8))         # info / warn / error
    observed_at: Mapped[datetime]
    details: Mapped[dict[str, Any] | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class MomentumEvent(Base):
    """Unusual momentum detected by the momentum pipeline (input for Phase 3, not a trade signal)."""
    __tablename__ = "momentum_events"
    __table_args__ = (Index("ix_momentum_chain_time", "chain", "detected_at"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    chain: Mapped[Chain] = mapped_column(CHAIN)
    pool_id: Mapped[int] = mapped_column(ForeignKey("pools.id"))
    profile: Mapped[str] = mapped_column(String(16))
    detected_at: Mapped[datetime]
    metrics_id: Mapped[int | None] = mapped_column(ForeignKey("pool_metrics.id"))
    triggers: Mapped[list[str]] = mapped_column(JSONB)
    details: Mapped[dict[str, Any] | None]


class CollectorState(Base):
    """Small key/value store so the collector resumes schedules after a restart."""
    __tablename__ = "collector_state"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict[str, Any]]
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
