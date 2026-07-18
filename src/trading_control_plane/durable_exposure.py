from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from trading_control_plane.commands import CommandRejected
from trading_control_plane.execution_models import (
    RiskExposureState,
    RiskReservation,
)

ZERO = Decimal("0")


class DurableExposureComponent(BaseModel):
    model_config = ConfigDict(frozen=True)

    risk_reservation_id: UUID
    order_intent_id: UUID
    campaign_id: UUID
    status: str
    state_version: int = Field(ge=1)
    ledger_sequence: int = Field(ge=1)
    active_ratio: Decimal = Field(ge=0, le=1)
    total_heat: Decimal = Field(gt=0)
    base_heat_reserved: Decimal = Field(gt=0)
    protected_profit_giveback_reserved: Decimal = Field(ge=0)
    cost_stress_add_on_reserved: Decimal = Field(ge=0)
    active_protected_profit_giveback: Decimal = Field(ge=0)
    active_cost_stress_add_on: Decimal = Field(ge=0)
    open_heat: Decimal = Field(ge=0)
    reserved_heat: Decimal = Field(ge=0)
    unknown_heat: Decimal = Field(ge=0)
    funding_used: Decimal = Field(ge=0)
    funding_reserved: Decimal = Field(ge=0)
    funding_unknown: Decimal = Field(ge=0)
    margin_reserved: Decimal = Field(ge=0)
    margin_unknown: Decimal = Field(ge=0)
    last_evidence_ref: str = Field(min_length=1, max_length=255)
    last_evidence_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class DurableScopeExposure(BaseModel):
    model_config = ConfigDict(frozen=True)

    scope_type: str = Field(min_length=1, max_length=80)
    scope_id: str = Field(min_length=1, max_length=255)
    current_planned_loss: Decimal = Field(ge=0)
    current_stress_loss: Decimal = Field(ge=0)

    @property
    def key(self) -> tuple[str, str]:
        return (self.scope_type, self.scope_id)


class DurableExposureSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    organization_id: str = Field(min_length=1, max_length=120)
    campaign_id: UUID | None
    components: tuple[DurableExposureComponent, ...]
    campaign_open_heat: Decimal = Field(ge=0)
    campaign_reserved_heat: Decimal = Field(ge=0)
    campaign_unknown_heat: Decimal = Field(ge=0)
    campaign_protected_profit_giveback: Decimal = Field(ge=0)
    campaign_cost_stress_add_on: Decimal = Field(ge=0)
    global_unknown_heat: Decimal = Field(ge=0)
    global_funding_used: Decimal = Field(ge=0)
    global_funding_reserved: Decimal = Field(ge=0)
    global_funding_unknown: Decimal = Field(ge=0)
    global_margin_reserved: Decimal = Field(ge=0)
    global_margin_unknown: Decimal = Field(ge=0)
    available_margin_after_internal_reservations: Decimal = Field(ge=0)
    scope_exposures: tuple[DurableScopeExposure, ...]
    snapshot_version: str = Field(pattern=r"^durable-risk-exposure-v[0-9]+$")

    @property
    def has_unknown_exposure(self) -> bool:
        return (
            self.global_unknown_heat > ZERO
            or self.global_funding_unknown > ZERO
            or self.global_margin_unknown > ZERO
            or any(component.status == "UNKNOWN" for component in self.components)
        )


