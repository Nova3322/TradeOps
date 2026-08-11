from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import ValidationError
from sqlalchemy import text

from trading_control_plane.config import DEFAULT_SESSION_SECRET, Settings, get_settings
from trading_control_plane.database import REQUIRED_SCHEMA_REVISION, Database

DANGEROUS_TRANSPORT_SWITCHES = (
    "binance_live_order_send_enabled",
    "binance_testnet_order_send_enabled",
    "hyperliquid_live_order_send_enabled",
    "hyperliquid_testnet_order_send_enabled",
    "freqtrade_live_order_send_enabled",
    "binance_capital_withdraw_enabled",
)


def _deployment_state(*, enabled: bool, configured: bool) -> str:
    if enabled:
        return "ENABLED" if configured else "BLOCKED_MISCONFIGURED"
    return "DISABLED_CONFIGURED" if configured else "DISABLED"


def connection_capability_matrix(
    settings: Settings,
    *,
    database_binding_counts: Mapping[str, int] | None = None,
    freqtrade_binding_counts: Mapping[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Return a secret-free implementation/configuration matrix.

    Team-owned credentials are deliberately reported as TEAM_CONFIGURATION rather
    than inferred from deployment environment variables.
    """

    binance_read_configured = bool(
        settings.binance_api_key
        and settings.binance_api_secret
        and settings.runtime_binance_account_id
    )
    hyperliquid_read_configured = bool(
        settings.hyperliquid_effective_account_address and settings.runtime_hyperliquid_account_id
    )
    legacy_freqtrade_configured = bool(
        settings.freqtrade_workers_enabled
        and settings.freqtrade_api_username
        and settings.freqtrade_api_password
    )
    binding_counts = database_binding_counts or {}
    worker_binding_counts = freqtrade_binding_counts or {}
    freqtrade_configured = any(
        int(worker_binding_counts.get(venue, 0)) > 0
        for venue in ("BINANCE", "HYPERLIQUID", "OKX", "BYBIT")
    )
    legacy_freqtrade_state = "configured" if legacy_freqtrade_configured else "absent"
    database_binance = int(binding_counts.get("BINANCE", 0)) > 0
    database_hyperliquid = int(binding_counts.get("HYPERLIQUID", 0)) > 0
    database_okx = int(binding_counts.get("OKX", 0)) > 0
    database_bybit = int(binding_counts.get("BYBIT", 0)) > 0
    return [
        {
            "capability": "TEAM_SIGNAL_SOURCE",
            "providers": ["PERPTAPE", "TRADINGVIEW_WEBHOOK", "MODEL_WEBHOOK"],
            "implementation": "IMPLEMENTED",
            "deployment_state": "TEAM_CONFIGURATION",
            "external_side_effect": "NONE",
            "boundary": "signals stop at a frozen proposal; Webhook remains manual-only",
        },
        {
            "capability": "TEAM_ACCOUNT_CONNECTION_CHECK",
            "providers": ["BINANCE", "HYPERLIQUID", "OKX", "BYBIT"],
            "implementation": "IMPLEMENTED",
            "deployment_state": "TEAM_CONFIGURATION",
            "external_side_effect": "READ_ONLY",
            "boundary": "one-time verification never enables continuous sync or trading",
        },
        {
            "capability": "BINANCE_CONTINUOUS_FACTS",
            "providers": ["BINANCE"],
            "implementation": "IMPLEMENTED_TEAM_ACCOUNT_BOUND",
            "deployment_state": _deployment_state(
                enabled=(
                    settings.runtime_sync_enabled
                    if database_binance
                    else settings.runtime_sync_enabled and settings.binance_read_only_enabled
                ),
                configured=database_binance or binance_read_configured,
            ),
            "external_side_effect": "READ_ONLY",
            "boundary": "database binding is explicit, versioned and read-only",
        },
        {
            "capability": "HYPERLIQUID_CONTINUOUS_FACTS",
            "providers": ["HYPERLIQUID"],
            "implementation": "IMPLEMENTED_TEAM_ACCOUNT_BOUND",
            "deployment_state": _deployment_state(
                enabled=(
                    settings.runtime_sync_enabled
                    if database_hyperliquid
                    else settings.runtime_sync_enabled and settings.hyperliquid_read_only_enabled
                ),
                configured=database_hyperliquid or hyperliquid_read_configured,
            ),
            "external_side_effect": "READ_ONLY",
            "boundary": "database binding strips signing material and is read-only",
        },
        {
            "capability": "OKX_CONTINUOUS_FACTS",
            "providers": ["OKX"],
            "implementation": "IMPLEMENTED_TEAM_ACCOUNT_BOUND",
            "deployment_state": _deployment_state(
                enabled=settings.runtime_sync_enabled and database_okx,
                configured=database_okx,
            ),
            "external_side_effect": "READ_ONLY",
            "boundary": "USDT linear SWAP facts only; unsupported exposure fails closed",
        },
        {
            "capability": "BYBIT_CONTINUOUS_FACTS",
            "providers": ["BYBIT"],
            "implementation": "IMPLEMENTED_TEAM_ACCOUNT_BOUND",
            "deployment_state": _deployment_state(
                enabled=settings.runtime_sync_enabled and database_bybit,
                configured=database_bybit,
            ),
            "external_side_effect": "READ_ONLY",
            "boundary": "Unified USDT linear facts only; unsupported exposure fails closed",
        },
        {
            "capability": "SHADOW_EXECUTION",
            "providers": ["INTERNAL_SIMULATOR"],
            "implementation": "IMPLEMENTED",
            "deployment_state": "TEAM_CONFIGURATION",
            "external_side_effect": "NONE",
            "boundary": "database-only SHADOW facts cannot call venue or capital adapters",
        },
        {
            "capability": "FREQTRADE_EXECUTION",
            "providers": ["BINANCE", "HYPERLIQUID", "OKX", "BYBIT"],
            "implementation": "IMPLEMENTED_TEAM_ACCOUNT_BOUND_UNCERTIFIED",
            "deployment_state": _deployment_state(
                enabled=settings.freqtrade_live_order_send_enabled,
                configured=freqtrade_configured,
            ),
            "external_side_effect": "ORDER_SEND",
            "boundary": (
                "requires exact-account ELIGIBLE status, verified encrypted credentials, "
                "continuous read-only binding, a verified LIVE worker for the same Team/Account/"
                "Venue, process switch, database gate, sender lease, authorization and fresh risk; "
                f"legacy venue defaults are {legacy_freqtrade_state} "
                "but never eligible for LIVE routing"
            ),
        },
        {
            "capability": "OKX_BYBIT_EXECUTION",
            "providers": ["OKX", "BYBIT"],
            "implementation": "IMPLEMENTED_VIA_FREQTRADE_EXECUTION_UNCERTIFIED",
            "deployment_state": _deployment_state(
                enabled=settings.freqtrade_live_order_send_enabled,
                configured=any(
                    int(worker_binding_counts.get(venue, 0)) > 0
                    for venue in ("OKX", "BYBIT")
                ),
            ),
            "external_side_effect": "ORDER_SEND",
            "boundary": (
                "compatibility projection only; execution reuses the same exact-account "
                "Freqtrade path and never creates a second venue OMS"
            ),
        },
        {
            "capability": "TEAM_NOTIFICATIONS",
            "providers": ["TELEGRAM", "SLACK", "LARK", "EMAIL"],
            "implementation": "IMPLEMENTED",
            "deployment_state": _deployment_state(
                enabled=settings.notification_worker_enabled,
                configured=bool(settings.credential_encryption_key),
            ),
            "external_side_effect": "MESSAGE_SEND",
            "provider_controls": {
                "EMAIL": (
                    "ALLOWLIST_CONFIGURED"
                    if settings.notification_email_smtp_allowlist
                    else "BLOCKED_NO_SMTP_ALLOWLIST"
                )
            },
            "boundary": (
                "API only enqueues durable deliveries; the independent notification worker "
                "has no order, capital, signing or broadcast adapter, and email SMTP requires "
                "an exact process allowlist"
            ),
        },
        {
            "capability": "AGENT_API",
            "providers": ["BEARER_TOKEN_V1"],
            "implementation": "IMPLEMENTED",
            "deployment_state": "TEAM_CONFIGURATION",
            "external_side_effect": "NONE",
            "boundary": "observer/proposer/reviewer only; no risk, order, capital or key access",
        },
        {
            "capability": "CAPITAL_SIGNING_BROADCAST",
            "providers": ["HUMAN_CONTROLLED_WALLET"],
            "implementation": "NOT_IN_CONTROL_PLANE",
            "deployment_state": "DISABLED",
            "external_side_effect": "NONE",
            "boundary": "control plane only prepares bounded unsigned requests",
        },
    ]


def build_diagnostic_report(
    settings: Settings,
    *,
    database: Database | None,
) -> dict[str, Any]:
    settings.validate_runtime_security()
    dangerous_switches = {
        name: bool(getattr(settings, name)) for name in DANGEROUS_TRANSPORT_SWITCHES
    }
    database_report: dict[str, Any]
    gates: dict[str, str] | None = None
    database_binding_counts: dict[str, int] | None = None
    freqtrade_binding_counts: dict[str, int] | None = None
    if database is None:
        database_report = {
            "checked": False,
            "status": "SKIPPED",
            "required_schema_revision": REQUIRED_SCHEMA_REVISION,
        }
        database_ready = True
    else:
        database_ready, error = database.is_ready()
        database_report = {
            "checked": True,
            "status": "READY" if database_ready else "BLOCKED",
            "error_code": error,
            "required_schema_revision": REQUIRED_SCHEMA_REVISION,
        }
        if database_ready:
            with database.engine.connect() as connection:
                database_report["observed_schema_revision"] = connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one()
                gates = {
                    str(row.capability_key): str(row.status)
                    for row in connection.execute(
                        text(
                            "SELECT capability_key, status FROM capability_gates "
                            "ORDER BY capability_key"
                        )
                    )
                }
                database_binding_counts = {
                    str(row.venue): int(row.binding_count)
                    for row in connection.execute(
                        text(
                            "SELECT venue, count(*) AS binding_count "
                            "FROM exchange_accounts WHERE runtime_sync_enabled "
                            "GROUP BY venue ORDER BY venue"
                        )
                    )
                }
                freqtrade_binding_counts = {
                    str(row.venue): int(row.binding_count)
                    for row in connection.execute(
                        text(
                            "SELECT venue, count(*) AS binding_count "
                            "FROM exchange_accounts "
                            "WHERE freqtrade_worker_mode <> 'UNCONFIGURED' "
                            "GROUP BY venue ORDER BY venue"
                        )
                    )
                }
    enabled_gates = (
        [] if gates is None else [key for key, value in gates.items() if value != "DISABLED"]
    )
    enabled_transports = [key for key, value in dangerous_switches.items() if value]
    capability_matrix = connection_capability_matrix(
        settings,
        database_binding_counts=database_binding_counts,
        freqtrade_binding_counts=freqtrade_binding_counts,
    )
    configuration_ready = not any(
        item["deployment_state"] == "BLOCKED_MISCONFIGURED" for item in capability_matrix
    )
    report_status = "READY" if database_ready and configuration_ready else "BLOCKED"
    return {
        "status": report_status,
        "environment": settings.environment,
        "configuration": {
            "runtime_security": "PASS",
            "mock_identity": "ENABLED" if settings.allow_mock_identity else "DISABLED",
            "session_secret": (
                "DEFAULT_LOCAL"
                if settings.session_signing_secret == DEFAULT_SESSION_SECRET
                else "CONFIGURED"
            ),
            "credential_encryption": (
                "CONFIGURED" if settings.credential_encryption_key else "MISSING"
            ),
        },
        "database": database_report,
        "dangerous_controls": {
            "transport_switches": dangerous_switches,
            "database_gates": gates,
            "enabled_transport_switches": enabled_transports,
            "enabled_database_gates": enabled_gates,
            "default_safe": not enabled_transports and not enabled_gates,
        },
        "connection_capability_matrix": capability_matrix,
    }


def _validation_errors(exc: ValidationError) -> list[dict[str, str]]:
    return [
        {
            "field": ".".join(str(part) for part in item["loc"]),
            "type": str(item["type"]),
            "message": str(item["msg"]),
        }
        for item in exc.errors(include_input=False, include_url=False)
    ]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Secret-free TradingOPS configuration doctor")
    parser.add_argument(
        "--skip-database",
        action="store_true",
        help="validate configuration and capability declarations without connecting to PostgreSQL",
    )
    parser.add_argument("--compact", action="store_true", help="emit compact JSON")
    args = parser.parse_args(argv)
    database: Database | None = None
    try:
        settings = get_settings()
        database = None if args.skip_database else Database(settings.database_url)
        report = build_diagnostic_report(settings, database=database)
    except ValidationError as exc:
        report = {
            "status": "BLOCKED",
            "configuration": "INVALID",
            "errors": _validation_errors(exc),
        }
    except Exception as exc:  # fail closed without echoing possibly secret-bearing values
        report = {
            "status": "BLOCKED",
            "configuration": "INVALID",
            "errors": [{"type": type(exc).__name__, "message": "configuration check failed"}],
        }
    finally:
        if database is not None:
            database.dispose()
    print(
        json.dumps(
            report,
            ensure_ascii=False,
            sort_keys=True,
            indent=None if args.compact else 2,
        )
    )
    return 0 if report["status"] == "READY" else 2


if __name__ == "__main__":  # pragma: no cover - console script uses main()
    raise SystemExit(main())
