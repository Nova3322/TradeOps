from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from tests.integration.test_execution import prepare_open_add_campaign
from tests.reconciliation_fixtures import (
    execute_reconciliation,
    finish_envelope,
    input_envelope,
    phase_envelope,
    start_envelope,
)
from tests.venue_fact_fixtures import funding_payment_request, venue_fact_envelope
from trading_control_plane.campaign_funding_coverage import (
    UNAVAILABLE_REASONS,
    CampaignFundingCoverageProjection,
    CampaignFundingCoverageProjectionService,
)
from trading_control_plane.campaign_opening_projection import (
    CampaignOpeningFillProjectionService,
)
from trading_control_plane.campaign_position_binding import (
    CampaignCurrentPositionBindingService,
)
from trading_control_plane.command_executor import IdempotentCommandExecutor
from trading_control_plane.commands import CommandRejected, CommandStatus
from trading_control_plane.database import Database
from trading_control_plane.projections import ProjectionQueryContext
from trading_control_plane.reconciliation import (
    REQUIRED_RECONCILIATION_SOURCES,
    ReconciliationPhase,
    ReconciliationSourceType,
    ReconciliationStatus,
)
from trading_control_plane.reconciliation_models import (
    ExecutionReconciliationInput,
    ExecutionReconciliationRun,
    ExecutionReconciliationRunState,
)
from trading_control_plane.sender_fencing import SenderScopeBinding
from trading_control_plane.sender_fencing_models import (
    ExecutionSenderScope,
    ExecutionSenderScopeState,
    ShadowDispatchClaim,
)
from trading_control_plane.venue_facts import (
    FundingEffect,
    VenueFactNormalizationService,
    VenuePositionSide,
)


def _sender_scope_binding(scope: ExecutionSenderScope) -> SenderScopeBinding:
    return SenderScopeBinding(
        organization_id=scope.organization_id,
        venue=scope.venue,
        execution_domain=scope.execution_domain,
        account_id=scope.account_id,
        account_abstraction=scope.account_abstraction,
        position_mode=scope.position_mode,
        margin_mode=scope.margin_mode,
        collateral_scope=scope.collateral_scope,
        collateral_pool_id=scope.collateral_pool_id,
    )


def _prepare_funding_reconciliation(
    database: Database,
    campaign_id: UUID,
    payments: tuple[tuple[str, Decimal, FundingEffect, datetime], ...],
    *,
    target_status: ReconciliationStatus = ReconciliationStatus.SUCCEEDED,
    observed_from: datetime | None = None,
) -> UUID:
    context = ProjectionQueryContext(as_of=datetime.now(UTC), max_age_ms=60_000)
    with database.session_factory.begin() as session:
        position = CampaignCurrentPositionBindingService.resolve(session, campaign_id, context)
        claim = session.execute(
            select(ShadowDispatchClaim).where(
                ShadowDispatchClaim.order_intent_id == position.initial_order_intent_id
            )
        ).scalar_one()
        scope = session.get(ExecutionSenderScope, claim.scope_id)
        sender_state = session.get(ExecutionSenderScopeState, claim.scope_id)
        latest = session.execute(
            select(ExecutionReconciliationRun)
            .where(ExecutionReconciliationRun.scope_id == claim.scope_id)
            .order_by(
                ExecutionReconciliationRun.started_at.desc(),
                ExecutionReconciliationRun.run_id.desc(),
            )
            .limit(1)
        ).scalar_one()
        latest_state = session.get(ExecutionReconciliationRunState, latest.run_id)
        assert scope is not None and sender_state is not None
        assert sender_state.active_lease_id is not None
        assert latest_state is not None and latest_state.completed_at is not None
        binding = _sender_scope_binding(scope)

    run_at = max(
        datetime.now(UTC),
        latest.started_at + timedelta(milliseconds=1),
        latest_state.completed_at + timedelta(milliseconds=1),
    )
    window_start = observed_from or run_at - timedelta(minutes=5)
    run_id = uuid4()
    started_envelope = start_envelope(
        run_id,
        binding,
        sender_state.active_lease_id,
        sender_state.current_fencing_token,
        now=run_at,
        supersedes_run_id=latest.run_id,
    )
    start_payload = dict(started_envelope.payload)
    start_payload["observation_window_start"] = window_start.isoformat()
    start_payload["observation_window_end"] = run_at.isoformat()
    started = execute_reconciliation(
        database,
        started_envelope.model_copy(update={"payload": start_payload}),
        now=run_at,
    )
    assert started.status is CommandStatus.COMPLETED

    version = 1
    for source_type in REQUIRED_RECONCILIATION_SOURCES:
        result = execute_reconciliation(
            database,
            input_envelope(
                run_id,
                source_type,
                now=run_at,
                expected_version=version,
                observed_from=window_start,
                observed_through=run_at,
                item_count=(
                    len(payments) if source_type is ReconciliationSourceType.VENUE_FUNDING else 0
                ),
            ),
            now=run_at,
        )
        assert result.status is CommandStatus.COMPLETED
        version += 1

    with database.session_factory.begin() as session:
        funding_input = session.execute(
            select(ExecutionReconciliationInput).where(
                ExecutionReconciliationInput.run_id == run_id,
                ExecutionReconciliationInput.source_type == "VENUE_FUNDING",
            )
        ).scalar_one()
    service = VenueFactNormalizationService(clock=lambda: run_at)
    for payment_id, amount, effect, event_time in payments:
        request = funding_payment_request(
            funding_input,
            now=run_at,
            venue_payment_id=payment_id,
            instrument_id=position.instrument_id,
            position_side=VenuePositionSide(position.position_side),
            margin_mode=position.margin_mode,
            collateral_pool_id=position.collateral_pool_id,
            funding_amount=amount,
            funding_effect=effect,
            event_time=event_time,
            venue_observed_at=event_time,
            received_at=run_at,
        )
        result = IdempotentCommandExecutor(database.session_factory).execute(
            venue_fact_envelope(
                run_id,
                service.funding_command_type,
                request.model_dump(mode="json"),
                now=run_at,
            ),
            service.record_funding_payment,
        )
        assert result.status is CommandStatus.COMPLETED

    compared = execute_reconciliation(
        database,
        phase_envelope(
            run_id,
            ReconciliationPhase.COMPARING,
            now=run_at,
            expected_version=version,
        ),
        now=run_at,
    )
    assert compared.status is CommandStatus.COMPLETED
    finished_at = run_at + timedelta(milliseconds=1)
    finished = execute_reconciliation(
        database,
        finish_envelope(
            run_id,
            target_status,
            now=finished_at,
            expected_version=version + 1,
        ),
        now=finished_at,
    )
    assert finished.status is CommandStatus.COMPLETED
    return run_id


