# Running DexBot on a small box

Paper only. Nothing in these processes signs, approves, or submits a transaction. Postgres and Redis listen on 127.0.0.1. The box this was sized for has about 2 GB free: the collector does not keep candles in memory, and the default cap is 150 new pools per day.

GeckoTerminal's keyless limit is about 5 calls per minute **per IP**, shared by every process. The launch collector uses DexScreener for bulk reads and makes at most one GeckoTerminal call every 3 minutes. Leave the rest for the paper runner. Do not also run `python -m app.research.collect` while both are up.

## Processes

From `backend/`, with the virtualenv and `DEXBOT_DATABASE_URL` set:

```bash
.venv/bin/alembic upgrade head

# terminal 1 — API and the PAPER dashboard
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000

# terminal 2 — simulated trading. Opens a position only when strategy_validation.json says so.
.venv/bin/python -m app.paper

# terminal 3 — point-in-time launch recorder
.venv/bin/python -m app.collector
```

One cycle, then exit:

```bash
.venv/bin/python -m app.collector --once
.venv/bin/python -m app.paper --once
```

The collector follows Solana and Base pools first seen within 30 minutes of launch. It records liquidity, reserves when the provider sends them, price, volume, buy and sell counts, unique buyers when a GeckoTerminal slot is free, FDV, and a safety screen. Slots are 1, 5, 15, 30, 60 and 240 minutes after launch, then hourly through 24 hours. A slot that was already too late is stored as missed, with no invented price. A pool that disappears is stored as not found, which is how a dead launch stays in the sample.

Uniswap v4 and PancakeSwap Infinity pool ids are 32 bytes. Those are accepted as pool identifiers. Token addresses stay 20 bytes. If GoPlus, GeckoTerminal, or DexScreener cannot query one, the cycle counts it as `unsupported_pool_id` and does not call that an invalid address.

Every HTTP request and database call has a timeout, and each cycle has a deadline (120 seconds). `/health` and `/api/paper/summary` include `heartbeats`: the last successful paper-runner and collector cycle. After the machine wakes from a long suspend, the collector treats the wall-clock gap as a gap. Slots that are past their tolerance are written as missed. They are not backfilled with the post-wake price. If the collector hits its cycle deadline twice in a row it exits with status 1 so a watchdog can restart it. A usable research pool needs at least 8 slots that were actually observed (a price or a not-found) and those slots have to cover at least half of the slots that should have happened by now. Missed slots lower that coverage. The 14-day and 30-pool gate is unchanged.

## How much data

Default cap: 150 new pools per day, 200 watched at once. Each pool has 26 scheduled snapshots.

150 × 26 × about 1.2 KB ≈ **5 MB/day of rows, about 10 MB/day including indexes**. A year at that cap is a few gigabytes, not something the 2 GB of free RAM has to hold. The process only loads the handful of watches that are due.

## Later, when weeks of snapshots exist

```bash
.venv/bin/python -m app.research.pit
```

Until there are 14 days of snapshots and 30 pools with enough observed coverage, this only prints the inventory, including how many elapsed slots were actually seen. It does not change which strategies the paper runner may open. With `--promote`, a strategy that then passes the same out-of-sample gate can be written into `docs/strategy_validation.json`. That is still paper trading.

A watchdog can restart a collector that exits 1. Example, if `runit` or `systemd` is not already in front of the process: a shell loop that runs `python -m app.collector` and starts it again when the status is 1. Do not restart it in a tight loop with no pause; the process itself sleeps between cycles.
