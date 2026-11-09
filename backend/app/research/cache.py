"""Disk cache for GeckoTerminal payloads and merged candles.

Reruns read this directory and do not call the network. The directory is gitignored.
"""
from __future__ import annotations

import json
from pathlib import Path


def cache_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "docs" / "ARCHITECTURE.md").exists():
            root = parent / "data" / "market_cache"
            root.mkdir(parents=True, exist_ok=True)
            return root
    root = here.parents[3] / "data" / "market_cache"
    root.mkdir(parents=True, exist_ok=True)
    return root


def read_json(relative: str):
    path = cache_root() / relative
    if not path.exists():
        return None
    return json.loads(path.read_text())


def write_json(relative: str, payload) -> Path:
    path = cache_root() / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))
    return path
