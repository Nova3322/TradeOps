from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError

from tests.integration.test_campaign_target_facts import _envelope as target_envelope
from tests.integration.test_campaign_target_facts import _execute as execute_target
from tests.integration.test_execution import prepare_open_add_campaign
from trading_control_plane.campaign_reduction_models import (
    CampaignReductionPlanSnapshotRecord,
)
from trading_control_plane.campaign_reduction_preparation import (
    SERVICE_PRINCIPAL,
    CampaignReductionPlanPreparationService,
    reduction_plan_snapshot_from_record,
)
from trading_control_plane.command_executor import IdempotentCommandExecutor
from trading_control_plane.commands import CommandChannel, CommandEnvelope, CommandStatus
from trading_control_plane.database import Database
from trading_control_plane.execution_models import OrderIntent
from trading_control_plane.models import AuditEvent, OutboxMessage
from trading_control_plane.trading_authorization_models import Campaign


def _envelope(
    campaign: Campaign,
    *,
    now: datetime,
    expected_version: int,
    idempotency_key: str,
) -> CommandEnvelope:
    return CommandEnvelope(
        idempotency_key=idempotency_key,
        command_type=CampaignReductionPlanPreparationService.command_type,
        object_type="CampaignReductionPlan",
        object_id=str(campaign.campaign_id),
        expected_version=expected_version,
        service_principal=SERVICE_PRINCIPAL,
        channel=CommandChannel.INTERNAL,
        scope={"organization_id": campaign.organization_id},
        correlation_id=uuid4(),
        issued_at=now,
        expires_at=now + timedelta(minutes=5),
        auth_context_ref="service:campaign-reduction-preparation-test",
        payload_schema_version=1,
        reason="prepare canonical Campaign reduction plan",
        payload={
            "campaign_id": str(campaign.campaign_id),
            "organization_id": campaign.organization_id,
            "max_age_ms": 60_000,
        },
    )


def _execute(
    database: Database,
    envelope: CommandEnvelope,
    *,
    now: datetime,
):
    return IdempotentCommandExecutor(database.session_factory).execute(
        envelope,
        CampaignReductionPlanPreparationService(clock=lambda: now).prepare,
    )


def _record_exit_target(database: Database, campaign: Campaign, now: datetime) -> None:
    result = execute_target(
        database,
        target_envelope(
            campaign,
            now=now,
            expected_version=None,
            idempotency_key=f"target-for-reduction-plan:{campaign.campaign_id}",
        ),
        now=now,
    )
    assert result.status is CommandStatus.COMPLETED
    assert result.data["action"] == "EXIT"


def test_prepares_immutable_non_dispatchable_exit_snapshot(database: Database) -> None:
    _, campaign, _, _ = prepare_open_add_campaign(
        database,
        unrealized_pnl=Decimal("9.75"),
        protection_confirmed=False,
    )
    now = datetime.now(UTC)
    _record_exit_target(database, campaign, now)
    with database.session_factory.begin() as session:
        intent_count_before = session.execute(
            select(func.count()).select_from(OrderIntent)
        ).scalar_one()

    result = _execute(
        database,
        _envelope(
            campaign,
            now=now + timedelta(milliseconds=1),
            expected_version=1,
            idempotency_key="prepare-reduction-exit-v1",
        ),
        now=now + timedelta(milliseconds=1),
    )

    assert result.status is CommandStatus.COMPLETED
    assert result.object_version == 1
    assert result.data["result"] == "PREPARED"
    assert result.data["dispatch_eligible"] is False
    with database.session_factory.begin() as session:
        record = session.execute(select(CampaignReductionPlanSnapshotRecord)).scalar_one()
        snapshot = reduction_plan_snapshot_from_record(record)
        intent_count_after = session.execute(
            select(func.count()).select_from(OrderIntent)
        ).scalar_one()
        audit_count = session.execute(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.event_type == "CampaignReductionPlanPrepared")
        ).scalar_one()
        outbox_count = session.execute(
            select(func.count())
            .select_from(OutboxMessage)
            .where(OutboxMessage.event_type == "CampaignReductionPlanPrepared")
        ).scalar_one()

    plan = snapshot.plan()
    assert plan.side == "SELL"
    assert plan.action == "EXIT"
    assert plan.order_quantity == Decimal("0.5")
    assert plan.order_type_status == "UNAVAILABLE"
    assert plan.venue_execution_terms_status == "UNAVAILABLE"
    assert not plan.live_order_eligible
    assert not snapshot.dispatch_eligible
    assert intent_count_after == intent_count_before
    assert audit_count == 1
    assert outbox_count == 1


def test_preparation_replays_and_converges_on_one_target_snapshot(database: Database) -> None:
    _, campaign, _, _ = prepare_open_add_campaign(
        database,
        unrealized_pnl=Decimal("9.75"),
        protection_confirmed=False,
    )
    now = datetime.now(UTC)
    _record_exit_target(database, campaign, now)
    first_envelope = _envelope(
        campaign,
        now=now + timedelta(milliseconds=1),
        expected_version=1,
        idempotency_key="prepare-reduction-idempotent-v1",
    )
    first = _execute(database, first_envelope, now=now + timedelta(milliseconds=1))
    replay = _execute(database, first_envelope, now=now + timedelta(milliseconds=1))
    duplicate = _execute(
        database,
        _envelope(
            campaign,
            now=now + timedelta(milliseconds=2),
            expected_version=1,
            idempotency_key="prepare-reduction-second-key-v1",
        ),
        now=now + timedelta(milliseconds=2),
    )

    assert first.status is CommandStatus.COMPLETED
    assert replay.status is CommandStatus.ALREADY_PROCESSED and replay.replayed
    assert duplicate.status is CommandStatus.COMPLETED
    assert duplicate.data["result"] == "ALREADY_PREPARED"
    assert (
        duplicate.data["campaign_reduction_plan_snapshot_id"]
        == first.data["campaign_reduction_plan_snapshot_id"]
    )
    with database.session_factory.begin() as session:
        count = session.execute(
            select(func.count()).select_from(CampaignReductionPlanSnapshotRecord)
        ).scalar_one()
    assert count == 1


