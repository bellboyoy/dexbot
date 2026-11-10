"""Fetch real GeckoTerminal OHLCV and report each strategy after costs.

Usage (from backend/):
    python -m app.backtest
    python -m app.backtest --per-chain 3 --limit 1000
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from app.backtest.engine import PoolSeries, run_backtest
from app.backtest.metrics import StrategyReport, report
from app.config import get_settings
from app.costs import fee_rate_for_snapshot
from app.domain.chains import Chain
from app.domain.market import PoolSnapshot, Timeframe
from app.logging import configure_logging, get_logger
from app.marketdata.aggregator import build_market_data_service
from app.marketdata.errors import ProviderError
from app.screening.filters import is_gas_token, is_stable, quote_allowed
from app.strategy.registry import build_strategies
from app.strategy.validation import (
    MAX_DRAWDOWN_FRACTION, MIN_TRADES, SAMPLE_NOTE, dump_validation, validation_path,
)

log = get_logger(__name__)
MIN_LIQUIDITY = Decimal("30000")
MIN_BARS = 80


def _jsonable(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _report_dict(row: StrategyReport) -> dict:
    return _jsonable({
        "strategy": row.strategy,
        "trades": row.trades,
        "wins": row.wins,
        "losses": row.losses,
        "breakeven": row.breakeven,
        "win_rate": row.win_rate,
        "avg_win": row.avg_win,
        "avg_loss": row.avg_loss,
        "risk_reward": row.risk_reward,
        "profit_factor": row.profit_factor,
        "expectancy": row.expectancy,
        "max_drawdown_usd": row.max_drawdown_usd,
        "max_drawdown_pct": row.max_drawdown_pct,
        "fees": row.fees,
        "gas": row.gas,
        "slippage": row.slippage,
        "total_costs": row.total_costs,
        "net_pnl": row.net_pnl,
        "ending_equity": row.ending_equity,
        "end_of_data_exits": row.end_of_data_exits,
        "passed": row.passed,
        "reason": row.reason,
        "risk_adjusted_score": row.score,
    })


def _eligible(snap: PoolSnapshot) -> bool:
    quote = snap.quote_token.address if snap.quote_token else None
    if quote is None or not quote_allowed(snap.chain, quote):
        return False
    if is_stable(snap.chain, snap.base_token.address) or is_gas_token(snap.chain, snap.base_token.address):
        return False
    if snap.liquidity_usd is None or snap.liquidity_usd < MIN_LIQUIDITY:
        return False
    if snap.price_usd is None or snap.price_usd <= 0:
        return False
    return True


async def _load(per_chain: int, limit: int, chains: list[Chain]) -> tuple[list[PoolSeries], list[dict]]:
    settings = get_settings()
    service = build_market_data_service(settings)
    notes: list[dict] = []
    series: list[PoolSeries] = []
    try:
        for chain in chains:
            try:
                snaps = await service.get_trending_pools(chain)
            except ProviderError as exc:
                notes.append({"chain": chain.value, "error": str(exc)})
                log.warning("trending_failed", chain=chain.value, error=str(exc))
                continue
            picked = [s for s in snaps if _eligible(s)]
            picked.sort(key=lambda s: s.liquidity_usd or Decimal(0), reverse=True)
            for snap in picked[:per_chain]:
                try:
                    candles = await service.get_ohlcv(chain, snap.pool_address, Timeframe.M5, limit)
                except ProviderError as exc:
                    notes.append({"chain": chain.value, "pool": snap.pool_address, "error": str(exc)})
                    continue
                meta = {
                    "chain": chain.value, "pool": snap.pool_address, "symbol": snap.base_token.symbol,
                    "liquidity_usd": format(snap.liquidity_usd or 0, "f"),
                    "fee_rate": format(fee_rate_for_snapshot(snap), "f"),
                    "bars": len(candles),
                    "first": candles[0].open_time.isoformat() if candles else None,
                    "last": candles[-1].open_time.isoformat() if candles else None,
                }
                if len(candles) < MIN_BARS:
                    meta["skipped"] = f"only {len(candles)} closed bars"
                    notes.append(meta)
                    continue
                notes.append(meta)
                series.append(PoolSeries(
                    chain=chain, pool_address=snap.pool_address, token_address=snap.base_token.address,
                    symbol=snap.base_token.symbol, liquidity_usd=snap.liquidity_usd,  # type: ignore[arg-type]
                    fee_rate=fee_rate_for_snapshot(snap), candles=list(candles),
                ))
                print(f"loaded {chain.value} {snap.base_token.symbol} {snap.pool_address} bars={len(candles)}", flush=True)
    finally:
        await service.aclose()
    return series, notes


def _print_table(rows: list[StrategyReport]) -> None:
    header = f"{'strategy':<16} {'trades':>6} {'win%':>7} {'avgW':>10} {'avgL':>10} {'R/R':>7} {'maxDD':>10} {'costs':>10} {'net':>10} {'pass'}"
    print(header)
    print("-" * len(header))
    for row in rows:
        def n(value: Decimal | None) -> str:
            return "" if value is None else f"{value:.4f}"
        win = "" if row.win_rate is None else f"{row.win_rate * 100:.1f}"
        print(f"{row.strategy:<16} {row.trades:>6} {win:>7} {n(row.avg_win):>10} {n(row.avg_loss):>10} "
              f"{n(row.risk_reward):>7} {n(row.max_drawdown_usd):>10} {n(row.total_costs):>10} "
              f"{n(row.net_pnl):>10} {'yes' if row.passed else 'no'}")
        print(f"  {row.reason}")


async def _run(args: argparse.Namespace) -> dict:
    settings = get_settings()
    chains = [Chain(c.strip()) for c in args.chains.split(",") if c.strip()] if args.chains else list(settings.enabled_chains)
    series, notes = await _load(args.per_chain, args.limit, chains)
    rows: list[StrategyReport] = []
    if series:
        for strategy in build_strategies():
            print(f"running {strategy.name}", flush=True)
            result = run_backtest(strategy, series)
            rows.append(report(strategy.name, result.trades, result.equity, Decimal("1000")))
    rows.sort(key=lambda row: row.score, reverse=True)
    _print_table(rows)
    passed = [row.strategy for row in rows if row.passed]
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "timeframe": "5m",
        "starting_balance_usd": "1000",
        "position_usd": "50",
        "min_trades": MIN_TRADES,
        "max_drawdown_fraction": format(MAX_DRAWDOWN_FRACTION, "f"),
        "note": SAMPLE_NOTE,
        "sample": notes,
        "pools_used": len(series),
        "passed": passed,
        "rejected": {row.strategy: row.reason for row in rows if not row.passed},
        "ranking": [_report_dict(row) for row in rows],
    }
    if not series:
        payload["passed"] = []
        payload["rejected"] = {"_sample": "no pool returned enough closed bars"}
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n")
    dump_validation({
        "generated_at": payload["generated_at"],
        "passed": payload["passed"],
        "rejected": payload["rejected"],
        "note": SAMPLE_NOTE,
        "source": str(out),
    })
    print(f"wrote {out}")
    print(f"wrote {validation_path()}")
    print("passed:", ", ".join(passed) if passed else "(none)")
    return payload


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Backtest paper strategies on live GeckoTerminal OHLCV. Does not trade.")
    parser.add_argument("--per-chain", type=int, default=3)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--chains", default="")
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)
    if not args.out:
        args.out = str(validation_path().parent / "backtest-results.json")
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
