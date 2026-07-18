from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError

from tests.integration.test_projections import (
    _prepare_collecting_run,
    _record_account_equity,
)
from trading_control_plane.capital_scope import (
    CAPITAL_SCOPE_CATALOG_SERVICE_PRINCIPAL,
    CapitalEnvironment,
    ManagedCapitalScopeManifestService,
    NativeCurrencyMtmComponent,
    PortfolioMtmProjectionService,
    PortfolioMtmQuery,
    PortfolioMtmState,
    RegisterManagedCapitalScopeManifestDraft,
    RegisterManagedCapitalScopeManifestRequest,
    RiskInclusionMode,
    managed_capital_scope_evidence_hash,
    managed_capital_scope_manifest_hash,
)
from trading_control_plane.capital_scope_models import ManagedCapitalScopeManifest
from trading_control_plane.command_executor import IdempotentCommandExecutor
from trading_control_plane.commands import CommandChannel, CommandEnvelope, CommandStatus
from trading_control_plane.database import Database
from trading_control_plane.models import AuditEvent, CapabilityGate, OutboxMessage
from trading_control_plane.projections import (
    CurrentAccountEquityScope,
    ProjectionQueryContext,
    ProjectionState,
)
from trading_control_plane.reconciliation import ReconciliationSourceType
from trading_control_plane.venue_facts import (
    RecordVenueAccountEquitySnapshotRequest,
    VenueAccountEquityState,
)

pytestmark = pytest.mark.integration


def _scope(**updates: str) -> CurrentAccountEquityScope:
    values = {
        "organization_id": "org-1",
        "venue": "BINANCE",
        "execution_domain": "BINANCE_USDM",
        "account_id": "account-1",
        "margin_mode": "ISOLATED",
        "collateral_pool_id": "pool-usdt-1",
        "settlement_currency": "USD",
    }
    values.update(updates)
    return CurrentAccountEquityScope.model_validate(values)


def _manifest_request(
    scopes: tuple[CurrentAccountEquityScope, ...],
    *,
    now: datetime,
    manifest_id: UUID | None = None,
    manifest_version: int = 1,
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
) -> RegisterManagedCapitalScopeManifestRequest:
    draft = RegisterManagedCapitalScopeManifestDraft(
        manifest_id=manifest_id or uuid4(),
        organization_id="org-1",
        manifest_version=manifest_version,
        environment=CapitalEnvironment.SHADOW,
        real_funds_eligible=False,
        risk_inclusion_mode=RiskInclusionMode.EXCHANGE_ONLY,
        report_currency="USD",
        account_scopes=scopes,
        valid_from=valid_from or now - timedelta(minutes=1),
        valid_until=valid_until or now + timedelta(hours=1),
        evidence_refs=("test-only:capital-scope-approval", "test-only:capital-scope-catalog"),
        source_ref="test-only:managed-capital-scope",
    )
    return RegisterManagedCapitalScopeManifestRequest.model_validate(
        {
            **draft.model_dump(mode="json"),
            "manifest_hash": managed_capital_scope_manifest_hash(draft),
            "evidence_hash": managed_capital_scope_evidence_hash(draft),
        }
    )


def _manifest_envelope(
    request: RegisterManagedCapitalScopeManifestRequest,
    *,
    now: datetime,
    idempotency_key: str | None = None,
    service_principal: str = CAPITAL_SCOPE_CATALOG_SERVICE_PRINCIPAL,
) -> CommandEnvelope:
    return CommandEnvelope(
        idempotency_key=idempotency_key or f"capital-scope-{uuid4()}",
        command_type=ManagedCapitalScopeManifestService.command_type,
        object_type="ManagedCapitalScopeManifest",
        object_id=str(request.manifest_id),
        expected_version=request.manifest_version,
        service_principal=service_principal,
        channel=CommandChannel.INTERNAL,
        scope={"organization_id": request.organization_id},
        correlation_id=uuid4(),
        issued_at=now,
        expires_at=now + timedelta(minutes=2),
        auth_context_ref="test-only:capital-scope-catalog",
        payload_schema_version=1,
        reason="register explicit shadow managed account universe",
        payload=request.model_dump(mode="json"),
    )


def _register(
    database: Database,
    request: RegisterManagedCapitalScopeManifestRequest,
    *,
    now: datetime,
    envelope: CommandEnvelope | None = None,
):
    service = ManagedCapitalScopeManifestService(clock=lambda: now)
    return IdempotentCommandExecutor(database.session_factory).execute(
        envelope or _manifest_envelope(request, now=now),
        service.register,
    )