def test_concurrent_preparations_serialize_to_one_snapshot(database: Database) -> None:
    _, campaign, _, _ = prepare_open_add_campaign(
        database,
        unrealized_pnl=Decimal("9.75"),
        protection_confirmed=False,
    )
    now = datetime.now(UTC)
    _record_exit_target(database, campaign, now)
    prepared_at = now + timedelta(milliseconds=1)
    envelopes = tuple(
        _envelope(
            campaign,
            now=prepared_at,
            expected_version=1,
            idempotency_key=f"prepare-reduction-concurrent-{index}",
        )
        for index in range(2)
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(
            pool.map(
                lambda envelope: _execute(
                    database,
                    envelope,
                    now=prepared_at,
                ),
                envelopes,
            )
        )

    assert {result.status for result in results} == {CommandStatus.COMPLETED}
    assert {result.data["result"] for result in results} == {
        "PREPARED",
        "ALREADY_PREPARED",
    }
    assert len({result.data["campaign_reduction_plan_snapshot_id"] for result in results}) == 1
    with database.session_factory.begin() as session:
        count = session.execute(
            select(func.count()).select_from(CampaignReductionPlanSnapshotRecord)
        ).scalar_one()
    assert count == 1


def test_preparation_rejects_hold_target(database: Database) -> None:
    _, hold_campaign, _, _ = prepare_open_add_campaign(
        database,
        unrealized_pnl=Decimal("9.75"),
    )
    now = datetime.now(UTC)
    hold = execute_target(
        database,
        target_envelope(
            hold_campaign,
            now=now,
            expected_version=None,
            idempotency_key="target-hold-for-preparation",
        ),
        now=now,
    )
    assert hold.status is CommandStatus.COMPLETED
    not_actionable = _execute(
        database,
        _envelope(
            hold_campaign,
            now=now + timedelta(milliseconds=1),
            expected_version=1,
            idempotency_key="prepare-hold-rejected",
        ),
        now=now + timedelta(milliseconds=1),
    )
    assert not_actionable.status is CommandStatus.REJECTED
    assert not_actionable.error_code == "CAMPAIGN_REDUCTION_TARGET_NOT_ACTIONABLE"


def test_preparation_rejects_version_conflict_and_stale_position(
    database: Database,
) -> None:
    _, exit_campaign, _, _ = prepare_open_add_campaign(
        database,
        unrealized_pnl=Decimal("9.75"),
        protection_confirmed=False,
    )
    later = datetime.now(UTC)
    _record_exit_target(database, exit_campaign, later)
    conflict = _execute(
        database,
        _envelope(
            exit_campaign,
            now=later + timedelta(milliseconds=1),
            expected_version=2,
            idempotency_key="prepare-version-conflict",
        ),
        now=later + timedelta(milliseconds=1),
    )
    assert conflict.status is CommandStatus.REJECTED
    assert conflict.error_code == "VERSION_CONFLICT"
    stale = _execute(
        database,
        _envelope(
            exit_campaign,
            now=later + timedelta(days=1),
            expected_version=1,
            idempotency_key="prepare-stale-position",
        ),
        now=later + timedelta(days=1),
    )
    assert stale.status is CommandStatus.REJECTED
    assert stale.error_code == "CAMPAIGN_CURRENT_POSITION_UNAVAILABLE"


def test_reduction_plan_snapshot_is_database_immutable(database: Database) -> None:
    _, campaign, _, _ = prepare_open_add_campaign(
        database,
        unrealized_pnl=Decimal("9.75"),
        protection_confirmed=False,
    )
    now = datetime.now(UTC)
    _record_exit_target(database, campaign, now)
    prepared = _execute(
        database,
        _envelope(
            campaign,
            now=now + timedelta(milliseconds=1),
            expected_version=1,
            idempotency_key="prepare-immutable-plan",
        ),
        now=now + timedelta(milliseconds=1),
    )
    assert prepared.status is CommandStatus.COMPLETED

    with pytest.raises(DBAPIError):
        with database.engine.begin() as connection:
            connection.execute(
                update(CampaignReductionPlanSnapshotRecord).values(record_hash="f" * 64)
            )

    with database.session_factory.begin() as session:
        contract = (
            session.execute(select(CampaignReductionPlanSnapshotRecord)).scalar_one().contract()
        )
    contract.update(
        campaign_reduction_plan_snapshot_id=uuid4(),
        plan_idempotency_ref="forged-plan",
        plan_hash="e" * 64,
        record_hash="e" * 64,
    )
    with pytest.raises(DBAPIError):
        with database.session_factory.begin() as session:
            session.add(CampaignReductionPlanSnapshotRecord(**contract))
