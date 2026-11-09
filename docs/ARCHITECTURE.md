# DexBot: Architecture and Implementation Plan

Status: Phase 1 (architecture, structure, market data layer). Paper trading only. Live trading is not implemented and can never be switched on automatically.

## 1. Honest framing before any code

The brief asks for net profitability after all costs. Three facts shape everything below.

1. **Costs are a large hurdle at this size.** With 100 USDT and 1 USDT risk per trade, positions will be roughly 15–40 USDT. A round trip on a small-cap DEX pool typically costs pool fees (0.25–1% per side on most AMMs, often more on launchpad pools), slippage and price impact, plus gas (fractions of a cent on Solana, cents on Base, a few cents on BSC). A realistic all-in round-trip cost of 1–3% means a strategy has to beat that before it earns anything. The cost model is therefore a first-class component, not an afterthought.
2. **Historical data for new tokens is thin.** Free APIs give recent OHLCV and current aggregates (buyers, txns, volume), but not historical buyer counts, holder distributions, or liquidity snapshots. Dead tokens also drop out of listings (survivorship bias). So the system starts **recording its own snapshots from day one**, and the backtester trains on that forward-collected data plus whatever OHLCV history exists. Strategies for new pools cannot be honestly validated until weeks of our own data exist.
3. **The 75-point score threshold is a starting point, not a truth.** Weights and thresholds will be calibrated on recorded data with walk-forward splits. If no strategy shows positive expectancy out of sample, the right outcome is "don't trade", and the system will say so.

## 2. System overview

```
                 ┌───────────────────────── Dashboard (Next.js) ─────────────────────────┐
                 │ portfolio · positions · scanner · signals · strategy stats · controls │
                 └───────────────▲───────────────────────────────────────┬────────────────┘
                                 │ REST + WebSocket (no secrets)          │ control commands
┌────────────────────────────────┴───────────────────────────────────────▼────────────────┐
│ FastAPI backend (asyncio)                                                                 │
│                                                                                           │
│  Market data ──► Discovery/Screening ──► Strategies (5) ──► Scoring ──► Security engine  │
│   (providers,       (new vs established     (independent      (weighted   (multi-source,  │
│    cache, limits)    profiles)               signals)          0–100)      unknown≠safe)  │
│                                                                              │            │
│                                                                              ▼            │
│                        Position manager ◄──── Execution router ◄──── RISK ENGINE (final   │
│                        (TP/SL/trail/time,      (paper | solana |       authority, kill    │
│                         reconciliation)         evm adapters)          switch, limits)    │
│                                                     │                                     │
│                                         Wallet service (separate process, signs only     │
│                                         validated, limit-checked payloads; Phase 7+)      │
└───────────────────────────────┬───────────────────────────────────────────────────────────┘
                                │
                  PostgreSQL (system of record)  ·  Redis (cache, rate-limit state, pub/sub)
```

Key rules baked into the structure:

- **Every external source sits behind an interface.** `MarketDataProvider`, `SecurityProvider`, `ExecutionAdapter`. Swapping DexScreener for Birdeye, or 0x for a direct router, is a new class, not a rewrite.
- **The risk engine is the only path to execution.** Strategies emit signals; they never call adapters. The execution router refuses any order without a risk approval token produced for that exact order (chain, token, size, max loss, exit route).
- **Paper trading uses the same adapter interface as live.** The paper adapter takes real quotes and applies a cost/fill model, so moving to live changes one component, not the pipeline.
- **The wallet service is isolated.** Keys never enter the main API process or the frontend. It signs only payloads that pass its own validation (allowed router addresses, spending limits, approval caps).
- **Unknown is not clean.** Every security check returns `PASS`, `FAIL` or `UNKNOWN`; a critical check that is `UNKNOWN` blocks the trade.

## 3. API credentials needed

