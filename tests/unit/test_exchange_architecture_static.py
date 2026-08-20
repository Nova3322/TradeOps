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

    searched = tuple(ROOT / name for name in ("src", "scripts", ".env.example", "compose.yaml"))
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


def test_live_freqtrade_workers_start_stopped_and_never_cancel_external_orders_on_exit() -> None:
    configs = tuple((ROOT / "freqtrade").glob("config-*-live-smoke.json"))
    assert {path.name for path in configs} == {
        "config-binance-live-smoke.json",
        "config-hyperliquid-live-smoke.json",
    }
    for path in configs:
        payload = json.loads(path.read_text())
        assert payload["initial_state"] == "stopped", path
        assert payload["force_entry_enable"] is False, path
        assert payload["cancel_open_orders_on_exit"] is False, path
        assert payload["telegram"]["enabled"] is False, path


def test_bybit_external_testnet_worker_cannot_fall_back_to_mainnet_or_demo() -> None:
    payload = json.loads((ROOT / "freqtrade" / "config-bybit-testnet.json").read_text())
    assert payload["bot_name"] == "tradeops-bybit-testnet"
    assert payload["dry_run"] is False
    assert payload["initial_state"] == "stopped"
    assert payload["force_entry_enable"] is False
    assert payload["cancel_open_orders_on_exit"] is False
    assert payload["exchange"]["name"] == "bybit"
    assert payload["exchange"]["demo_trading"] is False
    for key in ("ccxt_config", "ccxt_async_config"):
        urls = payload["exchange"][key]["urls"]["api"]
        assert set(urls) == {"spot", "futures", "v2", "public", "private"}
        assert set(urls.values()) == {"https://api-testnet.bybit.com"}
        assert not any(value == "https://api.bybit.com" for value in urls.values())
    source = (ROOT / "compose.yaml").read_text()
    assert "freqtrade-bybit-testnet:" in source
    assert 'FREQTRADE__DRY_RUN: "false"' in source
    assert "./freqtrade/config-bybit-testnet.json:/freqtrade/config.json:ro" in source


def test_binance_live_freqtrade_worker_corrects_exchange_clock_skew() -> None:
    payload = json.loads(
        (ROOT / "freqtrade" / "config-binance-live-smoke.json").read_text()
    )
    assert payload["exchange"]["ccxt_config"]["options"]["adjustForTimeDifference"] is True
    assert payload["exchange"]["ccxt_config"]["options"]["recvWindow"] == 60_000
    assert payload["exchange"]["ccxt_config"]["options"]["papi"] is False
    assert (
        payload["exchange"]["ccxt_async_config"]["options"]["adjustForTimeDifference"]
        is True
    )
    assert payload["exchange"]["ccxt_async_config"]["options"]["recvWindow"] == 60_000
    assert payload["exchange"]["ccxt_async_config"]["options"]["papi"] is False


def test_compose_forwards_production_integration_switches_explicitly() -> None:
    source = (ROOT / "compose.yaml").read_text()
    for name in (
        "TRADING_ENVIRONMENT",
        "TRADING_RUNTIME_BINANCE_SYMBOL",
        "TRADING_RUNTIME_HYPERLIQUID_SYMBOL",
        "TRADING_RUNTIME_OKX_SYMBOL",
        "TRADING_RUNTIME_BYBIT_SYMBOL",
        "TRADING_FREQTRADE_WORKERS_ENABLED",
        "TRADING_TELEGRAM_ENABLED",
        "TRADING_TELEGRAM_BOT_TOKEN",
        "TRADING_BINANCE_CAPITAL_API_KEY",
        "TRADING_BINANCE_CAPITAL_API_SECRET",
        "TRADING_BINANCE_CAPITAL_ACCOUNT_ID",
        "TRADING_NOTILT_ENABLED",
        "TRADING_NOTILT_AGENT_ADDRESS",
        "TRADING_NOTILT_ARBITRUM_VAULT_ADDRESS",
        "TRADING_HYPERLIQUID_ACCOUNT_ADDRESS",
    ):
        assert f"{name}: ${{{name}" in source


def test_validation_branch_push_runs_ci_without_a_pull_request() -> None:
    expected = "branches: [main, codex/trading-production-validation]"
    for workflow in ("ci.yml", "security.yml"):
        source = (ROOT / ".github" / "workflows" / workflow).read_text()
        assert expected in source


