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


def test_pool_ids_accept_v4_style_ids():
    from app.domain.chains import normalize_pool_id
    v4 = "0x57EDA0aab145bc3ca0e28c778cfcffdcc182776c57dcbadbbe3b497fcf3442d3"
    assert normalize_pool_id(Chain.BASE, v4) == v4.lower()
    assert normalize_pool_id(Chain.BSC, "0x" + "A" * 40) == "0x" + "a" * 40
    with pytest.raises(InvalidAddress):
        normalize_pool_id(Chain.BSC, "0x0ec0EB639863Cc6a235Fae27CF632a1b044AFfff:4meme")   # bonding curve
    with pytest.raises(InvalidAddress):
        normalize_address(Chain.BASE, v4)                     # a 32-byte id is never a token address
