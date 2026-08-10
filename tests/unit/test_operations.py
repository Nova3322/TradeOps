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
    assert matrix["OKX_BYBIT_CONTINUOUS_FACTS"]["implementation"] == "NOT_IMPLEMENTED"
    assert matrix["OKX_BYBIT_EXECUTION"]["deployment_state"] == "UNAVAILABLE"
    assert matrix["CAPITAL_SIGNING_BROADCAST"]["implementation"] == "NOT_IN_CONTROL_PLANE"


def test_connection_matrix_projects_database_bindings_behind_process_master_switch() -> None:
    stopped = {
        item["capability"]: item
        for item in connection_capability_matrix(
            safe_settings(runtime_sync_enabled=False),
            database_binding_counts={"BINANCE": 2},
        )
    }
    running = {
        item["capability"]: item
        for item in connection_capability_matrix(
            safe_settings(runtime_sync_enabled=True),
            database_binding_counts={"BINANCE": 2},
        )
    }

    assert stopped["BINANCE_CONTINUOUS_FACTS"]["deployment_state"] == (
        "DISABLED_CONFIGURED"
    )
    assert running["BINANCE_CONTINUOUS_FACTS"]["deployment_state"] == "ENABLED"


def test_environment_template_names_every_settings_field_without_values_from_runtime() -> None:
    template = Path(".env.example").read_text(encoding="utf-8")
    declared = set(re.findall(r"^([A-Z][A-Z0-9_]*)=", template, re.MULTILINE))
    expected = {f"TRADING_{name.upper()}" for name in Settings.model_fields}

    assert expected <= declared


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
