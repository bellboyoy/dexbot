import pytest

from app.domain.chains import Chain, ChainFamily, InvalidAddress, normalize_address


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
