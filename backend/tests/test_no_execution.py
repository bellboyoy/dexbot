"""The new paper path must not grow a signing or live-execution import."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app"
BANNED = ("web3", "solders", "eth_account", "nacl", "solana.keypair", "solana.transaction")


def test_paper_modules_do_not_import_signing_libraries():
    for path in ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                assert not any(name == banned or name.startswith(banned + ".") for banned in BANNED), name
