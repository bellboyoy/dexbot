import pytest

from app.domain.chains import (
    Chain, ChainFamily, InvalidAddress, is_extended_pool_id, normalize_address, normalize_pool_id,
    provider_supports_pool_id,
)


def test_families():
    assert Chain.SOLANA.family is ChainFamily.SOLANA
    assert {Chain.BASE.family, Chain.BSC.family, Chain.ARBITRUM.family} == {ChainFamily.EVM}
    assert Chain.BSC.evm_chain_id == 56 and Chain.SOLANA.evm_chain_id is None


def test_evm_address_lowercased():
    a = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    assert normalize_address(Chain.BASE, a) == a.lower()


@pytest.mark.parametrize("bad", ["", "0x123", "833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "0x" + "g" * 40])
def test_evm_address_rejected(bad):
    with pytest.raises(InvalidAddress):
        normalize_address(Chain.BSC, bad)


def test_solana_address_case_preserved():
    a = "So11111111111111111111111111111111111111112"
    assert normalize_address(Chain.SOLANA, a) == a


@pytest.mark.parametrize("bad", ["0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "O0Il" * 10, "short"])
def test_solana_address_rejected(bad):
    with pytest.raises(InvalidAddress):
        normalize_address(Chain.SOLANA, bad)


def test_32_byte_pool_id_is_a_pool_id_not_a_token():
    pool_id = "0x" + "AB" * 32
    token = "0x" + "cd" * 20
    assert is_extended_pool_id(pool_id)
    assert normalize_pool_id(Chain.BASE, pool_id) == pool_id.lower()
    assert len(normalize_pool_id(Chain.BSC, pool_id)) == 66
    assert normalize_pool_id(Chain.BASE, token) == token
    with pytest.raises(InvalidAddress, match="token address"):
        normalize_address(Chain.BASE, pool_id)
    with pytest.raises(InvalidAddress, match="pool id"):
        normalize_pool_id(Chain.BASE, "0x" + "ab" * 20 + "ff")
    assert provider_supports_pool_id("goplus", pool_id.lower()) is False
    assert provider_supports_pool_id("dexscreener", pool_id.lower()) is True
    assert provider_supports_pool_id("geckoterminal", pool_id.lower()) is True
    assert provider_supports_pool_id("goplus", token) is True
