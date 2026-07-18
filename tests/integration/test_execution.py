from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from tests.integration.test_capital_scope import _register
from tests.integration.test_projections import _prepare_collecting_run, _record_account_equity
from tests.integration.test_trading_authorization import (
    execute_issue,
    issue_envelope,
    prepare_approved,
)
from tests.reconciliation_fixtures import (
    collect_complete_inputs,
    complete_successful_reconciliation,
    execute_reconciliation,
    finish_envelope,
    phase_envelope,
    start_envelope,
)
from tests.risk_fixtures import (
    TEST_EXECUTION_CAPITAL_PROJECTION_BINDING,
    TEST_EXECUTION_CAPITAL_SCOPE_MANIFEST,
    make_capital,
    make_policy,
    make_request,
    make_requested,
)
from tests.sender_fencing_fixtures import (
    acquire_envelope,
    claim_envelope,
    execute_acquire,
    execute_claim,
    make_sender_scope,
)
from tests.venue_fact_fixtures import (
    execute_venue_fact,
    fill_request,
    order_observation_request,
    position_snapshot_request,
    protection_snapshot_request,
    venue_fact_envelope,
)
from trading_control_plane.capability_certificate_models import CapabilityCertificate
from trading_control_plane.capability_certificates import CapabilityScope
from trading_control_plane.command_executor import IdempotentCommandExecutor
from trading_control_plane.commands import (
    CommandChannel,
    CommandEnvelope,
    CommandRejected,
    CommandStatus,
    hash_json,
)
from trading_control_plane.database import Database
from trading_control_plane.execution import (
    EXECUTION_INTENT_SERVICE_PRINCIPAL,
    EXECUTION_RECONCILIATION_SERVICE_PRINCIPAL,
    CreateExecutionIntentRequest,
    DurableExposureResolver,
    ExecutionFactKind,
    ExecutionIntentService,
    ExecutionReconciliationService,
    RecordExecutionFactRequest,
)
from trading_control_plane.execution_models import (
    ExecutionFact,
    ExecutionRiskDecision,
    OrderIntent,
    OrderIntentState,
    OrderIntentStateHistory,
    RiskExposureState,
    RiskExposureStateHistory,
    RiskLedgerEntry,
    RiskReservation,
)
from trading_control_plane.models import AuditEvent, OutboxMessage
from trading_control_plane.proposal_models import FrozenProposalVersion
from trading_control_plane.reconciliation import (
    ReconciliationPhase,
    ReconciliationSourceType,
    ReconciliationStatus,
    ReconciliationTriggerType,
)
from trading_control_plane.reconciliation_models import (
    ExecutionReconciliationInput,
    ExecutionReconciliationRun,
    ExecutionReconciliationRunState,
)
from trading_control_plane.risk import (
    CertificationBinding,
    MarketRiskInput,
    PositionDirection,
    RiskPrecheckRequest,
    RiskPrecheckService,
    ScopeLimit,
    ScopeRiskInput,
    ScopeType,
    TradeLossComponents,
)
from trading_control_plane.risk_models import RiskDecisionSnapshot, RiskPolicyRecord
from trading_control_plane.sender_fencing import SenderScopeBinding, sender_scope_id
from trading_control_plane.sender_fencing_models import (
    ExecutionSenderScope,
    ExecutionSenderScopeState,
    ShadowDispatchClaim,
)
from trading_control_plane.trading_authorization_models import (
    AddAuthorizationPackage,
    AddAuthorizationPackageState,
    AddUnit,
    AddUnitState,
    Campaign,
    CampaignState,
    InitialAuthorizationState,
    InitialOrderAuthorization,
)
from trading_control_plane.venue_fact_models import (
    VenueFactInputLink,
    VenueFill,
    VenueOrderObservation,
    VenuePositionSnapshot,
    VenueProtectionSnapshot,
)
from trading_control_plane.venue_facts import (
    FeeEffect,
    VenueFactNormalizationService,
    VenueOrderStatus,
    VenuePositionDirection,
    VenuePositionMode,
    VenuePositionSide,
    VenueSide,
)

pytestmark = pytest.mark.integration

EXECUTION_SCOPE_IDS = {
    ScopeType.UNDERLYING: "BTC",
    ScopeType.RISK_CLUSTER: "CRYPTO_MAJOR",
    ScopeType.SECTOR: "CRYPTO",
    ScopeType.EXECUTION_DOMAIN: "BINANCE_USDM",
    ScopeType.VENUE: "BINANCE",
    ScopeType.COLLATERAL_POOL: "pool-usdt-1",
    ScopeType.PORTFOLIO: "org-1",
}


def count_rows(session: Session, model: type[object]) -> int:
    return session.execute(select(func.count()).select_from(model)).scalar_one()


def seed_execution_policy(database: Database, now: datetime) -> None:
    policy = make_policy(
        scope_limits=tuple(
            ScopeLimit(
                scope_type=scope_type,
                scope_id=scope_id,
                planned_loss_cap=Decimal("10000"),
                stress_loss_cap=Decimal("15000"),
            )
            for scope_type, scope_id in EXECUTION_SCOPE_IDS.items()
        )
    )
    parameters = policy.model_dump(mode="json")
    with database.session_factory.begin() as session:
        session.add(
            RiskPolicyRecord(
                risk_policy_id=uuid4(),
                organization_id="org-1",
                policy_version="risk-policy-v1",
                policy_mode="SHADOW",
                parameters=parameters,
                policy_hash=hash_json(parameters),
                evidence_refs=["test-only:wp-0006-risk-policy"],
                valid_from=now - timedelta(days=1),
                valid_until=now + timedelta(days=1),
                created_at=now,
            )
        )


def record_execution_capital_equity(
    database: Database,
    *,
    exchange_margin_equity: Decimal = Decimal("100000"),
    total_unrealized_pnl: Decimal = Decimal("0"),
    available_margin: Decimal = Decimal("10000"),
) -> datetime:
    collector_scope = make_sender_scope(
        account_abstraction=f"CAPITAL_COLLECTOR_{uuid4().hex}",
        margin_mode="ISOLATED",
        collateral_pool_id="pool-usdt-1",
    )
    run_id, _, run_time, inputs = _prepare_collecting_run(
        database,
        balance_count=1,
        sender_scope=collector_scope,
    )
    normalized_at = run_time + timedelta(seconds=1)
    request, recorded = _record_account_equity(
        database,
        run_id,
        inputs[ReconciliationSourceType.VENUE_BALANCES],
        now=normalized_at,
        venue_update_id=f"execution-equity-{uuid4()}",
        margin_mode="ISOLATED",
        collateral_pool_id="pool-usdt-1",
        settlement_currency="USD",
        wallet_balance=exchange_margin_equity - total_unrealized_pnl,
        exchange_margin_equity=exchange_margin_equity,
        available_margin=available_margin,
        total_unrealized_pnl=total_unrealized_pnl,
    )
    assert recorded.status is CommandStatus.COMPLETED
    return request.event_time


def seed_execution_capital_projection(database: Database) -> datetime:
    registered = _register(
        database,
        TEST_EXECUTION_CAPITAL_SCOPE_MANIFEST,
        now=datetime.now(UTC),
    )
    assert registered.status is CommandStatus.COMPLETED
    return record_execution_capital_equity(database)


def prepare_authorization(
    database: Database, *, auto_add: bool = False
) -> tuple[FrozenProposalVersion, Campaign, InitialOrderAuthorization]:
    proposal, decision = prepare_approved(
        database,
        auto_add_enabled=auto_add,
        requested_add_count=1 if auto_add else 0,
    )
    result = execute_issue(
        database,
        issue_envelope(
            proposal.proposal_version_id,
            proposal.version,
            proposal.spec_hash,
            proposal.risk_summary_hash,
            decision.approval_decision_id,
        ),
    )
    assert result.status is CommandStatus.COMPLETED
    seed_execution_capital_projection(database)
    with database.session_factory.begin() as session:
        campaign = session.execute(select(Campaign)).scalar_one()
        initial = session.execute(select(InitialOrderAuthorization)).scalar_one()
    return proposal, campaign, initial


