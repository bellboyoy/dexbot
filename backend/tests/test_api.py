"""HTTP API surface: paper mode, input validation, and no secrets in responses."""
import pytest
from fastapi.testclient import TestClient

SECRETS = {
    "DEXBOT_ZEROX_API_KEY": "zx-secret-123456",
    "DEXBOT_JUPITER_API_KEY": "jup-secret-123456",
    "DEXBOT_COINGECKO_API_KEY": "cg-secret-123456",
    "DEXBOT_SOLANA_RPC_URL": "https://rpc.example/?api-key=rpc-secret-123456",
}


@pytest.fixture
def client(monkeypatch):
    for k, v in SECRETS.items():
        monkeypatch.setenv(k, v)
    from app.config import get_settings
    get_settings.cache_clear()
    from app.main import app
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


def test_health_is_paper_and_leaks_nothing(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["mode"] == "paper"
    body = r.text + client.get("/openapi.json").text
    for v in SECRETS.values():
        assert v not in body


def test_invalid_address_rejected(client):
    assert client.get("/market/base/pools/not-an-address").status_code == 422
    assert client.get("/market/solana/pools/0x" + "a" * 40).status_code == 422


def test_unknown_chain_rejected(client):
    assert client.get("/market/ethereum/pools/0x" + "a" * 40).status_code == 422


def test_no_trading_or_wallet_routes(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert not [p for p in paths if any(w in p for w in ("trade", "wallet", "order", "live", "sign"))]
