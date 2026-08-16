from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_local_worker_configs_cannot_trade_or_adjust_positions_autonomously() -> None:
    binance = json.loads((ROOT / "freqtrade" / "config-binance.json").read_text())
    hyperliquid = json.loads((ROOT / "freqtrade" / "config-hyperliquid.json").read_text())

    for config in (binance, hyperliquid):
        assert config["dry_run"] is True
        assert config["force_entry_enable"] is False
        assert config["position_adjustment_enable"] is False

    assert binance["exchange"]["name"] == "binance"
    assert hyperliquid["exchange"]["name"] == "hyperliquid"
    assert hyperliquid["exchange"]["hip3_dexes"] == ["xyz"]
