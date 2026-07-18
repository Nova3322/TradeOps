from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import func, select

from trading_control_plane.database import Database
from trading_control_plane.domain import (
    CapabilityStatus,
    Direction,
    DomainRejected,
    IdempotencyConflict,
    IntentKind,
    OrderIntentStatus,
    ProposalSource,
    ProposalStatus,
    ReconciliationStatus,
    ReservationStatus,
    ReviewDecision,
    RiskEvaluationInput,
    RiskTier,
    Role,
    SystemRiskState,
    TargetCandidate,
    TargetUrgency,
)
from trading_control_plane.models import (
    Campaign,
    CapabilityGate,
    CommandReceipt,
    OrderIntent,
    Proposal,
    ReconciliationRun,
    RiskReservation,
    VenueOrder,
)
from trading_control_plane.service import TradingService

NOW = datetime(2026, 7, 18, 12, tzinfo=UTC)


def seed(service: TradingService) -> dict[str, UUID]:
    admin = service.bootstrap_admin("admin", now=NOW)
    proposer = service.create_user("proposer", admin, now=NOW)
    reviewer_one = service.create_user("reviewer-1", admin, now=NOW)
    reviewer_two = service.create_user("reviewer-2", admin, now=NOW)
    operator = service.create_user("operator", admin, now=NOW)
    observer = service.create_user("observer", admin, now=NOW)
    service.assign_role(proposer, Role.PROPOSER, admin, "acct-1", "BINANCE", now=NOW)
    service.assign_role(reviewer_one, Role.REVIEWER, admin, "acct-1", "BINANCE", now=NOW)
    service.assign_role(reviewer_two, Role.REVIEWER, admin, "acct-1", "BINANCE", now=NOW)
    service.assign_role(operator, Role.OPERATOR, admin, "acct-1", "BINANCE", now=NOW)
    service.assign_role(observer, Role.OBSERVER, admin, "acct-1", "BINANCE", now=NOW)
    instrument = service.register_instrument(
        actor_id=admin,
        venue="BINANCE",
        symbol="BTCUSDT",
        tick_size=Decimal("0.1"),
        lot_size=Decimal("0.001"),
        minimum_notional=Decimal("5"),
        contract_multiplier=Decimal("1"),
        quote_currency="USDT",
        collateral_currency="USDT",
        protection_supported=True,
        now=NOW,
    )
    service.set_risk_policy(
        actor_id=admin,
        version="risk-v1",
        system_state=SystemRiskState.NORMAL,
        max_total_risk=Decimal("100"),
        max_fact_age=timedelta(seconds=30),
        now=NOW,
    )
    return {
        "admin": admin,
        "proposer": proposer,
        "reviewer_one": reviewer_one,
        "reviewer_two": reviewer_two,
        "operator": operator,
        "observer": observer,
        "instrument": instrument,
    }


def create_approved_proposal(
    service: TradingService,
    ids: dict[str, UUID],
    *,
    risk_tier: RiskTier = RiskTier.HIGH,
    key: str = "proposal-1",
) -> UUID:
    proposal_id = service.create_proposal(
        actor_id=ids["proposer"],
        source=ProposalSource.MANUAL,
        risk_tier=risk_tier,
        account_id="acct-1",
        venue="BINANCE",
        instrument_id=ids["instrument"],
        direction=Direction.LONG,
        quantity=Decimal("1"),
        max_risk=Decimal("40"),
        expires_at=NOW + timedelta(hours=2),
        idempotency_key=key,
        now=NOW,
    )
    service.submit_proposal(proposal_id, ids["proposer"], now=NOW)
    service.review_proposal(
        proposal_id,
        ids["reviewer_one"],
        ReviewDecision.APPROVE,
        "reviewed",
        now=NOW,
    )
    if risk_tier is RiskTier.HIGH:
        service.review_proposal(
            proposal_id,
            ids["reviewer_two"],
            ReviewDecision.APPROVE,
            "second review",
            now=NOW,
        )
    return proposal_id


