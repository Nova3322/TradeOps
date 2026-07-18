from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from trading_control_plane.campaign_economics import (
    CampaignEconomicBaselineService,
    CampaignEconomicBaselineSnapshot,
)
from trading_control_plane.commands import CommandRejected, hash_json
from trading_control_plane.execution_models import OrderIntent
from trading_control_plane.venue_fact_models import VenuePositionSnapshot


def baseline_values() -> dict[str, object]:
    now = datetime.now(UTC)
    return {
        "campaign_economic_baseline_id": uuid4(),
        "campaign_id": uuid4(),
        "initial_order_intent_id": uuid4(),
        "initial_execution_fact_id": uuid4(),
        "position_snapshot_id": uuid4(),
        "organization_id": "org-1",
        "venue": "BINANCE",
        "execution_domain": "BINANCE_USDM",
        "account_id": "account-1",
        "instrument_id": "BINANCE:BTCUSDT-PERP",
        "direction": "LONG",
        "position_mode": "ONE_WAY",
        "position_side": "BOTH",
        "margin_mode": "ISOLATED",
        "collateral_scope": "ACCOUNT",
        "collateral_pool_id": "pool-usdt-1",
        "settlement_currency": "USD",
        "initial_quantity": Decimal("0.5"),
        "initial_entry_price": Decimal("100.5"),
        "initial_mark_price": Decimal("120"),
        "contract_multiplier": Decimal("1"),
        "initial_notional": Decimal("60"),
        "frozen_initial_margin_reference": Decimal("30"),
        "margin_reference_source": "VENUE_POSITION_INITIAL_MARGIN",
        "position_snapshot_hash": "a" * 64,
        "execution_fact_evidence_hash": "b" * 64,
        "baseline_version": "campaign-economic-baseline-v1",
        "environment": "SHADOW",
        "real_funds_eligible": False,
        "facts_event_time": now,
        "recorded_at": now,
    }


def test_campaign_economic_baseline_snapshot_rejects_tampering() -> None:
    values = baseline_values()
    draft = CampaignEconomicBaselineSnapshot.model_construct(
        **values,
        baseline_hash="0" * 64,
    )
    baseline_hash = hash_json(draft.model_dump(mode="json", exclude={"baseline_hash"}))
    snapshot = CampaignEconomicBaselineSnapshot.model_validate(
        {**values, "baseline_hash": baseline_hash}
    )
    assert snapshot.frozen_initial_margin_reference == 30

    with pytest.raises(ValueError, match="position economics are inconsistent"):
        CampaignEconomicBaselineSnapshot.model_validate(
            {
                **values,
                "initial_notional": "61",
                "baseline_hash": baseline_hash,
            }
        )
    with pytest.raises(ValueError, match="hash mismatch"):
        CampaignEconomicBaselineSnapshot.model_validate({**values, "baseline_hash": "f" * 64})
    with pytest.raises(ValueError, match="cannot be real-funds eligible"):
        CampaignEconomicBaselineSnapshot.model_validate(
            {
                **values,
                "real_funds_eligible": True,
                "baseline_hash": baseline_hash,
            }
        )


def test_campaign_economic_baseline_rejects_uncertified_cross_margin_attribution() -> None:
    intent = OrderIntent(
        order_intent_id=uuid4(),
        intent_kind="INITIAL",
        venue="BINANCE",
        execution_domain="BINANCE_USDM",
        account_id="account-1",
        instrument_id="BINANCE:BTCUSDT-PERP",
        position_side="LONG",
        margin_mode="CROSS",
        collateral_pool_id="pool-usdt-1",
        risk_currency="USD",
    )
    position = VenuePositionSnapshot(
        venue_position_snapshot_id=uuid4(),
        organization_id="org-1",
        venue="BINANCE",
        execution_domain="BINANCE_USDM",
        account_id="account-1",
        instrument_id="BINANCE:BTCUSDT-PERP",
        direction="LONG",
        position_mode="ONE_WAY",
        position_side="BOTH",
        margin_mode="CROSS",
        collateral_pool_id="pool-usdt-1",
        settlement_currency="USD",
        position_state="OPEN",
        quantity=Decimal("0.5"),
        initial_margin=Decimal("30"),
        snapshot_hash="a" * 64,
    )

    with pytest.raises(CommandRejected) as exc_info:
        CampaignEconomicBaselineService.validate_initial_margin_source(
            intent,
            position,
            "org-1",
            Decimal("0.5"),
            "a" * 64,
        )

    assert exc_info.value.error_code == "CAMPAIGN_MARGIN_BASELINE_UNSUPPORTED"