def test_campaign_funding_coverage_projects_reconciled_candidates_without_attribution(
    database: Database,
) -> None:
    _, campaign, _, _ = prepare_open_add_campaign(database, unrealized_pnl=Decimal("9.75"))
    context = ProjectionQueryContext(as_of=datetime.now(UTC), max_age_ms=60_000)
    with database.session_factory.begin() as session:
        opening = CampaignOpeningFillProjectionService.resolve(session, campaign.campaign_id)
        position = CampaignCurrentPositionBindingService.resolve(
            session,
            campaign.campaign_id,
            context,
        )
    interval_start = min(source.facts_event_time for source in opening.source_entries)
    interval_span = position.current_facts_as_of - interval_start
    payment_time = interval_start + interval_span / 3
    receipt_time = interval_start + interval_span * 2 / 3
    _prepare_funding_reconciliation(
        database,
        campaign.campaign_id,
        (
            ("campaign-funding-payment", Decimal("2.5"), FundingEffect.PAYMENT, payment_time),
            ("campaign-funding-receipt", Decimal("-1.25"), FundingEffect.RECEIPT, receipt_time),
        ),
    )

    with database.session_factory.begin() as session:
        first = CampaignFundingCoverageProjectionService.resolve(
            session,
            campaign.campaign_id,
            context,
        )
        replay = CampaignFundingCoverageProjectionService.resolve(
            session,
            campaign.campaign_id,
            context.model_copy(update={"as_of": context.as_of + timedelta(milliseconds=1)}),
        )

    assert first.projection_hash == replay.projection_hash
    assert first.scope_interval_coverage_status == "EXACT"
    assert first.candidate_count == 2
    assert [source.funding_effect for source in first.source_payments] == [
        "PAYMENT",
        "RECEIPT",
    ]
    assert len(first.native_signed_cost_totals) == 1
    assert first.native_signed_cost_totals[0].currency == "USDT"
    assert first.native_signed_cost_totals[0].amount == Decimal("1.25")
    assert first.campaign_attribution_status == "UNAVAILABLE"
    assert first.economic_equity_status == "UNAVAILABLE"
    assert first.unavailable_reasons == UNAVAILABLE_REASONS

    with pytest.raises(ValueError, match="candidate count is inconsistent"):
        CampaignFundingCoverageProjection.model_validate(
            {**first.model_dump(mode="python"), "candidate_count": 1}
        )


def test_campaign_funding_coverage_accepts_complete_zero_count_then_rejects_latest_failure(
    database: Database,
) -> None:
    _, campaign, _, _ = prepare_open_add_campaign(database, unrealized_pnl=Decimal("9.75"))
    _prepare_funding_reconciliation(database, campaign.campaign_id, ())
    context = ProjectionQueryContext(as_of=datetime.now(UTC), max_age_ms=60_000)
    with database.session_factory.begin() as session:
        empty = CampaignFundingCoverageProjectionService.resolve(
            session,
            campaign.campaign_id,
            context,
        )
    assert empty.input_item_count == 0
    assert empty.candidate_count == 0
    assert empty.source_payments == ()
    assert empty.native_signed_cost_totals == ()

    _prepare_funding_reconciliation(
        database,
        campaign.campaign_id,
        (),
        target_status=ReconciliationStatus.FAILED,
    )
    with database.session_factory.begin() as session:
        with pytest.raises(CommandRejected) as unavailable:
            CampaignFundingCoverageProjectionService.resolve(
                session,
                campaign.campaign_id,
                context,
            )
    assert unavailable.value.error_code == "CAMPAIGN_FUNDING_COVERAGE_UNAVAILABLE"


def test_campaign_funding_coverage_rejects_latest_window_that_starts_after_campaign(
    database: Database,
) -> None:
    _, campaign, _, _ = prepare_open_add_campaign(database, unrealized_pnl=Decimal("9.75"))
    context = ProjectionQueryContext(as_of=datetime.now(UTC), max_age_ms=60_000)
    with database.session_factory.begin() as session:
        position = CampaignCurrentPositionBindingService.resolve(
            session,
            campaign.campaign_id,
            context,
        )
    _prepare_funding_reconciliation(
        database,
        campaign.campaign_id,
        (),
        observed_from=position.current_facts_as_of + timedelta(microseconds=1),
    )

    with database.session_factory.begin() as session:
        with pytest.raises(CommandRejected) as unavailable:
            CampaignFundingCoverageProjectionService.resolve(
                session,
                campaign.campaign_id,
                context,
            )
    assert unavailable.value.error_code == "CAMPAIGN_FUNDING_COVERAGE_UNAVAILABLE"
