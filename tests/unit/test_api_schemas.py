from decimal import Decimal

import pytest
from pydantic import ValidationError

from trading_control_plane.api_schemas import (
    AgentAccessRequest,
    AgentCreateRequest,
    AgentProposalRequest,
    FreqtradeLiveActionRequest,
    ManagedUserAccessRequest,
    ManagedUserCreateRequest,
    ManualProposalRequest,
    NotificationRouteWriteRequest,
    ProposalDefaultConfigRequest,
    SystemProposalRequest,
    TeamMemberInviteRequest,
    TransferProposalRequest,
)


def proposal_payload() -> dict[str, object]:
    return {
        "account_id": "acct-1",
        "venue": "BINANCE",
        "instrument_id": "00000000-0000-0000-0000-000000000001",
        "direction": "LONG",
        "risk_tier": "LOW",
        "quantity": "1",
        "max_risk": "40",
        "trigger_price": "100",
        "invalidation_price": "95",
        "rationale": "frozen user request",
        "idempotency_key": "manual-proposal",
    }


def test_freqtrade_live_dispatch_requires_an_explicit_idempotency_key() -> None:
    request = FreqtradeLiveActionRequest.model_validate(
        {
            "execution_scope": "LIVE:acct-1:BINANCE",
            "owner_id": "execution-worker-1",
            "fencing_token": 7,
            "idempotency_key": "dispatch-intent-1",
        }
    )

    assert request.idempotency_key == "dispatch-intent-1"
    with pytest.raises(ValidationError):
        FreqtradeLiveActionRequest.model_validate(
            {
                "execution_scope": "LIVE:acct-1:BINANCE",
                "owner_id": "execution-worker-1",
                "fencing_token": 7,
            }
        )


def test_auto_add_proposal_requires_frozen_capacity_trigger_and_tier_limit() -> None:
    valid = ManualProposalRequest.model_validate(
        {
            **proposal_payload(),
            "initial_quantity": "0.5",
            "allow_auto_add": True,
            "requested_adds": 1,
            "add_trigger_price": "105",
        }
    )

    assert valid.initial_quantity == Decimal("0.5")
    assert valid.requested_adds == 1

    with pytest.raises(ValidationError, match="risk tier limit"):
        ManualProposalRequest.model_validate(
            {
                **proposal_payload(),
                "initial_quantity": "0.5",
                "allow_auto_add": True,
                "requested_adds": 2,
                "add_trigger_price": "105",
            }
        )
    with pytest.raises(ValidationError, match="requires a frozen add trigger"):
        ManualProposalRequest.model_validate(
            {
                **proposal_payload(),
                "initial_quantity": "0.5",
                "allow_auto_add": True,
                "requested_adds": 1,
            }
        )


def test_disabled_auto_add_cannot_hide_reserved_quantity_or_units() -> None:
    with pytest.raises(ValidationError, match="disabled AUTO_ADD"):
        ManualProposalRequest.model_validate(
            {
                **proposal_payload(),
                "initial_quantity": "0.5",
                "allow_auto_add": False,
            }
        )


def test_live_environment_is_explicit_for_perptape_and_capital_proposals() -> None:
    manual = ManualProposalRequest.model_validate({**proposal_payload(), "environment": "LIVE"})
    system = SystemProposalRequest.model_validate(
        {
            "environment": "LIVE",
            "account_id": "binance-main",
            "risk_tier": "LOW",
            "quantity": "0.001",
            "max_risk": "1",
            "invalidation_price": "100000",
            "rationale": "review current external candidate",
        }
    )
    transfer = TransferProposalRequest.model_validate(
        {
            "environment": "LIVE",
            "direction": "VAULT_TO_VENUE",
            "account_id": "binance-main",
            "venue": "BINANCE",
            "vault_id": "0x1111111111111111111111111111111111111111",
            "asset": "USDC",
            "network": "ARBITRUM",
            "destination_reference": "approved-destination-reference",
            "amount": "1",
            "max_fee": "0.01",
            "min_received": "0.99",
            "reason": "reviewed operating capital allocation",
            "idempotency_key": "live-transfer-proposal",
        }
    )

    assert manual.environment == "LIVE"
    assert system.environment == "LIVE"
    assert transfer.environment == "LIVE"