def execution_risk_request(
    proposal: FrozenProposalVersion,
    *,
    now: datetime,
    quantity: Decimal = Decimal("0.5"),
    requested_heat: Decimal = Decimal("100"),
    requested_cost: Decimal = Decimal("10"),
    requested_funding: Decimal = Decimal("500"),
    current_open_heat: Decimal = Decimal("0"),
    current_reserved_heat: Decimal = Decimal("0"),
    current_unknown_heat: Decimal = Decimal("0"),
    funding_used: Decimal = Decimal("0"),
    funding_reserved: Decimal = Decimal("0"),
    scope_current_planned: Decimal = Decimal("0"),
    scope_current_stress: Decimal = Decimal("0"),
    fact_age: timedelta = timedelta(milliseconds=100),
) -> RiskPrecheckRequest:
    requested = make_requested(
        requested_quantity=quantity,
        requested_reserved_heat=requested_heat,
        requested_cost_stress_add_on=requested_cost,
        requested_funding=requested_funding,
        requested_margin=Decimal("500"),
        requested_effective_leverage=Decimal("2"),
        proposal_requested_loss_cap=Decimal("500"),
    )
    scopes = tuple(
        ScopeRiskInput(
            scope_type=scope_type,
            scope_id=scope_id,
            current_planned_loss=scope_current_planned,
            requested_incremental_planned_loss=requested.incremental_worst_case_loss,
            current_stress_loss=scope_current_stress,
            requested_incremental_stress_loss=requested.incremental_worst_case_loss + Decimal("40"),
        )
        for scope_type, scope_id in EXECUTION_SCOPE_IDS.items()
    )
    base = make_request(
        now=now,
        requested=requested,
        capital=make_capital(funding_used=funding_used, funding_reserved=funding_reserved),
        current_trade_loss=TradeLossComponents(
            open_heat=current_open_heat,
            reserved_heat=current_reserved_heat,
            unknown_heat=current_unknown_heat,
            protected_profit_giveback=Decimal("0"),
            cost_stress_add_on=Decimal("0"),
        ),
        market=MarketRiskInput(
            direction=PositionDirection.LONG,
            mark_price=Decimal("100.5"),
            index_price=Decimal("100.4"),
            executable_price=Decimal("100.5"),
            initial_invalidation_price=Decimal("90"),
            contract_multiplier=Decimal("1"),
            tick_size=Decimal("0.1"),
            minimum_quantity=Decimal("0.001"),
            minimum_notional=Decimal("5"),
            funding_rate=Decimal("0.0001"),
            max_slippage_bps=Decimal("20"),
            contract_rules_version="rules-test-v1",
            loss_model_version="loss-model-test-v1",
            loss_calculation_ref="test-only:wp-0006-loss",
        ),
        scope_risks=scopes,
        fact_age=fact_age,
    )
    values = base.model_dump(mode="python")
    values.update(
        {
            "proposal_ref": str(proposal.proposal_version_id),
            "candidate_version": proposal.version,
            "policy_version": "risk-policy-v1",
            "capital_projection_binding": TEST_EXECUTION_CAPITAL_PROJECTION_BINDING,
            "binding": CertificationBinding(
                proposal_source=proposal.source,
                strategy_id="trend-breakout",
                strategy_version="1.0.0",
                strategy_parameter_version="params-v1",
                authorization_policy_version="authorization-policy-v1",
                instrument_identity="BINANCE:BTCUSDT-PERP",
                contract_multiplier=Decimal("1"),
                underlying_id="BTC",
                sector_id="CRYPTO",
                risk_cluster_id="CRYPTO_MAJOR",
                venue="BINANCE",
                execution_domain="BINANCE_USDM",
                account_id="account-1",
                account_abstraction="UNIFIED",
                position_mode="ONE_WAY",
                margin_mode="ISOLATED",
                collateral_scope="ACCOUNT",
                collateral_pool_id="pool-usdt-1",
                settlement_asset="USD",
                adapter_version="binance-adapter-v1",
                worker_id="freqtrade-binance-account-1-isolated",
                worker_config_hash="a" * 64,
                credential_fingerprint="b" * 64,
                freqtrade_worker_version="freqtrade-worker-v1",
                account_capability_version="account-capability-v1",
                credential_permission_profile_version="trade-no-withdraw-v1",
                venue_client_version="ccxt-test-v1",
                instrument_scope_version="whitelist-test-v1",
                catalog_version="catalog-v1",
                execution_capability_version="shadow-only-v1",
                position_management_template_version="position-template-v1",
                add_milestone_policy_version="add-milestones-30-50-100-v1",
                requested_add_count=proposal.requested_add_count,
                capability_certificate_ref="capability:test-shadow-only",
            ),
        }
    )
    return RiskPrecheckRequest.model_validate(values)


def create_intent_envelope(
    proposal: FrozenProposalVersion,
    campaign: Campaign,
    initial: InitialOrderAuthorization,
    *,
    now: datetime,
    candidate_ref: str | None = None,
    risk_request: RiskPrecheckRequest | None = None,
    idempotency_key: str | None = None,
) -> CommandEnvelope:
    request = risk_request or execution_risk_request(proposal, now=now)
    return CommandEnvelope(
        idempotency_key=idempotency_key or f"execution-intent-{uuid4()}",
        command_type="execution.intent.create.v1",
        object_type="Campaign",
        object_id=str(campaign.campaign_id),
        expected_version=1,
        service_principal=EXECUTION_INTENT_SERVICE_PRINCIPAL,
        channel=CommandChannel.INTERNAL,
        scope={"organization_id": "org-1"},
        correlation_id=uuid4(),
        issued_at=now,
        expires_at=now + timedelta(minutes=2),
        auth_context_ref="internal:oms-risk-reservation-service",
        payload_schema_version=1,
        reason="create non-dispatchable shadow intent",
        payload={
            "intent_kind": "INITIAL",
            "candidate_ref": candidate_ref or f"initial-candidate-{uuid4()}",
            "initial_authorization_id": str(initial.initial_authorization_id),
            "add_package_id": None,
            "add_unit_id": None,
            "current_position_quantity": "0",
            "target_position_quantity": str(request.requested.requested_quantity),
            "order_type": "LIMIT",
            "time_in_force": "GTC",
            "risk_currency": "USD",
            "valuation_price_source_ref": "test-only:mark-price-snapshot",
            "risk_request": request.model_dump(mode="json"),
            "add_eligibility": None,
        },
    )


def proposal_precheck_envelope(request: RiskPrecheckRequest) -> CommandEnvelope:
    now = datetime.now(UTC)
    return CommandEnvelope(
        idempotency_key=f"proposal-risk-precheck-{uuid4()}",
        command_type=RiskPrecheckService.command_type,
        object_type="ProposalCandidate",
        object_id=request.proposal_ref,
        expected_version=request.candidate_version,
        service_principal="trading:proposal-test",
        channel=CommandChannel.INTERNAL,
        scope={"organization_id": request.organization_id},
        correlation_id=uuid4(),
        issued_at=now,
        expires_at=now + timedelta(minutes=2),
        auth_context_ref="test-only:proposal-precheck-auth",
        payload_schema_version=1,
        reason="evaluate proposal against existing durable exposure",
        payload=request.model_dump(mode="json"),
    )


def create_add_envelope(
    proposal: FrozenProposalVersion,
    campaign: Campaign,
    package: AddAuthorizationPackage,
    unit: AddUnit,
    *,
    now: datetime,
    candidate_ref: str,
) -> CommandEnvelope:
    request = execution_risk_request(
        proposal,
        now=now,
        quantity=Decimal("0.1"),
        requested_heat=Decimal("50"),
        requested_cost=Decimal("5"),
        requested_funding=Decimal("200"),
        current_open_heat=Decimal("110"),
        funding_used=Decimal("500"),
        scope_current_planned=Decimal("110"),
        scope_current_stress=Decimal("150"),
    )
    return CommandEnvelope(
        idempotency_key=f"execution-add-{uuid4()}",
        command_type="execution.intent.create.v1",
        object_type="Campaign",
        object_id=str(campaign.campaign_id),
        expected_version=2,
        service_principal=EXECUTION_INTENT_SERVICE_PRINCIPAL,
        channel=CommandChannel.INTERNAL,
        scope={"organization_id": "org-1"},
        correlation_id=uuid4(),
        issued_at=now,
        expires_at=now + timedelta(minutes=2),
        auth_context_ref="internal:oms-risk-reservation-service",
        payload_schema_version=1,
        reason="create non-dispatchable shadow add intent",
        payload={
            "intent_kind": "ADD",
            "candidate_ref": candidate_ref,
            "initial_authorization_id": None,
            "add_package_id": str(package.add_package_id),
            "add_unit_id": str(unit.add_unit_id),
            "current_position_quantity": "0.5",
            "target_position_quantity": "0.6",
            "order_type": "LIMIT",
            "time_in_force": "GTC",
            "risk_currency": "USD",
            "valuation_price_source_ref": "test-only:add-mark-price-snapshot",
            "risk_request": request.model_dump(mode="json"),
            "add_eligibility": {
                "frozen_return_pct": "30",
                "trend_valid": True,
                "protection_valid": True,
                "authorization_valid": True,
                "current_effective_leverage": "0.5",
                "target_effective_leverage": "1",
                "current_position_equity": "60.3",
                "position_snapshot_ref": "test-only:position-snapshot",
                "position_snapshot_hash": hash_json({"position": "initial-open"}),
                "protection_snapshot_ref": "test-only:protection-snapshot",
                "protection_snapshot_hash": hash_json({"protection": "confirmed"}),
            },
        },
    )


