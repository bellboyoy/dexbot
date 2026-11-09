"""Execution adapter interface shared by paper, Solana (Jupiter) and EVM (0x, PancakeSwap).

Only interfaces live here in Phase 1. Implementations arrive in Phase 6 (paper, with real
quotes) and Phase 7 (on-chain, simulation only). No adapter signs anything itself: signing
is delegated to the isolated wallet service, which re-validates every payload.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from app.domain.chains import Chain
from app.domain.market import TokenRef


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class TxStatus(StrEnum):
    BUILT = "built"
    SIMULATED = "simulated"
    SUBMITTED = "submitted"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    DROPPED = "dropped"      # expired blockhash / replaced nonce / never mined


@dataclass(frozen=True)
class QuoteRequest:
    chain: Chain
    side: Side
    token: TokenRef            # the traded token
    quote_token: TokenRef      # what we pay with / receive (e.g. USDC, WSOL, WBNB)
    amount_in_raw: int         # smallest units of the input token
    max_slippage_bps: int
    idempotency_key: str


@dataclass(frozen=True)
class Quote:
    request: QuoteRequest
    provider: str              # "jupiter", "0x", "pancakeswap", "paper"
    amount_out_raw: int
    min_amount_out_raw: int
    price_impact_bps: int | None   # None = unknown, which the risk engine treats as unacceptable
    route_summary: str
    fees_usd: Decimal | None
    expires_at: datetime
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class CostEstimate:
    gas_usd: Decimal
    protocol_fees_usd: Decimal
    expected_slippage_usd: Decimal
    price_impact_usd: Decimal

    @property
    def total_usd(self) -> Decimal:
        return self.gas_usd + self.protocol_fees_usd + self.expected_slippage_usd + self.price_impact_usd


@dataclass(frozen=True)
class ExitRoute:
    available: bool
    quote: Quote | None
    estimated_exit_cost: CostEstimate | None
    reason: str | None = None


@dataclass(frozen=True)
class PreparedTx:
    chain: Chain
    quote: Quote
    deadline: datetime
    payload: dict[str, Any]    # chain-specific unsigned tx; never contains keys
    idempotency_key: str


@dataclass(frozen=True)
class SimulationResult:
    success: bool
    expected_amount_out_raw: int | None
    gas_used: int | None
    error: str | None = None


@dataclass(frozen=True)
class SubmittedTx:
    chain: Chain
    tx_id: str                 # EVM hash or Solana signature
    submitted_at: datetime
    idempotency_key: str


@dataclass(frozen=True)
class TxOutcome:
    status: TxStatus
    tx_id: str | None
    amount_in_raw: int | None
    amount_out_raw: int | None
    gas_paid_usd: Decimal | None
    error: str | None = None


@dataclass(frozen=True)
class Balance:
    token: TokenRef
    amount_raw: int
    decimals: int
    observed_at: datetime


@dataclass(frozen=True)
class ReconciliationResult:
    consistent: bool
    expected_amount_raw: int
    actual_amount_raw: int
    notes: str = ""


@runtime_checkable
class ExecutionAdapter(Protocol):
    chain: Chain
    name: str

    async def get_quote(self, req: QuoteRequest) -> Quote: ...
    async def check_exit_route(self, token: TokenRef, amount_raw: int) -> ExitRoute: ...
    async def estimate_costs(self, quote: Quote) -> CostEstimate: ...
    async def build(self, quote: Quote, deadline: datetime) -> PreparedTx: ...
    async def simulate(self, tx: PreparedTx) -> SimulationResult: ...
    async def submit(self, tx: PreparedTx) -> SubmittedTx: ...
    async def confirm(self, tx: SubmittedTx, timeout_s: float) -> TxOutcome: ...
    async def get_balance(self, token: TokenRef) -> Balance: ...
    async def reconcile(self, token: TokenRef, expected_amount_raw: int) -> ReconciliationResult: ...
