from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from trading_control_plane.config import Settings
from trading_control_plane.connections import project_runtime_connections


def _settings(**overrides: object) -> Settings:
    return Settings(
        database_url="postgresql+psycopg://user:pass@localhost/trading",
        _env_file=None,
        **overrides,
    )


def test_connection_projection_uses_exact_database_bindings_and_current_probes() -> None:
    settings = _settings(
        runtime_sync_enabled=True,
        fact_adapter_enabled=True,
        freqtrade_workers_enabled=True,
        perptape_api_key="perptape-secret",
        notilt_enabled=True,
        notilt_agent_address="0x2222222222222222222222222222222222222222",
        notilt_arbitrum_vault_address="0x3333333333333333333333333333333333333333",
    )
    health = {
        source: {
            "status": "SUCCESS",
            "items_observed": 1,
            "error_code": None,
            "checked_at": "2026-08-02T12:00:00+00:00",
        }
        for source in (
            "BINANCE:binance-main",
            "HYPERLIQUID:hyperliquid-main",
            "OKX:okx-main",
            "BYBIT:bybit-main",
            "PERPTAPE",
            "NOTILT:42161",
        )
    }

    projected = project_runtime_connections(
        settings,
        health,
        database_binding_counts={venue: 1 for venue in ("BINANCE", "HYPERLIQUID", "OKX", "BYBIT")},
    )

    assert all(item["available"] for item in projected.values())
    assert all(item["category"] == "READ_ONLY_CONNECTED" for item in projected.values())
    assert all(
        projected[venue]["write_process_enabled"]
        for venue in ("BINANCE", "HYPERLIQUID", "OKX", "BYBIT")
    )
    assert projected["PERPTAPE"]["write_process_enabled"] is False
    assert projected["NOTILT"]["write_process_enabled"] is False
    serialized = json.dumps(projected)
    for secret in (
        "perptape-secret",
        "0x2222222222222222222222222222222222222222",
        "0x3333333333333333333333333333333333333333",
    ):
        assert secret not in serialized


def test_connection_projection_fails_closed_without_database_bindings() -> None:
    missing = project_runtime_connections(_settings(), {})

    for venue in ("BINANCE", "HYPERLIQUID", "OKX", "BYBIT"):
        assert missing[venue]["category"] == "CREDENTIALS_NOT_LOADED"
    assert missing["PERPTAPE"]["category"] == "CREDENTIALS_NOT_LOADED"
    assert missing["NOTILT"]["category"] == "CREDENTIALS_NOT_LOADED"


def test_connection_projection_uses_fresh_worker_health_not_api_local_flags() -> None:
    now = datetime(2026, 8, 19, 12, tzinfo=UTC)
    health = {
        "BINANCE:binance-main": {
            "status": "SUCCESS",
            "checked_at": now.isoformat(),
        }
    }
    current = project_runtime_connections(
        _settings(runtime_sync_enabled=False, fact_adapter_enabled=False),
        health,
        database_binding_counts={"BINANCE": 1},
        now=now,
        fact_stale_after_seconds=360,
    )
    stale = project_runtime_connections(
        _settings(runtime_sync_enabled=False, fact_adapter_enabled=False),
        {
            "BINANCE:binance-main": {
                "status": "SUCCESS",
                "checked_at": (now - timedelta(seconds=361)).isoformat(),
            }
        },
        database_binding_counts={"BINANCE": 1},
        now=now,
        fact_stale_after_seconds=360,
    )

    assert current["BINANCE"]["category"] == "READ_ONLY_CONNECTED"
    assert current["BINANCE"]["available"] is True
    assert stale["BINANCE"]["category"] == "READ_ONLY_PROBE_FAILED"
    assert stale["BINANCE"]["error_code"] == "FACT_ADAPTER_STALE"
    assert stale["BINANCE"]["available"] is False


def test_connection_projection_classifies_exact_adapter_failures() -> None:
    settings = _settings(runtime_sync_enabled=True, fact_adapter_enabled=True)
    bindings = {venue: 1 for venue in ("BINANCE", "HYPERLIQUID", "OKX", "BYBIT")}
    projected = project_runtime_connections(
        settings,
        {
            "BINANCE:binance-main": {
                "status": "FAILED",
                "error_code": "BINANCE_AUTHENTICATION_FAILED",
            },
            "HYPERLIQUID:hyperliquid-main": {
                "status": "FAILED",
                "error_code": "HYPERLIQUID_RATE_LIMITED",
                "checked_at": "2026-08-02T12:01:00+00:00",
                "last_success_at": "2026-08-02T11:59:00+00:00",
                "retry_at": "2026-08-02T12:02:00+00:00",
                "consecutive_failures": 1,
            },
            "OKX:okx-main": {
                "status": "FAILED",
                "error_code": "OKX_HISTORY_INCOMPLETE:OKX_READ_ONLY_UNAVAILABLE",
            },
        },
        database_binding_counts=bindings,
    )

    assert projected["BINANCE"]["category"] == "AUTH_OR_PERMISSION_FAILED"
    assert projected["HYPERLIQUID"]["category"] == "UPSTREAM_RATE_LIMITED"
    assert projected["HYPERLIQUID"]["available"] is False
    assert projected["HYPERLIQUID"]["retry_at"].endswith("12:02:00+00:00")
    assert projected["HYPERLIQUID"]["consecutive_failures"] == 1
    assert projected["OKX"]["category"] == "READ_ONLY_CONNECTED_HISTORY_INCOMPLETE"
    assert projected["OKX"]["available"] is True