def execute_create(
    database: Database,
    envelope: CommandEnvelope,
    service: ExecutionIntentService | None = None,
):
    return IdempotentCommandExecutor(database.session_factory).execute(
        envelope, (service or ExecutionIntentService()).create
    )


@dataclass(frozen=True)
class ExecutionFactDraft:
    sequence: int
    status: str
    filled: Decimal
    remaining: Decimal
    zero: bool
    terminal: bool
    reconciled: bool
    protected: bool
    external_fact_id: str


@dataclass(frozen=True)
class CanonicalVenueFactContext:
    fact: VenueOrderObservation | VenueFill | VenuePositionSnapshot | VenueProtectionSnapshot
    input_link: VenueFactInputLink


def fact_request(
    *,
    sequence: int,
    status: str,
    filled: Decimal,
    remaining: Decimal,
    zero: bool = False,
    terminal: bool = False,
    reconciled: bool = False,
    protected: bool = False,
    external_fact_id: str | None = None,
) -> ExecutionFactDraft:
    return ExecutionFactDraft(
        sequence=sequence,
        status=status,
        filled=filled,
        remaining=remaining,
        zero=zero,
        terminal=terminal,
        reconciled=reconciled,
        protected=protected,
        external_fact_id=external_fact_id or f"venue-fact-{uuid4()}",
    )


def _fact_kind_and_source(
    status: str,
) -> tuple[ExecutionFactKind, ReconciliationSourceType]:
    if status in {"PARTIALLY_FILLED", "FILLED"}:
        return ExecutionFactKind.VENUE_FILL, ReconciliationSourceType.VENUE_FILLS
    if status == "POSITION_RECONCILED":
        return ExecutionFactKind.VENUE_POSITION, ReconciliationSourceType.VENUE_POSITIONS
    if status in {"PROTECTION_CONFIRMED", "COMPLETED"}:
        return ExecutionFactKind.VENUE_PROTECTION, ReconciliationSourceType.VENUE_PROTECTION
    if status == "DISPATCHING":
        return ExecutionFactKind.WORKER_RECEIPT, ReconciliationSourceType.WORKER_LOCAL
    return ExecutionFactKind.VENUE_ORDER, ReconciliationSourceType.VENUE_ORDERS


def _scope_from_persisted(row: ExecutionSenderScope) -> SenderScopeBinding:
    return SenderScopeBinding(
        organization_id=row.organization_id,
        venue=row.venue,
        execution_domain=row.execution_domain,
        account_id=row.account_id,
        account_abstraction=row.account_abstraction,
        position_mode=row.position_mode,
        margin_mode=row.margin_mode,
        collateral_scope=row.collateral_scope,
        collateral_pool_id=row.collateral_pool_id,
    )


def ensure_shadow_claim(
    database: Database, order_intent_id: UUID, *, now: datetime
) -> tuple[ShadowDispatchClaim, SenderScopeBinding]:
    with database.session_factory.begin() as session:
        existing_claim = session.execute(
            select(ShadowDispatchClaim).where(
                ShadowDispatchClaim.order_intent_id == order_intent_id
            )
        ).scalar_one_or_none()
        if existing_claim is not None:
            scope_row = session.get(ExecutionSenderScope, existing_claim.scope_id)
            assert scope_row is not None
            return existing_claim, _scope_from_persisted(scope_row)
        intent = session.get(OrderIntent, order_intent_id)
        assert intent is not None
        certificate = session.get(CapabilityCertificate, intent.capability_certificate_ref)
        assert certificate is not None
        certificate_scope = CapabilityScope.model_validate(certificate.scope)

    scope = make_sender_scope(
        organization_id="org-1",
        venue=certificate_scope.venue,
        execution_domain=certificate_scope.execution_domain,
        account_id=certificate_scope.account_id,
        account_abstraction=certificate_scope.account_abstraction,
        position_mode=certificate_scope.position_mode,
        margin_mode=certificate_scope.margin_mode,
        collateral_scope=certificate_scope.collateral_scope,
        collateral_pool_id=certificate_scope.collateral_pool_id,
    )
    with database.session_factory.begin() as session:
        sender_state = session.get(ExecutionSenderScopeState, sender_scope_id(scope))
        latest_run = session.execute(
            select(ExecutionReconciliationRun)
            .where(ExecutionReconciliationRun.scope_id == sender_scope_id(scope))
            .order_by(
                ExecutionReconciliationRun.started_at.desc(),
                ExecutionReconciliationRun.run_id.desc(),
            )
            .limit(1)
        ).scalar_one_or_none()
        latest_run_state = (
            session.get(ExecutionReconciliationRunState, latest_run.run_id)
            if latest_run is not None
            else None
        )

    if sender_state is None:
        lease_id = uuid4()
        acquired = execute_acquire(
            database,
            acquire_envelope(
                scope,
                now=now,
                lease_id=lease_id,
                ttl_seconds=300,
                max_lifetime_seconds=600,
            ),
            now=now,
        )
        assert acquired.status is CommandStatus.COMPLETED
        fencing_token = int(acquired.data["fencing_token"])
        startup_at = now + timedelta(milliseconds=1)
        startup_run_id = complete_successful_reconciliation(
            database,
            scope,
            lease_id,
            fencing_token,
            now=startup_at,
        )
        claimed_at = startup_at + timedelta(milliseconds=1)
    else:
        assert sender_state.status == "LEASED"
        assert sender_state.active_lease_id is not None
        assert latest_run is not None
        assert latest_run_state is not None and latest_run_state.status == "SUCCEEDED"
        lease_id = sender_state.active_lease_id
        fencing_token = sender_state.current_fencing_token
        startup_run_id = latest_run.run_id
        assert latest_run_state.completed_at is not None
        claimed_at = max(now, latest_run_state.completed_at + timedelta(milliseconds=1))
    claimed = execute_claim(
        database,
        claim_envelope(
            scope,
            order_intent_id,
            lease_id,
            fencing_token,
            startup_run_id,
            now=claimed_at,
        ),
        now=claimed_at,
    )
    assert claimed.status is CommandStatus.COMPLETED
    with database.session_factory.begin() as session:
        claim = session.execute(
            select(ShadowDispatchClaim).where(
                ShadowDispatchClaim.order_intent_id == order_intent_id
            )
        ).scalar_one()
    return claim, scope


