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

## Phase 2 — discovery and market data collection (2026-11-09)

Completed:
- **Collector process** (`python -m app.collector`, compose service `collector`) with isolated loops for discovery, snapshots, buyers and OHLCV, scanning Solana, Base and BSC independently. Arbitrum is wired in but not enabled.
- **Three discovery pipelines** (`new`, `established`, `momentum`), each with its own sources, schedule, screening profile and stats. Sources are pluggable adapters (`DiscoverySource`): DexScreener profiles/boosts, GeckoTerminal new/trending/top pools, and quote-asset seeds.
- **Deduplication** across providers per `(chain, token)` and per pool, enforced in code and by unique constraints (`ON CONFLICT`). Every lead is stored with its outcome, including rejected and unresolved ones, to avoid survivorship bias.
- **Historical snapshots** every 30s for active pools (DexScreener, 30 pools per request), with `source` and real fetch time `observed_at` on every row.
- **OHLCV**: 1m and 15m backfill (1000 candles each), then 1m refreshes. 5m, 1h and 4h are resampled from stored 1m candles; only fully covered buckets are emitted. Only closed candles are stored.
- **Metrics** (`pool_metrics`): volume acceleration (age-gated), buy/sell ratios, unique buyers/sellers (GeckoTerminal, used only if under 10 min old), liquidity change over 5m and 1h from our own history. Missing inputs are `NULL`, never 0.
- **Momentum detection** among watched pools, from reliable metrics only, with a 15-minute cooldown.
- **Data-quality system**: stale, frozen, missing fields, invalid values, liquidity jumps, cross-source price/liquidity divergence, provider outages, candle gaps/lag. Blocking flags mark an observation unreliable; it is kept for audit but never feeds metrics or momentum. Cached responses keep their original fetch time, so stale data cannot be passed off as fresh.
- **Restart recovery**: watchlist schedules, discovery schedules, liquidity history and momentum cooldowns are restored from Postgres. Clean shutdown on SIGINT/SIGTERM.
- **Pool lifecycle** active → inactive → dead → retired, with outages never counted as a pool going missing.
- **API**: `/collector/status`, `/quality?hours=`, `/history/{chain}/pools/{pool}/candles?timeframe=`.
- **Migrations**: 3 new (discovery/watchlist, wider pool ids for Uniswap v4-style 32-byte ids, market intelligence).

Tests (all passing on 2026-11-09):
- Unit (offline): 114 passed. They cover parsing, screening, metrics, quality checks, resampling, scheduling, lifecycle and API, including failure cases (outages, 429s, missing fields, stale and cached data, invalid addresses).
- PostgreSQL: 19 passed. They cover migrations up/down/up with no drift, discovery to watchlist to snapshots and candles with fake providers, deduplication, outage handling, restart recovery, lifecycle, OHLCV idempotency, chain fairness and API endpoints.
- Live: 3 passed (provider APIs, quote assets on all chains, end-to-end collection).

Live run (30 minutes, all three chains, restarted from the previous run's state):
- All three pipelines ran on every chain. Lead sources: GeckoTerminal new, trending and top pools, DexScreener profiles and boosts, and quote-asset seeds. Every outcome was recorded, including rejections (for example `too_young`, `unknown_liquidity`, `quote_not_exitable`, `no_sells`).
- Watchlist: Solana 35 pools, Base 22, BSC 30 (some moved to inactive or dead during the run). Snapshots this run: Solana 1,969, Base 1,025, BSC 1,352.
- Metrics: every row reliable. 63% of rows had a fresh unique-buyers count.
- Six momentum events (Solana 4, Base 1, BSC 1).
- Quality: no stale or frozen data. Cross-source divergence and liquidity-jump flags were raised and recorded.
- 15 GeckoTerminal 429s were absorbed by the limiter, with no lost loops. Restart restore and SIGINT shutdown were clean.
- **Issue found and fixed:** all candle backfills had gone to Solana, and Base and BSC had none, because the queue was ordered globally by due time. It now rotates across chains and puts active pools before dead ones (new test). A 9-minute re-run backfilled one pool on each chain, and Base and BSC now have candles.

Fixed during live runs:
1. Uniswap v4-style 32-byte pool ids (Base, BSC) were rejected as invalid, silently dropping those pools. They are now accepted and the column was widened.
2. GeckoTerminal 429 storms starved OHLCV collection. Budget lowered to 3 calls/min, penalty capped at 120s, retries capped at 2, discovery spread out, at most one rate-limited fetch per pipeline per cycle, priority gate (discovery > buyers > backfill).
3. Cached responses were re-stamped with the current time, which could pass stale data off as fresh. The cache now keeps the original fetch time and the stale check uses it.
4. JSON `null` was stored instead of SQL `NULL` in `quality_flags`, which broke the restart query that rebuilds liquidity history.
5. Volume acceleration was trivially high for pools younger than the comparison window, producing false momentum events. It is now only computed once the pool is old enough; the false events from that run were deleted.
6. Shutdown could hang behind a rate-limit penalty. Loops are now cancelled after a 10s grace period.

API limitations:
- **Keyless GeckoTerminal (~3 usable calls/min, shared per IP)** is the binding constraint. It limits how many pools get candles and buyer counts: the first backfill of N pools takes about N×2/3 minutes, and buyer counts for 300 pools need 10 requests, one every 90 seconds, so each pool is refreshed about every 15 minutes. With a full watchlist many buyer counts will therefore be older than the 10-minute freshness limit and reported as unknown. A CoinGecko API key (pro endpoint) would lift this; it is not wired yet.
- **DexScreener has no OHLCV, unique buyers or history.** Our 30s snapshots are the history; candles come only from GeckoTerminal.
- **Unique buyers** come only from GeckoTerminal and are treated as unknown when older than 10 minutes.
- DexScreener's limits for pair/token endpoints are not documented; batches of 30 were verified live and the client stays conservative.
- GeckoTerminal top pools (`/networks/{n}/pools?sort=h24_volume_usd_desc`) was verified live on all three chains in the final run.

Outstanding issues:
- **Candle coverage is budget-bound.** Discovery and buyer counts take most of the ~3 GeckoTerminal calls a minute, so backfill managed only about one pool every 3 minutes. 75 watched pools were still waiting for candles at the end of the run, while snapshots and metrics covered every pool. A CoinGecko key, or a smaller watchlist, is needed for full candle coverage.
- No Redis-backed cache or rate limiter yet; a single collector process owns the budgets. Running two collectors would double the GeckoTerminal load.
- No on-chain RPC monitoring yet (planned before paper trading, to reduce dependence on the two aggregators).
- Momentum thresholds and screening profiles are first estimates and need calibration against collected history (Phase 3 backtesting).
- EIP-55 checksum validation of EVM addresses is still pending (Phase 4).
- The same sandbox caveat as Phase 1 applies: please run `docker compose up -d --build` once on your machine.

## Next: Phase 3
Not started. Waiting for Phase 2 acceptance.
