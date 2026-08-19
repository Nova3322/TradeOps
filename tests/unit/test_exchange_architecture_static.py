from __future__ import annotations

import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TARGET_MODULES = (
    ROOT / "src/trading_control_plane/service_domains/accounts.py",
    ROOT / "src/trading_control_plane/service_domains/execution_freqtrade.py",
    ROOT / "src/trading_control_plane/service_domains/execution_facts.py",
    ROOT / "src/trading_control_plane/query_domains/accounts.py",
)
REQUIRED_DELETIONS = (
    "binance.py",
    "hyperliquid.py",
    "okx.py",
    "bybit.py",
    "binance_execution.py",
    "hyperliquid_execution.py",
    "service_domains/execution_venue.py",
)


def _imports(path: Path) -> tuple[tuple[str, tuple[str, ...]], ...]:
    tree = ast.parse(path.read_text())
    result: list[tuple[str, tuple[str, ...]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.extend((item.name, ()) for item in node.names)
        elif isinstance(node, ast.ImportFrom):
            result.append((node.module or "", tuple(item.name for item in node.names)))
    return tuple(result)


def test_target_exchange_modules_have_explicit_imports_and_no_type_suppression() -> None:
    for path in TARGET_MODULES:
        source = path.read_text()
        assert "# mypy: disable-error-code=attr-defined" not in source, path
        assert "# ruff: noqa: F403, F405" not in source, path
        assert not any(
            module in {"trading_control_plane.service_core", "trading_control_plane.query_core"}
            and "*" in names
            for module, names in _imports(path)
        ), path


def test_domain_does_not_import_api_ccxt_or_freqtrade_transport() -> None:
    forbidden = {
        "ccxt",
        "ccxt.pro",
        "trading_control_plane.api",
        "trading_control_plane.api_core",
        "trading_control_plane.freqtrade",
    }
    for path in (ROOT / "src/trading_control_plane/service_domains").glob("*.py"):
        imported = {module for module, _names in _imports(path)}
        assert not imported.intersection(forbidden), path


def test_ccxt_imports_remain_inside_fact_and_capital_adapter_boundaries() -> None:
    allowed = {
        ROOT / "src/trading_control_plane/adapters/facts.py",
        ROOT / "src/trading_control_plane/adapters/capital.py",
    }
    for path in (ROOT / "src/trading_control_plane").rglob("*.py"):
        imported = {module for module, _names in _imports(path)}
        if {"ccxt", "ccxt.pro"}.intersection(imported):
            assert path in allowed, path


def test_retired_exchange_modules_and_direct_backend_markers_stay_absent() -> None:
    package = ROOT / "src/trading_control_plane"
    for relative in REQUIRED_DELETIONS:
        assert not (package / relative).exists(), relative

    searched = tuple(
        ROOT / name for name in ("src", "scripts", ".env.example", "compose.yaml")
    )
    forbidden = (
        "DIRECT_LEGACY",
        "BinanceReadOnlyClient",
        "HyperliquidReadOnlyClient",
        "VenueCommandExecutionService",
        "/api/venues/binance/sync",
        "/api/venues/hyperliquid/sync",
    )
    for root in searched:
        paths = root.rglob("*") if root.is_dir() else (root,)
        for path in paths:
            if path.is_file() and path.suffix not in {".pyc", ".woff2", ".png"}:
                source = path.read_text(errors="ignore")
                assert not any(marker in source for marker in forbidden), path


def test_all_freqtrade_configs_use_built_in_sync_and_async_rate_limiting() -> None:
    configs = tuple((ROOT / "freqtrade").glob("config*.json"))
    assert configs
    for path in configs:
        payload = json.loads(path.read_text())
        exchange = payload["exchange"]
        assert exchange["ccxt_config"]["enableRateLimit"] is True, path
        assert exchange["ccxt_async_config"]["enableRateLimit"] is True, path
        assert "rateLimit" not in exchange["ccxt_config"], path
        assert "rateLimit" not in exchange["ccxt_async_config"], path
        assert payload["process_only_new_candles"] is True, path
        assert 1 <= len(exchange["pair_whitelist"]) <= 2, path
