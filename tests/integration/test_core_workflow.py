from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import func, select

from trading_control_plane.database import Database
from trading_control_plane.domain import (
    AddCandidateFacts,
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
    RiskDecision,
    RiskReservation,
    TradingAuthorization,
    VenueFill,
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
    strategy = service.create_service_principal("strategy-v1", admin, now=NOW)
    service.assign_role(proposer, Role.PROPOSER, admin, "acct-1", "BINANCE", now=NOW)
    service.assign_role(reviewer_one, Role.REVIEWER, admin, "acct-1", "BINANCE", now=NOW)
    service.assign_role(reviewer_two, Role.REVIEWER, admin, "acct-1", "BINANCE", now=NOW)
    service.assign_role(operator, Role.OPERATOR, admin, "acct-1", "BINANCE", now=NOW)
    service.assign_role(observer, Role.OBSERVER, admin, "acct-1", "BINANCE", now=NOW)
    service.assign_role(strategy, Role.PROPOSER, admin, "acct-1", "BINANCE", now=NOW)
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
    position = service.record_position(
        "acct-1",
        "BINANCE",
        instrument,
        Decimal("0"),
        Decimal("0"),
        Decimal("100"),
        True,
        operator,
        now=NOW,
    )
    service.record_account_equity(
        "acct-1",
        "BINANCE",
        Decimal("10000"),
        Decimal("9000"),
        "USDT",
        True,
        operator,
        now=NOW,
    )
    return {
        "admin": admin,
        "proposer": proposer,
        "reviewer_one": reviewer_one,
        "reviewer_two": reviewer_two,
        "operator": operator,
        "observer": observer,
        "strategy": strategy,
        "instrument": instrument,
        "position": position,
    }


def create_approved_proposal(
    service: TradingService,
    ids: dict[str, UUID],
    *,
    risk_tier: RiskTier = RiskTier.HIGH,
    key: str = "proposal-1",
    instrument_id: UUID | None = None,
    quantity: Decimal = Decimal("1"),
    max_risk: Decimal = Decimal("40"),
    actor_id: UUID | None = None,
    source: ProposalSource = ProposalSource.MANUAL,
    strategy_id: str | None = None,
    strategy_version: str | None = None,
    allow_auto_add: bool = False,
) -> UUID:
    proposer_id = ids["proposer"] if actor_id is None else actor_id
    proposal_id = service.create_proposal(
        actor_id=proposer_id,
        source=source,
        risk_tier=risk_tier,
        account_id="acct-1",
        venue="BINANCE",
        instrument_id=ids["instrument"] if instrument_id is None else instrument_id,
        direction=Direction.LONG,
        quantity=quantity,
        max_risk=max_risk,
        expires_at=NOW + timedelta(hours=2),
        idempotency_key=key,
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        source_candidate_id="pt_test_candidate" if source is ProposalSource.SYSTEM else None,
        source_observed_at=NOW if source is ProposalSource.SYSTEM else None,
        source_readiness="READY" if source is ProposalSource.SYSTEM else None,
        details=(
            {
                "allow_auto_add": True,
                "requested_adds": 1,
                "add_trigger_price": "105",
                "initial_quantity": "0.5",
                "invalidation_price": "90",
            }
            if allow_auto_add
            else None
        ),
        now=NOW,
    )
    service.submit_proposal(proposal_id, proposer_id, now=NOW)
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


def current_add_candidate() -> AddCandidateFacts:
    return AddCandidateFacts(
        candidate_id="pt_test_add_candidate",
        contract_version="breakouts-v1",
        venue="BINANCE",
        symbol="BTCUSDT",
        direction=Direction.LONG,
        observed_at=NOW,
        reference_price=Decimal("110"),
        readiness="READY",
    )


def issue_authorization(
    service: TradingService,
    ids: dict[str, UUID],
    proposal_id: UUID,
    *,
    expires_at: datetime | None = None,
    requested_quantity: Decimal | None = None,
    allowed_adds: int = 0,
) -> UUID:
    service.decide_risk(
        proposal_id=proposal_id,
        actor_id=ids["operator"],
        kind=IntentKind.INITIAL,
        idempotency_key=f"risk:{proposal_id}",
        now=NOW,
        requested_quantity=requested_quantity,
    )
    return service.issue_authorization(
        proposal_id=proposal_id,
        actor_id=ids["operator"],
        expires_at=expires_at or NOW + timedelta(minutes=30),
        allowed_adds=allowed_adds,
        idempotency_key=f"authorization:{proposal_id}",
        now=NOW,
    )


def register_flat_instrument(
    service: TradingService,
    ids: dict[str, UUID],
    symbol: str,
) -> UUID:
    instrument_id = service.register_instrument(
        actor_id=ids["admin"],
        venue="BINANCE",
        symbol=symbol,
        tick_size=Decimal("0.01"),
        lot_size=Decimal("0.001"),
        minimum_notional=Decimal("5"),
        contract_multiplier=Decimal("1"),
        quote_currency="USDT",
        collateral_currency="USDT",
        protection_supported=True,
        now=NOW,
    )
    service.record_position(
        "acct-1",
        "BINANCE",
        instrument_id,
        Decimal("0"),
        Decimal("0"),
        Decimal("100"),
        True,
        ids["operator"],
        now=NOW,
    )
    return instrument_id


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
        source=ProposalSource.MANUAL,
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
    token = service.acquire_sender("acct-1:BINANCE", "unknown-worker", ids["operator"], NOW)
    service.record_shadow_order(
        created.intent_id,
        ids["operator"],
        "acct-1:BINANCE",
        "unknown-worker",
        token,
        "unknown-shadow-order",
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
        assert (
            session.scalar(
                select(VenueOrder).where(VenueOrder.order_intent_id == created.intent_id)
            ).status
            == "UNKNOWN"
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
    takeover_time = NOW + timedelta(minutes=2)
    service.record_position(
        "acct-1",
        "BINANCE",
        ids["instrument"],
        Decimal("0"),
        Decimal("0"),
        Decimal("100"),
        True,
        ids["operator"],
        now=takeover_time,
    )
    service.record_account_equity(
        "acct-1",
        "BINANCE",
        Decimal("10000"),
        Decimal("9000"),
        "USDT",
        True,
        ids["operator"],
        now=takeover_time,
    )
    service.reconcile_scope("acct-1:BINANCE", ids["operator"], now=takeover_time)
    second = service.acquire_sender("acct-1:BINANCE", "worker-b", ids["operator"], takeover_time)

    assert second > first
    with pytest.raises(DomainRejected, match="FENCING_TOKEN_REJECTED"):
        service.validate_sender("acct-1:BINANCE", "worker-a", first, takeover_time)
    service.validate_sender("acct-1:BINANCE", "worker-b", second, takeover_time)


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
        assert any(value.startswith("POSITION_QUANTITY_MISMATCH") for value in run.differences)
        assert any(value.startswith("PROTECTION_INSUFFICIENT") for value in run.differences)


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


def test_risk_decision_uses_server_facts_and_enforces_proposal_caps(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)
    proposal_id = create_approved_proposal(
        service,
        ids,
        risk_tier=RiskTier.LOW,
        key="server-risk",
        quantity=Decimal("2"),
        max_risk=Decimal("80"),
    )

    decision_id = service.decide_risk(
        proposal_id=proposal_id,
        actor_id=ids["operator"],
        kind=IntentKind.INITIAL,
        requested_quantity=Decimal("0.5"),
        idempotency_key="server-risk-decision",
        now=NOW,
    )
    with pytest.raises(DomainRejected, match="PROPOSAL_QUANTITY_EXCEEDED"):
        service.decide_risk(
            proposal_id=proposal_id,
            actor_id=ids["operator"],
            kind=IntentKind.INITIAL,
            requested_quantity=Decimal("2.1"),
            idempotency_key="server-risk-over-cap",
            now=NOW,
        )

    with database.session_factory() as session:
        decision = session.get(RiskDecision, decision_id)
        assert decision is not None
        assert decision.approved_quantity == Decimal("0.500000000000000000")
        assert decision.risk_amount == Decimal("20.000000000000000000")
        assert Decimal(decision.input_data["current_risk"]) == 0
        assert decision.input_data["position"]["position_id"] == str(ids["position"])
        assert decision.input_data["data_as_of"] == NOW.isoformat()


def test_risk_decision_rejects_persisted_stale_and_unknown_facts(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)
    proposal_id = create_approved_proposal(service, ids, risk_tier=RiskTier.LOW, key="stale-risk")
    stale_time = NOW + timedelta(seconds=31)

    stale_decision_id = service.decide_risk(
        proposal_id=proposal_id,
        actor_id=ids["operator"],
        kind=IntentKind.INITIAL,
        idempotency_key="stale-risk-decision",
        now=stale_time,
    )
    service.record_position(
        "acct-1",
        "BINANCE",
        ids["instrument"],
        Decimal("0"),
        Decimal("0"),
        Decimal("100"),
        False,
        ids["operator"],
        now=stale_time,
    )
    service.record_account_equity(
        "acct-1",
        "BINANCE",
        Decimal("10000"),
        Decimal("9000"),
        "USDT",
        True,
        ids["operator"],
        now=stale_time,
    )
    unknown_decision_id = service.decide_risk(
        proposal_id=proposal_id,
        actor_id=ids["operator"],
        kind=IntentKind.INITIAL,
        idempotency_key="unknown-risk-decision",
        now=stale_time,
    )

    with database.session_factory() as session:
        stale = session.get(RiskDecision, stale_decision_id)
        unknown = session.get(RiskDecision, unknown_decision_id)
        assert stale is not None and stale.reasons == ["STALE_FACTS"]
        assert unknown is not None and unknown.reasons == ["POSITION_UNKNOWN"]
        assert unknown.input_data["position"]["fact_status"] == "UNKNOWN"


def test_concurrent_intents_cannot_oversubscribe_global_risk_capacity(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)
    second_instrument = register_flat_instrument(service, ids, "ETHUSDT")
    first_proposal = create_approved_proposal(
        service,
        ids,
        risk_tier=RiskTier.LOW,
        key="capacity-p1",
        max_risk=Decimal("60"),
    )
    second_proposal = create_approved_proposal(
        service,
        ids,
        risk_tier=RiskTier.LOW,
        key="capacity-p2",
        instrument_id=second_instrument,
        max_risk=Decimal("60"),
    )
    first_auth = issue_authorization(service, ids, first_proposal)
    second_auth = issue_authorization(service, ids, second_proposal)

    def prepare(authorization_id: UUID, instrument_id: UUID, key: str) -> str:
        try:
            service.create_order_intent(
                authorization_id,
                ids["operator"],
                IntentKind.INITIAL,
                "acct-1",
                "BINANCE",
                instrument_id,
                Direction.LONG,
                Decimal("1"),
                key,
                now=NOW,
            )
        except DomainRejected as exc:
            return exc.code
        return "CREATED"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda args: prepare(*args),
                (
                    (first_auth, ids["instrument"], "capacity-i1"),
                    (second_auth, second_instrument, "capacity-i2"),
                ),
            )
        )

    assert sorted(results) == ["CREATED", "FINAL_RISK_CHECK_FAILED"]
    with database.session_factory() as session:
        occupied = session.scalar(
            select(func.sum(RiskReservation.amount)).where(
                RiskReservation.status.in_(["RESERVED", "OPEN", "UNKNOWN"])
            )
        )
        assert occupied == Decimal("60.000000000000000000")
        assert session.scalar(select(func.count()).select_from(OrderIntent)) == 1


def test_final_risk_check_rejects_policy_and_fact_changes_after_authorization(
    service: TradingService,
) -> None:
    ids = seed(service)
    proposal_id = create_approved_proposal(service, ids, risk_tier=RiskTier.LOW, key="final-check")
    authorization_id = issue_authorization(service, ids, proposal_id)
    service.set_risk_policy(
        actor_id=ids["admin"],
        version="risk-kill",
        system_state=SystemRiskState.KILL_SWITCH,
        max_total_risk=Decimal("100"),
        max_fact_age=timedelta(seconds=30),
        now=NOW + timedelta(seconds=1),
    )

    with pytest.raises(DomainRejected, match="FINAL_RISK_CHECK_FAILED: KILL_SWITCH"):
        service.create_order_intent(
            authorization_id,
            ids["operator"],
            IntentKind.INITIAL,
            "acct-1",
            "BINANCE",
            ids["instrument"],
            Direction.LONG,
            Decimal("1"),
            "kill-after-auth",
            now=NOW + timedelta(seconds=1),
        )

    service.set_risk_policy(
        actor_id=ids["admin"],
        version="risk-normal-2",
        system_state=SystemRiskState.NORMAL,
        max_total_risk=Decimal("100"),
        max_fact_age=timedelta(seconds=30),
        now=NOW + timedelta(seconds=2),
    )
    with pytest.raises(DomainRejected, match="FINAL_RISK_CHECK_FAILED: STALE_FACTS"):
        service.create_order_intent(
            authorization_id,
            ids["operator"],
            IntentKind.INITIAL,
            "acct-1",
            "BINANCE",
            ids["instrument"],
            Direction.LONG,
            Decimal("1"),
            "stale-after-auth",
            now=NOW + timedelta(seconds=31),
        )


def test_unknown_reservation_still_occupies_capacity(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)
    first_proposal = create_approved_proposal(
        service,
        ids,
        risk_tier=RiskTier.LOW,
        key="unknown-capacity-1",
        max_risk=Decimal("80"),
    )
    first_auth = issue_authorization(service, ids, first_proposal)
    first = service.create_order_intent(
        first_auth,
        ids["operator"],
        IntentKind.INITIAL,
        "acct-1",
        "BINANCE",
        ids["instrument"],
        Direction.LONG,
        Decimal("1"),
        "unknown-capacity-intent",
        now=NOW,
    )
    service.mark_intent_unknown(first.intent_id, ids["operator"], "uncertain send", now=NOW)
    second_proposal = create_approved_proposal(
        service,
        ids,
        risk_tier=RiskTier.LOW,
        key="unknown-capacity-2",
        max_risk=Decimal("50"),
    )
    second_decision = service.decide_risk(
        proposal_id=second_proposal,
        actor_id=ids["operator"],
        kind=IntentKind.INITIAL,
        idempotency_key="unknown-capacity-decision",
        now=NOW,
    )

    with database.session_factory() as session:
        reservation = session.get(RiskReservation, first.reservation_id)
        decision = session.get(RiskDecision, second_decision)
        assert reservation is not None and reservation.status == ReservationStatus.UNKNOWN.value
        assert decision is not None and decision.result == "SCALE"
        assert decision.risk_amount == Decimal("20.000000000000000000")
        assert decision.input_data["current_risk"] == "80.000000000000000000"


def test_add_requires_profit_position_protection_and_normal_risk_state(
    service: TradingService,
) -> None:
    ids = seed(service)
    proposal_id = create_approved_proposal(
        service,
        ids,
        risk_tier=RiskTier.LOW,
        key="add-invariants",
        allow_auto_add=True,
    )
    authorization_id = issue_authorization(service, ids, proposal_id, allowed_adds=1)
    opening = service.create_order_intent(
        authorization_id,
        ids["operator"],
        IntentKind.INITIAL,
        "acct-1",
        "BINANCE",
        ids["instrument"],
        Direction.LONG,
        Decimal("0.5"),
        "add-opening",
        now=NOW,
    )
    token = service.acquire_sender("acct-1:BINANCE", "add-worker", ids["operator"], NOW)
    service.record_shadow_order(
        opening.intent_id,
        ids["operator"],
        "acct-1:BINANCE",
        "add-worker",
        token,
        "add-opening-order",
        now=NOW,
    )
    service.record_fill(
        opening.intent_id,
        ids["operator"],
        "add-opening-fill",
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
        Decimal("0.5"),
        Decimal("100"),
        Decimal("90"),
        True,
        ids["operator"],
        now=NOW,
    )
    service.record_protection(
        position_id,
        "add-stop",
        Decimal("0.5"),
        Decimal("80"),
        True,
        ids["operator"],
        now=NOW,
    )
    service.set_capability_gate(
        "AUTO_ADD", CapabilityStatus.ENABLED, "test Add", ids["admin"], now=NOW
    )

    with pytest.raises(DomainRejected, match="ADD_NOT_PROFITABLE"):
        service.create_order_intent(
            authorization_id,
            ids["operator"],
            IntentKind.ADD,
            "acct-1",
            "BINANCE",
            ids["instrument"],
            Direction.LONG,
            Decimal("0.5"),
            "add-loss",
            add_candidate=current_add_candidate(),
            now=NOW,
        )
    service.record_position(
        "acct-1",
        "BINANCE",
        ids["instrument"],
        Decimal("0.5"),
        Decimal("100"),
        Decimal("110"),
        True,
        ids["operator"],
        now=NOW,
    )
    service.record_protection(
        position_id,
        "add-stop-unknown",
        Decimal("0.5"),
        Decimal("80"),
        True,
        ids["operator"],
        known=False,
        now=NOW,
    )
    with pytest.raises(DomainRejected, match="FINAL_RISK_CHECK_FAILED: PROTECTION_UNKNOWN"):
        service.create_order_intent(
            authorization_id,
            ids["operator"],
            IntentKind.ADD,
            "acct-1",
            "BINANCE",
            ids["instrument"],
            Direction.LONG,
            Decimal("0.5"),
            "add-unprotected",
            add_candidate=current_add_candidate(),
            now=NOW,
        )
    service.record_protection(
        position_id,
        "add-stop-restored",
        Decimal("0.5"),
        Decimal("80"),
        True,
        ids["operator"],
        now=NOW,
    )
    service.record_position(
        "acct-1",
        "BINANCE",
        ids["instrument"],
        Decimal("0"),
        Decimal("0"),
        Decimal("110"),
        True,
        ids["operator"],
        now=NOW,
    )
    with pytest.raises(DomainRejected, match="ADD_POSITION_INVALID"):
        service.create_order_intent(
            authorization_id,
            ids["operator"],
            IntentKind.ADD,
            "acct-1",
            "BINANCE",
            ids["instrument"],
            Direction.LONG,
            Decimal("0.5"),
            "add-no-position",
            add_candidate=current_add_candidate(),
            now=NOW,
        )
    service.record_position(
        "acct-1",
        "BINANCE",
        ids["instrument"],
        Decimal("0.5"),
        Decimal("100"),
        Decimal("110"),
        True,
        ids["operator"],
        now=NOW,
    )
    service.set_risk_policy(
        actor_id=ids["admin"],
        version="risk-no-pyramid",
        system_state=SystemRiskState.NO_PYRAMID,
        max_total_risk=Decimal("100"),
        max_fact_age=timedelta(seconds=30),
        now=NOW,
    )
    with pytest.raises(DomainRejected, match="FINAL_RISK_CHECK_FAILED: PYRAMID_DISABLED"):
        service.create_order_intent(
            authorization_id,
            ids["operator"],
            IntentKind.ADD,
            "acct-1",
            "BINANCE",
            ids["instrument"],
            Direction.LONG,
            Decimal("0.5"),
            "add-no-pyramid",
            add_candidate=current_add_candidate(),
            now=NOW,
        )


def test_zero_fill_cancelled_add_does_not_consume_unit_and_requires_operator(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)
    proposal_id = create_approved_proposal(
        service,
        ids,
        risk_tier=RiskTier.LOW,
        key="add-cancel",
        allow_auto_add=True,
    )
    authorization_id = issue_authorization(service, ids, proposal_id, allowed_adds=1)
    opening = service.create_order_intent(
        authorization_id,
        ids["operator"],
        IntentKind.INITIAL,
        "acct-1",
        "BINANCE",
        ids["instrument"],
        Direction.LONG,
        Decimal("0.5"),
        "add-cancel-opening",
        now=NOW,
    )
    token = service.acquire_sender("acct-1:BINANCE", "cancel-worker", ids["operator"], NOW)
    service.record_shadow_order(
        opening.intent_id,
        ids["operator"],
        "acct-1:BINANCE",
        "cancel-worker",
        token,
        "add-cancel-order",
        now=NOW,
    )
    service.record_fill(
        opening.intent_id,
        ids["operator"],
        "add-cancel-fill",
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
        Decimal("0.5"),
        Decimal("100"),
        Decimal("110"),
        True,
        ids["operator"],
        now=NOW,
    )
    service.record_protection(
        position_id,
        "cancel-stop",
        Decimal("0.5"),
        Decimal("90"),
        True,
        ids["operator"],
        now=NOW,
    )
    service.set_capability_gate(
        "AUTO_ADD", CapabilityStatus.ENABLED, "test Add", ids["admin"], now=NOW
    )
    addition = service.create_order_intent(
        authorization_id,
        ids["operator"],
        IntentKind.ADD,
        "acct-1",
        "BINANCE",
        ids["instrument"],
        Direction.LONG,
        Decimal("0.5"),
        "addition-to-cancel",
        add_candidate=current_add_candidate(),
        now=NOW,
    )

    with pytest.raises(DomainRejected, match="RBAC_DENIED"):
        service.release_unfilled_intent(
            addition.intent_id,
            ids["observer"],
            OrderIntentStatus.CANCELLED,
            "unauthorized",
            now=NOW,
        )
    service.release_unfilled_intent(
        addition.intent_id,
        ids["operator"],
        OrderIntentStatus.CANCELLED,
        "confirmed zero fill",
        now=NOW,
    )
    service.release_unfilled_intent(
        addition.intent_id,
        ids["operator"],
        OrderIntentStatus.CANCELLED,
        "duplicate result",
        now=NOW,
    )

    with database.session_factory() as session:
        authorization = session.get(TradingAuthorization, authorization_id)
        reservation = session.get(RiskReservation, addition.reservation_id)
        assert authorization is not None
        assert authorization.used_quantity == Decimal("0.500000000000000000")
        assert authorization.used_adds == 0
        assert reservation is not None
        assert reservation.status == ReservationStatus.RELEASED.value


def test_fill_validation_rejects_wrong_side_overfill_currency_and_semantic_reuse(
    service: TradingService,
) -> None:
    ids = seed(service)
    second_instrument = register_flat_instrument(service, ids, "SOLUSDT")
    first_proposal = create_approved_proposal(service, ids, risk_tier=RiskTier.LOW, key="fill-p1")
    second_proposal = create_approved_proposal(
        service,
        ids,
        risk_tier=RiskTier.LOW,
        key="fill-p2",
        instrument_id=second_instrument,
    )
    first_auth = issue_authorization(service, ids, first_proposal)
    second_auth = issue_authorization(service, ids, second_proposal)
    first = service.create_order_intent(
        first_auth,
        ids["operator"],
        IntentKind.INITIAL,
        "acct-1",
        "BINANCE",
        ids["instrument"],
        Direction.LONG,
        Decimal("1"),
        "fill-i1",
        now=NOW,
    )
    second = service.create_order_intent(
        second_auth,
        ids["operator"],
        IntentKind.INITIAL,
        "acct-1",
        "BINANCE",
        second_instrument,
        Direction.LONG,
        Decimal("1"),
        "fill-i2",
        now=NOW,
    )
    token = service.acquire_sender("acct-1:BINANCE", "fill-worker", ids["operator"], NOW)
    for created, order_id in ((first, "fill-order-1"), (second, "fill-order-2")):
        service.record_shadow_order(
            created.intent_id,
            ids["operator"],
            "acct-1:BINANCE",
            "fill-worker",
            token,
            order_id,
            now=NOW,
        )

    with pytest.raises(DomainRejected, match="FILL_SIDE_MISMATCH"):
        service.record_fill(
            first.intent_id,
            ids["operator"],
            "wrong-side",
            "SELL",
            Decimal("0.1"),
            Decimal("100"),
            Decimal("0"),
            "USDT",
            Decimal("0"),
            now=NOW,
        )
    with pytest.raises(DomainRejected, match="ORDER_INTENT_OVERFILLED"):
        service.record_fill(
            first.intent_id,
            ids["operator"],
            "overfill",
            "BUY",
            Decimal("1.1"),
            Decimal("100"),
            Decimal("0"),
            "USDT",
            Decimal("0"),
            now=NOW,
        )
    with pytest.raises(DomainRejected, match="PNL_CURRENCY_MISMATCH"):
        service.record_fill(
            first.intent_id,
            ids["operator"],
            "wrong-currency",
            "BUY",
            Decimal("0.1"),
            Decimal("100"),
            Decimal("0"),
            "USDC",
            Decimal("0"),
            now=NOW,
        )
    first_fill = service.record_fill(
        first.intent_id,
        ids["operator"],
        "semantic-fill",
        "BUY",
        Decimal("0.4"),
        Decimal("100"),
        Decimal("1"),
        "USDT",
        Decimal("0"),
        now=NOW,
    )
    duplicate = service.record_fill(
        first.intent_id,
        ids["operator"],
        "semantic-fill",
        "BUY",
        Decimal("0.4"),
        Decimal("100"),
        Decimal("1"),
        "USDT",
        Decimal("0"),
        now=NOW,
    )
    assert duplicate == first_fill
    with pytest.raises(IdempotencyConflict):
        service.record_fill(
            first.intent_id,
            ids["operator"],
            "semantic-fill",
            "BUY",
            Decimal("0.4"),
            Decimal("101"),
            Decimal("1"),
            "USDT",
            Decimal("0"),
            now=NOW,
        )
    with pytest.raises(IdempotencyConflict):
        service.record_fill(
            second.intent_id,
            ids["operator"],
            "semantic-fill",
            "BUY",
            Decimal("0.4"),
            Decimal("100"),
            Decimal("1"),
            "USDT",
            Decimal("0"),
            now=NOW,
        )


def test_target_cap_and_single_unclosed_campaign_are_enforced(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)
    first_proposal = create_approved_proposal(
        service, ids, risk_tier=RiskTier.LOW, key="campaign-one"
    )
    second_proposal = create_approved_proposal(
        service, ids, risk_tier=RiskTier.LOW, key="campaign-two"
    )
    first_auth = issue_authorization(service, ids, first_proposal)
    second_auth = issue_authorization(service, ids, second_proposal)
    first = service.create_order_intent(
        first_auth,
        ids["operator"],
        IntentKind.INITIAL,
        "acct-1",
        "BINANCE",
        ids["instrument"],
        Direction.LONG,
        Decimal("1"),
        "campaign-first-intent",
        now=NOW,
    )
    with pytest.raises(DomainRejected, match="ACTIVE_CAMPAIGN_EXISTS"):
        service.create_order_intent(
            second_auth,
            ids["operator"],
            IntentKind.INITIAL,
            "acct-1",
            "BINANCE",
            ids["instrument"],
            Direction.LONG,
            Decimal("1"),
            "campaign-second-intent",
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
    with pytest.raises(DomainRejected, match="TARGET_EXCEEDS_POSITION"):
        service.update_campaign_target(
            first.campaign_id,
            ids["operator"],
            (TargetCandidate(Decimal("1.1"), TargetUrgency.NORMAL, "invalid"),),
            now=NOW,
        )
    decision = service.update_campaign_target(
        first.campaign_id,
        ids["operator"],
        (TargetCandidate(Decimal("1"), TargetUrgency.NORMAL, "hold"),),
        now=NOW,
    )
    assert decision.target_quantity == Decimal("1")
    with database.session_factory() as session:
        campaign = session.get(Campaign, first.campaign_id)
        assert campaign is not None and campaign.status == "OPEN"


def test_reconciliation_match_is_computed_for_the_whole_scope(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)
    second_instrument = register_flat_instrument(service, ids, "XRPUSDT")
    first_proposal = create_approved_proposal(service, ids, risk_tier=RiskTier.LOW, key="scope-p1")
    second_proposal = create_approved_proposal(
        service,
        ids,
        risk_tier=RiskTier.LOW,
        key="scope-p2",
        instrument_id=second_instrument,
    )
    first_auth = issue_authorization(service, ids, first_proposal)
    second_auth = issue_authorization(service, ids, second_proposal)
    first = service.create_order_intent(
        first_auth,
        ids["operator"],
        IntentKind.INITIAL,
        "acct-1",
        "BINANCE",
        ids["instrument"],
        Direction.LONG,
        Decimal("1"),
        "scope-i1",
        now=NOW,
    )
    service.create_order_intent(
        second_auth,
        ids["operator"],
        IntentKind.INITIAL,
        "acct-1",
        "BINANCE",
        second_instrument,
        Direction.LONG,
        Decimal("1"),
        "scope-i2",
        now=NOW,
    )
    token = service.acquire_sender("acct-1:BINANCE", "scope-worker", ids["operator"], NOW)
    service.record_shadow_order(
        first.intent_id,
        ids["operator"],
        "acct-1:BINANCE",
        "scope-worker",
        token,
        "scope-order-1",
        now=NOW,
    )
    service.record_fill(
        first.intent_id,
        ids["operator"],
        "scope-fill-1",
        "BUY",
        Decimal("1"),
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
        Decimal("1"),
        Decimal("100"),
        Decimal("100"),
        True,
        ids["operator"],
        now=NOW,
    )
    service.record_protection(
        position_id,
        "scope-stop",
        Decimal("1"),
        Decimal("90"),
        True,
        ids["operator"],
        now=NOW,
    )
    service.record_position(
        "acct-1",
        "BINANCE",
        second_instrument,
        Decimal("0"),
        Decimal("0"),
        Decimal("100"),
        False,
        ids["operator"],
        now=NOW,
    )

    run_id = service.reconcile_campaign(
        first.campaign_id, "acct-1:BINANCE", ids["operator"], now=NOW
    )
    with pytest.raises(DomainRejected, match="RECONCILIATION_STATUS_NOT_TRUSTED"):
        service.record_scope_reconciliation(
            "acct-1:BINANCE",
            ids["operator"],
            ReconciliationStatus.MATCH,
            (),
            now=NOW,
        )

    with database.session_factory() as session:
        run = session.get(ReconciliationRun, run_id)
        assert run is not None
        assert run.status == ReconciliationStatus.UNKNOWN.value
        assert run.is_computed is True
        assert run.campaign_id is None
        assert any(value.startswith("POSITION_UNKNOWN") for value in run.differences)


def test_resolved_run_cannot_authorize_sender_takeover(service: TradingService) -> None:
    ids = seed(service)
    first = service.acquire_sender("acct-1:BINANCE", "worker-a", ids["operator"], NOW)
    run_id = service.record_scope_reconciliation(
        "acct-1:BINANCE",
        ids["operator"],
        ReconciliationStatus.UNKNOWN,
        ("POSITION_UNKNOWN",),
        now=NOW + timedelta(minutes=2),
    )
    service.require_manual_reconciliation(
        run_id, ids["operator"], "manual investigation", now=NOW + timedelta(minutes=2)
    )
    service.resolve_reconciliation(
        run_id, ids["operator"], "manual note only", now=NOW + timedelta(minutes=2)
    )

    with pytest.raises(DomainRejected, match="RECONCILIATION_REQUIRED"):
        service.acquire_sender(
            "acct-1:BINANCE",
            "worker-b",
            ids["operator"],
            NOW + timedelta(minutes=2),
        )
    service.validate_sender("acct-1:BINANCE", "worker-a", first, NOW + timedelta(seconds=30))


def test_valid_fencing_token_cannot_send_for_another_scope(service: TradingService) -> None:
    ids = seed(service)
    proposal_id = create_approved_proposal(service, ids, risk_tier=RiskTier.LOW, key="wrong-scope")
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
        "wrong-scope-intent",
        now=NOW,
    )
    wrong_token = service.acquire_sender("acct-2:BINANCE", "wrong-worker", ids["admin"], NOW)

    with pytest.raises(DomainRejected, match="EXECUTION_SCOPE_MISMATCH"):
        service.record_shadow_order(
            created.intent_id,
            ids["admin"],
            "acct-2:BINANCE",
            "wrong-worker",
            wrong_token,
            "wrong-scope-order",
            now=NOW,
        )
    with pytest.raises(DomainRejected, match="EXECUTION_SCOPE_INVALID"):
        service.acquire_sender("acct-1:", "worker", ids["admin"], NOW)


def test_pnl_rejects_fee_and_funding_currency_without_fx(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)
    proposal_id = create_approved_proposal(service, ids, risk_tier=RiskTier.LOW, key="currency-pnl")
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
        "currency-intent",
        now=NOW,
    )
    token = service.acquire_sender("acct-1:BINANCE", "currency-worker", ids["operator"], NOW)
    service.record_shadow_order(
        created.intent_id,
        ids["operator"],
        "acct-1:BINANCE",
        "currency-worker",
        token,
        "currency-order",
        now=NOW,
    )
    with pytest.raises(DomainRejected, match="PNL_CURRENCY_MISMATCH"):
        service.record_fill(
            created.intent_id,
            ids["operator"],
            "currency-fill-invalid",
            "BUY",
            Decimal("1"),
            Decimal("100"),
            Decimal("1"),
            "USDC",
            Decimal("0"),
            now=NOW,
        )
    service.record_fill(
        created.intent_id,
        ids["operator"],
        "currency-fill",
        "BUY",
        Decimal("1"),
        Decimal("100"),
        Decimal("1"),
        "USDT",
        Decimal("0"),
        now=NOW,
    )
    service.record_position(
        "acct-1",
        "BINANCE",
        ids["instrument"],
        Decimal("1"),
        Decimal("100"),
        Decimal("101"),
        True,
        ids["operator"],
        now=NOW,
    )
    with pytest.raises(DomainRejected, match="PNL_CURRENCY_MISMATCH"):
        service.record_funding(
            created.campaign_id,
            "BINANCE",
            "currency-funding-invalid",
            Decimal("1"),
            "USDC",
            ids["operator"],
            now=NOW,
        )
    with database.session_factory.begin() as session:
        fill = session.scalar(select(VenueFill).where(VenueFill.venue_fill_id == "currency-fill"))
        assert fill is not None
        fill.fee_currency = "USDC"
    with pytest.raises(DomainRejected, match="PNL_CURRENCY_MISMATCH"):
        service.refresh_campaign_pnl(created.campaign_id, ids["operator"], now=NOW)


