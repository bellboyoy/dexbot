from app.config import Settings, TradingMode
from app.domain.chains import Chain


def test_chains_from_env(monkeypatch):
    monkeypatch.setenv("DEXBOT_ENABLED_CHAINS", "solana, base")
    assert Settings().enabled_chains == [Chain.SOLANA, Chain.BASE]


def test_live_mode_cannot_be_enabled_from_env(monkeypatch):
    monkeypatch.setenv("DEXBOT_TRADING_MODE", "live")
    assert Settings().trading_mode is TradingMode.PAPER


def test_secrets_not_rendered(monkeypatch):
    monkeypatch.setenv("DEXBOT_ZEROX_API_KEY", "supersecret")
    s = Settings()
    assert "supersecret" not in repr(s) and s.zerox_api_key.get_secret_value() == "supersecret"
