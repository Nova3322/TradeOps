from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from tests.instrument_catalog_fixtures import (
    instrument_catalog_request_for_risk,
    register_instrument_catalog,
)
from tests.integration.test_capital_scope import _register
from tests.integration.test_projections import (
    _prepare_collecting_run,
    _record_account_equity,
    _record_position,
)
from tests.integration.test_trading_authorization import (
    execute_issue,
    issue_envelope,
    prepare_approved,
)
from tests.protection_capability_fixtures import (
    protection_capability_request_for_risk,
    register_protection_capability,
)
from tests.reconciliation_fixtures import (
    collect_complete_inputs,
    complete_successful_reconciliation,
    execute_reconciliation,
    finish_envelope,
    phase_envelope,
    start_envelope,
)
from tests.risk_fact_set_fixtures import (
    register_risk_fact_set,
    risk_fact_set_request_for_risk,
)
from tests.risk_fixtures import (
    TEST_EXECUTION_CAPITAL_PROJECTION_BINDING,
    TEST_EXECUTION_CAPITAL_SCOPE_MANIFEST,
    TEST_SCOPE_STRESS_SCENARIO,
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
from tests.strategy_evaluation_fixtures import (
    register_strategy_evaluation,
    strategy_evaluation_request_for_add,
)
from tests.venue_fact_fixtures import (
    execute_venue_fact,
    fill_request,
    order_observation_request,
    position_snapshot_request,
    protection_snapshot_request,
    venue_fact_envelope,
)
from trading_control_plane.campaign_economics import baseline_snapshot_from_record
from trading_control_plane.campaign_economics_models import CampaignEconomicBaseline
from trading_control_plane.campaign_fill_economics import fill_entry_snapshot_from_record
from trading_control_plane.campaign_fill_economics_models import CampaignFillEconomicEntry
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
    AddLeverageCalculation,
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
from trading_control_plane.instrument_catalog_models import InstrumentCatalogRecord
from trading_control_plane.models import AuditEvent, OutboxMessage
from trading_control_plane.proposal_models import FrozenProposalVersion
from trading_control_plane.protection_capability_models import (
    InstrumentProtectionCapabilityRecord,
)
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
    CANONICAL_LOSS_MODEL_VERSION,
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
from trading_control_plane.risk_fact_set_models import RiskFactSetRecord
from trading_control_plane.risk_models import RiskDecisionSnapshot, RiskPolicyRecord
from trading_control_plane.sender_fencing import SenderScopeBinding, sender_scope_id
from trading_control_plane.sender_fencing_models import (
    ExecutionSenderScope,
    ExecutionSenderScopeState,
    ShadowDispatchClaim,
)
from trading_control_plane.strategy_evaluations import StrategyRuleId, StrategyRuleStatus
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
    VenuePositionState,
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
                stress_scenario=TEST_SCOPE_STRESS_SCENARIO,
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


def record_execution_position(
    database: Database,
    *,
    position_state: VenuePositionState = VenuePositionState.FLAT,
    event_time: datetime | None = None,
):
    collector_scope = make_sender_scope(
        account_abstraction=f"POSITION_COLLECTOR_{uuid4().hex}",
        margin_mode="ISOLATED",
        collateral_pool_id="pool-usdt-1",
    )
    run_id, _, run_time, inputs = _prepare_collecting_run(
        database,
        position_count=1,
        sender_scope=collector_scope,
    )
    normalized_at = run_time + timedelta(seconds=1)
    direction = {
        VenuePositionState.FLAT: VenuePositionDirection.FLAT,
        VenuePositionState.OPEN: VenuePositionDirection.LONG,
        VenuePositionState.UNKNOWN: VenuePositionDirection.UNKNOWN,
    }[position_state]
    request, recorded = _record_position(
        database,
        run_id,
        inputs[ReconciliationSourceType.VENUE_POSITIONS],
        now=normalized_at,
        venue_update_id=f"execution-position-{uuid4()}",
        instrument_id="BINANCE:BTCUSDT-PERP",
        position_state=position_state,
        direction=direction,
        settlement_currency="USD",
        event_time=event_time or datetime.now(UTC) - timedelta(seconds=1),
    )
    assert recorded.status is CommandStatus.COMPLETED
    return request


def prepare_authorization(
    database: Database,
    *,
    auto_add: bool = False,
    register_catalog: bool = True,
    register_protection: bool = True,
    register_fact_set: bool = True,
    register_position: bool = True,
    position_state: VenuePositionState = VenuePositionState.FLAT,
    position_event_time: datetime | None = None,
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
    if register_position:
        record_execution_position(
            database,
            position_state=position_state,
            event_time=position_event_time,
        )
    fact_now = datetime.now(UTC)
    risk_request = execution_risk_request(proposal, now=fact_now)
    if register_catalog:
        catalog_request = instrument_catalog_request_for_risk(
            risk_request,
            now=fact_now,
        )
        catalog_result = register_instrument_catalog(database, catalog_request, now=fact_now)
        assert catalog_result.status is CommandStatus.COMPLETED
        if register_protection:
            protection_request = protection_capability_request_for_risk(
                risk_request,
                catalog_request,
                now=fact_now,
            )
            protection_result = register_protection_capability(
                database,
                protection_request,
                now=fact_now,
            )
            assert protection_result.status is CommandStatus.COMPLETED
    if register_fact_set:
        fact_set_request = risk_fact_set_request_for_risk(
            risk_request,
            now=fact_now,
        )
        fact_set_result = register_risk_fact_set(
            database,
            fact_set_request,
            now=fact_now,
        )
        assert fact_set_result.status is CommandStatus.COMPLETED
    with database.session_factory.begin() as session:
        campaign = session.execute(select(Campaign)).scalar_one()
        initial = session.execute(select(InitialOrderAuthorization)).scalar_one()
    return proposal, campaign, initial


def execution_risk_request(
    proposal: FrozenProposalVersion,
    *,
    now: datetime,
    quantity: Decimal = Decimal("0.5"),
    requested_funding: Decimal = Decimal("500"),
    current_open_heat: Decimal = Decimal("0"),
    current_reserved_heat: Decimal = Decimal("0"),
    current_unknown_heat: Decimal = Decimal("0"),
    current_protected_profit_giveback: Decimal = Decimal("0"),
    current_cost_stress_add_on: Decimal = Decimal("0"),
    funding_used: Decimal = Decimal("0"),
    funding_reserved: Decimal = Decimal("0"),
    scope_current_planned: Decimal = Decimal("0"),
    scope_current_stress: Decimal = Decimal("0"),
    market_price: Decimal = Decimal("100.5"),
) -> RiskPrecheckRequest:
    requested = make_requested(
        requested_quantity=quantity,
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
            current_stress_loss=scope_current_stress,
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
            protected_profit_giveback=current_protected_profit_giveback,
            cost_stress_add_on=current_cost_stress_add_on,
        ),
        market=MarketRiskInput(
            direction=PositionDirection.LONG,
            mark_price=market_price,
            index_price=market_price - Decimal("0.1"),
            executable_price=market_price,
            initial_invalidation_price=Decimal("90"),
            contract_multiplier=Decimal("1"),
            tick_size=Decimal("0.1"),
            minimum_quantity=Decimal("0.001"),
            minimum_notional=Decimal("5"),
            funding_rate=Decimal("0.0001"),
            max_slippage_bps=Decimal("20"),
            contract_rules_version="rules-test-v1",
            loss_model_version=CANONICAL_LOSS_MODEL_VERSION,
            loss_calculation_ref="test-only:wp-0006-loss",
        ),
        scope_risks=scopes,
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
        command_type="execution.intent.create.v14",
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
        payload_schema_version=14,
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
        payload_schema_version=9,
        reason="evaluate proposal against existing durable exposure",
        payload=request.model_dump(mode="json"),
    )


def create_add_envelope(
    database: Database,
    proposal: FrozenProposalVersion,
    campaign: Campaign,
    package: AddAuthorizationPackage,
    unit: AddUnit,
    *,
    now: datetime,
    candidate_ref: str,
    register_strategy: bool = True,
    strategy_rule_statuses: dict[StrategyRuleId, StrategyRuleStatus] | None = None,
) -> CommandEnvelope:
    with database.session_factory.begin() as session:
        position = (
            session.execute(
                select(VenuePositionSnapshot).order_by(
                    VenuePositionSnapshot.event_time.desc(),
                    VenuePositionSnapshot.venue_position_snapshot_id.desc(),
                )
            )
            .scalars()
            .first()
        )
        protection = (
            session.execute(
                select(VenueProtectionSnapshot).order_by(
                    VenueProtectionSnapshot.event_time.desc(),
                    VenueProtectionSnapshot.venue_protection_snapshot_id.desc(),
                )
            )
            .scalars()
            .first()
        )
        assert position is not None and protection is not None
        assert protection.venue_position_snapshot_id == position.venue_position_snapshot_id
    request = execution_risk_request(
        proposal,
        now=now,
        quantity=Decimal("0.1"),
        requested_funding=Decimal("200"),
        current_open_heat=Decimal("0"),
        current_protected_profit_giveback=Decimal("5"),
        current_cost_stress_add_on=Decimal("0.1608"),
        funding_used=Decimal("500"),
        scope_current_planned=Decimal("5.1608"),
        scope_current_stress=Decimal("6.040175"),
        market_price=Decimal("120"),
    )
    fact_set = risk_fact_set_request_for_risk(
        request,
        now=now,
        fact_set_version=f"risk-fact-set-{candidate_ref}",
    )
    assert register_risk_fact_set(database, fact_set, now=now).status is CommandStatus.COMPLETED
    if register_strategy:
        strategy_evaluation = strategy_evaluation_request_for_add(
            request,
            campaign,
            fact_set,
            position,
            protection,
            now=now,
            evaluation_version=f"strategy-evaluation-{candidate_ref}",
            rule_statuses=strategy_rule_statuses,
        )
        assert (
            register_strategy_evaluation(
                database,
                strategy_evaluation,
                now=now,
            ).status
            is CommandStatus.COMPLETED
        )
    return CommandEnvelope(
        idempotency_key=f"execution-add-{uuid4()}",
        command_type="execution.intent.create.v14",
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
        payload_schema_version=14,
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
                "target_effective_leverage": "1",
                "current_position_equity": "72",
                "position_snapshot_ref": (
                    f"venue-position-snapshot:{position.venue_position_snapshot_id}"
                ),
                "position_snapshot_hash": position.snapshot_hash,
                "protection_snapshot_ref": (
                    f"venue-protection-snapshot:{protection.venue_protection_snapshot_id}"
                ),
                "protection_snapshot_hash": protection.snapshot_hash,
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
    fill_overrides: dict[str, Any] | None = None,
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
            fill_overrides=fill_overrides,
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
    fill_overrides: dict[str, Any] | None,
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
        fill_values: dict[str, Any] = {
            "venue_trade_id": draft.external_fact_id,
            "venue_order_id": venue_order_id,
            "observed_client_order_id": observed_client_order_id,
            "instrument_id": intent.instrument_id,
            "side": VenueSide(intent.side),
            "position_side": position_side,
            "reduce_only": intent.reduce_only,
            "quantity": fill_quantity,
            "fee_amount": Decimal("0"),
            "fee_effect": FeeEffect.ZERO,
            "event_time": event_time,
            "venue_observed_at": event_time,
            "received_at": normalized_at,
        }
        fill_values.update(fill_overrides or {})
        request = fill_request(
            reconciliation_input,
            now=normalized_at,
            **fill_values,
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
            "settlement_currency": intent.risk_currency,
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


def execute_fact(
    database: Database,
    order_intent_id: UUID,
    draft: ExecutionFactDraft,
    *,
    fill_overrides: dict[str, Any] | None = None,
    position_snapshot_overrides: dict[str, Any] | None = None,
    protection_snapshot_overrides: dict[str, Any] | None = None,
):
    _, source_type = _fact_kind_and_source(draft.status)
    claim, run, reconciliation_input, event_time, canonical_context = prepare_active_fact_run(
        database,
        order_intent_id,
        source_type,
        draft=draft,
        fill_overrides=fill_overrides,
        position_snapshot_overrides=position_snapshot_overrides,
        protection_snapshot_overrides=protection_snapshot_overrides,
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


def prepare_open_add_campaign(
    database: Database,
    *,
    unrealized_pnl: Decimal,
    protection_confirmed: bool = True,
) -> tuple[FrozenProposalVersion, Campaign, AddAuthorizationPackage, AddUnit]:
    now = datetime.now(UTC)
    proposal, campaign, initial = prepare_authorization(database, auto_add=True)
    seed_execution_policy(database, now)
    initial_result = execute_create(
        database,
        create_intent_envelope(proposal, campaign, initial, now=now),
    )
    initial_intent_id = UUID(str(initial_result.data["order_intent_id"]))
    facts = [
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
    ]
    if protection_confirmed:
        facts.append(
            fact_request(
                sequence=3,
                status="PROTECTION_CONFIRMED",
                filled=Decimal("0.5"),
                remaining=Decimal("0"),
                terminal=True,
                reconciled=True,
                protected=True,
            )
        )
    for fact in facts:
        result = execute_fact(
            database,
            initial_intent_id,
            fact,
            position_snapshot_overrides=(
                {
                    "entry_price": Decimal("100.5"),
                    "mark_price": Decimal("120"),
                    "notional": Decimal("60"),
                    "unrealized_pnl": unrealized_pnl,
                    "initial_margin": Decimal("30"),
                }
                if fact.status == "POSITION_RECONCILED"
                else None
            ),
            protection_snapshot_overrides=(
                {"worst_active_trigger_price": Decimal("110")}
                if fact.status == "PROTECTION_CONFIRMED"
                else None
            ),
        )
        assert result.status is CommandStatus.COMPLETED
    with database.session_factory.begin() as session:
        package = session.execute(select(AddAuthorizationPackage)).scalar_one()
        unit = session.execute(select(AddUnit)).scalar_one()
        package_state = session.get(AddAuthorizationPackageState, package.add_package_id)
        assert package_state is not None
        if protection_confirmed:
            assert package_state.status == "ACTIVE"
    return proposal, campaign, package, unit


def test_initial_position_reconciliation_freezes_immutable_campaign_economic_baseline(
    database: Database,
) -> None:
    _, campaign, _, _ = prepare_open_add_campaign(database, unrealized_pnl=Decimal("9.75"))

    with database.session_factory.begin() as session:
        baseline = session.execute(select(CampaignEconomicBaseline)).scalar_one()
        intent = session.get(OrderIntent, baseline.initial_order_intent_id)
        fact = session.get(ExecutionFact, baseline.initial_execution_fact_id)
        position = session.get(VenuePositionSnapshot, baseline.position_snapshot_id)
        position_event = next(
            item
            for item in session.scalars(
                select(OutboxMessage).where(OutboxMessage.event_type == "ExecutionFactReconciled")
            )
            if item.payload["target_status"] == "POSITION_RECONCILED"
        )
        snapshot = baseline_snapshot_from_record(baseline)

        assert baseline.campaign_id == campaign.campaign_id
        assert intent is not None and intent.intent_kind == "INITIAL"
        assert fact is not None and fact.target_status == "POSITION_RECONCILED"
        assert position is not None and position.position_state == "OPEN"
        assert baseline.position_snapshot_hash == position.snapshot_hash
        assert baseline.execution_fact_evidence_hash == fact.evidence_hash
        assert baseline.frozen_initial_margin_reference == Decimal("30")
        assert baseline.margin_reference_source == "VENUE_POSITION_INITIAL_MARGIN"
        assert baseline.initial_quantity == Decimal("0.5")
        assert baseline.initial_notional == Decimal("60")
        assert baseline.margin_mode == "ISOLATED"
        assert baseline.environment == "SHADOW"
        assert baseline.real_funds_eligible is False
        assert snapshot.baseline_hash == baseline.baseline_hash
        assert position_event.payload["campaign_economic_baseline_id"] == str(
            baseline.campaign_economic_baseline_id
        )
        assert position_event.payload["campaign_economic_baseline_hash"] == baseline.baseline_hash

    with pytest.raises(DBAPIError, match="campaign_economic_baselines is immutable"):
        with database.engine.begin() as connection:
            connection.execute(
                update(CampaignEconomicBaseline).values(
                    frozen_initial_margin_reference=Decimal("31")
                )
            )
    with pytest.raises(DBAPIError, match="campaign_economic_baselines is immutable"):
        with database.engine.begin() as connection:
            connection.execute(delete(CampaignEconomicBaseline))


def test_canonical_fill_records_immutable_campaign_fee_attribution(
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
    filled = execute_fact(
        database,
        order_intent_id,
        fact_request(
            sequence=1,
            status="FILLED",
            filled=Decimal("0.5"),
            remaining=Decimal("0"),
            terminal=True,
        ),
        fill_overrides={
            "fee_amount": Decimal("0.002"),
            "fee_currency": "BNB",
            "fee_effect": FeeEffect.CHARGE,
            "realized_pnl": None,
            "settlement_currency": "USDT",
        },
    )

    assert filled.status is CommandStatus.COMPLETED
    with database.session_factory.begin() as session:
        entry = session.execute(select(CampaignFillEconomicEntry)).scalar_one()
        fact = session.get(ExecutionFact, entry.execution_fact_id)
        venue_fill = session.get(VenueFill, entry.venue_fill_id)
        fill_event = next(
            item
            for item in session.scalars(
                select(OutboxMessage).where(OutboxMessage.event_type == "ExecutionFactReconciled")
            )
            if item.payload["target_status"] == "FILLED"
        )
        snapshot = fill_entry_snapshot_from_record(entry)

        assert entry.campaign_id == campaign.campaign_id
        assert entry.order_intent_id == order_intent_id
        assert entry.intent_kind == "INITIAL"
        assert entry.add_unit_id is None
        assert entry.economic_effect == "POSITION_INCREASE"
        assert entry.margin_mode == "ISOLATED"
        assert entry.collateral_scope == "ACCOUNT"
        assert entry.collateral_pool_id == "pool-usdt-1"
        assert entry.risk_currency == "USD"
        assert entry.quantity == Decimal("0.5")
        assert entry.fee_amount == Decimal("0.002")
        assert entry.fee_currency == "BNB"
        assert entry.fee_effect == "CHARGE"
        assert entry.realized_pnl is None
        assert entry.realized_pnl_status == "UNKNOWN"
        assert entry.settlement_currency == "USDT"
        assert entry.environment == "SHADOW"
        assert entry.real_funds_eligible is False
        assert fact is not None and fact.fact_kind == "VENUE_FILL"
        assert venue_fill is not None and entry.fill_hash == venue_fill.fill_hash
        assert entry.execution_fact_evidence_hash == fact.evidence_hash
        assert snapshot.entry_hash == entry.entry_hash
        assert filled.data["campaign_fill_economic_entry_id"] == str(
            entry.campaign_fill_economic_entry_id
        )
        assert filled.data["campaign_fill_economic_entry_hash"] == entry.entry_hash
        assert fill_event.payload["campaign_fill_economic_entry_id"] == str(
            entry.campaign_fill_economic_entry_id
        )
        assert fill_event.payload["campaign_fill_economic_entry_hash"] == entry.entry_hash

    with pytest.raises(DBAPIError, match="campaign_fill_economic_entries is immutable"):
        with database.engine.begin() as connection:
            connection.execute(
                update(CampaignFillEconomicEntry).values(fee_amount=Decimal("0.003"))
            )
    with pytest.raises(DBAPIError, match="campaign_fill_economic_entries is immutable"):
        with database.engine.begin() as connection:
            connection.execute(delete(CampaignFillEconomicEntry))


def test_initial_position_reconciliation_rejects_missing_positive_margin_baseline(
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
    filled = execute_fact(
        database,
        order_intent_id,
        fact_request(
            sequence=1,
            status="FILLED",
            filled=Decimal("0.5"),
            remaining=Decimal("0"),
            terminal=True,
        ),
    )
    rejected = execute_fact(
        database,
        order_intent_id,
        fact_request(
            sequence=2,
            status="POSITION_RECONCILED",
            filled=Decimal("0.5"),
            remaining=Decimal("0"),
            terminal=True,
            reconciled=True,
        ),
        position_snapshot_overrides={"initial_margin": Decimal("0")},
    )

    assert filled.status is CommandStatus.COMPLETED
    assert rejected.status is CommandStatus.REJECTED
    assert rejected.error_code == "CAMPAIGN_INITIAL_MARGIN_REFERENCE_UNAVAILABLE"
    with database.session_factory.begin() as session:
        state = session.get(OrderIntentState, order_intent_id)
        assert state is not None and state.status == "FILLED"
        assert count_rows(session, CampaignEconomicBaseline) == 0
        assert count_rows(session, ExecutionFact) == 1


def test_execution_intent_legacy_commands_and_wrong_v14_schema_are_rejected(
    database: Database,
) -> None:
    proposal, campaign, initial = prepare_authorization(database)
    v1 = create_intent_envelope(proposal, campaign, initial, now=datetime.now(UTC)).model_copy(
        update={"command_type": "execution.intent.create.v1"}
    )
    v2 = create_intent_envelope(proposal, campaign, initial, now=datetime.now(UTC)).model_copy(
        update={"command_type": "execution.intent.create.v2"}
    )
    v3 = create_intent_envelope(proposal, campaign, initial, now=datetime.now(UTC)).model_copy(
        update={"command_type": "execution.intent.create.v3"}
    )
    v4 = create_intent_envelope(proposal, campaign, initial, now=datetime.now(UTC)).model_copy(
        update={"command_type": "execution.intent.create.v4"}
    )
    v5 = create_intent_envelope(proposal, campaign, initial, now=datetime.now(UTC)).model_copy(
        update={"command_type": "execution.intent.create.v5"}
    )
    v6 = create_intent_envelope(proposal, campaign, initial, now=datetime.now(UTC)).model_copy(
        update={"command_type": "execution.intent.create.v6"}
    )
    v7 = create_intent_envelope(proposal, campaign, initial, now=datetime.now(UTC)).model_copy(
        update={"command_type": "execution.intent.create.v7"}
    )
    v8 = create_intent_envelope(proposal, campaign, initial, now=datetime.now(UTC)).model_copy(
        update={"command_type": "execution.intent.create.v8"}
    )
    v9 = create_intent_envelope(proposal, campaign, initial, now=datetime.now(UTC)).model_copy(
        update={"command_type": "execution.intent.create.v9"}
    )
    v10 = create_intent_envelope(proposal, campaign, initial, now=datetime.now(UTC)).model_copy(
        update={"command_type": "execution.intent.create.v10"}
    )
    v11 = create_intent_envelope(proposal, campaign, initial, now=datetime.now(UTC)).model_copy(
        update={"command_type": "execution.intent.create.v11"}
    )
    v12 = create_intent_envelope(proposal, campaign, initial, now=datetime.now(UTC)).model_copy(
        update={"command_type": "execution.intent.create.v12"}
    )
    v13 = create_intent_envelope(proposal, campaign, initial, now=datetime.now(UTC)).model_copy(
        update={"command_type": "execution.intent.create.v13"}
    )
    old_field = create_intent_envelope(proposal, campaign, initial, now=datetime.now(UTC))
    old_payload = dict(old_field.payload)
    old_risk_request = dict(old_payload["risk_request"])
    old_scopes = [dict(scope) for scope in old_risk_request["scope_risks"]]
    old_scopes[0]["requested_incremental_stress_loss"] = "1"
    old_risk_request["scope_risks"] = old_scopes
    old_payload["risk_request"] = old_risk_request
    old_field = old_field.model_copy(update={"payload": old_payload})
    caller_boolean = create_intent_envelope(proposal, campaign, initial, now=datetime.now(UTC))
    caller_boolean_payload = dict(caller_boolean.payload)
    caller_boolean_risk = dict(caller_boolean_payload["risk_request"])
    caller_boolean_risk["instrument_classified"] = True
    caller_boolean_payload["risk_request"] = caller_boolean_risk
    caller_boolean = caller_boolean.model_copy(update={"payload": caller_boolean_payload})
    protection_boolean = create_intent_envelope(
        proposal,
        campaign,
        initial,
        now=datetime.now(UTC),
    )
    protection_boolean_payload = dict(protection_boolean.payload)
    protection_boolean_risk = dict(protection_boolean_payload["risk_request"])
    protection_boolean_risk["protection_available"] = True
    protection_boolean_payload["risk_request"] = protection_boolean_risk
    protection_boolean = protection_boolean.model_copy(
        update={"payload": protection_boolean_payload}
    )
    caller_facts = create_intent_envelope(
        proposal,
        campaign,
        initial,
        now=datetime.now(UTC),
    )
    caller_facts_payload = dict(caller_facts.payload)
    caller_facts_risk = dict(caller_facts_payload["risk_request"])
    caller_facts_risk["facts"] = []
    caller_facts_payload["risk_request"] = caller_facts_risk
    caller_facts = caller_facts.model_copy(update={"payload": caller_facts_payload})
    wrong_schema = create_intent_envelope(
        proposal,
        campaign,
        initial,
        now=datetime.now(UTC),
    ).model_copy(update={"payload_schema_version": 2})

    v1_result = execute_create(database, v1)
    v2_result = execute_create(database, v2)
    v3_result = execute_create(database, v3)
    v4_result = execute_create(database, v4)
    v5_result = execute_create(database, v5)
    v6_result = execute_create(database, v6)
    v7_result = execute_create(database, v7)
    v8_result = execute_create(database, v8)
    v9_result = execute_create(database, v9)
    v10_result = execute_create(database, v10)
    v11_result = execute_create(database, v11)
    v12_result = execute_create(database, v12)
    v13_result = execute_create(database, v13)
    old_field_result = execute_create(database, old_field)
    caller_boolean_result = execute_create(database, caller_boolean)
    protection_boolean_result = execute_create(database, protection_boolean)
    caller_facts_result = execute_create(database, caller_facts)
    schema_result = execute_create(database, wrong_schema)

    assert v1_result.status is CommandStatus.REJECTED
    assert v1_result.error_code == "COMMAND_TYPE_MISMATCH"
    assert v2_result.status is CommandStatus.REJECTED
    assert v2_result.error_code == "COMMAND_TYPE_MISMATCH"
    assert v3_result.status is CommandStatus.REJECTED
    assert v3_result.error_code == "COMMAND_TYPE_MISMATCH"
    assert v4_result.status is CommandStatus.REJECTED
    assert v4_result.error_code == "COMMAND_TYPE_MISMATCH"
    assert v5_result.status is CommandStatus.REJECTED
    assert v5_result.error_code == "COMMAND_TYPE_MISMATCH"
    assert v6_result.status is CommandStatus.REJECTED
    assert v6_result.error_code == "COMMAND_TYPE_MISMATCH"
    assert v7_result.status is CommandStatus.REJECTED
    assert v7_result.error_code == "COMMAND_TYPE_MISMATCH"
    assert v8_result.status is CommandStatus.REJECTED
    assert v8_result.error_code == "COMMAND_TYPE_MISMATCH"
    assert v9_result.status is CommandStatus.REJECTED
    assert v9_result.error_code == "COMMAND_TYPE_MISMATCH"
    assert v10_result.status is CommandStatus.REJECTED
    assert v10_result.error_code == "COMMAND_TYPE_MISMATCH"
    assert v11_result.status is CommandStatus.REJECTED
    assert v11_result.error_code == "COMMAND_TYPE_MISMATCH"
    assert v12_result.status is CommandStatus.REJECTED
    assert v12_result.error_code == "COMMAND_TYPE_MISMATCH"
    assert v13_result.status is CommandStatus.REJECTED
    assert v13_result.error_code == "COMMAND_TYPE_MISMATCH"
    assert old_field_result.status is CommandStatus.REJECTED
    assert old_field_result.error_code == "EXECUTION_INPUT_INVALID"
    assert caller_boolean_result.status is CommandStatus.REJECTED
    assert caller_boolean_result.error_code == "EXECUTION_INPUT_INVALID"
    assert protection_boolean_result.status is CommandStatus.REJECTED
    assert protection_boolean_result.error_code == "EXECUTION_INPUT_INVALID"
    assert caller_facts_result.status is CommandStatus.REJECTED
    assert caller_facts_result.error_code == "EXECUTION_INPUT_INVALID"
    assert schema_result.status is CommandStatus.REJECTED
    assert schema_result.error_code == "PAYLOAD_SCHEMA_VERSION_MISMATCH"


def test_add_leverage_calculation_rejects_tampered_value_or_hash() -> None:
    material = {
        "position_snapshot_id": str(uuid4()),
        "position_snapshot_hash": "a" * 64,
        "current_position_quantity": "0.5",
        "mark_price": "120",
        "contract_multiplier": "1",
        "submitted_campaign_equity": "72",
        "campaign_equity_source": "CALLER_PENDING_CAMPAIGN_PNL_LEDGER",
        "current_position_notional": "60.0",
        "current_effective_leverage": "0.833333333333333334",
        "calculation_version": "add-effective-leverage-v1",
    }
    calculation = AddLeverageCalculation.model_validate(
        {**material, "calculation_hash": hash_json(material)}
    )
    assert calculation.current_effective_leverage == Decimal("0.833333333333333334")

    with pytest.raises(ValueError, match="calculation is inconsistent"):
        AddLeverageCalculation.model_validate(
            {
                **material,
                "current_effective_leverage": "0.1",
                "calculation_hash": hash_json(material),
            }
        )
    with pytest.raises(ValueError, match="calculation hash mismatch"):
        AddLeverageCalculation.model_validate({**material, "calculation_hash": "f" * 64})


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
    assert result.data["initial_flat_position_snapshot_id"] is not None
    assert result.data["initial_flat_position_snapshot_hash"] is not None
    with database.session_factory.begin() as session:
        decision = session.execute(select(ExecutionRiskDecision)).scalar_one()
        intent = session.execute(select(OrderIntent)).scalar_one()
        reservation = session.execute(select(RiskReservation)).scalar_one()
        exposure = session.execute(select(RiskExposureState)).scalar_one()
        initial_event = session.execute(
            select(OutboxMessage).where(
                OutboxMessage.event_type == "ShadowOrderIntentRiskReserved",
                OutboxMessage.message_key == f"OrderIntent:{intent.order_intent_id}",
            )
        ).scalar_one()
        assert decision.result == "ALLOW"
        assert decision.initial_flat_position_snapshot_id is not None
        flat_position = session.get(
            VenuePositionSnapshot,
            decision.initial_flat_position_snapshot_id,
        )
        assert flat_position is not None
        assert flat_position.position_state == "FLAT"
        assert flat_position.quantity == 0
        assert decision.initial_flat_position_snapshot_hash == flat_position.snapshot_hash
        assert decision.input_snapshot["initial_flat_position"]["source_snapshot_id"] == str(
            flat_position.venue_position_snapshot_id
        )
        assert (
            decision.decision["initial_flat_position_snapshot_hash"] == flat_position.snapshot_hash
        )
        assert initial_event.payload["initial_flat_position_snapshot_id"] == str(
            flat_position.venue_position_snapshot_id
        )
        assert (
            initial_event.payload["initial_flat_position_snapshot_hash"]
            == flat_position.snapshot_hash
        )
        assert decision.valid_until <= flat_position.event_time + timedelta(seconds=5)
        assert (
            decision.capital_scope_manifest_id == TEST_EXECUTION_CAPITAL_SCOPE_MANIFEST.manifest_id
        )
        assert decision.capital_scope_manifest_version == 1
        assert (
            decision.capital_scope_manifest_hash
            == TEST_EXECUTION_CAPITAL_SCOPE_MANIFEST.manifest_hash
        )
        assert decision.capital_projection_version == "portfolio-mtm-v2"
        assert decision.catalog_record_id is not None
        catalog_record = session.get(InstrumentCatalogRecord, decision.catalog_record_id)
        assert catalog_record is not None
        assert decision.catalog_version == catalog_record.catalog_version
        assert decision.catalog_classification_version == catalog_record.classification_version
        assert decision.catalog_record_hash == catalog_record.record_hash
        assert decision.input_snapshot["evaluation"]["instrument_classification"]["valid"] is True
        assert decision.decision["catalog_record_hash"] == catalog_record.record_hash
        assert decision.protection_capability_record_id is not None
        protection_record = session.get(
            InstrumentProtectionCapabilityRecord,
            decision.protection_capability_record_id,
        )
        assert protection_record is not None
        assert (
            decision.protection_capability_version
            == protection_record.position_management_template_version
        )
        assert decision.protection_capability_record_hash == protection_record.record_hash
        assert decision.input_snapshot["evaluation"]["protection_capability"]["valid"] is True
        assert (
            decision.decision["protection_capability_record_hash"] == protection_record.record_hash
        )
        assert decision.risk_fact_set_id is not None
        fact_set = session.get(RiskFactSetRecord, decision.risk_fact_set_id)
        assert fact_set is not None
        assert decision.risk_fact_set_version == fact_set.fact_set_version
        assert decision.risk_fact_set_record_hash == fact_set.record_hash
        assert decision.input_snapshot["evaluation"]["risk_fact_set"]["valid"] is True
        assert decision.decision["risk_fact_set_record_hash"] == fact_set.record_hash
        assert (
            decision.decision["market_fact_payload_hash"]
            == decision.decision["market_observation_payload_hash"]
        )
        assert (
            hash_json(decision.input_snapshot["capital_projection"])
            == decision.capital_projection_hash
        )
        assert (
            hash_json(decision.input_snapshot["durable_exposure_snapshot"])
            == decision.durable_exposure_snapshot_hash
        )
        assert decision.input_snapshot["durable_exposure_snapshot"]["components"] == []
        assert decision.decision["requested_base_heat"] == "5.250000000000000000"
        assert decision.decision["current_trade_loss"]["protected_profit_giveback"] == "0"
        assert decision.decision["current_protected_position_risk_calculation_hash"] is None
        assert decision.decision["requested_fee_stress"] == "0.050250000000000000"
        assert decision.decision["requested_stop_penetration_stress"] == ("0.100500000000000000")
        assert decision.decision["requested_adverse_funding_stress"] == ("0.010050000000000000")
        assert decision.decision["requested_cost_stress_add_on"] == "0.160800000000000000"
        assert decision.decision["requested_incremental_worst_case_loss"] == (
            "5.410800000000000000"
        )
        assert decision.decision["cost_stress_model_version"] == ("fee-stop-funding-stress-v1")
        underlying_stress = next(
            scope
            for scope in decision.decision["scope_decisions"]
            if scope["scope_type"] == "UNDERLYING"
        )
        assert Decimal(underlying_stress["incremental_planned_loss"]) == Decimal("5.4108")
        assert Decimal(underlying_stress["gap_stress_add_on"]) == Decimal("0.25125")
        assert Decimal(underlying_stress["liquidity_degradation_stress_add_on"]) == Decimal(
            "0.5025"
        )
        assert Decimal(underlying_stress["unprotected_window_stress_add_on"]) == Decimal("0.125625")
        assert Decimal(underlying_stress["incremental_stress_loss"]) == Decimal("6.290175")
        assert (
            underlying_stress["scope_stress_model_version"] == "planned-loss-plus-scope-shocks-v1"
        )
        assert (
            "requested_reserved_heat"
            not in decision.input_snapshot["request"]["risk_request"]["requested"]
        )
        assert (
            "requested_cost_stress_add_on"
            not in decision.input_snapshot["request"]["risk_request"]["requested"]
        )
        assert (
            "requested_protected_profit_giveback"
            not in decision.input_snapshot["request"]["risk_request"]["requested"]
        )
        assert all(
            "requested_incremental_planned_loss" not in scope
            for scope in decision.input_snapshot["request"]["risk_request"]["scope_risks"]
        )
        assert all(
            "requested_incremental_stress_loss" not in scope
            for scope in decision.input_snapshot["request"]["risk_request"]["scope_risks"]
        )
        assert intent.dispatch_eligible is False
        assert reservation.order_intent_id == intent.order_intent_id
        assert reservation.reserved_heat == Decimal("5.4108")
        assert reservation.base_heat_reserved == Decimal("5.25")
        assert reservation.protected_profit_giveback_reserved == 0
        assert reservation.cost_stress_add_on_reserved == Decimal("0.1608")
        assert all(
            Decimal(allocation["planned_loss"]) == Decimal("5.4108")
            and Decimal(allocation["stress_loss"]) == Decimal("6.290175")
            for allocation in reservation.scope_allocations
        )
        assert exposure.status == "RESERVED"
        assert exposure.total_heat == Decimal("5.4108")
        assert count_rows(session, RiskLedgerEntry) == 1
        assert count_rows(session, OrderIntentStateHistory) == 1
        assert count_rows(session, RiskExposureStateHistory) == 1
        audit_count = count_rows(session, AuditEvent)
        assert audit_count >= 3
        assert count_rows(session, OutboxMessage) == audit_count


@pytest.mark.parametrize(
    ("position_state", "event_age", "expected_error"),
    (
        (
            VenuePositionState.OPEN,
            timedelta(seconds=1),
            "INITIAL_REQUIRES_CANONICAL_FLAT_POSITION",
        ),
        (
            VenuePositionState.UNKNOWN,
            timedelta(seconds=1),
            "INITIAL_CURRENT_POSITION_UNAVAILABLE",
        ),
        (
            VenuePositionState.FLAT,
            timedelta(seconds=10),
            "INITIAL_CURRENT_POSITION_UNAVAILABLE",
        ),
    ),
)
def test_initial_intent_rejects_nonflat_unknown_or_stale_canonical_position_without_side_effects(
    database: Database,
    position_state: VenuePositionState,
    event_age: timedelta,
    expected_error: str,
) -> None:
    position_time = datetime.now(UTC) - event_age
    proposal, campaign, initial = prepare_authorization(
        database,
        position_state=position_state,
        position_event_time=position_time,
    )
    now = datetime.now(UTC)
    seed_execution_policy(database, now)

    result = execute_create(
        database,
        create_intent_envelope(proposal, campaign, initial, now=now),
        ExecutionIntentService(clock=lambda: now),
    )

    assert result.status is CommandStatus.REJECTED
    assert result.error_code == expected_error
    with database.session_factory.begin() as session:
        assert count_rows(session, ExecutionRiskDecision) == 0
        assert count_rows(session, OrderIntent) == 0
        assert count_rows(session, RiskReservation) == 0


def test_initial_intent_rejects_missing_canonical_position_without_side_effects(
    database: Database,
) -> None:
    proposal, campaign, initial = prepare_authorization(database, register_position=False)
    now = datetime.now(UTC)
    seed_execution_policy(database, now)

    result = execute_create(
        database,
        create_intent_envelope(proposal, campaign, initial, now=now),
        ExecutionIntentService(clock=lambda: now),
    )

    assert result.status is CommandStatus.REJECTED
    assert result.error_code == "INITIAL_CURRENT_POSITION_UNAVAILABLE"
    with database.session_factory.begin() as session:
        assert count_rows(session, ExecutionRiskDecision) == 0
        assert count_rows(session, OrderIntent) == 0
        assert count_rows(session, RiskReservation) == 0


def test_initial_intent_rejects_nonzero_caller_position_quantity(
    database: Database,
) -> None:
    proposal, campaign, initial = prepare_authorization(database)
    envelope = create_intent_envelope(proposal, campaign, initial, now=datetime.now(UTC))
    payload = dict(envelope.payload)
    payload["current_position_quantity"] = "0.1"
    payload["target_position_quantity"] = "0.6"

    result = execute_create(database, envelope.model_copy(update={"payload": payload}))

    assert result.status is CommandStatus.REJECTED
    assert result.error_code == "EXECUTION_INPUT_INVALID"
    with database.session_factory.begin() as session:
        assert count_rows(session, ExecutionRiskDecision) == 0
        assert count_rows(session, OrderIntent) == 0
        assert count_rows(session, RiskReservation) == 0


def test_final_precheck_denies_when_exact_catalog_record_is_missing(
    database: Database,
) -> None:
    proposal, campaign, initial = prepare_authorization(database, register_catalog=False)
    now = datetime.now(UTC)
    seed_execution_policy(database, now)

    result = execute_create(
        database,
        create_intent_envelope(proposal, campaign, initial, now=now),
        ExecutionIntentService(clock=lambda: now),
    )

    assert result.status is CommandStatus.COMPLETED
    assert result.data["result"] == "DENY"
    assert result.data["primary_reason_code"] == "INSTRUMENT_UNCLASSIFIED"
    assert result.data["catalog_record_id"] is None
    assert result.data["initial_flat_position_snapshot_id"] is not None
    assert result.data["initial_flat_position_snapshot_hash"] is not None
    assert result.data["catalog_validation_reason_codes"] == ["INSTRUMENT_CATALOG_RECORD_NOT_FOUND"]
    with database.session_factory.begin() as session:
        decision = session.execute(select(ExecutionRiskDecision)).scalar_one()
        assert decision.catalog_record_id is None
        assert decision.initial_flat_position_snapshot_id is not None
        assert decision.initial_flat_position_snapshot_hash is not None
        assert decision.input_snapshot["initial_flat_position"]["position_state"] == "FLAT"
        assert decision.valid_until == now
        assert count_rows(session, OrderIntent) == 0
        assert count_rows(session, RiskReservation) == 0


def test_final_precheck_denies_when_exact_protection_capability_is_missing(
    database: Database,
) -> None:
    proposal, campaign, initial = prepare_authorization(
        database,
        register_protection=False,
    )
    now = datetime.now(UTC)
    seed_execution_policy(database, now)

    result = execute_create(
        database,
        create_intent_envelope(proposal, campaign, initial, now=now),
        ExecutionIntentService(clock=lambda: now),
    )

    assert result.status is CommandStatus.COMPLETED
    assert result.data["result"] == "DENY"
    assert result.data["primary_reason_code"] == "PROTECTION_UNAVAILABLE"
    assert result.data["protection_capability_record_id"] is None
    assert result.data["protection_capability_reason_codes"] == [
        "PROTECTION_CAPABILITY_RECORD_NOT_FOUND"
    ]
    with database.session_factory.begin() as session:
        decision = session.execute(select(ExecutionRiskDecision)).scalar_one()
        assert decision.catalog_record_id is not None
        assert decision.protection_capability_record_id is None
        assert decision.valid_until == now
        assert count_rows(session, OrderIntent) == 0
        assert count_rows(session, RiskReservation) == 0


def test_final_precheck_denies_when_exact_risk_fact_set_is_missing(
    database: Database,
) -> None:
    proposal, campaign, initial = prepare_authorization(
        database,
        register_fact_set=False,
    )
    now = datetime.now(UTC)
    seed_execution_policy(database, now)

    result = execute_create(
        database,
        create_intent_envelope(proposal, campaign, initial, now=now),
        ExecutionIntentService(clock=lambda: now),
    )

    assert result.status is CommandStatus.COMPLETED
    assert result.data["result"] == "DENY"
    assert result.data["primary_reason_code"] == "RISK_FACT_SET_UNAVAILABLE"
    assert result.data["risk_fact_set_id"] is None
    assert result.data["risk_fact_set_reason_codes"] == ["RISK_FACT_SET_RECORD_NOT_FOUND"]
    with database.session_factory.begin() as session:
        decision = session.execute(select(ExecutionRiskDecision)).scalar_one()
        assert decision.catalog_record_id is not None
        assert decision.protection_capability_record_id is not None
        assert decision.risk_fact_set_id is None
        assert decision.valid_until == now
        assert count_rows(session, OrderIntent) == 0
        assert count_rows(session, RiskReservation) == 0


def test_final_precheck_denies_market_payload_not_bound_to_durable_observation(
    database: Database,
) -> None:
    proposal, campaign, initial = prepare_authorization(database)
    now = datetime.now(UTC)
    seed_execution_policy(database, now)
    request = execution_risk_request(proposal, now=now)
    tampered = request.model_copy(
        update={"market": request.market.model_copy(update={"funding_rate": Decimal("0.0002")})}
    )

    result = execute_create(
        database,
        create_intent_envelope(
            proposal,
            campaign,
            initial,
            now=now,
            risk_request=tampered,
        ),
        ExecutionIntentService(clock=lambda: now),
    )

    assert result.status is CommandStatus.COMPLETED
    assert result.data["result"] == "DENY"
    assert result.data["primary_reason_code"] == "MARKET_FACT_BINDING_MISMATCH"
    assert result.data["market_fact_payload_hash"] != result.data["market_observation_payload_hash"]
    with database.session_factory.begin() as session:
        decision = session.execute(select(ExecutionRiskDecision)).scalar_one()
        assert (
            decision.decision["market_fact_payload_hash"]
            != decision.decision["market_observation_payload_hash"]
        )
        assert count_rows(session, OrderIntent) == 0
        assert count_rows(session, RiskReservation) == 0


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
            risk_request=execution_risk_request(proposal, now=now),
        ),
    )
    assert created.status is CommandStatus.COMPLETED
    exact_risk = execution_risk_request(
        proposal,
        now=datetime.now(UTC),
        current_reserved_heat=Decimal("5.25"),
        current_cost_stress_add_on=Decimal("0.1608"),
        funding_reserved=Decimal("500"),
        scope_current_planned=Decimal("5.4108"),
        scope_current_stress=Decimal("6.290175"),
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
    assert verified.risk_request.current_trade_loss.reserved_heat == Decimal("5.25")
    assert verified.risk_request.current_trade_loss.protected_profit_giveback == 0
    assert verified.risk_request.current_trade_loss.cost_stress_add_on == Decimal("0.1608")
    assert verified.snapshot.global_margin_reserved == Decimal("500")
    assert verified.snapshot.campaign_reserved_heat == Decimal("5.25")
    assert verified.snapshot.campaign_protected_profit_giveback == 0
    assert verified.snapshot.campaign_cost_stress_add_on == Decimal("0.1608")
    assert verified.snapshot.snapshot_version == "durable-risk-exposure-v2"
    assert verified.snapshot.components[0].base_heat_reserved == Decimal("5.25")
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
        scope_current_planned=Decimal("5.4108"),
        scope_current_stress=Decimal("6.290175"),
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
        current_reserved_heat=Decimal("5.25"),
        current_cost_stress_add_on=Decimal("0.1608"),
        funding_reserved=Decimal("500"),
        scope_current_planned=Decimal("5.4108"),
        scope_current_stress=Decimal("6.290175"),
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
    proposal, campaign, initial = prepare_authorization(database)
    now = datetime.now(UTC)
    seed_execution_policy(database, now)
    risk_request = execution_risk_request(proposal, now=now)
    fact_set = risk_fact_set_request_for_risk(
        risk_request,
        now=now,
        fact_set_version="risk-fact-set-stale-execution-v2",
        fact_age=timedelta(seconds=20),
    )
    assert register_risk_fact_set(database, fact_set, now=now).status is CommandStatus.COMPLETED

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

    current = execution_risk_request(
        proposal,
        now=datetime.now(UTC),
        current_open_heat=Decimal("2.10"),
        current_cost_stress_add_on=Decimal("0.06432"),
        funding_used=Decimal("200"),
        scope_current_planned=Decimal("2.16432"),
        scope_current_stress=Decimal("2.51607"),
    )
    request = CreateExecutionIntentRequest.model_validate(
        create_intent_envelope(
            proposal,
            campaign,
            initial,
            now=datetime.now(UTC),
            candidate_ref="partial-release-loss-components",
            risk_request=current,
        ).payload
    )
    with database.session_factory.begin() as session:
        verified = DurableExposureResolver.resolve(session, request, campaign)
    assert verified.risk_request.current_trade_loss.open_heat == Decimal("2.10")
    assert verified.risk_request.current_trade_loss.cost_stress_add_on == Decimal("0.06432")
    assert verified.risk_request.current_trade_loss.total == Decimal("2.16432")


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


@pytest.mark.parametrize(
    "canonical_upnl",
    (Decimal("0"), Decimal("-1")),
    ids=("zero", "negative"),
)
def test_add_rejects_nonpositive_canonical_upnl_without_intent_or_reservation(
    database: Database,
    canonical_upnl: Decimal,
) -> None:
    proposal, campaign, package, unit = prepare_open_add_campaign(
        database,
        unrealized_pnl=canonical_upnl,
    )

    result = execute_create(
        database,
        create_add_envelope(
            database,
            proposal,
            campaign,
            package,
            unit,
            now=datetime.now(UTC),
            candidate_ref=f"add-nonpositive-canonical-upnl-{canonical_upnl}",
        ),
    )

    assert result.status is CommandStatus.REJECTED
    assert result.error_code == "ADD_CURRENT_UNREALIZED_PNL_NOT_POSITIVE"
    with database.session_factory.begin() as session:
        assert (
            session.execute(
                select(func.count())
                .select_from(ExecutionRiskDecision)
                .where(ExecutionRiskDecision.intent_kind == "ADD")
            ).scalar_one()
            == 0
        )
        assert (
            session.execute(
                select(func.count())
                .select_from(OrderIntent)
                .where(OrderIntent.intent_kind == "ADD")
            ).scalar_one()
            == 0
        )
        assert (
            session.execute(
                select(func.count())
                .select_from(RiskReservation)
                .where(RiskReservation.add_package_id.is_not(None))
            ).scalar_one()
            == 0
        )
        unit_state = session.get(AddUnitState, unit.add_unit_id)
        assert unit_state is not None and unit_state.status == "AVAILABLE"


@pytest.mark.parametrize(
    ("mode", "rule_status", "expected_reason"),
    (
        ("missing", None, "STRATEGY_EVALUATION_RECORD_NOT_FOUND"),
        ("failed", StrategyRuleStatus.FAIL, "STRATEGY_EVALUATION_OUTCOME_NOT_PASS"),
        ("unknown", StrategyRuleStatus.UNKNOWN, "STRATEGY_EVALUATION_OUTCOME_NOT_PASS"),
    ),
)
def test_add_denies_without_exact_passing_strategy_evaluation(
    database: Database,
    mode: str,
    rule_status: StrategyRuleStatus | None,
    expected_reason: str,
) -> None:
    proposal, campaign, package, unit = prepare_open_add_campaign(
        database,
        unrealized_pnl=Decimal("9.75"),
    )
    envelope = create_add_envelope(
        database,
        proposal,
        campaign,
        package,
        unit,
        now=datetime.now(UTC),
        candidate_ref=f"add-strategy-evaluation-{mode}",
        register_strategy=mode != "missing",
        strategy_rule_statuses=(
            {StrategyRuleId.TREND_CONTINUATION: rule_status} if rule_status is not None else None
        ),
    )

    result = execute_create(database, envelope)

    assert result.status is CommandStatus.COMPLETED
    assert result.data["result"] == "DENY"
    assert result.data["primary_reason_code"] == expected_reason
    assert result.data["reservation_created"] is False
    assert result.data["order_intent_created"] is False
    with database.session_factory.begin() as session:
        decision = session.execute(
            select(ExecutionRiskDecision).where(ExecutionRiskDecision.intent_kind == "ADD")
        ).scalar_one()
        assert decision.primary_reason_code == expected_reason
        if mode == "missing":
            assert decision.strategy_evaluation_id is None
            assert decision.input_snapshot["strategy_evaluation"]["valid"] is False
            assert decision.input_snapshot["strategy_evaluation"]["strategy_evaluation_id"] is None
        else:
            assert decision.strategy_evaluation_id is not None
            assert decision.input_snapshot["strategy_evaluation"]["valid"] is False
        assert (
            session.execute(
                select(func.count())
                .select_from(OrderIntent)
                .where(OrderIntent.intent_kind == "ADD")
            ).scalar_one()
            == 0
        )
        assert (
            session.execute(
                select(func.count())
                .select_from(RiskReservation)
                .where(RiskReservation.add_package_id.is_not(None))
            ).scalar_one()
            == 0
        )
        unit_state = session.get(AddUnitState, unit.add_unit_id)
        assert unit_state is not None and unit_state.status == "AVAILABLE"


def test_add_zero_fill_releases_unit_then_positive_fill_consumes_it(
    database: Database,
) -> None:
    proposal, campaign, package, unit = prepare_open_add_campaign(
        database,
        unrealized_pnl=Decimal("9.75"),
    )

    for legacy_field, legacy_value in (
        ("protection_valid", True),
        ("authorization_valid", True),
        ("current_effective_leverage", "0"),
        ("trend_valid", True),
    ):
        legacy_input = create_add_envelope(
            database,
            proposal,
            campaign,
            package,
            unit,
            now=datetime.now(UTC),
            candidate_ref=f"add-legacy-{legacy_field}",
        )
        legacy_payload = dict(legacy_input.payload)
        legacy_eligibility = dict(legacy_payload["add_eligibility"])
        legacy_eligibility[legacy_field] = legacy_value
        legacy_payload["add_eligibility"] = legacy_eligibility
        legacy_result = execute_create(
            database,
            legacy_input.model_copy(update={"payload": legacy_payload}),
        )
        assert legacy_result.status is CommandStatus.REJECTED
        assert legacy_result.error_code == "EXECUTION_INPUT_INVALID"

    leverage_at_minimum = create_add_envelope(
        database,
        proposal,
        campaign,
        package,
        unit,
        now=datetime.now(UTC),
        candidate_ref="add-derived-leverage-at-minimum",
    )
    leverage_payload = dict(leverage_at_minimum.payload)
    leverage_eligibility = dict(leverage_payload["add_eligibility"])
    leverage_eligibility["current_position_equity"] = "60"
    leverage_eligibility["target_effective_leverage"] = "1.2"
    leverage_payload["add_eligibility"] = leverage_eligibility
    leverage_result = execute_create(
        database,
        leverage_at_minimum.model_copy(update={"payload": leverage_payload}),
    )
    assert leverage_result.status is CommandStatus.REJECTED
    assert leverage_result.error_code == "ADD_LEVERAGE_NOT_BELOW_MINIMUM"

    tampered = create_add_envelope(
        database,
        proposal,
        campaign,
        package,
        unit,
        now=datetime.now(UTC),
        candidate_ref="add-tampered-protection-evidence",
    )
    tampered_payload = dict(tampered.payload)
    tampered_eligibility = dict(tampered_payload["add_eligibility"])
    tampered_eligibility["protection_snapshot_hash"] = "f" * 64
    tampered_payload["add_eligibility"] = tampered_eligibility
    tampered_result = execute_create(
        database,
        tampered.model_copy(update={"payload": tampered_payload}),
    )
    assert tampered_result.status is CommandStatus.REJECTED
    assert tampered_result.error_code == "ADD_ELIGIBILITY_CANONICAL_FACT_MISMATCH"

    first_add = execute_create(
        database,
        create_add_envelope(
            database,
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
    with database.session_factory.begin() as session:
        add_decision = session.execute(
            select(ExecutionRiskDecision).where(ExecutionRiskDecision.intent_kind == "ADD")
        ).scalar_one()
        add_reservation = session.execute(
            select(RiskReservation).where(RiskReservation.order_intent_id == first_add_id)
        ).scalar_one()
        add_event = session.execute(
            select(OutboxMessage).where(
                OutboxMessage.event_type == "ShadowOrderIntentRiskReserved",
                OutboxMessage.message_key == f"OrderIntent:{first_add_id}",
            )
        ).scalar_one()
        protected = add_decision.input_snapshot["protected_position_risk"]
        leverage = add_decision.input_snapshot["add_leverage_calculation"]
        strategy = add_decision.input_snapshot["strategy_evaluation"]
        assert Decimal(leverage["current_position_notional"]) == Decimal("60")
        assert Decimal(leverage["submitted_campaign_equity"]) == Decimal("72")
        assert Decimal(leverage["current_effective_leverage"]) == Decimal("0.833333333333333334")
        assert leverage["campaign_equity_source"] == "CALLER_PENDING_CAMPAIGN_PNL_LEDGER"
        assert (
            hash_json({key: value for key, value in leverage.items() if key != "calculation_hash"})
            == leverage["calculation_hash"]
        )
        assert (
            add_decision.decision["add_leverage_calculation_hash"]
            == leverage["calculation_hash"]
            == first_add.data["add_leverage_calculation_hash"]
            == add_event.payload["add_leverage_calculation_hash"]
        )
        assert strategy["valid"] is True
        assert strategy["outcome"] == "PASS"
        assert len(strategy["rule_results"]) == 3
        assert add_decision.strategy_evaluation_id == UUID(str(strategy["strategy_evaluation_id"]))
        assert (
            add_decision.strategy_evaluation_record_hash
            == strategy["record_hash"]
            == first_add.data["strategy_evaluation_record_hash"]
            == add_event.payload["strategy_evaluation_record_hash"]
        )
        assert add_decision.valid_until <= datetime.fromisoformat(strategy["valid_until"])
        assert Decimal(protected["current_to_protection_loss"]) == Decimal("5")
        assert Decimal(protected["unrealized_pnl"]) == Decimal("9.75")
        assert Decimal(protected["open_heat"]) == 0
        assert Decimal(protected["protected_profit_giveback"]) == Decimal("5")
        assert protected["scope"]["settlement_currency"] == "USD"
        assert protected["calculation_hash"] is not None
        assert (
            add_decision.decision["current_protected_position_risk_calculation_hash"]
            == protected["calculation_hash"]
        )
        protected_valid_until = datetime.fromisoformat(
            add_decision.input_snapshot["protected_position_risk_valid_until"]
        )
        assert add_decision.valid_until <= protected_valid_until
        assert Decimal(add_decision.decision["current_trade_loss"]["open_heat"]) == 0
        assert Decimal(
            add_decision.decision["current_trade_loss"]["protected_profit_giveback"]
        ) == Decimal("5")
        derived_scopes = add_decision.input_snapshot["request"]["risk_request"]["scope_risks"]
        assert all(
            Decimal(scope["current_planned_loss"]) == Decimal("5.1608")
            and Decimal(scope["current_stress_loss"]) == Decimal("6.040175")
            for scope in derived_scopes
        )
        assert add_reservation.protected_profit_giveback_reserved == 0
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
            database,
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
