from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, NoReturn
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from trading_control_plane.binance import BinanceReadOnlySnapshot
from trading_control_plane.binance_execution import (
    BinanceTestnetOrder,
    BinanceTestnetOrderCommand,
    BinanceTestnetProtectionCommand,
    ProtectionCancelCommand,
)
from trading_control_plane.capital import (
    CapitalTransferCommand,
    CapitalTransferSubmission,
    evaluate_capital_automation,
)
from trading_control_plane.database import Database
from trading_control_plane.domain import (
    AddCandidateFacts,
    CampaignStatus,
    CapabilityStatus,
    CapitalDirection,
    CapitalTransferStatus,
    Direction,
    DomainRejected,
    EconomicFill,
    ExecutionEnvironment,
    FactStatus,
    IdempotencyConflict,
    IntentCreation,
    IntentKind,
    OrderIntentStatus,
    PnlBreakdown,
    PrincipalType,
    ProposalSource,
    ProposalStatus,
    ProtectionStatus,
    ReconciliationStatus,
    ReservationStatus,
    ReviewDecision,
    RiskEvaluationInput,
    RiskPolicyInput,
    RiskResult,
    RiskTier,
    Role,
    SystemRiskState,
    TargetCandidate,
    TargetDecision,
    TargetUrgency,
    VenueOrderStatus,
    compute_pnl,
    evaluate_risk,
    select_target_position,
)
from trading_control_plane.hyperliquid import HyperliquidReadOnlySnapshot
from trading_control_plane.hyperliquid_execution import (
    HyperliquidTestnetOrder,
    HyperliquidTestnetOrderCommand,
    HyperliquidTestnetProtectionCommand,
)
from trading_control_plane.metrics import (
    FENCING_REJECTIONS,
    INTENT_TRANSITIONS,
    PROTECTION_ISSUES,
    RECONCILIATION_RESULTS,
    RISK_RESULTS,
)
from trading_control_plane.models import (
    AccountEquity,
    Approval,
    AuditEvent,
    Campaign,
    CapabilityGate,
    CapitalAutomationPolicy,
    CapitalTransfer,
    CommandReceipt,
    FundingPayment,
    Instrument,
    OrderIntent,
    PerptapeFeed,
    Position,
    Proposal,
    ProtectionOrder,
    ReconciliationRun,
    RiskDecision,
    RiskPolicy,
    RiskReservation,
    RoleAssignment,
    SenderLease,
    TradingAuthorization,
    TransferAuthorization,
    TransferProposal,
    User,
    VenueFill,
    VenueOrder,
)
from trading_control_plane.notilt import (
    USD_STABLE_ASSETS,
    NoTiltReceipt,
    NoTiltUnsignedTransaction,
    NoTiltVaultSnapshot,
    UsdValuation,
)
from trading_control_plane.perptape import (
    PerptapeCandidate,
    PerptapeFeedSnapshot,
    apply_perptape_feed_delta,
    bound_perptape_feed_snapshot,
    perptape_snapshot_identity,
    validate_perptape_datetime,
    validate_perptape_feed_payload,
)

ACTIVE_INTENT_STATUSES = {
    OrderIntentStatus.PENDING.value,
    OrderIntentStatus.RESERVED.value,
    OrderIntentStatus.READY.value,
    OrderIntentStatus.SENT.value,
    OrderIntentStatus.PARTIALLY_FILLED.value,
    OrderIntentStatus.UNKNOWN.value,
}

OCCUPIED_RESERVATION_STATUSES = {
    ReservationStatus.RESERVED.value,
    ReservationStatus.OPEN.value,
    ReservationStatus.UNKNOWN.value,
}

# Venue clocks and a request's wall clock can differ slightly. Read-only ingestion
# rejects observations more than 30 seconds in the future, so downstream freshness
# checks use the same bounded tolerance.
MAX_FACT_CLOCK_SKEW = timedelta(seconds=30)

RELEASABLE_INTENT_STATUSES = {
    OrderIntentStatus.PENDING.value,
    OrderIntentStatus.RESERVED.value,
    OrderIntentStatus.READY.value,
    OrderIntentStatus.SENT.value,
    OrderIntentStatus.PARTIALLY_FILLED.value,
}

ROLE_ACTIONS: dict[Role, frozenset[str]] = {
    Role.OBSERVER: frozenset({"view", "capital.view"}),
    Role.PROPOSER: frozenset({"view", "capital.view", "proposal.create", "proposal.submit"}),
    Role.REVIEWER: frozenset({"view", "capital.view", "proposal.review"}),
    Role.OPERATOR: frozenset(
        {
            "view",
            "capital.view",
            "risk.decide",
            "authorization.issue",
            "order.prepare",
            "venue.record",
            "reconcile",
            "sender.manage",
            "risk.tighten",
        }
    ),
    Role.TREASURY_ADMIN: frozenset(
        {
            "capital.view",
            "capital.fact.record",
            "capital.propose",
            "capital.submit",
            "capital.review",
            "capital.authorize",
            "capital.execute",
            "capital.reconcile",
            "capital.policy.manage",
            "capital.automation.evaluate",
        }
    ),
    Role.SYSTEM_ADMIN: frozenset({"*"}),
}

MAX_ADD_UNITS: dict[RiskTier, int] = {
    RiskTier.LOW: 1,
    RiskTier.MEDIUM: 2,
    RiskTier.HIGH: 3,
}


def _reject(code: str, detail: str) -> NoReturn:
    raise DomainRejected(code, detail)


