from __future__ import annotations

from copy import deepcopy

import pytest

from trading_control_plane.domain import DomainRejected
from trading_control_plane.production_config import (
    ConfigurationCheck,
    ProductionConfiguration,
    ProductionConfigurator,
    _reject_secret_keys,
    _report,
)
from trading_control_plane.telegram import TelegramBotClient


def configuration_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "operator_username": "production-admin",
        "team": {
            "team_id": "9f5b7320-59ad-4bf4-b6dc-f7ac7f39a522",
            "workspace_id": "6006a981-e306-4da8-9ec6-f3f39f7b2f06",
            "trading_enabled": True,
            "mode": "LIVE",
        },
        "gates": {
            "LIVE_ORDER_SEND": {"status": "ENABLED", "reason": "verified production"},
            "CAPITAL_TRANSFER": {"status": "ENABLED", "reason": "verified capital paths"},
            "AUTO_ADD": {"status": "DISABLED", "reason": "reviewed restore required"},
            "AUTO_OPERATING_REFILL": {
                "status": "DISABLED",
                "reason": "no complete automatic refill policy",
            },
            "AUTO_PROFIT_SWEEP": {
                "status": "DISABLED",
                "reason": "no complete automatic sweep policy",
            },
        },
        "accounts": [
            {
                "account_id": "bn-exact",
                "venue": "BINANCE",
                "environment": "LIVE",
                "worker": {"mode": "LIVE"},
            },
            {
                "account_id": "hl-exact",
                "venue": "HYPERLIQUID",
                "environment": "LIVE",
                "worker": {"mode": "LIVE", "hip3_dexes": ["xyz"]},
            },
        ],
        "risk": {
            "version": "risk-v1",
            "system_state": "NORMAL",
            "max_total_risk": "150",
            "max_account_risk": "100",
            "max_single_loss": "25",
            "max_consecutive_losses": 3,
            "loss_cooldown_seconds": 3600,
            "max_fact_age_seconds": 900,
        },
        "proposals": {
            "default_account_id": "bn-exact",
            "notional": "100",
            "max_risk": "25",
            "risk_tier": "MEDIUM",
            "invalidation_bps": 500,
            "expires_in_minutes": 480,
            "rationale": "production default",
            "automatic_proposals": True,
            "automatic_min_timeframes": 4,
        },
        "perptape": {"source_id": "79b540f6-4b4e-4a85-ac83-d2f71716806c"},
        "telegram_routes": [
            {
                "route_id": "a8c40680-9622-5efd-8612-aea8fa4b3322",
                "name": "review",
                "enabled": True,
                "subscribed_events": ["PROPOSAL_REVIEW_REQUIRED"],
                "reviewer_username": "production-admin",
                "reviewer_telegram_username": "production_admin",
            }
        ],
        "capital_runtime": {
            "safe_spending_enabled": True,
            "safe_spending_arbitrum_rpc_url": "https://arb.example/rpc",
            "capital_arbitrum_rpc_url": "https://arb.example/rpc",
            "binance_capital_withdraw_enabled": True,
        },
        "capital": {
            "treasury_provider": "SAFE_SPENDING_LIMIT",
            "owned_arbitrum_address": "0x2222222222222222222222222222222222222222",
            "binance_account_id": "bn-exact",
            "binance_deposit_address": "0x3333333333333333333333333333333333333333",
            "binance_withdrawal_address": "0x1111111111111111111111111111111111111111",
            "hyperliquid_account_id": "hl-exact",
            "hyperliquid_bridge_address": "0x4444444444444444444444444444444444444444",
            "safe_address": "0x1111111111111111111111111111111111111111",
            "safe_delegate_address": "0x2222222222222222222222222222222222222222",
            "max_amount": "150",
            "max_fee": "1",
            "enabled_paths": [
                "VAULT_TO_BINANCE",
                "BINANCE_TO_VAULT",
                "VAULT_TO_HYPERLIQUID",
                "HYPERLIQUID_TO_VAULT",
            ],
        },
        "capital_automation_policies": [],
    }


def test_production_configuration_requires_exact_complete_fail_closed_scope() -> None:
    parsed = ProductionConfiguration.model_validate(configuration_payload())
    assert parsed.proposals.default_account_id == "bn-exact"
    assert parsed.accounts[1].worker.hip3_dexes == ["xyz"]
    assert parsed.telegram_routes[0].reviewer_username == "production-admin"

    invalid = deepcopy(configuration_payload())
    invalid["proposals"]["default_account_id"] = "display-name"  # type: ignore[index]
    with pytest.raises(ValueError, match="default_account_id"):
        ProductionConfiguration.model_validate(invalid)

    invalid = deepcopy(configuration_payload())
    invalid["gates"]["AUTO_PROFIT_SWEEP"]["status"] = "ENABLED"  # type: ignore[index]
    with pytest.raises(ValueError, match="AUTO_PROFIT_SWEEP"):
        ProductionConfiguration.model_validate(invalid)

    invalid = deepcopy(configuration_payload())
    invalid["capital"]["enabled_paths"] = ["VAULT_TO_BINANCE"]  # type: ignore[index]
    with pytest.raises(ValueError, match="all four"):
        ProductionConfiguration.model_validate(invalid)

    invalid = deepcopy(configuration_payload())
    invalid["capital_automation_policies"] = [
        {
            "account_id": "bn-exact",
            "venue": "BINANCE",
            "asset": "USDC",
            "operating_low": "10",
            "operating_target": "20",
            "operating_high": "30",
            "vault_minimum_reserve": "100",
            "minimum_transfer": "5",
            "maximum_transfer": "25",
            "max_fee": "1",
        }
    ]
    with pytest.raises(ValueError, match="LIVE capital automation policies"):
        ProductionConfiguration.model_validate(invalid)


def test_production_configuration_rejects_secret_fields_and_unbound_review_routes() -> None:
    with pytest.raises(ValueError, match="secrets are forbidden"):
        _reject_secret_keys({"telegram": {"bot_token": "not-allowed"}})

    invalid = deepcopy(configuration_payload())
    del invalid["telegram_routes"][0]["reviewer_telegram_username"]  # type: ignore[index]
    with pytest.raises(ValueError, match="Telegram reviewer usernames"):
        ProductionConfiguration.model_validate(invalid)


def test_telegram_reviewer_identity_must_be_exact_private_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        TelegramBotClient,
        "call",
        lambda _self, _method, _payload: {
            "ok": True,
            "result": {"id": 123, "type": "private", "username": "Production_Admin"},
        },
    )
    assert ProductionConfigurator._telegram_private_identity(
        {"bot_token": "opaque", "chat_id": "123"}, "production_admin"
    ) == ("123", "production_admin")
    with pytest.raises(DomainRejected, match="TELEGRAM_REVIEWER_IDENTITY_MISMATCH"):
        ProductionConfigurator._telegram_private_identity(
            {"bot_token": "opaque", "chat_id": "123"}, "another_user"
        )


def test_status_report_distinguishes_mutable_drift_from_blockers() -> None:
    report = _report(
        "status",
        [
            ConfigurationCheck("team.mode", "OK", "MATCH", "configured"),
            ConfigurationCheck("proposals", "DRIFT", "PROPOSAL_DEFAULTS_DRIFT", "differs"),
            ConfigurationCheck("capital.safe", "BLOCKED", "SAFE_NOT_READY", "missing"),
        ],
        [],
    )
    assert report["status"] == "BLOCKED"
    assert report["summary"] == {"blocked": 1, "drift": 1, "ok": 1}
