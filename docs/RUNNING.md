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

## How much data

Default cap: 150 new pools per day, 200 watched at once. Each pool has 26 scheduled snapshots.

150 × 26 × about 1.2 KB ≈ **5 MB/day of rows, about 10 MB/day including indexes**. A year at that cap is a few gigabytes, not something the 2 GB of free RAM has to hold. The process only loads the handful of watches that are due.

## Later, when weeks of snapshots exist

```bash
.venv/bin/python -m app.research.pit
```

Until there are 14 days of snapshots and 30 pools with at least 8 real observations, this only prints the inventory. It does not change which strategies the paper runner may open. With `--promote`, a strategy that then passes the same out-of-sample gate can be written into `docs/strategy_validation.json`. That is still paper trading.
