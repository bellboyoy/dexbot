"""Paper-trading runner. Simulated fills only.

    python -m app.paper --once
    python -m app.paper
"""
from __future__ import annotations

import argparse
import asyncio
from decimal import Decimal

from app.config import TradingMode, get_settings
from app.logging import configure_logging
from app.paper.runner import run_loop, run_once


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="DexBot paper runner. Does not sign, approve, or submit transactions.")
    parser.add_argument("--once", action="store_true", help="run a single scan and exit")
    parser.add_argument("--interval", type=float, default=None, help="seconds between scans (default from settings)")
    parser.add_argument("--balance", default=None, help="starting USD balance, used only when the account is created")
    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level, False)
    if settings.trading_mode is not TradingMode.PAPER:
        raise SystemExit("refusing to start: trading mode is not paper")
    balance = Decimal(args.balance) if args.balance is not None else None
    print("DexBot paper runner. Simulated balance only. Live execution is not implemented.")
    if args.once:
        async def _bounded() -> None:
            await asyncio.wait_for(run_once(balance=balance), timeout=settings.cycle_deadline_s)

        try:
            asyncio.run(_bounded())
        except TimeoutError:
            raise SystemExit(1) from None
        return
    interval = args.interval if args.interval is not None else settings.paper_scan_interval_s
    try:
        asyncio.run(run_loop(interval, balance))
    except KeyboardInterrupt:
        print("stopped")


if __name__ == "__main__":
    main()
