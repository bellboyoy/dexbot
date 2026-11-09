# DexBot

Multi-chain (Solana, Base, BSC; Arbitrum later) short-term DEX trading system. **Paper trading only.** Live trading and wallet execution are not implemented, and the trading mode cannot be changed through configuration.

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): design, credentials, provider limits, schema and phase plan
- [docs/PROGRESS.md](docs/PROGRESS.md): what is done, what was verified, and known issues

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

The `migrate` service runs `alembic upgrade head` against a fresh database and exits, then `backend` and `collector` start. All ports bind to 127.0.0.1 only.

The `collector` service discovers tokens on every enabled chain, screens them, and records history for the pools it watches: DexScreener snapshots every 30 seconds and 1-minute candles from GeckoTerminal. Check what it has recorded with:

```bash
curl localhost:8000/collector/status                       # per chain: watchlist, snapshots, candles, leads, momentum
curl "localhost:8000/quality?hours=24"                     # data-quality events by chain, provider and check
curl "localhost:8000/history/solana/pools/<pool>/candles?timeframe=1h"   # stored candles (resampled from 1m if needed)
docker compose logs -f collector
```

To run it outside Docker: `cd backend && DEXBOT_DATABASE_URL=... .venv/bin/python -m app.collector`.

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