def test_execution_route_delegates_transport_orchestration_to_application_use_case() -> None:
    route = ROOT / "src/trading_control_plane/api_routes/execution.py"
    application = ROOT / "src/trading_control_plane/execution_dispatch.py"
    route_imports = {module for module, _names in _imports(route)}
    assert "trading_control_plane.freqtrade" not in route_imports
    assert "trading_control_plane.freqtrade_contracts" not in route_imports
    forbidden_attributes = {
        "find_open_trade",
        "force_enter",
        "force_exit",
        "probe",
        "recover_entry",
        "recover_exit",
        "spec",
    }
    route_attributes = {
        node.attr
        for node in ast.walk(ast.parse(route.read_text()))
        if isinstance(node, ast.Attribute)
    }
    assert route_attributes.isdisjoint(forbidden_attributes)

    application_imports = {module for module, _names in _imports(application)}
    assert not any(
        module == boundary or module.startswith(f"{boundary}.")
        for module in application_imports
        for boundary in {"fastapi", "trading_control_plane.api", "trading_control_plane.api_routes"}
    )


def test_connection_verification_route_uses_server_side_application_boundary() -> None:
    route = ROOT / "src/trading_control_plane/api_routes/accounts.py"
    application = ROOT / "src/trading_control_plane/exchange_connection_verification.py"
    source = route.read_text()
    assert "exchange_connection_verifier" not in source
    assert "prepare_exchange_account_connection_verification" not in source
    assert "record_exchange_account_connection_verification" not in source
    assert "self.connection_verification.verify(" in source
    application_imports = {module for module, _names in _imports(application)}
    assert not any(
        module == boundary or module.startswith(f"{boundary}.")
        for module in application_imports
        for boundary in {"fastapi", "trading_control_plane.api", "trading_control_plane.api_routes"}
    )


def test_capital_route_has_no_direct_transport_dependency() -> None:
    route = ROOT / "src/trading_control_plane/api_routes/capital.py"
    applications = (
        ROOT / "src/trading_control_plane/capital_application.py",
        ROOT / "src/trading_control_plane/capital_configuration_use_cases.py",
        ROOT / "src/trading_control_plane/capital_direct_use_cases.py",
        ROOT / "src/trading_control_plane/capital_receipt_use_cases.py",
        ROOT / "src/trading_control_plane/capital_transfer_use_cases.py",
    )
    route_imports = {module for module, _names in _imports(route)}
    forbidden_imports = {
        "trading_control_plane.adapters.capital",
        "trading_control_plane.capital",
        "trading_control_plane.notilt",
        "trading_control_plane.safe_spending",
    }
    assert route_imports.isdisjoint(forbidden_imports)
    source = route.read_text()
    assert "capital_adapter_resolver" not in source
    assert "resolved_capital_transfer" not in source
    assert "resolved_notilt" not in source
    assert "resolved_safe_spending" not in source
    route_attributes = {
        node.attr for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Attribute)
    }
    assert route_attributes.isdisjoint(
        {
            "adapter_resolver",
            "execute_mapping",
            "execute_string",
            "prepare_deposit",
            "prepare_release_execution",
            "prepare_release_request",
            "prepare_spend",
            "submit",
            "verify_receipt",
        }
    )

    for application in applications:
        application_imports = {module for module, _names in _imports(application)}
        assert not any(
            module == boundary or module.startswith(f"{boundary}.")
            for module in application_imports
            for boundary in {
                "fastapi",
                "trading_control_plane.api",
                "trading_control_plane.api_routes",
                "trading_control_plane.api_schemas",
            }
        ), application
        assert not any(
            isinstance(node, ast.Name) and node.id == "Any"
            for node in ast.walk(ast.parse(application.read_text()))
        ), application


def test_direct_capital_domain_lifecycle_ownership_is_split_without_forwarders() -> None:
    aggregate = ROOT / "src/trading_control_plane/service_domains/capital_direct.py"
    tree = ast.parse(aggregate.read_text())
    service = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "DirectOperationCapitalService"
    )
    assert len(service.bases) == 4
    assert not any(isinstance(node, ast.FunctionDef) for node in service.body)

    expected_owners = {
        "capital_direct_configuration.py": {
            "set_direct_capital_configuration",
            "create_direct_capital_operation",
        },
        "capital_direct_preview.py": {
            "record_direct_capital_unsigned_preview",
            "record_direct_capital_safe_preview",
        },
        "capital_direct_submission.py": {
            "record_direct_capital_wallet_submission",
            "record_direct_capital_binance_submission",
        },
        "capital_direct_receipt.py": {
            "record_direct_capital_notilt_receipt",
            "record_direct_capital_hyperliquid_receipt",
        },
    }
    for filename, expected in expected_owners.items():
        module = ast.parse(
            (ROOT / "src/trading_control_plane/service_domains" / filename).read_text()
        )
        methods = {node.name for node in ast.walk(module) if isinstance(node, ast.FunctionDef)}
        assert expected.issubset(methods), filename
