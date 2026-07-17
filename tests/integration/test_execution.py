from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from tests.integration.test_trading_authorization import (
    execute_issue,
    issue_envelope,
    prepare_approved,
)
from tests.risk_fixtures import make_capital, make_policy, make_request, make_requested
from trading_control_plane.command_executor import IdempotentCommandExecutor
from trading_control_plane.commands import CommandChannel, CommandEnvelope, CommandStatus, hash_json
from trading_control_plane.database import Database
from trading_control_plane.execution import (
    EXECUTION_INTENT_SERVICE_PRINCIPAL,
    EXECUTION_RECONCILIATION_SERVICE_PRINCIPAL,
    ExecutionIntentService,
    ExecutionReconciliationService,
    RecordExecutionFactRequest,
)
from trading_control_plane.execution_models import (
    ExecutionFact,
    ExecutionRiskDecision,
    OrderIntent,
    OrderIntentStateHistory,
    RiskExposureState,
    RiskExposureStateHistory,
    RiskLedgerEntry,
    RiskReservation,
)
from trading_control_plane.models import AuditEvent, OutboxMessage
from trading_control_plane.proposal_models import FrozenProposalVersion
from trading_control_plane.risk import (
    CertificationBinding,
    MarketRiskInput,
    PositionDirection,
    RiskPrecheckRequest,
    ScopeLimit,
    ScopeRiskInput,
    ScopeType,
    TradeLossComponents,
)
from trading_control_plane.risk_models import RiskPolicyRecord
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
            "binding": CertificationBinding(
                strategy_id="trend-breakout",
                strategy_version="1.0.0",
                strategy_parameter_version="params-v1",
                authorization_policy_version="authorization-policy-v1",
                instrument_identity="BINANCE:BTCUSDT-PERP",
                venue="BINANCE",
                execution_domain="BINANCE_USDM",
                account_id="account-1",
                account_abstraction="UNIFIED",
                margin_mode="ISOLATED",
                collateral_scope="ACCOUNT",
                collateral_pool_id="pool-usdt-1",
                adapter_version="binance-adapter-v1",
                freqtrade_worker_version="freqtrade-worker-v1",
                account_capability_version="account-capability-v1",
                catalog_version="catalog-v1",
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
            "risk_currency": "USDT",
            "valuation_price_source_ref": "test-only:mark-price-snapshot",
            "risk_request": request.model_dump(mode="json"),
            "add_eligibility": None,
        },
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
            "risk_currency": "USDT",
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


def execute_create(database: Database, envelope: CommandEnvelope):
    return IdempotentCommandExecutor(database.session_factory).execute(
        envelope, ExecutionIntentService().create
    )


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
) -> RecordExecutionFactRequest:
    received = datetime.now(UTC)
    payload = {
        "sequence": sequence,
        "status": status,
        "filled": str(filled),
        "remaining": str(remaining),
    }
    values: dict[str, Any] = {
        "fact_sequence": sequence,
        "target_status": status,
        "venue": "BINANCE",
        "execution_domain": "BINANCE_USDM",
        "account_id": "account-1",
        "external_fact_id": external_fact_id or f"venue-fact-{uuid4()}",
        "cumulative_filled_quantity": filled,
        "known_remaining_quantity": remaining,
        "zero_fill_confirmed": zero,
        "venue_order_terminal": terminal,
        "position_reconciled": reconciled,
        "protection_confirmed": protected,
        "reconciliation_run_ref": "test-only:reconciliation-run",
        "source_ref": "test-only:venue-private-order-fact",
        "source_version": "venue-adapter-test-v1",
        "payload": payload,
        "payload_hash": hash_json(payload),
        "evidence_ref": f"test-only:evidence:{sequence}:{status}",
        "event_time": received - timedelta(milliseconds=10),
        "received_at": received,
    }
    provisional = RecordExecutionFactRequest.model_construct(**values, evidence_hash="0" * 64)
    values["evidence_hash"] = hash_json(
        provisional.model_dump(mode="json", exclude={"evidence_hash"})
    )
    return RecordExecutionFactRequest.model_validate(values)


def fact_envelope(order_intent_id: UUID, request: RecordExecutionFactRequest) -> CommandEnvelope:
    now = datetime.now(UTC)
    return CommandEnvelope(
        idempotency_key=f"execution-fact-{uuid4()}",
        command_type="execution.fact.record.v1",
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


def execute_fact(database: Database, order_intent_id: UUID, request: RecordExecutionFactRequest):
    return IdempotentCommandExecutor(database.session_factory).execute(
        fact_envelope(order_intent_id, request), ExecutionReconciliationService().record
    )


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
        assert session.execute(select(ExecutionRiskDecision.result)).scalar_one() == "DENY"
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