def prepare_active_fact_run(
    database: Database,
    order_intent_id: UUID,
    source_type: ReconciliationSourceType,
    *,
    now: datetime | None = None,
    draft: ExecutionFactDraft | None = None,
    canonical_client_order_id: str | None = None,
    canonical_venue_order_id: str | None = None,
    position_snapshot_overrides: dict[str, Any] | None = None,
    protection_snapshot_overrides: dict[str, Any] | None = None,
    protection_position_snapshot_id: UUID | None = None,
) -> tuple[
    ShadowDispatchClaim,
    ExecutionReconciliationRun,
    ExecutionReconciliationInput,
    datetime,
    CanonicalVenueFactContext | None,
]:
    base_time = now or datetime.now(UTC)
    claim, scope = ensure_shadow_claim(database, order_intent_id, now=base_time)
    with database.session_factory.begin() as session:
        sender_state = session.get(ExecutionSenderScopeState, claim.scope_id)
        assert sender_state is not None
        assert sender_state.status == "LEASED"
        assert sender_state.active_lease_id is not None
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
        assert latest_state is not None and latest_state.completed_at is not None
    run_at = max(
        base_time,
        claim.claimed_at + timedelta(milliseconds=1),
        latest.started_at + timedelta(milliseconds=1),
        latest_state.completed_at + timedelta(milliseconds=1),
    )
    run_id = uuid4()
    started = execute_reconciliation(
        database,
        start_envelope(
            run_id,
            scope,
            sender_state.active_lease_id,
            sender_state.current_fencing_token,
            now=run_at,
            trigger_type=ReconciliationTriggerType.PRIVATE_STREAM_RECONNECT,
            supersedes_run_id=latest.run_id,
        ),
        now=run_at,
    )
    assert started.status is CommandStatus.COMPLETED
    canonical_source = source_type in {
        ReconciliationSourceType.VENUE_ORDERS,
        ReconciliationSourceType.VENUE_FILLS,
        ReconciliationSourceType.VENUE_POSITIONS,
        ReconciliationSourceType.VENUE_PROTECTION,
    }
    version = collect_complete_inputs(
        database,
        run_id,
        now=run_at,
        item_counts={source_type: 1} if canonical_source and draft is not None else None,
    )
    with database.session_factory.begin() as session:
        run = session.get(ExecutionReconciliationRun, run_id)
        reconciliation_input = session.execute(
            select(ExecutionReconciliationInput).where(
                ExecutionReconciliationInput.run_id == run_id,
                ExecutionReconciliationInput.source_type == source_type.value,
            )
        ).scalar_one()
        claim = session.get(ShadowDispatchClaim, claim.claim_id)
        intent = session.get(OrderIntent, order_intent_id)
        intent_state = session.get(OrderIntentState, order_intent_id)
        assert run is not None and claim is not None and intent is not None
        assert intent_state is not None
    canonical_context = None
    phase_at = run_at
    if canonical_source and draft is not None:
        phase_at = run_at
        canonical_context = _normalize_execution_venue_fact(
            database,
            run,
            reconciliation_input,
            claim,
            scope,
            intent,
            intent_state,
            draft,
            event_time=run_at,
            normalized_at=phase_at,
            canonical_client_order_id=canonical_client_order_id,
            canonical_venue_order_id=canonical_venue_order_id,
            position_snapshot_overrides=position_snapshot_overrides,
            protection_snapshot_overrides=protection_snapshot_overrides,
            protection_position_snapshot_id=protection_position_snapshot_id,
        )
    compared = execute_reconciliation(
        database,
        phase_envelope(
            run_id,
            ReconciliationPhase.COMPARING,
            now=phase_at,
            expected_version=version,
        ),
        now=phase_at,
    )
    assert compared.status is CommandStatus.COMPLETED
    event_time = canonical_context.fact.event_time if canonical_context is not None else run_at
    return claim, run, reconciliation_input, event_time, canonical_context


def _normalize_execution_venue_fact(
    database: Database,
    run: ExecutionReconciliationRun,
    reconciliation_input: ExecutionReconciliationInput,
    claim: ShadowDispatchClaim,
    scope: SenderScopeBinding,
    intent: OrderIntent,
    intent_state: OrderIntentState,
    draft: ExecutionFactDraft,
    *,
    event_time: datetime,
    normalized_at: datetime,
    canonical_client_order_id: str | None,
    canonical_venue_order_id: str | None,
    position_snapshot_overrides: dict[str, Any] | None,
    protection_snapshot_overrides: dict[str, Any] | None,
    protection_position_snapshot_id: UUID | None,
) -> CanonicalVenueFactContext:
    venue_order_id = canonical_venue_order_id or f"shadow-{claim.client_order_id}"
    observed_client_order_id = canonical_client_order_id or claim.client_order_id
    position_side = (
        VenuePositionSide.BOTH
        if scope.position_mode == "ONE_WAY"
        else VenuePositionSide(intent.position_side)
    )
    if reconciliation_input.source_type == ReconciliationSourceType.VENUE_FILLS.value:
        fill_quantity = draft.filled - intent_state.cumulative_filled_quantity
        request = fill_request(
            reconciliation_input,
            now=normalized_at,
            venue_trade_id=draft.external_fact_id,
            venue_order_id=venue_order_id,
            observed_client_order_id=observed_client_order_id,
            instrument_id=intent.instrument_id,
            side=VenueSide(intent.side),
            position_side=position_side,
            reduce_only=intent.reduce_only,
            quantity=fill_quantity,
            fee_amount=Decimal("0"),
            fee_effect=FeeEffect.ZERO,
            event_time=event_time,
            venue_observed_at=event_time,
            received_at=normalized_at,
        )
        command_type = VenueFactNormalizationService.fill_command_type
    elif reconciliation_input.source_type == ReconciliationSourceType.VENUE_POSITIONS.value:
        position_values: dict[str, Any] = {
            "venue_update_id": draft.external_fact_id,
            "instrument_id": intent.instrument_id,
            "position_mode": VenuePositionMode(scope.position_mode),
            "position_side": position_side,
            "margin_mode": scope.margin_mode,
            "collateral_pool_id": scope.collateral_pool_id,
            "direction": VenuePositionDirection(intent.position_side),
            "quantity": intent.current_position_quantity + draft.filled,
            "event_time": event_time,
            "venue_observed_at": event_time,
            "received_at": normalized_at,
        }
        position_values.update(position_snapshot_overrides or {})
        request = position_snapshot_request(
            reconciliation_input,
            now=normalized_at,
            **position_values,
        )
        command_type = VenueFactNormalizationService.position_command_type
    elif reconciliation_input.source_type == ReconciliationSourceType.VENUE_PROTECTION.value:
        with database.session_factory.begin() as session:
            if protection_position_snapshot_id is None:
                position_fact = session.execute(
                    select(ExecutionFact)
                    .where(
                        ExecutionFact.order_intent_id == intent.order_intent_id,
                        ExecutionFact.fact_kind == "VENUE_POSITION",
                        ExecutionFact.venue_position_snapshot_id.is_not(None),
                    )
                    .order_by(ExecutionFact.fact_sequence.desc())
                    .limit(1)
                ).scalar_one_or_none()
                assert position_fact is not None
                effective_position_snapshot_id = position_fact.venue_position_snapshot_id
            else:
                effective_position_snapshot_id = protection_position_snapshot_id
            position_snapshot = session.get(VenuePositionSnapshot, effective_position_snapshot_id)
            assert position_snapshot is not None
        protection_values: dict[str, Any] = {
            "venue_update_id": draft.external_fact_id,
            "event_time": event_time,
            "venue_observed_at": event_time,
            "received_at": normalized_at,
        }
        protection_values.update(protection_snapshot_overrides or {})
        request = protection_snapshot_request(
            reconciliation_input,
            position_snapshot,
            now=normalized_at,
            **protection_values,
        )
        command_type = VenueFactNormalizationService.protection_command_type
    else:
        order_status = {
            "VENUE_ACKNOWLEDGED": VenueOrderStatus.OPEN,
            "CANCEL_PENDING": VenueOrderStatus.CANCEL_PENDING,
            "CANCELLED_ZERO_FILL": VenueOrderStatus.CANCELLED,
            "CANCELLED_PARTIAL": VenueOrderStatus.CANCELLED,
            "REJECTED_ZERO_FILL": VenueOrderStatus.REJECTED,
            "RESULT_UNKNOWN": VenueOrderStatus.UNKNOWN,
        }[draft.status]
        request = order_observation_request(
            reconciliation_input,
            now=normalized_at,
            venue_order_id=venue_order_id,
            venue_update_id=draft.external_fact_id,
            observed_client_order_id=observed_client_order_id,
            instrument_id=intent.instrument_id,
            side=VenueSide(intent.side),
            position_side=position_side,
            reduce_only=intent.reduce_only,
            order_type=intent.order_type,
            time_in_force=intent.time_in_force,
            status=order_status,
            original_quantity=intent_state.intent_quantity,
            cumulative_filled_quantity=draft.filled,
            known_remaining_quantity=draft.remaining,
            zero_fill_confirmed=draft.zero,
            terminal=draft.terminal,
            event_time=event_time,
            venue_observed_at=event_time,
            received_at=normalized_at,
        )
        command_type = VenueFactNormalizationService.order_command_type
    result = execute_venue_fact(
        database,
        venue_fact_envelope(
            run.run_id,
            command_type,
            request.model_dump(mode="json"),
            now=normalized_at,
        ),
        now=normalized_at,
    )
    assert result.status is CommandStatus.COMPLETED
    fact_id = UUID(str(result.data["venue_fact_id"]))
    link_id = UUID(str(result.data["venue_fact_input_link_id"]))
    with database.session_factory.begin() as session:
        if command_type == VenueFactNormalizationService.fill_command_type:
            fact = session.get(VenueFill, fact_id)
        elif command_type == VenueFactNormalizationService.position_command_type:
            fact = session.get(VenuePositionSnapshot, fact_id)
        elif command_type == VenueFactNormalizationService.protection_command_type:
            fact = session.get(VenueProtectionSnapshot, fact_id)
        else:
            fact = session.get(VenueOrderObservation, fact_id)
        input_link = session.get(VenueFactInputLink, link_id)
        assert fact is not None and input_link is not None
    return CanonicalVenueFactContext(fact=fact, input_link=input_link)


