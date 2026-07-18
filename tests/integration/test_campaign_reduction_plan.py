from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tests.integration.test_campaign_target_facts import _envelope, _execute
from tests.integration.test_execution import prepare_open_add_campaign
from trading_control_plane.campaign_reduction_plan import (
    CampaignReductionExecutionPlanService,
)
from trading_control_plane.commands import CommandRejected, CommandStatus
from trading_control_plane.database import Database
from trading_control_plane.projections import ProjectionQueryContext


def test_zero_target_resolves_non_dispatchable_reduce_only_exit_plan(
    database: Database,
) -> None:
    _, campaign, _, _ = prepare_open_add_campaign(
        database,
        unrealized_pnl=Decimal("9.75"),
        protection_confirmed=False,
    )
    now = datetime.now(UTC)
    recorded = _execute(
        database,
        _envelope(
            campaign,
            now=now,
            expected_version=None,
            idempotency_key="reduction-plan-exit-target-v1",
        ),
        now=now,
    )
    assert recorded.status is CommandStatus.COMPLETED
    context = ProjectionQueryContext(as_of=now + timedelta(milliseconds=1), max_age_ms=60_000)

    with database.session_factory.begin() as session:
        plan = CampaignReductionExecutionPlanService.resolve(
            session,
            campaign.campaign_id,
            context,
        )

    assert plan.target_version == 1
    assert plan.direction == "LONG"
    assert plan.side == "SELL"
    assert plan.current_quantity == Decimal("0.5")
    assert plan.target_quantity == 0
    assert plan.order_quantity == Decimal("0.5")
    assert plan.action == "EXIT"
    assert plan.urgency == "IMMEDIATE"
    assert plan.reason_codes == ("PROTECTION_MISSING",)
    assert plan.reduce_only
    assert plan.order_type_status == "UNAVAILABLE"
    assert plan.venue_execution_terms_status == "UNAVAILABLE"
    assert plan.environment == "SHADOW"
    assert not plan.live_order_eligible


def test_hold_target_is_not_an_actionable_reduction_plan(database: Database) -> None:
    _, campaign, _, _ = prepare_open_add_campaign(database, unrealized_pnl=Decimal("9.75"))
    now = datetime.now(UTC)
    recorded = _execute(
        database,
        _envelope(
            campaign,
            now=now,
            expected_version=None,
            idempotency_key="reduction-plan-hold-target-v1",
        ),
        now=now,
    )
    assert recorded.status is CommandStatus.COMPLETED

    with database.session_factory.begin() as session:
        with pytest.raises(CommandRejected) as not_actionable:
            CampaignReductionExecutionPlanService.resolve(
                session,
                campaign.campaign_id,
                ProjectionQueryContext(
                    as_of=now + timedelta(milliseconds=1),
                    max_age_ms=60_000,
                ),
            )

    assert not_actionable.value.error_code == "CAMPAIGN_REDUCTION_TARGET_NOT_ACTIONABLE"


def test_reduction_plan_requires_a_durable_target_and_fresh_current_position(
    database: Database,
) -> None:
    _, campaign, _, _ = prepare_open_add_campaign(
        database,
        unrealized_pnl=Decimal("9.75"),
        protection_confirmed=False,
    )
    now = datetime.now(UTC)
    with database.session_factory.begin() as session:
        with pytest.raises(CommandRejected) as missing:
            CampaignReductionExecutionPlanService.resolve(
                session,
                campaign.campaign_id,
                ProjectionQueryContext(as_of=now, max_age_ms=60_000),
            )
    assert missing.value.error_code == "CAMPAIGN_REDUCTION_TARGET_MISSING"

    recorded = _execute(
        database,
        _envelope(
            campaign,
            now=now,
            expected_version=None,
            idempotency_key="reduction-plan-stale-target-v1",
        ),
        now=now,
    )
    assert recorded.status is CommandStatus.COMPLETED
    with database.session_factory.begin() as session:
        with pytest.raises(CommandRejected) as stale:
            CampaignReductionExecutionPlanService.resolve(
                session,
                campaign.campaign_id,
                ProjectionQueryContext(
                    as_of=now + timedelta(days=1),
                    max_age_ms=1_000,
                ),
            )
    assert stale.value.error_code == "CAMPAIGN_CURRENT_POSITION_UNAVAILABLE"
