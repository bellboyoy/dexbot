from app.discovery.validation import validate_quote_assets
from app.domain.chains import Chain
from tests.factories import USDC_SOL, WSOL, snap

USDT_SOL = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"


class FakeDS:
    async def get_tokens(self, chain, addrs):
        return [snap(token=WSOL, quote=USDC_SOL, quote_symbol="USDC"),        # WSOL symbol is "TKN" here -> fails
                snap(token=USDT_SOL, quote=USDC_SOL, quote_symbol="USDC")]


async def test_only_symbol_matches_are_verified():
    got = await validate_quote_assets(FakeDS(), [Chain.SOLANA])
    assert got == {Chain.SOLANA: {USDC_SOL}}      # USDT seen only as "TKN", WSOL as "TKN"
