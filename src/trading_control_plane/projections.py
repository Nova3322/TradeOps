from __future__ import annotations

from datetime import datetime, timedelta
from decimal import ROUND_CEILING, Decimal, localcontext
from enum import StrEnum
from typing import Any, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from trading_control_plane.commands import hash_json
from trading_control_plane.metrics import (
    VENUE_CURRENT_PROJECTION_AGE,
    VENUE_CURRENT_PROJECTION_QUERIES,
)

PROJECTION_VERSION = "venue-current-v1"
PROTECTED_POSITION_RISK_VERSION = "protected-position-risk-v2"
RISK_AMOUNT_QUANTUM = Decimal("0.000000000000000001")
ZERO = Decimal("0")


class ProjectionState(StrEnum):
    CONFIRMED = "CONFIRMED"
    UNKNOWN = "UNKNOWN"


class ProjectionFreshness(StrEnum):
    FRESH = "FRESH"
    STALE = "STALE"
    MISSING = "MISSING"
    UNKNOWN = "UNKNOWN"


class ProjectionMaturity(StrEnum):
    VENUE_CONFIRMED = "VENUE_CONFIRMED"
    UNKNOWN = "UNKNOWN"


class CurrentPositionState(StrEnum):
    OPEN = "OPEN"
    FLAT = "FLAT"
    UNKNOWN = "UNKNOWN"


