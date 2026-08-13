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
    CampaignStatus,
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
    AccountEquity,
    AuditEvent,
    Campaign,
    CapabilityGate,
    CommandReceipt,
    ExchangeAccount,
    Instrument,
    OrderIntent,
    Position,
    Proposal,
    RiskDecision,
    RiskPolicy,
    RiskReservation,
    Team,
    TradingAuthorization,
)
from trading_control_plane.queries import TradingQueries
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
        max_account_risk=Decimal("100"),
        max_single_loss=Decimal("100"),
        max_consecutive_losses=3,
        loss_cooldown=timedelta(hours=1),
        max_fact_age=timedelta(seconds=30),
        now=NOW,
    )
    context = TradingQueries(service.database).user_context(admin)
    with service.database.session_factory.begin() as session:
        team = session.get(Team, UUID(str(context["active_team"]["team_id"])), with_for_update=True)
        assert team is not None
        # Exercise the retained pre-lock TESTNET compatibility workflow. Fresh
        # bootstrap and migrated Teams use the unified exchange-backed ledger.
        team.execution_mode = "TESTNET"
        team.execution_mode_locked_at = None
        session.add(
            ExchangeAccount(
                team_id=team.team_id,
                environment="TESTNET",
                account_id="acct-1",
                venue="BINANCE",
                label="Core workflow test account",
                registration_source="WORKFLOW_REFERENCE",
                connection_status="UNCONFIGURED",
                trading_status="DISABLED",
                credential_metadata={},
                created_by=admin,
                updated_by=admin,
                created_at=NOW,
                updated_at=NOW,
            )
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


def enable_auto_add_fixture(database: Database, admin: UUID, *, now: datetime = NOW) -> None:
    """Open AUTO_ADD only while arranging a pre-existing test scenario."""
    with database.session_factory.begin() as session:
        gate = session.get(CapabilityGate, "AUTO_ADD", with_for_update=True)
        assert gate is not None
        gate.status = CapabilityStatus.ENABLED.value
        gate.reason = "integration fixture precondition"
        gate.operator_id = str(admin)
        gate.version += 1
        gate.updated_at = now


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


def test_auto_add_rejects_initial_candidate_through_legacy_identity() -> None:
    proposal = Proposal(
        source=ProposalSource.SYSTEM.value,
        strategy_version="breakouts-v1",
        source_candidate_id="pt_legacy_initial",
        venue="BINANCE",
        direction=Direction.LONG.value,
        frozen_payload={"details": {"allow_auto_add": True}},
    )
    instrument = Instrument(symbol="BTCUSDT")
    policy = RiskPolicy(max_fact_age_seconds=30)
    candidate = AddCandidateFacts(
        candidate_id="pt_exact_initial",
        legacy_candidate_id="pt_legacy_initial",
        contract_version="breakouts-v1",
        venue="BINANCE",
        symbol="BTCUSDT",
        direction=Direction.LONG,
        observed_at=NOW,
        reference_price=Decimal("110"),
        readiness="READY",
    )

    with pytest.raises(DomainRejected, match="AUTO_ADD_CANDIDATE_VERSION_INVALID"):
        TradingService._validate_add_candidate(
            proposal=proposal,
            instrument=instrument,
            candidate=candidate,
            policy=policy,
            now=NOW,
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
        assert (
            session.scalar(
                select(func.count())
                .select_from(CommandReceipt)
                .where(CommandReceipt.operation == "proposal.create")
            )
            == 1
        )


def test_concurrent_manual_semantic_duplicates_reuse_one_active_proposal(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)
    second_proposer = service.create_user("proposer-2", ids["admin"], now=NOW)
    service.assign_role(
        second_proposer,
        Role.PROPOSER,
        ids["admin"],
        "acct-1",
        "BINANCE",
        now=NOW,
    )
    service.assign_role(
        second_proposer,
        Role.REVIEWER,
        ids["admin"],
        "acct-1",
        "BINANCE",
        now=NOW,
    )

    def create(actor_id: UUID) -> UUID:
        return service.create_proposal(
            actor_id=actor_id,
            source=ProposalSource.MANUAL,
            risk_tier=RiskTier.LOW,
            account_id="acct-1",
            venue="BINANCE",
            instrument_id=ids["instrument"],
            direction=Direction.LONG,
            quantity=Decimal("1"),
            max_risk=Decimal("10"),
            expires_at=NOW + timedelta(hours=8),
            idempotency_key=f"concurrent-manual-{actor_id}",
            details={
                "trigger_price": "100",
                "invalidation_price": "95",
                "initial_quantity": "1",
                "rationale": f"human note {actor_id}",
            },
            deduplicate_active_manual_semantics=True,
            now=NOW,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        proposal_ids = list(pool.map(create, (ids["proposer"], second_proposer)))

    assert proposal_ids[0] == proposal_ids[1]
    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Proposal)) == 1
        assert session.scalar(select(func.count()).select_from(CommandReceipt)) == 2


def test_perptape_manual_and_automatic_entry_points_share_one_active_scope(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)
    automatic_id = service.create_proposal(
        actor_id=ids["strategy"],
        source=ProposalSource.SYSTEM,
        risk_tier=RiskTier.LOW,
        account_id="acct-1",
        venue="BINANCE",
        instrument_id=ids["instrument"],
        direction=Direction.LONG,
        quantity=Decimal("1"),
        max_risk=Decimal("1"),
        expires_at=NOW + timedelta(hours=8),
        idempotency_key="perptape-auto-scope",
        strategy_id="perptape-resonance",
        strategy_version="breakouts-v1:auto",
        source_candidate_id="ptr_auto_scope",
        source_observed_at=NOW,
        source_readiness="READY",
        now=NOW,
    )
    service.submit_proposal(automatic_id, ids["strategy"], now=NOW)

    one_click_id = service.create_proposal(
        actor_id=ids["strategy"],
        source=ProposalSource.SYSTEM,
        risk_tier=RiskTier.MEDIUM,
        account_id="acct-1",
        venue="BINANCE",
        instrument_id=ids["instrument"],
        direction=Direction.LONG,
        quantity=Decimal("2"),
        max_risk=Decimal("2"),
        expires_at=NOW + timedelta(hours=8),
        idempotency_key="perptape-one-click-scope",
        strategy_id="perptape",
        strategy_version="breakouts-v1:default",
        source_candidate_id="pt_one_click_scope",
        source_observed_at=NOW + timedelta(seconds=1),
        source_readiness="READY",
        deduplicate_active_system_scope=True,
        now=NOW + timedelta(seconds=1),
    )

    assert one_click_id == automatic_id
    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Proposal)) == 1
        assert (
            session.scalar(
                select(func.count())
                .select_from(CommandReceipt)
                .where(CommandReceipt.operation == "proposal.create")
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.event_type == "PROPOSAL_DUPLICATE_REUSED")
            )
            == 1
        )


