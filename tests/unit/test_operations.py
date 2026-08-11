from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import pytest

import trading_control_plane.operations as operations_module
from trading_control_plane.config import Settings
from trading_control_plane.operations import build_diagnostic_report, connection_capability_matrix


def encryption_key() -> str:
    return base64.urlsafe_b64encode(b"operations-doctor-key-32-bytes!!"[:32]).decode().rstrip("=")


def safe_settings(**overrides: object) -> Settings:
    return Settings(
        environment="local",
        database_url="postgresql+psycopg://user:secret@127.0.0.1/trading_test",
        session_signing_secret="doctor-session-secret-that-is-not-reported",  # noqa: S106
        credential_encryption_key=encryption_key(),
        _env_file=None,
        **overrides,
    )


def test_doctor_report_is_secret_free_and_default_safe_without_database() -> None:
    settings = safe_settings()

    report = build_diagnostic_report(settings, database=None)
    serialized = json.dumps(report, sort_keys=True)

    assert report["status"] == "READY"
    assert report["database"]["status"] == "SKIPPED"
    assert report["dangerous_controls"]["default_safe"] is True
    assert "doctor-session-secret" not in serialized
    assert "user:secret" not in serialized
    assert encryption_key() not in serialized


def test_connection_matrix_distinguishes_one_time_checks_from_continuous_runtime() -> None:
    matrix = {
        item["capability"]: item
        for item in connection_capability_matrix(
            safe_settings(
                runtime_sync_enabled=True,
                binance_read_only_enabled=True,
                binance_api_key="read-key",
                binance_api_secret="read-secret",  # noqa: S106 - inert fixture
                runtime_binance_account_id="binance-main",
            )
        )
    }

    assert matrix["TEAM_ACCOUNT_CONNECTION_CHECK"]["providers"] == [
        "BINANCE",
        "HYPERLIQUID",
        "OKX",
        "BYBIT",
    ]
    assert matrix["BINANCE_CONTINUOUS_FACTS"]["deployment_state"] == "ENABLED"
    assert matrix["OKX_CONTINUOUS_FACTS"]["implementation"] == ("IMPLEMENTED_TEAM_ACCOUNT_BOUND")
    assert matrix["BYBIT_CONTINUOUS_FACTS"]["implementation"] == ("IMPLEMENTED_TEAM_ACCOUNT_BOUND")
    assert matrix["OKX_CONTINUOUS_FACTS"]["deployment_state"] == "DISABLED"
    assert matrix["FREQTRADE_EXECUTION"]["providers"] == [
        "BINANCE",
        "HYPERLIQUID",
        "OKX",
        "BYBIT",
    ]
    assert matrix["OKX_BYBIT_EXECUTION"]["implementation"] == (
        "IMPLEMENTED_VIA_FREQTRADE_EXECUTION_UNCERTIFIED"
    )
    assert matrix["OKX_BYBIT_EXECUTION"]["deployment_state"] == "DISABLED"
    assert matrix["TEAM_NOTIFICATIONS"]["provider_controls"]["EMAIL"] == (
        "BLOCKED_NO_SMTP_ALLOWLIST"
    )
    assert "API only enqueues" in matrix["TEAM_NOTIFICATIONS"]["boundary"]
    assert "exact-account ELIGIBLE status" in matrix["FREQTRADE_EXECUTION"]["boundary"]
    assert matrix["CAPITAL_SIGNING_BROADCAST"]["implementation"] == "NOT_IN_CONTROL_PLANE"


def test_connection_matrix_reports_explicit_email_smtp_allowlist() -> None:
    matrix = {
        item["capability"]: item
        for item in connection_capability_matrix(
            safe_settings(notification_email_smtp_allowed_hosts="smtp.example.com")
        )
    }

    assert matrix["TEAM_NOTIFICATIONS"]["provider_controls"]["EMAIL"] == ("ALLOWLIST_CONFIGURED")


def test_connection_matrix_projects_database_bindings_behind_process_master_switch() -> None:
    stopped = {
        item["capability"]: item
        for item in connection_capability_matrix(
            safe_settings(runtime_sync_enabled=False),
            database_binding_counts={"BINANCE": 2, "OKX": 1, "BYBIT": 1},
        )
    }
    running = {
        item["capability"]: item
        for item in connection_capability_matrix(
            safe_settings(runtime_sync_enabled=True),
            database_binding_counts={"BINANCE": 2, "OKX": 1, "BYBIT": 1},
        )
    }

    assert stopped["BINANCE_CONTINUOUS_FACTS"]["deployment_state"] == ("DISABLED_CONFIGURED")
    assert running["BINANCE_CONTINUOUS_FACTS"]["deployment_state"] == "ENABLED"
    assert stopped["OKX_CONTINUOUS_FACTS"]["deployment_state"] == ("DISABLED_CONFIGURED")
    assert running["OKX_CONTINUOUS_FACTS"]["deployment_state"] == "ENABLED"
    assert running["BYBIT_CONTINUOUS_FACTS"]["deployment_state"] == "ENABLED"


