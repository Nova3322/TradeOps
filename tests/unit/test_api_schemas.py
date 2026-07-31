from decimal import Decimal

import pytest
from pydantic import ValidationError

from trading_control_plane.api_schemas import (
    ManualProposalRequest,
    SystemProposalRequest,
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

    assert system.environment == "LIVE"
    assert transfer.environment == "LIVE"
