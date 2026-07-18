from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from tests.integration.test_execution import (
    create_intent_envelope,
    execute_create,
    execute_fact,
    fact_request,
    prepare_authorization,
    seed_execution_policy,
)
from trading_control_plane.campaign_opening_projection import (
    UNAVAILABLE_REASONS,
    CampaignOpeningFillProjection,
    CampaignOpeningFillProjectionService,
)
from trading_control_plane.commands import CommandRejected, CommandStatus
from trading_control_plane.database import Database
from trading_control_plane.venue_facts import FeeEffect


def test_opening_fill_projection_rebuilds_native_totals_without_claiming_equity(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    proposal, campaign, initial = prepare_authorization(database)
    seed_execution_policy(database, now)
    created = execute_create(
        database,
        create_intent_envelope(proposal, campaign, initial, now=now),
    )
    order_intent_id = UUID(str(created.data["order_intent_id"]))
    partial = execute_fact(
        database,
        order_intent_id,
        fact_request(
            sequence=1,
            status="PARTIALLY_FILLED",
            filled=Decimal("0.2"),
            remaining=Decimal("0.3"),
        ),
        fill_overrides={
            "fee_amount": Decimal("0.002"),
            "fee_currency": "BNB",
            "fee_effect": FeeEffect.CHARGE,
            "realized_pnl": None,
            "settlement_currency": "USDT",
        },
    )
    filled = execute_fact(
        database,
        order_intent_id,
        fact_request(
            sequence=2,
            status="FILLED",
            filled=Decimal("0.5"),
            remaining=Decimal("0"),
            terminal=True,
        ),
        fill_overrides={
            "fee_amount": Decimal("-0.001"),
            "fee_currency": "BNB",
            "fee_effect": FeeEffect.REBATE,
            "realized_pnl": Decimal("0"),
            "settlement_currency": "USDT",
        },
    )

    assert partial.status is CommandStatus.COMPLETED
    assert filled.status is CommandStatus.COMPLETED
    with database.session_factory.begin() as session:
        projection = CampaignOpeningFillProjectionService.resolve(
            session,
            campaign.campaign_id,
        )

    assert projection.fill_count == 2
    assert projection.intent_count == 1
    assert projection.initial_fill_count == 2
    assert projection.add_fill_count == 0
    assert projection.cumulative_quantity == Decimal("0.5")
    assert projection.cumulative_notional == Decimal("25000")
    assert projection.native_fee_totals[0].currency == "BNB"
    assert projection.native_fee_totals[0].amount == Decimal("0.001")
    assert projection.known_realized_pnl_totals[0].currency == "USDT"
    assert projection.known_realized_pnl_totals[0].amount == Decimal("0")
    assert projection.realized_pnl_unknown_count == 1
    assert projection.settlement_currencies == ("USDT",)
    assert projection.economic_equity_status == "UNAVAILABLE"
    assert projection.unavailable_reasons == UNAVAILABLE_REASONS
    assert projection.facts_as_of == projection.source_entries[-1].facts_event_time

    with pytest.raises(ValueError, match="quantity is inconsistent"):
        CampaignOpeningFillProjection.model_validate(
            {
                **projection.model_dump(mode="python"),
                "cumulative_quantity": Decimal("0.6"),
            }
        )


def test_opening_fill_projection_rejects_campaign_without_accepted_fills(
    database: Database,
) -> None:
    with database.session_factory.begin() as session:
        with pytest.raises(CommandRejected) as exc_info:
            CampaignOpeningFillProjectionService.resolve(session, uuid4())

    assert exc_info.value.error_code == "CAMPAIGN_OPENING_FILL_PROJECTION_UNAVAILABLE"