def issue_authorization(
    service: TradingService,
    ids: dict[str, UUID],
    proposal_id: UUID,
    *,
    expires_at: datetime | None = None,
) -> UUID:
    service.decide_risk(
        proposal_id=proposal_id,
        actor_id=ids["operator"],
        inputs=RiskEvaluationInput(
            kind=IntentKind.INITIAL,
            requested_quantity=Decimal("1"),
            requested_risk=Decimal("40"),
            current_risk=Decimal("10"),
            fact_age=timedelta(seconds=1),
            position_known=True,
            protection_known=True,
        ),
        idempotency_key=f"risk:{proposal_id}",
        now=NOW,
    )
    return service.issue_authorization(
        proposal_id=proposal_id,
        actor_id=ids["operator"],
        expires_at=expires_at or NOW + timedelta(minutes=30),
        allowed_adds=1,
        idempotency_key=f"authorization:{proposal_id}",
        now=NOW,
    )


def test_proposal_idempotency_and_semantic_conflict(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)
    proposal_id = create_approved_proposal(service, ids, risk_tier=RiskTier.LOW)
    duplicate = service.create_proposal(
        actor_id=ids["proposer"],
        source=ProposalSource.MANUAL,
        risk_tier=RiskTier.LOW,
        account_id="acct-1",
        venue="BINANCE",
        instrument_id=ids["instrument"],
        direction=Direction.LONG,
        quantity=Decimal("1"),
        max_risk=Decimal("40"),
        expires_at=NOW + timedelta(hours=2),
        idempotency_key="proposal-1",
        now=NOW,
    )
    with pytest.raises(IdempotencyConflict):
        service.create_proposal(
            actor_id=ids["proposer"],
            source=ProposalSource.MANUAL,
            risk_tier=RiskTier.LOW,
            account_id="acct-1",
            venue="BINANCE",
            instrument_id=ids["instrument"],
            direction=Direction.LONG,
            quantity=Decimal("2"),
            max_risk=Decimal("40"),
            expires_at=NOW + timedelta(hours=2),
            idempotency_key="proposal-1",
            now=NOW,
        )

    with database.session_factory() as session:
        assert duplicate == proposal_id
        assert session.scalar(select(func.count()).select_from(Proposal)) == 1
        assert session.scalar(select(func.count()).select_from(CommandReceipt)) == 1


def test_self_review_is_forbidden_and_high_risk_needs_two_reviewers(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)
    proposal_id = service.create_proposal(
        actor_id=ids["proposer"],
        source=ProposalSource.MANUAL,
        risk_tier=RiskTier.HIGH,
        account_id="acct-1",
        venue="BINANCE",
        instrument_id=ids["instrument"],
        direction=Direction.LONG,
        quantity=Decimal("1"),
        max_risk=Decimal("40"),
        expires_at=NOW + timedelta(hours=1),
        idempotency_key="review-proposal",
        now=NOW,
    )
    service.submit_proposal(proposal_id, ids["proposer"], now=NOW)

    with pytest.raises(DomainRejected, match="SELF_REVIEW_FORBIDDEN"):
        service.review_proposal(
            proposal_id,
            ids["proposer"],
            ReviewDecision.APPROVE,
            "self",
            now=NOW,
        )
    first = service.review_proposal(
        proposal_id,
        ids["reviewer_one"],
        ReviewDecision.APPROVE,
        "first",
        now=NOW,
    )
    second = service.review_proposal(
        proposal_id,
        ids["reviewer_two"],
        ReviewDecision.APPROVE,
        "second",
        now=NOW,
    )

    assert first is ProposalStatus.PENDING_REVIEW
    assert second is ProposalStatus.APPROVED
    with database.session_factory() as session:
        assert session.get(Proposal, proposal_id).status == ProposalStatus.APPROVED.value


