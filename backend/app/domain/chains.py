"""Supported blockchains and per-provider identifiers.

Networks do not share a transaction model: Solana is account-based with blockhash
expiry; EVM chains use nonces, gas and ERC-20 approvals. Code that differs by chain
lives in adapters, keyed on `ChainFamily`.
"""
from __future__ import annotations

import re
from enum import StrEnum


class ChainFamily(StrEnum):
    SOLANA = "solana"
    EVM = "evm"


class Chain(StrEnum):
    SOLANA = "solana"
    BASE = "base"
    BSC = "bsc"
    ARBITRUM = "arbitrum"

    @property
    def family(self) -> ChainFamily:
        return ChainFamily.SOLANA if self is Chain.SOLANA else ChainFamily.EVM

    @property
    def evm_chain_id(self) -> int | None:
        return {Chain.BASE: 8453, Chain.BSC: 56, Chain.ARBITRUM: 42161}.get(self)


# Provider ids, verified against live API responses on 2026-11-09.
DEXSCREENER_CHAIN_IDS: dict[Chain, str] = {
    Chain.SOLANA: "solana",
    Chain.BASE: "base",
    Chain.BSC: "bsc",
    Chain.ARBITRUM: "arbitrum",
}
GECKOTERMINAL_NETWORK_IDS: dict[Chain, str] = {
    Chain.SOLANA: "solana",
    Chain.BASE: "base",
    Chain.BSC: "bsc",
    Chain.ARBITRUM: "arbitrum",
}

_EVM_ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")
# Uniswap v4 / PancakeSwap Infinity pool ids are 32 bytes (64 hex chars), not 20-byte token addresses.
_EVM_POOL_ID = re.compile(r"^0x[0-9a-fA-F]{64}$")
_B58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


class InvalidAddress(ValueError):
    pass


def _as_text(address: object) -> str:
    if not isinstance(address, str):
        raise InvalidAddress(f"address must be a string, got {type(address).__name__}")
    return address.strip()


def is_extended_pool_id(address: str) -> bool:
    """True for a 32-byte EVM pool id. These are not token contracts."""
    return bool(_EVM_POOL_ID.match(address.strip()))


def normalize_address(chain: Chain, address: str) -> str:
    """Validate a token address and return its canonical form.

    EVM token addresses are 20 bytes, canonicalised to lowercase. A 32-byte pool id is
    not a token address and is rejected here. EIP-55 checksum verification is added with
    web3 in the security phase. Solana addresses are case-sensitive base58 and are
    returned unchanged. This is a format check only.
    """
    address = _as_text(address)
    if chain.family is ChainFamily.EVM:
        if not _EVM_ADDR.match(address):
            raise InvalidAddress(f"invalid EVM token address for {chain}: {address!r}")
        return address.lower()
    if not _B58.match(address):
        raise InvalidAddress(f"invalid Solana address: {address!r}")
    return address


def normalize_pool_id(chain: Chain, address: str) -> str:
    """Validate a pool identifier. EVM pools may be a 20-byte address or a 32-byte id."""
    address = _as_text(address)
    if chain.family is ChainFamily.EVM:
        if _EVM_ADDR.match(address) or _EVM_POOL_ID.match(address):
            return address.lower()
        raise InvalidAddress(f"invalid EVM pool id for {chain}: {address!r}")
    return normalize_address(chain, address)


# Pool lookups on these providers accept the 32-byte ids they themselves publish.
# GoPlus screens token contracts only.
_POOL_ID_PROVIDERS = frozenset({"dexscreener", "geckoterminal"})


def provider_supports_pool_id(provider: str, pool_id: str) -> bool:
    if not is_extended_pool_id(pool_id):
        return True
    return provider in _POOL_ID_PROVIDERS


def chain_from_provider_id(mapping: dict[Chain, str], provider_id: str) -> Chain | None:
    for chain, pid in mapping.items():
        if pid == provider_id:
            return chain
    return None