def _canonical(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _semantic_hash(value: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _advisory_lock_key(caller_id: str, operation: str, key: str) -> int:
    digest = hashlib.sha256(f"{caller_id}:{operation}:{key}".encode()).digest()[:8]
    return int.from_bytes(digest, byteorder="big", signed=True)


RISK_CAPACITY_LOCK_KEY = _advisory_lock_key("trading", "risk-capacity", "global")

OCCUPIED_CAPITAL_STATUSES = {
    CapitalTransferStatus.SOURCE_RESERVED.value,
    CapitalTransferStatus.SUBMITTED.value,
    CapitalTransferStatus.IN_FLIGHT.value,
    CapitalTransferStatus.DESTINATION_CONFIRMED.value,
    CapitalTransferStatus.UNKNOWN.value,
    CapitalTransferStatus.MANUAL_REQUIRED.value,
}


def _as_uuid(value: str) -> UUID:
    return UUID(value)


def _scope_key(environment: str, account_id: str, venue: str) -> str:
    return (
        f"{account_id}:{venue}"
        if environment == ExecutionEnvironment.SHADOW.value
        else f"{environment}:{account_id}:{venue}"
    )


def _scope_parts(execution_scope: str) -> tuple[ExecutionEnvironment, str, str]:
    parts = execution_scope.split(":")
    if len(parts) == 2:
        environment = ExecutionEnvironment.SHADOW
        account_id, venue = parts
    elif len(parts) == 3:
        try:
            environment = ExecutionEnvironment(parts[0])
        except ValueError:
            _reject("EXECUTION_SCOPE_INVALID", "execution scope environment is invalid")
        account_id, venue = parts[1:]
    else:
        _reject(
            "EXECUTION_SCOPE_INVALID",
            "execution scope must be account:venue or environment:account:venue",
        )
    if not account_id or not venue or account_id.strip() != account_id or venue.strip() != venue:
        _reject("EXECUTION_SCOPE_INVALID", "execution scope must contain non-empty exact parts")
    return environment, account_id, venue


class TradingService:
    """Single transactional entry point for the pre-production SHADOW trading core."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def _audit(
        self,
        session: Session,
        *,
        actor_id: str,
        event_type: str,
        object_type: str,
        object_id: UUID | str,
        reason: str,
        correlation_id: UUID,
        object_version: int,
        idempotency_key: str | None = None,
        now: datetime,
    ) -> None:
        session.add(
            AuditEvent(
                actor_id=actor_id,
                event_type=event_type,
                object_type=object_type,
                object_id=str(object_id),
                reason=reason,
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
                object_version=object_version,
                created_at=now,
            )
        )

    def _idempotency(
        self,
        session: Session,
        *,
        caller_id: str,
        operation: str,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> tuple[str, dict[str, Any] | None]:
        session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": _advisory_lock_key(caller_id, operation, idempotency_key)},
        )
        digest = _semantic_hash(payload)
        receipt = session.scalar(
            select(CommandReceipt).where(
                CommandReceipt.caller_id == caller_id,
                CommandReceipt.operation == operation,
                CommandReceipt.idempotency_key == idempotency_key,
            )
        )
        if receipt is None:
            return digest, None
        if receipt.semantic_hash != digest:
            raise IdempotencyConflict
        return digest, receipt.response

    @staticmethod
    def _save_receipt(
        session: Session,
        *,
        caller_id: str,
        operation: str,
        idempotency_key: str,
        semantic_hash: str,
        response: dict[str, Any],
        now: datetime,
    ) -> None:
        session.add(
            CommandReceipt(
                caller_id=caller_id,
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=semantic_hash,
                response=response,
                created_at=now,
            )
        )

    def _require_role(
        self,
        session: Session,
        user_id: UUID,
        action: str,
        account_id: str | None = None,
        venue: str | None = None,
    ) -> None:
        user = session.get(User, user_id)
        if user is None or not user.active:
            _reject("USER_NOT_AUTHORIZED", "user is missing or inactive")
        assignments = session.scalars(
            select(RoleAssignment).where(RoleAssignment.user_id == user_id)
        ).all()
        for assignment in assignments:
            role = Role(assignment.role)
            if action.startswith("capital.") and role is Role.SYSTEM_ADMIN:
                continue
            if action not in ROLE_ACTIONS[role] and "*" not in ROLE_ACTIONS[role]:
                continue
            if assignment.account_scope is not None and assignment.account_scope != account_id:
                continue
            if assignment.venue_scope is not None and assignment.venue_scope != venue:
                continue
            return
        _reject("RBAC_DENIED", f"{action} is not allowed in the requested scope")

    def can_user(
        self,
        user_id: UUID,
        action: str,
        account_id: str | None = None,
        venue: str | None = None,
    ) -> bool:
        with self.database.session_factory() as session:
            try:
                self._require_role(session, user_id, action, account_id, venue)
            except DomainRejected:
                return False
            return True

    def bootstrap_admin(self, username: str, *, now: datetime) -> UUID:
        with self.database.session_factory.begin() as session:
            if session.scalar(select(func.count()).select_from(User)) != 0:
                _reject("BOOTSTRAP_CLOSED", "an administrator already exists")
            user = User(
                username=username,
                principal_type=PrincipalType.HUMAN.value,
                active=True,
                created_at=now,
            )
            session.add(user)
            session.flush()
            session.add(
                RoleAssignment(
                    user_id=user.user_id,
                    role=Role.SYSTEM_ADMIN.value,
                    account_scope=None,
                    venue_scope=None,
                    created_at=now,
                )
            )
            self._audit(
                session,
                actor_id="bootstrap",
                event_type="USER_BOOTSTRAPPED",
                object_type="User",
                object_id=user.user_id,
                reason="first internal administrator",
                correlation_id=uuid4(),
                object_version=1,
                now=now,
            )
            return user.user_id

    def create_user(self, username: str, actor_id: UUID, *, now: datetime) -> UUID:
        with self.database.session_factory.begin() as session:
            self._require_role(session, actor_id, "user.manage")
            user = User(
                username=username,
                principal_type=PrincipalType.HUMAN.value,
                active=True,
                created_at=now,
            )
            session.add(user)
            session.flush()
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="USER_CREATED",
                object_type="User",
                object_id=user.user_id,
                reason="internal user created",
                correlation_id=uuid4(),
                object_version=1,
                now=now,
            )
            return user.user_id

    def bind_telegram_private_chat(
        self,
        *,
        internal_username: str,
        telegram_username: str,
        telegram_chat_id: str,
        now: datetime,
    ) -> str:
        """Bind one allowlisted private chat to one existing human user.

        The caller performs the Telegram username allowlist check. Once bound, all
        authorization uses the immutable numeric private-chat ID and current Trading RBAC.
        """

        with self.database.session_factory.begin() as session:
            user = session.scalar(
                select(User).where(
                    User.username == internal_username,
                    User.principal_type == PrincipalType.HUMAN.value,
                    User.active,
                )
            )
            if user is None:
                _reject(
                    "TELEGRAM_INTERNAL_USER_NOT_FOUND",
                    "the configured internal Telegram user is missing or inactive",
                )
            bound_to_chat = session.scalar(
                select(User).where(User.telegram_chat_id == telegram_chat_id)
            )
            if bound_to_chat is not None and bound_to_chat.user_id != user.user_id:
                _reject(
                    "TELEGRAM_BINDING_CONFLICT",
                    "the Telegram private chat is already bound to another internal user",
                )
            if user.telegram_chat_id is not None and user.telegram_chat_id != telegram_chat_id:
                _reject(
                    "TELEGRAM_BINDING_CONFLICT",
                    "the internal user already has another Telegram private chat",
                )
            if user.telegram_chat_id == telegram_chat_id:
                return user.username
            user.telegram_chat_id = telegram_chat_id
            self._audit(
                session,
                actor_id=str(user.user_id),
                event_type="TELEGRAM_PRIVATE_CHAT_BOUND",
                object_type="User",
                object_id=user.user_id,
                reason=f"allowlisted Telegram user @{telegram_username} completed private /start",
                correlation_id=uuid4(),
                object_version=1,
                now=now,
            )
            return user.username

    def create_service_principal(self, username: str, actor_id: UUID, *, now: datetime) -> UUID:
        with self.database.session_factory.begin() as session:
            self._require_role(session, actor_id, "user.manage")
            principal = User(
                username=username,
                principal_type=PrincipalType.SERVICE.value,
                active=True,
                created_at=now,
            )
            session.add(principal)
            session.flush()
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="SERVICE_PRINCIPAL_CREATED",
                object_type="User",
                object_id=principal.user_id,
                reason="internal strategy or service principal created",
                correlation_id=uuid4(),
                object_version=1,
                now=now,
            )
            return principal.user_id

    def record_perptape_feed(
        self,
        actor_id: UUID,
        feed: PerptapeFeedSnapshot,
        *,
        now: datetime,
        base_snapshot: PerptapeFeedSnapshot | None,
    ) -> int:
        validate_perptape_datetime(now)
        feed = bound_perptape_feed_snapshot(feed)
        if base_snapshot is not None:
            base_snapshot = bound_perptape_feed_snapshot(base_snapshot)
        validate_perptape_feed_payload(feed)
        if (
            feed.fetched_at > now + MAX_FACT_CLOCK_SKEW
            or feed.generated_at > feed.fetched_at + MAX_FACT_CLOCK_SKEW
            or feed.next_allowed_at < feed.generated_at
            or any(
                candidate.source_contract_version != feed.contract_version
                for candidate in feed.candidates
            )
        ):
            _reject("PERPTAPE_RESPONSE_INVALID", "Perptape feed metadata is inconsistent")
        with self.database.session_factory.begin() as session:
            self._require_role(session, actor_id, "proposal.create")
            current = session.get(PerptapeFeed, "BREAKOUTS", with_for_update=True)
            current_feed = (
                None
                if current is None
                else PerptapeFeedSnapshot(
                    contract_version=current.contract_version,
                    generated_at=current.generated_at,
                    fetched_at=current.fetched_at,
                    next_allowed_at=current.next_allowed_at,
                    candidates=tuple(
                        PerptapeCandidate.from_dict(value) for value in current.candidates
                    ),
                )
            )
            if current is None and base_snapshot is not None:
                _reject(
                    "PERPTAPE_FEED_CONFLICT",
                    "the base Perptape snapshot no longer exists",
                )
            feed = apply_perptape_feed_delta(
                base=base_snapshot,
                current=current_feed,
                incoming=feed,
            )
            validate_perptape_feed_payload(feed)
            if current_feed is not None and perptape_snapshot_identity(
                current_feed
            ) == perptape_snapshot_identity(feed):
                assert current is not None
                return current.version
            if current is not None and feed.fetched_at <= current.fetched_at:
                feed = replace(
                    feed,
                    fetched_at=current.fetched_at + timedelta(microseconds=1),
                )
            if (
                feed.fetched_at > now + MAX_FACT_CLOCK_SKEW
                or feed.generated_at > feed.fetched_at + MAX_FACT_CLOCK_SKEW
                or feed.next_allowed_at < feed.generated_at
            ):
                _reject(
                    "PERPTAPE_RESPONSE_INVALID",
                    "merged Perptape feed metadata is inconsistent",
                )
            candidates = [candidate.to_dict() for candidate in feed.candidates]
            if current is None:
                current = PerptapeFeed(
                    feed_key="BREAKOUTS",
                    contract_version=feed.contract_version,
                    candidates=candidates,
                    generated_at=feed.generated_at,
                    fetched_at=feed.fetched_at,
                    next_allowed_at=feed.next_allowed_at,
                    version=1,
                    updated_at=now,
                )
                session.add(current)
            else:
                current.contract_version = feed.contract_version
                current.candidates = candidates
                current.generated_at = feed.generated_at
                current.fetched_at = feed.fetched_at
                current.next_allowed_at = feed.next_allowed_at
                current.version += 1
                current.updated_at = now
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="PERPTAPE_FEED_RECORDED",
                object_type="PerptapeFeed",
                object_id=current.feed_key,
                reason=f"{len(candidates)} candidates",
                correlation_id=uuid4(),
                object_version=current.version,
                now=now,
            )
            return current.version

    def assign_role(
        self,
        user_id: UUID,
        role: Role,
        actor_id: UUID,
        account_scope: str | None = None,
        venue_scope: str | None = None,
        *,
        now: datetime,
    ) -> UUID:
        with self.database.session_factory.begin() as session:
            self._require_role(session, actor_id, "role.manage")
            if session.get(User, user_id) is None:
                _reject("USER_NOT_FOUND", "role target does not exist")
            assignment = RoleAssignment(
                user_id=user_id,
                role=role.value,
                account_scope=account_scope,
                venue_scope=venue_scope,
                created_at=now,
            )
            session.add(assignment)
            session.flush()
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="ROLE_ASSIGNED",
                object_type="RoleAssignment",
                object_id=assignment.assignment_id,
                reason=f"{role.value} assigned",
                correlation_id=uuid4(),
                object_version=1,
                now=now,
            )
            return assignment.assignment_id

    def register_instrument(
        self,
        *,
        actor_id: UUID,
        venue: str,
        symbol: str,
        tick_size: Decimal,
        lot_size: Decimal,
        minimum_notional: Decimal,
        contract_multiplier: Decimal,
        quote_currency: str,
        collateral_currency: str,
        protection_supported: bool,
        now: datetime,
    ) -> UUID:
        with self.database.session_factory.begin() as session:
            self._require_role(session, actor_id, "instrument.manage")
            instrument = Instrument(
                venue=venue,
                symbol=symbol,
                tick_size=tick_size,
                lot_size=lot_size,
                minimum_notional=minimum_notional,
                contract_multiplier=contract_multiplier,
                quote_currency=quote_currency,
                collateral_currency=collateral_currency,
                active=True,
                protection_supported=protection_supported,
                updated_at=now,
            )
            session.add(instrument)
            session.flush()
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="INSTRUMENT_REGISTERED",
                object_type="Instrument",
                object_id=instrument.instrument_id,
                reason=f"{venue}:{symbol}",
                correlation_id=uuid4(),
                object_version=1,
                now=now,
            )
            return instrument.instrument_id

    def set_risk_policy(
        self,
        *,
        actor_id: UUID,
        version: str,
        system_state: SystemRiskState,
        max_total_risk: Decimal,
        max_fact_age: timedelta,
        now: datetime,
    ) -> UUID:
        with self.database.session_factory.begin() as session:
            self._require_role(session, actor_id, "risk_policy.manage")
            if max_total_risk <= 0 or max_fact_age <= timedelta(0):
                _reject("RISK_POLICY_INVALID", "risk capacity and fact age must be positive")
            session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": RISK_CAPACITY_LOCK_KEY},
            )
            for current in session.scalars(select(RiskPolicy).where(RiskPolicy.active)).all():
                current.active = False
            policy = RiskPolicy(
                version=version,
                system_state=system_state.value,
                max_total_risk=max_total_risk,
                max_fact_age_seconds=int(max_fact_age.total_seconds()),
                active=True,
                updated_by=str(actor_id),
                updated_at=now,
            )
            session.add(policy)
            session.flush()
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="RISK_POLICY_SET",
                object_type="RiskPolicy",
                object_id=policy.policy_id,
                reason=system_state.value,
                correlation_id=uuid4(),
                object_version=1,
                now=now,
            )
            return policy.policy_id

    def create_proposal(
        self,
        *,
        actor_id: UUID,
        source: ProposalSource,
        risk_tier: RiskTier,
        account_id: str,
        venue: str,
        instrument_id: UUID,
        direction: Direction,
        quantity: Decimal,
        max_risk: Decimal,
        expires_at: datetime,
        idempotency_key: str,
        strategy_id: str | None = None,
        strategy_version: str | None = None,
        environment: ExecutionEnvironment = ExecutionEnvironment.SHADOW,
        source_candidate_id: str | None = None,
        source_link: str | None = None,
        source_observed_at: datetime | None = None,
        source_readiness: str | None = None,
        details: dict[str, Any] | None = None,
        idempotency_payload: dict[str, Any] | None = None,
        now: datetime,
    ) -> UUID:
        payload = {
            "source": source.value,
            "risk_tier": risk_tier.value,
            "account_id": account_id,
            "venue": venue,
            "instrument_id": str(instrument_id),
            "direction": direction.value,
            "quantity": str(quantity),
            "max_risk": str(max_risk),
            "expires_at": expires_at.isoformat(),
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "environment": environment.value,
            "source_candidate_id": source_candidate_id,
            "source_link": source_link,
            "source_observed_at": (
                None if source_observed_at is None else source_observed_at.isoformat()
            ),
            "source_readiness": source_readiness,
            "details": details or {},
        }
        operation = "proposal.create"
        with self.database.session_factory.begin() as session:
            self._require_role(session, actor_id, operation, account_id, venue)
            digest, response = self._idempotency(
                session,
                caller_id=str(actor_id),
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload if idempotency_payload is None else idempotency_payload,
            )
            if response is not None:
                return _as_uuid(str(response["proposal_id"]))
            principal = session.get(User, actor_id)
            if principal is None:
                _reject("USER_NOT_AUTHORIZED", "proposal principal does not exist")
            if source is ProposalSource.MANUAL:
                if principal.principal_type != PrincipalType.HUMAN.value:
                    _reject("PROPOSAL_SOURCE_INVALID", "MANUAL proposals require a human")
                if strategy_id is not None or strategy_version is not None:
                    _reject("PROPOSAL_STRATEGY_INVALID", "MANUAL proposals do not bind a strategy")
                if source_candidate_id is not None:
                    _reject(
                        "PROPOSAL_SOURCE_INVALID",
                        "MANUAL proposals cannot bind a source candidate",
                    )
            elif (
                principal.principal_type != PrincipalType.SERVICE.value
                or not strategy_id
                or not strategy_version
                or not source_candidate_id
                or source_observed_at is None
            ):
                _reject(
                    "PROPOSAL_SOURCE_INVALID",
                    "SYSTEM proposals require a service principal, strategy version and candidate",
                )
            instrument = session.get(Instrument, instrument_id)
            if instrument is None or not instrument.active or instrument.venue != venue:
                _reject("INSTRUMENT_UNAVAILABLE", "instrument is inactive or outside venue scope")
            if expires_at <= now:
                _reject("PROPOSAL_EXPIRY_INVALID", "proposal expiry must be in the future")
            correlation_id = uuid4()
            proposal = Proposal(
                source=source.value,
                environment=environment.value,
                proposer_id=actor_id,
                strategy_id=strategy_id,
                strategy_version=strategy_version,
                source_candidate_id=source_candidate_id,
                source_link=source_link,
                source_observed_at=source_observed_at,
                source_readiness=source_readiness,
                status=ProposalStatus.DRAFT.value,
                version=1,
                risk_tier=risk_tier.value,
                account_id=account_id,
                venue=venue,
                instrument_id=instrument_id,
                direction=direction.value,
                quantity=quantity,
                max_risk=max_risk,
                frozen_payload=payload,
                semantic_hash=digest,
                frozen_at=None,
                expires_at=expires_at,
                correlation_id=correlation_id,
                created_at=now,
                updated_at=now,
            )
            session.add(proposal)
            session.flush()
            result = {"proposal_id": str(proposal.proposal_id)}
            self._save_receipt(
                session,
                caller_id=str(actor_id),
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="PROPOSAL_CREATED",
                object_type="Proposal",
                object_id=proposal.proposal_id,
                reason=source.value,
                correlation_id=correlation_id,
                object_version=proposal.version,
                idempotency_key=idempotency_key,
                now=now,
            )
            return proposal.proposal_id

    def submit_proposal(self, proposal_id: UUID, actor_id: UUID, *, now: datetime) -> None:
        expired = False
        with self.database.session_factory.begin() as session:
            proposal = session.get(Proposal, proposal_id, with_for_update=True)
            if proposal is None:
                _reject("PROPOSAL_NOT_FOUND", "proposal does not exist")
            self._require_role(
                session, actor_id, "proposal.submit", proposal.account_id, proposal.venue
            )
            if proposal.proposer_id != actor_id:
                _reject("PROPOSAL_OWNER_REQUIRED", "only the proposer may submit the draft")
            if proposal.expires_at <= now:
                proposal.status = ProposalStatus.EXPIRED.value
                proposal.updated_at = now
                proposal.version += 1
                self._audit(
                    session,
                    actor_id=str(actor_id),
                    event_type="PROPOSAL_EXPIRED",
                    object_type="Proposal",
                    object_id=proposal.proposal_id,
                    reason="expired before submission",
                    correlation_id=proposal.correlation_id,
                    object_version=proposal.version,
                    now=now,
                )
                expired = True
            elif proposal.status != ProposalStatus.DRAFT.value:
                _reject("PROPOSAL_NOT_DRAFT", "only a draft can be submitted")
            else:
                proposal.status = ProposalStatus.PENDING_REVIEW.value
                proposal.frozen_at = now
                proposal.updated_at = now
                proposal.version += 1
                self._audit(
                    session,
                    actor_id=str(actor_id),
                    event_type="PROPOSAL_SUBMITTED",
                    object_type="Proposal",
                    object_id=proposal.proposal_id,
                    reason="frozen for review",
                    correlation_id=proposal.correlation_id,
                    object_version=proposal.version,
                    now=now,
                )
        if expired:
            _reject("PROPOSAL_EXPIRED", "proposal expired before submission")

    def review_proposal(
        self,
        proposal_id: UUID,
        reviewer_id: UUID,
        decision: ReviewDecision,
        reason: str,
        expected_version: int | None = None,
        *,
        now: datetime,
    ) -> ProposalStatus:
        expired = False
        result: ProposalStatus | None = None
        with self.database.session_factory.begin() as session:
            proposal = session.get(Proposal, proposal_id, with_for_update=True)
            if proposal is None:
                _reject("PROPOSAL_NOT_FOUND", "proposal does not exist")
            if expected_version is not None and proposal.version != expected_version:
                _reject("VERSION_CONFLICT", "proposal version changed; refresh before reviewing")
            if proposal.proposer_id == reviewer_id:
                _reject("SELF_REVIEW_FORBIDDEN", "a proposer cannot review the same proposal")
            self._require_role(
                session, reviewer_id, "proposal.review", proposal.account_id, proposal.venue
            )
            if proposal.expires_at <= now:
                proposal.status = ProposalStatus.EXPIRED.value
                proposal.updated_at = now
                proposal.version += 1
                self._audit(
                    session,
                    actor_id=str(reviewer_id),
                    event_type="PROPOSAL_EXPIRED",
                    object_type="Proposal",
                    object_id=proposal.proposal_id,
                    reason="expired before review",
                    correlation_id=proposal.correlation_id,
                    object_version=proposal.version,
                    now=now,
                )
                expired = True
            else:
                if proposal.status != ProposalStatus.PENDING_REVIEW.value:
                    _reject("PROPOSAL_NOT_REVIEWABLE", "proposal is not pending review")
                duplicate = session.scalar(
                    select(Approval).where(
                        Approval.proposal_id == proposal_id,
                        Approval.reviewer_id == reviewer_id,
                    )
                )
                if duplicate is not None:
                    _reject("REVIEW_ALREADY_RECORDED", "reviewer already voted")
                session.add(
                    Approval(
                        proposal_id=proposal_id,
                        reviewer_id=reviewer_id,
                        decision=decision.value,
                        reason=reason,
                        created_at=now,
                    )
                )
                session.flush()
                if decision is ReviewDecision.REJECT:
                    proposal.status = ProposalStatus.REJECTED.value
                else:
                    approvals = session.execute(
                        select(func.count())
                        .select_from(Approval)
                        .where(
                            Approval.proposal_id == proposal_id,
                            Approval.decision == ReviewDecision.APPROVE.value,
                        )
                    ).scalar_one()
                    required = 2 if proposal.risk_tier == RiskTier.HIGH.value else 1
                    if approvals >= required:
                        proposal.status = ProposalStatus.APPROVED.value
                proposal.updated_at = now
                proposal.version += 1
                self._audit(
                    session,
                    actor_id=str(reviewer_id),
                    event_type="PROPOSAL_REVIEWED",
                    object_type="Proposal",
                    object_id=proposal_id,
                    reason=f"{decision.value}: {reason}",
                    correlation_id=proposal.correlation_id,
                    object_version=proposal.version,
                    now=now,
                )
                result = ProposalStatus(proposal.status)
        if expired:
            _reject("PROPOSAL_EXPIRED", "proposal expired before review")
        if result is None:
            raise RuntimeError("proposal review completed without a result")
        return result

    @staticmethod
    def _lock_risk_capacity(session: Session) -> None:
        session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": RISK_CAPACITY_LOCK_KEY},
        )

    @staticmethod
    def _occupied_risk(session: Session) -> Decimal:
        reservations = session.scalars(
            select(RiskReservation)
            .where(RiskReservation.status.in_(OCCUPIED_RESERVATION_STATUSES))
            .with_for_update()
        ).all()
        return sum((reservation.amount for reservation in reservations), Decimal(0))

    @staticmethod
    def _active_risk_policy(session: Session) -> RiskPolicy:
        policy = session.scalar(select(RiskPolicy).where(RiskPolicy.active).with_for_update())
        if policy is None:
            _reject("RISK_POLICY_MISSING", "no active risk policy exists")
        return policy

    def _managed_capital_context(
        self,
        session: Session,
        *,
        environment: str,
        now: datetime,
        max_age: timedelta,
    ) -> tuple[bool, Decimal, list[dict[str, Any]], datetime]:
        rows = session.scalars(
            select(AccountEquity)
            .where(AccountEquity.environment == environment)
            .order_by(
                AccountEquity.location_type,
                AccountEquity.venue,
                AccountEquity.account_id,
                AccountEquity.currency,
            )
            .with_for_update()
        ).all()
        if not rows:
            return False, Decimal(0), [], now
        known = True
        total = Decimal(0)
        data_as_of = now
        facts: list[dict[str, Any]] = []
        unit_prices: dict[tuple[str, str, str], Decimal] = {}
        for row in rows:
            valuation_time = row.observed_at
            unit_price: Decimal | None
            value: Decimal | None
            if row.currency.upper() in USD_STABLE_ASSETS:
                unit_price = Decimal(1)
                value = row.equity
            else:
                unit_price = row.valuation_price
                value = row.valuation_equity
                if row.valuation_observed_at is not None:
                    valuation_time = min(valuation_time, row.valuation_observed_at)
            control_known = row.location_type != "VAULT" or row.control_status == "CONTROLLED"
            row_known = (
                row.fact_status == FactStatus.KNOWN.value
                and control_known
                and unit_price is not None
                and unit_price > 0
                and value is not None
                and value >= 0
                and not self._fact_is_stale(valuation_time, now, max_age)
            )
            known = known and row_known
            if row_known:
                assert unit_price is not None and value is not None
                total += value
                unit_prices[(row.account_id, row.venue, row.currency)] = unit_price
            data_as_of = min(data_as_of, valuation_time)
            facts.append(
                {
                    "account_equity_id": str(row.account_equity_id),
                    "location_type": row.location_type,
                    "location_id": row.account_id,
                    "venue": row.venue,
                    "asset": row.currency,
                    "fact_status": row.fact_status,
                    "control_status": row.control_status,
                    "usd_value": None if not row_known else str(value),
                    "observed_at": row.observed_at.isoformat(),
                    "valuation_observed_at": (
                        None
                        if row.valuation_observed_at is None
                        else row.valuation_observed_at.isoformat()
                    ),
                }
            )

        occupied = session.scalars(
            select(CapitalTransfer).where(
                CapitalTransfer.environment == environment,
                CapitalTransfer.status.in_(OCCUPIED_CAPITAL_STATUSES),
            )
        ).all()
        occupied_usd = Decimal(0)
        for transfer in occupied:
            source_venue = (
                transfer.venue if transfer.direction == CapitalDirection.VENUE_TO_VAULT else "VAULT"
            )
            price = unit_prices.get((transfer.source_id, source_venue, transfer.asset))
            if price is None:
                known = False
                continue
            occupied_usd += transfer.reserved_amount * price
        total = max(Decimal(0), total - occupied_usd)
        facts.append(
            {
                "capital_transfer_reserved_usd": str(occupied_usd),
                "managed_capital_usd": str(total),
            }
        )
        return known and total > 0, total, facts, data_as_of

    def _server_risk_context(
        self,
        session: Session,
        *,
        proposal: Proposal,
        policy: RiskPolicy,
        kind: IntentKind,
        requested_quantity: Decimal,
        requested_risk: Decimal,
        current_risk: Decimal,
        now: datetime,
    ) -> tuple[RiskEvaluationInput, dict[str, Any], datetime, Decimal]:
        instrument = session.get(Instrument, proposal.instrument_id)
        if instrument is None or not instrument.active:
            _reject("INSTRUMENT_UNAVAILABLE", "proposal instrument is unavailable")
        position = session.scalar(
            select(Position)
            .where(
                Position.account_id == proposal.account_id,
                Position.venue == proposal.venue,
                Position.environment == proposal.environment,
                Position.instrument_id == proposal.instrument_id,
            )
            .with_for_update()
        )
        equity = session.scalar(
            select(AccountEquity)
            .where(
                AccountEquity.account_id == proposal.account_id,
                AccountEquity.venue == proposal.venue,
                AccountEquity.environment == proposal.environment,
                AccountEquity.currency == instrument.collateral_currency,
            )
            .with_for_update()
        )
        protection = None
        if position is not None:
            protection = session.scalar(
                select(ProtectionOrder)
                .where(ProtectionOrder.position_id == position.position_id)
                .with_for_update()
            )

        position_known = position is not None and position.fact_status == FactStatus.KNOWN.value
        venue_equity_known = (
            equity is not None
            and equity.fact_status == FactStatus.KNOWN.value
            and equity.currency == instrument.collateral_currency
        )
        max_age = timedelta(seconds=policy.max_fact_age_seconds)
        capital_known, managed_capital_usd, managed_facts, capital_as_of = (
            self._managed_capital_context(
                session,
                environment=proposal.environment,
                now=now,
                max_age=max_age,
            )
        )
        equity_known = venue_equity_known and capital_known
        protection_required = kind is IntentKind.ADD or (
            position_known and position is not None and position.quantity != 0
        )
        protection_known = not protection_required or (
            protection is not None
            and protection.status == ProtectionStatus.ACTIVE.value
            and protection.fully_covered
            and position is not None
            and protection.quantity >= abs(position.quantity)
        )
        observed_times = [fact.observed_at for fact in (position, equity) if fact is not None]
        if protection_required and protection is not None:
            observed_times.append(protection.observed_at)
        observed_times.append(capital_as_of)
        data_as_of = min(observed_times, default=now)
        raw_fact_age = now - data_as_of
        fact_age = (
            timedelta(0) if -MAX_FACT_CLOCK_SKEW <= raw_fact_age < timedelta(0) else raw_fact_age
        )
        inputs = RiskEvaluationInput(
            kind=kind,
            requested_quantity=requested_quantity,
            requested_risk=requested_risk,
            current_risk=current_risk,
            fact_age=fact_age,
            position_known=position_known,
            equity_known=equity_known,
            protection_known=protection_known,
        )
        facts = {
            "proposal_id": str(proposal.proposal_id),
            "proposal_version": proposal.version,
            "kind": kind.value,
            "requested_quantity": str(requested_quantity),
            "requested_risk": str(requested_risk),
            "current_risk": str(current_risk),
            "policy": {
                "policy_id": str(policy.policy_id),
                "version": policy.version,
                "system_state": policy.system_state,
                "max_total_risk": str(policy.max_total_risk),
                "max_fact_age_seconds": policy.max_fact_age_seconds,
            },
            "position": None
            if position is None
            else {
                "position_id": str(position.position_id),
                "quantity": str(position.quantity),
                "fact_status": position.fact_status,
                "observed_at": position.observed_at.isoformat(),
                "written_at": position.updated_at.isoformat(),
            },
            "equity": None
            if equity is None
            else {
                "account_equity_id": str(equity.account_equity_id),
                "fact_status": equity.fact_status,
                "currency": equity.currency,
                "observed_at": equity.observed_at.isoformat(),
                "written_at": equity.updated_at.isoformat(),
            },
            "managed_capital": {
                "known": capital_known,
                "total_usd": str(managed_capital_usd),
                "effective_max_total_risk": str(min(policy.max_total_risk, managed_capital_usd)),
                "facts": managed_facts,
            },
            "protection_required": protection_required,
            "protection": None
            if protection is None
            else {
                "protection_id": str(protection.protection_id),
                "status": protection.status,
                "quantity": str(protection.quantity),
                "fully_covered": protection.fully_covered,
                "observed_at": protection.observed_at.isoformat(),
                "written_at": protection.updated_at.isoformat(),
            },
            "data_as_of": data_as_of.isoformat(),
            "fact_age_seconds": str(fact_age.total_seconds()),
        }
        return (
            inputs,
            facts,
            data_as_of,
            min(policy.max_total_risk, managed_capital_usd),
        )

    def decide_risk(
        self,
        *,
        proposal_id: UUID,
        actor_id: UUID,
        kind: IntentKind,
        idempotency_key: str,
        now: datetime,
        requested_quantity: Decimal | None = None,
    ) -> UUID:
        operation = "risk.decide"
        with self.database.session_factory.begin() as session:
            proposal = session.get(Proposal, proposal_id, with_for_update=True)
            if proposal is None:
                _reject("PROPOSAL_NOT_FOUND", "proposal does not exist")
            self._require_role(session, actor_id, operation, proposal.account_id, proposal.venue)
            quantity = proposal.quantity if requested_quantity is None else requested_quantity
            request_payload = {
                "proposal_id": str(proposal_id),
                "kind": kind.value,
                "requested_quantity": str(quantity),
            }
            digest, response = self._idempotency(
                session,
                caller_id=str(actor_id),
                operation=operation,
                idempotency_key=idempotency_key,
                payload=request_payload,
            )
            if response is not None:
                return _as_uuid(str(response["decision_id"]))
            if proposal.status != ProposalStatus.APPROVED.value or proposal.expires_at <= now:
                _reject("PROPOSAL_NOT_APPROVED", "risk decision requires a live approved proposal")
            if quantity <= 0 or quantity > proposal.quantity:
                _reject("PROPOSAL_QUANTITY_EXCEEDED", "requested quantity exceeds proposal cap")

            self._lock_risk_capacity(session)
            policy = self._active_risk_policy(session)
            current_risk = self._occupied_risk(session)
            requested_risk = proposal.max_risk * quantity / proposal.quantity
            if requested_risk > proposal.max_risk:
                _reject("PROPOSAL_RISK_EXCEEDED", "requested risk exceeds proposal cap")
            inputs, facts, data_as_of, effective_max_total_risk = self._server_risk_context(
                session,
                proposal=proposal,
                policy=policy,
                kind=kind,
                requested_quantity=quantity,
                requested_risk=requested_risk,
                current_risk=current_risk,
                now=now,
            )
            outcome = evaluate_risk(
                RiskPolicyInput(
                    version=policy.version,
                    system_state=SystemRiskState(policy.system_state),
                    max_total_risk=(
                        effective_max_total_risk
                        if effective_max_total_risk > 0
                        else policy.max_total_risk
                    ),
                    max_fact_age=timedelta(seconds=policy.max_fact_age_seconds),
                ),
                inputs,
            )
            decision = RiskDecision(
                proposal_id=proposal_id,
                policy_id=policy.policy_id,
                input_data=facts,
                result=outcome.result.value,
                approved_quantity=outcome.allowed_quantity,
                risk_amount=outcome.allowed_risk,
                reasons=list(outcome.reasons),
                data_as_of=data_as_of,
                actor_id=str(actor_id),
                correlation_id=proposal.correlation_id,
                created_at=now,
            )
            session.add(decision)
            session.flush()
            result = {"decision_id": str(decision.decision_id)}
            self._save_receipt(
                session,
                caller_id=str(actor_id),
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="RISK_DECIDED",
                object_type="RiskDecision",
                object_id=decision.decision_id,
                reason=outcome.result.value,
                correlation_id=proposal.correlation_id,
                object_version=1,
                idempotency_key=idempotency_key,
                now=now,
            )
            RISK_RESULTS.labels(outcome.result.value).inc()
            return decision.decision_id

    def issue_authorization(
        self,
        *,
        proposal_id: UUID,
        actor_id: UUID,
        expires_at: datetime,
        allowed_adds: int,
        idempotency_key: str,
        now: datetime,
    ) -> UUID:
        payload = {
            "proposal_id": str(proposal_id),
            "expires_at": expires_at.isoformat(),
            "allowed_adds": allowed_adds,
        }
        operation = "authorization.issue"
        with self.database.session_factory.begin() as session:
            proposal = session.get(Proposal, proposal_id)
            if proposal is None:
                _reject("PROPOSAL_NOT_FOUND", "proposal does not exist")
            self._require_role(session, actor_id, operation, proposal.account_id, proposal.venue)
            digest, response = self._idempotency(
                session,
                caller_id=str(actor_id),
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if response is not None:
                return _as_uuid(str(response["authorization_id"]))
            if proposal.status != ProposalStatus.APPROVED.value:
                _reject("PROPOSAL_NOT_APPROVED", "authorization requires approved proposal")
            decision = session.scalar(
                select(RiskDecision)
                .where(RiskDecision.proposal_id == proposal_id)
                .order_by(RiskDecision.created_at.desc())
                .limit(1)
            )
            if decision is None or decision.result == RiskResult.DENY.value:
                _reject("RISK_DECISION_NOT_ALLOWING", "latest risk decision does not allow risk")
            if expires_at <= now or expires_at > proposal.expires_at:
                _reject("AUTHORIZATION_EXPIRY_INVALID", "authorization must be short-lived")
            details = proposal.frozen_payload.get("details")
            management = details if isinstance(details, dict) else {}
            requested_adds_raw = management.get("requested_adds", 0)
            try:
                requested_adds = int(requested_adds_raw)
            except (TypeError, ValueError):
                _reject(
                    "PROPOSAL_ADD_CONTRACT_INVALID",
                    "frozen proposal AddUnit request is invalid",
                )
            tier_limit = MAX_ADD_UNITS[RiskTier(proposal.risk_tier)]
            proposal_limit = min(requested_adds, tier_limit)
            if (
                allowed_adds < 0
                or allowed_adds > proposal_limit
                or (allowed_adds > 0 and management.get("allow_auto_add") is not True)
            ):
                _reject(
                    "AUTHORIZATION_ADD_LIMIT_INVALID",
                    "allowed Add count exceeds the frozen proposal and risk tier",
                )
            authorization = TradingAuthorization(
                proposal_id=proposal_id,
                risk_decision_id=decision.decision_id,
                account_id=proposal.account_id,
                venue=proposal.venue,
                environment=proposal.environment,
                instrument_id=proposal.instrument_id,
                direction=proposal.direction,
                quantity_limit=decision.approved_quantity,
                used_quantity=Decimal(0),
                risk_limit=decision.risk_amount,
                expires_at=expires_at,
                allowed_adds=allowed_adds,
                used_adds=0,
                active=True,
                actor_id=str(actor_id),
                created_at=now,
            )
            session.add(authorization)
            session.flush()
            result = {"authorization_id": str(authorization.authorization_id)}
            self._save_receipt(
                session,
                caller_id=str(actor_id),
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="AUTHORIZATION_ISSUED",
                object_type="TradingAuthorization",
                object_id=authorization.authorization_id,
                reason="approved proposal and risk decision",
                correlation_id=proposal.correlation_id,
                object_version=1,
                idempotency_key=idempotency_key,
                now=now,
            )
            return authorization.authorization_id

    @staticmethod
    def _intent_creation(response: dict[str, Any]) -> IntentCreation:
        return IntentCreation(
            campaign_id=_as_uuid(str(response["campaign_id"])),
            reservation_id=_as_uuid(str(response["reservation_id"])),
            intent_id=_as_uuid(str(response["intent_id"])),
        )

    @staticmethod
    def _proposal_limit_price(proposal: Proposal) -> Decimal | None:
        details = proposal.frozen_payload.get("details")
        if not isinstance(details, dict) or details.get("limit_price") is None:
            return None
        try:
            value = Decimal(str(details["limit_price"]))
        except (ArithmeticError, TypeError, ValueError):
            _reject("PROPOSAL_PRICE_INVALID", "frozen proposal limit price is invalid")
        if not value.is_finite() or value <= 0:
            _reject("PROPOSAL_PRICE_INVALID", "frozen proposal limit price is invalid")
        return value

    @staticmethod
    def _proposal_detail_decimal(proposal: Proposal, key: str) -> Decimal:
        details = proposal.frozen_payload.get("details")
        if not isinstance(details, dict) or details.get(key) is None:
            _reject(
                "PROPOSAL_ADD_CONTRACT_INVALID",
                f"frozen proposal is missing {key}",
            )
        try:
            value = Decimal(str(details[key]))
        except (ArithmeticError, TypeError, ValueError):
            _reject("PROPOSAL_ADD_CONTRACT_INVALID", f"frozen {key} is invalid")
        if not value.is_finite() or value <= 0:
            _reject("PROPOSAL_ADD_CONTRACT_INVALID", f"frozen {key} is invalid")
        return value

    @staticmethod
    def _validate_add_candidate(
        *,
        proposal: Proposal,
        instrument: Instrument,
        candidate: AddCandidateFacts | None,
        policy: RiskPolicy,
        now: datetime,
    ) -> None:
        details = proposal.frozen_payload.get("details")
        if not isinstance(details, dict) or details.get("allow_auto_add") is not True:
            _reject("PROPOSAL_AUTO_ADD_DISABLED", "frozen proposal does not allow AUTO_ADD")
        if candidate is None:
            _reject(
                "AUTO_ADD_CANDIDATE_REQUIRED",
                "ADD requires a current Perptape candidate at the Trading boundary",
            )
        if (
            candidate.readiness != "READY"
            or candidate.venue != proposal.venue
            or candidate.symbol != instrument.symbol
            or candidate.direction.value != proposal.direction
        ):
            _reject(
                "AUTO_ADD_CANDIDATE_SCOPE_INVALID",
                "Perptape candidate does not match the frozen Campaign scope",
            )
        if proposal.source == ProposalSource.SYSTEM.value and (
            candidate.contract_version != proposal.strategy_version
            or candidate.candidate_id == proposal.source_candidate_id
        ):
            _reject(
                "AUTO_ADD_CANDIDATE_VERSION_INVALID",
                "SYSTEM Add requires a new candidate from the frozen Perptape contract",
            )
        baseline = proposal.source_observed_at or proposal.frozen_at or proposal.created_at
        if proposal.source == ProposalSource.SYSTEM.value and candidate.observed_at <= baseline:
            _reject(
                "AUTO_ADD_CANDIDATE_NOT_SUBSEQUENT",
                "Add candidate must be newer than the frozen Proposal facts",
            )
        age = now - candidate.observed_at
        if age < timedelta(0) or age > timedelta(seconds=policy.max_fact_age_seconds):
            _reject("AUTO_ADD_CANDIDATE_STALE", "Add candidate is not a current fact")
        trigger_price = TradingService._proposal_detail_decimal(proposal, "add_trigger_price")
        if proposal.direction == Direction.LONG.value:
            passed = candidate.reference_price >= trigger_price
        else:
            passed = candidate.reference_price <= trigger_price
        if not passed:
            _reject(
                "AUTO_ADD_TRIGGER_NOT_MET",
                "Perptape candidate has not reached the frozen favorable-price gate",
            )

    def create_order_intent(
        self,
        authorization_id: UUID,
        actor_id: UUID,
        kind: IntentKind,
        account_id: str,
        venue: str,
        instrument_id: UUID,
        direction: Direction,
        quantity: Decimal,
        idempotency_key: str,
        *,
        add_candidate: AddCandidateFacts | None = None,
        now: datetime,
    ) -> IntentCreation:
        if kind not in {IntentKind.INITIAL, IntentKind.ADD}:
            _reject("NEW_RISK_INTENT_REQUIRED", "this entry point only creates INITIAL or ADD")
        payload = {
            "authorization_id": str(authorization_id),
            "kind": kind.value,
            "account_id": account_id,
            "venue": venue,
            "instrument_id": str(instrument_id),
            "direction": direction.value,
            "quantity": str(quantity),
            "add_candidate": None
            if add_candidate is None
            else {
                "candidate_id": add_candidate.candidate_id,
                "contract_version": add_candidate.contract_version,
                "venue": add_candidate.venue,
                "symbol": add_candidate.symbol,
                "direction": add_candidate.direction.value,
                "observed_at": add_candidate.observed_at.isoformat(),
                "reference_price": str(add_candidate.reference_price),
                "readiness": add_candidate.readiness,
            },
        }
        operation = "order.prepare"
        with self.database.session_factory.begin() as session:
            digest, response = self._idempotency(
                session,
                caller_id=str(actor_id),
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if response is not None:
                return self._intent_creation(response)
            authorization = session.get(
                TradingAuthorization, authorization_id, with_for_update=True
            )
            if authorization is None or not authorization.active:
                _reject("AUTHORIZATION_INACTIVE", "authorization is missing or inactive")
            if authorization.expires_at <= now:
                _reject("AUTHORIZATION_EXPIRED", "authorization expired")
            if (
                authorization.account_id != account_id
                or authorization.venue != venue
                or authorization.instrument_id != instrument_id
                or authorization.direction != direction.value
            ):
                _reject("AUTHORIZATION_SCOPE_MISMATCH", "request exceeds frozen scope")
            self._require_role(session, actor_id, operation, account_id, venue)
            if (
                quantity <= 0
                or authorization.used_quantity + quantity > authorization.quantity_limit
            ):
                _reject("AUTHORIZATION_QUANTITY_EXCEEDED", "request exceeds quantity limit")
            proposal = session.get(Proposal, authorization.proposal_id, with_for_update=True)
            if proposal is None or proposal.status != ProposalStatus.APPROVED.value:
                _reject("PROPOSAL_NOT_APPROVED", "authorization proposal is not approved")
            if proposal.expires_at <= now:
                _reject("PROPOSAL_EXPIRED", "authorization proposal expired")
            if (
                authorization.quantity_limit > proposal.quantity
                or authorization.risk_limit > proposal.max_risk
            ):
                _reject("AUTHORIZATION_SCOPE_MISMATCH", "authorization exceeds proposal caps")

            self._lock_risk_capacity(session)
            policy = self._active_risk_policy(session)
            occupied_risk = self._occupied_risk(session)
            risk_amount = authorization.risk_limit * quantity / authorization.quantity_limit
            if risk_amount <= 0 or risk_amount > authorization.risk_limit:
                _reject("AUTHORIZATION_RISK_EXCEEDED", "request exceeds risk authorization")
            inputs, _, _, effective_max_total_risk = self._server_risk_context(
                session,
                proposal=proposal,
                policy=policy,
                kind=kind,
                requested_quantity=quantity,
                requested_risk=risk_amount,
                current_risk=occupied_risk,
                now=now,
            )
            final_outcome = evaluate_risk(
                RiskPolicyInput(
                    version=policy.version,
                    system_state=SystemRiskState(policy.system_state),
                    max_total_risk=(
                        effective_max_total_risk
                        if effective_max_total_risk > 0
                        else policy.max_total_risk
                    ),
                    max_fact_age=timedelta(seconds=policy.max_fact_age_seconds),
                ),
                inputs,
            )
            if final_outcome.result is not RiskResult.ALLOW:
                reason = final_outcome.reasons[0] if final_outcome.reasons else "RISK_REJECTED"
                _reject("FINAL_RISK_CHECK_FAILED", reason)

            position = session.scalar(
                select(Position)
                .where(
                    Position.account_id == account_id,
                    Position.venue == venue,
                    Position.environment == proposal.environment,
                    Position.instrument_id == instrument_id,
                )
                .with_for_update()
            )
            if position is None or position.fact_status != FactStatus.KNOWN.value:
                _reject("POSITION_UNKNOWN", "new risk requires a current known position fact")

            campaign = session.scalar(
                select(Campaign)
                .where(Campaign.authorization_id == authorization_id)
                .with_for_update()
            )
            if kind is IntentKind.INITIAL:
                if position.quantity != 0:
                    _reject("POSITION_NOT_FLAT", "INITIAL requires a confirmed flat position")
                conflicting_campaign = session.scalar(
                    select(Campaign)
                    .where(
                        Campaign.account_id == account_id,
                        Campaign.venue == venue,
                        Campaign.environment == proposal.environment,
                        Campaign.instrument_id == instrument_id,
                        Campaign.status != CampaignStatus.CLOSED.value,
                    )
                    .with_for_update()
                )
                if conflicting_campaign is not None and (
                    campaign is None or conflicting_campaign.campaign_id != campaign.campaign_id
                ):
                    _reject("ACTIVE_CAMPAIGN_EXISTS", "scope already has an unclosed campaign")
                if campaign is None:
                    campaign = Campaign(
                        proposal_id=authorization.proposal_id,
                        authorization_id=authorization_id,
                        account_id=authorization.account_id,
                        venue=authorization.venue,
                        environment=proposal.environment,
                        instrument_id=authorization.instrument_id,
                        direction=authorization.direction,
                        status=CampaignStatus.OPENING.value,
                        current_target_quantity=authorization.quantity_limit,
                        target_version=0,
                        target_reason=None,
                        target_urgency=None,
                        target_calculated_at=None,
                        realized_pnl=Decimal(0),
                        unrealized_pnl=Decimal(0),
                        final_pnl=Decimal(0),
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(campaign)
                    session.flush()
            else:
                gate = session.get(CapabilityGate, "AUTO_ADD", with_for_update=True)
                if gate is None or gate.status != CapabilityStatus.ENABLED.value:
                    _reject("AUTO_ADD_DISABLED", "automatic add capability is disabled")
                instrument = session.get(Instrument, proposal.instrument_id)
                if instrument is None:
                    _reject("INSTRUMENT_UNAVAILABLE", "proposal instrument is unavailable")
                self._validate_add_candidate(
                    proposal=proposal,
                    instrument=instrument,
                    candidate=add_candidate,
                    policy=policy,
                    now=now,
                )
                if policy.system_state != SystemRiskState.NORMAL.value:
                    _reject("ADD_RISK_STATE_INVALID", "ADD requires NORMAL risk state")
                if authorization.used_adds >= authorization.allowed_adds:
                    _reject("ADD_LIMIT_EXHAUSTED", "authorization add count is exhausted")
                if campaign is None or campaign.status in {
                    CampaignStatus.CLOSED.value,
                    CampaignStatus.UNKNOWN.value,
                }:
                    _reject("ADD_CAMPAIGN_REQUIRED", "ADD requires an existing known campaign")
                expected_long = campaign.direction == Direction.LONG.value
                if position.quantity == 0 or (position.quantity > 0) != expected_long:
                    _reject("ADD_POSITION_INVALID", "ADD requires an existing aligned position")
                unrealized_pnl = (
                    position.mark_price - position.average_entry_price
                ) * position.quantity
                if unrealized_pnl <= 0:
                    _reject("ADD_NOT_PROFITABLE", "ADD requires strictly positive unrealized PnL")

            active = session.scalar(
                select(OrderIntent.intent_id).where(
                    OrderIntent.campaign_id == campaign.campaign_id,
                    OrderIntent.status.in_(ACTIVE_INTENT_STATUSES),
                )
            )
            if active is not None:
                _reject("ACTIVE_ORDER_INTENT", "campaign already has unresolved intent")
            if occupied_risk + risk_amount > policy.max_total_risk:
                _reject("RISK_CAPACITY_EXHAUSTED", "atomic risk capacity is exhausted")
            reservation = RiskReservation(
                campaign_id=campaign.campaign_id,
                authorization_id=authorization_id,
                status=ReservationStatus.RESERVED.value,
                amount=risk_amount,
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(reservation)
            session.flush()
            side = "BUY" if direction is Direction.LONG else "SELL"
            intent = OrderIntent(
                campaign_id=campaign.campaign_id,
                authorization_id=authorization_id,
                reservation_id=reservation.reservation_id,
                kind=kind.value,
                side=side,
                quantity=quantity,
                limit_price=self._proposal_limit_price(proposal),
                reduce_only=False,
                trigger_source=(
                    None if add_candidate is None else f"PERPTAPE:{add_candidate.candidate_id}"
                ),
                trigger_observed_at=(None if add_candidate is None else add_candidate.observed_at),
                add_unit_consumed=False,
                target_version=None,
                position_id=position.position_id,
                position_observed_at=position.observed_at,
                status=OrderIntentStatus.READY.value,
                semantic_hash=digest,
                actor_id=str(actor_id),
                correlation_id=uuid4(),
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(intent)
            authorization.used_quantity += quantity
            session.flush()
            result = {
                "campaign_id": str(campaign.campaign_id),
                "reservation_id": str(reservation.reservation_id),
                "intent_id": str(intent.intent_id),
            }
            self._save_receipt(
                session,
                caller_id=str(actor_id),
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="ORDER_INTENT_PREPARED",
                object_type="OrderIntent",
                object_id=intent.intent_id,
                reason=kind.value,
                correlation_id=intent.correlation_id,
                object_version=intent.version,
                idempotency_key=idempotency_key,
                now=now,
            )
            INTENT_TRANSITIONS.labels("CREATED", intent.status).inc()
            return IntentCreation(
                campaign_id=campaign.campaign_id,
                reservation_id=reservation.reservation_id,
                intent_id=intent.intent_id,
            )

    @staticmethod
    def _consume_add_unit(session: Session, intent: OrderIntent) -> None:
        if intent.kind != IntentKind.ADD.value or intent.add_unit_consumed:
            return
        authorization = session.get(
            TradingAuthorization, intent.authorization_id, with_for_update=True
        )
        if authorization is None or authorization.used_adds >= authorization.allowed_adds:
            _reject(
                "AUTHORIZATION_ADD_LIMIT_INVALID",
                "positive Add execution exceeds the authorized AddUnit count",
            )
        authorization.used_adds += 1
        intent.add_unit_consumed = True

    def mark_intent_unknown(
        self, intent_id: UUID, actor_id: UUID, reason: str, *, now: datetime
    ) -> None:
        with self.database.session_factory.begin() as session:
            intent = session.get(OrderIntent, intent_id, with_for_update=True)
            if intent is None:
                _reject("ORDER_INTENT_NOT_FOUND", "intent does not exist")
            campaign = session.get(Campaign, intent.campaign_id)
            if campaign is None:
                _reject("CAMPAIGN_NOT_FOUND", "intent campaign does not exist")
            self._require_role(
                session, actor_id, "order.prepare", campaign.account_id, campaign.venue
            )
            if intent.status == OrderIntentStatus.UNKNOWN.value:
                return
            if intent.status not in ACTIVE_INTENT_STATUSES:
                _reject("ORDER_INTENT_NOT_ACTIVE", "only an active intent may become UNKNOWN")
            previous = intent.status
            intent.status = OrderIntentStatus.UNKNOWN.value
            intent.updated_at = now
            intent.version += 1
            if intent.reservation_id is not None:
                reservation = session.get(RiskReservation, intent.reservation_id)
                if reservation is not None:
                    reservation.status = ReservationStatus.UNKNOWN.value
                    reservation.updated_at = now
                    reservation.version += 1
            venue_order = session.scalar(
                select(VenueOrder).where(VenueOrder.order_intent_id == intent_id)
            )
            if venue_order is not None:
                venue_order.status = VenueOrderStatus.UNKNOWN.value
                venue_order.observed_at = now
                venue_order.updated_at = now
            campaign.status = CampaignStatus.UNKNOWN.value
            campaign.updated_at = now
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="ORDER_INTENT_UNKNOWN",
                object_type="OrderIntent",
                object_id=intent.intent_id,
                reason=reason,
                correlation_id=intent.correlation_id,
                object_version=intent.version,
                now=now,
            )
            INTENT_TRANSITIONS.labels(previous, intent.status).inc()

    def release_unfilled_intent(
        self,
        intent_id: UUID,
        actor_id: UUID,
        terminal_status: OrderIntentStatus,
        reason: str,
        *,
        now: datetime,
    ) -> None:
        if terminal_status not in {OrderIntentStatus.CANCELLED, OrderIntentStatus.REJECTED}:
            _reject("INVALID_TERMINAL_STATUS", "only cancelled or rejected may release risk")
        with self.database.session_factory.begin() as session:
            intent = session.get(OrderIntent, intent_id, with_for_update=True)
            if intent is None:
                _reject("ORDER_INTENT_NOT_FOUND", "intent does not exist")
            campaign = session.get(Campaign, intent.campaign_id)
            if campaign is None:
                _reject("CAMPAIGN_NOT_FOUND", "intent campaign does not exist")
            self._require_role(
                session, actor_id, "order.prepare", campaign.account_id, campaign.venue
            )
            reservation = (
                session.get(RiskReservation, intent.reservation_id, with_for_update=True)
                if intent.reservation_id is not None
                else None
            )
            if intent.status == terminal_status.value:
                if reservation is None or reservation.status == ReservationStatus.RELEASED.value:
                    return
                _reject("RISK_RELEASE_INCOMPLETE", "terminal intent still occupies risk")
            if intent.status in {
                OrderIntentStatus.CANCELLED.value,
                OrderIntentStatus.REJECTED.value,
                OrderIntentStatus.FILLED.value,
            }:
                _reject("ORDER_INTENT_TERMINAL", "terminal intent cannot change outcome")
            if intent.status not in RELEASABLE_INTENT_STATUSES:
                _reject("ORDER_INTENT_NOT_RELEASABLE", "unknown intent cannot release risk")
            filled = session.scalar(
                select(func.coalesce(func.sum(VenueFill.quantity), 0)).where(
                    VenueFill.order_intent_id == intent_id
                )
            )
            if filled != 0:
                _reject("FILLED_INTENT_RISK_REQUIRED", "filled intent cannot release all risk")
            previous = intent.status
            intent.status = terminal_status.value
            intent.updated_at = now
            intent.version += 1
            if reservation is not None:
                if reservation.status == ReservationStatus.UNKNOWN.value:
                    _reject("RISK_RESERVATION_UNKNOWN", "unknown risk cannot be released")
                if reservation.status != ReservationStatus.RELEASED.value:
                    reservation.status = ReservationStatus.RELEASED.value
                    reservation.updated_at = now
                    reservation.version += 1
                    authorization = session.get(
                        TradingAuthorization, intent.authorization_id, with_for_update=True
                    )
                    if authorization is None or authorization.used_quantity < intent.quantity:
                        _reject(
                            "AUTHORIZATION_USAGE_INVALID", "authorization usage is inconsistent"
                        )
                    authorization.used_quantity -= intent.quantity
            venue_order = session.scalar(
                select(VenueOrder).where(VenueOrder.order_intent_id == intent_id)
            )
            if venue_order is not None:
                venue_order.status = (
                    VenueOrderStatus.CANCELLED.value
                    if terminal_status is OrderIntentStatus.CANCELLED
                    else VenueOrderStatus.REJECTED.value
                )
                venue_order.observed_at = now
                venue_order.updated_at = now
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="ORDER_INTENT_TERMINATED",
                object_type="OrderIntent",
                object_id=intent.intent_id,
                reason=reason,
                correlation_id=intent.correlation_id,
                object_version=intent.version,
                now=now,
            )
            INTENT_TRANSITIONS.labels(previous, intent.status).inc()

    def acquire_sender(
        self,
        execution_scope: str,
        owner_id: str,
        actor_id: UUID,
        now: datetime,
        lease_duration: timedelta = timedelta(minutes=1),
    ) -> int:
        with self.database.session_factory.begin() as session:
            _environment, account_id, venue = _scope_parts(execution_scope)
            if not owner_id or lease_duration <= timedelta(0):
                _reject("SENDER_LEASE_INVALID", "owner and positive lease duration are required")
            self._require_role(session, actor_id, "sender.manage", account_id, venue)
            lease = session.get(SenderLease, execution_scope, with_for_update=True)
            if lease is None:
                token = 1
                session.add(
                    SenderLease(
                        execution_scope=execution_scope,
                        owner_id=owner_id,
                        fencing_token=token,
                        expires_at=now + lease_duration,
                        updated_at=now,
                    )
                )
            elif lease.owner_id == owner_id and lease.expires_at > now:
                token = lease.fencing_token
                lease.expires_at = now + lease_duration
                lease.updated_at = now
            else:
                if lease.expires_at > now:
                    _reject("SENDER_LEASE_HELD", "another sender still owns the live lease")
                latest = session.scalar(
                    select(ReconciliationRun)
                    .where(ReconciliationRun.execution_scope == execution_scope)
                    .order_by(ReconciliationRun.completed_at.desc())
                    .limit(1)
                )
                policy = session.scalar(select(RiskPolicy).where(RiskPolicy.active))
                max_age = (
                    timedelta(seconds=policy.max_fact_age_seconds)
                    if policy is not None
                    else timedelta(0)
                )
                if (
                    latest is None
                    or policy is None
                    or latest.status != ReconciliationStatus.MATCH.value
                    or not latest.is_computed
                    or latest.completed_at <= lease.expires_at
                    or latest.completed_at > now
                    or now - latest.completed_at > max_age
                ):
                    _reject(
                        "RECONCILIATION_REQUIRED",
                        "sender takeover requires a fresh computed MATCH after lease expiry",
                    )
                token = lease.fencing_token + 1
                lease.owner_id = owner_id
                lease.fencing_token = token
                lease.expires_at = now + lease_duration
                lease.updated_at = now
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="SENDER_LEASE_ACQUIRED",
                object_type="SenderLease",
                object_id=execution_scope,
                reason=owner_id,
                correlation_id=uuid4(),
                object_version=token,
                now=now,
            )
            return token

    def _validate_sender(
        self,
        session: Session,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        now: datetime,
    ) -> None:
        _scope_parts(execution_scope)
        lease = session.get(SenderLease, execution_scope)
        if (
            lease is None
            or lease.owner_id != owner_id
            or lease.fencing_token != fencing_token
            or lease.expires_at <= now
        ):
            FENCING_REJECTIONS.inc()
            _reject("FENCING_TOKEN_REJECTED", "sender lease is stale, expired, or superseded")

    def validate_sender(
        self,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        now: datetime,
    ) -> None:
        with self.database.session_factory() as session:
            self._validate_sender(session, execution_scope, owner_id, fencing_token, now)

    @staticmethod
    def _binance_client_order_id(intent_id: UUID) -> str:
        encoded = base64.urlsafe_b64encode(intent_id.bytes).rstrip(b"=").decode("ascii")
        return f"tcp-{encoded}"

    @staticmethod
    def _binance_protection_client_order_id(position_id: UUID) -> str:
        encoded = base64.urlsafe_b64encode(position_id.bytes).rstrip(b"=").decode("ascii")
        return f"tpp-{encoded}"

    @staticmethod
    def _hyperliquid_client_order_id(intent_id: UUID) -> str:
        return f"0x{intent_id.hex}"

    @staticmethod
    def _hyperliquid_protection_client_order_id(position_id: UUID) -> str:
        return f"0x{position_id.hex}"

    def _binance_testnet_command(
        self,
        session: Session,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        now: datetime,
        allowed_statuses: set[str],
        *,
        venue: str = "BINANCE",
        require_limit_price: bool = False,
        environment: ExecutionEnvironment = ExecutionEnvironment.TESTNET,
    ) -> BinanceTestnetOrderCommand:
        self._validate_sender(session, execution_scope, owner_id, fencing_token, now)
        intent = session.get(OrderIntent, intent_id)
        if intent is None:
            _reject("ORDER_INTENT_NOT_FOUND", "intent does not exist")
        campaign = session.get(Campaign, intent.campaign_id)
        if campaign is None:
            _reject("CAMPAIGN_NOT_FOUND", "intent campaign is missing")
        if campaign.environment != environment.value or campaign.venue != venue:
            _reject(
                f"{venue}_{environment.value}_SCOPE_REQUIRED",
                f"{venue} execution only accepts its own {environment.value} campaigns",
            )
        if execution_scope != _scope_key(campaign.environment, campaign.account_id, campaign.venue):
            _reject("EXECUTION_SCOPE_MISMATCH", "sender scope does not match campaign scope")
        self._require_role(session, actor_id, "venue.record", campaign.account_id, campaign.venue)
        if environment is ExecutionEnvironment.LIVE:
            live_gate = session.get(CapabilityGate, "LIVE_ORDER_SEND")
            if live_gate is None or live_gate.status != CapabilityStatus.ENABLED.value:
                _reject(
                    "LIVE_ORDER_SEND_DISABLED",
                    "LIVE order send requires the explicit capability gate",
                )
        if intent.status not in allowed_statuses:
            _reject("ORDER_INTENT_STATE_INVALID", "intent state does not allow this venue action")
        if intent.status == OrderIntentStatus.FILLED.value and intent.updated_at < now - timedelta(
            hours=24
        ):
            _reject(
                "ORDER_INTENT_STATE_INVALID",
                "filled intent replay window has expired",
            )
        instrument = session.get(Instrument, campaign.instrument_id)
        if instrument is None or not instrument.active or instrument.venue != venue:
            _reject("INSTRUMENT_UNAVAILABLE", "campaign instrument is unavailable")
        if intent.quantity % instrument.lot_size != 0:
            _reject("INSTRUMENT_QUANTITY_INVALID", "intent quantity is not aligned to lot size")
        if require_limit_price:
            if intent.limit_price is None or intent.limit_price <= 0:
                _reject(
                    "HYPERLIQUID_LIMIT_PRICE_REQUIRED",
                    "Hyperliquid IOC execution requires an explicit frozen limit price",
                )
            if intent.limit_price % instrument.tick_size != 0:
                _reject("INSTRUMENT_PRICE_INVALID", "intent price exceeds current price precision")
        if intent.status == OrderIntentStatus.READY.value and not intent.reduce_only:
            authorization = session.get(TradingAuthorization, intent.authorization_id)
            proposal = (
                None if authorization is None else session.get(Proposal, authorization.proposal_id)
            )
            policy = session.scalar(select(RiskPolicy).where(RiskPolicy.active))
            if (
                authorization is None
                or proposal is None
                or not authorization.active
                or authorization.expires_at <= now
                or proposal.expires_at <= now
            ):
                _reject("AUTHORIZATION_EXPIRED", "new-risk intent authorization is no longer valid")
            if policy is None:
                _reject("RISK_POLICY_UNKNOWN", "active risk policy is unavailable")
            state = SystemRiskState(policy.system_state)
            if state is SystemRiskState.KILL_SWITCH:
                _reject("KILL_SWITCH", "new-risk venue send is blocked")
            if state is SystemRiskState.REDUCE_ONLY:
                _reject("REDUCE_ONLY", "new-risk venue send is blocked")
            if state is SystemRiskState.NO_PYRAMID and intent.kind == IntentKind.ADD.value:
                _reject("PYRAMID_DISABLED", "Add venue send is blocked")
            position = session.scalar(
                select(Position).where(
                    Position.account_id == campaign.account_id,
                    Position.venue == campaign.venue,
                    Position.environment == campaign.environment,
                    Position.instrument_id == campaign.instrument_id,
                )
            )
            equity = session.scalar(
                select(AccountEquity).where(
                    AccountEquity.account_id == campaign.account_id,
                    AccountEquity.venue == campaign.venue,
                    AccountEquity.environment == campaign.environment,
                    AccountEquity.currency == instrument.collateral_currency,
                )
            )
            max_age = timedelta(seconds=policy.max_fact_age_seconds)
            if (
                position is None
                or position.fact_status != FactStatus.KNOWN.value
                or self._fact_is_stale(position.observed_at, now, max_age)
            ):
                _reject("POSITION_UNKNOWN", "new-risk venue send requires a fresh position")
            if (
                equity is None
                or equity.fact_status != FactStatus.KNOWN.value
                or self._fact_is_stale(equity.observed_at, now, max_age)
            ):
                _reject("EQUITY_UNKNOWN", "new-risk venue send requires fresh equity")
            capital_known, managed_capital_usd, _, _ = self._managed_capital_context(
                session,
                environment=campaign.environment,
                now=now,
                max_age=max_age,
            )
            if not capital_known:
                _reject(
                    "MANAGED_CAPITAL_UNKNOWN",
                    "new-risk venue send requires fresh total managed capital",
                )
            occupied_risk = self._occupied_risk(session)
            if occupied_risk > min(policy.max_total_risk, managed_capital_usd):
                _reject(
                    "RISK_CAPACITY_EXHAUSTED",
                    "current reservations exceed total managed capital risk capacity",
                )
            if intent.kind == IntentKind.INITIAL.value and position.quantity != 0:
                _reject("POSITION_NOT_FLAT", "INITIAL venue send requires a flat position")
            if intent.kind == IntentKind.ADD.value:
                protection = session.scalar(
                    select(ProtectionOrder).where(
                        ProtectionOrder.position_id == position.position_id
                    )
                )
                if (
                    protection is None
                    or protection.status != ProtectionStatus.ACTIVE.value
                    or not protection.fully_covered
                    or protection.quantity < abs(position.quantity)
                    or self._fact_is_stale(protection.observed_at, now, max_age)
                ):
                    _reject("PROTECTION_UNKNOWN", "Add venue send requires current protection")
            if (
                session.scalar(
                    select(RiskReservation.reservation_id).where(
                        RiskReservation.status == ReservationStatus.UNKNOWN.value
                    )
                )
                is not None
            ):
                _reject("RISK_RESERVATION_UNKNOWN", "unresolved risk blocks new venue sends")
            reference_price = intent.limit_price if require_limit_price else position.mark_price
            assert reference_price is not None
            notional = intent.quantity * reference_price * instrument.contract_multiplier
            if notional < instrument.minimum_notional:
                _reject("MINIMUM_NOTIONAL", "intent is below current instrument minimum notional")
        return BinanceTestnetOrderCommand(
            symbol=instrument.symbol,
            side=intent.side,
            quantity=intent.quantity,
            reduce_only=intent.reduce_only,
            client_order_id=(
                self._hyperliquid_client_order_id(intent.intent_id)
                if venue == "HYPERLIQUID"
                else self._binance_client_order_id(intent.intent_id)
            ),
        )

    def prepare_binance_testnet_send(
        self,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        *,
        now: datetime,
    ) -> BinanceTestnetOrderCommand:
        with self.database.session_factory() as session:
            return self._binance_testnet_command(
                session,
                intent_id,
                actor_id,
                execution_scope,
                owner_id,
                fencing_token,
                now,
                {
                    OrderIntentStatus.READY.value,
                    OrderIntentStatus.SENT.value,
                    OrderIntentStatus.PARTIALLY_FILLED.value,
                    OrderIntentStatus.FILLED.value,
                },
            )

    def prepare_binance_testnet_cancel(
        self,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        *,
        now: datetime,
    ) -> BinanceTestnetOrderCommand:
        with self.database.session_factory() as session:
            return self._binance_testnet_command(
                session,
                intent_id,
                actor_id,
                execution_scope,
                owner_id,
                fencing_token,
                now,
                {
                    OrderIntentStatus.SENT.value,
                    OrderIntentStatus.PARTIALLY_FILLED.value,
                },
            )

    def prepare_binance_testnet_recovery(
        self,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        *,
        now: datetime,
    ) -> BinanceTestnetOrderCommand:
        with self.database.session_factory() as session:
            return self._binance_testnet_command(
                session,
                intent_id,
                actor_id,
                execution_scope,
                owner_id,
                fencing_token,
                now,
                {OrderIntentStatus.UNKNOWN.value},
            )

    def prepare_binance_live_send(
        self,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        *,
        now: datetime,
    ) -> BinanceTestnetOrderCommand:
        with self.database.session_factory() as session:
            return self._binance_testnet_command(
                session,
                intent_id,
                actor_id,
                execution_scope,
                owner_id,
                fencing_token,
                now,
                {
                    OrderIntentStatus.READY.value,
                    OrderIntentStatus.SENT.value,
                    OrderIntentStatus.PARTIALLY_FILLED.value,
                    OrderIntentStatus.FILLED.value,
                },
                environment=ExecutionEnvironment.LIVE,
            )

    def prepare_binance_live_cancel(
        self,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        *,
        now: datetime,
    ) -> BinanceTestnetOrderCommand:
        with self.database.session_factory() as session:
            return self._binance_testnet_command(
                session,
                intent_id,
                actor_id,
                execution_scope,
                owner_id,
                fencing_token,
                now,
                {
                    OrderIntentStatus.SENT.value,
                    OrderIntentStatus.PARTIALLY_FILLED.value,
                },
                environment=ExecutionEnvironment.LIVE,
            )

    def prepare_binance_live_recovery(
        self,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        *,
        now: datetime,
    ) -> BinanceTestnetOrderCommand:
        with self.database.session_factory() as session:
            return self._binance_testnet_command(
                session,
                intent_id,
                actor_id,
                execution_scope,
                owner_id,
                fencing_token,
                now,
                {OrderIntentStatus.UNKNOWN.value},
                environment=ExecutionEnvironment.LIVE,
            )

    def _hyperliquid_testnet_command(
        self,
        session: Session,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        now: datetime,
        allowed_statuses: set[str],
        *,
        environment: ExecutionEnvironment = ExecutionEnvironment.TESTNET,
    ) -> HyperliquidTestnetOrderCommand:
        base = self._binance_testnet_command(
            session,
            intent_id,
            actor_id,
            execution_scope,
            owner_id,
            fencing_token,
            now,
            allowed_statuses,
            venue="HYPERLIQUID",
            require_limit_price=True,
            environment=environment,
        )
        intent = session.get(OrderIntent, intent_id)
        if intent is None or intent.limit_price is None:
            _reject(
                "HYPERLIQUID_LIMIT_PRICE_REQUIRED",
                "Hyperliquid IOC execution requires an explicit frozen limit price",
            )
        campaign = session.get(Campaign, intent.campaign_id)
        instrument = None if campaign is None else session.get(Instrument, campaign.instrument_id)
        if instrument is None:
            _reject("INSTRUMENT_UNAVAILABLE", "campaign instrument is unavailable")
        notional = intent.quantity * intent.limit_price * instrument.contract_multiplier
        if notional < instrument.minimum_notional:
            _reject("MINIMUM_NOTIONAL", "intent is below current instrument minimum notional")
        return HyperliquidTestnetOrderCommand(
            symbol=base.symbol,
            side=base.side,
            quantity=base.quantity,
            limit_price=intent.limit_price,
            reduce_only=base.reduce_only,
            client_order_id=base.client_order_id,
        )

    def prepare_hyperliquid_testnet_send(
        self,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        *,
        now: datetime,
    ) -> HyperliquidTestnetOrderCommand:
        with self.database.session_factory() as session:
            return self._hyperliquid_testnet_command(
                session,
                intent_id,
                actor_id,
                execution_scope,
                owner_id,
                fencing_token,
                now,
                {
                    OrderIntentStatus.READY.value,
                    OrderIntentStatus.SENT.value,
                    OrderIntentStatus.PARTIALLY_FILLED.value,
                    OrderIntentStatus.FILLED.value,
                },
            )

    def prepare_hyperliquid_testnet_cancel(
        self,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        *,
        now: datetime,
    ) -> HyperliquidTestnetOrderCommand:
        with self.database.session_factory() as session:
            return self._hyperliquid_testnet_command(
                session,
                intent_id,
                actor_id,
                execution_scope,
                owner_id,
                fencing_token,
                now,
                {OrderIntentStatus.SENT.value, OrderIntentStatus.PARTIALLY_FILLED.value},
            )

    def prepare_hyperliquid_testnet_recovery(
        self,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        *,
        now: datetime,
    ) -> HyperliquidTestnetOrderCommand:
        with self.database.session_factory() as session:
            return self._hyperliquid_testnet_command(
                session,
                intent_id,
                actor_id,
                execution_scope,
                owner_id,
                fencing_token,
                now,
                {OrderIntentStatus.UNKNOWN.value},
            )

    def prepare_hyperliquid_live_send(
        self,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        *,
        now: datetime,
    ) -> HyperliquidTestnetOrderCommand:
        with self.database.session_factory() as session:
            return self._hyperliquid_testnet_command(
                session,
                intent_id,
                actor_id,
                execution_scope,
                owner_id,
                fencing_token,
                now,
                {
                    OrderIntentStatus.READY.value,
                    OrderIntentStatus.SENT.value,
                    OrderIntentStatus.PARTIALLY_FILLED.value,
                    OrderIntentStatus.FILLED.value,
                },
                environment=ExecutionEnvironment.LIVE,
            )

    def prepare_hyperliquid_live_cancel(
        self,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        *,
        now: datetime,
    ) -> HyperliquidTestnetOrderCommand:
        with self.database.session_factory() as session:
            return self._hyperliquid_testnet_command(
                session,
                intent_id,
                actor_id,
                execution_scope,
                owner_id,
                fencing_token,
                now,
                {
                    OrderIntentStatus.SENT.value,
                    OrderIntentStatus.PARTIALLY_FILLED.value,
                },
                environment=ExecutionEnvironment.LIVE,
            )

    def prepare_hyperliquid_live_recovery(
        self,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        *,
        now: datetime,
    ) -> HyperliquidTestnetOrderCommand:
        with self.database.session_factory() as session:
            return self._hyperliquid_testnet_command(
                session,
                intent_id,
                actor_id,
                execution_scope,
                owner_id,
                fencing_token,
                now,
                {OrderIntentStatus.UNKNOWN.value},
                environment=ExecutionEnvironment.LIVE,
            )

    @staticmethod
    def _validate_binance_order_result(
        intent: OrderIntent,
        command: BinanceTestnetOrderCommand,
        result: BinanceTestnetOrder,
        *,
        expected_order_type: str = "MARKET",
        identity_code: str = "BINANCE_TESTNET_IDENTITY_CONFLICT",
    ) -> None:
        if (
            result.client_order_id != command.client_order_id
            or result.side != intent.side
            or result.order_type != expected_order_type
            or result.ordered_quantity != intent.quantity
            or result.reduce_only != intent.reduce_only
            or result.close_position
            or result.filled_quantity > intent.quantity
        ):
            _reject(
                identity_code,
                "testnet order result does not match the frozen order intent",
            )

    def _release_zero_fill_in_session(
        self,
        session: Session,
        intent: OrderIntent,
        terminal_status: OrderIntentStatus,
        confirmed_external: bool = False,
        *,
        now: datetime,
    ) -> None:
        reservation = (
            session.get(RiskReservation, intent.reservation_id, with_for_update=True)
            if intent.reservation_id is not None
            else None
        )
        if reservation is not None and reservation.status != ReservationStatus.RELEASED.value:
            if reservation.status == ReservationStatus.UNKNOWN.value and not confirmed_external:
                _reject("RISK_RESERVATION_UNKNOWN", "unknown risk cannot be released")
            reservation.status = ReservationStatus.RELEASED.value
            reservation.updated_at = now
            reservation.version += 1
            authorization = session.get(
                TradingAuthorization, intent.authorization_id, with_for_update=True
            )
            if authorization is None or authorization.used_quantity < intent.quantity:
                _reject("AUTHORIZATION_USAGE_INVALID", "authorization usage is inconsistent")
            authorization.used_quantity -= intent.quantity
        intent.status = terminal_status.value
        intent.updated_at = now
        intent.version += 1

    def record_binance_testnet_order(
        self,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        command: BinanceTestnetOrderCommand,
        result: BinanceTestnetOrder,
        *,
        venue: str = "BINANCE",
        expected_order_type: str = "MARKET",
        expected_limit_price: Decimal | None = None,
        environment: ExecutionEnvironment = ExecutionEnvironment.TESTNET,
        now: datetime,
    ) -> UUID:
        with self.database.session_factory.begin() as session:
            self._validate_sender(session, execution_scope, owner_id, fencing_token, now)
            intent = session.get(OrderIntent, intent_id, with_for_update=True)
            if intent is None:
                _reject("ORDER_INTENT_NOT_FOUND", "intent does not exist")
            campaign = session.get(Campaign, intent.campaign_id, with_for_update=True)
            if campaign is None:
                _reject("CAMPAIGN_NOT_FOUND", "intent campaign is missing")
            if (
                campaign.environment != environment.value
                or campaign.venue != venue
                or execution_scope
                != _scope_key(campaign.environment, campaign.account_id, campaign.venue)
            ):
                _reject(
                    "EXECUTION_SCOPE_MISMATCH",
                    f"result is outside the {environment.value} campaign scope",
                )
            self._require_role(
                session, actor_id, "venue.record", campaign.account_id, campaign.venue
            )
            if expected_limit_price is not None and intent.limit_price != expected_limit_price:
                _reject(
                    f"{venue}_{environment.value}_IDENTITY_CONFLICT",
                    "result does not match the intent's frozen price boundary",
                )
            identity_code = f"{venue}_{environment.value}_IDENTITY_CONFLICT"
            self._validate_binance_order_result(
                intent,
                command,
                result,
                expected_order_type=expected_order_type,
                identity_code=identity_code,
            )
            fact = session.scalar(
                select(VenueOrder)
                .where(VenueOrder.order_intent_id == intent.intent_id)
                .with_for_update()
            )
            if fact is None:
                fact = VenueOrder(
                    order_intent_id=intent.intent_id,
                    account_id=campaign.account_id,
                    venue=campaign.venue,
                    environment=campaign.environment,
                    instrument_id=campaign.instrument_id,
                    venue_order_id=result.order_id,
                    client_order_id=result.client_order_id,
                    side=result.side,
                    order_type=result.order_type,
                    reduce_only=result.reduce_only,
                    status=result.status,
                    ordered_quantity=result.ordered_quantity,
                    filled_quantity=result.filled_quantity,
                    observed_at=result.observed_at,
                    updated_at=now,
                )
                session.add(fact)
            elif (
                fact.client_order_id != result.client_order_id
                or fact.side != result.side
                or fact.order_type != result.order_type
                or fact.reduce_only != result.reduce_only
            ):
                _reject(
                    identity_code,
                    "persisted client order identity changed semantics",
                )
            else:
                fact.venue_order_id = result.order_id
                fact.status = result.status
                fact.ordered_quantity = result.ordered_quantity
                fact.filled_quantity = result.filled_quantity
                fact.observed_at = result.observed_at
                fact.updated_at = now

            if result.filled_quantity > 0:
                self._consume_add_unit(session, intent)
            previous = intent.status
            terminal = {
                VenueOrderStatus.CANCELLED.value: OrderIntentStatus.CANCELLED,
                VenueOrderStatus.REJECTED.value: OrderIntentStatus.REJECTED,
            }
            if result.status == VenueOrderStatus.UNKNOWN.value:
                intent.status = OrderIntentStatus.UNKNOWN.value
                if intent.reservation_id is not None:
                    reservation = session.get(
                        RiskReservation, intent.reservation_id, with_for_update=True
                    )
                    if reservation is not None:
                        reservation.status = ReservationStatus.UNKNOWN.value
                        reservation.updated_at = now
                        reservation.version += 1
                campaign.status = CampaignStatus.UNKNOWN.value
            elif result.status in terminal and result.filled_quantity == 0:
                self._release_zero_fill_in_session(
                    session,
                    intent,
                    terminal[result.status],
                    confirmed_external=True,
                    now=now,
                )
            else:
                if result.status == VenueOrderStatus.FILLED.value:
                    intent.status = OrderIntentStatus.FILLED.value
                elif result.status in terminal:
                    intent.status = terminal[result.status].value
                elif result.filled_quantity > 0:
                    intent.status = OrderIntentStatus.PARTIALLY_FILLED.value
                else:
                    intent.status = OrderIntentStatus.SENT.value
                intent.updated_at = now
                intent.version += 1
                if result.filled_quantity > 0 and intent.reservation_id is not None:
                    reservation = session.get(
                        RiskReservation, intent.reservation_id, with_for_update=True
                    )
                    if reservation is not None:
                        reservation.status = ReservationStatus.OPEN.value
                        reservation.updated_at = now
                        reservation.version += 1
                elif (
                    previous == OrderIntentStatus.UNKNOWN.value
                    and intent.reservation_id is not None
                ):
                    reservation = session.get(
                        RiskReservation, intent.reservation_id, with_for_update=True
                    )
                    if reservation is not None:
                        reservation.status = ReservationStatus.RESERVED.value
                        reservation.updated_at = now
                        reservation.version += 1
                if result.filled_quantity > 0:
                    if intent.kind in {IntentKind.INITIAL.value, IntentKind.ADD.value}:
                        campaign.status = CampaignStatus.OPEN.value
                    elif intent.kind == IntentKind.EXIT.value:
                        campaign.status = CampaignStatus.CLOSING.value
                    else:
                        campaign.status = CampaignStatus.REDUCING.value
                elif previous == OrderIntentStatus.UNKNOWN.value:
                    if intent.kind in {IntentKind.INITIAL.value, IntentKind.ADD.value}:
                        campaign.status = CampaignStatus.OPENING.value
                    elif intent.kind == IntentKind.EXIT.value:
                        campaign.status = CampaignStatus.CLOSING.value
                    else:
                        campaign.status = CampaignStatus.REDUCING.value
            campaign.updated_at = now
            session.flush()
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type=f"{venue}_{environment.value}_ORDER_OBSERVED",
                object_type="VenueOrder",
                object_id=fact.venue_order_fact_id,
                reason=f"{result.client_order_id}:{result.status}",
                correlation_id=intent.correlation_id,
                object_version=intent.version,
                now=now,
            )
            if previous != intent.status:
                INTENT_TRANSITIONS.labels(previous, intent.status).inc()
            return fact.venue_order_fact_id

    def record_hyperliquid_testnet_order(
        self,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        command: HyperliquidTestnetOrderCommand,
        result: HyperliquidTestnetOrder,
        *,
        environment: ExecutionEnvironment = ExecutionEnvironment.TESTNET,
        now: datetime,
    ) -> UUID:
        if result.limit_price != command.limit_price or result.stop_price != 0:
            _reject(
                f"HYPERLIQUID_{environment.value}_IDENTITY_CONFLICT",
                "Hyperliquid result changed the explicit IOC price boundary",
            )
        return self.record_binance_testnet_order(
            intent_id,
            actor_id,
            execution_scope,
            owner_id,
            fencing_token,
            BinanceTestnetOrderCommand(
                symbol=command.symbol,
                side=command.side,
                quantity=command.quantity,
                reduce_only=command.reduce_only,
                client_order_id=command.client_order_id,
            ),
            BinanceTestnetOrder(
                order_id=result.order_id,
                client_order_id=result.client_order_id,
                status=result.status,
                side=result.side,
                order_type=result.order_type,
                ordered_quantity=result.ordered_quantity,
                filled_quantity=result.filled_quantity,
                stop_price=result.stop_price,
                reduce_only=result.reduce_only,
                close_position=result.close_position,
                observed_at=result.observed_at,
            ),
            venue="HYPERLIQUID",
            expected_order_type="IOC_LIMIT",
            expected_limit_price=command.limit_price,
            environment=environment,
            now=now,
        )

    def record_binance_testnet_unknown(
        self,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        command: BinanceTestnetOrderCommand,
        reason: str,
        *,
        venue: str = "BINANCE",
        order_type: str = "MARKET",
        environment: ExecutionEnvironment = ExecutionEnvironment.TESTNET,
        now: datetime,
    ) -> None:
        with self.database.session_factory.begin() as session:
            self._validate_sender(session, execution_scope, owner_id, fencing_token, now)
            intent = session.get(OrderIntent, intent_id, with_for_update=True)
            if intent is None:
                _reject("ORDER_INTENT_NOT_FOUND", "intent does not exist")
            campaign = session.get(Campaign, intent.campaign_id, with_for_update=True)
            if campaign is None:
                _reject("CAMPAIGN_NOT_FOUND", "intent campaign is missing")
            if (
                campaign.environment != environment.value
                or campaign.venue != venue
                or execution_scope
                != _scope_key(campaign.environment, campaign.account_id, campaign.venue)
            ):
                _reject("EXECUTION_SCOPE_MISMATCH", "sender scope does not match campaign scope")
            self._require_role(
                session, actor_id, "venue.record", campaign.account_id, campaign.venue
            )
            fact = session.scalar(
                select(VenueOrder)
                .where(VenueOrder.order_intent_id == intent.intent_id)
                .with_for_update()
            )
            if fact is None:
                fact = VenueOrder(
                    order_intent_id=intent.intent_id,
                    account_id=campaign.account_id,
                    venue=campaign.venue,
                    environment=campaign.environment,
                    instrument_id=campaign.instrument_id,
                    venue_order_id=f"UNKNOWN:{command.client_order_id}",
                    client_order_id=command.client_order_id,
                    side=command.side,
                    order_type=order_type,
                    reduce_only=command.reduce_only,
                    status=VenueOrderStatus.UNKNOWN.value,
                    ordered_quantity=command.quantity,
                    filled_quantity=Decimal(0),
                    observed_at=now,
                    updated_at=now,
                )
                session.add(fact)
            else:
                fact.status = VenueOrderStatus.UNKNOWN.value
                fact.observed_at = now
                fact.updated_at = now
            previous = intent.status
            intent.status = OrderIntentStatus.UNKNOWN.value
            intent.updated_at = now
            intent.version += 1
            if intent.reservation_id is not None:
                reservation = session.get(
                    RiskReservation, intent.reservation_id, with_for_update=True
                )
                if reservation is not None:
                    reservation.status = ReservationStatus.UNKNOWN.value
                    reservation.updated_at = now
                    reservation.version += 1
            campaign.status = CampaignStatus.UNKNOWN.value
            campaign.updated_at = now
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type=f"{venue}_{environment.value}_OUTCOME_UNKNOWN",
                object_type="OrderIntent",
                object_id=intent.intent_id,
                reason=reason,
                correlation_id=intent.correlation_id,
                object_version=intent.version,
                now=now,
            )
            if previous != intent.status:
                INTENT_TRANSITIONS.labels(previous, intent.status).inc()

    def record_hyperliquid_testnet_unknown(
        self,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        command: HyperliquidTestnetOrderCommand,
        reason: str,
        *,
        environment: ExecutionEnvironment = ExecutionEnvironment.TESTNET,
        now: datetime,
    ) -> None:
        self.record_binance_testnet_unknown(
            intent_id,
            actor_id,
            execution_scope,
            owner_id,
            fencing_token,
            BinanceTestnetOrderCommand(
                symbol=command.symbol,
                side=command.side,
                quantity=command.quantity,
                reduce_only=command.reduce_only,
                client_order_id=command.client_order_id,
            ),
            reason,
            venue="HYPERLIQUID",
            order_type="IOC_LIMIT",
            environment=environment,
            now=now,
        )

    def prepare_binance_testnet_protection(
        self,
        campaign_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        trigger_price: Decimal,
        *,
        venue: str = "BINANCE",
        environment: ExecutionEnvironment = ExecutionEnvironment.TESTNET,
        now: datetime,
    ) -> BinanceTestnetProtectionCommand:
        with self.database.session_factory() as session:
            self._validate_sender(session, execution_scope, owner_id, fencing_token, now)
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                _reject("CAMPAIGN_NOT_FOUND", "campaign does not exist")
            if (
                campaign.environment != environment.value
                or campaign.venue != venue
                or execution_scope
                != _scope_key(campaign.environment, campaign.account_id, campaign.venue)
            ):
                _reject(
                    "EXECUTION_SCOPE_MISMATCH",
                    f"protection is outside {environment.value} scope",
                )
            self._require_role(
                session, actor_id, "venue.record", campaign.account_id, campaign.venue
            )
            if environment is ExecutionEnvironment.LIVE:
                live_gate = session.get(CapabilityGate, "LIVE_ORDER_SEND")
                if live_gate is None or live_gate.status != CapabilityStatus.ENABLED.value:
                    _reject(
                        "LIVE_ORDER_SEND_DISABLED",
                        "LIVE protection requires the explicit capability gate",
                    )
            if trigger_price <= 0:
                _reject("PROTECTION_TRIGGER_INVALID", "protection trigger must be positive")
            position = session.scalar(
                select(Position).where(
                    Position.account_id == campaign.account_id,
                    Position.venue == campaign.venue,
                    Position.environment == campaign.environment,
                    Position.instrument_id == campaign.instrument_id,
                )
            )
            policy = session.scalar(select(RiskPolicy).where(RiskPolicy.active))
            if (
                position is None
                or position.fact_status != FactStatus.KNOWN.value
                or position.quantity == 0
                or policy is None
                or self._fact_is_stale(
                    position.observed_at,
                    now,
                    timedelta(seconds=policy.max_fact_age_seconds),
                )
            ):
                _reject("POSITION_UNKNOWN", "native protection requires a fresh nonzero position")
            instrument = session.get(Instrument, campaign.instrument_id)
            if (
                instrument is None
                or instrument.venue != venue
                or not instrument.protection_supported
            ):
                _reject("PROTECTION_UNSUPPORTED", "instrument does not support native protection")
            if trigger_price % instrument.tick_size != 0:
                _reject("INSTRUMENT_PRICE_INVALID", "protection price exceeds instrument precision")
            side = "SELL" if position.quantity > 0 else "BUY"
            return BinanceTestnetProtectionCommand(
                symbol=instrument.symbol,
                side=side,
                trigger_price=trigger_price,
                client_order_id=(
                    self._hyperliquid_protection_client_order_id(position.position_id)
                    if venue == "HYPERLIQUID"
                    else self._binance_protection_client_order_id(position.position_id)
                ),
                quantity=abs(position.quantity),
            )

    def record_binance_testnet_protection(
        self,
        campaign_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        command: BinanceTestnetProtectionCommand,
        result: BinanceTestnetOrder,
        *,
        venue: str = "BINANCE",
        expected_order_type: str = "STOP_MARKET",
        expected_close_position: bool = True,
        require_reduce_only: bool = False,
        environment: ExecutionEnvironment = ExecutionEnvironment.TESTNET,
        now: datetime,
    ) -> UUID:
        with self.database.session_factory.begin() as session:
            self._validate_sender(session, execution_scope, owner_id, fencing_token, now)
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                _reject("CAMPAIGN_NOT_FOUND", "campaign does not exist")
            if (
                campaign.environment != environment.value
                or campaign.venue != venue
                or execution_scope
                != _scope_key(campaign.environment, campaign.account_id, campaign.venue)
            ):
                _reject("EXECUTION_SCOPE_MISMATCH", "protection is outside campaign scope")
            self._require_role(
                session, actor_id, "venue.record", campaign.account_id, campaign.venue
            )
            position = session.scalar(
                select(Position)
                .where(
                    Position.account_id == campaign.account_id,
                    Position.venue == campaign.venue,
                    Position.environment == campaign.environment,
                    Position.instrument_id == campaign.instrument_id,
                )
                .with_for_update()
            )
            if position is None or position.quantity == 0:
                _reject("POSITION_UNKNOWN", "protection result requires a nonzero position")
            if (
                result.client_order_id != command.client_order_id
                or result.side != command.side
                or result.order_type != expected_order_type
                or result.stop_price != command.trigger_price
                or result.close_position != expected_close_position
                or (require_reduce_only and not result.reduce_only)
            ):
                _reject(
                    f"{venue}_{environment.value}_IDENTITY_CONFLICT",
                    "venue protection result changed frozen semantics",
                )
            order = session.scalar(
                select(VenueOrder)
                .where(
                    VenueOrder.environment == campaign.environment,
                    VenueOrder.account_id == campaign.account_id,
                    VenueOrder.venue == campaign.venue,
                    VenueOrder.client_order_id == command.client_order_id,
                )
                .with_for_update()
            )
            if order is None:
                order = VenueOrder(
                    order_intent_id=None,
                    account_id=campaign.account_id,
                    venue=campaign.venue,
                    environment=campaign.environment,
                    instrument_id=campaign.instrument_id,
                    venue_order_id=result.order_id,
                    client_order_id=result.client_order_id,
                    side=result.side,
                    order_type=result.order_type,
                    reduce_only=True,
                    status=result.status,
                    ordered_quantity=result.ordered_quantity,
                    filled_quantity=result.filled_quantity,
                    observed_at=result.observed_at,
                    updated_at=now,
                )
                session.add(order)
            else:
                order.venue_order_id = result.order_id
                order.status = result.status
                order.filled_quantity = result.filled_quantity
                order.observed_at = result.observed_at
                order.updated_at = now
            active = result.status in {
                VenueOrderStatus.SENT.value,
                VenueOrderStatus.PARTIALLY_FILLED.value,
            }
            protection = session.scalar(
                select(ProtectionOrder)
                .where(ProtectionOrder.position_id == position.position_id)
                .with_for_update()
            )
            if protection is None:
                protection = ProtectionOrder(
                    position_id=position.position_id,
                    venue_order_id=result.order_id,
                    quantity=abs(position.quantity) if active else Decimal(0),
                    trigger_price=result.stop_price,
                    status=(
                        ProtectionStatus.ACTIVE.value
                        if active
                        else ProtectionStatus.UNKNOWN.value
                        if result.status == VenueOrderStatus.UNKNOWN.value
                        else ProtectionStatus.DEGRADED.value
                    ),
                    fully_covered=active,
                    observed_at=result.observed_at,
                    updated_at=now,
                )
                session.add(protection)
            else:
                protection.venue_order_id = result.order_id
                protection.quantity = abs(position.quantity) if active else Decimal(0)
                protection.trigger_price = result.stop_price
                protection.status = (
                    ProtectionStatus.ACTIVE.value
                    if active
                    else ProtectionStatus.UNKNOWN.value
                    if result.status == VenueOrderStatus.UNKNOWN.value
                    else ProtectionStatus.DEGRADED.value
                )
                protection.fully_covered = active
                protection.observed_at = result.observed_at
                protection.updated_at = now
            session.flush()
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type=f"{venue}_{environment.value}_PROTECTION_OBSERVED",
                object_type="ProtectionOrder",
                object_id=protection.protection_id,
                reason=f"{result.client_order_id}:{result.status}",
                correlation_id=uuid4(),
                object_version=1,
                now=now,
            )
            return protection.protection_id

    def prepare_hyperliquid_testnet_protection(
        self,
        campaign_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        trigger_price: Decimal,
        limit_price: Decimal,
        *,
        environment: ExecutionEnvironment = ExecutionEnvironment.TESTNET,
        now: datetime,
    ) -> HyperliquidTestnetProtectionCommand:
        base = self.prepare_binance_testnet_protection(
            campaign_id,
            actor_id,
            execution_scope,
            owner_id,
            fencing_token,
            trigger_price,
            venue="HYPERLIQUID",
            environment=environment,
            now=now,
        )
        if limit_price <= 0:
            _reject(
                "PROTECTION_TRIGGER_INVALID",
                "Hyperliquid protection requires a positive explicit limit price",
            )
        with self.database.session_factory() as session:
            campaign = session.get(Campaign, campaign_id)
            position = (
                None
                if campaign is None
                else session.scalar(
                    select(Position).where(
                        Position.account_id == campaign.account_id,
                        Position.venue == campaign.venue,
                        Position.environment == campaign.environment,
                        Position.instrument_id == campaign.instrument_id,
                    )
                )
            )
            instrument = (
                None if campaign is None else session.get(Instrument, campaign.instrument_id)
            )
            if position is None or instrument is None:
                _reject("POSITION_UNKNOWN", "native protection position is unavailable")
            if limit_price % instrument.tick_size != 0:
                _reject("INSTRUMENT_PRICE_INVALID", "protection price exceeds instrument precision")
            return HyperliquidTestnetProtectionCommand(
                symbol=base.symbol,
                side=base.side,
                quantity=abs(position.quantity),
                trigger_price=base.trigger_price,
                limit_price=limit_price,
                client_order_id=base.client_order_id,
            )

    def record_hyperliquid_testnet_protection(
        self,
        campaign_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        command: HyperliquidTestnetProtectionCommand,
        result: HyperliquidTestnetOrder,
        *,
        environment: ExecutionEnvironment = ExecutionEnvironment.TESTNET,
        now: datetime,
    ) -> UUID:
        if (
            result.ordered_quantity != command.quantity
            or result.limit_price != command.limit_price
            or result.reduce_only is not True
        ):
            _reject(
                f"HYPERLIQUID_{environment.value}_IDENTITY_CONFLICT",
                "Hyperliquid protection changed frozen quantity or price semantics",
            )
        return self.record_binance_testnet_protection(
            campaign_id,
            actor_id,
            execution_scope,
            owner_id,
            fencing_token,
            BinanceTestnetProtectionCommand(
                symbol=command.symbol,
                side=command.side,
                trigger_price=command.trigger_price,
                client_order_id=command.client_order_id,
                quantity=command.quantity,
            ),
            BinanceTestnetOrder(
                order_id=result.order_id,
                client_order_id=result.client_order_id,
                status=result.status,
                side=result.side,
                order_type=result.order_type,
                ordered_quantity=result.ordered_quantity,
                filled_quantity=result.filled_quantity,
                stop_price=result.stop_price,
                reduce_only=result.reduce_only,
                close_position=result.close_position,
                observed_at=result.observed_at,
            ),
            venue="HYPERLIQUID",
            expected_order_type="TRIGGER_MARKET",
            expected_close_position=False,
            require_reduce_only=True,
            environment=environment,
            now=now,
        )

    def record_binance_live_order(
        self,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        command: BinanceTestnetOrderCommand,
        result: BinanceTestnetOrder,
        *,
        now: datetime,
    ) -> UUID:
        return self.record_binance_testnet_order(
            intent_id,
            actor_id,
            execution_scope,
            owner_id,
            fencing_token,
            command,
            result,
            environment=ExecutionEnvironment.LIVE,
            now=now,
        )

    def record_binance_live_unknown(
        self,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        command: BinanceTestnetOrderCommand,
        reason: str,
        *,
        now: datetime,
    ) -> None:
        self.record_binance_testnet_unknown(
            intent_id,
            actor_id,
            execution_scope,
            owner_id,
            fencing_token,
            command,
            reason,
            environment=ExecutionEnvironment.LIVE,
            now=now,
        )

    def record_hyperliquid_live_order(
        self,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        command: HyperliquidTestnetOrderCommand,
        result: HyperliquidTestnetOrder,
        *,
        now: datetime,
    ) -> UUID:
        return self.record_hyperliquid_testnet_order(
            intent_id,
            actor_id,
            execution_scope,
            owner_id,
            fencing_token,
            command,
            result,
            environment=ExecutionEnvironment.LIVE,
            now=now,
        )

    def record_hyperliquid_live_unknown(
        self,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        command: HyperliquidTestnetOrderCommand,
        reason: str,
        *,
        now: datetime,
    ) -> None:
        self.record_hyperliquid_testnet_unknown(
            intent_id,
            actor_id,
            execution_scope,
            owner_id,
            fencing_token,
            command,
            reason,
            environment=ExecutionEnvironment.LIVE,
            now=now,
        )

    def prepare_binance_live_protection(
        self,
        campaign_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        trigger_price: Decimal,
        *,
        now: datetime,
    ) -> BinanceTestnetProtectionCommand:
        return self.prepare_binance_testnet_protection(
            campaign_id,
            actor_id,
            execution_scope,
            owner_id,
            fencing_token,
            trigger_price,
            environment=ExecutionEnvironment.LIVE,
            now=now,
        )

    def record_binance_live_protection(
        self,
        campaign_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        command: BinanceTestnetProtectionCommand,
        result: BinanceTestnetOrder,
        *,
        now: datetime,
    ) -> UUID:
        return self.record_binance_testnet_protection(
            campaign_id,
            actor_id,
            execution_scope,
            owner_id,
            fencing_token,
            command,
            result,
            expected_close_position=False,
            require_reduce_only=True,
            environment=ExecutionEnvironment.LIVE,
            now=now,
        )

    def prepare_hyperliquid_live_protection(
        self,
        campaign_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        trigger_price: Decimal,
        limit_price: Decimal,
        *,
        now: datetime,
    ) -> HyperliquidTestnetProtectionCommand:
        return self.prepare_hyperliquid_testnet_protection(
            campaign_id,
            actor_id,
            execution_scope,
            owner_id,
            fencing_token,
            trigger_price,
            limit_price,
            environment=ExecutionEnvironment.LIVE,
            now=now,
        )

    def record_hyperliquid_live_protection(
        self,
        campaign_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        command: HyperliquidTestnetProtectionCommand,
        result: HyperliquidTestnetOrder,
        *,
        now: datetime,
    ) -> UUID:
        return self.record_hyperliquid_testnet_protection(
            campaign_id,
            actor_id,
            execution_scope,
            owner_id,
            fencing_token,
            command,
            result,
            environment=ExecutionEnvironment.LIVE,
            now=now,
        )

    def prepare_live_protection_cancel(
        self,
        campaign_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        *,
        venue: str,
        now: datetime,
    ) -> ProtectionCancelCommand:
        with self.database.session_factory() as session:
            self._validate_sender(session, execution_scope, owner_id, fencing_token, now)
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                _reject("CAMPAIGN_NOT_FOUND", "campaign does not exist")
            if (
                campaign.environment != ExecutionEnvironment.LIVE.value
                or campaign.venue != venue
                or execution_scope
                != _scope_key(campaign.environment, campaign.account_id, campaign.venue)
            ):
                _reject("EXECUTION_SCOPE_MISMATCH", "protection cancel is outside LIVE scope")
            self._require_role(
                session, actor_id, "venue.record", campaign.account_id, campaign.venue
            )
            if campaign.current_target_quantity != 0:
                _reject(
                    "PROTECTION_CANCEL_UNSAFE",
                    "native protection can only be removed after the campaign target is zero",
                )
            position = session.scalar(
                select(Position).where(
                    Position.account_id == campaign.account_id,
                    Position.venue == campaign.venue,
                    Position.environment == campaign.environment,
                    Position.instrument_id == campaign.instrument_id,
                )
            )
            protection = (
                None
                if position is None
                else session.scalar(
                    select(ProtectionOrder).where(
                        ProtectionOrder.position_id == position.position_id
                    )
                )
            )
            order = (
                None
                if protection is None
                else session.scalar(
                    select(VenueOrder).where(
                        VenueOrder.environment == campaign.environment,
                        VenueOrder.account_id == campaign.account_id,
                        VenueOrder.venue == campaign.venue,
                        VenueOrder.venue_order_id == protection.venue_order_id,
                    )
                )
            )
            instrument = session.get(Instrument, campaign.instrument_id)
            if protection is None or order is None or instrument is None:
                _reject(
                    "PROTECTION_NOT_FOUND",
                    "campaign has no recorded native protection to cancel",
                )
            expected_type = "TRIGGER_MARKET" if venue == "HYPERLIQUID" else "STOP_MARKET"
            if (
                order.order_type != expected_type
                or not order.reduce_only
                or order.client_order_id == ""
            ):
                _reject(
                    f"{venue}_LIVE_IDENTITY_CONFLICT",
                    "recorded protection order identity is inconsistent",
                )
            return ProtectionCancelCommand(
                symbol=instrument.symbol,
                client_order_id=order.client_order_id,
            )

    def record_live_protection_cancel(
        self,
        campaign_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        command: ProtectionCancelCommand,
        result: BinanceTestnetOrder | HyperliquidTestnetOrder | None,
        *,
        venue: str,
        now: datetime,
    ) -> None:
        with self.database.session_factory.begin() as session:
            self._validate_sender(session, execution_scope, owner_id, fencing_token, now)
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                _reject("CAMPAIGN_NOT_FOUND", "campaign does not exist")
            if (
                campaign.environment != ExecutionEnvironment.LIVE.value
                or campaign.venue != venue
                or execution_scope
                != _scope_key(campaign.environment, campaign.account_id, campaign.venue)
            ):
                _reject("EXECUTION_SCOPE_MISMATCH", "protection cancel is outside LIVE scope")
            self._require_role(
                session, actor_id, "venue.record", campaign.account_id, campaign.venue
            )
            position = session.scalar(
                select(Position).where(
                    Position.account_id == campaign.account_id,
                    Position.venue == campaign.venue,
                    Position.environment == campaign.environment,
                    Position.instrument_id == campaign.instrument_id,
                )
            )
            protection = (
                None
                if position is None
                else session.scalar(
                    select(ProtectionOrder)
                    .where(ProtectionOrder.position_id == position.position_id)
                    .with_for_update()
                )
            )
            order = session.scalar(
                select(VenueOrder)
                .where(
                    VenueOrder.environment == campaign.environment,
                    VenueOrder.account_id == campaign.account_id,
                    VenueOrder.venue == campaign.venue,
                    VenueOrder.client_order_id == command.client_order_id,
                )
                .with_for_update()
            )
            if protection is None or order is None:
                _reject("PROTECTION_NOT_FOUND", "recorded native protection disappeared")
            if result is not None and (
                result.client_order_id != command.client_order_id
                or result.status not in {"CANCELLED", "REJECTED", "FILLED"}
            ):
                _reject(
                    f"{venue}_LIVE_IDENTITY_CONFLICT",
                    "venue did not return a terminal protection cancellation result",
                )
            order.status = VenueOrderStatus.CANCELLED.value if result is None else result.status
            order.observed_at = now if result is None else result.observed_at
            order.updated_at = now
            protection.quantity = Decimal(0)
            protection.status = ProtectionStatus.DEGRADED.value
            protection.fully_covered = False
            protection.observed_at = order.observed_at
            protection.updated_at = now
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type=f"{venue}_LIVE_PROTECTION_CANCELLED",
                object_type="ProtectionOrder",
                object_id=protection.protection_id,
                reason="NOT_FOUND" if result is None else result.status,
                correlation_id=uuid4(),
                object_version=1,
                now=now,
            )

    def record_shadow_order(
        self,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        venue_order_id: str,
        *,
        now: datetime,
    ) -> UUID:
        """Record a synthetic SHADOW send; this method never connects to a venue."""

        with self.database.session_factory.begin() as session:
            self._validate_sender(session, execution_scope, owner_id, fencing_token, now)
            intent = session.get(OrderIntent, intent_id, with_for_update=True)
            if intent is None or intent.status != OrderIntentStatus.READY.value:
                _reject("ORDER_INTENT_NOT_READY", "only a ready intent can be shadow-sent")
            campaign = session.get(Campaign, intent.campaign_id)
            if campaign is None:
                _reject("CAMPAIGN_NOT_FOUND", "intent campaign is missing")
            expected_scope = _scope_key(campaign.environment, campaign.account_id, campaign.venue)
            if execution_scope != expected_scope:
                _reject("EXECUTION_SCOPE_MISMATCH", "sender scope does not match campaign scope")
            self._require_role(
                session, actor_id, "venue.record", campaign.account_id, campaign.venue
            )
            fact = VenueOrder(
                order_intent_id=intent_id,
                account_id=campaign.account_id,
                venue=campaign.venue,
                environment=campaign.environment,
                instrument_id=campaign.instrument_id,
                venue_order_id=venue_order_id,
                client_order_id=venue_order_id,
                side=intent.side,
                order_type="MARKET",
                reduce_only=intent.reduce_only,
                status=VenueOrderStatus.SENT.value,
                ordered_quantity=intent.quantity,
                filled_quantity=Decimal(0),
                observed_at=now,
                updated_at=now,
            )
            session.add(fact)
            previous = intent.status
            intent.status = OrderIntentStatus.SENT.value
            intent.updated_at = now
            intent.version += 1
            session.flush()
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="SHADOW_ORDER_RECORDED",
                object_type="VenueOrder",
                object_id=fact.venue_order_fact_id,
                reason=venue_order_id,
                correlation_id=intent.correlation_id,
                object_version=1,
                now=now,
            )
            INTENT_TRANSITIONS.labels(previous, intent.status).inc()
            return fact.venue_order_fact_id

    def record_fill(
        self,
        intent_id: UUID,
        actor_id: UUID,
        venue_fill_id: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
        fee: Decimal,
        fee_currency: str,
        slippage_cost: Decimal,
        *,
        now: datetime,
    ) -> UUID:
        with self.database.session_factory.begin() as session:
            intent = session.get(OrderIntent, intent_id, with_for_update=True)
            if intent is None:
                _reject("ORDER_INTENT_NOT_FOUND", "intent does not exist")
            campaign = session.get(Campaign, intent.campaign_id)
            if campaign is None:
                _reject("CAMPAIGN_NOT_FOUND", "intent campaign is missing")
            self._require_role(
                session, actor_id, "venue.record", campaign.account_id, campaign.venue
            )
            existing = session.scalar(
                select(VenueFill).where(
                    VenueFill.environment == campaign.environment,
                    VenueFill.account_id == campaign.account_id,
                    VenueFill.venue == campaign.venue,
                    VenueFill.venue_fill_id == venue_fill_id,
                )
            )
            if existing is not None:
                if (
                    existing.order_intent_id == intent_id
                    and existing.campaign_id == campaign.campaign_id
                    and existing.side == side
                    and existing.quantity == quantity
                    and existing.price == price
                    and existing.fee == fee
                    and existing.fee_currency == fee_currency
                    and existing.slippage_cost == slippage_cost
                ):
                    return existing.venue_fill_fact_id
                raise IdempotencyConflict
            if intent.status not in {
                OrderIntentStatus.SENT.value,
                OrderIntentStatus.PARTIALLY_FILLED.value,
            }:
                _reject("ORDER_INTENT_NOT_FILLABLE", "fill requires a sent active intent")
            if side != intent.side:
                _reject("FILL_SIDE_MISMATCH", "fill side must match the order intent")
            if quantity <= 0 or price <= 0 or fee < 0 or slippage_cost < 0:
                _reject("FILL_INVALID", "fill amounts and price are invalid")
            instrument = session.get(Instrument, campaign.instrument_id)
            if instrument is None or fee_currency != instrument.collateral_currency:
                _reject("PNL_CURRENCY_MISMATCH", "fill fee currency lacks an FX conversion")
            venue_order = session.scalar(
                select(VenueOrder).where(VenueOrder.order_intent_id == intent_id)
            )
            if venue_order is None or venue_order.venue != campaign.venue:
                _reject("VENUE_ORDER_MISSING", "fill must reference a known venue order")
            current_filled = session.execute(
                select(func.coalesce(func.sum(VenueFill.quantity), 0)).where(
                    VenueFill.order_intent_id == intent_id
                )
            ).scalar_one()
            if current_filled + quantity > intent.quantity:
                _reject("ORDER_INTENT_OVERFILLED", "cumulative fill exceeds intent quantity")
            fact = VenueFill(
                venue=campaign.venue,
                venue_fill_id=venue_fill_id,
                order_intent_id=intent_id,
                campaign_id=campaign.campaign_id,
                account_id=campaign.account_id,
                environment=campaign.environment,
                instrument_id=campaign.instrument_id,
                side=side,
                quantity=quantity,
                price=price,
                fee=fee,
                fee_currency=fee_currency,
                slippage_cost=slippage_cost,
                executed_at=now,
            )
            session.add(fact)
            session.flush()
            total_filled = current_filled + quantity
            self._consume_add_unit(session, intent)
            previous = intent.status
            if total_filled == intent.quantity:
                intent.status = OrderIntentStatus.FILLED.value
                venue_order.status = VenueOrderStatus.FILLED.value
            else:
                intent.status = OrderIntentStatus.PARTIALLY_FILLED.value
                venue_order.status = VenueOrderStatus.PARTIALLY_FILLED.value
            venue_order.filled_quantity = total_filled
            venue_order.observed_at = now
            venue_order.updated_at = now
            intent.updated_at = now
            intent.version += 1
            if intent.reservation_id is not None:
                reservation = session.get(RiskReservation, intent.reservation_id)
                if reservation is not None:
                    reservation.status = ReservationStatus.OPEN.value
                    reservation.updated_at = now
                    reservation.version += 1
            if intent.kind in {IntentKind.INITIAL.value, IntentKind.ADD.value}:
                campaign.status = CampaignStatus.OPEN.value
            campaign.updated_at = now
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="VENUE_FILL_RECORDED",
                object_type="VenueFill",
                object_id=fact.venue_fill_fact_id,
                reason=venue_fill_id,
                correlation_id=intent.correlation_id,
                object_version=1,
                now=now,
            )
            INTENT_TRANSITIONS.labels(previous, intent.status).inc()
            return fact.venue_fill_fact_id

    def record_position(
        self,
        account_id: str,
        venue: str,
        instrument_id: UUID,
        quantity: Decimal,
        average_entry_price: Decimal,
        mark_price: Decimal,
        known: bool,
        actor_id: UUID,
        *,
        environment: ExecutionEnvironment = ExecutionEnvironment.SHADOW,
        observed_at: datetime | None = None,
        now: datetime,
    ) -> UUID:
        fact_time = now if observed_at is None else observed_at
        if fact_time > now:
            _reject("FACT_TIME_INVALID", "position observation cannot be in the future")
        with self.database.session_factory.begin() as session:
            self._require_role(session, actor_id, "venue.record", account_id, venue)
            position = session.scalar(
                select(Position).where(
                    Position.account_id == account_id,
                    Position.venue == venue,
                    Position.environment == environment.value,
                    Position.instrument_id == instrument_id,
                )
            )
            if position is None:
                position = Position(
                    account_id=account_id,
                    venue=venue,
                    environment=environment.value,
                    instrument_id=instrument_id,
                    quantity=quantity,
                    average_entry_price=average_entry_price,
                    mark_price=mark_price,
                    fact_status=FactStatus.KNOWN.value if known else FactStatus.UNKNOWN.value,
                    observed_at=fact_time,
                    updated_at=now,
                )
                session.add(position)
                session.flush()
            else:
                position.quantity = quantity
                position.average_entry_price = average_entry_price
                position.mark_price = mark_price
                position.fact_status = FactStatus.KNOWN.value if known else FactStatus.UNKNOWN.value
                position.observed_at = fact_time
                position.updated_at = now
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="POSITION_RECORDED",
                object_type="Position",
                object_id=position.position_id,
                reason=position.fact_status,
                correlation_id=uuid4(),
                object_version=1,
                now=now,
            )
            return position.position_id

    def record_protection(
        self,
        position_id: UUID,
        venue_order_id: str,
        quantity: Decimal,
        trigger_price: Decimal,
        fully_covered: bool,
        actor_id: UUID,
        *,
        known: bool = True,
        observed_at: datetime | None = None,
        now: datetime,
    ) -> UUID:
        fact_time = now if observed_at is None else observed_at
        if fact_time > now:
            _reject("FACT_TIME_INVALID", "protection observation cannot be in the future")
        with self.database.session_factory.begin() as session:
            position = session.get(Position, position_id)
            if position is None:
                _reject("POSITION_NOT_FOUND", "protection position is missing")
            self._require_role(
                session, actor_id, "venue.record", position.account_id, position.venue
            )
            protection = session.scalar(
                select(ProtectionOrder).where(ProtectionOrder.position_id == position_id)
            )
            effective_coverage = fully_covered and known
            status = (
                ProtectionStatus.UNKNOWN
                if not known
                else ProtectionStatus.ACTIVE
                if effective_coverage
                else ProtectionStatus.DEGRADED
            )
            if protection is None:
                protection = ProtectionOrder(
                    position_id=position_id,
                    venue_order_id=venue_order_id,
                    quantity=quantity,
                    trigger_price=trigger_price,
                    status=status.value,
                    fully_covered=effective_coverage,
                    observed_at=fact_time,
                    updated_at=now,
                )
                session.add(protection)
                session.flush()
            else:
                protection.venue_order_id = venue_order_id
                protection.quantity = quantity
                protection.trigger_price = trigger_price
                protection.status = status.value
                protection.fully_covered = effective_coverage
                protection.observed_at = fact_time
                protection.updated_at = now
            if not effective_coverage:
                PROTECTION_ISSUES.inc()
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="PROTECTION_RECORDED",
                object_type="ProtectionOrder",
                object_id=protection.protection_id,
                reason=status.value,
                correlation_id=uuid4(),
                object_version=1,
                now=now,
            )
            return protection.protection_id

    def record_account_equity(
        self,
        account_id: str,
        venue: str,
        equity: Decimal,
        available_balance: Decimal,
        currency: str,
        known: bool,
        actor_id: UUID,
        *,
        environment: ExecutionEnvironment = ExecutionEnvironment.SHADOW,
        observed_at: datetime | None = None,
        now: datetime,
    ) -> UUID:
        fact_time = now if observed_at is None else observed_at
        if fact_time > now:
            _reject("FACT_TIME_INVALID", "equity observation cannot be in the future")
        with self.database.session_factory.begin() as session:
            self._require_role(session, actor_id, "venue.record", account_id, venue)
            fact = session.scalar(
                select(AccountEquity).where(
                    AccountEquity.account_id == account_id,
                    AccountEquity.venue == venue,
                    AccountEquity.environment == environment.value,
                    AccountEquity.currency == currency,
                )
            )
            stable = currency.upper() in USD_STABLE_ASSETS
            if fact is None:
                fact = AccountEquity(
                    account_id=account_id,
                    venue=venue,
                    environment=environment.value,
                    equity=equity,
                    available_balance=available_balance,
                    currency=currency,
                    valuation_currency="USD" if stable else None,
                    valuation_price=Decimal(1) if stable else None,
                    valuation_equity=equity if stable else None,
                    valuation_observed_at=fact_time if stable else None,
                    fact_status=FactStatus.KNOWN.value if known else FactStatus.UNKNOWN.value,
                    observed_at=fact_time,
                    updated_at=now,
                )
                session.add(fact)
                session.flush()
            else:
                fact.equity = equity
                fact.available_balance = available_balance
                fact.currency = currency
                fact.valuation_currency = "USD" if stable else None
                fact.valuation_price = Decimal(1) if stable else None
                fact.valuation_equity = equity if stable else None
                fact.valuation_observed_at = fact_time if stable else None
                fact.fact_status = FactStatus.KNOWN.value if known else FactStatus.UNKNOWN.value
                fact.observed_at = fact_time
                fact.updated_at = now
            return fact.account_equity_id

    def record_funding(
        self,
        campaign_id: UUID,
        venue: str,
        venue_payment_id: str,
        amount: Decimal,
        currency: str,
        actor_id: UUID,
        *,
        now: datetime,
    ) -> UUID:
        with self.database.session_factory.begin() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                _reject("CAMPAIGN_NOT_FOUND", "funding campaign is missing")
            self._require_role(
                session, actor_id, "venue.record", campaign.account_id, campaign.venue
            )
            instrument = session.get(Instrument, campaign.instrument_id)
            if venue != campaign.venue:
                _reject("VENUE_SCOPE_MISMATCH", "funding venue does not match campaign")
            if instrument is None or currency != instrument.collateral_currency:
                _reject("PNL_CURRENCY_MISMATCH", "funding currency lacks an FX conversion")
            existing = session.scalar(
                select(FundingPayment).where(
                    FundingPayment.environment == campaign.environment,
                    FundingPayment.account_id == campaign.account_id,
                    FundingPayment.venue == venue,
                    FundingPayment.venue_payment_id == venue_payment_id,
                )
            )
            if existing is not None:
                if (
                    existing.campaign_id == campaign_id
                    and existing.amount == amount
                    and existing.currency == currency
                ):
                    return existing.funding_payment_id
                raise IdempotencyConflict
            payment = FundingPayment(
                campaign_id=campaign_id,
                account_id=campaign.account_id,
                venue=venue,
                environment=campaign.environment,
                instrument_id=campaign.instrument_id,
                venue_payment_id=venue_payment_id,
                amount=amount,
                currency=currency,
                paid_at=now,
            )
            session.add(payment)
            session.flush()
            return payment.funding_payment_id

    def ingest_binance_read_only_snapshot(
        self,
        account_id: str,
        actor_id: UUID,
        snapshot: BinanceReadOnlySnapshot,
        *,
        environment: ExecutionEnvironment,
        now: datetime,
    ) -> dict[str, Any]:
        """Persist one Binance USER_DATA snapshot without any venue-side mutation."""

        return self._ingest_read_only_snapshot(
            account_id,
            actor_id,
            snapshot,
            venue="BINANCE",
            environment=environment,
            now=now,
        )

    def ingest_hyperliquid_read_only_snapshot(
        self,
        account_id: str,
        actor_id: UUID,
        snapshot: HyperliquidReadOnlySnapshot,
        *,
        environment: ExecutionEnvironment,
        now: datetime,
    ) -> dict[str, Any]:
        """Persist one Hyperliquid Core Info snapshot without Exchange actions."""

        return self._ingest_read_only_snapshot(
            account_id,
            actor_id,
            snapshot,
            venue="HYPERLIQUID",
            environment=environment,
            now=now,
        )

    @staticmethod
    def _intent_id_from_client_order(venue: str, client_order_id: str) -> UUID | None:
        raw: str | None = None
        if venue == "BINANCE" and client_order_id.startswith("tcp-"):
            raw = client_order_id.removeprefix("tcp-")
        elif venue == "HYPERLIQUID" and client_order_id.startswith("0x"):
            raw = client_order_id.removeprefix("0x")
        if raw is None:
            return None
        try:
            if venue == "BINANCE" and len(raw) == 22:
                return UUID(bytes=base64.urlsafe_b64decode(f"{raw}=="))
            return _as_uuid(raw)
        except (binascii.Error, ValueError):
            return None

    def _ingest_read_only_snapshot(
        self,
        account_id: str,
        actor_id: UUID,
        snapshot: BinanceReadOnlySnapshot | HyperliquidReadOnlySnapshot,
        *,
        venue: str,
        environment: ExecutionEnvironment,
        now: datetime,
    ) -> dict[str, Any]:
        """Persist one normalized narrow-adapter snapshot into authoritative facts."""

        if snapshot.observed_at > now + MAX_FACT_CLOCK_SKEW:
            _reject("FACT_TIME_INVALID", f"{venue} snapshot is unexpectedly in the future")
        with self.database.session_factory.begin() as session:
            self._require_role(session, actor_id, "venue.record", account_id, venue)
            instrument = session.scalar(
                select(Instrument)
                .where(
                    Instrument.venue == venue,
                    Instrument.symbol == snapshot.symbol,
                )
                .with_for_update()
            )
            if instrument is None:
                instrument = Instrument(
                    venue=venue,
                    symbol=snapshot.symbol,
                    tick_size=snapshot.instrument.tick_size,
                    lot_size=snapshot.instrument.lot_size,
                    minimum_notional=snapshot.instrument.minimum_notional,
                    contract_multiplier=Decimal(1),
                    quote_currency=snapshot.instrument.quote_currency,
                    collateral_currency=snapshot.instrument.collateral_currency,
                    active=snapshot.instrument.active,
                    protection_supported=True,
                    updated_at=now,
                )
                session.add(instrument)
                session.flush()
            else:
                instrument.tick_size = snapshot.instrument.tick_size
                instrument.lot_size = snapshot.instrument.lot_size
                instrument.minimum_notional = snapshot.instrument.minimum_notional
                instrument.quote_currency = snapshot.instrument.quote_currency
                instrument.collateral_currency = snapshot.instrument.collateral_currency
                instrument.active = snapshot.instrument.active
                instrument.updated_at = now

            position = session.scalar(
                select(Position)
                .where(
                    Position.account_id == account_id,
                    Position.venue == venue,
                    Position.environment == environment.value,
                    Position.instrument_id == instrument.instrument_id,
                )
                .with_for_update()
            )
            if position is None:
                position = Position(
                    account_id=account_id,
                    venue=venue,
                    environment=environment.value,
                    instrument_id=instrument.instrument_id,
                    quantity=snapshot.position.quantity,
                    average_entry_price=snapshot.position.average_entry_price,
                    mark_price=snapshot.position.mark_price,
                    fact_status=FactStatus.KNOWN.value,
                    # A current snapshot's freshness starts when this process
                    # successfully receives it. Exchange event timestamps can be
                    # ahead of the host clock and remain attached to orders/fills.
                    observed_at=now,
                    updated_at=now,
                )
                session.add(position)
                session.flush()
            else:
                position.quantity = snapshot.position.quantity
                position.average_entry_price = snapshot.position.average_entry_price
                position.mark_price = snapshot.position.mark_price
                position.fact_status = FactStatus.KNOWN.value
                position.observed_at = now
                position.updated_at = now

            equity = session.scalar(
                select(AccountEquity)
                .where(
                    AccountEquity.account_id == account_id,
                    AccountEquity.venue == venue,
                    AccountEquity.environment == environment.value,
                    AccountEquity.currency == snapshot.equity.currency,
                )
                .with_for_update()
            )
            stable_equity = snapshot.equity.currency.upper() in USD_STABLE_ASSETS
            if equity is None:
                equity = AccountEquity(
                    account_id=account_id,
                    venue=venue,
                    environment=environment.value,
                    equity=snapshot.equity.equity,
                    available_balance=snapshot.equity.available_balance,
                    currency=snapshot.equity.currency,
                    valuation_currency="USD" if stable_equity else None,
                    valuation_price=Decimal(1) if stable_equity else None,
                    valuation_equity=snapshot.equity.equity if stable_equity else None,
                    valuation_observed_at=now if stable_equity else None,
                    fact_status=FactStatus.KNOWN.value,
                    observed_at=now,
                    updated_at=now,
                )
                session.add(equity)
            else:
                equity.equity = snapshot.equity.equity
                equity.available_balance = snapshot.equity.available_balance
                equity.currency = snapshot.equity.currency
                equity.valuation_currency = "USD" if stable_equity else None
                equity.valuation_price = Decimal(1) if stable_equity else None
                equity.valuation_equity = snapshot.equity.equity if stable_equity else None
                equity.valuation_observed_at = now if stable_equity else None
                equity.fact_status = FactStatus.KNOWN.value
                equity.observed_at = now
                equity.updated_at = now

            order_count = 0
            for external_order in snapshot.orders:
                intent: OrderIntent | None = None
                candidate = self._intent_id_from_client_order(venue, external_order.client_order_id)
                if candidate is not None:
                    intent = session.get(OrderIntent, candidate)
                if intent is not None:
                    campaign = session.get(Campaign, intent.campaign_id)
                    if (
                        campaign is None
                        or campaign.account_id != account_id
                        or campaign.venue != venue
                        or campaign.environment != environment.value
                        or campaign.instrument_id != instrument.instrument_id
                    ):
                        _reject(
                            f"{venue}_ORDER_BINDING_INVALID",
                            "client order identity does not match its internal scope",
                        )
                current_order = session.scalar(
                    select(VenueOrder)
                    .where(
                        VenueOrder.environment == environment.value,
                        VenueOrder.account_id == account_id,
                        VenueOrder.venue == venue,
                        VenueOrder.venue_order_id == external_order.order_id,
                    )
                    .with_for_update()
                )
                if current_order is None:
                    current_order = session.scalar(
                        select(VenueOrder)
                        .where(
                            VenueOrder.environment == environment.value,
                            VenueOrder.account_id == account_id,
                            VenueOrder.venue == venue,
                            VenueOrder.client_order_id == external_order.client_order_id,
                        )
                        .with_for_update()
                    )
                if current_order is None:
                    current_order = VenueOrder(
                        order_intent_id=None if intent is None else intent.intent_id,
                        account_id=account_id,
                        venue=venue,
                        environment=environment.value,
                        instrument_id=instrument.instrument_id,
                        venue_order_id=external_order.order_id,
                        client_order_id=external_order.client_order_id,
                        side=external_order.side,
                        order_type=external_order.order_type,
                        reduce_only=external_order.reduce_only or external_order.close_position,
                        status=external_order.status,
                        ordered_quantity=external_order.ordered_quantity,
                        filled_quantity=external_order.filled_quantity,
                        observed_at=external_order.observed_at,
                        updated_at=now,
                    )
                    session.add(current_order)
                else:
                    if (
                        current_order.account_id != account_id
                        or current_order.instrument_id != instrument.instrument_id
                        or current_order.client_order_id != external_order.client_order_id
                        or current_order.side != external_order.side
                        or current_order.order_type != external_order.order_type
                        or current_order.reduce_only
                        != (external_order.reduce_only or external_order.close_position)
                        or (
                            current_order.order_intent_id is not None
                            and intent is not None
                            and current_order.order_intent_id != intent.intent_id
                        )
                    ):
                        _reject(f"{venue}_FACT_CONFLICT", "venue order identity changed scope")
                    if current_order.order_intent_id is None and intent is not None:
                        current_order.order_intent_id = intent.intent_id
                    current_order.venue_order_id = external_order.order_id
                    current_order.status = external_order.status
                    current_order.ordered_quantity = external_order.ordered_quantity
                    current_order.filled_quantity = external_order.filled_quantity
                    current_order.observed_at = external_order.observed_at
                    current_order.updated_at = now
                order_count += 1
            session.flush()

            fill_count = 0
            for external_fill in snapshot.fills:
                current_fill = session.scalar(
                    select(VenueFill).where(
                        VenueFill.environment == environment.value,
                        VenueFill.account_id == account_id,
                        VenueFill.venue == venue,
                        VenueFill.venue_fill_id == external_fill.fill_id,
                    )
                )
                venue_order = session.scalar(
                    select(VenueOrder).where(
                        VenueOrder.environment == environment.value,
                        VenueOrder.account_id == account_id,
                        VenueOrder.venue == venue,
                        VenueOrder.venue_order_id == external_fill.order_id,
                    )
                )
                intent = (
                    session.get(OrderIntent, venue_order.order_intent_id)
                    if venue_order is not None and venue_order.order_intent_id is not None
                    else None
                )
                campaign_id = None if intent is None else intent.campaign_id
                if current_fill is None:
                    session.add(
                        VenueFill(
                            venue=venue,
                            venue_fill_id=external_fill.fill_id,
                            order_intent_id=None if intent is None else intent.intent_id,
                            campaign_id=campaign_id,
                            account_id=account_id,
                            environment=environment.value,
                            instrument_id=instrument.instrument_id,
                            side=external_fill.side,
                            quantity=external_fill.quantity,
                            price=external_fill.price,
                            fee=external_fill.fee,
                            fee_currency=external_fill.fee_currency,
                            slippage_cost=Decimal(0),
                            executed_at=external_fill.executed_at,
                        )
                    )
                elif (
                    current_fill.account_id != account_id
                    or current_fill.instrument_id != instrument.instrument_id
                    or current_fill.side != external_fill.side
                    or current_fill.quantity != external_fill.quantity
                    or current_fill.price != external_fill.price
                    or current_fill.fee != external_fill.fee
                    or current_fill.fee_currency != external_fill.fee_currency
                ):
                    _reject(f"{venue}_FACT_CONFLICT", "venue fill identity changed semantics")
                fill_count += 1

            session.flush()
            bound_orders = session.scalars(
                select(VenueOrder)
                .where(
                    VenueOrder.environment == environment.value,
                    VenueOrder.account_id == account_id,
                    VenueOrder.venue == venue,
                    VenueOrder.instrument_id == instrument.instrument_id,
                    VenueOrder.order_intent_id.is_not(None),
                )
                .with_for_update()
            ).all()
            for bound_order in bound_orders:
                if bound_order.order_intent_id is None:
                    continue
                bound_intent = session.get(
                    OrderIntent, bound_order.order_intent_id, with_for_update=True
                )
                if bound_intent is None:
                    _reject(f"{venue}_ORDER_BINDING_INVALID", "bound order intent is missing")
                bound_campaign = session.get(
                    Campaign, bound_intent.campaign_id, with_for_update=True
                )
                if bound_campaign is None:
                    _reject(f"{venue}_ORDER_BINDING_INVALID", "bound order campaign is missing")
                intent_fills = session.scalars(
                    select(VenueFill).where(VenueFill.order_intent_id == bound_intent.intent_id)
                ).all()
                if any(fill.side != bound_intent.side for fill in intent_fills):
                    _reject("FILL_SIDE_MISMATCH", f"{venue} fill side changed intent semantics")
                filled = sum((fill.quantity for fill in intent_fills), Decimal(0))
                if (
                    filled > bound_intent.quantity
                    or bound_order.filled_quantity > bound_intent.quantity
                ):
                    _reject(
                        "ORDER_INTENT_OVERFILLED",
                        f"{venue} cumulative fill exceeds intent",
                    )
                if filled > bound_order.filled_quantity:
                    bound_order.filled_quantity = filled
                if filled > 0:
                    self._consume_add_unit(session, bound_intent)
                previous = bound_intent.status
                release_updated_intent = False
                terminal = {
                    VenueOrderStatus.CANCELLED.value: OrderIntentStatus.CANCELLED,
                    VenueOrderStatus.REJECTED.value: OrderIntentStatus.REJECTED,
                }
                if bound_order.status == VenueOrderStatus.UNKNOWN.value:
                    bound_intent.status = OrderIntentStatus.UNKNOWN.value
                    if bound_intent.reservation_id is not None:
                        reservation = session.get(
                            RiskReservation, bound_intent.reservation_id, with_for_update=True
                        )
                        if reservation is not None:
                            reservation.status = ReservationStatus.UNKNOWN.value
                            reservation.updated_at = now
                            reservation.version += 1
                    bound_campaign.status = CampaignStatus.UNKNOWN.value
                elif bound_order.status in terminal and filled == 0:
                    self._release_zero_fill_in_session(
                        session,
                        bound_intent,
                        terminal[bound_order.status],
                        confirmed_external=True,
                        now=now,
                    )
                    release_updated_intent = True
                else:
                    if filled == bound_intent.quantity:
                        bound_intent.status = OrderIntentStatus.FILLED.value
                        bound_order.status = VenueOrderStatus.FILLED.value
                    elif filled > 0 and bound_order.status not in terminal:
                        bound_intent.status = OrderIntentStatus.PARTIALLY_FILLED.value
                        bound_order.status = VenueOrderStatus.PARTIALLY_FILLED.value
                    elif bound_order.status in terminal:
                        bound_intent.status = terminal[bound_order.status].value
                    if filled > 0 and bound_intent.reservation_id is not None:
                        reservation = session.get(
                            RiskReservation, bound_intent.reservation_id, with_for_update=True
                        )
                        if reservation is not None:
                            reservation.status = ReservationStatus.OPEN.value
                            reservation.updated_at = now
                            reservation.version += 1
                    if filled > 0:
                        if bound_intent.kind in {IntentKind.INITIAL.value, IntentKind.ADD.value}:
                            if bound_campaign.status not in {
                                CampaignStatus.CLOSING.value,
                                CampaignStatus.REDUCING.value,
                            }:
                                bound_campaign.status = CampaignStatus.OPEN.value
                        elif bound_intent.kind == IntentKind.EXIT.value:
                            bound_campaign.status = CampaignStatus.CLOSING.value
                        else:
                            bound_campaign.status = CampaignStatus.REDUCING.value
                if previous != bound_intent.status:
                    if not release_updated_intent:
                        bound_intent.updated_at = now
                        bound_intent.version += 1
                    INTENT_TRANSITIONS.labels(previous, bound_intent.status).inc()
                bound_order.updated_at = now
                bound_campaign.updated_at = now

            funding_count = 0
            funding_campaign = session.scalar(
                select(Campaign)
                .where(
                    Campaign.account_id == account_id,
                    Campaign.venue == venue,
                    Campaign.environment == environment.value,
                    Campaign.instrument_id == instrument.instrument_id,
                    Campaign.status != CampaignStatus.CLOSED.value,
                )
                .with_for_update()
            )
            for external_funding in snapshot.funding:
                current_funding = session.scalar(
                    select(FundingPayment).where(
                        FundingPayment.environment == environment.value,
                        FundingPayment.account_id == account_id,
                        FundingPayment.venue == venue,
                        FundingPayment.venue_payment_id == external_funding.payment_id,
                    )
                )
                if current_funding is None:
                    session.add(
                        FundingPayment(
                            campaign_id=(
                                None if funding_campaign is None else funding_campaign.campaign_id
                            ),
                            account_id=account_id,
                            venue=venue,
                            environment=environment.value,
                            instrument_id=instrument.instrument_id,
                            venue_payment_id=external_funding.payment_id,
                            amount=external_funding.amount,
                            currency=external_funding.currency,
                            paid_at=external_funding.paid_at,
                        )
                    )
                elif (
                    current_funding.account_id != account_id
                    or current_funding.instrument_id != instrument.instrument_id
                    or current_funding.amount != external_funding.amount
                    or current_funding.currency != external_funding.currency
                ):
                    _reject(f"{venue}_FACT_CONFLICT", "funding identity changed semantics")
                elif current_funding.campaign_id is None and funding_campaign is not None:
                    current_funding.campaign_id = funding_campaign.campaign_id
                funding_count += 1

            protection = session.scalar(
                select(ProtectionOrder)
                .where(ProtectionOrder.position_id == position.position_id)
                .with_for_update()
            )
            if snapshot.position.quantity != 0:
                observed_protection = snapshot.protection
                covered = (
                    observed_protection is not None
                    and observed_protection.quantity >= abs(snapshot.position.quantity)
                    and observed_protection.trigger_price > 0
                )
                if protection is None:
                    protection = ProtectionOrder(
                        position_id=position.position_id,
                        venue_order_id=(
                            f"{venue}:UNKNOWN:{snapshot.symbol}"
                            if observed_protection is None
                            else observed_protection.order_id
                        ),
                        quantity=(
                            Decimal(0)
                            if observed_protection is None
                            else observed_protection.quantity
                        ),
                        trigger_price=(
                            Decimal(0)
                            if observed_protection is None
                            else observed_protection.trigger_price
                        ),
                        status=(
                            ProtectionStatus.ACTIVE.value
                            if covered
                            else ProtectionStatus.UNKNOWN.value
                            if observed_protection is None
                            else ProtectionStatus.DEGRADED.value
                        ),
                        fully_covered=covered,
                        observed_at=now,
                        updated_at=now,
                    )
                    session.add(protection)
                else:
                    protection.venue_order_id = (
                        f"{venue}:UNKNOWN:{snapshot.symbol}"
                        if observed_protection is None
                        else observed_protection.order_id
                    )
                    protection.quantity = (
                        Decimal(0) if observed_protection is None else observed_protection.quantity
                    )
                    protection.trigger_price = (
                        Decimal(0)
                        if observed_protection is None
                        else observed_protection.trigger_price
                    )
                    protection.status = (
                        ProtectionStatus.ACTIVE.value
                        if covered
                        else ProtectionStatus.UNKNOWN.value
                        if observed_protection is None
                        else ProtectionStatus.DEGRADED.value
                    )
                    protection.fully_covered = covered
                    protection.observed_at = now
                    protection.updated_at = now
            elif protection is not None:
                protection_order = session.scalar(
                    select(VenueOrder)
                    .where(
                        VenueOrder.environment == environment.value,
                        VenueOrder.account_id == account_id,
                        VenueOrder.venue == venue,
                        VenueOrder.venue_order_id == protection.venue_order_id,
                    )
                    .with_for_update()
                )
                observed_order_ids = {item.order_id for item in snapshot.orders}
                if protection_order is not None and protection_order.status in {
                    VenueOrderStatus.SENT.value,
                    VenueOrderStatus.PARTIALLY_FILLED.value,
                    VenueOrderStatus.UNKNOWN.value,
                }:
                    still_open = protection_order.venue_order_id in observed_order_ids
                    if not still_open:
                        protection_order.status = VenueOrderStatus.UNKNOWN.value
                    protection_order.observed_at = snapshot.observed_at
                    protection_order.updated_at = now
                    protection.quantity = Decimal(0)
                    protection.status = (
                        ProtectionStatus.DEGRADED.value
                        if still_open
                        else ProtectionStatus.UNKNOWN.value
                    )
                    protection.fully_covered = False
                    protection.observed_at = snapshot.observed_at
                    protection.updated_at = now
                else:
                    session.delete(protection)
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type=f"{venue}_READ_ONLY_SYNCED",
                object_type="Instrument",
                object_id=instrument.instrument_id,
                reason=(
                    f"orders={order_count},fills={fill_count},funding={funding_count},"
                    f"position={snapshot.position.quantity}"
                ),
                correlation_id=uuid4(),
                object_version=1,
                now=now,
            )
            return {
                "instrument_id": str(instrument.instrument_id),
                "orders": order_count,
                "fills": fill_count,
                "funding": funding_count,
            }

    def update_campaign_target(
        self,
        campaign_id: UUID,
        actor_id: UUID,
        candidates: tuple[TargetCandidate, ...],
        *,
        now: datetime,
    ) -> TargetDecision:
        decision = select_target_position(candidates)
        with self.database.session_factory.begin() as session:
            campaign = session.get(Campaign, campaign_id, with_for_update=True)
            if campaign is None:
                _reject("CAMPAIGN_NOT_FOUND", "campaign does not exist")
            self._require_role(
                session, actor_id, "order.prepare", campaign.account_id, campaign.venue
            )
            policy = session.scalar(select(RiskPolicy).where(RiskPolicy.active))
            if policy is None:
                _reject("RISK_POLICY_MISSING", "target update requires an active policy")
            position = session.scalar(
                select(Position)
                .where(
                    Position.account_id == campaign.account_id,
                    Position.venue == campaign.venue,
                    Position.environment == campaign.environment,
                    Position.instrument_id == campaign.instrument_id,
                )
                .with_for_update()
            )
            if position is None or position.fact_status != FactStatus.KNOWN.value:
                _reject("POSITION_UNKNOWN", "target update requires a known position")
            if now - position.observed_at > timedelta(seconds=policy.max_fact_age_seconds):
                _reject("STALE_FACTS", "target update requires a fresh position")
            if decision.target_quantity > abs(position.quantity):
                _reject("TARGET_EXCEEDS_POSITION", "target cannot exceed the current position")
            campaign.current_target_quantity = decision.target_quantity
            campaign.target_version += 1
            campaign.target_reason = ",".join(decision.reasons)
            campaign.target_urgency = decision.urgency.value
            campaign.target_calculated_at = now
            if decision.target_quantity == 0:
                campaign.status = CampaignStatus.CLOSING.value
            elif decision.target_quantity < abs(position.quantity):
                campaign.status = CampaignStatus.REDUCING.value
            else:
                campaign.status = CampaignStatus.OPEN.value
            campaign.updated_at = now
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="CAMPAIGN_TARGET_UPDATED",
                object_type="Campaign",
                object_id=campaign.campaign_id,
                reason=campaign.target_reason,
                correlation_id=uuid4(),
                object_version=campaign.target_version,
                now=now,
            )
        return decision

    def create_reduction_intent(
        self,
        campaign_id: UUID,
        actor_id: UUID,
        idempotency_key: str,
        *,
        candidates: tuple[TargetCandidate, ...] | None = None,
        expected_target_version: int | None = None,
        limit_price: Decimal | None = None,
        now: datetime,
    ) -> UUID:
        operation = "order.reduce"
        with self.database.session_factory.begin() as session:
            campaign = session.get(Campaign, campaign_id, with_for_update=True)
            if campaign is None:
                _reject("CAMPAIGN_NOT_FOUND", "campaign does not exist")
            self._require_role(
                session, actor_id, "order.prepare", campaign.account_id, campaign.venue
            )
            position = session.scalar(
                select(Position).where(
                    Position.account_id == campaign.account_id,
                    Position.venue == campaign.venue,
                    Position.environment == campaign.environment,
                    Position.instrument_id == campaign.instrument_id,
                )
            )
            if position is None or position.fact_status != FactStatus.KNOWN.value:
                _reject("POSITION_UNKNOWN", "reduction requires current known position")
            policy = session.scalar(select(RiskPolicy).where(RiskPolicy.active))
            if policy is None:
                _reject("RISK_POLICY_MISSING", "reduction requires an active policy")
            if now - position.observed_at > timedelta(seconds=policy.max_fact_age_seconds):
                _reject("STALE_FACTS", "reduction requires a fresh position")
            expected_long = campaign.direction == Direction.LONG.value
            if position.quantity == 0 or (position.quantity > 0) != expected_long:
                _reject("POSITION_DIRECTION_MISMATCH", "position does not match campaign direction")
            if candidates is None:
                payload = {
                    "campaign_id": str(campaign_id),
                    "target_version": campaign.target_version,
                    "target_quantity": str(campaign.current_target_quantity),
                    "position_quantity": str(position.quantity),
                    "limit_price": None if limit_price is None else str(limit_price),
                    "expected_target_version": expected_target_version,
                }
            else:
                payload = {
                    "campaign_id": str(campaign_id),
                    "candidates": [
                        {
                            "target_quantity": str(candidate.target_quantity),
                            "urgency": candidate.urgency.value,
                            "reason": candidate.reason,
                        }
                        for candidate in candidates
                    ],
                    "limit_price": None if limit_price is None else str(limit_price),
                    "expected_target_version": expected_target_version,
                }
            if limit_price is not None and (not limit_price.is_finite() or limit_price <= 0):
                _reject("ORDER_LIMIT_PRICE_INVALID", "explicit reduction limit must be positive")
            digest, response = self._idempotency(
                session,
                caller_id=str(actor_id),
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if response is not None:
                return _as_uuid(str(response["intent_id"]))
            if (
                expected_target_version is not None
                and campaign.target_version != expected_target_version
            ):
                _reject("VERSION_CONFLICT", "Campaign target changed before the action")
            active = session.scalar(
                select(OrderIntent.intent_id).where(
                    OrderIntent.campaign_id == campaign_id,
                    OrderIntent.status.in_(ACTIVE_INTENT_STATUSES),
                )
            )
            if active is not None:
                _reject("ACTIVE_ORDER_INTENT", "campaign already has unresolved intent")
            if candidates is not None:
                effective_candidates = candidates
                if campaign.target_calculated_at is not None:
                    try:
                        existing_urgency = TargetUrgency(
                            campaign.target_urgency or TargetUrgency.NORMAL.value
                        )
                    except ValueError:
                        _reject("CAMPAIGN_TARGET_INVALID", "stored target urgency is invalid")
                    effective_candidates += (
                        TargetCandidate(
                            campaign.current_target_quantity,
                            existing_urgency,
                            campaign.target_reason or "existing campaign target",
                        ),
                    )
                decision = select_target_position(effective_candidates)
                if decision.target_quantity > abs(position.quantity):
                    _reject("TARGET_EXCEEDS_POSITION", "target cannot exceed the current position")
                campaign.current_target_quantity = decision.target_quantity
                campaign.target_version += 1
                campaign.target_reason = ",".join(decision.reasons)
                campaign.target_urgency = decision.urgency.value
                campaign.target_calculated_at = now
                campaign.status = (
                    CampaignStatus.CLOSING.value
                    if decision.target_quantity == 0
                    else CampaignStatus.REDUCING.value
                )
                campaign.updated_at = now
            reduction_quantity = abs(position.quantity) - campaign.current_target_quantity
            if reduction_quantity <= 0:
                _reject("TARGET_NOT_REDUCING", "target does not reduce current position")
            side = "SELL" if position.quantity > 0 else "BUY"
            kind = IntentKind.EXIT if campaign.current_target_quantity == 0 else IntentKind.REDUCE
            intent = OrderIntent(
                campaign_id=campaign_id,
                authorization_id=campaign.authorization_id,
                reservation_id=None,
                kind=kind.value,
                side=side,
                quantity=reduction_quantity,
                limit_price=limit_price,
                reduce_only=True,
                trigger_source="CAMPAIGN_TARGET",
                trigger_observed_at=position.observed_at,
                add_unit_consumed=False,
                target_version=campaign.target_version,
                position_id=position.position_id,
                position_observed_at=position.observed_at,
                status=OrderIntentStatus.READY.value,
                semantic_hash=digest,
                actor_id=str(actor_id),
                correlation_id=uuid4(),
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(intent)
            session.flush()
            result = {"intent_id": str(intent.intent_id)}
            self._save_receipt(
                session,
                caller_id=str(actor_id),
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="REDUCTION_INTENT_PREPARED",
                object_type="OrderIntent",
                object_id=intent.intent_id,
                reason=campaign.target_reason or kind.value,
                correlation_id=intent.correlation_id,
                object_version=intent.version,
                idempotency_key=idempotency_key,
                now=now,
            )
            return intent.intent_id

    def create_automatic_exit_intent(
        self,
        campaign_id: UUID,
        actor_id: UUID,
        idempotency_key: str,
        *,
        limit_price: Decimal | None = None,
        now: datetime,
    ) -> tuple[str, UUID | None]:
        operation = "campaign.auto_exit"
        payload = {
            "campaign_id": str(campaign_id),
            "limit_price": None if limit_price is None else str(limit_price),
        }
        with self.database.session_factory.begin() as session:
            campaign = session.get(Campaign, campaign_id, with_for_update=True)
            if campaign is None:
                _reject("CAMPAIGN_NOT_FOUND", "campaign does not exist")
            self._require_role(
                session, actor_id, "risk.tighten", campaign.account_id, campaign.venue
            )
            digest, response = self._idempotency(
                session,
                caller_id=str(actor_id),
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if response is not None:
                intent_value = response.get("intent_id")
                return (
                    str(response["reason"]),
                    None if intent_value is None else _as_uuid(str(intent_value)),
                )
            if limit_price is not None and (not limit_price.is_finite() or limit_price <= 0):
                _reject("ORDER_LIMIT_PRICE_INVALID", "explicit exit limit must be positive")
            proposal = session.get(Proposal, campaign.proposal_id)
            position = session.scalar(
                select(Position)
                .where(
                    Position.account_id == campaign.account_id,
                    Position.venue == campaign.venue,
                    Position.environment == campaign.environment,
                    Position.instrument_id == campaign.instrument_id,
                )
                .with_for_update()
            )
            policy = self._active_risk_policy(session)
            if proposal is None or policy is None:
                _reject("CAMPAIGN_MANAGEMENT_INVALID", "campaign management facts are incomplete")
            if position is None or position.fact_status != FactStatus.KNOWN.value:
                _reject("POSITION_UNKNOWN", "automatic exit requires a known current position")
            if self._fact_is_stale(
                position.observed_at,
                now,
                timedelta(seconds=policy.max_fact_age_seconds),
            ):
                _reject("STALE_FACTS", "automatic exit requires a fresh position")
            if position.quantity == 0:
                result: dict[str, Any] = {"reason": "POSITION_FLAT", "intent_id": None}
                self._save_receipt(
                    session,
                    caller_id=str(actor_id),
                    operation=operation,
                    idempotency_key=idempotency_key,
                    semantic_hash=digest,
                    response=result,
                    now=now,
                )
                return "POSITION_FLAT", None
            details = proposal.frozen_payload.get("details")
            if not isinstance(details, dict) or details.get("invalidation_price") is None:
                _reject(
                    "PROPOSAL_EXIT_CONTRACT_INVALID",
                    "frozen proposal lacks an invalidation price",
                )
            try:
                invalidation = Decimal(str(details["invalidation_price"]))
            except (ArithmeticError, TypeError, ValueError):
                _reject(
                    "PROPOSAL_EXIT_CONTRACT_INVALID",
                    "frozen invalidation price is invalid",
                )
            if not invalidation.is_finite() or invalidation <= 0:
                _reject(
                    "PROPOSAL_EXIT_CONTRACT_INVALID",
                    "frozen invalidation price is invalid",
                )
            kill_switch = policy.system_state == SystemRiskState.KILL_SWITCH.value
            invalidated = (
                position.mark_price <= invalidation
                if campaign.direction == Direction.LONG.value
                else position.mark_price >= invalidation
            )
            if not kill_switch and not invalidated:
                result = {"reason": "EXIT_TRIGGER_NOT_MET", "intent_id": None}
                self._save_receipt(
                    session,
                    caller_id=str(actor_id),
                    operation=operation,
                    idempotency_key=idempotency_key,
                    semantic_hash=digest,
                    response=result,
                    now=now,
                )
                return "EXIT_TRIGGER_NOT_MET", None
            reason = "KILL_SWITCH" if kill_switch else "FROZEN_INVALIDATION_REACHED"
            active = session.scalar(
                select(OrderIntent.intent_id).where(
                    OrderIntent.campaign_id == campaign_id,
                    OrderIntent.status.in_(ACTIVE_INTENT_STATUSES),
                )
            )
            if active is not None:
                _reject("ACTIVE_ORDER_INTENT", "campaign already has unresolved intent")
            existing_reason = () if campaign.target_reason is None else (campaign.target_reason,)
            target_reasons = tuple(sorted({*existing_reason, reason}))
            campaign.current_target_quantity = Decimal(0)
            campaign.target_version += 1
            campaign.target_reason = ",".join(target_reasons)
            campaign.target_urgency = TargetUrgency.IMMEDIATE.value
            campaign.target_calculated_at = now
            campaign.status = CampaignStatus.CLOSING.value
            campaign.updated_at = now
            intent = OrderIntent(
                campaign_id=campaign_id,
                authorization_id=campaign.authorization_id,
                reservation_id=None,
                kind=IntentKind.EXIT.value,
                side="SELL" if position.quantity > 0 else "BUY",
                quantity=abs(position.quantity),
                limit_price=limit_price,
                reduce_only=True,
                trigger_source=reason,
                trigger_observed_at=position.observed_at,
                add_unit_consumed=False,
                target_version=campaign.target_version,
                position_id=position.position_id,
                position_observed_at=position.observed_at,
                status=OrderIntentStatus.READY.value,
                semantic_hash=digest,
                actor_id=str(actor_id),
                correlation_id=uuid4(),
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(intent)
            session.flush()
            result = {"reason": reason, "intent_id": str(intent.intent_id)}
            self._save_receipt(
                session,
                caller_id=str(actor_id),
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="AUTOMATIC_EXIT_PREPARED",
                object_type="OrderIntent",
                object_id=intent.intent_id,
                reason=reason,
                correlation_id=intent.correlation_id,
                object_version=intent.version,
                idempotency_key=idempotency_key,
                now=now,
            )
            return reason, intent.intent_id

    def disable_campaign_auto_add(
        self,
        campaign_id: UUID,
        actor_id: UUID,
        idempotency_key: str,
        *,
        reason: str,
        expected_target_version: int | None = None,
        now: datetime,
    ) -> int:
        operation = "campaign.auto_add.disable"
        payload = {
            "campaign_id": str(campaign_id),
            "reason": reason,
            "expected_target_version": expected_target_version,
        }
        with self.database.session_factory.begin() as session:
            campaign = session.get(Campaign, campaign_id, with_for_update=True)
            if campaign is None:
                _reject("CAMPAIGN_NOT_FOUND", "campaign does not exist")
            self._require_role(
                session, actor_id, "risk.tighten", campaign.account_id, campaign.venue
            )
            digest, response = self._idempotency(
                session,
                caller_id=str(actor_id),
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if response is not None:
                return int(response["allowed_adds"])
            if (
                expected_target_version is not None
                and campaign.target_version != expected_target_version
            ):
                _reject("VERSION_CONFLICT", "Campaign target changed before the action")
            authorization = session.get(
                TradingAuthorization, campaign.authorization_id, with_for_update=True
            )
            if authorization is None:
                _reject("AUTHORIZATION_INACTIVE", "campaign authorization is missing")
            unresolved_add = session.scalar(
                select(OrderIntent.intent_id).where(
                    OrderIntent.campaign_id == campaign_id,
                    OrderIntent.kind == IntentKind.ADD.value,
                    OrderIntent.add_unit_consumed.is_(False),
                    OrderIntent.status.in_(ACTIVE_INTENT_STATUSES),
                )
            )
            authorization.allowed_adds = authorization.used_adds + (
                1 if unresolved_add is not None else 0
            )
            authorization.active = False
            result = {"allowed_adds": authorization.allowed_adds}
            self._save_receipt(
                session,
                caller_id=str(actor_id),
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="CAMPAIGN_AUTO_ADD_DISABLED",
                object_type="Campaign",
                object_id=campaign.campaign_id,
                reason=reason,
                correlation_id=uuid4(),
                object_version=campaign.target_version,
                idempotency_key=idempotency_key,
                now=now,
            )
            return authorization.allowed_adds

    def disable_global_auto_add(
        self,
        actor_id: UUID,
        idempotency_key: str,
        *,
        reason: str,
        now: datetime,
    ) -> None:
        operation = "auto_add.disable"
        payload = {"reason": reason}
        with self.database.session_factory.begin() as session:
            self._require_role(session, actor_id, "risk.tighten")
            digest, response = self._idempotency(
                session,
                caller_id=str(actor_id),
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if response is not None:
                return
            gate = session.get(CapabilityGate, "AUTO_ADD", with_for_update=True)
            if gate is None:
                _reject("CAPABILITY_GATE_NOT_FOUND", "AUTO_ADD gate is missing")
            gate.status = CapabilityStatus.DISABLED.value
            gate.reason = reason
            gate.operator_id = str(actor_id)
            gate.updated_at = now
            self._save_receipt(
                session,
                caller_id=str(actor_id),
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response={"status": gate.status},
                now=now,
            )
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="AUTO_ADD_DISABLED",
                object_type="CapabilityGate",
                object_id="AUTO_ADD",
                reason=reason,
                correlation_id=uuid4(),
                object_version=1,
                idempotency_key=idempotency_key,
                now=now,
            )

    def pause_new_risk(
        self,
        actor_id: UUID,
        idempotency_key: str,
        *,
        reason: str,
        now: datetime,
    ) -> SystemRiskState:
        operation = "risk.pause_new"
        payload = {"reason": reason}
        with self.database.session_factory.begin() as session:
            self._require_role(session, actor_id, "risk.tighten")
            digest, response = self._idempotency(
                session,
                caller_id=str(actor_id),
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if response is not None:
                return SystemRiskState(str(response["system_state"]))
            self._lock_risk_capacity(session)
            policy = self._active_risk_policy(session)
            if policy.system_state != SystemRiskState.KILL_SWITCH.value:
                policy.system_state = SystemRiskState.REDUCE_ONLY.value
                policy.updated_by = str(actor_id)
                policy.updated_at = now
            result = {"system_state": policy.system_state}
            self._save_receipt(
                session,
                caller_id=str(actor_id),
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="NEW_RISK_PAUSED",
                object_type="RiskPolicy",
                object_id=policy.policy_id,
                reason=reason,
                correlation_id=uuid4(),
                object_version=1,
                idempotency_key=idempotency_key,
                now=now,
            )
            return SystemRiskState(policy.system_state)

    def record_scope_reconciliation(
        self,
        execution_scope: str,
        actor_id: UUID,
        status: ReconciliationStatus,
        differences: tuple[str, ...],
        *,
        now: datetime,
        campaign_id: UUID | None = None,
    ) -> UUID:
        if status in {ReconciliationStatus.MATCH, ReconciliationStatus.RESOLVED}:
            _reject(
                "RECONCILIATION_STATUS_NOT_TRUSTED",
                "MATCH must be computed and RESOLVED requires a manual transition",
            )
        with self.database.session_factory.begin() as session:
            _environment, account_id, venue = _scope_parts(execution_scope)
            self._require_role(session, actor_id, "reconcile", account_id, venue)
            run = ReconciliationRun(
                execution_scope=execution_scope,
                campaign_id=campaign_id,
                status=status.value,
                is_computed=False,
                differences=list(differences),
                resolution_reason=None,
                actor_id=str(actor_id),
                correlation_id=uuid4(),
                started_at=now,
                completed_at=now,
            )
            session.add(run)
            session.flush()
            RECONCILIATION_RESULTS.labels(status.value).inc()
            return run.reconciliation_id

    def require_manual_reconciliation(
        self, reconciliation_id: UUID, actor_id: UUID, reason: str, *, now: datetime
    ) -> UUID:
        with self.database.session_factory.begin() as session:
            run = session.get(ReconciliationRun, reconciliation_id, with_for_update=True)
            if run is None:
                _reject("RECONCILIATION_NOT_FOUND", "run does not exist")
            _environment, account_id, venue = _scope_parts(run.execution_scope)
            self._require_role(session, actor_id, "reconcile", account_id, venue)
            if run.status not in {
                ReconciliationStatus.DIFFERENCE.value,
                ReconciliationStatus.UNKNOWN.value,
            }:
                _reject(
                    "RECONCILIATION_TRANSITION_INVALID",
                    "only DIFFERENCE or UNKNOWN may require manual handling",
                )
            run.status = ReconciliationStatus.MANUAL_REQUIRED.value
            run.resolution_reason = reason
            run.completed_at = now
            return run.reconciliation_id

    def resolve_reconciliation(
        self, reconciliation_id: UUID, actor_id: UUID, reason: str, *, now: datetime
    ) -> UUID:
        with self.database.session_factory.begin() as session:
            run = session.get(ReconciliationRun, reconciliation_id, with_for_update=True)
            if run is None:
                _reject("RECONCILIATION_NOT_FOUND", "run does not exist")
            _environment, account_id, venue = _scope_parts(run.execution_scope)
            self._require_role(session, actor_id, "reconcile", account_id, venue)
            if run.status != ReconciliationStatus.MANUAL_REQUIRED.value:
                _reject(
                    "RECONCILIATION_TRANSITION_INVALID",
                    "only MANUAL_REQUIRED may be resolved",
                )
            run.status = ReconciliationStatus.RESOLVED.value
            run.resolution_reason = reason
            run.completed_at = now
            return run.reconciliation_id

    def reconciliation_status(self, reconciliation_id: UUID) -> ReconciliationStatus:
        with self.database.session_factory() as session:
            run = session.get(ReconciliationRun, reconciliation_id)
            if run is None:
                _reject("RECONCILIATION_NOT_FOUND", "run does not exist")
            return ReconciliationStatus(run.status)

    @staticmethod
    def _fact_is_stale(observed_at: datetime, now: datetime, max_age: timedelta) -> bool:
        return observed_at > now + MAX_FACT_CLOCK_SKEW or now - observed_at > max_age

    def reconcile_scope(
        self,
        execution_scope: str,
        actor_id: UUID,
        *,
        now: datetime,
    ) -> UUID:
        environment, account_id, venue = _scope_parts(execution_scope)
        with self.database.session_factory.begin() as session:
            self._require_role(session, actor_id, "reconcile", account_id, venue)
            policy = session.scalar(select(RiskPolicy).where(RiskPolicy.active))
            max_age = (
                timedelta(seconds=policy.max_fact_age_seconds)
                if policy is not None
                else timedelta(0)
            )
            campaigns = session.scalars(
                select(Campaign)
                .where(
                    Campaign.account_id == account_id,
                    Campaign.venue == venue,
                    Campaign.environment == environment.value,
                    Campaign.status != CampaignStatus.CLOSED.value,
                )
                .order_by(Campaign.created_at, Campaign.campaign_id)
                .with_for_update()
            ).all()
            equity = session.scalar(
                select(AccountEquity)
                .where(
                    AccountEquity.account_id == account_id,
                    AccountEquity.venue == venue,
                    AccountEquity.environment == environment.value,
                )
                .with_for_update()
            )
            differences: list[str] = []
            unknown: list[str] = []
            if policy is None:
                unknown.append("RISK_POLICY_UNKNOWN")
            if equity is None or equity.fact_status != FactStatus.KNOWN.value:
                unknown.append("ACCOUNT_EQUITY_UNKNOWN")
            elif self._fact_is_stale(equity.observed_at, now, max_age):
                unknown.append("ACCOUNT_EQUITY_STALE")

            protection_order_ids = set(
                session.scalars(
                    select(ProtectionOrder.venue_order_id)
                    .join(Position, ProtectionOrder.position_id == Position.position_id)
                    .where(
                        Position.account_id == account_id,
                        Position.venue == venue,
                        Position.environment == environment.value,
                    )
                ).all()
            )
            unbound_orders = session.scalars(
                select(VenueOrder).where(
                    VenueOrder.account_id == account_id,
                    VenueOrder.venue == venue,
                    VenueOrder.environment == environment.value,
                    VenueOrder.order_intent_id.is_(None),
                    VenueOrder.status.in_(
                        {
                            VenueOrderStatus.SENT.value,
                            VenueOrderStatus.PARTIALLY_FILLED.value,
                            VenueOrderStatus.UNKNOWN.value,
                        }
                    ),
                )
            ).all()
            for unbound_order in unbound_orders:
                if unbound_order.venue_order_id in protection_order_ids:
                    continue
                if unbound_order.status == VenueOrderStatus.UNKNOWN.value:
                    unknown.append(f"EXTERNAL_ORDER_UNKNOWN:{unbound_order.venue_order_id}")
                else:
                    differences.append(f"EXTERNAL_ORDER_UNBOUND:{unbound_order.venue_order_id}")

            active_instrument_ids = {campaign.instrument_id for campaign in campaigns}
            scope_positions = session.scalars(
                select(Position).where(
                    Position.account_id == account_id,
                    Position.venue == venue,
                    Position.environment == environment.value,
                )
            ).all()
            for scope_position in scope_positions:
                if (
                    scope_position.quantity != 0
                    and scope_position.instrument_id not in active_instrument_ids
                ):
                    differences.append(f"EXTERNAL_POSITION_UNBOUND:{scope_position.instrument_id}")

            for campaign in campaigns:
                scope_suffix = str(campaign.campaign_id)
                instrument = session.get(Instrument, campaign.instrument_id)
                if instrument is None:
                    unknown.append(f"INSTRUMENT_UNKNOWN:{scope_suffix}")
                elif equity is not None and equity.currency != instrument.collateral_currency:
                    differences.append(f"EQUITY_CURRENCY_MISMATCH:{scope_suffix}")
                intents = session.scalars(
                    select(OrderIntent)
                    .where(OrderIntent.campaign_id == campaign.campaign_id)
                    .order_by(OrderIntent.created_at, OrderIntent.intent_id)
                    .with_for_update()
                ).all()
                fills = session.scalars(
                    select(VenueFill).where(VenueFill.campaign_id == campaign.campaign_id)
                ).all()
                reservations = session.scalars(
                    select(RiskReservation)
                    .where(RiskReservation.campaign_id == campaign.campaign_id)
                    .with_for_update()
                ).all()
                if not intents:
                    differences.append(f"ORDER_INTENT_MISSING:{scope_suffix}")
                for reservation in reservations:
                    if reservation.status == ReservationStatus.UNKNOWN.value:
                        unknown.append(f"RISK_RESERVATION_UNKNOWN:{reservation.reservation_id}")

                for intent in intents:
                    intent_fills = [
                        fill for fill in fills if fill.order_intent_id == intent.intent_id
                    ]
                    intent_fill_quantity = sum((fill.quantity for fill in intent_fills), Decimal(0))
                    intent_order = session.scalar(
                        select(VenueOrder)
                        .where(VenueOrder.order_intent_id == intent.intent_id)
                        .with_for_update()
                    )
                    order_required = intent.status in {
                        OrderIntentStatus.SENT.value,
                        OrderIntentStatus.PARTIALLY_FILLED.value,
                        OrderIntentStatus.FILLED.value,
                        OrderIntentStatus.UNKNOWN.value,
                    }
                    if intent_order is None and order_required:
                        differences.append(f"VENUE_ORDER_MISSING:{intent.intent_id}")
                    elif intent_order is not None:
                        if intent_order.venue != venue:
                            differences.append(f"VENUE_ORDER_SCOPE_MISMATCH:{intent.intent_id}")
                        if intent_order.filled_quantity != intent_fill_quantity:
                            differences.append(f"ORDER_FILL_MISMATCH:{intent.intent_id}")
                        if intent_order.status == VenueOrderStatus.UNKNOWN.value:
                            unknown.append(f"VENUE_ORDER_UNKNOWN:{intent.intent_id}")
                        elif self._fact_is_stale(intent_order.observed_at, now, max_age):
                            unknown.append(f"VENUE_ORDER_STALE:{intent.intent_id}")
                    if intent_fill_quantity > intent.quantity:
                        differences.append(f"ORDER_INTENT_OVERFILLED:{intent.intent_id}")
                    if intent.status == OrderIntentStatus.UNKNOWN.value:
                        unknown.append(f"ORDER_INTENT_UNKNOWN:{intent.intent_id}")
                    if intent.status == OrderIntentStatus.FILLED.value and (
                        intent_fill_quantity != intent.quantity
                    ):
                        differences.append(f"INTENT_FILL_STATE_MISMATCH:{intent.intent_id}")

                position = session.scalar(
                    select(Position)
                    .where(
                        Position.account_id == campaign.account_id,
                        Position.venue == campaign.venue,
                        Position.environment == campaign.environment,
                        Position.instrument_id == campaign.instrument_id,
                    )
                    .with_for_update()
                )
                if position is None or position.fact_status != FactStatus.KNOWN.value:
                    unknown.append(f"POSITION_UNKNOWN:{scope_suffix}")
                    continue
                if self._fact_is_stale(position.observed_at, now, max_age):
                    unknown.append(f"POSITION_STALE:{scope_suffix}")
                signed_fills = sum(
                    (fill.quantity if fill.side == "BUY" else -fill.quantity for fill in fills),
                    Decimal(0),
                )
                if signed_fills != position.quantity:
                    differences.append(f"POSITION_QUANTITY_MISMATCH:{scope_suffix}")
                if position.quantity != 0:
                    protection = session.scalar(
                        select(ProtectionOrder)
                        .where(ProtectionOrder.position_id == position.position_id)
                        .with_for_update()
                    )
                    if protection is None or protection.status == ProtectionStatus.UNKNOWN.value:
                        unknown.append(f"PROTECTION_UNKNOWN:{scope_suffix}")
                    elif self._fact_is_stale(protection.observed_at, now, max_age):
                        unknown.append(f"PROTECTION_STALE:{scope_suffix}")
                    elif (
                        protection.status != ProtectionStatus.ACTIVE.value
                        or not protection.fully_covered
                        or protection.quantity < abs(position.quantity)
                    ):
                        differences.append(f"PROTECTION_INSUFFICIENT:{scope_suffix}")

            if unknown:
                status = ReconciliationStatus.UNKNOWN
                result_differences = sorted(set(unknown + differences))
            elif differences:
                status = ReconciliationStatus.DIFFERENCE
                result_differences = sorted(set(differences))
            else:
                status = ReconciliationStatus.MATCH
                result_differences = []
            run = ReconciliationRun(
                execution_scope=execution_scope,
                campaign_id=None,
                status=status.value,
                is_computed=True,
                differences=result_differences,
                resolution_reason=None,
                actor_id=str(actor_id),
                correlation_id=uuid4(),
                started_at=now,
                completed_at=now,
            )
            session.add(run)
            session.flush()
            RECONCILIATION_RESULTS.labels(status.value).inc()
            return run.reconciliation_id

    def reconcile_campaign(
        self,
        campaign_id: UUID,
        execution_scope: str,
        actor_id: UUID,
        *,
        now: datetime,
    ) -> UUID:
        environment, account_id, venue = _scope_parts(execution_scope)
        with self.database.session_factory() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                _reject("CAMPAIGN_NOT_FOUND", "campaign does not exist")
            if (
                campaign.account_id != account_id
                or campaign.venue != venue
                or campaign.environment != environment.value
            ):
                _reject("EXECUTION_SCOPE_MISMATCH", "campaign is outside reconciliation scope")
        return self.reconcile_scope(execution_scope, actor_id, now=now)

    def close_campaign(self, campaign_id: UUID, actor_id: UUID, *, now: datetime) -> None:
        with self.database.session_factory.begin() as session:
            campaign = session.get(Campaign, campaign_id, with_for_update=True)
            if campaign is None:
                _reject("CAMPAIGN_NOT_FOUND", "campaign does not exist")
            self._require_role(
                session, actor_id, "order.prepare", campaign.account_id, campaign.venue
            )
            if campaign.status == CampaignStatus.CLOSED.value:
                return
            policy = session.scalar(select(RiskPolicy).where(RiskPolicy.active))
            position = session.scalar(
                select(Position)
                .where(
                    Position.account_id == campaign.account_id,
                    Position.venue == campaign.venue,
                    Position.environment == campaign.environment,
                    Position.instrument_id == campaign.instrument_id,
                )
                .with_for_update()
            )
            if (
                policy is None
                or position is None
                or position.fact_status != FactStatus.KNOWN.value
                or position.quantity != 0
                or self._fact_is_stale(
                    position.observed_at,
                    now,
                    timedelta(seconds=policy.max_fact_age_seconds),
                )
            ):
                _reject("CAMPAIGN_POSITION_NOT_CLOSED", "campaign requires a fresh flat position")
            exit_intent = session.scalar(
                select(OrderIntent)
                .where(
                    OrderIntent.campaign_id == campaign_id,
                    OrderIntent.kind == IntentKind.EXIT.value,
                )
                .order_by(OrderIntent.created_at.desc())
                .limit(1)
                .with_for_update()
            )
            terminal_statuses = {
                OrderIntentStatus.FILLED.value,
                OrderIntentStatus.CANCELLED.value,
                OrderIntentStatus.REJECTED.value,
            }
            if exit_intent is None or exit_intent.status not in terminal_statuses:
                _reject("CAMPAIGN_EXIT_NOT_TERMINAL", "campaign exit is not terminal")
            scope = _scope_key(campaign.environment, campaign.account_id, campaign.venue)
            latest = session.scalar(
                select(ReconciliationRun)
                .where(ReconciliationRun.execution_scope == scope)
                .order_by(ReconciliationRun.completed_at.desc())
                .limit(1)
            )
            if (
                latest is None
                or latest.status != ReconciliationStatus.MATCH.value
                or not latest.is_computed
                or latest.completed_at < position.observed_at
                or latest.completed_at < exit_intent.updated_at
            ):
                _reject("RECONCILIATION_REQUIRED", "campaign closure requires a current MATCH")
            reservations = session.scalars(
                select(RiskReservation)
                .where(RiskReservation.campaign_id == campaign_id)
                .with_for_update()
            ).all()
            if any(
                reservation.status
                in {ReservationStatus.UNKNOWN.value, ReservationStatus.RESERVED.value}
                for reservation in reservations
            ):
                _reject("RISK_RESERVATION_UNRESOLVED", "campaign risk is not confirmed closed")
            for reservation in reservations:
                if reservation.status == ReservationStatus.OPEN.value:
                    reservation.status = ReservationStatus.RELEASED.value
                    reservation.version += 1
                    reservation.updated_at = now
            authorization = session.get(
                TradingAuthorization, campaign.authorization_id, with_for_update=True
            )
            if authorization is not None:
                authorization.active = False
            campaign.status = CampaignStatus.CLOSED.value
            campaign.current_target_quantity = Decimal(0)
            campaign.updated_at = now
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="CAMPAIGN_CLOSED",
                object_type="Campaign",
                object_id=campaign.campaign_id,
                reason="flat position, terminal exit, and computed reconciliation MATCH",
                correlation_id=uuid4(),
                object_version=campaign.target_version,
                now=now,
            )

    def refresh_campaign_pnl(
        self, campaign_id: UUID, actor_id: UUID, *, now: datetime
    ) -> PnlBreakdown:
        with self.database.session_factory.begin() as session:
            campaign = session.get(Campaign, campaign_id, with_for_update=True)
            if campaign is None:
                _reject("CAMPAIGN_NOT_FOUND", "campaign does not exist")
            self._require_role(session, actor_id, "view", campaign.account_id, campaign.venue)
            fills = session.scalars(
                select(VenueFill)
                .where(VenueFill.campaign_id == campaign_id)
                .order_by(VenueFill.executed_at, VenueFill.venue_fill_fact_id)
            ).all()
            position = session.scalar(
                select(Position).where(
                    Position.account_id == campaign.account_id,
                    Position.venue == campaign.venue,
                    Position.environment == campaign.environment,
                    Position.instrument_id == campaign.instrument_id,
                )
            )
            if position is None or position.fact_status != FactStatus.KNOWN.value:
                _reject("POSITION_UNKNOWN", "PnL requires known current position")
            instrument = session.get(Instrument, campaign.instrument_id)
            if instrument is None:
                _reject("INSTRUMENT_UNAVAILABLE", "campaign instrument is missing")
            payments = session.scalars(
                select(FundingPayment).where(FundingPayment.campaign_id == campaign_id)
            ).all()
            if any(fill.fee_currency != instrument.collateral_currency for fill in fills) or any(
                payment.currency != instrument.collateral_currency for payment in payments
            ):
                _reject("PNL_CURRENCY_MISMATCH", "PnL requires an explicit FX conversion")
            funding = sum((payment.amount for payment in payments), Decimal(0))
            result = compute_pnl(
                fills=tuple(
                    EconomicFill(
                        fill.side,
                        fill.quantity,
                        fill.price,
                        fill.fee,
                        fill.slippage_cost,
                    )
                    for fill in fills
                ),
                mark_price=position.mark_price,
                funding=funding,
            )
            campaign.realized_pnl = result.realized_pnl
            campaign.unrealized_pnl = result.unrealized_pnl
            campaign.final_pnl = result.total_pnl
            campaign.updated_at = now
            return result

    def record_capital_balance(
        self,
        *,
        actor_id: UUID,
        environment: ExecutionEnvironment,
        location_type: str,
        location_id: str,
        venue: str,
        equity: Decimal,
        available_balance: Decimal,
        withdrawable_balance: Decimal,
        asset: str,
        control_status: str,
        deposit_status: str,
        network: str | None,
        address_reference: str | None,
        known: bool,
        observed_at: datetime,
        now: datetime,
        valuation_currency: str | None = None,
        valuation_price: Decimal | None = None,
        valuation_equity: Decimal | None = None,
        valuation_observed_at: datetime | None = None,
    ) -> UUID:
        if location_type not in {"VAULT", "VENUE"}:
            _reject("CAPITAL_LOCATION_INVALID", "capital location must be VAULT or VENUE")
        if environment is ExecutionEnvironment.LIVE:
            _reject(
                "CAPITAL_LIVE_FACT_DISABLED",
                "LIVE capital facts require a configured read-only adapter",
            )
        if observed_at > now:
            _reject("FACT_TIME_INVALID", "capital observation cannot be in the future")
        if withdrawable_balance > available_balance or available_balance > equity:
            _reject(
                "CAPITAL_BALANCE_INVALID",
                "withdrawable, available, and equity balances are inconsistent",
            )
        if valuation_price is not None and valuation_price <= 0:
            _reject("CAPITAL_VALUATION_INVALID", "capital valuation price must be positive")
        if valuation_equity is not None and valuation_equity < 0:
            _reject("CAPITAL_VALUATION_INVALID", "capital valuation cannot be negative")
        if valuation_observed_at is not None and valuation_observed_at > now + MAX_FACT_CLOCK_SKEW:
            _reject("FACT_TIME_INVALID", "capital valuation cannot be in the future")
        if asset.upper() in USD_STABLE_ASSETS and valuation_equity is None:
            valuation_currency = "USD"
            valuation_price = Decimal(1)
            valuation_equity = equity
            valuation_observed_at = observed_at
        fact_venue = venue if location_type == "VENUE" else "VAULT"
        with self.database.session_factory.begin() as session:
            self._require_role(
                session,
                actor_id,
                "capital.fact.record",
                location_id if location_type == "VENUE" else None,
                venue if location_type == "VENUE" else None,
            )
            fact = session.scalar(
                select(AccountEquity)
                .where(
                    AccountEquity.environment == environment.value,
                    AccountEquity.account_id == location_id,
                    AccountEquity.venue == fact_venue,
                    AccountEquity.currency == asset,
                )
                .with_for_update()
            )
            if fact is None:
                fact = AccountEquity(
                    account_id=location_id,
                    venue=fact_venue,
                    environment=environment.value,
                    equity=equity,
                    available_balance=available_balance,
                    withdrawable_balance=withdrawable_balance,
                    currency=asset,
                    location_type=location_type,
                    control_status=control_status,
                    deposit_status=deposit_status,
                    network=network,
                    address_reference=address_reference,
                    valuation_currency=valuation_currency,
                    valuation_price=valuation_price,
                    valuation_equity=valuation_equity,
                    valuation_observed_at=valuation_observed_at,
                    fact_status=FactStatus.KNOWN.value if known else FactStatus.UNKNOWN.value,
                    observed_at=observed_at,
                    updated_at=now,
                )
                session.add(fact)
                session.flush()
            else:
                fact.equity = equity
                fact.available_balance = available_balance
                fact.withdrawable_balance = withdrawable_balance
                fact.currency = asset
                fact.location_type = location_type
                fact.control_status = control_status
                fact.deposit_status = deposit_status
                fact.network = network
                fact.address_reference = address_reference
                fact.valuation_currency = valuation_currency
                fact.valuation_price = valuation_price
                fact.valuation_equity = valuation_equity
                fact.valuation_observed_at = valuation_observed_at
                fact.fact_status = FactStatus.KNOWN.value if known else FactStatus.UNKNOWN.value
                fact.observed_at = observed_at
                fact.updated_at = now
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="CAPITAL_BALANCE_RECORDED",
                object_type="AccountEquity",
                object_id=fact.account_equity_id,
                reason=f"{location_type}:{fact.fact_status}",
                correlation_id=uuid4(),
                object_version=1,
                now=now,
            )
            return fact.account_equity_id

    def record_notilt_vault_snapshot(
        self,
        *,
        actor_id: UUID,
        snapshot: NoTiltVaultSnapshot,
        valuations: dict[str, UsdValuation],
        now: datetime,
    ) -> tuple[UUID, ...]:
        if not snapshot.budgets:
            _reject("NOTILT_FACT_INVALID", "NoTilt snapshot must contain catalog assets")
        with self.database.session_factory.begin() as session:
            self._require_role(session, actor_id, "capital.fact.record")
            fact_ids: list[UUID] = []
            for budget in snapshot.budgets:
                if (
                    not budget.is_official_vault
                    or budget.chain_id != snapshot.chain_id
                    or budget.vault.lower() != snapshot.vault.lower()
                    or budget.agent.lower() != snapshot.agent.lower()
                ):
                    _reject(
                        "NOTILT_VAULT_UNVERIFIED",
                        "NoTilt facts must belong to one official configured Vault",
                    )
                if budget.block_timestamp > now + MAX_FACT_CLOCK_SKEW:
                    _reject("FACT_TIME_INVALID", "NoTilt block time cannot be in the future")
                valuation = valuations.get(budget.asset)
                if valuation is None or valuation.observed_at > now + MAX_FACT_CLOCK_SKEW:
                    _reject(
                        "NOTILT_VALUATION_UNKNOWN",
                        "every NoTilt asset requires a current USD valuation",
                    )
                assigned = (
                    budget.is_active_whitelist
                    and budget.assigned_whitelist_vault.lower() == snapshot.vault.lower()
                )
                controlled = assigned and not budget.panic_locked
                withdrawable = (
                    min(budget.balance, budget.max_release_net) if controlled else Decimal(0)
                )
                fact = session.scalar(
                    select(AccountEquity)
                    .where(
                        AccountEquity.environment == ExecutionEnvironment.LIVE.value,
                        AccountEquity.account_id == snapshot.vault,
                        AccountEquity.venue == "VAULT",
                        AccountEquity.currency == budget.asset,
                    )
                    .with_for_update()
                )
                if fact is None:
                    fact = AccountEquity(
                        account_id=snapshot.vault,
                        venue="VAULT",
                        environment=ExecutionEnvironment.LIVE.value,
                        equity=budget.balance,
                        available_balance=budget.balance,
                        withdrawable_balance=withdrawable,
                        currency=budget.asset,
                        location_type="VAULT",
                        control_status="CONTROLLED" if controlled else "READ_ONLY",
                        deposit_status="READY",
                        network=snapshot.chain,
                        address_reference=snapshot.vault,
                        valuation_currency="USD",
                        valuation_price=valuation.price,
                        valuation_equity=valuation.value,
                        valuation_observed_at=valuation.observed_at,
                        fact_status=FactStatus.KNOWN.value,
                        observed_at=budget.block_timestamp,
                        updated_at=now,
                    )
                    session.add(fact)
                    session.flush()
                else:
                    fact.equity = budget.balance
                    fact.available_balance = budget.balance
                    fact.withdrawable_balance = withdrawable
                    fact.location_type = "VAULT"
                    fact.control_status = "CONTROLLED" if controlled else "READ_ONLY"
                    fact.deposit_status = "READY"
                    fact.network = snapshot.chain
                    fact.address_reference = snapshot.vault
                    fact.valuation_currency = "USD"
                    fact.valuation_price = valuation.price
                    fact.valuation_equity = valuation.value
                    fact.valuation_observed_at = valuation.observed_at
                    fact.fact_status = FactStatus.KNOWN.value
                    fact.observed_at = budget.block_timestamp
                    fact.updated_at = now
                self._audit(
                    session,
                    actor_id=str(actor_id),
                    event_type="NOTILT_VAULT_FACT_RECORDED",
                    object_type="AccountEquity",
                    object_id=fact.account_equity_id,
                    reason=(
                        f"{snapshot.chain}:{budget.asset}:"
                        f"{'CONTROLLED' if controlled else 'READ_ONLY'}"
                    ),
                    correlation_id=uuid4(),
                    object_version=1,
                    now=now,
                )
                fact_ids.append(fact.account_equity_id)
            return tuple(fact_ids)

    def set_capital_automation_policy(
        self,
        *,
        actor_id: UUID,
        environment: ExecutionEnvironment,
        account_id: str,
        venue: str,
        vault_id: str,
        asset: str,
        network: str,
        vault_destination_reference: str,
        venue_destination_reference: str,
        operating_low: Decimal,
        operating_target: Decimal,
        operating_high: Decimal,
        vault_minimum_reserve: Decimal,
        minimum_transfer: Decimal,
        maximum_transfer: Decimal,
        max_fee: Decimal,
        idempotency_key: str,
        now: datetime,
    ) -> UUID:
        if environment is ExecutionEnvironment.LIVE:
            _reject(
                "CAPITAL_AUTOMATION_LIVE_DISABLED",
                "LIVE automation requires approved external capital parameters",
            )
        evaluate_capital_automation(
            purpose="AUTO_PROFIT_SWEEP",
            venue_available=Decimal(0),
            venue_withdrawable=Decimal(0),
            vault_available=Decimal(0),
            confirmed_realized_pnl=Decimal(0),
            operating_low=operating_low,
            operating_target=operating_target,
            operating_high=operating_high,
            vault_minimum_reserve=vault_minimum_reserve,
            minimum_transfer=minimum_transfer,
            maximum_transfer=maximum_transfer,
            max_fee=max_fee,
        )
        payload = {
            "environment": environment.value,
            "account_id": account_id,
            "venue": venue,
            "vault_id": vault_id,
            "asset": asset,
            "network": network,
            "vault_destination_reference": vault_destination_reference,
            "venue_destination_reference": venue_destination_reference,
            "operating_low": str(operating_low),
            "operating_target": str(operating_target),
            "operating_high": str(operating_high),
            "vault_minimum_reserve": str(vault_minimum_reserve),
            "minimum_transfer": str(minimum_transfer),
            "maximum_transfer": str(maximum_transfer),
            "max_fee": str(max_fee),
        }
        operation = "capital.policy.manage"
        with self.database.session_factory.begin() as session:
            self._require_role(session, actor_id, operation, account_id, venue)
            digest, response = self._idempotency(
                session,
                caller_id=str(actor_id),
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if response is not None:
                return _as_uuid(str(response["policy_id"]))
            policy = session.scalar(
                select(CapitalAutomationPolicy)
                .where(
                    CapitalAutomationPolicy.environment == environment.value,
                    CapitalAutomationPolicy.account_id == account_id,
                    CapitalAutomationPolicy.venue == venue,
                    CapitalAutomationPolicy.asset == asset,
                )
                .with_for_update()
            )
            if policy is None:
                policy = CapitalAutomationPolicy(
                    environment=environment.value,
                    account_id=account_id,
                    venue=venue,
                    vault_id=vault_id,
                    asset=asset,
                    network=network,
                    vault_destination_reference=vault_destination_reference,
                    venue_destination_reference=venue_destination_reference,
                    operating_low=operating_low,
                    operating_target=operating_target,
                    operating_high=operating_high,
                    vault_minimum_reserve=vault_minimum_reserve,
                    minimum_transfer=minimum_transfer,
                    maximum_transfer=maximum_transfer,
                    max_fee=max_fee,
                    active=True,
                    actor_id=str(actor_id),
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
                session.add(policy)
                session.flush()
            else:
                policy.vault_id = vault_id
                policy.network = network
                policy.vault_destination_reference = vault_destination_reference
                policy.venue_destination_reference = venue_destination_reference
                policy.operating_low = operating_low
                policy.operating_target = operating_target
                policy.operating_high = operating_high
                policy.vault_minimum_reserve = vault_minimum_reserve
                policy.minimum_transfer = minimum_transfer
                policy.maximum_transfer = maximum_transfer
                policy.max_fee = max_fee
                policy.active = True
                policy.actor_id = str(actor_id)
                policy.version += 1
                policy.updated_at = now
            result = {"policy_id": str(policy.policy_id)}
            self._save_receipt(
                session,
                caller_id=str(actor_id),
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="CAPITAL_AUTOMATION_POLICY_SET",
                object_type="CapitalAutomationPolicy",
                object_id=policy.policy_id,
                reason="SHADOW/TESTNET thresholds frozen; both automation gates remain independent",
                correlation_id=uuid4(),
                object_version=policy.version,
                idempotency_key=idempotency_key,
                now=now,
            )
            return policy.policy_id

    def create_capital_automation_candidate(
        self,
        policy_id: UUID,
        purpose: str,
        actor_id: UUID,
        idempotency_key: str,
        *,
        now: datetime,
    ) -> tuple[UUID | None, str]:
        if purpose not in {"AUTO_PROFIT_SWEEP", "AUTO_OPERATING_REFILL"}:
            _reject("CAPITAL_AUTOMATION_PURPOSE_INVALID", "unknown capital automation")
        operation = "capital.automation.evaluate"
        payload = {"policy_id": str(policy_id), "purpose": purpose}
        with self.database.session_factory.begin() as session:
            policy = session.get(CapitalAutomationPolicy, policy_id, with_for_update=True)
            if policy is None:
                _reject("CAPITAL_AUTOMATION_POLICY_NOT_FOUND", "capital policy is missing")
            self._require_role(session, actor_id, operation, policy.account_id, policy.venue)
            digest, response = self._idempotency(
                session,
                caller_id=str(actor_id),
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if response is not None:
                proposal_id = response.get("transfer_proposal_id")
                return (
                    None if proposal_id is None else _as_uuid(str(proposal_id)),
                    str(response["reason"]),
                )
            gate = session.get(CapabilityGate, purpose)
            if gate is None or gate.status != CapabilityStatus.ENABLED.value:
                _reject("CAPITAL_AUTOMATION_DISABLED", f"{purpose} is disabled")
            if not policy.active:
                _reject("CAPITAL_AUTOMATION_POLICY_INACTIVE", "capital policy is inactive")
            session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {
                    "key": _advisory_lock_key(
                        policy.environment,
                        "capital-automation",
                        f"{policy.account_id}:{policy.venue}:{policy.asset}",
                    )
                },
            )
            self._assert_capital_scope_flat(
                session,
                environment=policy.environment,
                account_id=policy.account_id,
                venue=policy.venue,
                now=now,
            )
            risk_policy = session.scalar(select(RiskPolicy).where(RiskPolicy.active))
            if risk_policy is None:
                _reject("RISK_POLICY_MISSING", "capital automation requires an active risk policy")
            latest_match = session.scalar(
                select(ReconciliationRun)
                .where(
                    ReconciliationRun.execution_scope
                    == _scope_key(policy.environment, policy.account_id, policy.venue)
                )
                .order_by(ReconciliationRun.completed_at.desc())
                .limit(1)
            )
            if (
                latest_match is None
                or latest_match.status != ReconciliationStatus.MATCH.value
                or not latest_match.is_computed
                or latest_match.completed_at
                < now - timedelta(seconds=risk_policy.max_fact_age_seconds)
            ):
                _reject(
                    "CAPITAL_AUTOMATION_RECONCILIATION_REQUIRED",
                    "fresh computed MATCH is required",
                )
            campaigns = session.scalars(
                select(Campaign).where(
                    Campaign.environment == policy.environment,
                    Campaign.account_id == policy.account_id,
                    Campaign.venue == policy.venue,
                )
            ).all()
            if any(item.status != CampaignStatus.CLOSED.value for item in campaigns):
                _reject(
                    "CAPITAL_AUTOMATION_ACTIVE_CYCLE",
                    "automation only prepares the next flat trading cycle",
                )
            active_transfer = session.scalar(
                select(CapitalTransfer.capital_transfer_id)
                .where(
                    CapitalTransfer.environment == policy.environment,
                    CapitalTransfer.account_id == policy.account_id,
                    CapitalTransfer.venue == policy.venue,
                    CapitalTransfer.status.in_(OCCUPIED_CAPITAL_STATUSES),
                )
                .limit(1)
            )
            active_proposal = session.scalar(
                select(TransferProposal.transfer_proposal_id)
                .where(
                    TransferProposal.environment == policy.environment,
                    TransferProposal.account_id == policy.account_id,
                    TransferProposal.venue == policy.venue,
                    TransferProposal.purpose.in_({"AUTO_PROFIT_SWEEP", "AUTO_OPERATING_REFILL"}),
                    TransferProposal.status.in_(
                        {
                            ProposalStatus.DRAFT.value,
                            ProposalStatus.PENDING_REVIEW.value,
                            ProposalStatus.APPROVED.value,
                        }
                    ),
                )
                .limit(1)
            )
            if active_transfer is not None or active_proposal is not None:
                _reject(
                    "CAPITAL_AUTOMATION_ALREADY_PENDING",
                    "another capital operation owns this scope",
                )
            venue_fact = self._capital_balance(
                session,
                environment=policy.environment,
                endpoint_type="VENUE",
                endpoint_id=policy.account_id,
                venue=policy.venue,
                asset=policy.asset,
                lock=True,
            )
            vault_fact = self._capital_balance(
                session,
                environment=policy.environment,
                endpoint_type="VAULT",
                endpoint_id=policy.vault_id,
                venue=policy.venue,
                asset=policy.asset,
                lock=True,
            )
            if (
                venue_fact.observed_at < now - timedelta(seconds=risk_policy.max_fact_age_seconds)
                or vault_fact.observed_at
                < now - timedelta(seconds=risk_policy.max_fact_age_seconds)
                or venue_fact.deposit_status != "READY"
                or vault_fact.control_status != "CONTROLLED"
            ):
                _reject("CAPITAL_FACT_UNKNOWN", "fresh controlled capital facts are required")
            realized_pnl = sum((item.final_pnl for item in campaigns), Decimal(0))
            already_swept = session.scalar(
                select(func.coalesce(func.sum(CapitalTransfer.gross_amount), 0))
                .join(
                    TransferAuthorization,
                    TransferAuthorization.transfer_authorization_id
                    == CapitalTransfer.transfer_authorization_id,
                )
                .where(
                    CapitalTransfer.environment == policy.environment,
                    CapitalTransfer.account_id == policy.account_id,
                    CapitalTransfer.venue == policy.venue,
                    TransferAuthorization.purpose == "AUTO_PROFIT_SWEEP",
                    CapitalTransfer.status != CapitalTransferStatus.FAILED_SOURCE_RESTORED.value,
                )
            )
            confirmed_profit = max(Decimal(0), realized_pnl - Decimal(already_swept or 0))
            decision = evaluate_capital_automation(
                purpose=purpose,
                venue_available=venue_fact.available_balance,
                venue_withdrawable=(
                    venue_fact.available_balance
                    if venue_fact.withdrawable_balance is None
                    else venue_fact.withdrawable_balance
                ),
                vault_available=(
                    vault_fact.available_balance
                    if vault_fact.withdrawable_balance is None
                    else vault_fact.withdrawable_balance
                ),
                confirmed_realized_pnl=(
                    realized_pnl if purpose == "AUTO_OPERATING_REFILL" else confirmed_profit
                ),
                operating_low=policy.operating_low,
                operating_target=policy.operating_target,
                operating_high=policy.operating_high,
                vault_minimum_reserve=policy.vault_minimum_reserve,
                minimum_transfer=policy.minimum_transfer,
                maximum_transfer=policy.maximum_transfer,
                max_fee=policy.max_fee,
            )
            if decision.amount is None:
                result: dict[str, Any] = {
                    "transfer_proposal_id": None,
                    "reason": decision.reason,
                }
                event_type = "CAPITAL_AUTOMATION_NO_ACTION"
                object_id: UUID | str = policy.policy_id
                object_version = policy.version
            else:
                direction = (
                    CapitalDirection.VENUE_TO_VAULT
                    if purpose == "AUTO_PROFIT_SWEEP"
                    else CapitalDirection.VAULT_TO_VENUE
                )
                source_type, source_id, destination_type, destination_id = (
                    ("VENUE", policy.account_id, "VAULT", policy.vault_id)
                    if direction is CapitalDirection.VENUE_TO_VAULT
                    else ("VAULT", policy.vault_id, "VENUE", policy.account_id)
                )
                destination_reference = (
                    policy.vault_destination_reference
                    if direction is CapitalDirection.VENUE_TO_VAULT
                    else policy.venue_destination_reference
                )
                frozen_payload = {
                    **payload,
                    "policy_version": policy.version,
                    "environment": policy.environment,
                    "direction": direction.value,
                    "account_id": policy.account_id,
                    "venue": policy.venue,
                    "vault_id": policy.vault_id,
                    "asset": policy.asset,
                    "network": policy.network,
                    "amount": str(decision.amount),
                    "max_fee": str(policy.max_fee),
                    "min_received": str(decision.amount - policy.max_fee),
                    "confirmed_realized_pnl": str(realized_pnl),
                    "remaining_sweepable_profit": str(confirmed_profit),
                    "venue_fact_id": str(venue_fact.account_equity_id),
                    "venue_observed_at": venue_fact.observed_at.isoformat(),
                    "vault_fact_id": str(vault_fact.account_equity_id),
                    "vault_observed_at": vault_fact.observed_at.isoformat(),
                    "reconciliation_id": str(latest_match.reconciliation_id),
                }
                proposal = TransferProposal(
                    proposer_id=actor_id,
                    environment=policy.environment,
                    direction=direction.value,
                    purpose=purpose,
                    status=ProposalStatus.PENDING_REVIEW.value,
                    version=1,
                    account_id=policy.account_id,
                    venue=policy.venue,
                    source_type=source_type,
                    source_id=source_id,
                    destination_type=destination_type,
                    destination_id=destination_id,
                    asset=policy.asset,
                    network=policy.network,
                    destination_reference=destination_reference,
                    amount=decision.amount,
                    max_fee=policy.max_fee,
                    min_received=decision.amount - policy.max_fee,
                    reason=(
                        "confirmed realized profit above operating high"
                        if purpose == "AUTO_PROFIT_SWEEP"
                        else "flat next-cycle operating balance below low"
                    ),
                    frozen_payload=frozen_payload,
                    semantic_hash=_semantic_hash(frozen_payload),
                    frozen_at=now,
                    expires_at=now + timedelta(hours=2),
                    correlation_id=uuid4(),
                    created_at=now,
                    updated_at=now,
                )
                session.add(proposal)
                session.flush()
                result = {
                    "transfer_proposal_id": str(proposal.transfer_proposal_id),
                    "reason": decision.reason,
                }
                event_type = "CAPITAL_AUTOMATION_CANDIDATE_CREATED"
                object_id = proposal.transfer_proposal_id
                object_version = proposal.version
            self._save_receipt(
                session,
                caller_id=str(actor_id),
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type=event_type,
                object_type=(
                    "TransferProposal"
                    if result["transfer_proposal_id"] is not None
                    else "CapitalAutomationPolicy"
                ),
                object_id=object_id,
                reason=f"{purpose}:{decision.reason}; no automatic transfer submission",
                correlation_id=uuid4(),
                object_version=object_version,
                idempotency_key=idempotency_key,
                now=now,
            )
            return (
                None
                if result["transfer_proposal_id"] is None
                else _as_uuid(str(result["transfer_proposal_id"])),
                decision.reason,
            )

    def create_transfer_proposal(
        self,
        *,
        actor_id: UUID,
        environment: ExecutionEnvironment,
        direction: CapitalDirection,
        account_id: str,
        venue: str,
        vault_id: str,
        asset: str,
        network: str,
        destination_reference: str,
        amount: Decimal,
        max_fee: Decimal,
        min_received: Decimal,
        reason: str,
        expires_at: datetime,
        idempotency_key: str,
        now: datetime,
        allow_live_unsigned: bool = False,
    ) -> UUID:
        if environment is ExecutionEnvironment.LIVE and not allow_live_unsigned:
            _reject(
                "CAPITAL_TRANSFER_LIVE_DISABLED",
                "LIVE capital proposals require the constrained unsigned transaction workflow",
            )
        if expires_at <= now:
            _reject("TRANSFER_PROPOSAL_EXPIRY_INVALID", "transfer proposal must expire later")
        source_type, source_id, destination_type, destination_id = (
            ("VAULT", vault_id, "VENUE", account_id)
            if direction is CapitalDirection.VAULT_TO_VENUE
            else ("VENUE", account_id, "VAULT", vault_id)
        )
        payload = {
            "environment": environment.value,
            "direction": direction.value,
            "purpose": "MANUAL_TRANSFER",
            "account_id": account_id,
            "venue": venue,
            "source_type": source_type,
            "source_id": source_id,
            "destination_type": destination_type,
            "destination_id": destination_id,
            "asset": asset,
            "network": network,
            "destination_reference": destination_reference,
            "amount": str(amount),
            "max_fee": str(max_fee),
            "min_received": str(min_received),
            "reason": reason,
            "expires_at": expires_at.isoformat(),
        }
        operation = "capital.propose"
        with self.database.session_factory.begin() as session:
            self._require_role(session, actor_id, operation, account_id, venue)
            digest, response = self._idempotency(
                session,
                caller_id=str(actor_id),
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if response is not None:
                return _as_uuid(str(response["transfer_proposal_id"]))
            proposal = TransferProposal(
                proposer_id=actor_id,
                environment=environment.value,
                direction=direction.value,
                purpose="MANUAL_TRANSFER",
                status=ProposalStatus.DRAFT.value,
                version=1,
                account_id=account_id,
                venue=venue,
                source_type=source_type,
                source_id=source_id,
                destination_type=destination_type,
                destination_id=destination_id,
                asset=asset,
                network=network,
                destination_reference=destination_reference,
                amount=amount,
                max_fee=max_fee,
                min_received=min_received,
                reason=reason,
                frozen_payload=payload,
                semantic_hash=digest,
                frozen_at=None,
                expires_at=expires_at,
                correlation_id=uuid4(),
                created_at=now,
                updated_at=now,
            )
            session.add(proposal)
            session.flush()
            result = {"transfer_proposal_id": str(proposal.transfer_proposal_id)}
            self._save_receipt(
                session,
                caller_id=str(actor_id),
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="TRANSFER_PROPOSAL_CREATED",
                object_type="TransferProposal",
                object_id=proposal.transfer_proposal_id,
                reason=direction.value,
                correlation_id=proposal.correlation_id,
                object_version=proposal.version,
                idempotency_key=idempotency_key,
                now=now,
            )
            return proposal.transfer_proposal_id

    def submit_transfer_proposal(
        self, transfer_proposal_id: UUID, actor_id: UUID, *, now: datetime
    ) -> None:
        with self.database.session_factory.begin() as session:
            proposal = session.get(TransferProposal, transfer_proposal_id, with_for_update=True)
            if proposal is None:
                _reject("TRANSFER_PROPOSAL_NOT_FOUND", "transfer proposal does not exist")
            self._require_role(
                session, actor_id, "capital.submit", proposal.account_id, proposal.venue
            )
            if proposal.proposer_id != actor_id:
                _reject("TRANSFER_PROPOSAL_OWNER_REQUIRED", "only the proposer may submit")
            if proposal.expires_at <= now:
                proposal.status = ProposalStatus.EXPIRED.value
                proposal.version += 1
                proposal.updated_at = now
                _reject("TRANSFER_PROPOSAL_EXPIRED", "transfer proposal expired")
            if proposal.status != ProposalStatus.DRAFT.value:
                _reject("TRANSFER_PROPOSAL_NOT_DRAFT", "only a draft may be submitted")
            proposal.status = ProposalStatus.PENDING_REVIEW.value
            proposal.frozen_at = now
            proposal.version += 1
            proposal.updated_at = now
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="TRANSFER_PROPOSAL_SUBMITTED",
                object_type="TransferProposal",
                object_id=proposal.transfer_proposal_id,
                reason="frozen for two independent Treasury reviewers",
                correlation_id=proposal.correlation_id,
                object_version=proposal.version,
                now=now,
            )

    def review_transfer_proposal(
        self,
        transfer_proposal_id: UUID,
        reviewer_id: UUID,
        decision: ReviewDecision,
        reason: str,
        expected_version: int,
        *,
        now: datetime,
    ) -> ProposalStatus:
        with self.database.session_factory.begin() as session:
            proposal = session.get(TransferProposal, transfer_proposal_id, with_for_update=True)
            if proposal is None:
                _reject("TRANSFER_PROPOSAL_NOT_FOUND", "transfer proposal does not exist")
            if proposal.version != expected_version:
                _reject("VERSION_CONFLICT", "transfer proposal changed before review")
            if proposal.proposer_id == reviewer_id:
                _reject("SELF_REVIEW_FORBIDDEN", "a transfer proposer cannot review it")
            self._require_role(
                session, reviewer_id, "capital.review", proposal.account_id, proposal.venue
            )
            reviewer = session.get(User, reviewer_id)
            if reviewer is None or reviewer.principal_type != PrincipalType.HUMAN.value:
                _reject("SERVICE_REVIEW_FORBIDDEN", "capital review requires a human")
            if proposal.expires_at <= now:
                proposal.status = ProposalStatus.EXPIRED.value
                proposal.version += 1
                proposal.updated_at = now
                _reject("TRANSFER_PROPOSAL_EXPIRED", "transfer proposal expired")
            if proposal.status != ProposalStatus.PENDING_REVIEW.value:
                _reject("TRANSFER_PROPOSAL_NOT_REVIEWABLE", "transfer proposal is not pending")
            duplicate = session.scalar(
                select(Approval).where(
                    Approval.transfer_proposal_id == transfer_proposal_id,
                    Approval.reviewer_id == reviewer_id,
                )
            )
            if duplicate is not None:
                _reject("REVIEW_ALREADY_RECORDED", "reviewer already decided this transfer")
            session.add(
                Approval(
                    proposal_id=None,
                    transfer_proposal_id=transfer_proposal_id,
                    reviewer_id=reviewer_id,
                    decision=decision.value,
                    reason=reason,
                    created_at=now,
                )
            )
            session.flush()
            if decision is ReviewDecision.REJECT:
                proposal.status = ProposalStatus.REJECTED.value
            else:
                approvals = session.scalar(
                    select(func.count())
                    .select_from(Approval)
                    .where(
                        Approval.transfer_proposal_id == transfer_proposal_id,
                        Approval.decision == ReviewDecision.APPROVE.value,
                    )
                )
                if int(approvals or 0) >= 2:
                    proposal.status = ProposalStatus.APPROVED.value
            proposal.version += 1
            proposal.updated_at = now
            self._audit(
                session,
                actor_id=str(reviewer_id),
                event_type="TRANSFER_PROPOSAL_REVIEWED",
                object_type="TransferProposal",
                object_id=proposal.transfer_proposal_id,
                reason=f"{decision.value}: {reason}",
                correlation_id=proposal.correlation_id,
                object_version=proposal.version,
                now=now,
            )
            return ProposalStatus(proposal.status)

    def issue_transfer_authorization(
        self,
        transfer_proposal_id: UUID,
        actor_id: UUID,
        expires_at: datetime,
        idempotency_key: str,
        *,
        now: datetime,
    ) -> UUID:
        payload = {
            "transfer_proposal_id": str(transfer_proposal_id),
            "expires_at": expires_at.isoformat(),
        }
        operation = "capital.authorize"
        with self.database.session_factory.begin() as session:
            proposal = session.get(TransferProposal, transfer_proposal_id)
            if proposal is None:
                _reject("TRANSFER_PROPOSAL_NOT_FOUND", "transfer proposal does not exist")
            self._require_role(session, actor_id, operation, proposal.account_id, proposal.venue)
            if proposal.proposer_id == actor_id:
                _reject(
                    "CAPITAL_DUTY_SEPARATION_REQUIRED",
                    "the transfer proposer cannot issue its authorization",
                )
            digest, response = self._idempotency(
                session,
                caller_id=str(actor_id),
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if response is not None:
                return _as_uuid(str(response["transfer_authorization_id"]))
            if proposal.status != ProposalStatus.APPROVED.value:
                _reject("TRANSFER_PROPOSAL_NOT_APPROVED", "two Treasury approvals are required")
            if expires_at <= now or expires_at > proposal.expires_at:
                _reject(
                    "TRANSFER_AUTHORIZATION_EXPIRY_INVALID",
                    "transfer authorization must be short-lived",
                )
            authorization = TransferAuthorization(
                transfer_proposal_id=proposal.transfer_proposal_id,
                environment=proposal.environment,
                direction=proposal.direction,
                purpose=proposal.purpose,
                account_id=proposal.account_id,
                venue=proposal.venue,
                source_type=proposal.source_type,
                source_id=proposal.source_id,
                destination_type=proposal.destination_type,
                destination_id=proposal.destination_id,
                asset=proposal.asset,
                network=proposal.network,
                destination_reference=proposal.destination_reference,
                amount_limit=proposal.amount,
                max_fee=proposal.max_fee,
                min_received=proposal.min_received,
                expires_at=expires_at,
                active=True,
                actor_id=str(actor_id),
                correlation_id=proposal.correlation_id,
                version=1,
                created_at=now,
            )
            session.add(authorization)
            session.flush()
            result = {"transfer_authorization_id": str(authorization.transfer_authorization_id)}
            self._save_receipt(
                session,
                caller_id=str(actor_id),
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="TRANSFER_AUTHORIZATION_ISSUED",
                object_type="TransferAuthorization",
                object_id=authorization.transfer_authorization_id,
                reason="two-reviewer frozen manual transfer",
                correlation_id=proposal.correlation_id,
                object_version=1,
                idempotency_key=idempotency_key,
                now=now,
            )
            return authorization.transfer_authorization_id

    @staticmethod
    def _capital_balance(
        session: Session,
        *,
        environment: str,
        endpoint_type: str,
        endpoint_id: str,
        venue: str,
        asset: str,
        lock: bool = False,
    ) -> AccountEquity:
        statement = select(AccountEquity).where(
            AccountEquity.environment == environment,
            AccountEquity.account_id == endpoint_id,
            AccountEquity.venue == (venue if endpoint_type == "VENUE" else "VAULT"),
            AccountEquity.location_type == endpoint_type,
            AccountEquity.currency == asset,
        )
        if lock:
            statement = statement.with_for_update()
        fact = session.scalar(statement)
        if fact is None or fact.fact_status != FactStatus.KNOWN.value:
            _reject("CAPITAL_FACT_UNKNOWN", "source or destination capital fact is unknown")
        return fact

    def record_capital_scope_reconciliation(
        self,
        *,
        actor_id: UUID,
        environment: ExecutionEnvironment,
        account_id: str,
        venue: str,
        now: datetime,
    ) -> UUID:
        with self.database.session_factory.begin() as session:
            self._require_role(session, actor_id, "capital.reconcile", account_id, venue)
            positions = session.scalars(
                select(Position).where(
                    Position.environment == environment.value,
                    Position.account_id == account_id,
                    Position.venue == venue,
                )
            ).all()
            orders = session.scalars(
                select(VenueOrder).where(
                    VenueOrder.environment == environment.value,
                    VenueOrder.account_id == account_id,
                    VenueOrder.venue == venue,
                )
            ).all()
            campaigns = session.scalars(
                select(Campaign).where(
                    Campaign.environment == environment.value,
                    Campaign.account_id == account_id,
                    Campaign.venue == venue,
                )
            ).all()
            campaign_ids = [item.campaign_id for item in campaigns]
            intents = (
                session.scalars(
                    select(OrderIntent).where(OrderIntent.campaign_id.in_(campaign_ids))
                ).all()
                if campaign_ids
                else []
            )
            unknown = (
                not positions
                or any(item.fact_status != FactStatus.KNOWN.value for item in positions)
                or any(item.status == VenueOrderStatus.UNKNOWN.value for item in orders)
            )
            differences: list[str] = []
            if any(item.quantity != 0 for item in positions):
                differences.append("NONZERO_POSITION")
            if any(item.status in ACTIVE_INTENT_STATUSES for item in intents):
                differences.append("ACTIVE_OR_UNKNOWN_INTENT")
            if any(
                item.status
                not in {
                    VenueOrderStatus.FILLED.value,
                    VenueOrderStatus.CANCELLED.value,
                    VenueOrderStatus.REJECTED.value,
                }
                for item in orders
            ):
                differences.append("ACTIVE_OR_UNKNOWN_VENUE_ORDER")
            status = (
                ReconciliationStatus.UNKNOWN.value
                if unknown
                else (
                    ReconciliationStatus.DIFFERENCE.value
                    if differences
                    else ReconciliationStatus.MATCH.value
                )
            )
            run = ReconciliationRun(
                execution_scope=_scope_key(environment.value, account_id, venue),
                campaign_id=None,
                status=status,
                is_computed=True,
                differences=differences,
                resolution_reason=None,
                actor_id=str(actor_id),
                correlation_id=uuid4(),
                started_at=now,
                completed_at=now,
            )
            session.add(run)
            session.flush()
            return run.reconciliation_id

    @staticmethod
    def _assert_capital_scope_flat(
        session: Session,
        *,
        environment: str,
        account_id: str,
        venue: str,
        now: datetime,
    ) -> None:
        positions = session.scalars(
            select(Position).where(
                Position.environment == environment,
                Position.account_id == account_id,
                Position.venue == venue,
            )
        ).all()
        if not positions:
            _reject("CAPITAL_POSITION_UNKNOWN", "flat position facts are required")
        if any(item.fact_status != FactStatus.KNOWN.value for item in positions):
            _reject("CAPITAL_POSITION_UNKNOWN", "unknown position blocks capital transfer")
        policy = session.scalar(select(RiskPolicy).where(RiskPolicy.active))
        if policy is None or any(
            item.observed_at < now - timedelta(seconds=policy.max_fact_age_seconds)
            for item in positions
        ):
            _reject("CAPITAL_POSITION_UNKNOWN", "fresh flat position facts are required")
        if any(item.quantity != 0 for item in positions):
            _reject(
                "ACTIVE_POSITION_CAPITAL_RESCUE_FORBIDDEN",
                "capital transfer cannot rescue an active position",
            )
        campaigns = session.scalars(
            select(Campaign).where(
                Campaign.environment == environment,
                Campaign.account_id == account_id,
                Campaign.venue == venue,
            )
        ).all()
        campaign_ids = [item.campaign_id for item in campaigns]
        if campaign_ids:
            active_intent = session.scalar(
                select(OrderIntent.intent_id)
                .where(
                    OrderIntent.campaign_id.in_(campaign_ids),
                    OrderIntent.status.in_(ACTIVE_INTENT_STATUSES),
                )
                .limit(1)
            )
            if active_intent is not None:
                _reject(
                    "CAPITAL_ORDER_UNRESOLVED",
                    "active or unknown OrderIntent blocks capital transfer",
                )
        venue_order = session.scalar(
            select(VenueOrder.venue_order_fact_id)
            .where(
                VenueOrder.environment == environment,
                VenueOrder.account_id == account_id,
                VenueOrder.venue == venue,
                VenueOrder.status.not_in(
                    {
                        VenueOrderStatus.FILLED.value,
                        VenueOrderStatus.CANCELLED.value,
                        VenueOrderStatus.REJECTED.value,
                    }
                ),
            )
            .limit(1)
        )
        if venue_order is not None:
            _reject(
                "CAPITAL_ORDER_UNRESOLVED",
                "active or unknown VenueOrder blocks capital transfer",
            )

    def reserve_capital_transfer(
        self,
        transfer_authorization_id: UUID,
        actor_id: UUID,
        idempotency_key: str,
        *,
        now: datetime,
        allow_live_unsigned: bool = False,
    ) -> UUID:
        operation = "capital.execute"
        payload = {"transfer_authorization_id": str(transfer_authorization_id)}
        with self.database.session_factory.begin() as session:
            authorization = session.get(
                TransferAuthorization, transfer_authorization_id, with_for_update=True
            )
            if authorization is None:
                _reject("TRANSFER_AUTHORIZATION_NOT_FOUND", "transfer authorization is missing")
            self._require_role(
                session, actor_id, operation, authorization.account_id, authorization.venue
            )
            proposal = session.get(TransferProposal, authorization.transfer_proposal_id)
            if proposal is None:
                _reject("TRANSFER_PROPOSAL_NOT_FOUND", "authorization proposal is missing")
            if proposal.proposer_id == actor_id:
                _reject(
                    "CAPITAL_DUTY_SEPARATION_REQUIRED",
                    "the transfer proposer cannot execute its transfer",
                )
            digest, response = self._idempotency(
                session,
                caller_id=str(actor_id),
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if response is not None:
                return _as_uuid(str(response["capital_transfer_id"]))
            if not authorization.active or authorization.expires_at <= now:
                _reject("TRANSFER_AUTHORIZATION_INACTIVE", "transfer authorization is inactive")
            if allow_live_unsigned and authorization.environment != ExecutionEnvironment.LIVE.value:
                _reject(
                    "NOTILT_TRANSFER_ENVIRONMENT_INVALID",
                    "NoTilt transaction plans require a LIVE authorization",
                )
            if (
                authorization.environment == ExecutionEnvironment.LIVE.value
                and not allow_live_unsigned
            ):
                _reject(
                    "CAPITAL_TRANSFER_LIVE_DISABLED",
                    "LIVE transfer requires the constrained unsigned transaction workflow",
                )
            if authorization.environment == ExecutionEnvironment.LIVE.value:
                gate = session.get(CapabilityGate, "CAPITAL_TRANSFER")
                if gate is None or gate.status != CapabilityStatus.ENABLED.value:
                    _reject(
                        "CAPABILITY_DISABLED",
                        "CAPITAL_TRANSFER must be explicitly enabled before a LIVE reservation",
                    )
            self._assert_capital_scope_flat(
                session,
                environment=authorization.environment,
                account_id=authorization.account_id,
                venue=authorization.venue,
                now=now,
            )
            if authorization.direction == CapitalDirection.VENUE_TO_VAULT.value:
                latest = session.scalar(
                    select(ReconciliationRun)
                    .where(
                        ReconciliationRun.execution_scope
                        == _scope_key(
                            authorization.environment,
                            authorization.account_id,
                            authorization.venue,
                        )
                    )
                    .order_by(ReconciliationRun.completed_at.desc())
                    .limit(1)
                )
                if (
                    latest is None
                    or latest.status != ReconciliationStatus.MATCH.value
                    or not latest.is_computed
                ):
                    _reject(
                        "CAPITAL_RECONCILIATION_REQUIRED",
                        "venue to Vault transfer requires a computed MATCH",
                    )
            session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {
                    "key": _advisory_lock_key(
                        authorization.environment,
                        "capital-source",
                        f"{authorization.source_type}:{authorization.source_id}:"
                        f"{authorization.asset}",
                    )
                },
            )
            source = self._capital_balance(
                session,
                environment=authorization.environment,
                endpoint_type=authorization.source_type,
                endpoint_id=authorization.source_id,
                venue=authorization.venue,
                asset=authorization.asset,
                lock=True,
            )
            destination = self._capital_balance(
                session,
                environment=authorization.environment,
                endpoint_type=authorization.destination_type,
                endpoint_id=authorization.destination_id,
                venue=authorization.venue,
                asset=authorization.asset,
                lock=True,
            )
            if source.control_status == "UNKNOWN" or destination.deposit_status != "READY":
                _reject("CAPITAL_FACT_UNKNOWN", "control or destination deposit status is unsafe")
            occupied = session.scalar(
                select(func.coalesce(func.sum(CapitalTransfer.reserved_amount), 0)).where(
                    CapitalTransfer.environment == authorization.environment,
                    CapitalTransfer.source_id == authorization.source_id,
                    CapitalTransfer.asset == authorization.asset,
                    CapitalTransfer.status.in_(OCCUPIED_CAPITAL_STATUSES),
                )
            )
            withdrawable = (
                source.available_balance
                if source.withdrawable_balance is None
                else source.withdrawable_balance
            )
            if withdrawable - Decimal(occupied or 0) < authorization.amount_limit:
                _reject("CAPITAL_CAPACITY_EXCEEDED", "source confirmed capital is insufficient")
            transfer = CapitalTransfer(
                transfer_authorization_id=authorization.transfer_authorization_id,
                environment=authorization.environment,
                account_id=authorization.account_id,
                venue=authorization.venue,
                direction=authorization.direction,
                source_id=authorization.source_id,
                destination_id=authorization.destination_id,
                asset=authorization.asset,
                network=authorization.network,
                status=CapitalTransferStatus.SOURCE_RESERVED.value,
                gross_amount=authorization.amount_limit,
                reserved_amount=authorization.amount_limit,
                source_balance_before=source.available_balance,
                destination_balance_before=destination.available_balance,
                fee_amount=None,
                net_received=None,
                external_transfer_id=None,
                transaction_reference=None,
                reconciliation_status="NOT_STARTED",
                reconciliation_details=[],
                actor_id=str(actor_id),
                correlation_id=authorization.correlation_id,
                idempotency_key=idempotency_key,
                version=1,
                observed_at=now,
                reconciled_at=None,
                created_at=now,
                updated_at=now,
            )
            authorization.active = False
            authorization.version += 1
            session.add(transfer)
            session.flush()
            result = {"capital_transfer_id": str(transfer.capital_transfer_id)}
            self._save_receipt(
                session,
                caller_id=str(actor_id),
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="CAPITAL_SOURCE_RESERVED",
                object_type="CapitalTransfer",
                object_id=transfer.capital_transfer_id,
                reason=(
                    "source availability reserved before independent wallet confirmation"
                    if authorization.environment == ExecutionEnvironment.LIVE.value
                    else "source availability reduced before mock submission"
                ),
                correlation_id=transfer.correlation_id,
                object_version=transfer.version,
                idempotency_key=idempotency_key,
                now=now,
            )
            return transfer.capital_transfer_id

    def capital_transfer_command(
        self, capital_transfer_id: UUID, actor_id: UUID, *, now: datetime
    ) -> CapitalTransferCommand:
        with self.database.session_factory() as session:
            transfer = session.get(CapitalTransfer, capital_transfer_id)
            if transfer is None:
                _reject("CAPITAL_TRANSFER_NOT_FOUND", "capital transfer does not exist")
            authorization = session.get(TransferAuthorization, transfer.transfer_authorization_id)
            if authorization is None:
                _reject("TRANSFER_AUTHORIZATION_NOT_FOUND", "transfer authorization is missing")
            self._require_role(
                session, actor_id, "capital.execute", transfer.account_id, transfer.venue
            )
            if transfer.status != CapitalTransferStatus.SOURCE_RESERVED.value:
                _reject("CAPITAL_TRANSFER_ALREADY_SUBMITTED", "capital transfer is not reserved")
            return CapitalTransferCommand(
                capital_transfer_id=transfer.capital_transfer_id,
                environment=ExecutionEnvironment(transfer.environment),
                direction=CapitalDirection(transfer.direction),
                source_id=transfer.source_id,
                destination_id=transfer.destination_id,
                asset=transfer.asset,
                network=transfer.network,
                destination_reference=authorization.destination_reference,
                gross_amount=transfer.gross_amount,
                max_fee=authorization.max_fee,
                min_received=authorization.min_received,
            )

    def notilt_transfer_command(
        self, capital_transfer_id: UUID, actor_id: UUID
    ) -> CapitalTransferCommand:
        with self.database.session_factory() as session:
            transfer = session.get(CapitalTransfer, capital_transfer_id)
            if transfer is None:
                _reject("CAPITAL_TRANSFER_NOT_FOUND", "capital transfer does not exist")
            authorization = session.get(TransferAuthorization, transfer.transfer_authorization_id)
            if authorization is None:
                _reject("TRANSFER_AUTHORIZATION_NOT_FOUND", "transfer authorization is missing")
            self._require_role(
                session, actor_id, "capital.execute", transfer.account_id, transfer.venue
            )
            if (
                transfer.transport != "NOTILT"
                or transfer.environment != ExecutionEnvironment.LIVE.value
            ):
                _reject("NOTILT_TRANSFER_STATE_INVALID", "capital transfer is not a NoTilt flow")
            return CapitalTransferCommand(
                capital_transfer_id=transfer.capital_transfer_id,
                environment=ExecutionEnvironment(transfer.environment),
                direction=CapitalDirection(transfer.direction),
                source_id=transfer.source_id,
                destination_id=transfer.destination_id,
                asset=transfer.asset,
                network=transfer.network,
                destination_reference=authorization.destination_reference,
                gross_amount=transfer.gross_amount,
                max_fee=authorization.max_fee,
                min_received=authorization.min_received,
            )

    def record_notilt_plan(
        self,
        capital_transfer_id: UUID,
        actor_id: UUID,
        *,
        chain_id: int,
        transport_state: str,
        transactions: tuple[NoTiltUnsignedTransaction, ...],
        now: datetime,
    ) -> None:
        expected_functions = {
            "DEPOSIT_PLAN_READY": {"approve", "deposit"},
            "RELEASE_REQUEST_PLAN_READY": {"requestWhitelistRelease"},
            "RELEASE_EXECUTION_PLAN_READY": {"executeWhitelistRelease"},
            "RELEASE_CANCELLATION_PLAN_READY": {"cancelWhitelistRelease"},
        }
        allowed_functions = expected_functions.get(transport_state)
        if allowed_functions is None or not transactions:
            _reject("NOTILT_PLAN_INVALID", "NoTilt transaction plan is invalid")
        function_names = {item.function_name for item in transactions}
        if (
            not function_names.issubset(allowed_functions)
            or transactions[-1].function_name
            not in {
                "deposit",
                "requestWhitelistRelease",
                "executeWhitelistRelease",
                "cancelWhitelistRelease",
            }
            or any(item.chain_id != chain_id for item in transactions)
        ):
            _reject("NOTILT_PLAN_INVALID", "NoTilt plan contains an unexpected transaction")
        planned = [item.to_dict() for item in transactions]
        with self.database.session_factory.begin() as session:
            transfer = session.get(CapitalTransfer, capital_transfer_id, with_for_update=True)
            if transfer is None:
                _reject("CAPITAL_TRANSFER_NOT_FOUND", "capital transfer does not exist")
            self._require_role(
                session, actor_id, "capital.execute", transfer.account_id, transfer.venue
            )
            if transfer.environment != ExecutionEnvironment.LIVE.value:
                _reject(
                    "NOTILT_TRANSFER_ENVIRONMENT_INVALID",
                    "NoTilt plans require a LIVE capital transfer",
                )
            expected_direction = (
                CapitalDirection.VENUE_TO_VAULT.value
                if transport_state == "DEPOSIT_PLAN_READY"
                else CapitalDirection.VAULT_TO_VENUE.value
            )
            if transfer.direction != expected_direction:
                _reject("NOTILT_PLAN_DIRECTION_INVALID", "NoTilt plan direction does not match")
            allowed_previous_by_state: dict[str, set[str | None]] = {
                "DEPOSIT_PLAN_READY": {None, "DEPOSIT_PLAN_READY"},
                "RELEASE_REQUEST_PLAN_READY": {None, "RELEASE_REQUEST_PLAN_READY"},
                "RELEASE_EXECUTION_PLAN_READY": {
                    "RELEASE_REQUEST_CONFIRMED",
                    "RELEASE_EXECUTION_PLAN_READY",
                },
                "RELEASE_CANCELLATION_PLAN_READY": {
                    "RELEASE_REQUEST_CONFIRMED",
                    "RELEASE_CANCELLATION_PLAN_READY",
                },
            }
            allowed_previous = allowed_previous_by_state[transport_state]
            if transfer.transport_state not in allowed_previous:
                _reject("NOTILT_PLAN_STATE_INVALID", "NoTilt plan is not valid in this state")
            if (
                transport_state
                in {
                    "DEPOSIT_PLAN_READY",
                    "RELEASE_REQUEST_PLAN_READY",
                }
                and transfer.status != CapitalTransferStatus.SOURCE_RESERVED.value
            ):
                _reject("NOTILT_PLAN_STATE_INVALID", "initial NoTilt plan is no longer available")
            if transport_state in {
                "RELEASE_EXECUTION_PLAN_READY",
                "RELEASE_CANCELLATION_PLAN_READY",
            } and transfer.status not in {
                CapitalTransferStatus.IN_FLIGHT.value,
                CapitalTransferStatus.MANUAL_REQUIRED.value,
            }:
                _reject("NOTILT_PLAN_STATE_INVALID", "release request is not awaiting resolution")
            if transfer.transport_state == transport_state:
                if (
                    transfer.transport == "NOTILT"
                    and transfer.chain_id == chain_id
                    and transfer.planned_transactions == planned
                ):
                    return
                _reject("NOTILT_PLAN_IDENTITY_CONFLICT", "NoTilt plan changed for the same stage")
            transfer.transport = "NOTILT"
            transfer.chain_id = chain_id
            transfer.transport_state = transport_state
            transfer.planned_transactions = planned
            transfer.updated_at = now
            transfer.version += 1
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="NOTILT_UNSIGNED_PLAN_RECORDED",
                object_type="CapitalTransfer",
                object_id=transfer.capital_transfer_id,
                reason=transport_state,
                correlation_id=transfer.correlation_id,
                object_version=transfer.version,
                now=now,
            )

    def record_notilt_receipt(
        self,
        capital_transfer_id: UUID,
        actor_id: UUID,
        receipt: NoTiltReceipt,
        *,
        now: datetime,
    ) -> str:
        with self.database.session_factory.begin() as session:
            transfer = session.get(CapitalTransfer, capital_transfer_id, with_for_update=True)
            if transfer is None:
                _reject("CAPITAL_TRANSFER_NOT_FOUND", "capital transfer does not exist")
            self._require_role(
                session, actor_id, "capital.reconcile", transfer.account_id, transfer.venue
            )
            authorization = session.get(TransferAuthorization, transfer.transfer_authorization_id)
            if authorization is None:
                _reject("TRANSFER_AUTHORIZATION_NOT_FOUND", "transfer authorization is missing")
            if (
                transfer.transport != "NOTILT"
                or transfer.chain_id != receipt.chain_id
                or transfer.environment != ExecutionEnvironment.LIVE.value
            ):
                _reject("NOTILT_RECEIPT_SCOPE_MISMATCH", "receipt is outside the NoTilt transfer")
            vault = (
                transfer.source_id
                if transfer.direction == CapitalDirection.VAULT_TO_VENUE.value
                else transfer.destination_id
            )
            if receipt.vault.lower() != vault.lower():
                _reject("NOTILT_RECEIPT_SCOPE_MISMATCH", "receipt Vault does not match")
            session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {
                    "key": _advisory_lock_key(
                        str(receipt.chain_id),
                        "notilt-receipt",
                        receipt.transaction_hash,
                    )
                },
            )
            replay = session.scalar(
                select(CapitalTransfer.capital_transfer_id)
                .where(
                    CapitalTransfer.capital_transfer_id != capital_transfer_id,
                    CapitalTransfer.chain_id == receipt.chain_id,
                    CapitalTransfer.confirmed_transaction_hashes.contains(
                        [receipt.transaction_hash]
                    ),
                )
                .limit(1)
            )
            if replay is not None:
                _reject(
                    "NOTILT_RECEIPT_REPLAY",
                    "NoTilt transaction receipt is already bound to another transfer",
                )
            confirmed = list(transfer.confirmed_transaction_hashes)
            if receipt.transaction_hash in confirmed:
                return str(transfer.transport_state)
            expected_state = {
                "DEPOSIT": "DEPOSIT_PLAN_READY",
                "RELEASE_REQUEST": "RELEASE_REQUEST_PLAN_READY",
                "RELEASE_EXECUTION": "RELEASE_EXECUTION_PLAN_READY",
                "RELEASE_CANCELLATION": "RELEASE_CANCELLATION_PLAN_READY",
            }[receipt.receipt_kind]
            if transfer.transport_state != expected_state:
                _reject("NOTILT_RECEIPT_STATE_INVALID", "receipt is unexpected for this transfer")
            if receipt.block_timestamp > now + MAX_FACT_CLOCK_SKEW:
                _reject("FACT_TIME_INVALID", "NoTilt receipt time cannot be in the future")

            if receipt.receipt_kind == "DEPOSIT":
                if (
                    transfer.direction != CapitalDirection.VENUE_TO_VAULT.value
                    or receipt.asset != transfer.asset
                    or receipt.requested_amount != authorization.min_received
                    or receipt.credited_amount != authorization.min_received
                ):
                    _reject(
                        "NOTILT_RECEIPT_AMOUNT_INVALID",
                        "NoTilt deposit receipt is outside the authorization",
                    )
                fee = transfer.gross_amount - receipt.credited_amount
                if fee < 0 or fee > authorization.max_fee:
                    _reject(
                        "CAPITAL_DESTINATION_AMOUNT_INVALID",
                        "NoTilt credited amount exceeds the authorized fee budget",
                    )
                transfer.fee_amount = fee
                transfer.net_received = receipt.credited_amount
                transfer.status = CapitalTransferStatus.DESTINATION_CONFIRMED.value
                transfer.transport_state = "DEPOSIT_CONFIRMED"
                transfer.external_transfer_id = receipt.transaction_hash
            elif receipt.receipt_kind == "RELEASE_REQUEST":
                if (
                    transfer.direction != CapitalDirection.VAULT_TO_VENUE.value
                    or receipt.asset != transfer.asset
                    or receipt.request_id is None
                    or receipt.net_amount != authorization.min_received
                    or receipt.fee is None
                    or receipt.execute_after is None
                    or receipt.expires_at is None
                    or receipt.execute_after >= receipt.expires_at
                ):
                    _reject(
                        "NOTILT_RECEIPT_AMOUNT_INVALID",
                        "NoTilt release request is outside the authorization",
                    )
                transfer.fee_amount = receipt.fee
                transfer.protocol_request_id = receipt.request_id
                transfer.protocol_execute_after = receipt.execute_after
                transfer.protocol_expires_at = receipt.expires_at
                transfer.external_transfer_id = receipt.request_id
                transfer.transport_state = "RELEASE_REQUEST_CONFIRMED"
                transfer.status = (
                    CapitalTransferStatus.MANUAL_REQUIRED.value
                    if (
                        receipt.fee > authorization.max_fee
                        or receipt.net_amount + receipt.fee > transfer.gross_amount
                    )
                    else CapitalTransferStatus.IN_FLIGHT.value
                )
            elif receipt.receipt_kind == "RELEASE_EXECUTION":
                if (
                    transfer.direction != CapitalDirection.VAULT_TO_VENUE.value
                    or receipt.request_id != transfer.protocol_request_id
                    or transfer.protocol_execute_after is None
                    or transfer.protocol_expires_at is None
                    or receipt.block_timestamp < transfer.protocol_execute_after
                    or receipt.block_timestamp >= transfer.protocol_expires_at
                    or transfer.fee_amount is None
                    or transfer.fee_amount > authorization.max_fee
                ):
                    _reject(
                        "NOTILT_RECEIPT_REQUEST_INVALID",
                        "NoTilt release execution is outside the authorized request",
                    )
                transfer.transport_state = "RELEASE_EXECUTION_CONFIRMED"
                transfer.status = CapitalTransferStatus.IN_FLIGHT.value
            else:
                if receipt.request_id != transfer.protocol_request_id:
                    _reject(
                        "NOTILT_RECEIPT_REQUEST_INVALID",
                        "NoTilt cancellation request identity does not match",
                    )
                transfer.transport_state = "RELEASE_CANCELLED"
                transfer.status = CapitalTransferStatus.FAILED_SOURCE_RESTORED.value

            confirmed.append(receipt.transaction_hash)
            transfer.confirmed_transaction_hashes = confirmed
            transfer.transaction_reference = receipt.transaction_hash
            transfer.observed_at = receipt.block_timestamp
            transfer.updated_at = now
            transfer.version += 1
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="NOTILT_RECEIPT_VERIFIED",
                object_type="CapitalTransfer",
                object_id=transfer.capital_transfer_id,
                reason=f"{receipt.receipt_kind}:{transfer.transport_state}",
                correlation_id=transfer.correlation_id,
                object_version=transfer.version,
                now=now,
            )
            return str(transfer.transport_state)

    def record_capital_submission(
        self,
        capital_transfer_id: UUID,
        actor_id: UUID,
        submission: CapitalTransferSubmission,
        *,
        now: datetime,
    ) -> None:
        if submission.status != CapitalTransferStatus.SUBMITTED.value:
            _reject("CAPITAL_SUBMISSION_INVALID", "adapter submission status is invalid")
        with self.database.session_factory.begin() as session:
            transfer = session.get(CapitalTransfer, capital_transfer_id, with_for_update=True)
            if transfer is None:
                _reject("CAPITAL_TRANSFER_NOT_FOUND", "capital transfer does not exist")
            self._require_role(
                session, actor_id, "capital.execute", transfer.account_id, transfer.venue
            )
            if transfer.status == CapitalTransferStatus.SUBMITTED.value:
                if transfer.external_transfer_id == submission.external_transfer_id:
                    return
                _reject("CAPITAL_TRANSFER_IDENTITY_CONFLICT", "submission identity changed")
            if transfer.status != CapitalTransferStatus.SOURCE_RESERVED.value:
                _reject("CAPITAL_TRANSFER_NOT_SUBMITTABLE", "transfer cannot be submitted again")
            transfer.status = CapitalTransferStatus.SUBMITTED.value
            transfer.external_transfer_id = submission.external_transfer_id
            transfer.observed_at = submission.observed_at
            transfer.updated_at = now
            transfer.version += 1
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="CAPITAL_TRANSFER_SUBMITTED_MOCK",
                object_type="CapitalTransfer",
                object_id=transfer.capital_transfer_id,
                reason=submission.external_transfer_id,
                correlation_id=transfer.correlation_id,
                object_version=transfer.version,
                now=now,
            )

    def record_capital_observation(
        self,
        capital_transfer_id: UUID,
        actor_id: UUID,
        status: CapitalTransferStatus,
        *,
        transaction_reference: str | None = None,
        fee_amount: Decimal | None = None,
        net_received: Decimal | None = None,
        now: datetime,
    ) -> CapitalTransferStatus:
        allowed = {
            CapitalTransferStatus.SUBMITTED: {
                CapitalTransferStatus.IN_FLIGHT,
                CapitalTransferStatus.UNKNOWN,
                CapitalTransferStatus.FAILED_SOURCE_RESTORED,
            },
            CapitalTransferStatus.IN_FLIGHT: {
                CapitalTransferStatus.DESTINATION_CONFIRMED,
                CapitalTransferStatus.UNKNOWN,
            },
            CapitalTransferStatus.UNKNOWN: {
                CapitalTransferStatus.IN_FLIGHT,
                CapitalTransferStatus.DESTINATION_CONFIRMED,
                CapitalTransferStatus.MANUAL_REQUIRED,
                CapitalTransferStatus.FAILED_SOURCE_RESTORED,
            },
            CapitalTransferStatus.MANUAL_REQUIRED: {
                CapitalTransferStatus.IN_FLIGHT,
                CapitalTransferStatus.DESTINATION_CONFIRMED,
                CapitalTransferStatus.FAILED_SOURCE_RESTORED,
            },
        }
        with self.database.session_factory.begin() as session:
            transfer = session.get(CapitalTransfer, capital_transfer_id, with_for_update=True)
            if transfer is None:
                _reject("CAPITAL_TRANSFER_NOT_FOUND", "capital transfer does not exist")
            self._require_role(
                session, actor_id, "capital.reconcile", transfer.account_id, transfer.venue
            )
            current = CapitalTransferStatus(transfer.status)
            if status is current:
                return current
            if status not in allowed.get(current, set()):
                _reject("CAPITAL_TRANSFER_TRANSITION_INVALID", "capital transition is invalid")
            authorization = session.get(TransferAuthorization, transfer.transfer_authorization_id)
            if authorization is None:
                _reject("TRANSFER_AUTHORIZATION_NOT_FOUND", "transfer authorization is missing")
            if status is CapitalTransferStatus.DESTINATION_CONFIRMED:
                if fee_amount is None or net_received is None:
                    _reject(
                        "CAPITAL_DESTINATION_EVIDENCE_REQUIRED",
                        "destination confirmation requires fee and net receipt",
                    )
                if (
                    fee_amount > authorization.max_fee
                    or net_received < authorization.min_received
                    or net_received + fee_amount > transfer.gross_amount
                ):
                    _reject(
                        "CAPITAL_DESTINATION_AMOUNT_INVALID",
                        "destination receipt is outside the authorization",
                    )
                transfer.fee_amount = fee_amount
                transfer.net_received = net_received
            transfer.status = status.value
            transfer.transaction_reference = transaction_reference
            transfer.observed_at = now
            transfer.updated_at = now
            transfer.version += 1
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="CAPITAL_TRANSFER_OBSERVED",
                object_type="CapitalTransfer",
                object_id=transfer.capital_transfer_id,
                reason=status.value,
                correlation_id=transfer.correlation_id,
                object_version=transfer.version,
                now=now,
            )
            return status

    def reconcile_capital_transfer(
        self, capital_transfer_id: UUID, actor_id: UUID, *, now: datetime
    ) -> str:
        with self.database.session_factory.begin() as session:
            transfer = session.get(CapitalTransfer, capital_transfer_id, with_for_update=True)
            if transfer is None:
                _reject("CAPITAL_TRANSFER_NOT_FOUND", "capital transfer does not exist")
            self._require_role(
                session, actor_id, "capital.reconcile", transfer.account_id, transfer.venue
            )
            authorization = session.get(TransferAuthorization, transfer.transfer_authorization_id)
            if authorization is None:
                _reject("TRANSFER_AUTHORIZATION_NOT_FOUND", "transfer authorization is missing")
            if (
                transfer.status == CapitalTransferStatus.IN_FLIGHT.value
                and transfer.transport_state == "RELEASE_EXECUTION_CONFIRMED"
                and transfer.fee_amount is not None
            ):
                source_fact = self._capital_balance(
                    session,
                    environment=authorization.environment,
                    endpoint_type=authorization.source_type,
                    endpoint_id=authorization.source_id,
                    venue=authorization.venue,
                    asset=authorization.asset,
                )
                destination_fact = self._capital_balance(
                    session,
                    environment=authorization.environment,
                    endpoint_type=authorization.destination_type,
                    endpoint_id=authorization.destination_id,
                    venue=authorization.venue,
                    asset=authorization.asset,
                )
                expected_source = (
                    transfer.source_balance_before
                    - authorization.min_received
                    - transfer.fee_amount
                )
                expected_destination = (
                    transfer.destination_balance_before + authorization.min_received
                )
                if (
                    source_fact.observed_at >= transfer.observed_at
                    and destination_fact.observed_at >= transfer.observed_at
                    and source_fact.available_balance == expected_source
                    and destination_fact.available_balance == expected_destination
                ):
                    transfer.net_received = authorization.min_received
                    transfer.status = CapitalTransferStatus.DESTINATION_CONFIRMED.value
                    transfer.version += 1
            differences: list[str] = []
            if transfer.status in {
                CapitalTransferStatus.UNKNOWN.value,
                CapitalTransferStatus.MANUAL_REQUIRED.value,
            }:
                result = ReconciliationStatus.UNKNOWN.value
                differences.append("TRANSFER_OUTCOME_UNKNOWN")
            elif transfer.status in {
                CapitalTransferStatus.SOURCE_RESERVED.value,
                CapitalTransferStatus.SUBMITTED.value,
                CapitalTransferStatus.IN_FLIGHT.value,
            }:
                result = "IN_FLIGHT"
            else:
                source = self._capital_balance(
                    session,
                    environment=authorization.environment,
                    endpoint_type=authorization.source_type,
                    endpoint_id=authorization.source_id,
                    venue=authorization.venue,
                    asset=authorization.asset,
                )
                destination = self._capital_balance(
                    session,
                    environment=authorization.environment,
                    endpoint_type=authorization.destination_type,
                    endpoint_id=authorization.destination_id,
                    venue=authorization.venue,
                    asset=authorization.asset,
                )
                if transfer.status == CapitalTransferStatus.FAILED_SOURCE_RESTORED.value:
                    if source.available_balance < transfer.source_balance_before:
                        differences.append("SOURCE_NOT_RESTORED")
                else:
                    if transfer.net_received is None:
                        differences.append("DESTINATION_NET_UNKNOWN")
                    else:
                        expected_source_debit = transfer.net_received + (
                            transfer.fee_amount or Decimal(0)
                        )
                        if source.available_balance > (
                            transfer.source_balance_before - expected_source_debit
                        ):
                            differences.append("SOURCE_DEBIT_NOT_CONFIRMED")
                        if destination.available_balance < (
                            transfer.destination_balance_before + transfer.net_received
                        ):
                            differences.append("DESTINATION_CREDIT_NOT_CONFIRMED")
                        if source.observed_at < transfer.observed_at:
                            differences.append("SOURCE_FACT_STALE")
                        if destination.observed_at < transfer.observed_at:
                            differences.append("DESTINATION_FACT_STALE")
                result = (
                    ReconciliationStatus.DIFFERENCE.value
                    if differences
                    else ReconciliationStatus.MATCH.value
                )
                if (
                    result == ReconciliationStatus.MATCH.value
                    and transfer.status == CapitalTransferStatus.DESTINATION_CONFIRMED.value
                ):
                    transfer.status = CapitalTransferStatus.SETTLED.value
                    transfer.version += 1
            transfer.reconciliation_status = result
            transfer.reconciliation_details = differences
            transfer.reconciled_at = now
            transfer.updated_at = now
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="CAPITAL_TRANSFER_RECONCILED",
                object_type="CapitalTransfer",
                object_id=transfer.capital_transfer_id,
                reason=result,
                correlation_id=transfer.correlation_id,
                object_version=transfer.version,
                now=now,
            )
            return result

    def set_capability_gate(
        self,
        capability_key: str,
        status: CapabilityStatus,
        reason: str,
        actor_id: UUID,
        *,
        now: datetime,
    ) -> None:
        with self.database.session_factory.begin() as session:
            self._require_role(session, actor_id, "capability.manage")
            gate = session.get(CapabilityGate, capability_key, with_for_update=True)
            if gate is None:
                _reject("CAPABILITY_GATE_NOT_FOUND", "unknown capability")
            gate.status = status.value
            gate.reason = reason
            gate.operator_id = str(actor_id)
            gate.updated_at = now