| Credential | Needed for | When | Notes |
|---|---|---|---|
| None | DexScreener | Now | Keyless public API. |
| None (optional CoinGecko key) | GeckoTerminal | Now | Keyless works but is heavily limited (see §4). A CoinGecko Demo or paid key moves onchain endpoints to `pro-api.coingecko.com/api/v3/onchain` with higher limits. Recommended before paper trading at scale. |
| Solana RPC (HTTP + WebSocket) | Balances, holder checks, tx monitoring, simulation | Phase 2–4 | Public `api.mainnet-beta.solana.com` is too rate-limited for this. Helius, QuickNode or Triton. |
| Base, BSC, Arbitrum RPC | Same for EVM | Phase 2–4 | Alchemy, QuickNode, Infura or Ankr. One key per chain or a multi-chain provider. |
| 0x API key | EVM swaps (Base, BSC, Arbitrum) | Phase 6 (quotes for paper), 7 | Swap API v2 needs `0x-api-key` and `0x-version: v2` headers. Chain coverage to be confirmed against 0x docs when implemented. |
| Jupiter API | Solana quotes and swaps | Phase 6 (quotes), 7 | Jupiter has a keyless lite tier and a keyed tier; confirm current terms when implemented. |
| GoPlus Security | Token security (EVM + Solana) | Phase 4 | Free tier available, key optional; limits to confirm. |
| Honeypot.is | EVM honeypot simulation | Phase 4 | Covers Ethereum, BSC, Base; confirm Arbitrum. |
| RugCheck | Solana token risk report | Phase 4 | Public API. |
| Trading wallet keys | Signing | Phase 9 only | A new, dedicated wallet per chain family, funded only with trading capital. Never your main wallet. Stored encrypted, loaded only by the wallet service. |

Nothing needs to be supplied for Phase 1–2 work; RPC keys become useful at Phase 2's on-chain monitoring step.

## 4. Provider limitations (checked against docs and live calls on 2026-11-09)

**DexScreener** (`https://api.dexscreener.com`)
- Endpoints used: `/latest/dex/pairs/{chainId}/{pairId}`, `/latest/dex/search?q=`, `/token-pairs/v1/{chainId}/{tokenAddress}`, `/tokens/v1/{chainId}/{addresses}`, `/token-profiles/latest/v1`, `/token-boosts/latest/v1`, `/token-boosts/top/v1`.
- Documented limit: 60 req/min for profile/boost style endpoints. Limits for pair/token endpoints are not rendered in the docs page; commonly cited as 300 req/min, but **unverified**, so the client is configured at 60/min for profiles and 200/min for pairs and backs off on 429.
- Data: aggregated `txns` (buys/sells) and `volume` and `priceChange` for m5, h1, h6, h24; `liquidity.usd`; `fdv`; `marketCap`; `pairCreatedAt` (ms). **No OHLCV, no unique buyers, no holder data, no history.** Many fields are nullable (verified: wSOL pairs return `fdv: null`).
- Chain ids: `solana`, `base`, `bsc`, `arbitrum`.

**GeckoTerminal** (`https://api.geckoterminal.com/api/v2`)
- Endpoints used: `/networks/{network}/new_pools`, `/networks/{network}/trending_pools`, `/networks/{network}/pools/{address}`, `/networks/{network}/pools/{address}/ohlcv/{timeframe}`, `/networks/{network}/pools/{address}/trades`.
- **Keyless limit is very tight and dynamic** (docs: about 5 calls/min per IP, varying with load). Verified: a burst of 4 calls returned three 429s. Responses are cached server-side for roughly 30–60s (`cache-control: max-age=30, s-maxage=60`).
- OHLCV: timeframes `day` (agg 1), `hour` (1, 4, 12), `minute` (1, 5, 15), `second` (1, 15, 30); `limit` max 1000; page back with `before_timestamp`. Newest candle first. Gaps are omitted unless `include_empty_intervals=true`.
- Adds what DexScreener lacks: **unique buyers/sellers** per window (m5, m15, m30, h1, h6, h24), m15/m30 windows, OHLCV.
- Prices are returned as strings with very long decimals; parsed with `Decimal`.

**Consequence for design:** DexScreener is the high-frequency source for scanning and position monitoring. GeckoTerminal is used sparingly (new-pool discovery, OHLCV for shortlisted candidates, buyer counts), behind a cache and a strict rate limiter. On-chain RPC monitoring (Phase 2b) reduces dependence on both for open positions.

**Not yet verified** (marked as such in code until tested): 0x, Jupiter, GoPlus, Honeypot.is, RugCheck, RPC providers.

## 5. Database schema (PostgreSQL)

All tables have `created_at`/`updated_at` (UTC, `timestamptz`). Every chain-specific record carries `chain` (enum: solana, base, bsc, arbitrum). Addresses stored as text in canonical form (EVM checksummed, Solana base58). Token amounts stored as `NUMERIC(78,0)` raw units plus `decimals`; USD values as `NUMERIC(38,18)`.

