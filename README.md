# DexBot

Multi-chain (Solana, Base, BSC; Arbitrum later) short-term DEX trading system. **Paper trading only.** Live trading and wallet execution are not implemented, and the trading mode cannot be changed through configuration.

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): design, credentials, provider limits, schema and phase plan
- [docs/PROGRESS.md](docs/PROGRESS.md): what is done, what was verified, and known issues
- [docs/AUDIT.md](docs/AUDIT.md): audit of the Phase 1 layer and the paper stack
- [docs/backtest-results.json](docs/backtest-results.json): latest backtest, written by the command below
- [docs/strategy_validation.json](docs/strategy_validation.json): which strategies the paper runner is allowed to open

## Requirements

- Docker with Compose v2 (for the full stack)
- Python 3.12+ (for local development and tests)

No API keys are needed for Phase 1. `.env` is git-ignored; never commit it.

## Run the stack

```bash
cp .env.example .env
docker compose up -d --build
docker compose ps            # postgres, redis, backend healthy; migrate "exited (0)"
curl localhost:8000/health
curl "localhost:8000/market/solana/pools/<pool-address>?enrich=true"
curl "localhost:8000/market/solana/pools/<pool-address>/ohlcv?timeframe=5m&limit=50"
curl localhost:8000/market/solana/new-pools
```

The `migrate` service runs `alembic upgrade head` against a fresh database and exits, then `backend` starts. All ports bind to 127.0.0.1 only.

If you build behind a TLS-intercepting proxy, pass its CA certificate as a build secret:
`docker build --secret id=extra_ca,src=/path/to/ca.pem -t dexbot-backend backend`.

## Tests

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# 1. Unit tests: offline, use responses captured from the real APIs
.venv/bin/pytest

# 2. PostgreSQL tests: schema round-trips and Alembic migrations on a clean database
docker compose up -d postgres          # from the repo root
docker compose exec postgres createdb -U dexbot dexbot_test
DEXBOT_TEST_DATABASE_URL=postgresql+asyncpg://dexbot:dexbot@localhost:5432/dexbot_test .venv/bin/pytest -m postgres

# 3. Live smoke test against DexScreener and GeckoTerminal (1–2 minutes,
#    because keyless GeckoTerminal allows only a few calls per minute)
.venv/bin/pytest -m live
```

## Database migrations

```bash
cd backend
export DEXBOT_DATABASE_URL=postgresql+asyncpg://dexbot:dexbot@localhost:5432/dexbot
.venv/bin/alembic upgrade head                       # apply
.venv/bin/alembic check                              # models and migrations agree
.venv/bin/alembic revision --autogenerate -m "..."   # after changing app/db/models.py
```

The database URL always comes from `DEXBOT_DATABASE_URL`; `alembic.ini` holds no credentials.

## Run without Docker

Postgres and Redis listen on 127.0.0.1. The API does not use Redis yet (the cache and rate limits are in-process); it still expects the URL, and a later phase will share the GeckoTerminal budget through Redis. Do not run a second heavy GeckoTerminal client (the paper runner and a polling client) at the same time: keyless GeckoTerminal is about 5 calls per minute per IP.

```bash
# Debian/Ubuntu. Other systems: create the same role and databases.
sudo apt-get install -y postgresql redis-server python3.12-venv
sudo pg_ctlcluster 16 main start    # or: sudo service postgresql start
sudo service redis-server start

sudo -u postgres psql -c "CREATE ROLE dexbot LOGIN PASSWORD 'dexbot';"
sudo -u postgres createdb -O dexbot dexbot
sudo -u postgres createdb -O dexbot dexbot_test

cd backend
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
export DEXBOT_DATABASE_URL=postgresql+asyncpg://dexbot:dexbot@127.0.0.1:5432/dexbot
export DEXBOT_REDIS_URL=redis://127.0.0.1:6379/0
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

`GET /health` should report `"mode": "paper"` and `"execution": "disabled"`.

## Backtest

Fetches real 5-minute OHLCV from GeckoTerminal (trending alt pools on the enabled chains) and runs momentum, volume-spike, breakout and mean reversion. Fees, per-chain gas and size-dependent slippage are applied. A strategy is kept only when it has at least 20 trades, positive expectancy after costs, profit factor above 1, and max drawdown under 25% of the $1,000 book. Win rate is not the criterion. The run takes a few minutes because of the keyless rate limit.

```bash
cd backend
export DEXBOT_LOG_LEVEL=WARNING DEXBOT_LOG_JSON=false
.venv/bin/python -m app.backtest --per-chain 3 --limit 1000
```

This writes `docs/backtest-results.json` and `docs/strategy_validation.json`. If nothing passes, the paper runner will scan and screen but will not open positions.

## Research round (out of sample)

`python -m app.backtest` is the first fixed-parameter run. The research commands build a larger cached sample and only then search parameters. Search uses train pools and the earlier part of their history. A strategy is enabled for paper trading only if the frozen parameters also clear the time holdout, the pool holdout, a three-window walk-forward, and a neighbour check. The grid is 35 configurations, counted in `app/research/grid.py`.

```bash
cd backend
export DEXBOT_LOG_LEVEL=WARNING DEXBOT_LOG_JSON=false
.venv/bin/python -m app.research.collect --per-chain 8   # network, cached under data/market_cache
.venv/bin/python -m app.research.validate                # no network; rewrites docs/strategy_validation.json
```

GeckoTerminal's keyless API has no historical new-pool archive (pages past 10 return 401) and OHLCV has no reserve column, so liquidity is the spot snapshot. The thin cohort is low spot liquidity and an age of at least a few hours. Results land in `docs/research-results.json`. The dashboard shows each strategy's paper P&L next to that out-of-sample expectancy.

## Paper runner

Simulated starting balance defaults to $1,000 (`DEXBOT_PAPER_STARTING_BALANCE_USD`, or `--balance` the first time the account is created). Fills include the pool fee, gas and slippage. Restarts do not reset the book.

```bash
cd backend
export DEXBOT_DATABASE_URL=postgresql+asyncpg://dexbot:dexbot@127.0.0.1:5432/dexbot
.venv/bin/python -m app.paper --once     # one scan
.venv/bin/python -m app.paper            # loop (default every 90s)
```

Default risk limits (override with `DEXBOT_RISK_*` in `.env.example`; averaging down and martingale cannot be turned on):

| Limit | Default |
|---|---|
| Max position | $50 |
| Max exposure | $300 |
| Max concurrent positions | 5 |
| Max daily loss (kill switch, resets next UTC day) | $50 |
| Gas reserve left in cash | $5 |
| Stop / take-profit / time stop | 8% / 15% / 240 minutes |
| Cooldown | 15 minutes after 2 consecutive losses |
| Max estimated slippage | 200 bps |
| Min liquidity / h1 volume / age | $20,000 / $5,000 / 30 minutes |
| Max buy or sell tax | 10% |

Screening uses RugCheck for Solana (GoPlus Solana if RugCheck is down) and GoPlus for Base, BSC and Arbitrum. A missing critical check blocks the entry.

## Dashboard

With uvicorn running:

- http://127.0.0.1:8000/dashboard
- http://127.0.0.1:8000/api/paper/summary
- http://127.0.0.1:8000/api/paper/positions
- http://127.0.0.1:8000/api/paper/strategies

The page is labelled PAPER. Simulated balance is mark-to-market equity. Realised P&L is closed round trips. Unrealised P&L is an estimated exit (fee, slippage, gas) on open positions, so a new position is negative until price moves enough to pay the exit. Net P&L is the sum of the two.
