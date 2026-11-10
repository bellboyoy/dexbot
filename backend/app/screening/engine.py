"""Screen a pool before any paper entry. Market filters first, then a security vendor."""
from __future__ import annotations

from datetime import datetime

from app.domain.market import PoolSnapshot
from app.screening.filters import ScreenThresholds, market_checks, pool_is_young
from app.screening.models import ScreenDecision, decide
from app.screening.providers import TokenSecurityClients, security_checks


class ScreeningEngine:
    def __init__(self, clients: TokenSecurityClients, thresholds: ScreenThresholds | None = None):
        self.clients = clients
        self.thresholds = thresholds or ScreenThresholds()

    async def aclose(self) -> None:
        await self.clients.aclose()

    async def screen(self, snap: PoolSnapshot, *, now: datetime, with_security: bool = True) -> ScreenDecision:
        checks = market_checks(snap, self.thresholds, now=now)
        market = decide(checks)
        if not market.allowed or not with_security:
            return market
        young = pool_is_young(snap, self.thresholds, now)
        extra = await security_checks(
            self.clients, snap.chain, snap.base_token.address, young=young, thresholds=self.thresholds,
        )
        return decide(checks + extra)
