from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from trading_control_plane.capital_scope_models import ManagedCapitalScopeManifest
from trading_control_plane.commands import (
    CommandChannel,
    CommandEnvelope,
    CommandOutcome,
    CommandRejected,
    CommandStatus,
    DomainEvent,
    hash_json,
)
from trading_control_plane.metrics import (
    CAPITAL_SCOPE_MANIFEST_REGISTRATIONS,
    PORTFOLIO_MTM_PROJECTION_QUERIES,
)
from trading_control_plane.projections import (
    CurrentAccountEquityProjection,
    CurrentAccountEquityScope,
    ProjectionFreshness,
    ProjectionMaturity,
    ProjectionQueryContext,
    ProjectionState,
    VenueCurrentProjectionService,
)

CAPITAL_SCOPE_CATALOG_SERVICE_PRINCIPAL = "capital-scope-catalog-service"
PORTFOLIO_MTM_PROJECTION_VERSION = "portfolio-mtm-v2"


class CapitalEnvironment(StrEnum):
    SHADOW = "SHADOW"


class RiskInclusionMode(StrEnum):
    EXCHANGE_ONLY = "EXCHANGE_ONLY"


class PortfolioMtmState(StrEnum):
    CONFIRMED = "CONFIRMED"
    UNKNOWN = "UNKNOWN"


class RegisterManagedCapitalScopeManifestRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest_id: UUID
    organization_id: str = Field(min_length=1, max_length=120)
    manifest_version: int = Field(ge=1)
    environment: CapitalEnvironment
    real_funds_eligible: bool
    risk_inclusion_mode: RiskInclusionMode
    report_currency: str = Field(pattern=r"^USD$")
    account_scopes: tuple[CurrentAccountEquityScope, ...] = Field(min_length=1)
    valid_from: datetime
    valid_until: datetime
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    evidence_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_ref: str = Field(min_length=1, max_length=255)

    @model_validator(mode="after")
    def manifest_is_canonical_and_self_consistent(self) -> Self:
        if self.environment is not CapitalEnvironment.SHADOW or self.real_funds_eligible:
            raise ValueError("managed capital scope manifest must remain shadow-only")
        if self.risk_inclusion_mode is not RiskInclusionMode.EXCHANGE_ONLY:
            raise ValueError("only the confirmed EXCHANGE_ONLY risk mode is accepted")
        if self.valid_from.tzinfo is None or self.valid_until.tzinfo is None:
            raise ValueError("manifest validity timestamps must be timezone-aware")
        if self.valid_until <= self.valid_from:
            raise ValueError("manifest validity window is empty")
        if any(scope.organization_id != self.organization_id for scope in self.account_scopes):
            raise ValueError("account scope organization differs from manifest organization")
        ordered_scopes = tuple(sorted(self.account_scopes, key=account_scope_sort_key))
        if self.account_scopes != ordered_scopes:
            raise ValueError("account scopes must be in canonical order")
        if len(set(self.account_scopes)) != len(self.account_scopes):
            raise ValueError("account scopes must be unique")
        if any(not reference for reference in self.evidence_refs):
            raise ValueError("manifest evidence references cannot be empty")
        if self.evidence_refs != tuple(sorted(set(self.evidence_refs))):
            raise ValueError("manifest evidence references must be sorted and unique")
        if self.manifest_hash != managed_capital_scope_manifest_hash(self):
            raise ValueError("managed capital scope manifest hash mismatch")
        if self.evidence_hash != managed_capital_scope_evidence_hash(self):
            raise ValueError("managed capital scope evidence hash mismatch")
        return self


