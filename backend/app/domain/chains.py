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
_EVM_POOL_ID = re.compile(r"^0x[0-9a-fA-F]{64}$")      # Uniswap v4-style 32-byte pool ids
_B58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


class InvalidAddress(ValueError):
    pass


def normalize_address(chain: Chain, address: str) -> str:
    """Validate the address format for the chain and return its canonical form.

    EVM addresses are canonicalised to lowercase here; EIP-55 checksum verification is
    added with web3 in the security phase. Solana addresses are case-sensitive base58
    and are returned unchanged. This is a format check only: it does not prove that the
    address is a token contract (the security engine does that on-chain).
    """
    if not isinstance(address, str):
        raise InvalidAddress(f"address must be a string, got {type(address).__name__}")
    address = address.strip()
    if chain.family is ChainFamily.EVM:
        if not _EVM_ADDR.match(address):
            raise InvalidAddress(f"invalid EVM address for {chain}: {address!r}")
        return address.lower()
    if not _B58.match(address):
        raise InvalidAddress(f"invalid Solana address: {address!r}")
    return address


def chain_from_provider_id(mapping: dict[Chain, str], provider_id: str) -> Chain | None:
    for chain, pid in mapping.items():
        if pid == provider_id:
            return chain
    return None


def normalize_pool_id(chain: Chain, pool_id: str) -> str:
    """Validate a pool identifier. On EVM chains this is either a pool contract address or,
    for singleton-style DEXes (Uniswap v4, PancakeSwap Infinity), a 32-byte pool id.

    Pseudo-pairs such as launchpad bonding curves (DexScreener ids like "0x...:4meme") are
    rejected: they are not DEX pools and cannot be traded through normal routers.
    """
    if chain.family is ChainFamily.EVM and isinstance(pool_id, str) and _EVM_POOL_ID.match(pool_id.strip()):
        return pool_id.strip().lower()
    return normalize_address(chain, pool_id)