def test_proposal_rejection_and_expiry_are_durable_terminal_states(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)

    rejected = service.create_proposal(
        actor_id=ids["proposer"],
        source=ProposalSource.MANUAL,
        risk_tier=RiskTier.LOW,
        account_id="acct-1",
        venue="BINANCE",
        instrument_id=ids["instrument"],
        direction=Direction.LONG,
        quantity=Decimal("1"),
        max_risk=Decimal("10"),
        expires_at=NOW + timedelta(hours=1),
        idempotency_key="rejected-proposal",
        now=NOW,
    )
    service.submit_proposal(rejected, ids["proposer"], now=NOW)
    assert (
        service.review_proposal(
            rejected,
            ids["reviewer_one"],
            ReviewDecision.REJECT,
            "risk is not justified",
            now=NOW,
        )
        is ProposalStatus.REJECTED
    )

    expired_draft = service.create_proposal(
        actor_id=ids["proposer"],
        source=ProposalSource.SYSTEM,
        risk_tier=RiskTier.LOW,
        account_id="acct-1",
        venue="BINANCE",
        instrument_id=ids["instrument"],
        direction=Direction.LONG,
        quantity=Decimal("1"),
        max_risk=Decimal("10"),
        expires_at=NOW + timedelta(minutes=1),
        idempotency_key="expired-draft",
        now=NOW,
    )
    with pytest.raises(DomainRejected, match="PROPOSAL_EXPIRED"):
        service.submit_proposal(expired_draft, ids["proposer"], now=NOW + timedelta(minutes=2))

    expired_review = service.create_proposal(
        actor_id=ids["proposer"],
        source=ProposalSource.MANUAL,
        risk_tier=RiskTier.LOW,
        account_id="acct-1",
        venue="BINANCE",
        instrument_id=ids["instrument"],
        direction=Direction.LONG,
        quantity=Decimal("1"),
        max_risk=Decimal("10"),
        expires_at=NOW + timedelta(minutes=1),
        idempotency_key="expired-review",
        now=NOW,
    )
    service.submit_proposal(expired_review, ids["proposer"], now=NOW)
    with pytest.raises(DomainRejected, match="PROPOSAL_EXPIRED"):
        service.review_proposal(
            expired_review,
            ids["reviewer_one"],
            ReviewDecision.APPROVE,
            "too late",
            now=NOW + timedelta(minutes=2),
        )

    with database.session_factory() as session:
        assert session.get(Proposal, rejected).status == ProposalStatus.REJECTED.value
        assert session.get(Proposal, expired_draft).status == ProposalStatus.EXPIRED.value
        assert session.get(Proposal, expired_review).status == ProposalStatus.EXPIRED.value


def test_authorization_rejects_expiry_scope_and_quantity(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)
    proposal_id = create_approved_proposal(service, ids)
    authorization_id = issue_authorization(service, ids, proposal_id)

    with pytest.raises(DomainRejected, match="AUTHORIZATION_SCOPE_MISMATCH"):
        service.create_order_intent(
            authorization_id,
            ids["operator"],
            IntentKind.INITIAL,
            "wrong-account",
            "BINANCE",
            ids["instrument"],
            Direction.LONG,
            Decimal("1"),
            "scope-mismatch",
            now=NOW,
        )
    with pytest.raises(DomainRejected, match="AUTHORIZATION_QUANTITY_EXCEEDED"):
        service.create_order_intent(
            authorization_id,
            ids["operator"],
            IntentKind.INITIAL,
            "acct-1",
            "BINANCE",
            ids["instrument"],
            Direction.LONG,
            Decimal("2"),
            "quantity-exceeded",
            now=NOW,
        )
    with pytest.raises(DomainRejected, match="AUTHORIZATION_EXPIRED"):
        service.create_order_intent(
            authorization_id,
            ids["operator"],
            IntentKind.INITIAL,
            "acct-1",
            "BINANCE",
            ids["instrument"],
            Direction.LONG,
            Decimal("1"),
            "expired",
            now=NOW + timedelta(hours=1),
        )

    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(RiskReservation)) == 0
        assert session.scalar(select(func.count()).select_from(OrderIntent)) == 0


def test_order_reservation_intent_and_receipt_are_atomic_and_idempotent(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)
    proposal_id = create_approved_proposal(service, ids)
    authorization_id = issue_authorization(service, ids, proposal_id)
    created = service.create_order_intent(
        authorization_id,
        ids["operator"],
        IntentKind.INITIAL,
        "acct-1",
        "BINANCE",
        ids["instrument"],
        Direction.LONG,
        Decimal("1"),
        "initial-intent",
        now=NOW,
    )
    duplicate = service.create_order_intent(
        authorization_id,
        ids["operator"],
        IntentKind.INITIAL,
        "acct-1",
        "BINANCE",
        ids["instrument"],
        Direction.LONG,
        Decimal("1"),
        "initial-intent",
        now=NOW,
    )
    with pytest.raises(IdempotencyConflict):
        service.create_order_intent(
            authorization_id,
            ids["operator"],
            IntentKind.INITIAL,
            "acct-1",
            "BINANCE",
            ids["instrument"],
            Direction.LONG,
            Decimal("0.5"),
            "initial-intent",
            now=NOW,
        )

    assert duplicate == created
    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Campaign)) == 1
        assert session.scalar(select(func.count()).select_from(RiskReservation)) == 1
        assert session.scalar(select(func.count()).select_from(OrderIntent)) == 1


