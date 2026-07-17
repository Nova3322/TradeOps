from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from trading_control_plane.commands import (
    CommandChannel,
    CommandEnvelope,
    CommandOutcome,
    CommandRejected,
    CommandStatus,
    DomainEvent,
    hash_json,
)
from trading_control_plane.metrics import VENUE_FACT_INPUT_LINKS, VENUE_FACT_NORMALIZATIONS
from trading_control_plane.reconciliation import ReconciliationSourceType
from trading_control_plane.reconciliation_models import (
    ExecutionReconciliationInput,
    ExecutionReconciliationRun,
    ExecutionReconciliationRunState,
)
from trading_control_plane.sender_fencing_models import (
    ExecutionSenderScope,
    ExecutionSenderScopeState,
)
from trading_control_plane.venue_fact_models import (
    VenueFactInputLink,
    VenueFill,
    VenueOrderObservation,
)

VENUE_FACT_SERVICE_PRINCIPAL = "execution-reconciliation-service"


class VenueOrderStatus(StrEnum):
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


class VenueSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class VenuePositionSide(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    BOTH = "BOTH"


class LiquidityRole(StrEnum):
    MAKER = "MAKER"
    TAKER = "TAKER"
    UNKNOWN = "UNKNOWN"


class FeeEffect(StrEnum):
    CHARGE = "CHARGE"
    REBATE = "REBATE"
    ZERO = "ZERO"


class VenueFactCollectionBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    venue_fact_input_link_id: UUID
    reconciliation_input_id: UUID
    reconciliation_input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    venue: str = Field(min_length=1, max_length=80)
    execution_domain: str = Field(min_length=1, max_length=120)
    account_id: str = Field(min_length=1, max_length=160)
    instrument_id: str = Field(min_length=1, max_length=255)
    observed_client_order_id: str | None = Field(default=None, max_length=160)
    venue_order_id: str = Field(min_length=1, max_length=255)
    source_version: str = Field(min_length=1, max_length=160)
    normalization_version: str = Field(min_length=1, max_length=160)
    normalized_payload: dict[str, Any]
    raw_payload_ref: str = Field(min_length=1, max_length=255)
    raw_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_ref: str = Field(min_length=1, max_length=255)
    evidence_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    event_time: datetime
    venue_observed_at: datetime
    received_at: datetime

    @model_validator(mode="after")
    def collection_evidence_is_ordered(self) -> Self:
        times = (self.event_time, self.venue_observed_at, self.received_at)
        if any(value.tzinfo is None for value in times):
            raise ValueError("venue fact timestamps must be timezone-aware")
        if not self.event_time <= self.venue_observed_at <= self.received_at:
            raise ValueError("venue fact timestamps are not monotonic")
        return self


class RecordVenueOrderObservationRequest(VenueFactCollectionBinding):
    venue_order_observation_id: UUID
    venue_update_id: str = Field(min_length=1, max_length=255)
    status: VenueOrderStatus
    side: VenueSide
    position_side: VenuePositionSide
    reduce_only: bool
    order_type: str = Field(min_length=1, max_length=40)
    time_in_force: str = Field(min_length=1, max_length=40)
    original_quantity: Decimal = Field(gt=0)
    cumulative_filled_quantity: Decimal = Field(ge=0)
    known_remaining_quantity: Decimal = Field(ge=0)
    zero_fill_confirmed: bool
    terminal: bool
    observation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def observation_is_self_consistent(self) -> Self:
        filled = self.cumulative_filled_quantity
        remaining = self.known_remaining_quantity
        original = self.original_quantity
        if filled + remaining > original:
            raise ValueError("venue order quantities exceed original quantity")
        if self.status is VenueOrderStatus.OPEN:
            valid = filled == 0 and remaining == original and not self.terminal
        elif self.status is VenueOrderStatus.PARTIALLY_FILLED:
            valid = filled > 0 and remaining > 0 and filled + remaining == original
            valid = valid and not self.terminal
        elif self.status is VenueOrderStatus.FILLED:
            valid = filled == original and remaining == 0 and self.terminal
        elif self.status is VenueOrderStatus.CANCEL_PENDING:
            valid = filled + remaining == original and not self.terminal
        elif self.status in {VenueOrderStatus.CANCELLED, VenueOrderStatus.EXPIRED}:
            valid = remaining == 0 and self.terminal and self.zero_fill_confirmed == (filled == 0)
        elif self.status is VenueOrderStatus.REJECTED:
            valid = filled == 0 and remaining == 0 and self.terminal and self.zero_fill_confirmed
        else:
            valid = not self.terminal and not self.zero_fill_confirmed
        if not valid:
            raise ValueError("venue order status semantics are inconsistent")
        if self.observation_hash != hash_json(_order_observation_contract(self)):
            raise ValueError("venue order observation hash mismatch")
        if self.evidence_hash != hash_json(self.model_dump(mode="json", exclude={"evidence_hash"})):
            raise ValueError("venue order evidence hash mismatch")
        return self


class RecordVenueFillRequest(VenueFactCollectionBinding):
    venue_fill_id: UUID
    venue_trade_id: str = Field(min_length=1, max_length=255)
    side: VenueSide
    position_side: VenuePositionSide
    reduce_only: bool
    quantity: Decimal = Field(gt=0)
    price: Decimal = Field(gt=0)
    contract_multiplier: Decimal = Field(gt=0)
    notional: Decimal = Field(gt=0)
    liquidity_role: LiquidityRole
    fee_amount: Decimal
    fee_currency: str = Field(min_length=1, max_length=80)
    fee_effect: FeeEffect
    realized_pnl: Decimal | None = None
    settlement_currency: str = Field(min_length=1, max_length=80)
    fill_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def fill_is_self_consistent(self) -> Self:
        if self.notional != self.quantity * self.price * self.contract_multiplier:
            raise ValueError("venue fill notional mismatch")
        fee_valid = (
            (self.fee_effect is FeeEffect.CHARGE and self.fee_amount > 0)
            or (self.fee_effect is FeeEffect.REBATE and self.fee_amount < 0)
            or (self.fee_effect is FeeEffect.ZERO and self.fee_amount == 0)
        )
        if not fee_valid:
            raise ValueError("venue fill fee sign mismatch")
        if self.fill_hash != hash_json(_fill_contract(self)):
            raise ValueError("venue fill hash mismatch")
        if self.evidence_hash != hash_json(self.model_dump(mode="json", exclude={"evidence_hash"})):
            raise ValueError("venue fill evidence hash mismatch")
        return self


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _decimal(value: Decimal) -> str:
    normalized = format(value, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return "0" if normalized in {"", "-0"} else normalized


def _order_observation_contract(request: RecordVenueOrderObservationRequest) -> dict[str, Any]:
    return {
        "venue": request.venue,
        "execution_domain": request.execution_domain,
        "account_id": request.account_id,
        "instrument_id": request.instrument_id,
        "observed_client_order_id": request.observed_client_order_id,
        "venue_order_id": request.venue_order_id,
        "venue_update_id": request.venue_update_id,
        "status": request.status.value,
        "side": request.side.value,
        "position_side": request.position_side.value,
        "reduce_only": request.reduce_only,
        "order_type": request.order_type,
        "time_in_force": request.time_in_force,
        "original_quantity": _decimal(request.original_quantity),
        "cumulative_filled_quantity": _decimal(request.cumulative_filled_quantity),
        "known_remaining_quantity": _decimal(request.known_remaining_quantity),
        "zero_fill_confirmed": request.zero_fill_confirmed,
        "terminal": request.terminal,
        "event_time": _iso(request.event_time),
    }


def _fill_contract(request: RecordVenueFillRequest) -> dict[str, Any]:
    return {
        "venue": request.venue,
        "execution_domain": request.execution_domain,
        "account_id": request.account_id,
        "instrument_id": request.instrument_id,
        "observed_client_order_id": request.observed_client_order_id,
        "venue_order_id": request.venue_order_id,
        "venue_trade_id": request.venue_trade_id,
        "side": request.side.value,
        "position_side": request.position_side.value,
        "reduce_only": request.reduce_only,
        "quantity": _decimal(request.quantity),
        "price": _decimal(request.price),
        "contract_multiplier": _decimal(request.contract_multiplier),
        "notional": _decimal(request.notional),
        "liquidity_role": request.liquidity_role.value,
        "fee_amount": _decimal(request.fee_amount),
        "fee_currency": request.fee_currency,
        "fee_effect": request.fee_effect.value,
        "realized_pnl": (None if request.realized_pnl is None else _decimal(request.realized_pnl)),
        "settlement_currency": request.settlement_currency,
        "event_time": _iso(request.event_time),
    }


class VenueFactNormalizationService:
    order_command_type = "execution.venue-order-observation.record.v1"
    fill_command_type = "execution.venue-fill.record.v1"

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))

    def record_order_observation(
        self, session: Session, envelope: CommandEnvelope
    ) -> CommandOutcome:
        run_id = self._run_id(envelope, self.order_command_type)
        try:
            request = RecordVenueOrderObservationRequest.model_validate(envelope.payload)
        except ValidationError as exc:
            raise CommandRejected("VENUE_ORDER_OBSERVATION_INVALID", str(exc)) from exc
        now = self._clock()
        self._lock_external_identity(
            session,
            f"venue-order:{envelope.scope.get('organization_id')}:{request.venue}:"
            f"{request.execution_domain}:"
            f"{request.account_id}:{request.venue_order_id}:{request.venue_update_id}",
        )
        run, reconciliation_input = self._validate_collection_context(
            session,
            envelope,
            run_id,
            request,
            ReconciliationSourceType.VENUE_ORDERS,
            now,
        )
        existing = session.execute(
            select(VenueOrderObservation).where(
                VenueOrderObservation.organization_id == run.organization_id,
                VenueOrderObservation.venue == request.venue,
                VenueOrderObservation.execution_domain == request.execution_domain,
                VenueOrderObservation.account_id == request.account_id,
                VenueOrderObservation.venue_order_id == request.venue_order_id,
                VenueOrderObservation.venue_update_id == request.venue_update_id,
            )
        ).scalar_one_or_none()
        identity_owner = session.get(VenueOrderObservation, request.venue_order_observation_id)
        if identity_owner is not None and (
            existing is None
            or identity_owner.venue_order_observation_id != existing.venue_order_observation_id
        ):
            raise CommandRejected(
                "VENUE_ORDER_OBSERVATION_ID_CONFLICT",
                "venue order observation identity already exists",
            )
        new_fact = existing is None
        if existing is None:
            self._require_new_fact_link_possible(session, reconciliation_input, request)
            existing = VenueOrderObservation(
                venue_order_observation_id=request.venue_order_observation_id,
                organization_id=run.organization_id,
                first_seen_run_id=run.run_id,
                first_seen_input_id=reconciliation_input.input_id,
                venue=request.venue,
                execution_domain=request.execution_domain,
                account_id=request.account_id,
                instrument_id=request.instrument_id,
                observed_client_order_id=request.observed_client_order_id,
                venue_order_id=request.venue_order_id,
                venue_update_id=request.venue_update_id,
                status=request.status.value,
                side=request.side.value,
                position_side=request.position_side.value,
                reduce_only=request.reduce_only,
                order_type=request.order_type,
                time_in_force=request.time_in_force,
                original_quantity=request.original_quantity,
                cumulative_filled_quantity=request.cumulative_filled_quantity,
                known_remaining_quantity=request.known_remaining_quantity,
                zero_fill_confirmed=request.zero_fill_confirmed,
                terminal=request.terminal,
                fact_authority="VENUE_PRIVATE",
                environment="SHADOW",
                live_dispatch_eligible=False,
                source_version=request.source_version,
                normalization_version=request.normalization_version,
                normalized_payload=request.normalized_payload,
                raw_payload_ref=request.raw_payload_ref,
                raw_payload_hash=request.raw_payload_hash,
                evidence_ref=request.evidence_ref,
                evidence_hash=request.evidence_hash,
                observation_hash=request.observation_hash,
                event_time=request.event_time,
                venue_observed_at=request.venue_observed_at,
                first_received_at=request.received_at,
                recorded_at=now,
            )
            session.add(existing)
            session.flush()
        elif (
            existing.organization_id != run.organization_id
            or existing.observation_hash != request.observation_hash
        ):
            raise CommandRejected(
                "VENUE_ORDER_OBSERVATION_CONFLICT",
                "venue order update identity has different immutable semantics",
            )
        return self._link_and_outcome(
            session,
            run,
            reconciliation_input,
            request,
            ReconciliationSourceType.VENUE_ORDERS,
            "ORDER_OBSERVATION",
            existing.venue_order_observation_id,
            None,
            existing.observation_hash,
            new_fact,
            now,
        )

    def record_fill(self, session: Session, envelope: CommandEnvelope) -> CommandOutcome:
        run_id = self._run_id(envelope, self.fill_command_type)
        try:
            request = RecordVenueFillRequest.model_validate(envelope.payload)
        except ValidationError as exc:
            raise CommandRejected("VENUE_FILL_INVALID", str(exc)) from exc
        now = self._clock()
        self._lock_external_identity(
            session,
            f"venue-fill:{envelope.scope.get('organization_id')}:{request.venue}:"
            f"{request.execution_domain}:"
            f"{request.account_id}:{request.venue_trade_id}",
        )
        run, reconciliation_input = self._validate_collection_context(
            session,
            envelope,
            run_id,
            request,
            ReconciliationSourceType.VENUE_FILLS,
            now,
        )
        existing = session.execute(
            select(VenueFill).where(
                VenueFill.organization_id == run.organization_id,
                VenueFill.venue == request.venue,
                VenueFill.execution_domain == request.execution_domain,
                VenueFill.account_id == request.account_id,
                VenueFill.venue_trade_id == request.venue_trade_id,
            )
        ).scalar_one_or_none()
        identity_owner = session.get(VenueFill, request.venue_fill_id)
        if identity_owner is not None and (
            existing is None or identity_owner.venue_fill_id != existing.venue_fill_id
        ):
            raise CommandRejected("VENUE_FILL_ID_CONFLICT", "venue fill identity already exists")
        new_fact = existing is None
        if existing is None:
            self._require_new_fact_link_possible(session, reconciliation_input, request)
            existing = VenueFill(
                venue_fill_id=request.venue_fill_id,
                organization_id=run.organization_id,
                first_seen_run_id=run.run_id,
                first_seen_input_id=reconciliation_input.input_id,
                venue=request.venue,
                execution_domain=request.execution_domain,
                account_id=request.account_id,
                instrument_id=request.instrument_id,
                observed_client_order_id=request.observed_client_order_id,
                venue_order_id=request.venue_order_id,
                venue_trade_id=request.venue_trade_id,
                side=request.side.value,
                position_side=request.position_side.value,
                reduce_only=request.reduce_only,
                quantity=request.quantity,
                price=request.price,
                contract_multiplier=request.contract_multiplier,
                notional=request.notional,
                liquidity_role=request.liquidity_role.value,
                fee_amount=request.fee_amount,
                fee_currency=request.fee_currency,
                fee_effect=request.fee_effect.value,
                realized_pnl=request.realized_pnl,
                settlement_currency=request.settlement_currency,
                venue_confirmed=True,
                fact_authority="VENUE_PRIVATE",
                environment="SHADOW",
                live_dispatch_eligible=False,
                source_version=request.source_version,
                normalization_version=request.normalization_version,
                normalized_payload=request.normalized_payload,
                raw_payload_ref=request.raw_payload_ref,
                raw_payload_hash=request.raw_payload_hash,
                evidence_ref=request.evidence_ref,
                evidence_hash=request.evidence_hash,
                fill_hash=request.fill_hash,
                event_time=request.event_time,
                venue_observed_at=request.venue_observed_at,
                first_received_at=request.received_at,
                recorded_at=now,
            )
            session.add(existing)
            session.flush()
        elif (
            existing.organization_id != run.organization_id
            or existing.fill_hash != request.fill_hash
        ):
            raise CommandRejected(
                "VENUE_FILL_CONFLICT",
                "venue trade identity has different immutable semantics",
            )
        return self._link_and_outcome(
            session,
            run,
            reconciliation_input,
            request,
            ReconciliationSourceType.VENUE_FILLS,
            "FILL",
            None,
            existing.venue_fill_id,
            existing.fill_hash,
            new_fact,
            now,
        )

    @staticmethod
    def _run_id(envelope: CommandEnvelope, expected_command_type: str) -> UUID:
        if envelope.command_type != expected_command_type:
            raise CommandRejected("COMMAND_TYPE_MISMATCH", "unexpected command type")
        if (
            envelope.channel is not CommandChannel.INTERNAL
            or envelope.service_principal != VENUE_FACT_SERVICE_PRINCIPAL
        ):
            raise CommandRejected(
                "VENUE_FACT_SERVICE_REQUIRED",
                "only the reconciliation service may normalize private venue facts",
            )
        if envelope.object_type != "ExecutionReconciliationRun" or envelope.object_id is None:
            raise CommandRejected(
                "OBJECT_BINDING_MISMATCH", "ExecutionReconciliationRun binding is required"
            )
        try:
            return UUID(envelope.object_id)
        except ValueError as exc:
            raise CommandRejected("OBJECT_BINDING_MISMATCH", "run identity is invalid") from exc

    @staticmethod
    def _lock_external_identity(session: Session, lock_key: str) -> None:
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": lock_key},
        )

    @staticmethod
    def _validate_collection_context(
        session: Session,
        envelope: CommandEnvelope,
        run_id: UUID,
        request: VenueFactCollectionBinding,
        expected_source: ReconciliationSourceType,
        now: datetime,
    ) -> tuple[ExecutionReconciliationRun, ExecutionReconciliationInput]:
        run = session.get(ExecutionReconciliationRun, run_id)
        state = session.execute(
            select(ExecutionReconciliationRunState)
            .where(ExecutionReconciliationRunState.run_id == run_id)
            .with_for_update()
        ).scalar_one_or_none()
        if run is None or state is None:
            raise CommandRejected("RECONCILIATION_RUN_NOT_FOUND", "run is unavailable")
        if envelope.scope.get("organization_id") != run.organization_id:
            raise CommandRejected("SCOPE_MISMATCH", "organization scope changed")
        if (
            state.status != "RUNNING"
            or state.phase != "COLLECTING"
            or run.environment != "SHADOW"
            or run.live_dispatch_eligible
        ):
            raise CommandRejected(
                "VENUE_FACT_COLLECTION_CLOSED",
                "venue facts require an active SHADOW collection phase",
            )
        if now >= run.deadline_at:
            raise CommandRejected(
                "VENUE_FACT_COLLECTION_EXPIRED", "run deadline elapsed before normalization"
            )
        latest_run_id = session.execute(
            select(ExecutionReconciliationRun.run_id)
            .where(ExecutionReconciliationRun.scope_id == run.scope_id)
            .order_by(
                ExecutionReconciliationRun.started_at.desc(),
                ExecutionReconciliationRun.run_id.desc(),
            )
            .limit(1)
        ).scalar_one()
        if latest_run_id != run.run_id:
            raise CommandRejected(
                "VENUE_FACT_RUN_NOT_LATEST", "only the latest scope run may normalize facts"
            )
        reconciliation_input = session.get(
            ExecutionReconciliationInput, request.reconciliation_input_id
        )
        if (
            reconciliation_input is None
            or reconciliation_input.run_id != run.run_id
            or reconciliation_input.organization_id != run.organization_id
            or reconciliation_input.source_type != expected_source.value
            or reconciliation_input.collection_status != "COMPLETE"
            or reconciliation_input.source_version != request.source_version
            or reconciliation_input.input_hash != request.reconciliation_input_hash
        ):
            raise CommandRejected(
                "VENUE_FACT_INPUT_MISMATCH",
                "venue fact is not bound to the exact complete source input",
            )
        if not (
            reconciliation_input.observed_from
            <= request.event_time
            <= reconciliation_input.observed_through
        ):
            raise CommandRejected(
                "VENUE_FACT_OUTSIDE_INPUT_WINDOW", "venue fact is outside the input watermark"
            )
        if request.received_at > now or reconciliation_input.received_at > now:
            raise CommandRejected("VENUE_FACT_FROM_FUTURE", "venue fact is from the future")
        scope = session.get(ExecutionSenderScope, run.scope_id)
        sender_state = session.get(ExecutionSenderScopeState, run.scope_id)
        if scope is None or sender_state is None:
            raise CommandRejected("VENUE_FACT_SCOPE_NOT_FOUND", "sender scope is unavailable")
        if (
            request.venue != scope.venue
            or request.execution_domain != scope.execution_domain
            or request.account_id != scope.account_id
        ):
            raise CommandRejected("VENUE_FACT_ROUTE_MISMATCH", "venue fact route changed")
        if (
            sender_state.status != "LEASED"
            or sender_state.active_lease_id != run.lease_id
            or sender_state.current_fencing_token != run.fencing_token
            or sender_state.lease_expires_at is None
            or now >= sender_state.lease_expires_at
        ):
            raise CommandRejected(
                "VENUE_FACT_SENDER_LEASE_STALE", "normalization authority is fenced or expired"
            )
        return run, reconciliation_input

    @staticmethod
    def _link_and_outcome(
        session: Session,
        run: ExecutionReconciliationRun,
        reconciliation_input: ExecutionReconciliationInput,
        request: VenueFactCollectionBinding,
        source_type: ReconciliationSourceType,
        fact_type: str,
        order_observation_id: UUID | None,
        fill_id: UUID | None,
        fact_hash: str,
        new_fact: bool,
        now: datetime,
    ) -> CommandOutcome:
        existing_link = session.execute(
            select(VenueFactInputLink).where(
                VenueFactInputLink.reconciliation_input_id == reconciliation_input.input_id,
                VenueFactInputLink.venue_order_observation_id == order_observation_id
                if order_observation_id is not None
                else VenueFactInputLink.venue_fill_id == fill_id,
            )
        ).scalar_one_or_none()
        link_values = {
            "run_id": str(run.run_id),
            "reconciliation_input_id": str(reconciliation_input.input_id),
            "organization_id": run.organization_id,
            "source_type": source_type.value,
            "venue_order_observation_id": None
            if order_observation_id is None
            else str(order_observation_id),
            "venue_fill_id": None if fill_id is None else str(fill_id),
            "input_hash": reconciliation_input.input_hash,
            "fact_hash": fact_hash,
            "raw_payload_hash": request.raw_payload_hash,
            "evidence_hash": request.evidence_hash,
            "observed_at": _iso(request.venue_observed_at),
            "received_at": _iso(request.received_at),
        }
        link_hash = hash_json(link_values)
        if existing_link is not None:
            if existing_link.link_hash != link_hash:
                raise CommandRejected(
                    "VENUE_FACT_INPUT_LINK_CONFLICT",
                    "input already links different evidence for this venue fact",
                )
            VENUE_FACT_NORMALIZATIONS.labels(fact_type, "EXISTING_FACT").inc()
            VENUE_FACT_INPUT_LINKS.labels(source_type.value, "ALREADY_LINKED").inc()
            return VenueFactNormalizationService._outcome(
                run,
                fact_type,
                order_observation_id,
                fill_id,
                existing_link.venue_fact_input_link_id,
                fact_hash,
                new_fact=False,
                new_link=False,
            )
        if session.get(VenueFactInputLink, request.venue_fact_input_link_id) is not None:
            raise CommandRejected(
                "VENUE_FACT_INPUT_LINK_ID_CONFLICT", "input link identity already exists"
            )
        linked_count = session.execute(
            select(func.count())
            .select_from(VenueFactInputLink)
            .where(VenueFactInputLink.reconciliation_input_id == reconciliation_input.input_id)
        ).scalar_one()
        if linked_count >= reconciliation_input.item_count:
            raise CommandRejected(
                "VENUE_FACT_INPUT_COUNT_EXCEEDED",
                "normalized venue facts exceed the frozen input item count",
            )
        link = VenueFactInputLink(
            venue_fact_input_link_id=request.venue_fact_input_link_id,
            run_id=run.run_id,
            reconciliation_input_id=reconciliation_input.input_id,
            organization_id=run.organization_id,
            source_type=source_type.value,
            venue_order_observation_id=order_observation_id,
            venue_fill_id=fill_id,
            input_hash=reconciliation_input.input_hash,
            fact_hash=fact_hash,
            raw_payload_ref=request.raw_payload_ref,
            raw_payload_hash=request.raw_payload_hash,
            evidence_ref=request.evidence_ref,
            evidence_hash=request.evidence_hash,
            observed_at=request.venue_observed_at,
            received_at=request.received_at,
            link_hash=link_hash,
            linked_at=now,
        )
        session.add(link)
        session.flush()
        VENUE_FACT_NORMALIZATIONS.labels(
            fact_type, "NEW_FACT" if new_fact else "EXISTING_FACT"
        ).inc()
        VENUE_FACT_INPUT_LINKS.labels(source_type.value, "LINKED").inc()
        return VenueFactNormalizationService._outcome(
            run,
            fact_type,
            order_observation_id,
            fill_id,
            link.venue_fact_input_link_id,
            fact_hash,
            new_fact=new_fact,
            new_link=True,
        )

    @staticmethod
    def _require_new_fact_link_possible(
        session: Session,
        reconciliation_input: ExecutionReconciliationInput,
        request: VenueFactCollectionBinding,
    ) -> None:
        if session.get(VenueFactInputLink, request.venue_fact_input_link_id) is not None:
            raise CommandRejected(
                "VENUE_FACT_INPUT_LINK_ID_CONFLICT", "input link identity already exists"
            )
        linked_count = session.execute(
            select(func.count())
            .select_from(VenueFactInputLink)
            .where(VenueFactInputLink.reconciliation_input_id == reconciliation_input.input_id)
        ).scalar_one()
        if linked_count >= reconciliation_input.item_count:
            raise CommandRejected(
                "VENUE_FACT_INPUT_COUNT_EXCEEDED",
                "normalized venue facts exceed the frozen input item count",
            )

    @staticmethod
    def _outcome(
        run: ExecutionReconciliationRun,
        fact_type: str,
        order_observation_id: UUID | None,
        fill_id: UUID | None,
        link_id: UUID,
        fact_hash: str,
        *,
        new_fact: bool,
        new_link: bool,
    ) -> CommandOutcome:
        fact_id = order_observation_id or fill_id
        assert fact_id is not None
        event_type = (
            "VenueOrderObserved" if fact_type == "ORDER_OBSERVATION" else "VenueFillObserved"
        )
        return CommandOutcome(
            status=CommandStatus.COMPLETED,
            object_type="VenueOrderObservation" if order_observation_id else "VenueFill",
            object_id=str(fact_id),
            object_version=1,
            data={
                "venue_fact_id": str(fact_id),
                "venue_fact_input_link_id": str(link_id),
                "fact_type": fact_type,
                "fact_hash": fact_hash,
                "new_fact": new_fact,
                "new_input_link": new_link,
                "execution_reconciliation_run_id": str(run.run_id),
                "dispatch_eligible": False,
            },
            events=(
                DomainEvent(
                    event_type=event_type,
                    aggregate_type="ExecutionReconciliationRun",
                    aggregate_id=str(run.run_id),
                    payload={
                        "venue_fact_id": str(fact_id),
                        "venue_fact_input_link_id": str(link_id),
                        "fact_type": fact_type,
                        "fact_hash": fact_hash,
                        "new_fact": new_fact,
                        "new_input_link": new_link,
                    },
                ),
            ),
        )