def _query(
    database: Database,
    request: RegisterManagedCapitalScopeManifestRequest,
    *,
    as_of: datetime,
    max_age_ms: int = 10_000,
):
    with database.session_factory.begin() as session:
        return PortfolioMtmProjectionService.query(
            session,
            PortfolioMtmQuery(
                manifest_id=request.manifest_id,
                organization_id=request.organization_id,
                manifest_version=request.manifest_version,
                context=ProjectionQueryContext(as_of=as_of, max_age_ms=max_age_ms),
            ),
        )


def _record_equity(
    database: Database,
    *,
    settlement_currency: str,
    equity_state: VenueAccountEquityState = VenueAccountEquityState.CONFIRMED,
) -> tuple[datetime, RecordVenueAccountEquitySnapshotRequest]:
    run_id, _, run_time, inputs = _prepare_collecting_run(database, balance_count=1)
    normalized_at = run_time + timedelta(seconds=1)
    request, result = _record_account_equity(
        database,
        run_id,
        inputs[ReconciliationSourceType.VENUE_BALANCES],
        now=normalized_at,
        settlement_currency=settlement_currency,
        equity_state=equity_state,
    )
    assert result.status is CommandStatus.COMPLETED
    return normalized_at, request


def test_register_manifest_is_durable_idempotent_and_audited(database: Database) -> None:
    now = datetime.now(UTC)
    request = _manifest_request((_scope(),), now=now)
    envelope = _manifest_envelope(request, now=now, idempotency_key="capital-scope-stable-key")

    denied = _register(
        database,
        request,
        now=now,
        envelope=_manifest_envelope(
            request,
            now=now,
            service_principal="some-other-service",
        ),
    )
    original = _register(database, request, now=now, envelope=envelope)
    replay = _register(
        database,
        request,
        now=now,
        envelope=envelope.model_copy(update={"command_id": uuid4(), "correlation_id": uuid4()}),
    )
    changed_payload = {**envelope.payload, "source_ref": "test-only:changed-source"}
    conflict = _register(
        database,
        request,
        now=now,
        envelope=envelope.model_copy(
            update={
                "command_id": uuid4(),
                "correlation_id": uuid4(),
                "payload": changed_payload,
            }
        ),
    )
    version_conflict = _register(
        database,
        _manifest_request((_scope(),), now=now, manifest_version=1),
        now=now,
    )

    assert denied.status is CommandStatus.REJECTED
    assert denied.error_code == "CAPITAL_SCOPE_CATALOG_SERVICE_REQUIRED"
    assert original.status is CommandStatus.COMPLETED
    assert original.data["account_scope_count"] == 1
    assert original.data["real_funds_eligible"] is False
    assert replay.status is CommandStatus.ALREADY_PROCESSED
    assert conflict.status is CommandStatus.CONFLICT
    assert version_conflict.status is CommandStatus.REJECTED
    assert version_conflict.error_code == "CAPITAL_SCOPE_MANIFEST_VERSION_EXISTS"
    with database.session_factory.begin() as session:
        manifest = session.get(ManagedCapitalScopeManifest, request.manifest_id)
        assert manifest is not None
        assert manifest.environment == "SHADOW"
        assert manifest.report_currency == "USD"
        assert session.scalar(select(func.count()).select_from(ManagedCapitalScopeManifest)) == 1
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.event_type == "ManagedCapitalScopeManifestRegistered")
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxMessage)
                .where(OutboxMessage.event_type == "ManagedCapitalScopeManifestRegistered")
            )
            == 1
        )


def test_manifest_contract_rejects_unsorted_duplicate_and_cross_org_scopes() -> None:
    now = datetime.now(UTC)
    first = _scope(account_id="account-a")
    second = _scope(account_id="account-b")
    for scopes in (
        (second, first),
        (first, first),
        (first, _scope(organization_id="org-2", account_id="account-b")),
    ):
        draft = RegisterManagedCapitalScopeManifestDraft(
            manifest_id=uuid4(),
            organization_id="org-1",
            manifest_version=1,
            environment=CapitalEnvironment.SHADOW,
            real_funds_eligible=False,
            risk_inclusion_mode=RiskInclusionMode.EXCHANGE_ONLY,
            report_currency="USD",
            account_scopes=scopes,
            valid_from=now,
            valid_until=now + timedelta(hours=1),
            evidence_refs=("test-only:evidence",),
            source_ref="test-only:source",
        )
        with pytest.raises(ValidationError):
            RegisterManagedCapitalScopeManifestRequest.model_validate(
                {
                    **draft.model_dump(mode="json"),
                    "manifest_hash": managed_capital_scope_manifest_hash(draft),
                    "evidence_hash": managed_capital_scope_evidence_hash(draft),
                }
            )