def test_unknown_intent_keeps_risk_and_cannot_be_recreated(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)
    proposal_id = create_approved_proposal(service, ids)
    authorization_id = issue_authorization(service, ids, proposal_id)
    created = service.create_order_intent(
        authorization_id,
        ids["operator"],
        IntentKind.INITIAL,
        "acct-1",
        "BINANCE",
        ids["instrument"],
        Direction.LONG,
        Decimal("1"),
        "unknown-intent",
        now=NOW,
    )
    service.mark_intent_unknown(created.intent_id, ids["operator"], "venue timeout", now=NOW)
    duplicate = service.create_order_intent(
        authorization_id,
        ids["operator"],
        IntentKind.INITIAL,
        "acct-1",
        "BINANCE",
        ids["instrument"],
        Direction.LONG,
        Decimal("1"),
        "unknown-intent",
        now=NOW,
    )

    assert duplicate == created
    with database.session_factory() as session:
        assert session.get(OrderIntent, created.intent_id).status == OrderIntentStatus.UNKNOWN.value
        assert (
            session.get(RiskReservation, created.reservation_id).status
            == ReservationStatus.UNKNOWN.value
        )


def test_cancelled_unfilled_intent_releases_risk_and_builds_one_reduce_only_intent(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)
    proposal_id = create_approved_proposal(service, ids)
    authorization_id = issue_authorization(service, ids, proposal_id)
    opening = service.create_order_intent(
        authorization_id,
        ids["operator"],
        IntentKind.INITIAL,
        "acct-1",
        "BINANCE",
        ids["instrument"],
        Direction.LONG,
        Decimal("1"),
        "opening-to-release",
        now=NOW,
    )
    service.release_unfilled_intent(
        opening.intent_id,
        ids["operator"],
        OrderIntentStatus.CANCELLED,
        "shadow order was never sent",
        now=NOW,
    )
    service.record_position(
        "acct-1",
        "BINANCE",
        ids["instrument"],
        Decimal("1"),
        Decimal("100"),
        Decimal("100"),
        True,
        ids["operator"],
        now=NOW,
    )
    service.update_campaign_target(
        opening.campaign_id,
        ids["operator"],
        (TargetCandidate(Decimal("0.4"), TargetUrgency.URGENT, "risk"),),
        now=NOW,
    )

    reduction = service.create_reduction_intent(
        opening.campaign_id, ids["operator"], "reduce-after-release", now=NOW
    )
    duplicate = service.create_reduction_intent(
        opening.campaign_id, ids["operator"], "reduce-after-release", now=NOW
    )

    assert duplicate == reduction
    with database.session_factory() as session:
        reservation = session.get(RiskReservation, opening.reservation_id)
        intent = session.get(OrderIntent, reduction)
        assert reservation is not None
        assert reservation.status == ReservationStatus.RELEASED.value
        assert intent is not None
        assert intent.kind == IntentKind.REDUCE.value
        assert intent.reduce_only is True
        assert intent.quantity == Decimal("0.600000000000000000")


def test_basic_rbac_bootstrap_and_capability_gate_are_fail_closed(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)

    assert service.can_user(ids["observer"], "view", "acct-1", "BINANCE") is True
    assert service.can_user(ids["observer"], "order.prepare", "acct-1", "BINANCE") is False
    assert service.can_user(UUID(int=0), "view", "acct-1", "BINANCE") is False
    with pytest.raises(DomainRejected, match="BOOTSTRAP_CLOSED"):
        service.bootstrap_admin("second-admin", now=NOW)

    service.set_capability_gate(
        "AUTO_ADD",
        CapabilityStatus.ENABLED,
        "explicit test operator decision",
        ids["admin"],
        now=NOW,
    )
    with database.session_factory() as session:
        gate = session.get(CapabilityGate, "AUTO_ADD")
        assert gate is not None
        assert gate.status == CapabilityStatus.ENABLED.value
        assert gate.operator_id == str(ids["admin"])