class CurrentPositionDirection(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"
    UNKNOWN = "UNKNOWN"


class CurrentProtectionState(StrEnum):
    CONFIRMED = "CONFIRMED"
    UNKNOWN = "UNKNOWN"


class CurrentProtectionDirection(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    UNKNOWN = "UNKNOWN"


class ProjectionQueryContext(BaseModel):
    """Explicit query time and certified freshness limit; there is no runtime default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    as_of: datetime
    max_age_ms: int = Field(gt=0)

    @model_validator(mode="after")
    def as_of_is_timezone_aware(self) -> Self:
        if self.as_of.tzinfo is None or self.as_of.utcoffset() is None:
            raise ValueError("projection as_of must be timezone-aware")
        return self


class CurrentPositionScope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    organization_id: str = Field(min_length=1, max_length=120)
    venue: str = Field(min_length=1, max_length=80)
    execution_domain: str = Field(min_length=1, max_length=120)
    account_id: str = Field(min_length=1, max_length=160)
    instrument_id: str = Field(min_length=1, max_length=255)
    position_mode: str = Field(min_length=1, max_length=80)
    position_side: str = Field(min_length=1, max_length=20)
    margin_mode: str = Field(min_length=1, max_length=80)
    collateral_pool_id: str = Field(min_length=1, max_length=160)
    settlement_currency: str = Field(min_length=1, max_length=80)


class CurrentAccountEquityScope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    organization_id: str = Field(min_length=1, max_length=120)
    venue: str = Field(min_length=1, max_length=80)
    execution_domain: str = Field(min_length=1, max_length=120)
    account_id: str = Field(min_length=1, max_length=160)
    margin_mode: str = Field(min_length=1, max_length=80)
    collateral_pool_id: str = Field(min_length=1, max_length=160)
    settlement_currency: str = Field(min_length=1, max_length=80)


class CurrentProtectionScope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    organization_id: str = Field(min_length=1, max_length=120)
    venue: str = Field(min_length=1, max_length=80)
    execution_domain: str = Field(min_length=1, max_length=120)
    account_id: str = Field(min_length=1, max_length=160)
    instrument_id: str = Field(min_length=1, max_length=255)
    position_mode: str = Field(min_length=1, max_length=80)
    position_side: str = Field(min_length=1, max_length=20)
    margin_mode: str = Field(min_length=1, max_length=80)
    collateral_pool_id: str = Field(min_length=1, max_length=160)
    settlement_currency: str = Field(min_length=1, max_length=80)


class CurrentPositionProjection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: CurrentPositionScope
    projection_state: ProjectionState
    freshness: ProjectionFreshness
    maturity: ProjectionMaturity
    reason_code: str | None
    source_snapshot_id: UUID | None
    source_snapshot_hash: str | None
    source_version: str | None
    normalization_version: str | None
    position_state: CurrentPositionState
    direction: CurrentPositionDirection
    quantity: Decimal | None
    entry_price: Decimal | None
    mark_price: Decimal | None
    contract_multiplier: Decimal | None
    notional: Decimal | None
    unrealized_pnl: Decimal | None
    liquidation_price: Decimal | None
    leverage: Decimal | None
    initial_margin: Decimal | None
    maintenance_margin: Decimal | None
    facts_as_of: datetime | None
    venue_observed_at: datetime | None
    received_at: datetime | None
    age_ms: int | None = Field(ge=0)
    max_event_candidate_count: int = Field(ge=0)
    projection_version: str = Field(pattern=r"^venue-current-v[0-9]+$")

    @model_validator(mode="after")
    def unknown_never_exposes_position_economics(self) -> Self:
        economics = (
            self.quantity,
            self.entry_price,
            self.mark_price,
            self.contract_multiplier,
            self.notional,
            self.unrealized_pnl,
            self.liquidation_price,
            self.leverage,
            self.initial_margin,
            self.maintenance_margin,
        )
        if self.projection_state is ProjectionState.UNKNOWN:
            if (
                self.position_state is not CurrentPositionState.UNKNOWN
                or self.direction is not CurrentPositionDirection.UNKNOWN
                or any(value is not None for value in economics)
            ):
                raise ValueError("unknown position projection cannot expose economics")
        elif (
            self.position_state is CurrentPositionState.UNKNOWN
            or self.direction is CurrentPositionDirection.UNKNOWN
            or self.source_snapshot_id is None
            or self.source_snapshot_hash is None
            or self.source_version is None
            or self.normalization_version is None
            or self.freshness is not ProjectionFreshness.FRESH
            or self.maturity is not ProjectionMaturity.VENUE_CONFIRMED
        ):
            raise ValueError("confirmed position projection lacks usable source semantics")
        return self


class CurrentAccountEquityProjection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: CurrentAccountEquityScope
    projection_state: ProjectionState
    freshness: ProjectionFreshness
    maturity: ProjectionMaturity
    reason_code: str | None
    source_snapshot_id: UUID | None
    source_snapshot_hash: str | None
    source_version: str | None
    normalization_version: str | None
    wallet_balance: Decimal | None
    exchange_margin_equity: Decimal | None
    available_margin: Decimal | None
    total_unrealized_pnl: Decimal | None
    total_initial_margin: Decimal | None
    total_maintenance_margin: Decimal | None
    total_liability: Decimal | None
    unsettled_fee: Decimal | None
    unsettled_funding: Decimal | None
    includes_unrealized_pnl: bool
    facts_as_of: datetime | None
    venue_observed_at: datetime | None
    received_at: datetime | None
    age_ms: int | None = Field(ge=0)
    max_event_candidate_count: int = Field(ge=0)
    projection_version: str = Field(pattern=r"^venue-current-v[0-9]+$")

    @model_validator(mode="after")
    def unknown_never_exposes_account_economics(self) -> Self:
        economics = (
            self.wallet_balance,
            self.exchange_margin_equity,
            self.available_margin,
            self.total_unrealized_pnl,
            self.total_initial_margin,
            self.total_maintenance_margin,
            self.total_liability,
            self.unsettled_fee,
            self.unsettled_funding,
        )
        if self.projection_state is ProjectionState.UNKNOWN:
            if any(value is not None for value in economics) or self.includes_unrealized_pnl:
                raise ValueError("unknown account projection cannot expose economics")
        elif (
            any(value is None for value in economics)
            or not self.includes_unrealized_pnl
            or self.source_snapshot_id is None
            or self.source_snapshot_hash is None
            or self.source_version is None
            or self.normalization_version is None
            or self.freshness is not ProjectionFreshness.FRESH
            or self.maturity is not ProjectionMaturity.VENUE_CONFIRMED
        ):
            raise ValueError("confirmed account projection lacks usable source semantics")
        return self


class CurrentProtectionProjection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: CurrentProtectionScope
    projection_state: ProjectionState
    freshness: ProjectionFreshness
    maturity: ProjectionMaturity
    reason_code: str | None
    source_snapshot_id: UUID | None
    source_snapshot_hash: str | None
    source_version: str | None
    normalization_version: str | None
    source_position_snapshot_id: UUID | None
    protection_state: CurrentProtectionState
    protected_direction: CurrentProtectionDirection
    position_quantity: Decimal | None
    covered_quantity: Decimal | None
    uncovered_quantity: Decimal | None
    active_stop_order_count: int | None
    worst_active_trigger_price: Decimal | None
    venue_native: bool
    reduce_only_confirmed: bool
    replacement_in_progress: bool
    order_set_hash: str | None
    facts_as_of: datetime | None
    venue_observed_at: datetime | None
    received_at: datetime | None
    age_ms: int | None = Field(ge=0)
    max_event_candidate_count: int = Field(ge=0)
    projection_version: str = Field(pattern=r"^venue-current-v[0-9]+$")

    @model_validator(mode="after")
    def unknown_never_exposes_protection_semantics(self) -> Self:
        economics = (
            self.position_quantity,
            self.covered_quantity,
            self.uncovered_quantity,
            self.active_stop_order_count,
            self.worst_active_trigger_price,
        )
        if self.projection_state is ProjectionState.UNKNOWN:
            if (
                self.source_position_snapshot_id is not None
                or self.protection_state is not CurrentProtectionState.UNKNOWN
                or self.protected_direction is not CurrentProtectionDirection.UNKNOWN
                or any(value is not None for value in economics)
                or self.venue_native
                or self.reduce_only_confirmed
                or self.replacement_in_progress
                or self.order_set_hash is not None
            ):
                raise ValueError("unknown protection projection cannot expose semantics")
        elif (
            self.protection_state is not CurrentProtectionState.CONFIRMED
            or self.protected_direction
            not in {CurrentProtectionDirection.LONG, CurrentProtectionDirection.SHORT}
            or self.source_snapshot_id is None
            or self.source_snapshot_hash is None
            or self.source_version is None
            or self.normalization_version is None
            or self.source_position_snapshot_id is None
            or any(value is None for value in economics)
            or self.position_quantity is None
            or self.position_quantity <= 0
            or self.position_quantity != self.covered_quantity
            or self.uncovered_quantity != 0
            or self.active_stop_order_count is None
            or self.active_stop_order_count < 1
            or self.worst_active_trigger_price is None
            or self.worst_active_trigger_price <= 0
            or not self.venue_native
            or not self.reduce_only_confirmed
            or self.replacement_in_progress
            or self.order_set_hash is None
            or self.freshness is not ProjectionFreshness.FRESH
            or self.maturity is not ProjectionMaturity.VENUE_CONFIRMED
        ):
            raise ValueError("confirmed protection projection lacks usable source semantics")
        return self


class CurrentProtectedPositionRiskProjection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: CurrentProtectionScope
    projection_state: ProjectionState
    reason_code: str | None
    direction: CurrentProtectionDirection
    position_snapshot_id: UUID | None
    position_snapshot_hash: str | None
    protection_snapshot_id: UUID | None
    protection_snapshot_hash: str | None
    quantity: Decimal | None
    entry_price: Decimal | None
    mark_price: Decimal | None
    unrealized_pnl: Decimal | None
    protection_trigger_price: Decimal | None
    contract_multiplier: Decimal | None
    current_to_protection_loss: Decimal | None
    open_heat: Decimal | None
    protected_profit_giveback: Decimal | None
    facts_as_of: datetime | None
    calculation_version: str = Field(pattern=r"^protected-position-risk-v[0-9]+$")
    calculation_hash: str | None

    @model_validator(mode="after")
    def loss_components_are_complete_or_hidden(self) -> Self:
        economics = (
            self.quantity,
            self.entry_price,
            self.mark_price,
            self.unrealized_pnl,
            self.protection_trigger_price,
            self.contract_multiplier,
            self.current_to_protection_loss,
            self.open_heat,
            self.protected_profit_giveback,
        )
        sources = (
            self.position_snapshot_id,
            self.position_snapshot_hash,
            self.protection_snapshot_id,
            self.protection_snapshot_hash,
        )
        if self.projection_state is ProjectionState.UNKNOWN:
            if (
                self.reason_code is None
                or self.direction is not CurrentProtectionDirection.UNKNOWN
                or any(value is not None for value in economics)
                or any(value is not None for value in sources)
                or self.facts_as_of is not None
                or self.calculation_hash is not None
            ):
                raise ValueError("unknown protected-position risk cannot expose economics")
        elif (
            self.reason_code is not None
            or self.direction
            not in {CurrentProtectionDirection.LONG, CurrentProtectionDirection.SHORT}
            or any(value is None for value in economics)
            or any(value is None for value in sources)
            or self.facts_as_of is None
            or self.calculation_hash is None
            or self.current_to_protection_loss is None
            or self.open_heat is None
            or self.protected_profit_giveback is None
            or self.current_to_protection_loss < ZERO
            or self.open_heat < ZERO
            or self.protected_profit_giveback < ZERO
            or self.current_to_protection_loss != self.open_heat + self.protected_profit_giveback
            or self.calculation_hash != hash_json(_protected_position_risk_contract(self))
        ):
            raise ValueError("confirmed protected-position risk is inconsistent")
        return self


class VenueCurrentProjectionService:
    """Reads deterministic SQL views; it has no business-command or write path."""

    @staticmethod
    def current_position(
        session: Session,
        scope: CurrentPositionScope,
        context: ProjectionQueryContext,
    ) -> CurrentPositionProjection:
        row = (
            session.execute(
                text(
                    """
                SELECT *
                FROM venue_position_current_projection
                WHERE organization_id = :organization_id
                  AND venue = :venue
                  AND execution_domain = :execution_domain
                  AND account_id = :account_id
                  AND instrument_id = :instrument_id
                  AND position_mode = :position_mode
                  AND position_side = :position_side
                  AND margin_mode = :margin_mode
                  AND collateral_pool_id = :collateral_pool_id
                  AND settlement_currency = :settlement_currency
                """
                ),
                scope.model_dump(),
            )
            .mappings()
            .one_or_none()
        )
        values = dict(row) if row is not None else None
        status = _projection_status(values, context)
        if values is None:
            result = CurrentPositionProjection(
                scope=scope,
                projection_state=ProjectionState.UNKNOWN,
                freshness=ProjectionFreshness.MISSING,
                maturity=ProjectionMaturity.UNKNOWN,
                reason_code="SOURCE_MISSING",
                source_snapshot_id=None,
                source_snapshot_hash=None,
                source_version=None,
                normalization_version=None,
                position_state=CurrentPositionState.UNKNOWN,
                direction=CurrentPositionDirection.UNKNOWN,
                quantity=None,
                entry_price=None,
                mark_price=None,
                contract_multiplier=None,
                notional=None,
                unrealized_pnl=None,
                liquidation_price=None,
                leverage=None,
                initial_margin=None,
                maintenance_margin=None,
                facts_as_of=None,
                venue_observed_at=None,
                received_at=None,
                age_ms=None,
                max_event_candidate_count=0,
                projection_version=PROJECTION_VERSION,
            )
        else:
            usable = status.state is ProjectionState.CONFIRMED
            result = CurrentPositionProjection(
                scope=scope,
                projection_state=status.state,
                freshness=status.freshness,
                maturity=ProjectionMaturity(values["maturity"]),
                reason_code=status.reason_code,
                source_snapshot_id=values["source_snapshot_id"],
                source_snapshot_hash=values["source_snapshot_hash"],
                source_version=values["source_version"],
                normalization_version=values["normalization_version"],
                position_state=CurrentPositionState(values["position_state"])
                if usable
                else CurrentPositionState.UNKNOWN,
                direction=CurrentPositionDirection(values["direction"])
                if usable
                else CurrentPositionDirection.UNKNOWN,
                quantity=values["quantity"] if usable else None,
                entry_price=values["entry_price"] if usable else None,
                mark_price=values["mark_price"] if usable else None,
                contract_multiplier=values["contract_multiplier"] if usable else None,
                notional=values["notional"] if usable else None,
                unrealized_pnl=values["unrealized_pnl"] if usable else None,
                liquidation_price=values["liquidation_price"] if usable else None,
                leverage=values["leverage"] if usable else None,
                initial_margin=values["initial_margin"] if usable else None,
                maintenance_margin=values["maintenance_margin"] if usable else None,
                facts_as_of=values["facts_as_of"],
                venue_observed_at=values["venue_observed_at"],
                received_at=values["received_at"],
                age_ms=status.age_ms,
                max_event_candidate_count=values["max_event_candidate_count"],
                projection_version=values["projection_version"],
            )
        _observe("POSITION", result.projection_state, result.freshness, result.age_ms)
        return result

    @staticmethod
    def current_account_equity(
        session: Session,
        scope: CurrentAccountEquityScope,
        context: ProjectionQueryContext,
    ) -> CurrentAccountEquityProjection:
        row = (
            session.execute(
                text(
                    """
                SELECT *
                FROM venue_account_equity_current_projection
                WHERE organization_id = :organization_id
                  AND venue = :venue
                  AND execution_domain = :execution_domain
                  AND account_id = :account_id
                  AND margin_mode = :margin_mode
                  AND collateral_pool_id = :collateral_pool_id
                  AND settlement_currency = :settlement_currency
                """
                ),
                scope.model_dump(),
            )
            .mappings()
            .one_or_none()
        )
        values = dict(row) if row is not None else None
        status = _projection_status(values, context)
        if values is None:
            result = CurrentAccountEquityProjection(
                scope=scope,
                projection_state=ProjectionState.UNKNOWN,
                freshness=ProjectionFreshness.MISSING,
                maturity=ProjectionMaturity.UNKNOWN,
                reason_code="SOURCE_MISSING",
                source_snapshot_id=None,
                source_snapshot_hash=None,
                source_version=None,
                normalization_version=None,
                wallet_balance=None,
                exchange_margin_equity=None,
                available_margin=None,
                total_unrealized_pnl=None,
                total_initial_margin=None,
                total_maintenance_margin=None,
                total_liability=None,
                unsettled_fee=None,
                unsettled_funding=None,
                includes_unrealized_pnl=False,
                facts_as_of=None,
                venue_observed_at=None,
                received_at=None,
                age_ms=None,
                max_event_candidate_count=0,
                projection_version=PROJECTION_VERSION,
            )
        else:
            usable = status.state is ProjectionState.CONFIRMED
            result = CurrentAccountEquityProjection(
                scope=scope,
                projection_state=status.state,
                freshness=status.freshness,
                maturity=ProjectionMaturity(values["maturity"]),
                reason_code=status.reason_code,
                source_snapshot_id=values["source_snapshot_id"],
                source_snapshot_hash=values["source_snapshot_hash"],
                source_version=values["source_version"],
                normalization_version=values["normalization_version"],
                wallet_balance=values["wallet_balance"] if usable else None,
                exchange_margin_equity=values["exchange_margin_equity"] if usable else None,
                available_margin=values["available_margin"] if usable else None,
                total_unrealized_pnl=values["total_unrealized_pnl"] if usable else None,
                total_initial_margin=values["total_initial_margin"] if usable else None,
                total_maintenance_margin=values["total_maintenance_margin"] if usable else None,
                total_liability=values["total_liability"] if usable else None,
                unsettled_fee=values["unsettled_fee"] if usable else None,
                unsettled_funding=values["unsettled_funding"] if usable else None,
                includes_unrealized_pnl=bool(values["includes_unrealized_pnl"])
                if usable
                else False,
                facts_as_of=values["facts_as_of"],
                venue_observed_at=values["venue_observed_at"],
                received_at=values["received_at"],
                age_ms=status.age_ms,
                max_event_candidate_count=values["max_event_candidate_count"],
                projection_version=values["projection_version"],
            )
        _observe("ACCOUNT_EQUITY", result.projection_state, result.freshness, result.age_ms)
        return result

    @staticmethod
    def current_protection(
        session: Session,
        scope: CurrentProtectionScope,
        context: ProjectionQueryContext,
    ) -> CurrentProtectionProjection:
        row = (
            session.execute(
                text(
                    """
                SELECT *
                FROM venue_protection_current_projection
                WHERE organization_id = :organization_id
                  AND venue = :venue
                  AND execution_domain = :execution_domain
                  AND account_id = :account_id
                  AND instrument_id = :instrument_id
                  AND position_mode = :position_mode
                  AND position_side = :position_side
                  AND margin_mode = :margin_mode
                  AND collateral_pool_id = :collateral_pool_id
                  AND settlement_currency = :settlement_currency
                """
                ),
                scope.model_dump(),
            )
            .mappings()
            .one_or_none()
        )
        values = dict(row) if row is not None else None
        status = _projection_status(values, context)
        if values is None:
            result = CurrentProtectionProjection(
                scope=scope,
                projection_state=ProjectionState.UNKNOWN,
                freshness=ProjectionFreshness.MISSING,
                maturity=ProjectionMaturity.UNKNOWN,
                reason_code="SOURCE_MISSING",
                source_snapshot_id=None,
                source_snapshot_hash=None,
                source_version=None,
                normalization_version=None,
                source_position_snapshot_id=None,
                protection_state=CurrentProtectionState.UNKNOWN,
                protected_direction=CurrentProtectionDirection.UNKNOWN,
                position_quantity=None,
                covered_quantity=None,
                uncovered_quantity=None,
                active_stop_order_count=None,
                worst_active_trigger_price=None,
                venue_native=False,
                reduce_only_confirmed=False,
                replacement_in_progress=False,
                order_set_hash=None,
                facts_as_of=None,
                venue_observed_at=None,
                received_at=None,
                age_ms=None,
                max_event_candidate_count=0,
                projection_version=PROJECTION_VERSION,
            )
        else:
            usable = status.state is ProjectionState.CONFIRMED
            result = CurrentProtectionProjection(
                scope=scope,
                projection_state=status.state,
                freshness=status.freshness,
                maturity=ProjectionMaturity(values["maturity"]),
                reason_code=status.reason_code,
                source_snapshot_id=values["source_snapshot_id"],
                source_snapshot_hash=values["source_snapshot_hash"],
                source_version=values["source_version"],
                normalization_version=values["normalization_version"],
                source_position_snapshot_id=(
                    values["source_position_snapshot_id"] if usable else None
                ),
                protection_state=(
                    CurrentProtectionState(values["protection_state"])
                    if usable
                    else CurrentProtectionState.UNKNOWN
                ),
                protected_direction=(
                    CurrentProtectionDirection(values["protected_direction"])
                    if usable
                    else CurrentProtectionDirection.UNKNOWN
                ),
                position_quantity=values["position_quantity"] if usable else None,
                covered_quantity=values["covered_quantity"] if usable else None,
                uncovered_quantity=values["uncovered_quantity"] if usable else None,
                active_stop_order_count=(values["active_stop_order_count"] if usable else None),
                worst_active_trigger_price=(
                    values["worst_active_trigger_price"] if usable else None
                ),
                venue_native=bool(values["venue_native"]) if usable else False,
                reduce_only_confirmed=(bool(values["reduce_only_confirmed"]) if usable else False),
                replacement_in_progress=(
                    bool(values["replacement_in_progress"]) if usable else False
                ),
                order_set_hash=values["order_set_hash"] if usable else None,
                facts_as_of=values["facts_as_of"],
                venue_observed_at=values["venue_observed_at"],
                received_at=values["received_at"],
                age_ms=status.age_ms,
                max_event_candidate_count=values["max_event_candidate_count"],
                projection_version=values["projection_version"],
            )
        _observe("PROTECTION", result.projection_state, result.freshness, result.age_ms)
        return result

    @staticmethod
    def current_protected_position_risk(
        session: Session,
        scope: CurrentProtectionScope,
        context: ProjectionQueryContext,
    ) -> CurrentProtectedPositionRiskProjection:
        position = VenueCurrentProjectionService.current_position(
            session,
            CurrentPositionScope.model_validate(scope.model_dump()),
            context,
        )
        if position.projection_state is ProjectionState.UNKNOWN:
            return _unknown_protected_position_risk(
                scope,
                f"POSITION_{position.reason_code or 'UNKNOWN'}",
            )
        protection = VenueCurrentProjectionService.current_protection(
            session,
            scope,
            context,
        )
        if protection.projection_state is ProjectionState.UNKNOWN:
            return _unknown_protected_position_risk(
                scope,
                f"PROTECTION_{protection.reason_code or 'UNKNOWN'}",
            )
        return derive_protected_position_risk(position, protection)


class _ProjectionStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    state: ProjectionState
    freshness: ProjectionFreshness
    reason_code: str | None
    age_ms: int | None


def _projection_status(
    values: dict[str, Any] | None,
    context: ProjectionQueryContext,
) -> _ProjectionStatus:
    if values is None:
        return _ProjectionStatus(
            state=ProjectionState.UNKNOWN,
            freshness=ProjectionFreshness.MISSING,
            reason_code="SOURCE_MISSING",
            age_ms=None,
        )
    facts_as_of = values["facts_as_of"]
    assert isinstance(facts_as_of, datetime)
    age = context.as_of - facts_as_of
    age_ms = max(0, int(age.total_seconds() * 1000))
    if values["projection_state"] != ProjectionState.CONFIRMED.value:
        return _ProjectionStatus(
            state=ProjectionState.UNKNOWN,
            freshness=ProjectionFreshness.UNKNOWN,
            reason_code=str(values["reason_code"]),
            age_ms=age_ms,
        )
    if age < timedelta(0):
        return _ProjectionStatus(
            state=ProjectionState.UNKNOWN,
            freshness=ProjectionFreshness.UNKNOWN,
            reason_code="SOURCE_FROM_FUTURE",
            age_ms=0,
        )
    if age > timedelta(milliseconds=context.max_age_ms):
        return _ProjectionStatus(
            state=ProjectionState.UNKNOWN,
            freshness=ProjectionFreshness.STALE,
            reason_code="SOURCE_STALE",
            age_ms=age_ms,
        )
    return _ProjectionStatus(
        state=ProjectionState.CONFIRMED,
        freshness=ProjectionFreshness.FRESH,
        reason_code=None,
        age_ms=age_ms,
    )


def _observe(
    projection_type: str,
    state: ProjectionState,
    freshness: ProjectionFreshness,
    age_ms: int | None,
) -> None:
    VENUE_CURRENT_PROJECTION_QUERIES.labels(
        projection_type,
        state.value,
        freshness.value,
    ).inc()
    if age_ms is not None:
        VENUE_CURRENT_PROJECTION_AGE.labels(projection_type).observe(age_ms / 1000)


def derive_protected_position_risk(
    position: CurrentPositionProjection,
    protection: CurrentProtectionProjection,
) -> CurrentProtectedPositionRiskProjection:
    scope = protection.scope
    if position.scope.model_dump() != scope.model_dump():
        return _unknown_protected_position_risk(scope, "SCOPE_MISMATCH")
    if position.projection_state is not ProjectionState.CONFIRMED:
        return _unknown_protected_position_risk(scope, "POSITION_UNKNOWN")
    if protection.projection_state is not ProjectionState.CONFIRMED:
        return _unknown_protected_position_risk(scope, "PROTECTION_UNKNOWN")
    if position.position_state is not CurrentPositionState.OPEN:
        return _unknown_protected_position_risk(scope, "POSITION_NOT_OPEN")
    if protection.source_position_snapshot_id != position.source_snapshot_id:
        return _unknown_protected_position_risk(scope, "SOURCE_BINDING_MISMATCH")
    if position.direction.value != protection.protected_direction.value:
        return _unknown_protected_position_risk(scope, "DIRECTION_MISMATCH")
    if position.quantity != protection.position_quantity:
        return _unknown_protected_position_risk(scope, "QUANTITY_MISMATCH")
    inputs = (
        position.source_snapshot_id,
        position.source_snapshot_hash,
        protection.source_snapshot_id,
        protection.source_snapshot_hash,
        position.quantity,
        position.entry_price,
        position.mark_price,
        position.unrealized_pnl,
        protection.worst_active_trigger_price,
        position.contract_multiplier,
        position.facts_as_of,
        protection.facts_as_of,
    )
    if any(value is None for value in inputs):
        return _unknown_protected_position_risk(scope, "ECONOMICS_MISSING")
    assert position.quantity is not None
    assert position.entry_price is not None
    assert position.mark_price is not None
    assert position.unrealized_pnl is not None
    assert protection.worst_active_trigger_price is not None
    assert position.contract_multiplier is not None
    assert position.source_snapshot_id is not None
    assert position.source_snapshot_hash is not None
    assert protection.source_snapshot_id is not None
    assert protection.source_snapshot_hash is not None
    assert position.facts_as_of is not None
    assert protection.facts_as_of is not None
    if (
        position.quantity <= ZERO
        or position.entry_price <= ZERO
        or position.mark_price <= ZERO
        or protection.worst_active_trigger_price <= ZERO
        or position.contract_multiplier <= ZERO
    ):
        return _unknown_protected_position_risk(scope, "ECONOMICS_INVALID")

    quantity_multiplier = position.quantity * position.contract_multiplier
    if position.direction is CurrentPositionDirection.LONG:
        if protection.worst_active_trigger_price >= position.mark_price:
            return _unknown_protected_position_risk(scope, "TRIGGER_SIDE_INVALID")
        raw_total = (
            position.mark_price - protection.worst_active_trigger_price
        ) * quantity_multiplier
        raw_open = (
            max(
                ZERO,
                min(position.entry_price, position.mark_price)
                - protection.worst_active_trigger_price,
            )
            * quantity_multiplier
        )
        direction = CurrentProtectionDirection.LONG
    elif position.direction is CurrentPositionDirection.SHORT:
        if protection.worst_active_trigger_price <= position.mark_price:
            return _unknown_protected_position_risk(scope, "TRIGGER_SIDE_INVALID")
        raw_total = (
            protection.worst_active_trigger_price - position.mark_price
        ) * quantity_multiplier
        raw_open = (
            max(
                ZERO,
                protection.worst_active_trigger_price
                - max(position.entry_price, position.mark_price),
            )
            * quantity_multiplier
        )
        direction = CurrentProtectionDirection.SHORT
    else:
        return _unknown_protected_position_risk(scope, "DIRECTION_INVALID")

    total = _quantize_risk_amount(raw_total)
    open_heat = min(total, _quantize_risk_amount(raw_open))
    giveback = total - open_heat
    result = CurrentProtectedPositionRiskProjection.model_construct(
        scope=scope,
        projection_state=ProjectionState.CONFIRMED,
        reason_code=None,
        direction=direction,
        position_snapshot_id=position.source_snapshot_id,
        position_snapshot_hash=position.source_snapshot_hash,
        protection_snapshot_id=protection.source_snapshot_id,
        protection_snapshot_hash=protection.source_snapshot_hash,
        quantity=position.quantity,
        entry_price=position.entry_price,
        mark_price=position.mark_price,
        unrealized_pnl=position.unrealized_pnl,
        protection_trigger_price=protection.worst_active_trigger_price,
        contract_multiplier=position.contract_multiplier,
        current_to_protection_loss=total,
        open_heat=open_heat,
        protected_profit_giveback=giveback,
        facts_as_of=min(position.facts_as_of, protection.facts_as_of),
        calculation_version=PROTECTED_POSITION_RISK_VERSION,
        calculation_hash=None,
    )
    calculation_hash = hash_json(_protected_position_risk_contract(result))
    return CurrentProtectedPositionRiskProjection.model_validate(
        {**result.model_dump(mode="python"), "calculation_hash": calculation_hash}
    )


def _unknown_protected_position_risk(
    scope: CurrentProtectionScope,
    reason_code: str,
) -> CurrentProtectedPositionRiskProjection:
    return CurrentProtectedPositionRiskProjection(
        scope=scope,
        projection_state=ProjectionState.UNKNOWN,
        reason_code=reason_code,
        direction=CurrentProtectionDirection.UNKNOWN,
        position_snapshot_id=None,
        position_snapshot_hash=None,
        protection_snapshot_id=None,
        protection_snapshot_hash=None,
        quantity=None,
        entry_price=None,
        mark_price=None,
        unrealized_pnl=None,
        protection_trigger_price=None,
        contract_multiplier=None,
        current_to_protection_loss=None,
        open_heat=None,
        protected_profit_giveback=None,
        facts_as_of=None,
        calculation_version=PROTECTED_POSITION_RISK_VERSION,
        calculation_hash=None,
    )


def _quantize_risk_amount(value: Decimal) -> Decimal:
    if value <= ZERO:
        return ZERO
    with localcontext() as context:
        context.prec = 80
        return value.quantize(RISK_AMOUNT_QUANTUM, rounding=ROUND_CEILING)


def _protected_position_risk_contract(
    projection: CurrentProtectedPositionRiskProjection,
) -> dict[str, str]:
    assert projection.position_snapshot_id is not None
    assert projection.position_snapshot_hash is not None
    assert projection.protection_snapshot_id is not None
    assert projection.protection_snapshot_hash is not None
    assert projection.quantity is not None
    assert projection.entry_price is not None
    assert projection.mark_price is not None
    assert projection.unrealized_pnl is not None
    assert projection.protection_trigger_price is not None
    assert projection.contract_multiplier is not None
    assert projection.current_to_protection_loss is not None
    assert projection.open_heat is not None
    assert projection.protected_profit_giveback is not None
    assert projection.facts_as_of is not None
    return {
        "scope": hash_json(projection.scope.model_dump(mode="json")),
        "direction": projection.direction.value,
        "position_snapshot_id": str(projection.position_snapshot_id),
        "position_snapshot_hash": projection.position_snapshot_hash,
        "protection_snapshot_id": str(projection.protection_snapshot_id),
        "protection_snapshot_hash": projection.protection_snapshot_hash,
        "quantity": str(projection.quantity),
        "entry_price": str(projection.entry_price),
        "mark_price": str(projection.mark_price),
        "unrealized_pnl": str(projection.unrealized_pnl),
        "protection_trigger_price": str(projection.protection_trigger_price),
        "contract_multiplier": str(projection.contract_multiplier),
        "current_to_protection_loss": str(projection.current_to_protection_loss),
        "open_heat": str(projection.open_heat),
        "protected_profit_giveback": str(projection.protected_profit_giveback),
        "facts_as_of": projection.facts_as_of.isoformat(),
        "calculation_version": projection.calculation_version,
    }
