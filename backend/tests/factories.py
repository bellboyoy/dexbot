from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.domain.chains import Chain
from app.domain.market import DataSource, PoolSnapshot, TokenRef, WindowStats

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
WSOL = "So11111111111111111111111111111111111111112"
USDC_SOL = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def sol_addr(n: int) -> str:
    """Deterministic valid-format Solana address (44 base58 chars)."""
    s, x = "", n + 1
    while x:
        x, r = divmod(x, 58)
        s += _B58[r]
    return (s + "1" * 44)[:44]


def snap(*, pool: str | None = None, token: str | None = None, quote: str = WSOL, quote_symbol: str = "SOL",
         liquidity="50000", age=timedelta(hours=2), h1=(30, 20), h24=(800, 700), vol24="500000",
         chain=Chain.SOLANA, source=DataSource.DEXSCREENER, now=NOW) -> PoolSnapshot:
    return PoolSnapshot(
        source=source, chain=chain, pool_address=pool or sol_addr(1), dex_id="raydium",
        base_token=TokenRef(chain, token or sol_addr(2), "TKN"), quote_token=TokenRef(chain, quote, quote_symbol),
        observed_at=now, price_usd=Decimal("0.01"), price_native=None,
        liquidity_usd=None if liquidity is None else Decimal(liquidity), fdv_usd=None, market_cap_usd=None,
        pool_created_at=None if age is None else now - age,
        windows={
            "h1": WindowStats(buys=None if h1 is None else h1[0], sells=None if h1 is None else h1[1]),
            "h24": WindowStats(buys=h24[0], sells=h24[1], volume_usd=Decimal(vol24) if vol24 else None),
        },
    )
