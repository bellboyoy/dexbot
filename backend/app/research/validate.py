"""Select parameters on the train set only, then gate promotion on out-of-sample results.

Promotion requires all of:
- time holdout (last 30% of bars on train pools): the existing cost gate, at least 20 trades
- pool holdout (pools never seen in training): the same gate with at least 12 trades
- walk-forward on the train universe with the frozen parameters: at least 2 of 3 test
  windows have positive expectancy and at least 5 trades, and the mean of those is positive
- at least one neighbouring configuration also has positive time-holdout expectancy

A neighbour that looks better out of sample is not promoted in its place. The search
cannot see the test pools or the test window.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from app.backtest.engine import PoolSeries, run_backtest
from app.backtest.metrics import report
from app.domain.chains import Chain
from app.domain.market import Candle, Timeframe
from app.research.cache import read_json
from app.research.grid import Config, build_grid, is_neighbor
from app.research.splits import split_pools, split_time, walk_forward_windows
from app.strategy.validation import MIN_TRADES, SAMPLE_NOTE, dump_validation, validation_path

POOL_OOS_MIN_TRADES = 12
WF_MIN_TRADES = 5
MIN_BARS = 40
RESEARCH_NOTE = (
    "Parameters were chosen only on the train pools and the earlier 70% of their bars. "
    "Promotion uses the later 30% of those pools, a held-out set of pools, a 3-window "
    "walk-forward of the frozen parameters, and a check that a neighbouring configuration "
    "also has positive out-of-sample expectancy. GeckoTerminal does not publish historical "
    "pool reserves or a historical new-pool archive on the keyless API (pages past 10 return "
    "401; new_pools is only the last few minutes, checked 2026-11-09). Liquidity in the "
    "primary result is the spot snapshot. The thin cohort is low spot liquidity with age of "
    "at least a few hours, which is the closest free sample of failed launches, not a "
    "point-in-time listing from months ago. 5-minute and 1-minute results are reported for "
    "the frozen 15-minute champion and are not used to pick it. The number of configurations "
    "tried is the size of the pre-registered grid."
)


def _dec(value) -> Decimal:
    return Decimal(str(value))


def _candles(chain: str, address: str, timeframe: Timeframe) -> list[Candle]:
    rows = read_json(f"candles/{chain}/{address}/{timeframe.value}.json") or []
    out = []
    for row in rows:
        out.append(Candle(
            datetime.fromisoformat(row[0]), timeframe, _dec(row[1]), _dec(row[2]), _dec(row[3]),
            _dec(row[4]), _dec(row[5]), bool(row[6]),
        ))
    return [c for c in out if c.is_closed and min(c.open, c.high, c.low, c.close) > 0]


def load_timeframe(timeframe: Timeframe) -> tuple[list[PoolSeries], dict[Chain, list[Candle]]]:
    manifest = read_json("manifest.json")
    if not manifest:
        raise SystemExit("no research cache; run python -m app.research.collect first")
    pools: list[PoolSeries] = []
    for row in manifest.get("pools") or []:
        candles = _candles(row["chain"], row["address"], timeframe)
        if len(candles) < MIN_BARS:
            continue
        liquidity = row.get("liquidity_usd") or row.get("list_liquidity_usd")
        if liquidity in (None, ""):
            continue
        if _dec(liquidity) <= 0:
            continue
        created = datetime.fromisoformat(row["created"]) if row.get("created") else None
        pools.append(PoolSeries(
            chain=Chain(row["chain"]), pool_address=row["address"], token_address=row.get("token") or row["address"],
            symbol=row.get("symbol"), liquidity_usd=_dec(liquidity), fee_rate=_dec(row.get("fee_rate") or "0.003"),
            candles=candles, liquidity_source="spot_snapshot", cohort=row.get("cohort") or "",
            pool_created_at=created,
        ))
    benchmarks: dict[Chain, list[Candle]] = {}
    for row in manifest.get("benchmarks") or []:
        candles = _candles(row["chain"], row["address"], Timeframe.M15 if timeframe is not Timeframe.M15 else timeframe)
        if timeframe is not Timeframe.M15:
            candles = _candles(row["chain"], row["address"], Timeframe.M15)
        if candles:
            benchmarks[Chain(row["chain"])] = candles
    return pools, benchmarks


def _public(rep) -> dict:
    def num(value: Decimal | None):
        return None if value is None else format(value, "f")
    return {
        "trades": rep.trades,
        "wins": rep.wins,
        "losses": rep.losses,
        "win_rate": num(rep.win_rate),
        "avg_win": num(rep.avg_win),
        "avg_loss": num(rep.avg_loss),
        "risk_reward": num(rep.risk_reward),
        "profit_factor": num(rep.profit_factor),
        "expectancy": num(rep.expectancy),
        "max_drawdown_usd": num(rep.max_drawdown_usd),
        "max_drawdown_pct": num(rep.max_drawdown_pct),
        "total_costs": num(rep.total_costs),
        "net_pnl": num(rep.net_pnl),
        "passed_gate": rep.passed,
        "reason": rep.reason,
    }


def _run(cfg: Config, pools: list[PoolSeries], benchmarks) -> dict:
    if not pools:
        empty = report(cfg.family, [], [Decimal("1000")], Decimal("1000"))
        return _public(empty)
    result = run_backtest(cfg.strategy(), pools, limits=cfg.limits(), benchmarks=benchmarks)
    return _public(report(cfg.family, result.trades, result.equity, Decimal("1000")))


def _exp(row: dict) -> Decimal:
    return Decimal(row["expectancy"]) if row.get("expectancy") not in (None, "") else Decimal(0)


def select_on_train(configs: list[Config], train: list[PoolSeries], benchmarks, evaluate=_run) -> tuple[Config | None, list[dict]]:
    """Highest train expectancy among configs with enough train trades. Test data is not passed in."""
    scored = []
    best: tuple[Decimal, Config] | None = None
    for cfg in configs:
        row = evaluate(cfg, train, benchmarks)
        row["label"] = cfg.label
        scored.append(row)
        if row["trades"] >= MIN_TRADES and (best is None or _exp(row) > best[0]):
            best = (_exp(row), cfg)
    return (best[1] if best else None), scored


def _positive_window(row: dict) -> bool:
    return row["trades"] >= WF_MIN_TRADES and _exp(row) > 0 and Decimal(row["net_pnl"] or "0") > 0


def promote(time_row: dict, pool_row: dict, folds: list[dict], neighbor_rows: list[dict]) -> tuple[bool, str]:
    if not time_row.get("passed_gate"):
        return False, f"time holdout: {time_row.get('reason')}"
    if pool_row["trades"] < POOL_OOS_MIN_TRADES:
        return False, f"pool holdout inconclusive: {pool_row['trades']} trades is below {POOL_OOS_MIN_TRADES}"
    if _exp(pool_row) <= 0 or Decimal(pool_row["net_pnl"] or "0") <= 0:
        return False, "pool holdout expectancy is not positive"
    if Decimal(pool_row["max_drawdown_pct"] or "1") >= Decimal("0.25"):
        return False, "pool holdout drawdown exceeds 25%"
    good = [row for row in folds if _positive_window(row)]
    if len(good) < 2:
        return False, f"walk-forward: {len(good)} of {len(folds)} windows were positive with enough trades"
    if sum((_exp(row) for row in good), Decimal(0)) / Decimal(len(good)) <= 0:
        return False, "walk-forward mean expectancy is not positive"
    if not any(_positive_window(row) for row in neighbor_rows):
        return False, "no neighbouring configuration has positive out-of-sample expectancy"
    return True, "passed time holdout, pool holdout, walk-forward and a neighbour check"


def haircut(pools: list[PoolSeries], factor: Decimal = Decimal("0.5")) -> list[PoolSeries]:
    """Sensitivity only: pretend every bar had half the spot liquidity. Not a historical reserve."""
    from dataclasses import replace
    return [replace(pool, liquidity_usd=pool.liquidity_usd * factor, liquidity_source="spot_halved_sensitivity") for pool in pools]


def run_study() -> dict:
    grid = build_grid()
    pools, benchmarks = load_timeframe(Timeframe.M15)
    if len(pools) < 4:
        raise SystemExit(f"only {len(pools)} pools have at least {MIN_BARS} 15m bars; not enough to split")
    train_pools, holdout_pools = split_pools(pools)
    train, time_oos, cut = split_time(train_pools)
    print(f"15m pools={len(pools)} train_pools={len(train_pools)} holdout_pools={len(holdout_pools)} "
          f"configs={len(grid)} cut={cut}", flush=True)
    families: dict[str, list[Config]] = {}
    for cfg in grid:
        families.setdefault(cfg.family, []).append(cfg)
    rows = []
    passed = []
    rejected = {}
    configs = {}
    backtest = {}
    for family, configs_in_family in families.items():
        print(f"train search {family} ({len(configs_in_family)})", flush=True)
        champ, train_rows = select_on_train(configs_in_family, train, benchmarks)
        train_by_label = {row["label"]: row for row in train_rows}
        if champ is None:
            rejected[family] = "no configuration had 20 train trades"
            rows.append({"family": family, "label": None, "passed": False, "reason": rejected[family]})
            continue
        train_row = train_by_label[champ.label]
        print(f"  champion {champ.label} train net={train_row['net_pnl']} trades={train_row['trades']}", flush=True)
        time_row = _run(champ, time_oos, benchmarks)
        pool_row = _run(champ, holdout_pools, benchmarks)
        folds = []
        # Windows are cut from the train period only, so they do not reuse the time holdout.
        for _fold_train, fold_test in walk_forward_windows(train):
            folds.append(_run(champ, fold_test, benchmarks))
        neighbors = [other for other in configs_in_family if is_neighbor(champ, other)]
        neighbor_rows = [_run(other, time_oos, benchmarks) for other in neighbors]
        ok, reason = promote(time_row, pool_row, folds, neighbor_rows)
        # Train must itself be non-negative. A champion chosen as the least-bad loser does not graduate.
        if ok and _exp(train_row) <= 0:
            ok, reason = False, "train expectancy is not positive"
        halved = _run(champ, haircut(time_oos), benchmarks)
        five = _secondary(champ, Timeframe.M5, benchmarks)
        one = _secondary(champ, Timeframe.M1, benchmarks)
        record = {
            "family": family,
            "label": champ.label,
            "params": champ.params,
            "size": format(champ.size, "f"),
            "slippage_bps": champ.slippage_bps,
            "passed": ok,
            "reason": reason,
            "train": train_row,
            "time_oos": time_row,
            "pool_oos": pool_row,
            "walk_forward": folds,
            "neighbors": [{"label": cfg.label, **row} for cfg, row in zip(neighbors, neighbor_rows)],
            "liquidity_haircut_time_oos": halved,
            "timeframe_5m": five,
            "timeframe_1m": one,
        }
        rows.append(record)
        backtest[family] = {
            "label": champ.label,
            "oos_expectancy": time_row.get("expectancy"),
            "oos_net_pnl": time_row.get("net_pnl"),
            "oos_trades": time_row.get("trades"),
            "oos_max_drawdown_pct": time_row.get("max_drawdown_pct"),
            "train_expectancy": train_row.get("expectancy"),
            "train_trades": train_row.get("trades"),
            "pool_oos_expectancy": pool_row.get("expectancy"),
            "pool_oos_trades": pool_row.get("trades"),
            "passed": ok,
            "reason": reason,
        }
        if ok:
            passed.append(family)
            configs[family] = champ.params
        else:
            rejected[family] = reason
        print(f"  {family} {'PASS' if ok else 'FAIL'}: {reason}", flush=True)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "timeframe": "15m",
        "configs_tried": len(grid),
        "pools": len(pools),
        "train_pools": len(train_pools),
        "holdout_pools": len(holdout_pools),
        "liquidity_source": "spot_snapshot",
        "note": RESEARCH_NOTE,
        "passed": passed,
        "rejected": rejected,
        "rows": rows,
        "cohorts": _cohorts(pools),
    }
    out = validation_path().parent / "research-results.json"
    out.write_text(json.dumps(payload, indent=2) + "\n")
    dump_validation({
        "generated_at": payload["generated_at"],
        "passed": passed,
        "rejected": rejected,
        "configs": configs,
        "backtest": backtest,
        "timeframe": "15m",
        "configs_tried": len(grid),
        "note": RESEARCH_NOTE,
        "source": str(out),
        "min_trades": MIN_TRADES,
    })
    _print(rows, len(grid))
    print(f"wrote {out}")
    print("passed:", ", ".join(passed) if passed else "(none)")
    return payload


def _secondary(cfg: Config, timeframe: Timeframe, benchmarks) -> dict:
    try:
        pools, _ = load_timeframe(timeframe)
    except SystemExit:
        return {"trades": 0, "reason": "no cache"}
    # Same pool addresses as the 15m study is not re-filtered here beyond the bar minimum.
    return _run(cfg, pools, benchmarks)


def _cohorts(pools: list[PoolSeries]) -> dict:
    counts: dict[str, int] = {}
    for pool in pools:
        counts[pool.cohort or "unknown"] = counts.get(pool.cohort or "unknown", 0) + 1
    return counts


def _print(rows: list[dict], n_configs: int) -> None:
    print(f"\nconfigurations tried: {n_configs}")
    header = f"{'family':<16} {'label':<18} {'trN':>5} {'trNet':>10} {'oosN':>5} {'oosNet':>10} {'oosExp':>10} {'poolN':>5} {'poolNet':>10} pass"
    print(header)
    for row in rows:
        if not row.get("label"):
            print(f"{row['family']:<16} {'—':<18} {row.get('reason', '')}")
            continue
        train, oos, pool = row["train"], row["time_oos"], row["pool_oos"]
        print(f"{row['family']:<16} {row['label']:<18} {train['trades']:>5} {_m(train['net_pnl']):>10} "
              f"{oos['trades']:>5} {_m(oos['net_pnl']):>10} {_m(oos['expectancy']):>10} "
              f"{pool['trades']:>5} {_m(pool['net_pnl']):>10} {'yes' if row['passed'] else 'no'}")
        print(f"  {row['reason']}")


def _m(value) -> str:
    if value in (None, ""):
        return ""
    return f"{Decimal(value):.4f}"


def main() -> None:
    run_study()


if __name__ == "__main__":
    main()