def test_admin_cleanup_expires_cross_proposer_manual_duplicates_with_audit(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)
    second_proposer = service.create_user("proposer-2", ids["admin"], now=NOW)
    service.assign_role(
        second_proposer,
        Role.PROPOSER,
        ids["admin"],
        "acct-1",
        "BINANCE",
        now=NOW,
    )

    proposal_ids = [
        service.create_proposal(
            actor_id=actor_id,
            source=ProposalSource.MANUAL,
            risk_tier=RiskTier.LOW,
            account_id="acct-1",
            venue="BINANCE",
            instrument_id=ids["instrument"],
            direction=Direction.LONG,
            quantity=Decimal("1"),
            max_risk=Decimal("10"),
            expires_at=NOW + timedelta(minutes=index, hours=8),
            idempotency_key=f"legacy-manual-{index}",
            details={"trigger_price": "100", "invalidation_price": "95"},
            now=NOW + timedelta(minutes=index),
        )
        for index, actor_id in enumerate((ids["proposer"], second_proposer))
    ]

    assert (
        service.expire_duplicate_active_manual_proposals(
            actor_id=ids["admin"], now=NOW + timedelta(minutes=2)
        )
        == 1
    )
    with pytest.raises(DomainRejected, match="SELF_REVIEW_FORBIDDEN"):
        service.review_proposal(
            proposal_ids[0],
            second_proposer,
            ReviewDecision.REJECT,
            "must not review a proposal consolidated from my duplicate",
            now=NOW + timedelta(minutes=3),
        )
    with database.session_factory() as session:
        proposals = {
            item.proposal_id: item.status
            for item in session.scalars(
                select(Proposal).where(Proposal.proposal_id.in_(proposal_ids))
            ).all()
        }
        assert proposals[proposal_ids[0]] == ProposalStatus.DRAFT.value
        assert proposals[proposal_ids[1]] == ProposalStatus.EXPIRED.value
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.event_type == "PROPOSAL_DUPLICATE_EXPIRED")
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.event_type == "PROPOSAL_DUPLICATE_REUSED")
            )
            == 1
        )


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
    queries = TradingQueries(database)
    reviewer_two_summary = queries.list_proposals(ids["reviewer_two"], now=NOW)[0]
    assert reviewer_two_summary["approval_count"] == 1
    assert reviewer_two_summary["required_approvals"] == 2
    assert (
        queries.list_proposals(ids["reviewer_one"], now=NOW)[0]["actionable_for_current_user"]
        is False
    )
    assert reviewer_two_summary["actionable_for_current_user"] is True
    assert (
        queries.proposal_detail(ids["reviewer_one"], proposal_id, now=NOW)[
            "actionable_for_current_user"
        ]
        is False
    )
    assert (
        queries.proposal_detail(ids["reviewer_two"], proposal_id, now=NOW)[
            "actionable_for_current_user"
        ]
        is True
    )
    assert (
        queries.list_proposals(ids["proposer"], now=NOW)[0]["actionable_for_current_user"] is False
    )
    assert (
        queries.list_proposals(ids["observer"], now=NOW)[0]["actionable_for_current_user"] is False
    )
    expired_projection = queries.list_proposals(ids["reviewer_two"], now=NOW + timedelta(hours=2))[
        0
    ]
    assert expired_projection["status"] == "EXPIRED"
    assert expired_projection["actionable_for_current_user"] is False
    assert (
        queries.list_proposals(
            ids["reviewer_two"],
            status="PENDING_REVIEW",
            now=NOW + timedelta(hours=2),
        )
        == []
    )
    assert queries.list_proposals(
        ids["reviewer_two"],
        status="EXPIRED",
        now=NOW + timedelta(hours=2),
    )[0]["proposal_id"] == str(proposal_id)
    assert (
        queries.proposal_detail(ids["reviewer_two"], proposal_id, now=NOW + timedelta(hours=2))[
            "status"
        ]
        == "EXPIRED"
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
    approved_summary = queries.list_proposals(ids["reviewer_two"], now=NOW + timedelta(minutes=10))[
        0
    ]
    assert approved_summary["execution_status"] == "AWAITING_LAUNCH"
    expired_approval_summary = queries.list_proposals(
        ids["reviewer_two"], now=NOW + timedelta(hours=2)
    )[0]
    assert expired_approval_summary["status"] == "APPROVED"
    assert expired_approval_summary["execution_status"] == "WINDOW_EXPIRED"
    assert (
        queries.proposal_detail(ids["reviewer_two"], proposal_id, now=NOW + timedelta(hours=2))[
            "execution_status"
        ]
        == "WINDOW_EXPIRED"
    )
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


def test_cancelled_initial_intent_cannot_be_recreated_with_a_new_key(
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
        Decimal("0.5"),
        "one-time-initial",
        now=NOW,
    )
    service.reconcile_scope("TESTNET:acct-1:BINANCE", ids["operator"], now=NOW)
    service.release_unfilled_intent(
        created.intent_id,
        ids["operator"],
        OrderIntentStatus.CANCELLED,
        "venue confirmed no fill",
        now=NOW,
    )

    with pytest.raises(DomainRejected, match="AUTHORIZATION_INACTIVE"):
        service.create_order_intent(
            authorization_id,
            ids["operator"],
            IntentKind.INITIAL,
            "acct-1",
            "BINANCE",
            ids["instrument"],
            Direction.LONG,
            Decimal("0.5"),
            "second-initial-must-fail",
            now=NOW,
        )

    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Campaign)) == 1
        campaign = session.get(Campaign, created.campaign_id)
        authorization = session.get(TradingAuthorization, authorization_id)
        assert campaign is not None
        assert campaign.status == CampaignStatus.CLOSED.value
        assert authorization is not None
        assert authorization.active is False
        assert session.scalar(select(func.count()).select_from(OrderIntent)) == 1


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
        "testnet order was never sent",
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

    with pytest.raises(DomainRejected, match="REVIEWED_RESTORE_REQUIRED"):
        service.set_capability_gate(
            "AUTO_ADD",
            CapabilityStatus.ENABLED,
            "direct operator decision must remain closed",
            ids["admin"],
            now=NOW,
        )
    with database.session_factory() as session:
        gate = session.get(CapabilityGate, "AUTO_ADD")
        assert gate is not None
        assert gate.status == CapabilityStatus.DISABLED.value