def test_human_cannot_spoof_system_proposal_and_strategy_identity_is_frozen(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)
    with pytest.raises(DomainRejected, match="PROPOSAL_SOURCE_INVALID"):
        service.create_proposal(
            actor_id=ids["proposer"],
            source=ProposalSource.SYSTEM,
            risk_tier=RiskTier.LOW,
            account_id="acct-1",
            venue="BINANCE",
            instrument_id=ids["instrument"],
            direction=Direction.LONG,
            quantity=Decimal("1"),
            max_risk=Decimal("10"),
            expires_at=NOW + timedelta(hours=1),
            idempotency_key="human-system-spoof",
            strategy_id="spoofed",
            strategy_version="v1",
            source_candidate_id="pt_spoofed",
            source_observed_at=NOW,
            source_readiness="READY",
            now=NOW,
        )
    proposal_id = create_approved_proposal(
        service,
        ids,
        risk_tier=RiskTier.LOW,
        key="service-system-proposal",
        actor_id=ids["strategy"],
        source=ProposalSource.SYSTEM,
        strategy_id="breakout",
        strategy_version="2026-07-19",
    )

    with database.session_factory() as session:
        proposal = session.get(Proposal, proposal_id)
        assert proposal is not None
        assert proposal.proposer_id == ids["strategy"]
        assert proposal.strategy_id == "breakout"
        assert proposal.strategy_version == "2026-07-19"


