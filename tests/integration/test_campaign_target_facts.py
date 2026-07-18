from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError

from tests.integration.test_execution import (
    execute_fact,
    fact_request,
    prepare_open_add_campaign,
)
from trading_control_plane.campaign_target_facts import (
    SERVICE_PRINCIPAL,
    CampaignTargetPositionFactService,
    target_fact_from_record,
)
from trading_control_plane.campaign_target_models import CampaignTargetPositionFactRecord
from trading_control_plane.command_executor import IdempotentCommandExecutor
from trading_control_plane.commands import CommandChannel, CommandEnvelope, CommandStatus
from trading_control_plane.database import Database
from trading_control_plane.execution_models import OrderIntent
from trading_control_plane.models import AuditEvent, OutboxMessage
from trading_control_plane.trading_authorization_models import Campaign, CampaignState


def _envelope(
    campaign: Campaign,
    *,
    now: datetime,
    expected_version: int | None,
    idempotency_key: str,
) -> CommandEnvelope:
    return CommandEnvelope(
        idempotency_key=idempotency_key,
        command_type=CampaignTargetPositionFactService.command_type,
        object_type="CampaignTargetPosition",
        object_id=str(campaign.campaign_id),
        expected_version=expected_version,
        service_principal=SERVICE_PRINCIPAL,
        channel=CommandChannel.INTERNAL,
        scope={"organization_id": campaign.organization_id},
        correlation_id=uuid4(),
        issued_at=now,
        expires_at=now + timedelta(minutes=5),
        auth_context_ref="service:campaign-target-test",
        payload_schema_version=1,
        reason="evaluate canonical Campaign target",
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
        CampaignTargetPositionFactService(clock=lambda: now).evaluate_and_record,
    )


def test_exact_protection_records_immutable_campaign_hold_target(database: Database) -> None:
    _, campaign, _, _ = prepare_open_add_campaign(database, unrealized_pnl=Decimal("9.75"))
    now = datetime.now(UTC)
    result = _execute(
        database,
        _envelope(
            campaign,
            now=now,
            expected_version=None,
            idempotency_key="campaign-target-hold-v1",
        ),
        now=now,
    )

    assert result.status is CommandStatus.COMPLETED
    assert result.object_version == 1
    assert result.data["result"] == "RECORDED"
    assert result.data["action"] == "HOLD"
    with database.session_factory.begin() as session:
        record = session.execute(select(CampaignTargetPositionFactRecord)).scalar_one()
        snapshot = target_fact_from_record(record)
        state = session.get(CampaignState, campaign.campaign_id)
        event_count = session.execute(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.event_type == "CampaignTargetPositionRecorded")
        ).scalar_one()
        outbox_count = session.execute(
            select(func.count())
            .select_from(OutboxMessage)
            .where(OutboxMessage.event_type == "CampaignTargetPositionRecorded")
        ).scalar_one()

    assert snapshot.target_version == 1
    assert snapshot.current_quantity == Decimal("0.5")
    assert snapshot.target_quantity == Decimal("0.5")
    assert snapshot.action == "HOLD"
    assert not snapshot.requires_order
    assert snapshot.reduce_only_required
    assert snapshot.environment == "SHADOW"
    assert not snapshot.live_order_eligible
    assert state is not None and state.status == "OPEN"
    assert event_count == 1
    assert outbox_count == 1


def test_missing_protection_records_zero_target_and_closes_campaign_for_execution(
    database: Database,
) -> None:
    _, campaign, _, _ = prepare_open_add_campaign(
        database,
        unrealized_pnl=Decimal("9.75"),
        protection_confirmed=False,
    )
    now = datetime.now(UTC)
    result = _execute(
        database,
        _envelope(
            campaign,
            now=now,
            expected_version=None,
            idempotency_key="campaign-target-exit-v1",
        ),
        now=now,
    )

    assert result.status is CommandStatus.COMPLETED
    assert result.object_version == 1
    assert result.data["action"] == "EXIT"
    assert result.data["target_quantity"] == "0"
    with database.session_factory.begin() as session:
        snapshot = target_fact_from_record(
            session.execute(select(CampaignTargetPositionFactRecord)).scalar_one()
        )
        state = session.get(CampaignState, campaign.campaign_id)

    assert snapshot.target_quantity == 0
    assert snapshot.reduction_quantity == Decimal("0.5")
    assert snapshot.requires_order
    assert snapshot.reduce_only_required
    assert snapshot.urgency == "IMMEDIATE"
    assert snapshot.all_reason_codes == ("PROTECTION_MISSING",)
    assert state is not None
    assert state.status == "CLOSING"
    assert state.reason_code == "TARGET_POSITION_ZERO_RECORDED"


