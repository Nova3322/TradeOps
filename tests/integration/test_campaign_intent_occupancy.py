from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from tests.integration.test_campaign_target_facts import _envelope as target_envelope
from tests.integration.test_campaign_target_facts import _execute as execute_target
from tests.integration.test_execution import (
    create_add_envelope,
    execute_create,
    prepare_open_add_campaign,
)
from tests.integration.test_projections import _prepare_collecting_run, _record_position
from tests.sender_fencing_fixtures import make_sender_scope
from trading_control_plane.campaign_intent_occupancy import (
    CampaignOrderIntentOccupancyService,
)
from trading_control_plane.campaign_reduction_plan import (
    CampaignReductionExecutionPlanService,
)
from trading_control_plane.commands import CommandRejected, CommandStatus
from trading_control_plane.database import Database
from trading_control_plane.projections import ProjectionQueryContext
from trading_control_plane.reconciliation import ReconciliationSourceType
from trading_control_plane.venue_fact_models import VenuePositionSnapshot
from trading_control_plane.venue_facts import (
    VenuePositionDirection,
    VenuePositionMode,
    VenuePositionSide,
    VenuePositionState,
)


@pytest.mark.parametrize("protection_confirmed", (False, True))
def test_reconciled_initial_intent_does_not_occupy_reduction_planning(
    database: Database,
    protection_confirmed: bool,
) -> None:
    _, campaign, _, _ = prepare_open_add_campaign(
        database,
        unrealized_pnl=Decimal("9.75"),
        protection_confirmed=protection_confirmed,
    )

    with database.session_factory.begin() as session:
        occupancy = CampaignOrderIntentOccupancyService.resolve(
            session,
            campaign.campaign_id,
            lock=True,
        )

    assert occupancy.status == "CLEAR"
    assert occupancy.observed_intent_count == 1
    assert occupancy.stable_intent_count == 1
    assert occupancy.blocking_intents == ()


def test_active_add_intent_occupies_reduction_planning(database: Database) -> None:
    proposal, campaign, package, unit = prepare_open_add_campaign(
        database,
        unrealized_pnl=Decimal("9.75"),
    )
    created = execute_create(
        database,
        create_add_envelope(
            database,
            proposal,
            campaign,
            package,
            unit,
            now=datetime.now(UTC),
            candidate_ref="occupancy-active-add",
        ),
    )
    assert created.status is CommandStatus.COMPLETED

    with database.session_factory.begin() as session:
        occupancy = CampaignOrderIntentOccupancyService.resolve(
            session,
            campaign.campaign_id,
        )

    assert occupancy.status == "BLOCKED"
    assert occupancy.observed_intent_count == 2
    assert occupancy.stable_intent_count == 1
    assert len(occupancy.blocking_intents) == 1
    blocker = occupancy.blocking_intents[0]
    assert blocker.intent_kind == "ADD"
    assert blocker.status == "INTENT_CREATED"
    assert blocker.intent_quantity == Decimal("0.1")
    assert blocker.known_remaining_quantity == Decimal("0.1")


def test_active_add_blocks_plan_after_new_position_loses_protection(
    database: Database,
) -> None:
    proposal, campaign, package, unit = prepare_open_add_campaign(
        database,
        unrealized_pnl=Decimal("9.75"),
    )
    created = execute_create(
        database,
        create_add_envelope(
            database,
            proposal,
            campaign,
            package,
            unit,
            now=datetime.now(UTC),
            candidate_ref="occupancy-plan-blocked-add",
        ),
    )
    assert created.status is CommandStatus.COMPLETED
    with database.session_factory.begin() as session:
        current = (
            session.execute(
                select(VenuePositionSnapshot).order_by(
                    VenuePositionSnapshot.event_time.desc(),
                    VenuePositionSnapshot.venue_position_snapshot_id.desc(),
                )
            )
            .scalars()
            .first()
        )
        assert current is not None

    run_id, _, run_time, inputs = _prepare_collecting_run(
        database,
        position_count=1,
        sender_scope=make_sender_scope(
            account_abstraction=f"POSITION_COLLECTOR_{uuid4().hex}",
            margin_mode="ISOLATED",
            collateral_pool_id=current.collateral_pool_id,
        ),
    )
    normalized_at = run_time + timedelta(seconds=1)
    _, recorded = _record_position(
        database,
        run_id,
        inputs[ReconciliationSourceType.VENUE_POSITIONS],
        now=normalized_at,
        venue_update_id="occupancy-latest-unprotected-position",
        instrument_id=current.instrument_id,
        position_mode=VenuePositionMode(current.position_mode),
        position_side=VenuePositionSide(current.position_side),
        margin_mode=current.margin_mode,
        collateral_pool_id=current.collateral_pool_id,
        position_state=VenuePositionState.OPEN,
        direction=VenuePositionDirection.LONG,
        quantity=current.quantity,
        entry_price=current.entry_price,
        mark_price=current.mark_price,
        contract_multiplier=current.contract_multiplier,
        notional=current.notional,
        unrealized_pnl=current.unrealized_pnl,
        liquidation_price=current.liquidation_price,
        leverage=current.leverage,
        initial_margin=current.initial_margin,
        maintenance_margin=current.maintenance_margin,
        settlement_currency=current.settlement_currency,
        event_time=run_time,
    )
    assert recorded.status is CommandStatus.COMPLETED
    target_time = normalized_at + timedelta(milliseconds=1)
    target = execute_target(
        database,
        target_envelope(
            campaign,
            now=target_time,
            expected_version=None,
            idempotency_key="occupancy-target-after-protection-loss",
        ),
        now=target_time,
    )
    assert target.status is CommandStatus.COMPLETED
    assert target.data["action"] == "EXIT"

    with database.session_factory.begin() as session:
        with pytest.raises(CommandRejected) as occupied:
            CampaignReductionExecutionPlanService.resolve(
                session,
                campaign.campaign_id,
                ProjectionQueryContext(as_of=target_time, max_age_ms=60_000),
                lock_intents=True,
            )

    assert occupied.value.error_code == "CAMPAIGN_REDUCTION_INTENT_OCCUPIED"