def bind_fact_request(
    draft: ExecutionFactDraft,
    claim: ShadowDispatchClaim,
    run: ExecutionReconciliationRun,
    reconciliation_input: ExecutionReconciliationInput,
    *,
    event_time: datetime,
    received_at: datetime,
    canonical_context: CanonicalVenueFactContext | None = None,
) -> RecordExecutionFactRequest:
    fact_kind, source_type = _fact_kind_and_source(draft.status)
    venue_order_observation_id = None
    venue_fill_id = None
    venue_position_snapshot_id = None
    venue_protection_snapshot_id = None
    venue_fact_input_link_id = None
    venue_fact_hash = None
    if canonical_context is None:
        payload = {
            "sequence": draft.sequence,
            "status": draft.status,
            "filled": str(draft.filled),
            "remaining": str(draft.remaining),
        }
        external_fact_id = draft.external_fact_id
        source_ref = f"test-only:{source_type.value.lower()}-fact"
        evidence_ref = f"test-only:evidence:{draft.sequence}:{draft.status}"
    else:
        fact = canonical_context.fact
        input_link = canonical_context.input_link
        venue_fact_input_link_id = input_link.venue_fact_input_link_id
        source_ref = input_link.raw_payload_ref
        evidence_ref = input_link.evidence_ref
        event_time = fact.event_time
        received_at = input_link.received_at
        if isinstance(fact, VenueOrderObservation):
            venue_order_observation_id = fact.venue_order_observation_id
            venue_fact_hash = fact.observation_hash
            canonical_venue_order_id = fact.venue_order_id
            venue_fact_type = "VENUE_ORDER_OBSERVATION"
            external_fact_id = str(fact.venue_order_observation_id)
        elif isinstance(fact, VenueFill):
            venue_fill_id = fact.venue_fill_id
            venue_fact_hash = fact.fill_hash
            canonical_venue_order_id = fact.venue_order_id
            venue_fact_type = "VENUE_FILL"
            external_fact_id = str(fact.venue_fill_id)
        elif isinstance(fact, VenuePositionSnapshot):
            venue_position_snapshot_id = fact.venue_position_snapshot_id
            venue_fact_hash = fact.snapshot_hash
            venue_fact_type = "VENUE_POSITION_SNAPSHOT"
            external_fact_id = str(fact.venue_position_snapshot_id)
        else:
            venue_protection_snapshot_id = fact.venue_protection_snapshot_id
            venue_fact_hash = fact.snapshot_hash
            venue_fact_type = "VENUE_PROTECTION_SNAPSHOT"
            external_fact_id = str(fact.venue_protection_snapshot_id)
        payload = {
            "venue_fact_type": venue_fact_type,
            "venue_fact_id": external_fact_id,
            "venue_fact_hash": venue_fact_hash,
            "venue_fact_input_link_id": str(venue_fact_input_link_id),
        }
        if isinstance(fact, (VenueOrderObservation, VenueFill)):
            payload["canonical_venue_order_id"] = canonical_venue_order_id
        elif isinstance(fact, VenueProtectionSnapshot):
            payload["venue_position_snapshot_id"] = str(fact.venue_position_snapshot_id)
    values: dict[str, Any] = {
        "fact_sequence": draft.sequence,
        "fact_kind": fact_kind,
        "target_status": draft.status,
        "venue": "BINANCE",
        "execution_domain": "BINANCE_USDM",
        "account_id": "account-1",
        "external_fact_id": external_fact_id,
        "cumulative_filled_quantity": draft.filled,
        "known_remaining_quantity": draft.remaining,
        "zero_fill_confirmed": draft.zero,
        "venue_order_terminal": draft.terminal,
        "position_reconciled": draft.reconciled,
        "protection_confirmed": draft.protected,
        "shadow_dispatch_claim_id": claim.claim_id,
        "reconciliation_run_id": run.run_id,
        "reconciliation_input_id": reconciliation_input.input_id,
        "reconciliation_source_type": source_type,
        "reconciliation_run_hash": run.run_hash,
        "reconciliation_input_hash": reconciliation_input.input_hash,
        "dispatch_claim_hash": claim.claim_hash,
        "venue_order_observation_id": venue_order_observation_id,
        "venue_fill_id": venue_fill_id,
        "venue_position_snapshot_id": venue_position_snapshot_id,
        "venue_protection_snapshot_id": venue_protection_snapshot_id,
        "venue_fact_input_link_id": venue_fact_input_link_id,
        "venue_fact_hash": venue_fact_hash,
        "source_ref": source_ref,
        "source_version": reconciliation_input.source_version,
        "payload": payload,
        "payload_hash": hash_json(payload),
        "evidence_ref": evidence_ref,
        "event_time": event_time,
        "received_at": received_at,
    }
    provisional = RecordExecutionFactRequest.model_construct(**values, evidence_hash="0" * 64)
    values["evidence_hash"] = hash_json(
        provisional.model_dump(mode="json", exclude={"evidence_hash"})
    )
    return RecordExecutionFactRequest.model_validate(values)


def fact_envelope(order_intent_id: UUID, request: RecordExecutionFactRequest) -> CommandEnvelope:
    now = request.received_at
    return CommandEnvelope(
        idempotency_key=f"execution-fact-{uuid4()}",
        command_type=ExecutionReconciliationService.command_type,
        object_type="OrderIntent",
        object_id=str(order_intent_id),
        expected_version=request.fact_sequence,
        service_principal=EXECUTION_RECONCILIATION_SERVICE_PRINCIPAL,
        channel=CommandChannel.INTERNAL,
        scope={"organization_id": "org-1"},
        correlation_id=uuid4(),
        issued_at=now,
        expires_at=now + timedelta(minutes=2),
        auth_context_ref="internal:execution-reconciliation-service",
        payload_schema_version=1,
        reason="record reconciled venue fact",
        payload=request.model_dump(mode="json"),
    )


def execute_fact(database: Database, order_intent_id: UUID, draft: ExecutionFactDraft):
    _, source_type = _fact_kind_and_source(draft.status)
    claim, run, reconciliation_input, event_time, canonical_context = prepare_active_fact_run(
        database, order_intent_id, source_type, draft=draft
    )
    received_at = (
        canonical_context.input_link.received_at
        if canonical_context is not None
        else event_time + timedelta(milliseconds=1)
    )
    request = bind_fact_request(
        draft,
        claim,
        run,
        reconciliation_input,
        event_time=event_time,
        received_at=received_at,
        canonical_context=canonical_context,
    )
    result = IdempotentCommandExecutor(database.session_factory).execute(
        fact_envelope(order_intent_id, request),
        ExecutionReconciliationService(clock=lambda: received_at).record,
    )
    with database.session_factory.begin() as session:
        state = session.get(ExecutionReconciliationRunState, run.run_id)
        assert state is not None
        state_version = state.version
    finished_at = received_at + timedelta(milliseconds=1)
    finished = execute_reconciliation(
        database,
        finish_envelope(
            run.run_id,
            ReconciliationStatus.SUCCEEDED,
            now=finished_at,
            expected_version=state_version,
        ),
        now=finished_at,
    )
    assert finished.status is CommandStatus.COMPLETED
    return result