def test_campaign_target_command_replays_and_semantic_refresh_is_no_change(
    database: Database,
) -> None:
    _, campaign, _, _ = prepare_open_add_campaign(
        database,
        unrealized_pnl=Decimal("9.75"),
        protection_confirmed=False,
    )
    now = datetime.now(UTC)
    first_envelope = _envelope(
        campaign,
        now=now,
        expected_version=None,
        idempotency_key="campaign-target-idempotent-v1",
    )
    first = _execute(database, first_envelope, now=now)
    replay = _execute(database, first_envelope, now=now)
    refresh_time = now + timedelta(milliseconds=1)
    refresh = _execute(
        database,
        _envelope(
            campaign,
            now=refresh_time,
            expected_version=1,
            idempotency_key="campaign-target-no-change-v1",
        ),
        now=refresh_time,
    )

    assert first.status is CommandStatus.COMPLETED
    assert replay.status is CommandStatus.ALREADY_PROCESSED and replay.replayed
    assert refresh.status is CommandStatus.COMPLETED
    assert refresh.object_version == 1
    assert refresh.data["result"] == "NO_CHANGE"
    with database.session_factory.begin() as session:
        count = session.execute(
            select(func.count()).select_from(CampaignTargetPositionFactRecord)
        ).scalar_one()
    assert count == 1


def test_campaign_target_cannot_relax_zero_target_after_protection_recovers(
    database: Database,
) -> None:
    _, campaign, _, _ = prepare_open_add_campaign(
        database,
        unrealized_pnl=Decimal("9.75"),
        protection_confirmed=False,
    )
    first_time = datetime.now(UTC)
    first = _execute(
        database,
        _envelope(
            campaign,
            now=first_time,
            expected_version=None,
            idempotency_key="campaign-target-tight-v1",
        ),
        now=first_time,
    )
    assert first.status is CommandStatus.COMPLETED

    with database.session_factory.begin() as session:
        initial_intent_id = session.execute(
            select(OrderIntent.order_intent_id).where(OrderIntent.intent_kind == "INITIAL")
        ).scalar_one()
    protection = execute_fact(
        database,
        UUID(str(initial_intent_id)),
        fact_request(
            sequence=3,
            status="PROTECTION_CONFIRMED",
            filled=Decimal("0.5"),
            remaining=Decimal("0"),
            terminal=True,
            reconciled=True,
            protected=True,
        ),
        protection_snapshot_overrides={"worst_active_trigger_price": Decimal("110")},
    )
    assert protection.status is CommandStatus.COMPLETED

    second_time = datetime.now(UTC)
    relaxed = _execute(
        database,
        _envelope(
            campaign,
            now=second_time,
            expected_version=1,
            idempotency_key="campaign-target-relax-v2",
        ),
        now=second_time,
    )

    assert relaxed.status is CommandStatus.REJECTED
    assert relaxed.error_code == "CAMPAIGN_TARGET_POSITION_RELAXATION_FORBIDDEN"
    with database.session_factory.begin() as session:
        count = session.execute(
            select(func.count()).select_from(CampaignTargetPositionFactRecord)
        ).scalar_one()
        state = session.get(CampaignState, campaign.campaign_id)
    assert count == 1
    assert state is not None and state.status == "CLOSING"


def test_campaign_target_version_conflict_and_database_immutability(database: Database) -> None:
    _, campaign, _, _ = prepare_open_add_campaign(database, unrealized_pnl=Decimal("9.75"))
    now = datetime.now(UTC)
    first = _execute(
        database,
        _envelope(
            campaign,
            now=now,
            expected_version=None,
            idempotency_key="campaign-target-version-v1",
        ),
        now=now,
    )
    assert first.status is CommandStatus.COMPLETED
    conflict_time = now + timedelta(milliseconds=1)
    conflict = _execute(
        database,
        _envelope(
            campaign,
            now=conflict_time,
            expected_version=None,
            idempotency_key="campaign-target-version-conflict",
        ),
        now=conflict_time,
    )
    assert conflict.status is CommandStatus.REJECTED
    assert conflict.error_code == "VERSION_CONFLICT"

    with pytest.raises(DBAPIError):
        with database.engine.begin() as connection:
            connection.execute(
                update(CampaignTargetPositionFactRecord).values(record_hash="f" * 64)
            )

    with database.session_factory.begin() as session:
        contract = session.execute(select(CampaignTargetPositionFactRecord)).scalar_one().contract()
    contract.update(
        campaign_target_position_fact_id=uuid4(),
        target_version=2,
        decision_payload={},
        target_semantic_hash="e" * 64,
        record_hash="e" * 64,
    )
    with pytest.raises(DBAPIError):
        with database.session_factory.begin() as session:
            session.add(CampaignTargetPositionFactRecord(**contract))