class DurableExposureSnapshotService:
    """Locks and aggregates the canonical current projection of the risk ledger."""

    @staticmethod
    def query(
        session: Session,
        *,
        organization_id: str,
        campaign_id: UUID | None,
        scope_keys: tuple[tuple[str, str], ...],
        raw_available_margin: Decimal,
    ) -> DurableExposureSnapshot:
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": f"risk-scope:PORTFOLIO:{organization_id}"},
        )
        rows = tuple(
            session.execute(
                select(RiskReservation, RiskExposureState)
                .join(
                    RiskExposureState,
                    RiskExposureState.risk_reservation_id == RiskReservation.risk_reservation_id,
                )
                .where(RiskReservation.organization_id == organization_id)
                .order_by(RiskReservation.risk_reservation_id)
                .with_for_update(of=RiskExposureState)
            ).all()
        )
        campaign_open = ZERO
        campaign_reserved = ZERO
        campaign_unknown = ZERO
        campaign_protected_profit_giveback = ZERO
        campaign_cost_stress_add_on = ZERO
        global_unknown = ZERO
        global_funding_used = ZERO
        global_funding_reserved = ZERO
        global_funding_unknown = ZERO
        global_margin_reserved = ZERO
        global_margin_unknown = ZERO
        scope_planned: defaultdict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
        scope_stress: defaultdict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
        components: list[DurableExposureComponent] = []
        for reservation, state in rows:
            if state.total_heat != reservation.reserved_heat:
                raise CommandRejected(
                    "DURABLE_EXPOSURE_INTEGRITY_FAILED",
                    "reservation and exposure total Heat differ",
                )
            active_heat = state.open_heat + state.reserved_heat + state.unknown_heat
            active_ratio = active_heat / state.total_heat if active_heat > ZERO else ZERO
            base_heat_ratio = reservation.base_heat_reserved / reservation.reserved_heat
            active_protected_profit_giveback = (
                reservation.protected_profit_giveback_reserved * active_ratio
            )
            active_cost_stress_add_on = reservation.cost_stress_add_on_reserved * active_ratio
            components.append(
                DurableExposureComponent(
                    risk_reservation_id=reservation.risk_reservation_id,
                    order_intent_id=reservation.order_intent_id,
                    campaign_id=reservation.campaign_id,
                    status=state.status,
                    state_version=state.version,
                    ledger_sequence=state.ledger_sequence,
                    active_ratio=active_ratio,
                    total_heat=state.total_heat,
                    base_heat_reserved=reservation.base_heat_reserved,
                    protected_profit_giveback_reserved=(
                        reservation.protected_profit_giveback_reserved
                    ),
                    cost_stress_add_on_reserved=reservation.cost_stress_add_on_reserved,
                    active_protected_profit_giveback=active_protected_profit_giveback,
                    active_cost_stress_add_on=active_cost_stress_add_on,
                    open_heat=state.open_heat,
                    reserved_heat=state.reserved_heat,
                    unknown_heat=state.unknown_heat,
                    funding_used=state.funding_used,
                    funding_reserved=state.funding_reserved,
                    funding_unknown=state.funding_unknown,
                    margin_reserved=state.margin_reserved,
                    margin_unknown=state.margin_unknown,
                    last_evidence_ref=state.last_evidence_ref,
                    last_evidence_hash=state.last_evidence_hash,
                )
            )
            if campaign_id is not None and reservation.campaign_id == campaign_id:
                campaign_open += state.open_heat * base_heat_ratio
                campaign_reserved += state.reserved_heat * base_heat_ratio
                campaign_unknown += state.unknown_heat * base_heat_ratio
                campaign_protected_profit_giveback += active_protected_profit_giveback
                campaign_cost_stress_add_on += active_cost_stress_add_on
            global_unknown += state.unknown_heat
            global_funding_used += state.funding_used
            global_funding_reserved += state.funding_reserved
            global_funding_unknown += state.funding_unknown
            global_margin_reserved += state.margin_reserved
            global_margin_unknown += state.margin_unknown
            if active_heat == ZERO:
                continue
            for allocation in reservation.scope_allocations:
                key = (str(allocation["scope_type"]), str(allocation["scope_id"]))
                scope_planned[key] += Decimal(str(allocation["planned_loss"])) * active_ratio
                scope_stress[key] += Decimal(str(allocation["stress_loss"])) * active_ratio

        remaining_margin = max(
            ZERO,
            raw_available_margin - global_margin_reserved - global_margin_unknown,
        )
        scope_exposures = tuple(
            DurableScopeExposure(
                scope_type=scope_type,
                scope_id=scope_id,
                current_planned_loss=scope_planned[scope_type, scope_id],
                current_stress_loss=scope_stress[scope_type, scope_id],
            )
            for scope_type, scope_id in sorted(scope_keys)
        )
        return DurableExposureSnapshot(
            organization_id=organization_id,
            campaign_id=campaign_id,
            components=tuple(components),
            campaign_open_heat=campaign_open,
            campaign_reserved_heat=campaign_reserved,
            campaign_unknown_heat=campaign_unknown,
            campaign_protected_profit_giveback=campaign_protected_profit_giveback,
            campaign_cost_stress_add_on=campaign_cost_stress_add_on,
            global_unknown_heat=global_unknown,
            global_funding_used=global_funding_used,
            global_funding_reserved=global_funding_reserved,
            global_funding_unknown=global_funding_unknown,
            global_margin_reserved=global_margin_reserved,
            global_margin_unknown=global_margin_unknown,
            available_margin_after_internal_reservations=remaining_margin,
            scope_exposures=scope_exposures,
            snapshot_version="durable-risk-exposure-v2",
        )