def test_enabled_add_gate_keeps_database_ready_but_does_not_bypass_hard_checks(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)
    proposal_id = create_approved_proposal(
        service,
        ids,
        risk_tier=RiskTier.LOW,
        key="gate-hard-check",
        allow_auto_add=True,
    )
    authorization_id = issue_authorization(service, ids, proposal_id, allowed_adds=1)
    service.set_capability_gate(
        "AUTO_ADD", CapabilityStatus.ENABLED, "explicit test", ids["admin"], now=NOW
    )

    assert database.is_ready() == (True, None)
    with pytest.raises(DomainRejected, match="FINAL_RISK_CHECK_FAILED"):
        service.create_order_intent(
            authorization_id,
            ids["operator"],
            IntentKind.ADD,
            "acct-1",
            "BINANCE",
            ids["instrument"],
            Direction.LONG,
            Decimal("0.5"),
            "gate-is-not-authorization",
            add_candidate=current_add_candidate(),
            now=NOW,
        )


def test_campaign_closes_and_releases_open_risk_only_after_exit_and_match(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)
    proposal_id = create_approved_proposal(
        service, ids, risk_tier=RiskTier.LOW, key="campaign-close"
    )
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
        "campaign-close-open",
        now=NOW,
    )
    token = service.acquire_sender("acct-1:BINANCE", "close-worker", ids["operator"], NOW)
    service.record_shadow_order(
        opening.intent_id,
        ids["operator"],
        "acct-1:BINANCE",
        "close-worker",
        token,
        "campaign-close-order-open",
        now=NOW,
    )
    service.record_fill(
        opening.intent_id,
        ids["operator"],
        "campaign-close-fill-open",
        "BUY",
        Decimal("1"),
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
        Decimal("1"),
        Decimal("100"),
        Decimal("100"),
        True,
        ids["operator"],
        now=NOW,
    )
    service.record_protection(
        position_id,
        "campaign-close-stop",
        Decimal("1"),
        Decimal("90"),
        True,
        ids["operator"],
        now=NOW,
    )
    service.update_campaign_target(
        opening.campaign_id,
        ids["operator"],
        (TargetCandidate(Decimal("0"), TargetUrgency.IMMEDIATE, "exit"),),
        now=NOW,
    )
    exit_intent_id = service.create_reduction_intent(
        opening.campaign_id, ids["operator"], "campaign-close-exit", now=NOW
    )
    service.record_shadow_order(
        exit_intent_id,
        ids["operator"],
        "acct-1:BINANCE",
        "close-worker",
        token,
        "campaign-close-order-exit",
        now=NOW,
    )
    service.record_fill(
        exit_intent_id,
        ids["operator"],
        "campaign-close-fill-exit",
        "SELL",
        Decimal("1"),
        Decimal("105"),
        Decimal("0"),
        "USDT",
        Decimal("0"),
        now=NOW,
    )
    service.record_position(
        "acct-1",
        "BINANCE",
        ids["instrument"],
        Decimal("0"),
        Decimal("0"),
        Decimal("105"),
        True,
        ids["operator"],
        now=NOW,
    )
    service.reconcile_scope("acct-1:BINANCE", ids["operator"], now=NOW)
    service.close_campaign(opening.campaign_id, ids["operator"], now=NOW)

    with database.session_factory() as session:
        campaign = session.get(Campaign, opening.campaign_id)
        reservation = session.get(RiskReservation, opening.reservation_id)
        assert campaign is not None and campaign.status == "CLOSED"
        assert reservation is not None and reservation.status == "RELEASED"
