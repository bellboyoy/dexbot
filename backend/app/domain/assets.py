"""Quote assets a pool may be priced in for the token to count as exitable.

A pool quoted in another small token (e.g. MEME/OTHERMEME) has no reliable exit to
stable value, so screening rejects it. Addresses are verified at runtime by
`validate_quote_assets` against DexScreener (symbol must match) before the collector
uses them; this list alone is not trusted.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.domain.chains import Chain, normalize_address


@dataclass(frozen=True)
class QuoteAsset:
    chain: Chain
    address: str
    symbols: frozenset[str]      # accepted symbols (wrapped and unwrapped spellings)


_RAW: list[tuple[Chain, str, set[str]]] = [
    (Chain.SOLANA, "So11111111111111111111111111111111111111112", {"SOL", "WSOL"}),
    (Chain.SOLANA, "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", {"USDC"}),
    (Chain.SOLANA, "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB", {"USDT"}),
    (Chain.BASE, "0x4200000000000000000000000000000000000006", {"WETH", "ETH"}),
    (Chain.BASE, "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", {"USDC"}),
    (Chain.BSC, "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c", {"WBNB", "BNB"}),
    (Chain.BSC, "0x55d398326f99059fF775485246999027B3197955", {"USDT", "BSC-USD"}),
    (Chain.BSC, "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d", {"USDC"}),
    (Chain.ARBITRUM, "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1", {"WETH", "ETH"}),
    (Chain.ARBITRUM, "0xaf88d065e77c8cC2239327C5EDb3A432268e5831", {"USDC"}),
]

QUOTE_ASSETS: dict[Chain, dict[str, QuoteAsset]] = {}
for _chain, _addr, _syms in _RAW:
    _a = normalize_address(_chain, _addr)
    QUOTE_ASSETS.setdefault(_chain, {})[_a] = QuoteAsset(_chain, _a, frozenset(_syms))


def is_quote_asset(chain: Chain, address: str | None) -> bool:
    return address is not None and address in QUOTE_ASSETS.get(chain, {})
