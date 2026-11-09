"""Runtime validation of hardcoded addresses against live data."""
from __future__ import annotations

from app.domain.assets import QUOTE_ASSETS
from app.domain.chains import Chain
from app.logging import get_logger
from app.marketdata.dexscreener import DexScreenerProvider

log = get_logger(__name__)


async def validate_quote_assets(ds: DexScreenerProvider, chains: list[Chain]) -> dict[Chain, set[str]]:
    """Return the quote-asset addresses whose on-DEX symbol matches the expected symbol.

    An address that cannot be confirmed is excluded (and logged as an error) rather than
    trusted.
    """
    verified: dict[Chain, set[str]] = {}
    for chain in chains:
        assets = QUOTE_ASSETS.get(chain, {})
        if not assets:
            continue
        seen: dict[str, set[str]] = {}
        for snap in await ds.get_tokens(chain, list(assets)):
            for tok in (snap.base_token, snap.quote_token):
                if tok is not None and tok.address in assets and tok.symbol:
                    seen.setdefault(tok.address, set()).add(tok.symbol.upper())
        ok = {a for a, asset in assets.items() if seen.get(a, set()) & asset.symbols}
        for a in set(assets) - ok:
            log.error("quote_asset_unverified", chain=chain, address=a, seen_symbols=sorted(seen.get(a, [])))
        verified[chain] = ok
    return verified
