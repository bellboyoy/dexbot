"""Discovery sources: pluggable adapters that turn a provider feed into leads.

Adding a data source means writing one class with `name`, `every`, `per_chain` and
`fetch`; pipelines do not know which provider is behind a source.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from app.domain.chains import Chain
from app.domain.market import PoolSnapshot
from app.marketdata.dexscreener import DexScreenerProvider
from app.marketdata.geckoterminal import GeckoTerminalProvider
from app.marketdata.scheduling import Priority, PriorityGate


@dataclass
class Lead:
    chain: Chain
    token: str
    source: str            # provider/feed the lead came from
    reason: str            # e.g. geckoterminal_new_pools, dexscreener_boost_paid
    snapshot: PoolSnapshot | None = None    # present when the feed already returned pool data


class DiscoverySource(Protocol):
    name: str
    every: timedelta
    per_chain: bool        # True: fetch([chain]) is called once per chain

    async def fetch(self, chains: list[Chain]) -> list[Lead]: ...


class DexScreenerFeed:
    """Latest token profiles or (paid) boosts. One call covers all chains."""
    per_chain = False

    def __init__(self, ds: DexScreenerProvider, kind: str, every: timedelta = timedelta(seconds=60)):
        self.ds, self.kind, self.every = ds, kind, every
        self.name = f"dexscreener_{kind}"

    async def fetch(self, chains: list[Chain]) -> list[Lead]:
        return [Lead(d.chain, d.token_address, "dexscreener", d.reason)
                for d in await self.ds.discover(chains, kinds=(self.kind,))]


class GeckoPoolFeed:
    """GeckoTerminal pool lists (new_pools, trending, top_pools). Scarce budget: gated."""
    per_chain = True

    def __init__(self, gt: GeckoTerminalProvider, gate: PriorityGate, kind: str, every: timedelta):
        self.gt, self.gate, self.kind, self.every = gt, gate, kind, every
        self.name = f"geckoterminal_{kind}"

    async def fetch(self, chains: list[Chain]) -> list[Lead]:
        (chain,) = chains
        fetch = {"new_pools": self.gt.get_new_pools, "trending": self.gt.get_trending_pools,
                 "top_pools": self.gt.get_top_pools}[self.kind]
        async with self.gate.slot(Priority.DISCOVERY):
            snaps = await fetch(chain)
        return [Lead(chain, s.base_token.address, "geckoterminal", self.name, s) for s in snaps]


class QuoteSeedFeed:
    """Established seeds: each chain's verified quote assets (SOL, WETH, WBNB, stables).
    Their deepest pools anchor the established universe."""
    per_chain = True

    def __init__(self, verified_quotes: dict[Chain, set[str]], every: timedelta = timedelta(hours=6)):
        self.verified, self.every, self.name = verified_quotes, every, "established_seed"

    async def fetch(self, chains: list[Chain]) -> list[Lead]:
        (chain,) = chains
        return [Lead(chain, q, "seed", self.name) for q in sorted(self.verified.get(chain, ()))]