def test_manifest_database_guards_reject_bypass_and_mutation(database: Database) -> None:
    now = datetime.now(UTC)
    request = _manifest_request((_scope(),), now=now)
    registered = _register(database, request, now=now)
    assert registered.status is CommandStatus.COMPLETED

    with pytest.raises(DBAPIError, match="is immutable"):
        with database.session_factory.begin() as session:
            session.execute(
                update(ManagedCapitalScopeManifest)
                .where(ManagedCapitalScopeManifest.manifest_id == request.manifest_id)
                .values(source_ref="direct-db-tamper")
            )

    unsorted = [
        _scope(account_id="account-b").model_dump(mode="json"),
        _scope(account_id="account-a").model_dump(mode="json"),
    ]
    with pytest.raises(DBAPIError, match="not canonical"):
        with database.session_factory.begin() as session:
            session.add(
                ManagedCapitalScopeManifest(
                    manifest_id=uuid4(),
                    organization_id="org-1",
                    manifest_version=2,
                    environment="SHADOW",
                    real_funds_eligible=False,
                    risk_inclusion_mode="EXCHANGE_ONLY",
                    report_currency="USD",
                    account_scopes=unsorted,
                    account_scope_count=2,
                    valid_from=now,
                    valid_until=now + timedelta(hours=1),
                    manifest_hash="a" * 64,
                    evidence_refs=["test-only:evidence"],
                    evidence_hash="b" * 64,
                    source_ref="test-only:direct-db",
                    created_at=now,
                )
            )

    duplicate = _scope(account_id="account-c").model_dump(mode="json")
    with pytest.raises(DBAPIError, match="must be unique"):
        with database.session_factory.begin() as session:
            session.add(
                ManagedCapitalScopeManifest(
                    manifest_id=uuid4(),
                    organization_id="org-1",
                    manifest_version=3,
                    environment="SHADOW",
                    real_funds_eligible=False,
                    risk_inclusion_mode="EXCHANGE_ONLY",
                    report_currency="USD",
                    account_scopes=[duplicate, duplicate],
                    account_scope_count=2,
                    valid_from=now,
                    valid_until=now + timedelta(hours=1),
                    manifest_hash="a" * 64,
                    evidence_refs=["test-only:evidence"],
                    evidence_hash="b" * 64,
                    source_ref="test-only:direct-db",
                    created_at=now,
                )
            )


def test_all_usd_complete_manifest_produces_confirmed_exchange_only_mtm(
    database: Database,
) -> None:
    normalized_at, source = _record_equity(database, settlement_currency="USD")
    request = _manifest_request((_scope(),), now=normalized_at)
    registered = _register(database, request, now=normalized_at)
    assert registered.status is CommandStatus.COMPLETED

    projection = _query(database, request, as_of=normalized_at + timedelta(seconds=1))
    rebuilt = _query(database, request, as_of=normalized_at + timedelta(seconds=1))

    assert projection.projection_state is PortfolioMtmState.CONFIRMED
    assert rebuilt == projection
    assert projection.reason_code is None
    assert projection.manifest_hash == request.manifest_hash
    assert projection.exchange_margin_equity == Decimal("10500")
    assert projection.current_unrealized_pnl == Decimal("500")
    assert projection.eligible_vault_equity == 0
    assert projection.current_portfolio_mtm_equity == Decimal("10500")
    assert projection.account_scope_count == 1
    assert (
        projection.account_components[0].source_snapshot_id
        == source.venue_account_equity_snapshot_id
    )
    assert projection.native_currency_components == (
        NativeCurrencyMtmComponent(
            settlement_currency="USD",
            account_scope_count=1,
            exchange_margin_equity=Decimal("10500"),
            current_unrealized_pnl=Decimal("500"),
        ),
    )
    with pytest.raises(ValidationError):
        type(projection).model_validate(
            {
                **projection.model_dump(mode="json"),
                "current_portfolio_mtm_equity": "11000",
            }
        )


def test_non_usd_complete_manifest_exposes_native_component_but_requires_fx(
    database: Database,
) -> None:
    normalized_at, _ = _record_equity(database, settlement_currency="USDT")
    request = _manifest_request(
        (_scope(settlement_currency="USDT"),),
        now=normalized_at,
    )
    assert _register(database, request, now=normalized_at).status is CommandStatus.COMPLETED

    projection = _query(database, request, as_of=normalized_at + timedelta(seconds=1))

    assert projection.projection_state is PortfolioMtmState.UNKNOWN
    assert projection.reason_code == "FX_FACTS_REQUIRED"
    assert projection.current_portfolio_mtm_equity is None
    assert projection.exchange_margin_equity is None
    assert projection.account_components[0].projection_state is ProjectionState.CONFIRMED
    assert projection.native_currency_components[0].settlement_currency == "USDT"
    assert projection.native_currency_components[0].exchange_margin_equity == Decimal("10500")
    with pytest.raises(ValidationError):
        type(projection).model_validate(
            {
                **projection.model_dump(mode="json"),
                "native_currency_components": [],
            }
        )


