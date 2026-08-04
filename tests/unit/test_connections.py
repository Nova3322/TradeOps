from __future__ import annotations

import json

from trading_control_plane.config import Settings
from trading_control_plane.connections import project_runtime_connections


def test_connection_projection_uses_current_probe_and_never_exposes_credentials() -> None:
    settings = Settings(
        database_url="postgresql+psycopg://user:pass@localhost/trading",
        perptape_api_key="perptape-secret",
        runtime_binance_account_id="binance-main",
        runtime_hyperliquid_account_id="hyperliquid-main",
        binance_read_only_enabled=True,
        binance_api_key="binance-key",
        binance_api_secret="binance-secret",  # noqa: S106
        hyperliquid_read_only_enabled=True,
        hyperliquid_account_address="0x1111111111111111111111111111111111111111",
        notilt_enabled=True,
        notilt_agent_address="0x2222222222222222222222222222222222222222",
        notilt_arbitrum_vault_address="0x3333333333333333333333333333333333333333",
        _env_file=None,
    )
    health = {
        source: {
            "status": "SUCCESS",
            "items_observed": 1,
            "error_code": None,
            "checked_at": "2026-08-02T12:00:00+00:00",
        }
        for source in ("BINANCE", "HYPERLIQUID", "PERPTAPE", "NOTILT:42161")
    }

    projected = project_runtime_connections(settings, health)

    assert all(item["available"] for item in projected.values())
    assert all(item["category"] == "READ_ONLY_CONNECTED" for item in projected.values())
    assert all(item["write_process_enabled"] is False for item in projected.values())
    serialized = json.dumps(projected)
    for secret in (
        "perptape-secret",
        "binance-key",
        "binance-secret",
        "0x1111111111111111111111111111111111111111",
        "0x2222222222222222222222222222222222222222",
        "0x3333333333333333333333333333333333333333",
    ):
        assert secret not in serialized


def test_connection_projection_distinguishes_configuration_and_probe_failures() -> None:
    base = "postgresql+psycopg://user:pass@localhost/trading"
    missing = project_runtime_connections(Settings(database_url=base, _env_file=None), {})
    assert missing["BINANCE"]["category"] == "CREDENTIALS_NOT_LOADED"
    assert missing["HYPERLIQUID"]["category"] == "CREDENTIALS_NOT_LOADED"
    assert missing["PERPTAPE"]["category"] == "CREDENTIALS_NOT_LOADED"
    assert missing["NOTILT"]["category"] == "CREDENTIALS_NOT_LOADED"

    incomplete = Settings(
        database_url=base,
        binance_read_only_enabled=True,
        binance_api_key="key",
        binance_api_secret="secret",  # noqa: S106
        hyperliquid_read_only_enabled=True,
        hyperliquid_account_address="0x1111111111111111111111111111111111111111",
        notilt_enabled=True,
        notilt_agent_address="0x2222222222222222222222222222222222222222",
        _env_file=None,
    )
    projected = project_runtime_connections(
        incomplete,
        {
            "BINANCE": {
                "status": "FAILED",
                "error_code": "BINANCE_AUTHENTICATION_FAILED",
            }
        },
    )
    assert projected["BINANCE"]["category"] == "CONFIG_INCOMPLETE"
    assert projected["HYPERLIQUID"]["category"] == "CONFIG_INCOMPLETE"
    assert projected["NOTILT"]["category"] == "CONFIG_INCOMPLETE"

    failed = incomplete.model_copy(
        update={
            "runtime_binance_account_id": "binance-main",
            "runtime_hyperliquid_account_id": "hyperliquid-main",
        }
    )
    projected = project_runtime_connections(
        failed,
        {
            "BINANCE": {
                "status": "FAILED",
                "error_code": "BINANCE_AUTHENTICATION_FAILED",
            },
            "HYPERLIQUID": {
                "status": "FAILED",
                "error_code": "HYPERLIQUID_READ_ONLY_UNAVAILABLE",
            },
        },
    )
    assert projected["BINANCE"]["category"] == "AUTH_OR_PERMISSION_FAILED"
    assert projected["HYPERLIQUID"]["category"] == "NETWORK_OR_UPSTREAM_FAILED"

    rate_limited = project_runtime_connections(
        failed,
        {
            "HYPERLIQUID": {
                "status": "FAILED",
                "error_code": "HYPERLIQUID_RATE_LIMITED",
                "checked_at": "2026-08-02T12:01:00+00:00",
                "last_success_at": "2026-08-02T11:59:00+00:00",
                "retry_at": "2026-08-02T12:02:00+00:00",
                "consecutive_failures": 1,
            }
        },
    )
    assert rate_limited["HYPERLIQUID"]["available"] is False
    assert rate_limited["HYPERLIQUID"]["category"] == "UPSTREAM_RATE_LIMITED"
    assert "限流" in rate_limited["HYPERLIQUID"]["reason"]
    assert rate_limited["HYPERLIQUID"]["last_success_at"].endswith("11:59:00+00:00")
    assert rate_limited["HYPERLIQUID"]["retry_at"].endswith("12:02:00+00:00")
    assert rate_limited["HYPERLIQUID"]["consecutive_failures"] == 1

    cooldown = project_runtime_connections(
        failed,
        {
            "HYPERLIQUID": {
                "status": "SKIPPED",
                "error_code": "HYPERLIQUID_RATE_LIMITED_COOLDOWN",
            }
        },
    )
    assert cooldown["HYPERLIQUID"]["category"] == "UPSTREAM_RATE_LIMITED"
    assert cooldown["HYPERLIQUID"]["available"] is False

    degraded = project_runtime_connections(
        failed,
        {
            "BINANCE": {
                "status": "FAILED",
                "error_code": "BINANCE_HISTORY_INCOMPLETE:BINANCE_READ_ONLY_UNAVAILABLE",
            }
        },
    )
    assert degraded["BINANCE"]["available"] is True
    assert degraded["BINANCE"]["category"] == "READ_ONLY_CONNECTED_HISTORY_INCOMPLETE"
