"""Pre-registered bar for keeping a strategy. Written before the sample was fetched.

A pass requires all of:
- at least MIN_TRADES closed trades
- net P&L after fees, gas and slippage > 0
- expectancy (net / trades) > 0
- profit factor > 1 when both wins and losses exist
- max drawdown under MAX_DRAWDOWN_FRACTION of starting equity

Win rate is not a criterion. Ranking among passes is net P&L / max(drawdown, $1).
Failing the trade-count bar is "inconclusive", not a positive result.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

MIN_TRADES = 20
MAX_DRAWDOWN_FRACTION = Decimal("0.25")

SAMPLE_NOTE = (
    "The sample is a handful of pools that were trending or new when the backtest ran, "
    "with at most 1000 five-minute bars (about 3.5 days) from GeckoTerminal. Pools that "
    "already died are absent, so this is survivorship-biased. Liquidity is today's snapshot "
    "applied to every historical bar; past slippage is not reconstructed. Swap fees come from "
    "the pool when it publishes one, otherwise 30 bps per side, plus a fixed per-chain gas "
    "estimate. Parameters were set before this run and were not searched. A pass is positive "
    "expected value after costs on this sample only. It is not evidence of a durable edge."
)


@dataclass(frozen=True, slots=True)
class GateResult:
    passed: bool
    reason: str


def gate(trades: int, net_pnl: Decimal, expectancy: Decimal | None, profit_factor: Decimal | None,
         max_drawdown_pct: Decimal, min_trades: int = MIN_TRADES) -> GateResult:
    if trades < min_trades:
        return GateResult(False, f"inconclusive: {trades} trades is below {min_trades}")
    if net_pnl <= 0:
        return GateResult(False, "net P&L after costs is not positive")
    if expectancy is None or expectancy <= 0:
        return GateResult(False, "expectancy is not positive")
    if profit_factor is not None and profit_factor <= 1:
        return GateResult(False, "profit factor is not above 1")
    if max_drawdown_pct >= MAX_DRAWDOWN_FRACTION:
        return GateResult(False, f"max drawdown {max_drawdown_pct} exceeds {MAX_DRAWDOWN_FRACTION}")
    return GateResult(True, "positive expectancy after costs inside the drawdown limit")


def risk_adjusted_score(net_pnl: Decimal, max_drawdown_usd: Decimal) -> Decimal:
    return net_pnl / max(max_drawdown_usd, Decimal(1))


def validation_path() -> Path:
    override = os.environ.get("DEXBOT_VALIDATION_PATH")
    if override:
        return Path(override)
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "docs" / "ARCHITECTURE.md").exists():
            return parent / "docs" / "strategy_validation.json"
    return here.parents[3] / "docs" / "strategy_validation.json"


@dataclass(frozen=True, slots=True)
class ValidationFile:
    passed: tuple[str, ...]
    rejected: dict[str, str]
    note: str
    generated_at: str | None
    configs: dict = field(default_factory=dict)
    backtest: dict = field(default_factory=dict)
    timeframe: str = "5m"


def load_validation(path: Path | None = None) -> ValidationFile:
    file = path or validation_path()
    if not file.exists():
        return ValidationFile((), {}, "no validation file; paper entries are disabled", None)
    try:
        data = json.loads(file.read_text())
    except (OSError, json.JSONDecodeError):
        return ValidationFile((), {}, "validation file is unreadable; paper entries are disabled", None)
    passed = tuple(str(name) for name in data.get("passed") or [])
    rejected = {str(k): str(v) for k, v in (data.get("rejected") or {}).items()}
    configs = data.get("configs") if isinstance(data.get("configs"), dict) else {}
    backtest = data.get("backtest") if isinstance(data.get("backtest"), dict) else {}
    return ValidationFile(
        passed, rejected, str(data.get("note") or SAMPLE_NOTE), data.get("generated_at"),
        configs=configs, backtest=backtest, timeframe=str(data.get("timeframe") or "5m"),
    )


def dump_validation(payload: dict, path: Path | None = None) -> Path:
    file = path or validation_path()
    file.parent.mkdir(parents=True, exist_ok=True)
    payload.setdefault("generated_at", datetime.now(timezone.utc).isoformat())
    payload.setdefault("note", SAMPLE_NOTE)
    payload.setdefault("min_trades", MIN_TRADES)
    payload.setdefault("max_drawdown_fraction", format(MAX_DRAWDOWN_FRACTION, "f"))
    file.write_text(json.dumps(payload, indent=2) + "\n")
    return file