def test_sender_fencing_rejects_old_owner_after_reconciled_takeover(
    service: TradingService,
) -> None:
    ids = seed(service)
    first = service.acquire_sender("TESTNET:acct-1:BINANCE", "worker-a", ids["operator"], NOW)
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
    service.reconcile_scope("TESTNET:acct-1:BINANCE", ids["operator"], now=takeover_time)
    second = service.acquire_sender(
        "TESTNET:acct-1:BINANCE", "worker-b", ids["operator"], takeover_time
    )

    assert second > first
    with pytest.raises(DomainRejected, match="FENCING_TOKEN_REJECTED"):
        service.validate_sender(
            "TESTNET:acct-1:BINANCE", "worker-a", first, ids["operator"], takeover_time
        )
    service.validate_sender(
        "TESTNET:acct-1:BINANCE", "worker-b", second, ids["operator"], takeover_time
    )


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
        "TESTNET:acct-1:BINANCE",
        ids["operator"],
        ReconciliationStatus.UNKNOWN,
        ("POSITION_UNKNOWN",),
        now=NOW,
    )
    different = service.record_scope_reconciliation(
        "TESTNET:acct-1:BINANCE",
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


def test_capability_gates_are_default_disabled(database: Database) -> None:
    with database.session_factory() as session:
        gates = session.scalars(
            select(CapabilityGate).order_by(CapabilityGate.capability_key)
        ).all()

    assert [(gate.capability_key, gate.status) for gate in gates] == [
        ("AUTO_ADD", CapabilityStatus.DISABLED.value),
        ("AUTO_OPERATING_REFILL", CapabilityStatus.DISABLED.value),
        ("AUTO_PROFIT_SWEEP", CapabilityStatus.DISABLED.value),
        ("CAPITAL_TRANSFER", CapabilityStatus.DISABLED.value),
        ("LIVE_ORDER_SEND", CapabilityStatus.DISABLED.value),
    ]


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


def test_risk_and_send_freshness_allow_bounded_venue_clock_skew(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)
    proposal_id = create_approved_proposal(
        service,
        ids,
        risk_tier=RiskTier.LOW,
        key="bounded-clock-skew",
    )
    observed_at = NOW + timedelta(seconds=29)
    with database.session_factory.begin() as session:
        position = session.get(Position, ids["position"])
        equity = session.scalar(select(AccountEquity))
        assert position is not None and equity is not None
        position.observed_at = observed_at
        equity.observed_at = observed_at

    decision_id = service.decide_risk(
        proposal_id=proposal_id,
        actor_id=ids["operator"],
        kind=IntentKind.INITIAL,
        idempotency_key="bounded-clock-skew-risk",
        now=NOW,
    )

    with database.session_factory() as session:
        decision = session.get(RiskDecision, decision_id)
        assert decision is not None
        assert decision.result == "ALLOW"
        assert decision.input_data["fact_age_seconds"] == "0.0"
    assert not service._fact_is_stale(observed_at, NOW, timedelta(seconds=30))
    assert service._fact_is_stale(NOW + timedelta(seconds=31), NOW, timedelta(seconds=30))


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

    service.set_risk_policy(
        actor_id=ids["admin"],
        version="risk-kill",
        system_state=SystemRiskState.KILL_SWITCH,
        max_total_risk=Decimal("100"),
        max_account_risk=Decimal("100"),
        max_single_loss=Decimal("100"),
        max_consecutive_losses=3,
        loss_cooldown=timedelta(hours=1),
        max_fact_age=timedelta(seconds=30),
        now=NOW + timedelta(seconds=32),
    )
    service.record_position(
        "acct-1",
        "BINANCE",
        ids["instrument"],
        Decimal("0"),
        Decimal("0"),
        Decimal("100"),
        True,
        ids["operator"],
        now=NOW + timedelta(seconds=32),
    )
    service.record_account_equity(
        "acct-1",
        "BINANCE",
        Decimal("10000"),
        Decimal("9000"),
        "USDT",
        True,
        ids["operator"],
        now=NOW + timedelta(seconds=32),
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
            now=NOW + timedelta(seconds=32),
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


def test_resolved_run_cannot_authorize_sender_takeover(service: TradingService) -> None:
    ids = seed(service)
    first = service.acquire_sender("TESTNET:acct-1:BINANCE", "worker-a", ids["operator"], NOW)
    run_id = service.record_scope_reconciliation(
        "TESTNET:acct-1:BINANCE",
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
            "TESTNET:acct-1:BINANCE",
            "worker-b",
            ids["operator"],
            NOW + timedelta(minutes=2),
        )
    service.validate_sender(
        "TESTNET:acct-1:BINANCE",
        "worker-a",
        first,
        ids["operator"],
        NOW + timedelta(seconds=30),
    )


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
    enable_auto_add_fixture(database, ids["admin"])
    proposal_id = create_approved_proposal(
        service,
        ids,
        risk_tier=RiskTier.LOW,
        key="gate-hard-check",
        allow_auto_add=True,
    )
    authorization_id = issue_authorization(service, ids, proposal_id, allowed_adds=1)
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


def test_configured_single_loss_limit_is_enforced_by_server_risk_decision(
    database: Database,
    service: TradingService,
) -> None:
    ids = seed(service)
    service.configure_risk_policy(
        actor_id=ids["admin"],
        version="risk-v2-single-loss",
        max_total_risk=Decimal("100"),
        max_account_risk=Decimal("100"),
        max_single_loss=Decimal("10"),
        max_consecutive_losses=3,
        loss_cooldown=timedelta(hours=1),
        max_fact_age=timedelta(seconds=30),
        expected_revision=1,
        reason="tighten maximum loss per frozen proposal",
        idempotency_key="risk-policy-single-loss",
        now=NOW,
    )
    proposal_id = create_approved_proposal(
        service,
        ids,
        key="single-loss-denied",
        max_risk=Decimal("20"),
    )

    decision_id = service.decide_risk(
        proposal_id=proposal_id,
        actor_id=ids["operator"],
        kind=IntentKind.INITIAL,
        idempotency_key="single-loss-decision",
        now=NOW,
    )

    with database.session_factory() as session:
        decision = session.get(RiskDecision, decision_id)
        assert decision is not None
        assert decision.result == "DENY"
        assert decision.reasons == ["SINGLE_LOSS_LIMIT_EXCEEDED"]
        assert Decimal(decision.input_data["policy"]["max_single_loss"]) == Decimal("10")


def test_team_and_account_loss_streak_apply_cooldown_before_new_risk(
    database: Database,
    service: TradingService,
) -> None:
    ids = seed(service)
    for index in range(3):
        proposal_id = create_approved_proposal(
            service,
            ids,
            key=f"loss-streak-{index}",
            max_risk=Decimal("5"),
        )
        authorization_id = issue_authorization(service, ids, proposal_id)
        with database.session_factory.begin() as session:
            proposal = session.get(Proposal, proposal_id)
            authorization = session.get(TradingAuthorization, authorization_id)
            assert proposal is not None and authorization is not None
            session.add(
                Campaign(
                    team_id=proposal.team_id,
                    proposal_id=proposal_id,
                    authorization_id=authorization_id,
                    account_id=proposal.account_id,
                    venue=proposal.venue,
                    environment=proposal.environment,
                    instrument_id=proposal.instrument_id,
                    direction=proposal.direction,
                    status=CampaignStatus.CLOSED.value,
                    current_target_quantity=Decimal(0),
                    target_version=0,
                    target_reason="loss fixture",
                    target_urgency=TargetUrgency.IMMEDIATE.value,
                    target_calculated_at=NOW,
                    realized_pnl=Decimal("-1"),
                    unrealized_pnl=Decimal(0),
                    final_pnl=Decimal("-1"),
                    created_at=NOW - timedelta(minutes=2 - index),
                    updated_at=NOW,
                )
            )

    proposal_id = create_approved_proposal(
        service,
        ids,
        key="loss-streak-blocked",
        max_risk=Decimal("5"),
    )
    decision_id = service.decide_risk(
        proposal_id=proposal_id,
        actor_id=ids["operator"],
        kind=IntentKind.INITIAL,
        idempotency_key="loss-streak-decision",
        now=NOW + timedelta(seconds=10),
    )

    with database.session_factory() as session:
        decision = session.get(RiskDecision, decision_id)
        assert decision is not None
        assert decision.result == "DENY"
        assert decision.reasons == ["LOSS_COOLDOWN_ACTIVE"]
        assert decision.input_data["team_consecutive_losses"] == 3
        assert decision.input_data["account_consecutive_losses"] == 3
        assert Decimal(decision.input_data["loss_cooldown_remaining_seconds"]) > 0