def test_initial_intent_atomically_persists_decision_reservation_ledger_and_histories(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    proposal, campaign, initial = prepare_authorization(database)
    seed_execution_policy(database, now)

    result = execute_create(database, create_intent_envelope(proposal, campaign, initial, now=now))

    assert result.status is CommandStatus.COMPLETED
    assert result.data["execution_mode"] == "SHADOW"
    assert result.data["dispatch_eligible"] is False
    assert result.data["reservation_created"] is True
    with database.session_factory.begin() as session:
        decision = session.execute(select(ExecutionRiskDecision)).scalar_one()
        intent = session.execute(select(OrderIntent)).scalar_one()
        reservation = session.execute(select(RiskReservation)).scalar_one()
        exposure = session.execute(select(RiskExposureState)).scalar_one()
        assert decision.result == "ALLOW"
        assert (
            decision.capital_scope_manifest_id == TEST_EXECUTION_CAPITAL_SCOPE_MANIFEST.manifest_id
        )
        assert decision.capital_scope_manifest_version == 1
        assert (
            decision.capital_scope_manifest_hash
            == TEST_EXECUTION_CAPITAL_SCOPE_MANIFEST.manifest_hash
        )
        assert decision.capital_projection_version == "portfolio-mtm-v2"
        assert (
            hash_json(decision.input_snapshot["capital_projection"])
            == decision.capital_projection_hash
        )
        assert (
            hash_json(decision.input_snapshot["durable_exposure_snapshot"])
            == decision.durable_exposure_snapshot_hash
        )
        assert decision.input_snapshot["durable_exposure_snapshot"]["components"] == []
        assert intent.dispatch_eligible is False
        assert reservation.order_intent_id == intent.order_intent_id
        assert exposure.status == "RESERVED"
        assert exposure.total_heat == Decimal("110")
        assert count_rows(session, RiskLedgerEntry) == 1
        assert count_rows(session, OrderIntentStateHistory) == 1
        assert count_rows(session, RiskExposureStateHistory) == 1
        audit_count = count_rows(session, AuditEvent)
        assert audit_count >= 3
        assert count_rows(session, OutboxMessage) == audit_count


def test_final_precheck_refreshes_current_mtm_without_rewriting_frozen_one_r(
    database: Database,
) -> None:
    proposal, campaign, initial = prepare_authorization(database)
    record_execution_capital_equity(
        database,
        exchange_margin_equity=Decimal("90000"),
        total_unrealized_pnl=Decimal("-10000"),
        available_margin=Decimal("8000"),
    )
    now = datetime.now(UTC)
    seed_execution_policy(database, now)
    current_capital = make_capital(
        exchange_settled_equity_ex_upnl=Decimal("100000"),
        current_unrealized_pnl=Decimal("-10000"),
        total_capital_snapshot_0=Decimal("100000"),
        available_margin=Decimal("8000"),
    )
    risk_request = execution_risk_request(proposal, now=now).model_copy(
        update={"capital": current_capital}
    )

    result = execute_create(
        database,
        create_intent_envelope(
            proposal,
            campaign,
            initial,
            now=now,
            risk_request=risk_request,
        ),
    )

    assert result.status is CommandStatus.COMPLETED
    with database.session_factory.begin() as session:
        decision = session.execute(select(ExecutionRiskDecision)).scalar_one()
        evaluated_capital = decision.input_snapshot["evaluation"]["request"]["capital"]
        assert decision.current_portfolio_mtm_equity == Decimal("90000")
        assert decision.current_unrealized_pnl == Decimal("-10000")
        assert Decimal(str(evaluated_capital["total_capital_snapshot_0"])) == Decimal("100000")
        assert Decimal(str(decision.decision["one_r_0"])) == Decimal("500")
        assert Decimal(str(decision.decision["dynamic_trade_loss_cap"])) == Decimal("450")


def test_final_precheck_rejects_caller_capital_older_than_canonical_projection(
    database: Database,
) -> None:
    proposal, campaign, initial = prepare_authorization(database)
    record_execution_capital_equity(
        database,
        exchange_margin_equity=Decimal("90000"),
        total_unrealized_pnl=Decimal("-10000"),
        available_margin=Decimal("8000"),
    )
    now = datetime.now(UTC)
    seed_execution_policy(database, now)

    result = execute_create(
        database,
        create_intent_envelope(proposal, campaign, initial, now=now),
    )

    assert result.status is CommandStatus.REJECTED
    assert result.error_code == "CAPITAL_INPUT_MISMATCH"
    with database.session_factory.begin() as session:
        assert count_rows(session, ExecutionRiskDecision) == 0
        assert count_rows(session, OrderIntent) == 0
        assert count_rows(session, RiskReservation) == 0


def test_final_precheck_rejects_both_high_and_low_durable_exposure_reports(
    database: Database,
) -> None:
    proposal, campaign, initial = prepare_authorization(database)
    now = datetime.now(UTC)
    seed_execution_policy(database, now)
    over_reported = execution_risk_request(
        proposal,
        now=now,
        current_open_heat=Decimal("1"),
        funding_used=Decimal("1"),
        scope_current_planned=Decimal("1"),
        scope_current_stress=Decimal("1"),
    )

    high = execute_create(
        database,
        create_intent_envelope(
            proposal,
            campaign,
            initial,
            now=now,
            risk_request=over_reported,
        ),
    )

    assert high.status is CommandStatus.REJECTED
    assert high.error_code == "DURABLE_EXPOSURE_INPUT_MISMATCH"

    valid = execute_create(
        database,
        create_intent_envelope(
            proposal,
            campaign,
            initial,
            now=datetime.now(UTC),
            candidate_ref="durable-exposure-source",
        ),
    )
    assert valid.status is CommandStatus.COMPLETED
    under_reported_request = CreateExecutionIntentRequest.model_validate(
        create_intent_envelope(
            proposal,
            campaign,
            initial,
            now=datetime.now(UTC),
            candidate_ref="durable-exposure-under-report",
        ).payload
    )
    with database.session_factory.begin() as session:
        with pytest.raises(CommandRejected) as exc_info:
            DurableExposureResolver.resolve(session, under_reported_request, campaign)
    assert exc_info.value.error_code == "DURABLE_EXPOSURE_INPUT_MISMATCH"


def test_durable_exposure_snapshot_subtracts_internal_margin_reservations(
    database: Database,
) -> None:
    proposal, campaign, initial = prepare_authorization(database)
    now = datetime.now(UTC)
    seed_execution_policy(database, now)
    created = execute_create(
        database,
        create_intent_envelope(
            proposal,
            campaign,
            initial,
            now=now,
            candidate_ref="durable-margin-source",
        ),
    )
    assert created.status is CommandStatus.COMPLETED
    exact_risk = execution_risk_request(
        proposal,
        now=datetime.now(UTC),
        current_reserved_heat=Decimal("110"),
        funding_reserved=Decimal("500"),
        scope_current_planned=Decimal("110"),
        scope_current_stress=Decimal("150"),
    )
    exact_request = CreateExecutionIntentRequest.model_validate(
        create_intent_envelope(
            proposal,
            campaign,
            initial,
            now=datetime.now(UTC),
            candidate_ref="durable-margin-check",
            risk_request=exact_risk,
        ).payload
    )

    with database.session_factory.begin() as session:
        verified = DurableExposureResolver.resolve(session, exact_request, campaign)

    assert verified.risk_request.capital.available_margin == Decimal("9500")
    assert verified.snapshot.global_margin_reserved == Decimal("500")
    assert verified.snapshot.available_margin_after_internal_reservations == Decimal("9500")
    assert len(verified.snapshot.components) == 1
    assert hash_json(verified.snapshot.model_dump(mode="json")) == verified.snapshot_hash


def test_proposal_precheck_derives_other_campaign_funding_margin_and_scope(
    database: Database,
) -> None:
    proposal, campaign, initial = prepare_authorization(database)
    now = datetime.now(UTC)
    seed_execution_policy(database, now)
    created = execute_create(
        database,
        create_intent_envelope(
            proposal,
            campaign,
            initial,
            now=now,
            candidate_ref="proposal-durable-exposure-source",
        ),
    )
    assert created.status is CommandStatus.COMPLETED

    under_reported = execution_risk_request(proposal, now=datetime.now(UTC))
    low = IdempotentCommandExecutor(database.session_factory).execute(
        proposal_precheck_envelope(under_reported),
        RiskPrecheckService().evaluate,
    )
    assert low.status is CommandStatus.REJECTED
    assert low.error_code == "DURABLE_EXPOSURE_INPUT_MISMATCH"

    exact = execution_risk_request(
        proposal,
        now=datetime.now(UTC),
        funding_reserved=Decimal("500"),
        scope_current_planned=Decimal("110"),
        scope_current_stress=Decimal("150"),
    )
    allowed = IdempotentCommandExecutor(database.session_factory).execute(
        proposal_precheck_envelope(exact),
        RiskPrecheckService().evaluate,
    )

    assert allowed.status is CommandStatus.COMPLETED
    assert allowed.data["result"] == "ALLOW"
    with database.session_factory.begin() as session:
        decision = session.execute(select(RiskDecisionSnapshot)).scalar_one()
        durable = decision.input_snapshot["durable_exposure_snapshot"]
        assert durable["campaign_id"] is None
        assert len(durable["components"]) == 1
        assert durable["global_funding_reserved"] == "500.000000000000000000"
        assert durable["global_margin_reserved"] == "500.000000000000000000"
        assert durable["available_margin_after_internal_reservations"] == (
            "9500.000000000000000000"
        )
        assert decision.input_snapshot["request"]["current_trade_loss"] == {
            "open_heat": "0",
            "reserved_heat": "0",
            "unknown_heat": "0",
            "protected_profit_giveback": "0",
            "cost_stress_add_on": "0",
        }


def test_durable_exposure_resolver_blocks_internal_margin_overcommit(database: Database) -> None:
    proposal, campaign, initial = prepare_authorization(database)
    now = datetime.now(UTC)
    seed_execution_policy(database, now)
    created = execute_create(
        database,
        create_intent_envelope(
            proposal,
            campaign,
            initial,
            now=now,
            candidate_ref="durable-margin-capacity-source",
        ),
    )
    assert created.status is CommandStatus.COMPLETED
    risk_request = execution_risk_request(
        proposal,
        now=datetime.now(UTC),
        current_reserved_heat=Decimal("110"),
        funding_reserved=Decimal("500"),
        scope_current_planned=Decimal("110"),
        scope_current_stress=Decimal("150"),
    )
    risk_request = risk_request.model_copy(
        update={
            "requested": risk_request.requested.model_copy(
                update={"requested_margin": Decimal("9600")}
            )
        }
    )
    request = CreateExecutionIntentRequest.model_validate(
        create_intent_envelope(
            proposal,
            campaign,
            initial,
            now=datetime.now(UTC),
            candidate_ref="durable-margin-capacity-check",
            risk_request=risk_request,
        ).payload
    )

    with database.session_factory.begin() as session:
        with pytest.raises(CommandRejected) as exc_info:
            DurableExposureResolver.resolve(session, request, campaign)

    assert exc_info.value.error_code == "DURABLE_MARGIN_CAPACITY_EXCEEDED"


def test_final_precheck_cannot_replace_frozen_capital_scope(database: Database) -> None:
    proposal, campaign, initial = prepare_authorization(database)
    now = datetime.now(UTC)
    seed_execution_policy(database, now)
    baseline = execution_risk_request(proposal, now=now)
    changed = baseline.model_copy(
        update={
            "capital_projection_binding": baseline.capital_projection_binding.model_copy(
                update={"manifest_hash": "f" * 64}
            )
        }
    )

    result = execute_create(
        database,
        create_intent_envelope(
            proposal,
            campaign,
            initial,
            now=now,
            risk_request=changed,
        ),
    )

    assert result.status is CommandStatus.REJECTED
    assert result.error_code == "FROZEN_CAPITAL_SCOPE_BINDING_MISMATCH"
    with database.session_factory.begin() as session:
        assert count_rows(session, ExecutionRiskDecision) == 0
        assert count_rows(session, OrderIntent) == 0


def test_stale_canonical_capital_fails_before_final_risk_math(database: Database) -> None:
    proposal, campaign, initial = prepare_authorization(database)
    now = datetime.now(UTC)
    seed_execution_policy(database, now)
    service = ExecutionIntentService(clock=lambda: now + timedelta(seconds=10))

    result = execute_create(
        database,
        create_intent_envelope(proposal, campaign, initial, now=now),
        service,
    )

    assert result.status is CommandStatus.REJECTED
    assert result.error_code == "CAPITAL_ACCOUNT_SCOPE_INCOMPLETE"
    with database.session_factory.begin() as session:
        assert count_rows(session, ExecutionRiskDecision) == 0
        assert count_rows(session, OrderIntent) == 0


def test_stale_final_precheck_is_durable_deny_without_intent_or_reservation(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    proposal, campaign, initial = prepare_authorization(database)
    seed_execution_policy(database, now)
    risk_request = execution_risk_request(proposal, now=now, fact_age=timedelta(seconds=20))

    result = execute_create(
        database,
        create_intent_envelope(proposal, campaign, initial, now=now, risk_request=risk_request),
    )

    assert result.status is CommandStatus.COMPLETED
    assert result.data["result"] == "DENY"
    assert result.data["primary_reason_code"] == "FACTS_STALE"
    with database.session_factory.begin() as session:
        decision = session.execute(select(ExecutionRiskDecision)).scalar_one()
        assert decision.result == "DENY"
        assert decision.capital_projection_version == "portfolio-mtm-v2"
        assert (
            hash_json(decision.input_snapshot["capital_projection"])
            == decision.capital_projection_hash
        )
        assert (
            hash_json(decision.input_snapshot["durable_exposure_snapshot"])
            == decision.durable_exposure_snapshot_hash
        )
        assert count_rows(session, OrderIntent) == 0
        assert count_rows(session, RiskReservation) == 0


def test_partial_fill_consumes_initial_then_unknown_locks_remaining_risk(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    proposal, campaign, initial = prepare_authorization(database)
    seed_execution_policy(database, now)
    created = execute_create(database, create_intent_envelope(proposal, campaign, initial, now=now))
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
    )
    assert partial.status is CommandStatus.COMPLETED
    unknown = execute_fact(
        database,
        order_intent_id,
        fact_request(
            sequence=2,
            status="RESULT_UNKNOWN",
            filled=Decimal("0.2"),
            remaining=Decimal("0"),
        ),
    )
    assert unknown.data["risk_exposure_status"] == "UNKNOWN"
    with database.session_factory.begin() as session:
        exposure = session.execute(select(RiskExposureState)).scalar_one()
        initial_state = session.get(InitialAuthorizationState, initial.initial_authorization_id)
        campaign_state = session.get(CampaignState, campaign.campaign_id)
        assert exposure.open_quantity == Decimal("0.2")
        assert exposure.unknown_quantity == Decimal("0.3")
        assert exposure.reserved_quantity == 0
        assert initial_state is not None and initial_state.status == "CONSUMED"
        assert campaign_state is not None and campaign_state.status == "OPEN"
        assert count_rows(session, RiskLedgerEntry) == 3

    next_request = CreateExecutionIntentRequest.model_validate(
        create_intent_envelope(
            proposal,
            campaign,
            initial,
            now=datetime.now(UTC),
            candidate_ref="unknown-exposure-check",
        ).payload
    )
    with database.session_factory.begin() as session:
        with pytest.raises(CommandRejected) as exc_info:
            DurableExposureResolver.resolve(session, next_request, campaign)
    assert exc_info.value.error_code == "ORDER_RESULT_UNKNOWN"


def test_partial_fill_then_canonical_cancel_releases_only_unfilled_quantity(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    proposal, campaign, initial = prepare_authorization(database)
    seed_execution_policy(database, now)
    created = execute_create(database, create_intent_envelope(proposal, campaign, initial, now=now))
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
    )
    cancelled = execute_fact(
        database,
        order_intent_id,
        fact_request(
            sequence=2,
            status="CANCELLED_PARTIAL",
            filled=Decimal("0.2"),
            remaining=Decimal("0"),
            terminal=True,
        ),
    )

    assert partial.status is CommandStatus.COMPLETED
    assert cancelled.status is CommandStatus.COMPLETED
    with database.session_factory.begin() as session:
        exposure = session.execute(select(RiskExposureState)).scalar_one()
        facts = list(
            session.scalars(
                select(ExecutionFact)
                .where(ExecutionFact.order_intent_id == order_intent_id)
                .order_by(ExecutionFact.fact_sequence)
            )
        )
        assert exposure.open_quantity == Decimal("0.2")
        assert exposure.released_quantity == Decimal("0.3")
        assert exposure.reserved_quantity == 0
        assert [fact.fact_kind for fact in facts] == ["VENUE_FILL", "VENUE_ORDER"]
        assert all(fact.fact_contract_version == 5 for fact in facts)
        assert facts[0].canonical_venue_order_id == facts[1].canonical_venue_order_id


def test_terminal_zero_fill_releases_risk_and_allows_new_initial_candidate(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    proposal, campaign, initial = prepare_authorization(database)
    seed_execution_policy(database, now)
    first = execute_create(
        database,
        create_intent_envelope(
            proposal, campaign, initial, now=now, candidate_ref="initial-attempt-1"
        ),
    )
    first_id = UUID(str(first.data["order_intent_id"]))
    released = execute_fact(
        database,
        first_id,
        fact_request(
            sequence=1,
            status="CANCELLED_ZERO_FILL",
            filled=Decimal("0"),
            remaining=Decimal("0"),
            zero=True,
            terminal=True,
        ),
    )
    assert released.data["risk_exposure_status"] == "RELEASED"

    second = execute_create(
        database,
        create_intent_envelope(
            proposal,
            campaign,
            initial,
            now=datetime.now(UTC),
            candidate_ref="initial-attempt-2",
        ),
    )
    assert second.status is CommandStatus.COMPLETED
    assert second.data["order_intent_id"] != first.data["order_intent_id"]
    with database.session_factory.begin() as session:
        state = session.get(InitialAuthorizationState, initial.initial_authorization_id)
        assert state is not None and state.status == "ACTIVE"
        assert count_rows(session, OrderIntent) == 2


def test_add_zero_fill_releases_unit_then_positive_fill_consumes_it(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    proposal, campaign, initial = prepare_authorization(database, auto_add=True)
    seed_execution_policy(database, now)
    initial_result = execute_create(
        database, create_intent_envelope(proposal, campaign, initial, now=now)
    )
    initial_intent_id = UUID(str(initial_result.data["order_intent_id"]))
    facts = (
        fact_request(
            sequence=1,
            status="FILLED",
            filled=Decimal("0.5"),
            remaining=Decimal("0"),
            terminal=True,
        ),
        fact_request(
            sequence=2,
            status="POSITION_RECONCILED",
            filled=Decimal("0.5"),
            remaining=Decimal("0"),
            terminal=True,
            reconciled=True,
        ),
        fact_request(
            sequence=3,
            status="PROTECTION_CONFIRMED",
            filled=Decimal("0.5"),
            remaining=Decimal("0"),
            terminal=True,
            reconciled=True,
            protected=True,
        ),
    )
    for fact in facts:
        assert execute_fact(database, initial_intent_id, fact).status is CommandStatus.COMPLETED
    with database.session_factory.begin() as session:
        package = session.execute(select(AddAuthorizationPackage)).scalar_one()
        unit = session.execute(select(AddUnit)).scalar_one()
        package_state = session.get(AddAuthorizationPackageState, package.add_package_id)
        assert package_state is not None and package_state.status == "ACTIVE"

    first_add = execute_create(
        database,
        create_add_envelope(
            proposal,
            campaign,
            package,
            unit,
            now=datetime.now(UTC),
            candidate_ref="add-attempt-zero",
        ),
    )
    assert first_add.status is CommandStatus.COMPLETED
    first_add_id = UUID(str(first_add.data["order_intent_id"]))
    zero_result = execute_fact(
        database,
        first_add_id,
        fact_request(
            sequence=1,
            status="REJECTED_ZERO_FILL",
            filled=Decimal("0"),
            remaining=Decimal("0"),
            zero=True,
            terminal=True,
        ),
    )
    assert zero_result.data["risk_exposure_status"] == "RELEASED"
    with database.session_factory.begin() as session:
        unit_state = session.get(AddUnitState, unit.add_unit_id)
        assert unit_state is not None and unit_state.status == "AVAILABLE"

    second_add = execute_create(
        database,
        create_add_envelope(
            proposal,
            campaign,
            package,
            unit,
            now=datetime.now(UTC),
            candidate_ref="add-attempt-positive",
        ),
    )
    assert second_add.status is CommandStatus.COMPLETED
    second_add_id = UUID(str(second_add.data["order_intent_id"]))
    partial = execute_fact(
        database,
        second_add_id,
        fact_request(
            sequence=1,
            status="PARTIALLY_FILLED",
            filled=Decimal("0.05"),
            remaining=Decimal("0.05"),
        ),
    )
    assert partial.status is CommandStatus.COMPLETED
    with database.session_factory.begin() as session:
        unit_state = session.get(AddUnitState, unit.add_unit_id)
        package_state = session.get(AddAuthorizationPackageState, package.add_package_id)
        assert unit_state is not None and unit_state.status == "CONSUMED"
        assert package_state is not None and package_state.status == "EXHAUSTED"


def test_concurrent_initial_candidates_create_only_one_atomic_reservation(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    proposal, campaign, initial = prepare_authorization(database)
    seed_execution_policy(database, now)
    envelopes = (
        create_intent_envelope(proposal, campaign, initial, now=now, candidate_ref="concurrent-a"),
        create_intent_envelope(proposal, campaign, initial, now=now, candidate_ref="concurrent-b"),
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda item: execute_create(database, item), envelopes))

    assert sorted(result.status.value for result in results) == ["COMPLETED", "REJECTED"]
    rejected = next(result for result in results if result.status is CommandStatus.REJECTED)
    assert rejected.error_code == "INITIAL_INTENT_ALREADY_ACTIVE"
    with database.session_factory.begin() as session:
        assert count_rows(session, OrderIntent) == 1
        assert count_rows(session, RiskReservation) == 1
        assert count_rows(session, RiskLedgerEntry) == 1


def test_fact_sequence_and_immutable_root_are_database_enforced(database: Database) -> None:
    now = datetime.now(UTC)
    proposal, campaign, initial = prepare_authorization(database)
    seed_execution_policy(database, now)
    created = execute_create(database, create_intent_envelope(proposal, campaign, initial, now=now))
    order_intent_id = UUID(str(created.data["order_intent_id"]))

    out_of_order = execute_fact(
        database,
        order_intent_id,
        fact_request(
            sequence=2,
            status="VENUE_ACKNOWLEDGED",
            filled=Decimal("0"),
            remaining=Decimal("0.5"),
        ),
    )
    assert out_of_order.status is CommandStatus.REJECTED
    assert out_of_order.error_code == "EXECUTION_FACT_OUT_OF_ORDER"
    with pytest.raises(DBAPIError):
        with database.session_factory.begin() as session:
            session.execute(
                update(OrderIntent)
                .where(OrderIntent.order_intent_id == order_intent_id)
                .values(dispatch_eligible=True)
            )
    with pytest.raises(DBAPIError, match="execution_risk_decisions is immutable"):
        with database.session_factory.begin() as session:
            session.execute(
                update(ExecutionRiskDecision).values(durable_exposure_snapshot_hash="f" * 64)
            )
    with database.session_factory.begin() as session:
        assert count_rows(session, ExecutionFact) == 0
        assert session.get(OrderIntent, order_intent_id).dispatch_eligible is False


def test_command_idempotency_replays_without_second_reservation(database: Database) -> None:
    now = datetime.now(UTC)
    proposal, campaign, initial = prepare_authorization(database)
    seed_execution_policy(database, now)
    envelope = create_intent_envelope(
        proposal,
        campaign,
        initial,
        now=now,
        idempotency_key="execution-intent-stable-idempotency-key",
    )

    first = execute_create(database, envelope)
    second = execute_create(database, envelope)

    assert first.status is CommandStatus.COMPLETED
    assert second.status is CommandStatus.ALREADY_PROCESSED
    with database.session_factory.begin() as session:
        assert count_rows(session, OrderIntent) == 1
        assert count_rows(session, RiskReservation) == 1


def test_database_rejects_orphan_allow_decision_at_deferred_commit(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    proposal, campaign, initial = prepare_authorization(database)
    seed_execution_policy(database, now)
    created = execute_create(database, create_intent_envelope(proposal, campaign, initial, now=now))
    assert created.status is CommandStatus.COMPLETED

    with pytest.raises(DBAPIError):
        with database.session_factory.begin() as session:
            session.execute(
                text(
                    """
                    INSERT INTO execution_risk_decisions (
                        execution_risk_decision_id, decision_stage, intent_kind,
                        organization_id, authorization_id, campaign_id,
                        initial_authorization_id, add_package_id, add_unit_id,
                        risk_policy_id, risk_policy_version, system_risk_state,
                        capital_scope_manifest_id, capital_scope_manifest_version,
                        capital_scope_manifest_hash, capital_projection_version,
                        capital_projection_hash, durable_exposure_snapshot_hash,
                        result, primary_reason_code, requested_quantity,
                        max_safe_quantity, final_quantity, approved_reserved_heat,
                        approved_funding, approved_margin,
                        current_portfolio_mtm_equity, current_unrealized_pnl,
                        input_snapshot, input_hash, decision, decision_hash,
                        execution_eligible, reservation_created, order_intent_created,
                        decided_at, valid_until
                    )
                    SELECT
                        :new_id, decision_stage, intent_kind,
                        organization_id, authorization_id, campaign_id,
                        initial_authorization_id, add_package_id, add_unit_id,
                        risk_policy_id, risk_policy_version, system_risk_state,
                        capital_scope_manifest_id, capital_scope_manifest_version,
                        capital_scope_manifest_hash, capital_projection_version,
                        capital_projection_hash, durable_exposure_snapshot_hash,
                        result, primary_reason_code, requested_quantity,
                        max_safe_quantity, final_quantity, approved_reserved_heat,
                        approved_funding, approved_margin,
                        current_portfolio_mtm_equity, current_unrealized_pnl,
                        input_snapshot, input_hash, decision, decision_hash,
                        execution_eligible, reservation_created, order_intent_created,
                        decided_at, valid_until
                    FROM execution_risk_decisions
                    LIMIT 1
                    """
                ),
                {"new_id": uuid4()},
            )
    with database.session_factory.begin() as session:
        assert count_rows(session, ExecutionRiskDecision) == 1