def test_connection_matrix_projects_okx_bybit_workers_without_a_second_oms() -> None:
    matrix = {
        item["capability"]: item
        for item in connection_capability_matrix(
            safe_settings(freqtrade_live_order_send_enabled=False),
            freqtrade_binding_counts={"OKX": 1, "BYBIT": 1},
        )
    }

    assert matrix["FREQTRADE_EXECUTION"]["deployment_state"] == "DISABLED_CONFIGURED"
    assert matrix["OKX_BYBIT_EXECUTION"]["deployment_state"] == "DISABLED_CONFIGURED"
    assert matrix["OKX_BYBIT_EXECUTION"]["external_side_effect"] == "ORDER_SEND"
    assert "never creates a second venue OMS" in matrix["OKX_BYBIT_EXECUTION"]["boundary"]


def test_environment_template_names_every_settings_field_without_values_from_runtime() -> None:
    template = Path(".env.example").read_text(encoding="utf-8")
    declared = set(re.findall(r"^([A-Z][A-Z0-9_]*)=", template, re.MULTILINE))
    expected = {f"TRADING_{name.upper()}" for name in Settings.model_fields}

    assert expected <= declared


def test_notification_worker_compose_contract_is_explicit_and_hardened() -> None:
    compose = Path("compose.yaml").read_text(encoding="utf-8")
    service = compose.split("  notification-worker:\n", 1)[1].split("\n  freqtrade-binance:", 1)[0]
    launcher = Path("scripts/run_compose.sh").read_text(encoding="utf-8")

    assert 'profiles: ["notifications"]' in service
    assert 'TRADING_NOTIFICATION_WORKER_ENABLED: "true"' in service
    assert "TRADING_NOTIFICATION_EMAIL_SMTP_ALLOWED_HOSTS" in compose
    assert "trading-notification-worker\n        - --healthcheck" in service
    assert "read_only: true" in service
    assert 'cap_drop: ["ALL"]' in service
    assert 'security_opt: ["no-new-privileges:true"]' in service
    assert "restart: unless-stopped" in service
    assert "--notifications)" in launcher
    assert "profiles+=(--profile notifications)" in launcher
    assert "Notification delivery: disabled" in launcher


def test_runtime_worker_compose_contract_is_explicit_hardened_and_default_off() -> None:
    compose = Path("compose.yaml").read_text(encoding="utf-8")
    service = compose.split("  runtime-worker:\n", 1)[1].split("\n  notification-worker:", 1)[0]
    launcher = Path("scripts/run_compose.sh").read_text(encoding="utf-8")

    assert 'profiles: ["runtime"]' in service
    assert 'TRADING_RUNTIME_SYNC_ENABLED: "true"' in service
    assert "TRADING_PERPTAPE_WEBSOCKET_ENABLED" in service
    assert "trading-sync-worker\n        - --healthcheck" in service
    assert "read_only: true" in service
    assert 'cap_drop: ["ALL"]' in service
    assert 'security_opt: ["no-new-privileges:true"]' in service
    assert "restart: unless-stopped" in service
    assert "--runtime)" in launcher
    assert "profiles+=(--profile runtime)" in launcher
    assert "Read-only runtime synchronization: disabled" in launcher
    assert "[--runtime] [--notifications]" in launcher


def test_doctor_marks_enabled_but_incomplete_connection_as_blocked() -> None:
    report = build_diagnostic_report(
        safe_settings(
            runtime_sync_enabled=True,
            hyperliquid_read_only_enabled=True,
            runtime_hyperliquid_account_id="hyperliquid-main",
        ),
        database=None,
    )

    assert report["status"] == "BLOCKED"
    assert any(
        item["deployment_state"] == "BLOCKED_MISCONFIGURED"
        for item in report["connection_capability_matrix"]
    )


def test_doctor_cli_emits_compact_report_and_does_not_open_database(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(operations_module, "get_settings", safe_settings)
    monkeypatch.setattr(
        operations_module,
        "Database",
        lambda _url: pytest.fail("database must be skipped"),
    )

    assert operations_module.main(["--skip-database", "--compact"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["database"]["status"] == "SKIPPED"


def test_doctor_cli_sanitizes_validation_and_unexpected_failures(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def invalid_settings() -> Settings:
        return Settings(database_url="sqlite:///invalid", _env_file=None)

    monkeypatch.setattr(operations_module, "get_settings", invalid_settings)
    assert operations_module.main([]) == 2
    validation_report = json.loads(capsys.readouterr().out)
    assert validation_report["errors"][0]["field"] == "database_url"

    def unexpected_failure() -> Settings:
        raise RuntimeError("secret-value-must-not-appear")

    monkeypatch.setattr(operations_module, "get_settings", unexpected_failure)
    assert operations_module.main(["--compact"]) == 2
    unexpected_report = json.loads(capsys.readouterr().out)
    assert unexpected_report["errors"] == [
        {"type": "RuntimeError", "message": "configuration check failed"}
    ]
    assert "secret-value" not in json.dumps(unexpected_report)


def test_doctor_cli_disposes_database_on_blocked_readiness(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    disposed = False

    class BlockedDatabase:
        def __init__(self, _url: str) -> None:
            pass

        def is_ready(self) -> tuple[bool, str | None]:
            return False, "SCHEMA_REVISION_MISMATCH"

        def dispose(self) -> None:
            nonlocal disposed
            disposed = True

    monkeypatch.setattr(operations_module, "get_settings", safe_settings)
    monkeypatch.setattr(operations_module, "Database", BlockedDatabase)

    assert operations_module.main([]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["database"]["error_code"] == "SCHEMA_REVISION_MISMATCH"
    assert disposed is True