def test_sender_fencing_rejects_old_owner_after_reconciled_takeover(
    service: TradingService,
) -> None:
    ids = seed(service)
    first = service.acquire_sender("acct-1:BINANCE", "worker-a", ids["operator"], NOW)
    service.record_scope_reconciliation(
        "acct-1:BINANCE", ids["operator"], ReconciliationStatus.MATCH, (), now=NOW
    )
    second = service.acquire_sender(
        "acct-1:BINANCE", "worker-b", ids["operator"], NOW + timedelta(minutes=2)
    )

    assert second > first
    with pytest.raises(DomainRejected, match="FENCING_TOKEN_REJECTED"):
        service.validate_sender("acct-1:BINANCE", "worker-a", first, NOW + timedelta(minutes=2))
    service.validate_sender("acct-1:BINANCE", "worker-b", second, NOW + timedelta(minutes=2))


def test_active_intent_blocks_duplicate_reduce_only_intent(service: TradingService) -> None:
    ids = seed(service)
    proposal_id = create_approved_proposal(service, ids)
    authorization_id = issue_authorization(service, ids, proposal_id)
    created = service.create_order_intent(
        authorization_id,
        ids["operator"],
        IntentKind.INITIAL,
        "acct-1",
        "BINANCE",
        ids["instrument"],
        Direction.LONG,
        Decimal("1"),
        "opening",
        now=NOW,
    )
    service.record_position(
        "acct-1",
        "BINANCE",
        ids["instrument"],
        Decimal("1"),
        Decimal("100"),
        Decimal("105"),
        True,
        ids["operator"],
        now=NOW,
    )
    service.update_campaign_target(
        created.campaign_id,
        ids["operator"],
        (TargetCandidate(Decimal("0.5"), TargetUrgency.URGENT, "risk"),),
        now=NOW,
    )

    with pytest.raises(DomainRejected, match="ACTIVE_ORDER_INTENT"):
        service.create_reduction_intent(created.campaign_id, ids["operator"], "reduce-1", now=NOW)


def test_reconciliation_detects_unknown_difference_manual_and_resolution(
    service: TradingService,
) -> None:
    ids = seed(service)
    unknown = service.record_scope_reconciliation(
        "acct-1:BINANCE",
        ids["operator"],
        ReconciliationStatus.UNKNOWN,
        ("POSITION_UNKNOWN",),
        now=NOW,
    )
    different = service.record_scope_reconciliation(
        "acct-1:BINANCE",
        ids["operator"],
        ReconciliationStatus.DIFFERENCE,
        ("ORDER_QUANTITY_MISMATCH",),
        now=NOW,
    )
    manual = service.require_manual_reconciliation(
        different, ids["operator"], "operator review", now=NOW
    )
    resolved = service.resolve_reconciliation(
        manual, ids["operator"], "exchange facts confirmed", now=NOW
    )

    assert service.reconciliation_status(unknown) is ReconciliationStatus.UNKNOWN
    assert service.reconciliation_status(different) is ReconciliationStatus.RESOLVED
    assert resolved == different


def test_campaign_reconciliation_compares_orders_fills_positions_and_protection(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)
    proposal_id = create_approved_proposal(service, ids)
    authorization_id = issue_authorization(service, ids, proposal_id)
    created = service.create_order_intent(
        authorization_id,
        ids["operator"],
        IntentKind.INITIAL,
        "acct-1",
        "BINANCE",
        ids["instrument"],
        Direction.LONG,
        Decimal("1"),
        "reconciliation-intent",
        now=NOW,
    )
    token = service.acquire_sender("acct-1:BINANCE", "shadow-worker", ids["operator"], NOW)
    service.record_shadow_order(
        created.intent_id,
        ids["operator"],
        "acct-1:BINANCE",
        "shadow-worker",
        token,
        "shadow-order-reconcile",
        now=NOW,
    )
    service.record_fill(
        created.intent_id,
        ids["operator"],
        "shadow-fill-reconcile",
        "BUY",
        Decimal("0.5"),
        Decimal("100"),
        Decimal("0"),
        "USDT",
        Decimal("0"),
        now=NOW,
    )
    position_id = service.record_position(
        "acct-1",
        "BINANCE",
        ids["instrument"],
        Decimal("0.25"),
        Decimal("100"),
        Decimal("101"),
        True,
        ids["operator"],
        now=NOW,
    )
    service.record_protection(
        position_id,
        "insufficient-stop",
        Decimal("0.1"),
        Decimal("90"),
        False,
        ids["operator"],
        now=NOW,
    )
    service.record_account_equity(
        "acct-1",
        "BINANCE",
        Decimal("10000"),
        Decimal("9000"),
        "USDT",
        True,
        ids["operator"],
        now=NOW,
    )
    with database.session_factory.begin() as session:
        order = session.scalar(
            select(VenueOrder).where(VenueOrder.order_intent_id == created.intent_id)
        )
        assert order is not None
        order.filled_quantity = Decimal("0")

    run_id = service.reconcile_campaign(
        created.campaign_id, "acct-1:BINANCE", ids["operator"], now=NOW
    )

    with database.session_factory() as session:
        run = session.get(ReconciliationRun, run_id)
        assert run is not None
        assert run.status == ReconciliationStatus.DIFFERENCE.value
        assert any(value.startswith("ORDER_FILL_MISMATCH") for value in run.differences)
        assert "POSITION_QUANTITY_MISMATCH" in run.differences
        assert "PROTECTION_INSUFFICIENT" in run.differences