| Table | Purpose | Key columns |
|---|---|---|
| `tokens` | Token registry | chain, address (unique pair), symbol, name, decimals, first_seen_at, is_validated |
| `pools` | DEX pools/pairs | chain, address (unique), dex_id, base_token_id, quote_token_id, pool_created_at, first_seen_at |
| `market_snapshots` | Time series from providers | pool_id, source, observed_at, price_usd, liquidity_usd, fdv_usd, market_cap_usd, volume_{m5,h1,h6,h24}, buys/sells per window, buyers/sellers per window, raw jsonb. Indexed (pool_id, observed_at). Partition by month later. |
| `candles` | OHLCV | pool_id, source, timeframe, open_time (unique together), o/h/l/c, volume_usd |
| `signals` | Strategy output | strategy, version, pool_id, chain, score, score_components jsonb, features jsonb, decision (accepted/rejected + reason) |
| `security_checks` | Security engine results | token_id, chain, provider, check_name, status (PASS/FAIL/UNKNOWN), details jsonb, checked_at |
| `risk_events` | Every risk decision and limit hit | event_type, severity, signal_id, position_id, details |
| `positions` | Open/closed positions | mode (paper/live), chain, token_id, pool_id, strategy, status, entry/exit prices, size, planned max loss, stop/take-profit/trail params, opened_at, closed_at, exit_reason, net_pnl_usd |
| `trades` | Fills (entry and exit legs) | position_id, side, mode, amount_in/out raw, price, fees_usd, gas_usd, slippage_bps, price_impact_bps |
| `transactions` | On-chain or simulated tx lifecycle | chain, tx_hash/signature, trade_id, status (built/simulated/submitted/confirmed/failed/dropped), nonce, error, idempotency_key (unique) |
| `wallets` | Trading wallet metadata, never keys | chain_family, address, label, spending limits |
| `portfolio_balances` | Balance snapshots | mode, wallet_id, chain, token_id, amount, value_usd, observed_at |
| `strategy_performance` | Aggregated stats per strategy/period | strategy, period, trades, win_rate, profit_factor, expectancy, max_dd, fees |
| `config` | Versioned runtime configuration | key, value jsonb, version, changed_by |
| `system_logs` | Important structured events | level, component, event, context jsonb |
| `discovery_leads` | Every discovery lead and its screening outcome (Phase 2) | chain, token_address, pool_address, source, reason, profile, passed, rejections, metrics |
| `watchlist` | Pools whose history is collected (Phase 2) | pool_id, profile, status, status_since, last_snapshot_at, consecutive_misses, next_ohlcv_at |

Crash recovery: on startup the position manager loads every position not in a terminal state, reconciles each against the adapter (paper ledger or chain balance), and refuses to open new trades until reconciliation is clean. `transactions.idempotency_key` prevents duplicate orders across restarts.

## 6. Blockchain adapter interfaces

One interface for all execution, implemented by `PaperExecutionAdapter`, `SolanaJupiterAdapter`, `EvmZeroExAdapter`, `PancakeSwapAdapter`:

```python
class ExecutionAdapter(Protocol):
    chain: Chain
    async def get_quote(self, req: QuoteRequest) -> Quote            # amount out, price impact, route, fees, expiry
    async def check_exit_route(self, token, amount) -> ExitRoute     # must succeed before any entry
    async def estimate_costs(self, quote: Quote) -> CostEstimate      # gas + fees + expected slippage, in USD
    async def build(self, quote: Quote, params: ExecParams) -> PreparedTx   # deadline, slippage cap, idempotency key
    async def simulate(self, tx: PreparedTx) -> SimulationResult
    async def submit(self, tx: PreparedTx) -> SubmittedTx             # signing delegated to wallet service
    async def confirm(self, tx: SubmittedTx, timeout_s) -> TxOutcome  # confirmed/failed/dropped, actual amounts
    async def get_balance(self, token) -> Balance                     # actual, not assumed
    async def reconcile(self, position) -> ReconciliationResult
```

Chain differences handled inside adapters, never assumed equal: Solana uses blockhash expiry and signature confirmation levels, priority fees, ATA creation; EVM uses nonces, gas price/EIP-1559 (BSC is legacy-gas style in practice), ERC-20 approvals (exact-amount, never infinite), and deadlines. Quote comparison happens in the router across adapters for the same chain.

## 7. Phase plan with exit criteria

