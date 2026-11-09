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

Tests: 65 unit tests (offline, using responses captured from the live APIs), 4 PostgreSQL tests (schema + migrations), 1 live end-to-end smoke test. All passing.

Verified live: DexScreener `/tokens/v1`, `/latest/dex/pairs`, `/latest/dex/search`, `/token-profiles/latest/v1`, `/token-boosts/latest/v1`; GeckoTerminal `new_pools`, `trending_pools`, pool, OHLCV, trades.

Outstanding / known issues:
- GeckoTerminal keyless limit (~5/min) is the main bottleneck. A CoinGecko API key and the pro-api base URL would lift it; not yet wired.
- DexScreener pair-endpoint limits and `/tokens/v1` batch size (30) are not in rendered docs; configured conservatively.
- `/token-pairs/v1` is implemented but not yet covered by a live test.
- Cache is in-memory; Redis backend pending (Phase 2) so multiple workers share limits.
- EVM EIP-55 checksum validation pending (web3, Phase 4).

## Phase 1 verification pass (2026-11-09)

| Check | Result | Evidence |
|---|---|---|
| `docker compose up` | Pass | Clean volumes: postgres, redis, backend healthy; `migrate` exited 0; `/health`, pool (enriched), OHLCV and invalid-address 422 checked against the running stack. |
| Migrations on a clean DB | Pass after fix | Alembic added. `upgrade` → `check` (no drift) → `downgrade base` → `upgrade` cycle is now a test (`tests/test_migrations.py`). |
| Rate limiting | Pass | Live: 7 GeckoTerminal calls paced ~12s apart at 5/min. One 429 still occurred (the keyless limit is dynamic and shared per IP) and was absorbed by backoff. |
| Failover | Pass | Live: DexScreener forced down → 3/3 requests served by GeckoTerminal. Unit tests cover 5xx, timeouts, 429s and not-found fallthrough. |
| Recovery after outage | Pass | Live: DexScreener restored → after breaker cooldown the half-open probe succeeded and traffic returned to it. Failed probe re-opens the breaker (unit test). |
| Secrets / wallet data | Pass | Tracked files and history scanned; `.env`, keys, keystores, wallet and keypair files ignored. API responses and OpenAPI contain no secret values (test). Image runs as non-root, contains no `.env` or secret env. No trading, wallet or signing routes exist (test). |
| Fresh clone + README | Pass | Clean clone: venv install, unit, PostgreSQL, migration and live tests all pass following the README exactly. |

Issues found and fixed:
1. Downgrading the initial migration left the `chain` enum type behind, so re-upgrading failed. Downgrade now drops it.
2. Compose had no migration step and backend had no healthcheck. Added a one-shot `migrate` service that the backend waits on, plus redis/backend healthchecks.
3. A pool not yet indexed by DexScreener returned "not found" without trying GeckoTerminal. It now falls through.
4. The 429 penalty without `Retry-After` was 5s, too short for keyless GeckoTerminal, which kept rejecting. It is now configurable and 30s for GeckoTerminal.

Verification caveats:
- The sandbox used for verification reaches the internet only through a TLS-intercepting proxy, so the stack was started with a local, uncommitted override (host networking, proxy CA). The committed `docker-compose.yml` was otherwise used unchanged. Please run `docker compose up -d --build` once on your machine as a final check.
- During a DexScreener outage, GeckoTerminal alone cannot carry scanning load at ~5 calls/min. Requests fail closed (reported unavailable) rather than returning stale or partial data. A CoinGecko API key is the fix before paper trading at scale.
- API JSON renders Decimals as floats (display only; storage and internal math use Decimal).

## Paper stack (2026-11-09)

Added on top of Phase 1, still paper-only. `Settings.trading_mode` is unchanged. See `docs/AUDIT.md` for the bugs fixed in the rate limiter, the half-open breaker and closed-candle fetches, and for the provider checks (RugCheck, GoPlus EVM, GoPlus Solana).

The backtest result and the list of strategies the runner may open are generated, not hand-written: `docs/backtest-results.json` and `docs/strategy_validation.json`.

## Next

Keep collecting our own snapshots. The free OHLCV history is too short, and too biased toward pools that are still listed, to treat a single backtest pass as an edge. Redis is still not the shared cache. Live execution remains unbuilt and unauthorised.