def test_capability_gates_are_default_disabled(database: Database) -> None:
    with database.session_factory() as session:
        gates = session.scalars(
            select(CapabilityGate).order_by(CapabilityGate.capability_key)
        ).all()

    assert [(gate.capability_key, gate.status) for gate in gates] == [
        ("AUTO_ADD", CapabilityStatus.DISABLED.value),
        ("CAPITAL_TRANSFER", CapabilityStatus.DISABLED.value),
        ("LIVE_ORDER_SEND", CapabilityStatus.DISABLED.value),
    ]


def test_shadow_end_to_end_flow_reconciles_and_computes_pnl(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)
    proposal_id = create_approved_proposal(service, ids)
    authorization_id = issue_authorization(service, ids, proposal_id)
    created = service.create_order_intent(
        authorization_id,
        ids["operator"],
        IntentKind.INITIAL,
        "acct-1",
        "BINANCE",
        ids["instrument"],
        Direction.LONG,
        Decimal("1"),
        "e2e-intent",
        now=NOW,
    )
    token = service.acquire_sender("acct-1:BINANCE", "shadow-worker", ids["operator"], NOW)
    service.record_shadow_order(
        created.intent_id,
        ids["operator"],
        "acct-1:BINANCE",
        "shadow-worker",
        token,
        "shadow-order-1",
        now=NOW,
    )
    service.record_fill(
        created.intent_id,
        ids["operator"],
        "shadow-fill-1",
        "BUY",
        Decimal("1"),
        Decimal("100"),
        Decimal("1"),
        "USDT",
        Decimal("0.5"),
        now=NOW,
    )
    position_id = service.record_position(
        "acct-1",
        "BINANCE",
        ids["instrument"],
        Decimal("1"),
        Decimal("100"),
        Decimal("110"),
        True,
        ids["operator"],
        now=NOW,
    )
    service.record_protection(
        position_id,
        "shadow-stop-1",
        Decimal("1"),
        Decimal("90"),
        True,
        ids["operator"],
        now=NOW,
    )
    service.record_account_equity(
        "acct-1",
        "BINANCE",
        Decimal("10000"),
        Decimal("9000"),
        "USDT",
        True,
        ids["operator"],
        now=NOW,
    )
    service.record_funding(
        created.campaign_id,
        "BINANCE",
        "funding-1",
        Decimal("-0.2"),
        "USDT",
        ids["operator"],
        now=NOW,
    )
    run_id = service.reconcile_campaign(
        created.campaign_id, "acct-1:BINANCE", ids["operator"], now=NOW
    )
    pnl = service.refresh_campaign_pnl(created.campaign_id, ids["operator"], now=NOW)

    assert service.reconciliation_status(run_id) is ReconciliationStatus.MATCH
    assert pnl.realized_pnl == Decimal("-1.700000000000000000")
    assert pnl.unrealized_pnl == Decimal("10.000000000000000000")
    assert pnl.total_pnl == Decimal("8.300000000000000000")
    with database.session_factory() as session:
        campaign = session.get(Campaign, created.campaign_id)
        assert campaign.final_pnl == Decimal("8.300000000000000000")
