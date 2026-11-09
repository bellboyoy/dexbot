# Progress

## Phase 1 — done (2026-11-09)

Completed:
- Architecture, credentials list, provider limits, schema and phase plan (`docs/ARCHITECTURE.md`).
- Project structure, env-based config (live mode not settable from env), structured JSON logging, Docker Compose (Postgres, Redis, backend).
- Domain models: chains with per-family address validation, pool snapshots, candles, trades, discovery leads. Unknown values are `None`, never zero.
- HTTP resilience layer: token-bucket rate limiter with 429 penalties and `Retry-After`, TTL cache, circuit breaker, retries with jittered backoff.
- DexScreener provider: pair, token pairs, batched tokens, search, discovery from profiles and (labelled as paid) boosts.
- GeckoTerminal provider: pool, new pools, trending pools, OHLCV (all documented timeframes, forming candle flagged), trades, unique buyers/sellers.
- Market data service: capability routing, failover, gap-filling merge across sources, price-divergence warning, de-duplicated discovery, closed-candles-only by default.
- SQLAlchemy schema for all 14 tables (+ security_checks), tested on real PostgreSQL.
- Execution adapter interface (implementations in Phases 6–7).
- FastAPI: `/health`, pool snapshot, OHLCV, new pools.

Tests: 57 unit tests (offline, using responses captured from the live APIs), 3 PostgreSQL schema tests, 1 live end-to-end smoke test. All passing.

Verified live: DexScreener `/tokens/v1`, `/latest/dex/pairs`, `/latest/dex/search`, `/token-profiles/latest/v1`, `/token-boosts/latest/v1`; GeckoTerminal `new_pools`, `trending_pools`, pool, OHLCV, trades.

Outstanding / known issues:
- GeckoTerminal keyless limit (~5/min) is the main bottleneck. A CoinGecko API key and the pro-api base URL would lift it; not yet wired.
- DexScreener pair-endpoint limits and `/tokens/v1` batch size (30) are not in rendered docs; configured conservatively.
- `/token-pairs/v1` is implemented but not yet covered by a live test.
- Cache is in-memory; Redis backend pending (Phase 2) so multiple workers share limits.
- Migrations via Alembic pending (Phase 2); `create_schema` is a dev helper.
- EVM EIP-55 checksum validation pending (web3, Phase 4).

## Next: Phase 2
Snapshot collector persisting to Postgres, screening profiles for new vs established tokens, Redis cache, Alembic, RPC-based monitoring of watched pools.
