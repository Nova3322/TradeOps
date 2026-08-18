from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SETUP_LOCAL_SPEC = importlib.util.spec_from_file_location(
    "trading_setup_local",
    ROOT / "scripts" / "setup_local.py",
)
assert SETUP_LOCAL_SPEC is not None and SETUP_LOCAL_SPEC.loader is not None
SETUP_LOCAL = importlib.util.module_from_spec(SETUP_LOCAL_SPEC)
SETUP_LOCAL_SPEC.loader.exec_module(SETUP_LOCAL)
_preserved_enabled_capability_gates = SETUP_LOCAL._preserved_enabled_capability_gates


def test_local_worker_configs_cannot_trade_or_adjust_positions_autonomously() -> None:
    binance = json.loads((ROOT / "freqtrade" / "config-binance.json").read_text())
    hyperliquid = json.loads((ROOT / "freqtrade" / "config-hyperliquid.json").read_text())

    for config in (binance, hyperliquid):
        assert config["dry_run"] is True
        assert config["force_entry_enable"] is True
        assert config["position_adjustment_enable"] is True
        assert all("*" not in pair for pair in config["exchange"]["pair_whitelist"])

    assert binance["exchange"]["name"] == "binance"
    assert hyperliquid["exchange"]["name"] == "hyperliquid"
    assert hyperliquid["exchange"]["hip3_dexes"] == ["xyz"]


def test_local_startup_can_preserve_an_explicitly_authorized_capital_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "TRADING_LOCAL_PRESERVE_ENABLED_CAPABILITY_GATES",
        " CAPITAL_TRANSFER ",
    )

    assert _preserved_enabled_capability_gates() == frozenset({"CAPITAL_TRANSFER"})


def test_local_startup_preserves_no_capability_gates_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TRADING_LOCAL_PRESERVE_ENABLED_CAPABILITY_GATES", raising=False)

    assert _preserved_enabled_capability_gates() == frozenset()


def test_local_startup_rejects_unknown_preserved_capability_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "TRADING_LOCAL_PRESERVE_ENABLED_CAPABILITY_GATES",
        "CAPITAL_TRANSFER,UNKNOWN_GATE",
    )

    with pytest.raises(RuntimeError, match="unknown gates: UNKNOWN_GATE"):
        _preserved_enabled_capability_gates()