class RegisterManagedCapitalScopeManifestDraft(BaseModel):
    """Hash-free authoring contract used before final immutable validation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest_id: UUID
    organization_id: str = Field(min_length=1, max_length=120)
    manifest_version: int = Field(ge=1)
    environment: CapitalEnvironment
    real_funds_eligible: bool
    risk_inclusion_mode: RiskInclusionMode
    report_currency: str = Field(pattern=r"^USD$")
    account_scopes: tuple[CurrentAccountEquityScope, ...] = Field(min_length=1)
    valid_from: datetime
    valid_until: datetime
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    source_ref: str = Field(min_length=1, max_length=255)


class PortfolioMtmQuery(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest_id: UUID
    organization_id: str = Field(min_length=1, max_length=120)
    manifest_version: int = Field(ge=1)
    context: ProjectionQueryContext


class PortfolioAccountComponent(BaseModel):
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
    facts_as_of: datetime | None
    age_ms: int | None = Field(default=None, ge=0)
    exchange_margin_equity: Decimal | None
    current_unrealized_pnl: Decimal | None
    available_margin: Decimal | None

    @model_validator(mode="after")
    def unknown_component_never_exposes_economics(self) -> Self:
        if self.projection_state is ProjectionState.UNKNOWN and (
            self.exchange_margin_equity is not None
            or self.current_unrealized_pnl is not None
            or self.available_margin is not None
        ):
            raise ValueError("unknown account component cannot expose economics")
        if self.projection_state is ProjectionState.CONFIRMED and (
            self.exchange_margin_equity is None
            or self.current_unrealized_pnl is None
            or self.available_margin is None
            or self.reason_code is not None
            or self.freshness is not ProjectionFreshness.FRESH
            or self.maturity is not ProjectionMaturity.VENUE_CONFIRMED
            or self.source_snapshot_id is None
            or self.source_snapshot_hash is None
            or self.source_version is None
            or self.normalization_version is None
            or self.facts_as_of is None
        ):
            raise ValueError("confirmed account component lacks canonical economics")
        if self.projection_state is ProjectionState.UNKNOWN and self.reason_code is None:
            raise ValueError("unknown account component requires a reason")
        return self


class NativeCurrencyMtmComponent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    settlement_currency: str = Field(min_length=1, max_length=80)
    account_scope_count: int = Field(ge=1)
    exchange_margin_equity: Decimal
    current_unrealized_pnl: Decimal
    available_margin: Decimal


class PortfolioMtmProjection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest_id: UUID
    organization_id: str
    manifest_version: int
    manifest_hash: str | None
    risk_inclusion_mode: RiskInclusionMode
    report_currency: str
    projection_state: PortfolioMtmState
    reason_code: str | None
    queried_as_of: datetime
    facts_as_of: datetime | None
    account_scope_count: int = Field(ge=0)
    account_components: tuple[PortfolioAccountComponent, ...]
    native_currency_components: tuple[NativeCurrencyMtmComponent, ...]
    exchange_margin_equity: Decimal | None
    current_unrealized_pnl: Decimal | None
    available_margin: Decimal | None
    eligible_vault_equity: Decimal | None
    current_portfolio_mtm_equity: Decimal | None
    projection_version: str = Field(pattern=r"^portfolio-mtm-v[0-9]+$")

    @model_validator(mode="after")
    def portfolio_economics_are_fail_closed(self) -> Self:
        economics = (
            self.exchange_margin_equity,
            self.current_unrealized_pnl,
            self.available_margin,
            self.eligible_vault_equity,
            self.current_portfolio_mtm_equity,
        )
        if self.projection_state is PortfolioMtmState.UNKNOWN:
            if any(value is not None for value in economics) or self.reason_code is None:
                raise ValueError("unknown portfolio projection cannot expose report economics")
        elif (
            any(value is None for value in economics)
            or self.reason_code is not None
            or self.report_currency != "USD"
            or self.account_scope_count == 0
            or self.account_scope_count != len(self.account_components)
            or not self.native_currency_components
            or sum(component.account_scope_count for component in self.native_currency_components)
            != self.account_scope_count
            or any(
                component.projection_state is not ProjectionState.CONFIRMED
                for component in self.account_components
            )
            or any(
                component.settlement_currency != "USD"
                for component in self.native_currency_components
            )
            or self.exchange_margin_equity
            != sum(
                (component.exchange_margin_equity for component in self.native_currency_components),
                Decimal(0),
            )
            or self.current_unrealized_pnl
            != sum(
                (component.current_unrealized_pnl for component in self.native_currency_components),
                Decimal(0),
            )
            or self.available_margin
            != sum(
                (component.available_margin for component in self.native_currency_components),
                Decimal(0),
            )
            or self.eligible_vault_equity != 0
            or self.current_portfolio_mtm_equity != self.exchange_margin_equity
        ):
            raise ValueError("confirmed portfolio projection lacks complete USD economics")
        if self.reason_code == "FX_FACTS_REQUIRED" and (
            not self.native_currency_components
            or any(
                component.projection_state is not ProjectionState.CONFIRMED
                for component in self.account_components
            )
            or all(
                component.settlement_currency == self.report_currency
                for component in self.native_currency_components
            )
        ):
            raise ValueError("FX_FACTS_REQUIRED lacks complete non-report-currency components")
        if self.reason_code == "ACCOUNT_SCOPE_INCOMPLETE" and (
            not self.account_components
            or all(
                component.projection_state is ProjectionState.CONFIRMED
                for component in self.account_components
            )
            or self.native_currency_components
        ):
            raise ValueError("incomplete portfolio lacks an unknown account component")
        return self


def account_scope_sort_key(scope: CurrentAccountEquityScope) -> tuple[str, ...]:
    return (
        scope.organization_id,
        scope.venue,
        scope.execution_domain,
        scope.account_id,
        scope.margin_mode,
        scope.collateral_pool_id,
        scope.settlement_currency,
    )


def managed_capital_scope_manifest_hash(
    request: RegisterManagedCapitalScopeManifestRequest | RegisterManagedCapitalScopeManifestDraft,
) -> str:
    return hash_json(
        {
            "manifest_id": str(request.manifest_id),
            "organization_id": request.organization_id,
            "manifest_version": request.manifest_version,
            "environment": request.environment.value,
            "real_funds_eligible": request.real_funds_eligible,
            "risk_inclusion_mode": request.risk_inclusion_mode.value,
            "report_currency": request.report_currency,
            "account_scopes": [scope.model_dump(mode="json") for scope in request.account_scopes],
            "valid_from": request.valid_from.astimezone(UTC).isoformat(),
            "valid_until": request.valid_until.astimezone(UTC).isoformat(),
        }
    )


def managed_capital_scope_evidence_hash(
    request: RegisterManagedCapitalScopeManifestRequest | RegisterManagedCapitalScopeManifestDraft,
) -> str:
    return hash_json(
        {
            "evidence_refs": list(request.evidence_refs),
            "source_ref": request.source_ref,
        }
    )


class ManagedCapitalScopeManifestService:
    command_type = "capital_scope.manifest.register.v1"

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))

    def register(self, session: Session, envelope: CommandEnvelope) -> CommandOutcome:
        if envelope.command_type != self.command_type:
            raise CommandRejected("COMMAND_TYPE_MISMATCH", "unexpected command type")
        if (
            envelope.channel is not CommandChannel.INTERNAL
            or envelope.service_principal != CAPITAL_SCOPE_CATALOG_SERVICE_PRINCIPAL
        ):
            raise CommandRejected(
                "CAPITAL_SCOPE_CATALOG_SERVICE_REQUIRED",
                "only the exact internal capital-scope catalog service may register manifests",
            )
        if envelope.object_type != "ManagedCapitalScopeManifest" or envelope.object_id is None:
            raise CommandRejected(
                "OBJECT_BINDING_MISMATCH", "ManagedCapitalScopeManifest binding is required"
            )
        try:
            request = RegisterManagedCapitalScopeManifestRequest.model_validate(envelope.payload)
        except ValidationError as exc:
            raise CommandRejected("CAPITAL_SCOPE_MANIFEST_INVALID", str(exc)) from exc
        if envelope.object_id != str(request.manifest_id):
            raise CommandRejected("OBJECT_BINDING_MISMATCH", "manifest identity changed")
        if envelope.expected_version != request.manifest_version:
            raise CommandRejected("VERSION_CONFLICT", "manifest version changed")
        if envelope.scope.get("organization_id") != request.organization_id:
            raise CommandRejected("SCOPE_MISMATCH", "organization scope changed")
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {
                "lock_key": (
                    f"managed-capital-scope:{request.organization_id}:{request.manifest_version}"
                )
            },
        )
        existing = session.execute(
            select(ManagedCapitalScopeManifest).where(
                ManagedCapitalScopeManifest.organization_id == request.organization_id,
                ManagedCapitalScopeManifest.manifest_version == request.manifest_version,
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise CommandRejected(
                "CAPITAL_SCOPE_MANIFEST_VERSION_EXISTS",
                "organization manifest version already exists",
            )
        if session.get(ManagedCapitalScopeManifest, request.manifest_id) is not None:
            raise CommandRejected(
                "CAPITAL_SCOPE_MANIFEST_ID_EXISTS", "manifest identity already exists"
            )

        created_at = self._clock()
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise CommandRejected(
                "CAPITAL_SCOPE_CLOCK_INVALID",
                "capital-scope catalog clock must be timezone-aware",
            )
        session.add(
            ManagedCapitalScopeManifest(
                manifest_id=request.manifest_id,
                organization_id=request.organization_id,
                manifest_version=request.manifest_version,
                environment=request.environment.value,
                real_funds_eligible=request.real_funds_eligible,
                risk_inclusion_mode=request.risk_inclusion_mode.value,
                report_currency=request.report_currency,
                account_scopes=[scope.model_dump(mode="json") for scope in request.account_scopes],
                account_scope_count=len(request.account_scopes),
                valid_from=request.valid_from,
                valid_until=request.valid_until,
                manifest_hash=request.manifest_hash,
                evidence_refs=list(request.evidence_refs),
                evidence_hash=request.evidence_hash,
                source_ref=request.source_ref,
                created_at=created_at,
            )
        )
        CAPITAL_SCOPE_MANIFEST_REGISTRATIONS.labels("REGISTERED").inc()
        return CommandOutcome(
            status=CommandStatus.COMPLETED,
            object_type="ManagedCapitalScopeManifest",
            object_id=str(request.manifest_id),
            object_version=request.manifest_version,
            data={
                "environment": request.environment.value,
                "real_funds_eligible": request.real_funds_eligible,
                "risk_inclusion_mode": request.risk_inclusion_mode.value,
                "report_currency": request.report_currency,
                "account_scope_count": len(request.account_scopes),
                "manifest_hash": request.manifest_hash,
            },
            events=(
                DomainEvent(
                    event_type="ManagedCapitalScopeManifestRegistered",
                    aggregate_type="ManagedCapitalScopeManifest",
                    aggregate_id=str(request.manifest_id),
                    payload={
                        "organization_id": request.organization_id,
                        "manifest_version": request.manifest_version,
                        "manifest_hash": request.manifest_hash,
                        "account_scope_count": len(request.account_scopes),
                        "environment": request.environment.value,
                        "real_funds_eligible": request.real_funds_eligible,
                    },
                ),
            ),
        )


class PortfolioMtmProjectionService:
    @staticmethod
    def query(session: Session, request: PortfolioMtmQuery) -> PortfolioMtmProjection:
        manifest = session.get(ManagedCapitalScopeManifest, request.manifest_id)
        if manifest is None:
            return _unknown_portfolio(request, "MANIFEST_MISSING")
        if (
            manifest.organization_id != request.organization_id
            or manifest.manifest_version != request.manifest_version
        ):
            return _unknown_portfolio(request, "MANIFEST_BINDING_MISMATCH")
        try:
            validated = RegisterManagedCapitalScopeManifestRequest.model_validate(
                {
                    "manifest_id": manifest.manifest_id,
                    "organization_id": manifest.organization_id,
                    "manifest_version": manifest.manifest_version,
                    "environment": manifest.environment,
                    "real_funds_eligible": manifest.real_funds_eligible,
                    "risk_inclusion_mode": manifest.risk_inclusion_mode,
                    "report_currency": manifest.report_currency,
                    "account_scopes": manifest.account_scopes,
                    "valid_from": manifest.valid_from,
                    "valid_until": manifest.valid_until,
                    "manifest_hash": manifest.manifest_hash,
                    "evidence_refs": manifest.evidence_refs,
                    "evidence_hash": manifest.evidence_hash,
                    "source_ref": manifest.source_ref,
                }
            )
        except ValidationError:
            return _unknown_portfolio(request, "MANIFEST_INTEGRITY_FAILED")
        if request.context.as_of < validated.valid_from:
            return _unknown_portfolio(
                request,
                "MANIFEST_NOT_YET_VALID",
                manifest_hash=validated.manifest_hash,
                declared_scope_count=len(validated.account_scopes),
            )
        if request.context.as_of >= validated.valid_until:
            return _unknown_portfolio(
                request,
                "MANIFEST_EXPIRED",
                manifest_hash=validated.manifest_hash,
                declared_scope_count=len(validated.account_scopes),
            )

        projections = tuple(
            VenueCurrentProjectionService.current_account_equity(session, scope, request.context)
            for scope in validated.account_scopes
        )
        components = tuple(_account_component(item) for item in projections)
        all_confirmed = all(
            projection.projection_state is ProjectionState.CONFIRMED for projection in projections
        )
        if not all_confirmed:
            return _unknown_portfolio(
                request,
                "ACCOUNT_SCOPE_INCOMPLETE",
                manifest_hash=validated.manifest_hash,
                components=components,
            )

        native_components = _native_currency_components(projections)
        facts_as_of = min(
            projection.facts_as_of
            for projection in projections
            if projection.facts_as_of is not None
        )
        if any(
            component.settlement_currency != validated.report_currency
            for component in native_components
        ):
            return _unknown_portfolio(
                request,
                "FX_FACTS_REQUIRED",
                manifest_hash=validated.manifest_hash,
                components=components,
                native_components=native_components,
                facts_as_of=facts_as_of,
            )

        exchange_equity = sum(
            (
                projection.exchange_margin_equity
                for projection in projections
                if projection.exchange_margin_equity is not None
            ),
            Decimal(0),
        )
        current_upnl = sum(
            (
                projection.total_unrealized_pnl
                for projection in projections
                if projection.total_unrealized_pnl is not None
            ),
            Decimal(0),
        )
        result = PortfolioMtmProjection(
            manifest_id=request.manifest_id,
            organization_id=request.organization_id,
            manifest_version=request.manifest_version,
            manifest_hash=validated.manifest_hash,
            risk_inclusion_mode=validated.risk_inclusion_mode,
            report_currency=validated.report_currency,
            projection_state=PortfolioMtmState.CONFIRMED,
            reason_code=None,
            queried_as_of=request.context.as_of,
            facts_as_of=facts_as_of,
            account_scope_count=len(components),
            account_components=components,
            native_currency_components=native_components,
            exchange_margin_equity=exchange_equity,
            current_unrealized_pnl=current_upnl,
            available_margin=sum(
                (
                    projection.available_margin
                    for projection in projections
                    if projection.available_margin is not None
                ),
                Decimal(0),
            ),
            eligible_vault_equity=Decimal(0),
            current_portfolio_mtm_equity=exchange_equity,
            projection_version=PORTFOLIO_MTM_PROJECTION_VERSION,
        )
        _observe_portfolio(result)
        return result


def _account_component(projection: CurrentAccountEquityProjection) -> PortfolioAccountComponent:
    return PortfolioAccountComponent(
        scope=projection.scope,
        projection_state=projection.projection_state,
        freshness=projection.freshness,
        maturity=projection.maturity,
        reason_code=projection.reason_code,
        source_snapshot_id=projection.source_snapshot_id,
        source_snapshot_hash=projection.source_snapshot_hash,
        source_version=projection.source_version,
        normalization_version=projection.normalization_version,
        facts_as_of=projection.facts_as_of,
        age_ms=projection.age_ms,
        exchange_margin_equity=projection.exchange_margin_equity,
        current_unrealized_pnl=projection.total_unrealized_pnl,
        available_margin=projection.available_margin,
    )


def _native_currency_components(
    projections: tuple[CurrentAccountEquityProjection, ...],
) -> tuple[NativeCurrencyMtmComponent, ...]:
    grouped: dict[str, list[CurrentAccountEquityProjection]] = defaultdict(list)
    for projection in projections:
        grouped[projection.scope.settlement_currency].append(projection)
    return tuple(
        NativeCurrencyMtmComponent(
            settlement_currency=currency,
            account_scope_count=len(items),
            exchange_margin_equity=sum(
                (
                    item.exchange_margin_equity
                    for item in items
                    if item.exchange_margin_equity is not None
                ),
                Decimal(0),
            ),
            current_unrealized_pnl=sum(
                (
                    item.total_unrealized_pnl
                    for item in items
                    if item.total_unrealized_pnl is not None
                ),
                Decimal(0),
            ),
            available_margin=sum(
                (item.available_margin for item in items if item.available_margin is not None),
                Decimal(0),
            ),
        )
        for currency, items in sorted(grouped.items())
    )


def _unknown_portfolio(
    request: PortfolioMtmQuery,
    reason_code: str,
    *,
    manifest_hash: str | None = None,
    components: tuple[PortfolioAccountComponent, ...] = (),
    native_components: tuple[NativeCurrencyMtmComponent, ...] = (),
    facts_as_of: datetime | None = None,
    declared_scope_count: int | None = None,
) -> PortfolioMtmProjection:
    result = PortfolioMtmProjection(
        manifest_id=request.manifest_id,
        organization_id=request.organization_id,
        manifest_version=request.manifest_version,
        manifest_hash=manifest_hash,
        risk_inclusion_mode=RiskInclusionMode.EXCHANGE_ONLY,
        report_currency="USD",
        projection_state=PortfolioMtmState.UNKNOWN,
        reason_code=reason_code,
        queried_as_of=request.context.as_of,
        facts_as_of=facts_as_of,
        account_scope_count=(
            len(components) if declared_scope_count is None else declared_scope_count
        ),
        account_components=components,
        native_currency_components=native_components,
        exchange_margin_equity=None,
        current_unrealized_pnl=None,
        available_margin=None,
        eligible_vault_equity=None,
        current_portfolio_mtm_equity=None,
        projection_version=PORTFOLIO_MTM_PROJECTION_VERSION,
    )
    _observe_portfolio(result)
    return result


def _observe_portfolio(result: PortfolioMtmProjection) -> None:
    PORTFOLIO_MTM_PROJECTION_QUERIES.labels(
        result.projection_state.value,
        result.reason_code or "NONE",
    ).inc()
