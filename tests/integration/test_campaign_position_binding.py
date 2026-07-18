from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from tests.integration.test_execution import (
    create_add_envelope,
    execute_create,
    execute_fact,
    fact_request,
    prepare_open_add_campaign,
)
from trading_control_plane.campaign_position_binding import (
    UNAVAILABLE_REASONS,
    CampaignCurrentPositionBinding,
    CampaignCurrentPositionBindingService,
)
from trading_control_plane.commands import CommandRejected, CommandStatus
from trading_control_plane.database import Database
from trading_control_plane.projections import ProjectionQueryContext


def test_campaign_current_position_binds_exact_opening_prefix_without_claiming_equity(
    database: Database,
) -> None:
    _, campaign, _, _ = prepare_open_add_campaign(
        database,
        unrealized_pnl=Decimal("9.75"),
    )
    as_of = datetime.now(UTC)
    context = ProjectionQueryContext(as_of=as_of, max_age_ms=60_000)

    with database.session_factory.begin() as session:
        first = CampaignCurrentPositionBindingService.resolve(
            session,
            campaign.campaign_id,
            context,
        )
        replay = CampaignCurrentPositionBindingService.resolve(
            session,
            campaign.campaign_id,
            context.model_copy(update={"as_of": as_of + timedelta(milliseconds=1)}),
        )

    assert first.binding_hash == replay.binding_hash
    assert first.initial_quantity == Decimal("0.5")
    assert first.opening_initial_quantity == Decimal("0.5")
    assert first.opening_add_quantity == Decimal("0")
    assert first.opening_cumulative_quantity == Decimal("0.5")
    assert first.current_quantity == Decimal("0.5")
    assert first.current_entry_price == Decimal("100.5")
    assert first.current_mark_price == Decimal("120")
    assert first.current_notional == Decimal("60")
    assert first.current_unrealized_pnl == Decimal("9.75")
    assert first.quantity_consistency_status == "EXACT"
    assert first.exclusive_ownership_status == "UNAVAILABLE"
    assert first.economic_equity_status == "UNAVAILABLE"
    assert first.unavailable_reasons == UNAVAILABLE_REASONS
    assert first.current_facts_as_of >= first.opening_facts_as_of
    assert first.current_facts_as_of >= first.baseline_facts_event_time

    with pytest.raises(ValueError, match="opening prefix and current quantity diverged"):
        CampaignCurrentPositionBinding.model_validate(
            {
                **first.model_dump(mode="python"),
                "current_quantity": Decimal("0.6"),
            }
        )


def test_campaign_current_position_fails_closed_without_baseline_or_fresh_position(
    database: Database,
) -> None:
    with database.session_factory.begin() as session:
        with pytest.raises(CommandRejected) as missing:
            CampaignCurrentPositionBindingService.resolve(
                session,
                uuid4(),
                ProjectionQueryContext(as_of=datetime.now(UTC), max_age_ms=60_000),
            )
    assert missing.value.error_code == "CAMPAIGN_CURRENT_POSITION_BASELINE_UNAVAILABLE"

    _, campaign, _, _ = prepare_open_add_campaign(
        database,
        unrealized_pnl=Decimal("9.75"),
    )
    with database.session_factory.begin() as session:
        with pytest.raises(CommandRejected) as stale:
            CampaignCurrentPositionBindingService.resolve(
                session,
                campaign.campaign_id,
                ProjectionQueryContext(
                    as_of=datetime.now(UTC) + timedelta(days=1),
                    max_age_ms=1_000,
                ),
            )
    assert stale.value.error_code == "CAMPAIGN_CURRENT_POSITION_UNAVAILABLE"


def test_campaign_current_position_rejects_unreconciled_add_fill_prefix(
    database: Database,
) -> None:
    proposal, campaign, package, unit = prepare_open_add_campaign(
        database,
        unrealized_pnl=Decimal("9.75"),
    )
    add = execute_create(
        database,
        create_add_envelope(
            database,
            proposal,
            campaign,
            package,
            unit,
            now=datetime.now(UTC),
            candidate_ref="campaign-position-unreconciled-add",
        ),
    )
    assert add.status is CommandStatus.COMPLETED
    partial = execute_fact(
        database,
        UUID(str(add.data["order_intent_id"])),
        fact_request(
            sequence=1,
            status="PARTIALLY_FILLED",
            filled=Decimal("0.05"),
            remaining=Decimal("0.05"),
        ),
    )
    assert partial.status is CommandStatus.COMPLETED

    with database.session_factory.begin() as session:
        with pytest.raises(CommandRejected) as mismatch:
            CampaignCurrentPositionBindingService.resolve(
                session,
                campaign.campaign_id,
                ProjectionQueryContext(as_of=datetime.now(UTC), max_age_ms=60_000),
            )

    assert mismatch.value.error_code == "CAMPAIGN_CURRENT_POSITION_PREFIX_MISMATCH"
