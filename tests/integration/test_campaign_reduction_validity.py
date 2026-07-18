from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from tests.integration.test_campaign_reduction_preparation import (
    _envelope as preparation_envelope,
)
from tests.integration.test_campaign_reduction_preparation import _execute as execute_preparation
from tests.integration.test_campaign_target_facts import _envelope as target_envelope
from tests.integration.test_campaign_target_facts import _execute as execute_target
from tests.integration.test_execution import prepare_open_add_campaign
from trading_control_plane.campaign_reduction_validity import (
    CampaignReductionPlanValidityService,
)
from trading_control_plane.commands import CommandRejected, CommandStatus
from trading_control_plane.database import Database
from trading_control_plane.projections import ProjectionQueryContext
from trading_control_plane.trading_authorization_models import Campaign


def _prepare_exit_snapshot(
    database: Database,
) -> tuple[Campaign, UUID, datetime]:
    _, campaign, _, _ = prepare_open_add_campaign(
        database,
        unrealized_pnl=Decimal("9.75"),
        protection_confirmed=False,
    )
    target_time = datetime.now(UTC)
    target = execute_target(
        database,
        target_envelope(
            campaign,
            now=target_time,
            expected_version=None,
            idempotency_key=f"validity-target:{uuid4()}",
        ),
        now=target_time,
    )
    assert target.status is CommandStatus.COMPLETED
    prepared_at = target_time + timedelta(milliseconds=1)
    prepared = execute_preparation(
        database,
        preparation_envelope(
            campaign,
            now=prepared_at,
            expected_version=1,
            idempotency_key=f"validity-preparation:{uuid4()}",
        ),
        now=prepared_at,
    )
    assert prepared.status is CommandStatus.COMPLETED
    return (
        campaign,
        UUID(str(prepared.data["campaign_reduction_plan_snapshot_id"])),
        prepared_at,
    )


def test_current_snapshot_still_cannot_claim_dispatch_readiness(database: Database) -> None:
    _, snapshot_id, prepared_at = _prepare_exit_snapshot(database)

    with database.session_factory.begin() as session:
        validity = CampaignReductionPlanValidityService.evaluate(
            session,
            snapshot_id,
            ProjectionQueryContext(
                as_of=prepared_at + timedelta(milliseconds=1),
                max_age_ms=60_000,
            ),
        )

    assert validity.status == "CURRENT"
    assert validity.reason_code == "EXECUTION_TERMS_UNAVAILABLE"
    assert not validity.reprepare_required
    assert validity.order_type_status == "UNAVAILABLE"
    assert validity.venue_execution_terms_status == "UNAVAILABLE"
    assert not validity.dispatch_eligible


def test_snapshot_expires_without_becoming_a_dispatch_permit(database: Database) -> None:
    _, snapshot_id, prepared_at = _prepare_exit_snapshot(database)

    with database.session_factory.begin() as session:
        validity = CampaignReductionPlanValidityService.evaluate(
            session,
            snapshot_id,
            ProjectionQueryContext(
                as_of=prepared_at + timedelta(days=1),
                max_age_ms=60_000,
            ),
        )

    assert validity.status == "EXPIRED"
    assert validity.reason_code == "CAMPAIGN_REDUCTION_PLAN_EXPIRED"
    assert validity.reprepare_required
    assert not validity.dispatch_eligible


def test_new_target_version_supersedes_saved_plan(database: Database) -> None:
    campaign, snapshot_id, prepared_at = _prepare_exit_snapshot(database)
    refresh_time = prepared_at + timedelta(milliseconds=1)
    refreshed = execute_target(
        database,
        target_envelope(
            campaign,
            now=refresh_time,
            expected_version=1,
            idempotency_key="validity-target-v2",
        ),
        now=refresh_time,
    )
    assert refreshed.status is CommandStatus.COMPLETED
    assert refreshed.object_version == 2

    with database.session_factory.begin() as session:
        validity = CampaignReductionPlanValidityService.evaluate(
            session,
            snapshot_id,
            ProjectionQueryContext(
                as_of=refresh_time + timedelta(milliseconds=1),
                max_age_ms=60_000,
            ),
        )

    assert validity.status == "SUPERSEDED"
    assert validity.current_target_version == 2
    assert validity.current_target_fact_id != validity.stored_target_fact_id
    assert validity.reprepare_required
    assert not validity.dispatch_eligible


def test_missing_plan_snapshot_fails_closed(database: Database) -> None:
    with database.session_factory.begin() as session:
        with pytest.raises(CommandRejected) as missing:
            CampaignReductionPlanValidityService.evaluate(
                session,
                uuid4(),
                ProjectionQueryContext(as_of=datetime.now(UTC), max_age_ms=60_000),
            )

    assert missing.value.error_code == "CAMPAIGN_REDUCTION_PLAN_SNAPSHOT_MISSING"
