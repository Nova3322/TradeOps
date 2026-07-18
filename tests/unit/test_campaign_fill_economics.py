from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from trading_control_plane.campaign_fill_economics import (
    CampaignFillEconomicEntryService,
    CampaignFillEconomicEntrySnapshot,
)
from trading_control_plane.commands import CommandRejected, hash_json
from trading_control_plane.execution_models import OrderIntent
from trading_control_plane.venue_fact_models import VenueFill


def entry_values() -> dict[str, object]:
    now = datetime.now(UTC)
    return {
        "campaign_fill_economic_entry_id": uuid4(),
        "campaign_id": uuid4(),
        "order_intent_id": uuid4(),
        "execution_fact_id": uuid4(),
        "venue_fill_id": uuid4(),
        "add_unit_id": None,
        "organization_id": "org-1",
        "intent_kind": "INITIAL",
        "economic_effect": "POSITION_INCREASE",
        "venue": "BINANCE",
        "execution_domain": "BINANCE_USDM",
        "account_id": "account-1",
        "instrument_id": "BINANCE:BTCUSDT-PERP",
        "direction": "LONG",
        "side": "BUY",
        "position_side": "BOTH",
        "reduce_only": False,
        "margin_mode": "ISOLATED",
        "collateral_scope": "ACCOUNT",
        "collateral_pool_id": "pool-usdt-1",
        "risk_currency": "USD",
        "venue_order_id": "order-1",
        "venue_trade_id": "trade-1",
        "quantity": Decimal("0.5"),
        "price": Decimal("100"),
        "contract_multiplier": Decimal("1"),
        "notional": Decimal("50"),
        "liquidity_role": "TAKER",
        "fee_amount": Decimal("0.002"),
        "fee_currency": "BNB",
        "fee_effect": "CHARGE",
        "realized_pnl": None,
        "realized_pnl_status": "UNKNOWN",
        "settlement_currency": "USDT",
        "fill_hash": "a" * 64,
        "execution_fact_evidence_hash": "b" * 64,
        "entry_version": "campaign-fill-economic-entry-v1",
        "environment": "SHADOW",
        "real_funds_eligible": False,
        "facts_event_time": now,
        "recorded_at": now,
    }


def test_campaign_fill_economic_entry_snapshot_preserves_unknown_and_rejects_tampering() -> None:
    values = entry_values()
    draft = CampaignFillEconomicEntrySnapshot.model_construct(
        **values,
        entry_hash="0" * 64,
    )
    entry_hash = hash_json(draft.model_dump(mode="json", exclude={"entry_hash"}))
    snapshot = CampaignFillEconomicEntrySnapshot.model_validate(
        {**values, "entry_hash": entry_hash}
    )

    assert snapshot.realized_pnl is None
    assert snapshot.realized_pnl_status == "UNKNOWN"
    assert snapshot.fee_currency == "BNB"
    assert snapshot.settlement_currency == "USDT"

    known_values = {
        **values,
        "intent_kind": "ADD",
        "add_unit_id": uuid4(),
        "fee_amount": Decimal("-0.001"),
        "fee_effect": "REBATE",
        "realized_pnl": Decimal("0"),
        "realized_pnl_status": "KNOWN",
    }
    known_draft = CampaignFillEconomicEntrySnapshot.model_construct(
        **known_values,
        entry_hash="0" * 64,
    )
    known = CampaignFillEconomicEntrySnapshot.model_validate(
        {
            **known_values,
            "entry_hash": hash_json(known_draft.model_dump(mode="json", exclude={"entry_hash"})),
        }
    )
    assert known.realized_pnl == 0
    assert known.realized_pnl_status == "KNOWN"
    assert known.fee_effect == "REBATE"

    with pytest.raises(ValueError, match="realized PnL status is inconsistent"):
        CampaignFillEconomicEntrySnapshot.model_validate(
            {
                **values,
                "realized_pnl_status": "KNOWN",
                "entry_hash": entry_hash,
            }
        )
    with pytest.raises(ValueError, match="fee semantics are inconsistent"):
        CampaignFillEconomicEntrySnapshot.model_validate(
            {
                **values,
                "fee_effect": "REBATE",
                "entry_hash": entry_hash,
            }
        )
    with pytest.raises(ValueError, match="hash mismatch"):
        CampaignFillEconomicEntrySnapshot.model_validate({**values, "entry_hash": "f" * 64})


def test_campaign_fill_economic_entry_rejects_mismatched_campaign_source() -> None:
    intent = OrderIntent(
        order_intent_id=uuid4(),
        intent_kind="INITIAL",
        venue="BINANCE",
        execution_domain="BINANCE_USDM",
        account_id="account-1",
        instrument_id="BINANCE:BTCUSDT-PERP",
        side="BUY",
        position_side="LONG",
        reduce_only=False,
    )
    fill = VenueFill(
        venue_fill_id=uuid4(),
        organization_id="org-1",
        venue="BINANCE",
        execution_domain="BINANCE_USDM",
        account_id="account-1",
        instrument_id="BINANCE:ETHUSDT-PERP",
        side="BUY",
        position_side="BOTH",
        reduce_only=False,
        fill_hash="a" * 64,
    )

    with pytest.raises(CommandRejected) as exc_info:
        CampaignFillEconomicEntryService.validate_fill_source(
            intent,
            fill,
            "org-1",
            "a" * 64,
        )

    assert exc_info.value.error_code == "CAMPAIGN_FILL_ECONOMIC_ENTRY_FACT_MISMATCH"