| Phase | Deliverable | Exit criteria |
|---|---|---|
| 1 | Architecture, structure, schema, interfaces, market data layer, tests | Unit tests green; live smoke test against both APIs passes |
| 2 | Discovery and screening (new vs established profiles), snapshot collector writing to Postgres, on-chain monitoring for watched pools | Collector runs 24h unattended, no gaps beyond provider outages, rate limits respected |
| 3 | Indicators (EMA, RSI, MACD, VWAP, ATR, RVOL, S/R, acceleration, imbalance) and 5 strategies, scoring engine | Each strategy deterministic on fixtures; no look-ahead (tests feed bars one at a time) |
| 4 | Security engine (multi-provider), risk engine, kill switch | Unknown-critical blocks trade; every limit has a test that tries to break it |
| 5 | Backtester with cost model, walk-forward, metrics | Reproduces hand-computed results on synthetic series; reports out-of-sample metrics separately |
| 6 | Real-time paper trading with real quotes and simulated fills/failures | 200+ paper trades persisted; positions never untracked across restarts |
| 7 | Execution adapters (Jupiter, 0x, PancakeSwap) on testnet/simulation | Simulations pass; no live submission path enabled |
| 8 | Dashboard | All listed panels and controls; live mode button requires explicit multi-step confirmation |
| 9 | Controlled live deployment | Only after your explicit go-ahead, with tiny size and all risk checks on |

## 8. Discovery and historical collection (Phase 2)

The collector is a separate process (`python -m app.collector`, compose service `collector`). It has three loops. Each one is isolated, so an exception in one never stops the others.

**Discovery (every 60s).**
- Leads come from DexScreener latest profiles and boosts, GeckoTerminal new and trending pools per chain, and established seeds (the top pools of each chain's quote assets).
- Each token is resolved to its deepest pool *with an exitable quote asset* (SOL/WSOL, USDC, USDT, WETH, WBNB).
- The token is screened with the new-token profile (pool age 72h or less) or the established-token profile.
- Every lead is stored in `discovery_leads` with its outcome and metrics, including rejected and unresolved ones. Passing pools join `watchlist`.

**Snapshots.** Every watched pool is snapshotted via DexScreener's multi-pair endpoint (30 pools per request). Active pools are snapshotted every 30s, inactive every 10 min, dead every 30 min.

**OHLCV.**
- Each pool gets an initial 1m and 5m backfill (1000 candles each), then 1m refreshes: every 6h for active pools, 12–24h for inactive and dead ones.
- Requests go through a priority gate so discovery is served first.
- Only closed candles are stored, and the upserts are idempotent.

**Lifecycle and survivorship.**
- Watched pools move `active → inactive` (liquidity under $5k or no trades in an hour) `→ dead` (liquidity under $1k, or missing for 10 consecutive snapshots) `→ retired` (72h after going inactive or dead).
- Dead pools keep being recorded until retirement, so rugs and fades stay in the dataset.
- A provider outage is never counted as a missing pool.

**Address validation.** Token addresses must be valid for the chain. EVM pool identifiers may be contract addresses or 32-byte pool ids (Uniswap v4 and similar singleton DEXes). Launchpad bonding-curve pseudo-pairs (for example DexScreener's `…:4meme`) are rejected because they are not routable DEX pools. The quote-asset addresses are hardcoded, and are used only after a live check confirms their on-DEX symbols.

**Budget.** Keyless GeckoTerminal is the bottleneck. In a live run, 5 calls a minute still drew sustained 429s, so the default is now 3 a minute. The 429 penalty is 30s, doubling up to 120s, and a request gives up after two retries. At that rate the initial backfill of N pools takes about N×2/3 minutes. The DexScreener snapshot series (30s resolution, with buys, sells, volume and liquidity) does not depend on GeckoTerminal and is the primary historical record.

## 9. Repository layout

```
dexbot/
  docker-compose.yml        postgres, redis, backend (frontend added in Phase 8)
  .env.example              every setting, no secrets
  docs/ARCHITECTURE.md      this document
  backend/
    pyproject.toml
    app/
      config.py             pydantic-settings, env-based
      logging.py            structlog JSON logging
      main.py               FastAPI app (health, market data endpoints)
      domain/               chain enum, market models (pure, no I/O)
      marketdata/           http client (rate limit, cache, retry, circuit breaker),
                            providers (dexscreener, geckoterminal), aggregator, priority gate
      discovery/            screening profiles, discovery service, quote-asset validation
      collector/            collector worker (python -m app.collector)
      execution/            adapter interfaces (implementations in Phase 6–7)
      db/                   SQLAlchemy models, repository (idempotent upserts)
    migrations/             Alembic
    tests/
      fixtures/             real responses captured from the live APIs
      unit tests; tests marked `live` hit real APIs and are opt-in
```
