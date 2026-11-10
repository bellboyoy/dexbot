# Technical audit (2026-11-09)

Scope: the Phase 1 market-data layer on `phase-1-market-data`, plus the paper-trading code added with this change. Paper only. Live execution is not implemented and `Settings.trading_mode` is still hardcoded to `paper`.

## Critical bugs fixed

1. **Rate limiter held its lock while sleeping.** `RateLimiter.acquire` slept inside the asyncio lock. Every other caller waited out the first caller's full delay, and a 429 `penalize()` could not be observed until that sleep ended. Sleeping now happens outside the lock; the token update stays inside it.
2. **Half-open circuit breaker let every in-flight request through.** After the cooldown, `allow()` returned true for all callers, so a scan fan-out would hit a provider that was only possibly back. Half-open now admits one probe. A 4xx releases the probe without counting as a failure (client errors still do not open the breaker). A failed probe still re-opens immediately.
3. **Closed-candle requests were short one bar.** `get_ohlcv(..., limit=N)` asked the provider for N rows and then dropped the forming candle, so callers received N-1 closed bars. The service now requests N+1 and returns at most N closed bars. The provider still caps a single GeckoTerminal page at 1000.

## Other defects and inefficiencies (not changed, or only documented)

- **Redis is unused.** `DEXBOT_REDIS_URL` is configured and the process can run beside Redis, but the cache and the rate-limit buckets are in-memory and per process. The API and the paper runner each have their own GeckoTerminal budget. Running both against keyless GeckoTerminal (~5 calls/min per IP, verified earlier and still the binding constraint) will 429. Negative 404 responses are not cached, because the cache treats a missing value as a miss. Cached JSON objects are returned by reference.
- **DexScreener pool lookup is an exact address match.** Solana base58 is case-sensitive and is not canonicalised. A mismatched case falls through to GeckoTerminal rather than returning the wrong pool.
- **Market JSON still renders `Decimal` as float.** That is display-only. Storage, the cost model and the paper ledger stay in `Decimal`. The paper endpoints return decimal strings.
- **`pool_fee_percentage` is often null.** Checked on live GeckoTerminal trending pools (2026-11-09): the fee is in the pool name (`0.042%`, `1%`) and the attribute is null. The cost model uses the attribute when it is a percent, otherwise a single percentage in the name, otherwise 30 bps per side.
- **OHLCV volume is USD for this client.** A captured SOL/USDC 5-minute candle (~$285k) lines up with the pool's `volume_usd.m5` (~$278k). The client always requests `currency=usd`.
- **No historical liquidity.** Slippage in the backtest uses today's reserve for every past bar. That mis-states impact for pools whose liquidity changed. It is called out on the backtest report.
- **EIP-55 checksums are still not verified.** Unchanged from Phase 1. EVM addresses are lowercased.
- **Some Base and BSC pool ids are 32 bytes.** During the backtest, GeckoTerminal trending returned ids such as `0xc19d…627fa` (64 hex chars). At that time `normalize_address` only accepted 20-byte EVM addresses, so those pools (likely Uniswap v4) were dropped. Pool identifiers now accept a 20-byte address or a 32-byte id. Token addresses stay 20 bytes. A provider that cannot query a 32-byte id is counted as `unsupported_pool_id`.

## Security

- The paper lock and `test_live_mode_cannot_be_enabled_from_env` are unchanged. New code does not import a signing library (tested). There is no wallet, key, approval, or submit path. Paper fills write `transactions.status = simulated` and raw token amounts of 0; the integer raw columns are for a later on-chain phase that is not built here.
- Screening treats a critical `UNKNOWN` as a block. A vendor timeout is `UNKNOWN`, not a pass. Verified live on 2026-11-09:
  - RugCheck `GET /v1/tokens/{mint}/report` (BONK and a new mint): `mintAuthority` / `freezeAuthority` null means revoked; `lpLockedPct` is 0–100; `risks[].level` is `warn` or `danger`. The normalised score was 7 for BONK and 1 for a new mint. The scale is not used as a gate.
  - GoPlus `GET /api/v1/token_security/{chainId}` on Base and BSC. Taxes are fractions (`"0"` .. `"1"`). `honeypot_with_same_creator` is a count (BRETT returned `"3"`), so it is a non-critical warning. A minutes-old Base token came back with empty taxes and no honeypot flag; that is `UNKNOWN` and blocked. `is_mintable: "1"` (CAKE, DEGEN) is a critical fail. That rejects some established mintable tokens on purpose.
  - GoPlus Solana `GET /api/v1/solana/token_security` is the fallback when RugCheck is down. `mintable.status` / `freezable.status` are `"0"` or `"1"`.
- LP lock is critical only while the pool is under 72 hours old (or age is unknown). Older pools with unknown or unlocked LP are a warning. A two-year-old SOL/USDC pool reports `locked_liquidity_percentage: null`; blocking that would make the check meaningless.
- The API has no authentication. Bind it to 127.0.0.1. Do not put it on a public interface.
- Secrets stay in `SecretStr`. The optional GoPlus key is not sent: the keyless calls succeeded and a guessed auth header would be worse than omitting it.

## What was missing, and what this change adds

Phase 1 had providers, schema and an `ExecutionAdapter` protocol. It did not screen tokens, score a strategy after costs, enforce risk, simulate fills, or show a book. This change adds those pieces on the paper side only. The adapter protocol is still an interface. Strategies do not call it.

## Bottlenecks

- GeckoTerminal's keyless limit dominates scanning and the backtest. The runner rotates one chain and one list (new or trending) per cycle, caps security calls, and fetches OHLCV for at most one screened pool per cycle.
- Stops in the runner are evaluated on the scan snapshot, not on every swap. A wick between scans is invisible, and a gap fills at the snapshot price plus slippage.
- Backtest sample size is a handful of surviving pools and about 3.5 days of 5-minute bars. See the note in `docs/strategy_validation.json`. That is not a long enough record to claim an edge even if a number is positive.