def test_trade_proposals_and_defaults_require_at_least_eight_hours() -> None:
    assert ManualProposalRequest.model_validate(proposal_payload()).expires_in_minutes == 480
    with pytest.raises(ValidationError):
        ManualProposalRequest.model_validate({**proposal_payload(), "expires_in_minutes": 479})
    with pytest.raises(ValidationError):
        ProposalDefaultConfigRequest.model_validate(
            {
                "account_id": "acct-1",
                "risk_tier": "LOW",
                "notional": "100",
                "max_risk": "1",
                "invalidation_bps": 200,
                "expires_in_minutes": 120,
                "rationale": "too short for human review",
                "idempotency_key": "short-default",
            }
        )


def test_notification_route_schema_keeps_secrets_wrapped_and_limits_known_events() -> None:
    request = NotificationRouteWriteRequest.model_validate(
        {
            "name": "Slack alerts",
            "channel": "SLACK",
            "event_types": ["SIGNAL_EVENT_RECEIVED"],
            "configuration": {"webhook_url": "https://hooks.slack.com/services/T/B/secret"},
            "expected_version": 0,
            "idempotency_key": "route-create",
        }
    )

    assert request.configuration is not None
    assert request.configuration.plaintext() == {
        "webhook_url": "https://hooks.slack.com/services/T/B/secret"
    }
    assert "hooks.slack.com" not in repr(request)

    capital_request = NotificationRouteWriteRequest.model_validate(
        {
            "name": "Capital alerts",
            "channel": "SLACK",
            "event_types": ["CAPITAL_STATUS_CHANGED"],
            "expected_version": 0,
            "idempotency_key": "route-capital",
        }
    )
    assert capital_request.event_types == ["CAPITAL_STATUS_CHANGED"]

    with pytest.raises(ValidationError):
        NotificationRouteWriteRequest.model_validate(
            {
                "name": "Unknown event",
                "channel": "SLACK",
                "event_types": ["UNKNOWN_EVENT"],
                "expected_version": 0,
                "idempotency_key": "route-unknown",
            }
        )


def test_agent_schemas_limit_roles_require_exact_scope_and_current_time_shape() -> None:
    created = AgentCreateRequest.model_validate(
        {
            "username": "model-alpha",
            "roles": ["PROPOSER", "REVIEWER"],
            "account_scope": "paper-1",
            "venue_scope": "BINANCE",
            "idempotency_key": "agent-create",
        }
    )
    proposal = AgentProposalRequest.model_validate(
        {
            **proposal_payload(),
            "model_id": "alpha",
            "model_version": "2026.08",
            "request_id": "request-00000001",
            "generated_at": "2026-08-10T08:00:00+00:00",
            "rationale": "model facts are frozen for independent review",
        }
    )

    assert created.expires_in_days == 90
    assert proposal.expires_in_minutes == 480
    with pytest.raises(ValidationError):
        AgentCreateRequest.model_validate(
            {
                "username": "operator-agent",
                "roles": ["OPERATOR"],
                "account_scope": "paper-1",
                "venue_scope": "BINANCE",
                "idempotency_key": "agent-operator",
            }
        )
    with pytest.raises(ValidationError, match="active agent requires a role"):
        AgentAccessRequest.model_validate(
            {
                "roles": [],
                "active": True,
                "account_scope": "paper-1",
                "venue_scope": "BINANCE",
                "expected_auth_version": 1,
                "idempotency_key": "agent-empty-role",
            }
        )
    with pytest.raises(ValidationError, match="timezone"):
        AgentProposalRequest.model_validate(
            {
                **proposal_payload(),
                "model_id": "alpha",
                "model_version": "2026.08",
                "request_id": "request-00000001",
                "generated_at": "2026-08-10T08:00:00",
                "rationale": "model facts are frozen for independent review",
            }
        )


def test_human_access_schemas_share_the_four_exchange_scope_values() -> None:
    cases = (
        (
            ManagedUserCreateRequest,
            {
                "username": "observer-bybit",
                "password": "observer-password",
                "roles": ["OBSERVER"],
            },
        ),
        (ManagedUserAccessRequest, {"roles": ["OBSERVER"], "active": True}),
        (
            TeamMemberInviteRequest,
            {
                "username": "observer-bybit",
                "roles": ["OBSERVER"],
                "idempotency_key": "invite-bybit",
            },
        ),
    )
    for schema, payload in cases:
        assert schema.model_validate({**payload, "venue_scope": "BYBIT"}).venue_scope == "BYBIT"
        with pytest.raises(ValidationError):
            schema.model_validate({**payload, "venue_scope": "UNSUPPORTED"})