def test_missing_scope_makes_whole_portfolio_unknown_without_partial_sum(
    database: Database,
) -> None:
    normalized_at, _ = _record_equity(database, settlement_currency="USD")
    request = _manifest_request(
        (_scope(), _scope(account_id="missing-account")),
        now=normalized_at,
    )
    assert _register(database, request, now=normalized_at).status is CommandStatus.COMPLETED

    projection = _query(database, request, as_of=normalized_at + timedelta(seconds=1))

    assert projection.projection_state is PortfolioMtmState.UNKNOWN
    assert projection.reason_code == "ACCOUNT_SCOPE_INCOMPLETE"
    assert projection.current_portfolio_mtm_equity is None
    assert projection.native_currency_components == ()
    assert tuple(component.projection_state for component in projection.account_components) == (
        ProjectionState.CONFIRMED,
        ProjectionState.UNKNOWN,
    )
    assert projection.account_components[1].reason_code == "SOURCE_MISSING"


@pytest.mark.parametrize(
    ("equity_state", "max_age_ms", "as_of_delta", "component_reason"),
    (
        (VenueAccountEquityState.UNKNOWN, 10_000, timedelta(seconds=1), "SOURCE_UNKNOWN"),
        (VenueAccountEquityState.CONFIRMED, 1_000, timedelta(seconds=10), "SOURCE_STALE"),
    ),
)
def test_unknown_or_stale_scope_fails_closed(
    database: Database,
    equity_state: VenueAccountEquityState,
    max_age_ms: int,
    as_of_delta: timedelta,
    component_reason: str,
) -> None:
    normalized_at, _ = _record_equity(
        database,
        settlement_currency="USD",
        equity_state=equity_state,
    )
    request = _manifest_request((_scope(),), now=normalized_at)
    assert _register(database, request, now=normalized_at).status is CommandStatus.COMPLETED

    projection = _query(
        database,
        request,
        as_of=normalized_at + as_of_delta,
        max_age_ms=max_age_ms,
    )

    assert projection.projection_state is PortfolioMtmState.UNKNOWN
    assert projection.reason_code == "ACCOUNT_SCOPE_INCOMPLETE"
    assert projection.current_portfolio_mtm_equity is None
    assert projection.account_components[0].reason_code == component_reason
    assert projection.account_components[0].exchange_margin_equity is None


def test_manifest_validity_and_binding_fail_closed(database: Database) -> None:
    now = datetime.now(UTC)
    request = _manifest_request(
        (_scope(),),
        now=now,
        valid_from=now + timedelta(minutes=5),
        valid_until=now + timedelta(hours=1),
    )
    assert _register(database, request, now=now).status is CommandStatus.COMPLETED

    not_yet_valid = _query(database, request, as_of=now)
    expired = _query(database, request, as_of=now + timedelta(hours=2))
    with database.session_factory.begin() as session:
        binding_mismatch = PortfolioMtmProjectionService.query(
            session,
            PortfolioMtmQuery(
                manifest_id=request.manifest_id,
                organization_id="org-1",
                manifest_version=2,
                context=ProjectionQueryContext(as_of=now, max_age_ms=10_000),
            ),
        )
        missing = PortfolioMtmProjectionService.query(
            session,
            PortfolioMtmQuery(
                manifest_id=uuid4(),
                organization_id="org-1",
                manifest_version=1,
                context=ProjectionQueryContext(as_of=now, max_age_ms=10_000),
            ),
        )

    assert not_yet_valid.reason_code == "MANIFEST_NOT_YET_VALID"
    assert not_yet_valid.account_scope_count == 1
    assert expired.reason_code == "MANIFEST_EXPIRED"
    assert expired.account_scope_count == 1
    assert binding_mismatch.reason_code == "MANIFEST_BINDING_MISMATCH"
    assert missing.reason_code == "MANIFEST_MISSING"
    assert all(
        item.current_portfolio_mtm_equity is None
        for item in (not_yet_valid, expired, binding_mismatch, missing)
    )


def test_migration_seeds_no_manifest_and_keeps_live_gates_disabled(database: Database) -> None:
    with database.session_factory.begin() as session:
        assert session.scalar(select(func.count()).select_from(ManagedCapitalScopeManifest)) == 0
        gates = tuple(
            session.execute(
                select(CapabilityGate.capability_key, CapabilityGate.status).order_by(
                    CapabilityGate.capability_key
                )
            )
        )
    assert gates == (
        ("AUTO_ADD", "DISABLED"),
        ("CAPITAL_TRANSFER", "DISABLED"),
        ("LIVE_ORDER_SEND", "DISABLED"),
    )
