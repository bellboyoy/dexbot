"""SQLAlchemy models for the PostgreSQL schema (see docs/ARCHITECTURE.md §5)."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger, Boolean, Date, DateTime, Enum, ForeignKey, Index, Integer, Numeric, String, Text,
    UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.domain.chains import Chain

USD = Numeric(38, 18)
RAW = Numeric(78, 0)          # fits uint256
FEE_RATE = Numeric(12, 8)
CHAIN = Enum(Chain, name="chain", values_callable=lambda e: [m.value for m in e])


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONB, datetime: DateTime(timezone=True)}


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
    address: Mapped[str] = mapped_column(String(80))
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
    # Paper ledger. Raw on-chain amounts stay null; token_qty is the simulated quantity.
    token_qty: Mapped[Decimal | None] = mapped_column(USD)
    mark_price_usd: Mapped[Decimal | None] = mapped_column(USD)
    unrealized_pnl_usd: Mapped[Decimal | None] = mapped_column(USD)
    entry_liquidity_usd: Mapped[Decimal | None] = mapped_column(USD)
    fee_rate: Mapped[Decimal | None] = mapped_column(FEE_RATE)


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
    slippage_usd: Mapped[Decimal | None] = mapped_column(USD)
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


class LaunchWatch(Timestamps, Base):
    """A newly seen pool followed until about 24 hours after launch. No execution."""
    __tablename__ = "launch_watches"
    __table_args__ = (
        UniqueConstraint("chain", "pool_address"),
        Index("ix_launch_watches_due", "status", "next_due_at"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    chain: Mapped[Chain] = mapped_column(CHAIN)
    pool_address: Mapped[str] = mapped_column(String(80))
    token_address: Mapped[str] = mapped_column(String(64))
    quote_address: Mapped[str | None] = mapped_column(String(64))
    symbol: Mapped[str | None] = mapped_column(String(64))
    dex_id: Mapped[str | None] = mapped_column(String(64))
    pool_created_at: Mapped[datetime | None]
    first_seen_at: Mapped[datetime]
    launch_at: Mapped[datetime]
    status: Mapped[str] = mapped_column(String(16))
    next_due_at: Mapped[datetime | None]
    snapshots_taken: Mapped[int] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(Text)


class LaunchSnapshot(Base):
    """One scheduled observation. A missed slot is stored with null market fields, not invented numbers."""
    __tablename__ = "launch_snapshots"
    __table_args__ = (
        UniqueConstraint("watch_id", "age_minutes"),
        Index("ix_launch_snapshots_watch", "watch_id"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    watch_id: Mapped[int] = mapped_column(ForeignKey("launch_watches.id"))
    chain: Mapped[Chain] = mapped_column(CHAIN)
    age_minutes: Mapped[int] = mapped_column(Integer)
    due_at: Mapped[datetime]
    observed_at: Mapped[datetime | None]
    missed: Mapped[bool] = mapped_column(Boolean)
    not_found: Mapped[bool] = mapped_column(Boolean)
    source: Mapped[str | None] = mapped_column(String(32))
    price_usd: Mapped[Decimal | None] = mapped_column(USD)
    liquidity_usd: Mapped[Decimal | None] = mapped_column(USD)
    fdv_usd: Mapped[Decimal | None] = mapped_column(USD)
    market_cap_usd: Mapped[Decimal | None] = mapped_column(USD)
    base_reserve: Mapped[Decimal | None] = mapped_column(USD)
    quote_reserve: Mapped[Decimal | None] = mapped_column(USD)
    volume_m5: Mapped[Decimal | None] = mapped_column(USD)
    volume_h1: Mapped[Decimal | None] = mapped_column(USD)
    volume_h24: Mapped[Decimal | None] = mapped_column(USD)
    buys_m5: Mapped[int | None] = mapped_column(Integer)
    sells_m5: Mapped[int | None] = mapped_column(Integer)
    buys_h1: Mapped[int | None] = mapped_column(Integer)
    sells_h1: Mapped[int | None] = mapped_column(Integer)
    buyers_m5: Mapped[int | None] = mapped_column(Integer)
    sellers_m5: Mapped[int | None] = mapped_column(Integer)
    buyers_h1: Mapped[int | None] = mapped_column(Integer)
    sellers_h1: Mapped[int | None] = mapped_column(Integer)
    screen_allowed: Mapped[bool | None] = mapped_column(Boolean)
    screen_reason: Mapped[str | None] = mapped_column(Text)
    screen_checks: Mapped[dict[str, Any] | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ProcessHeartbeat(Base):
    """Last successful cycle of a long-running paper process. No secrets."""
    __tablename__ = "process_heartbeats"
    component: Mapped[str] = mapped_column(String(32), primary_key=True)
    last_success_at: Mapped[datetime | None]
    last_attempt_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class PaperAccount(Timestamps, Base):
    """One simulated cash account. `mode` is written as paper and the runner refuses anything else."""
    __tablename__ = "paper_accounts"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    mode: Mapped[str] = mapped_column(String(8))
    starting_balance_usd: Mapped[Decimal] = mapped_column(USD)
    cash_usd: Mapped[Decimal] = mapped_column(USD)
    realized_pnl_usd: Mapped[Decimal] = mapped_column(USD)
    fees_usd: Mapped[Decimal] = mapped_column(USD)
    gas_usd: Mapped[Decimal] = mapped_column(USD)
    slippage_usd: Mapped[Decimal] = mapped_column(USD)
    day_utc: Mapped[date] = mapped_column(Date)
    day_realized_pnl_usd: Mapped[Decimal] = mapped_column(USD)
    consecutive_losses: Mapped[int] = mapped_column(Integer)
    cooldown_until: Mapped[datetime | None]
    kill_switch: Mapped[bool] = mapped_column(Boolean)
    kill_reason: Mapped[str | None] = mapped_column(Text)
    last_scan_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)
    scan_count: Mapped[int] = mapped_column(Integer)
    last_cycle: Mapped[dict[str, Any] | None]


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
