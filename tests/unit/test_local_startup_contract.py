from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_local_console_starts_only_control_plane_dry_run_workers() -> None:
    script = (ROOT / "scripts" / "run_local.sh").read_text()

    assert "TRADING_EXECUTION_BACKEND=FREQTRADE" in script
    assert 'TRADING_API_PORT="${TRADING_API_PORT:-8014}"' in script
    assert "TRADING_FREQTRADE_WORKERS_ENABLED=true" in script
    assert "--profile execution-workers up -d" in script
    assert "freqtrade-binance" in script
    assert "freqtrade-hyperliquid" in script
    assert "TRADING_FREQTRADE_LIVE_ORDER_SEND_ENABLED=false" in script
    assert "TRADING_BINANCE_LIVE_ORDER_SEND_ENABLED=false" in script
    assert "TRADING_HYPERLIQUID_LIVE_ORDER_SEND_ENABLED=false" in script
    assert "--profile live-smoke" not in script


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
