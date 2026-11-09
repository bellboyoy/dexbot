# DexBot

Multi-chain (Solana, Base, BSC; Arbitrum later) short-term DEX trading system. **Paper trading only.** Live trading is not implemented and can never be enabled from configuration.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design, credentials, provider limits, schema and phase plan, and [docs/PROGRESS.md](docs/PROGRESS.md) for what is done.

## Run

```bash
cp .env.example .env
docker compose up --build
curl localhost:8000/health
curl "localhost:8000/market/solana/pools/<pool>?enrich=true"
curl "localhost:8000/market/solana/pools/<pool>/ohlcv?timeframe=5m&limit=50"
```

## Tests

```bash
cd backend
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest                         # unit tests (offline, real captured API fixtures)
DEXBOT_TEST_DATABASE_URL=postgresql+asyncpg://dexbot:dexbot@localhost:5432/dexbot_test .venv/bin/pytest -m postgres
.venv/bin/pytest -m live                 # real API smoke test (~1 min due to GeckoTerminal limits)
```
