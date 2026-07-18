from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from tests.capability_fixtures import issue_shadow_certificate_for_risk_request
from tests.instrument_catalog_fixtures import (
    instrument_catalog_request_for_risk,
    register_instrument_catalog,
)
from tests.integration.test_capital_scope import _manifest_request, _register, _scope
from tests.integration.test_projections import _prepare_collecting_run, _record_account_equity
from tests.risk_fixtures import (
    TEST_CAPITAL_SCOPE_MANIFEST,
    make_capital,
    make_policy,
    make_request,
)
from tests.sender_fencing_fixtures import make_sender_scope
from trading_control_plane.authorization import SystemRiskState
from trading_control_plane.command_executor import IdempotentCommandExecutor
from trading_control_plane.commands import (
    CommandChannel,
    CommandEnvelope,
    CommandRejected,
    CommandStatus,
    hash_json,
)
from trading_control_plane.database import Database
from trading_control_plane.instrument_catalog_models import InstrumentCatalogRecord
from trading_control_plane.models import AuditEvent, OutboxMessage
from trading_control_plane.proposal_models import SystemRiskStateRecord
from trading_control_plane.reconciliation import ReconciliationSourceType
from trading_control_plane.risk import (
    CapitalProjectionBinding,
    RiskPrecheckRequest,
    RiskPrecheckService,
    SystemRiskStateCommandService,
    SystemRiskStateService,
)
from trading_control_plane.risk_models import (
    RiskDecisionSnapshot,
    RiskPolicyRecord,
    SystemRiskStateTransition,
)

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def seed_default_shadow_certificate(database: Database, reset_database: None) -> None:
    del reset_database
    result = issue_shadow_certificate_for_risk_request(database, make_request())
    assert result.status is CommandStatus.COMPLETED


@pytest.fixture(autouse=True)
def seed_default_capital_projection(database: Database, reset_database: None) -> None:
    del reset_database
    now = datetime.now(UTC)
    registered = _register(database, TEST_CAPITAL_SCOPE_MANIFEST, now=now)
    assert registered.status is CommandStatus.COMPLETED
    sender_scope = make_sender_scope(
        margin_mode="CROSS",
        collateral_pool_id="BINANCE:USDT-CROSS",
    )
    run_id, _, run_time, inputs = _prepare_collecting_run(
        database,
        balance_count=1,
        sender_scope=sender_scope,
    )
    normalized_at = run_time + timedelta(seconds=1)
    _, recorded = _record_account_equity(
        database,
        run_id,
        inputs[ReconciliationSourceType.VENUE_BALANCES],
        now=normalized_at,
        margin_mode="CROSS",
        collateral_pool_id="BINANCE:USDT-CROSS",
        settlement_currency="USD",
        wallet_balance=Decimal("100000"),
        exchange_margin_equity=Decimal("100000"),
        available_margin=Decimal("10000"),
        total_unrealized_pnl=Decimal("0"),
    )
    assert recorded.status is CommandStatus.COMPLETED


@pytest.fixture(autouse=True)
def seed_default_instrument_catalog(database: Database, reset_database: None) -> None:
    del reset_database
    now = datetime.now(UTC)
    request = instrument_catalog_request_for_risk(make_request(now=now), now=now)
    result = register_instrument_catalog(database, request, now=now)
    assert result.status is CommandStatus.COMPLETED


def count_rows(session: Session, model: type[object]) -> int:
    return session.execute(select(func.count()).select_from(model)).scalar_one()


def seed_policy(
    database: Database,
    *,
    now: datetime | None = None,
    tampered_hash: bool = False,
) -> UUID:
    created_at = now or datetime.now(UTC)
    policy_id = uuid4()
    parameters = make_policy().model_dump(mode="json")
    with database.session_factory.begin() as session:
        session.add(
            RiskPolicyRecord(
                risk_policy_id=policy_id,
                organization_id="org-1",
                policy_version="risk-shadow-test-v1",
                policy_mode="SHADOW",
                parameters=parameters,
                policy_hash="0" * 64 if tampered_hash else hash_json(parameters),
                evidence_refs=["test-only:risk-policy-fixture"],
                valid_from=created_at - timedelta(days=1),
                valid_until=created_at + timedelta(days=1),
                created_at=created_at,
            )
        )
    return policy_id


def seed_state(
    database: Database,
    *,
    status: SystemRiskState = SystemRiskState.NORMAL,
) -> None:
    with database.session_factory.begin() as session:
        session.add(
            SystemRiskStateRecord(
                organization_id="org-1",
                status=status.value,
                version=1,
                reason_code="INTEGRATION_INITIAL_STATE",
                policy_version="risk-state-test-v1",
                transition_source_ref="test-only:initial-state",
                updated_at=datetime.now(UTC),
            )
        )


def risk_envelope(request: RiskPrecheckRequest) -> CommandEnvelope:
    now = datetime.now(UTC)
    return CommandEnvelope(
        idempotency_key=f"risk-precheck-{uuid4()}",
        command_type="risk.precheck.evaluate.v6",
        object_type="ProposalCandidate",
        object_id=request.proposal_ref,
        expected_version=request.candidate_version,
        service_principal="trading:proposal-test",
        channel=CommandChannel.INTERNAL,
        scope={"organization_id": request.organization_id},
        correlation_id=uuid4(),
        issued_at=now,
        expires_at=now + timedelta(minutes=2),
        auth_context_ref="test-only:risk-precheck-auth",
        payload_schema_version=6,
        reason="evaluate proposal candidate",
        payload=request.model_dump(mode="json"),
    )


def execute_precheck(
    database: Database,
    envelope: CommandEnvelope,
    *,
    now: datetime | None = None,
):
    return IdempotentCommandExecutor(database.session_factory).execute(
        envelope,
        RiskPrecheckService(clock=(lambda: now) if now is not None else None).evaluate,
    )


def state_envelope(
    *,
    target: SystemRiskState,
    expected_version: int = 1,
) -> CommandEnvelope:
    now = datetime.now(UTC)
    return CommandEnvelope(
        idempotency_key=f"risk-state-{uuid4()}",
        command_type="risk.state.tighten.v1",
        object_type="SystemRiskState",
        object_id="org-1",
        expected_version=expected_version,
        service_principal="risk-monitor:test",
        channel=CommandChannel.INTERNAL,
        scope={"organization_id": "org-1"},
        correlation_id=uuid4(),
        issued_at=now,
        expires_at=now + timedelta(minutes=2),
        auth_context_ref="test-only:risk-monitor-auth",
        payload_schema_version=1,
        reason="tighten automatic risk state",
        payload={
            "organization_id": "org-1",
            "target_status": target.value,
            "reason_code": f"TEST_{target.value}",
            "policy_version": "risk-state-test-v1",
            "source_ref": f"monitor:{target.value.lower()}",
        },
    )


def test_allow_precheck_persists_immutable_snapshot_audit_and_outbox(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    seed_policy(database, now=now)
    seed_state(database)

    result = execute_precheck(database, risk_envelope(make_request(now=now)))

    assert result.status is CommandStatus.COMPLETED
    assert result.data["result"] == "ALLOW"
    assert result.data["execution_eligible"] is False
    assert result.data["reservation_created"] is False
    with database.session_factory.begin() as session:
        snapshot = session.execute(select(RiskDecisionSnapshot)).scalar_one()
        assert snapshot.current_portfolio_mtm_equity == Decimal("100000")
        assert snapshot.current_unrealized_pnl == 0
        assert snapshot.capital_scope_manifest_id == TEST_CAPITAL_SCOPE_MANIFEST.manifest_id
        assert snapshot.capital_scope_manifest_version == 1
        assert snapshot.capital_scope_manifest_hash == TEST_CAPITAL_SCOPE_MANIFEST.manifest_hash
        assert snapshot.capital_projection_version == "portfolio-mtm-v2"
        assert snapshot.catalog_record_id is not None
        catalog_record = session.get(InstrumentCatalogRecord, snapshot.catalog_record_id)
        assert catalog_record is not None
        assert snapshot.catalog_version == catalog_record.catalog_version
        assert snapshot.catalog_classification_version == catalog_record.classification_version
        assert snapshot.catalog_record_hash == catalog_record.record_hash
        assert snapshot.input_snapshot["instrument_classification"]["valid"] is True
        assert snapshot.decision["catalog_record_hash"] == catalog_record.record_hash
        assert (
            hash_json(snapshot.input_snapshot["capital_projection"])
            == snapshot.capital_projection_hash
        )
        assert (
            hash_json(snapshot.input_snapshot["durable_exposure_snapshot"])
            == snapshot.durable_exposure_snapshot_hash
        )
        assert (
            result.data["durable_exposure_snapshot_hash"] == snapshot.durable_exposure_snapshot_hash
        )
        assert snapshot.input_snapshot["durable_exposure_snapshot"]["campaign_id"] is None
        assert snapshot.input_snapshot["durable_exposure_snapshot"]["components"] == []
        assert snapshot.input_snapshot["request"]["current_trade_loss"] == {
            "open_heat": "0",
            "reserved_heat": "0",
            "unknown_heat": "0",
            "protected_profit_giveback": "0",
            "cost_stress_add_on": "0",
        }
        assert (
            snapshot.input_snapshot["capital_projection"]["account_components"][0][
                "source_snapshot_id"
            ]
            is not None
        )
        assert snapshot.one_r_0 == Decimal("500")
        assert snapshot.frozen_trade_loss_cap == Decimal("500")
        assert snapshot.dynamic_trade_loss_cap == Decimal("500")
        assert snapshot.trade_worst_case_loss_after == Decimal("10.8216")
        assert snapshot.decision["requested_base_heat"] == "10.500000000000000000"
        assert snapshot.decision["current_trade_loss"]["protected_profit_giveback"] == "0"
        assert snapshot.decision["current_protected_position_risk_calculation_hash"] is None
        assert snapshot.decision["requested_fee_stress"] == "0.100500000000000000"
        assert snapshot.decision["requested_stop_penetration_stress"] == ("0.201000000000000000")
        assert snapshot.decision["requested_adverse_funding_stress"] == ("0.020100000000000000")
        assert snapshot.decision["requested_cost_stress_add_on"] == "0.321600000000000000"
        assert snapshot.decision["requested_incremental_worst_case_loss"] == (
            "10.821600000000000000"
        )
        assert snapshot.decision["cost_stress_model_version"] == ("fee-stop-funding-stress-v1")
        underlying_stress = next(
            decision
            for decision in snapshot.decision["scope_decisions"]
            if decision["scope_type"] == "UNDERLYING"
        )
        assert Decimal(underlying_stress["incremental_planned_loss"]) == Decimal("10.8216")
        assert Decimal(underlying_stress["gap_stress_add_on"]) == Decimal("0.5025")
        assert Decimal(underlying_stress["liquidity_degradation_stress_add_on"]) == Decimal("1.005")
        assert Decimal(underlying_stress["unprotected_window_stress_add_on"]) == Decimal("0.25125")
        assert Decimal(underlying_stress["incremental_stress_loss"]) == Decimal("12.58035")
        assert (
            underlying_stress["scope_stress_model_version"] == "planned-loss-plus-scope-shocks-v1"
        )
        assert underlying_stress["scope_stress_source_ref"] == "test-only:scope-stress-research-v1"
        assert "requested_reserved_heat" not in snapshot.input_snapshot["request"]["requested"]
        assert "requested_cost_stress_add_on" not in snapshot.input_snapshot["request"]["requested"]
        assert (
            "requested_protected_profit_giveback"
            not in snapshot.input_snapshot["request"]["requested"]
        )
        assert all(
            "requested_incremental_planned_loss" not in scope
            for scope in snapshot.input_snapshot["request"]["scope_risks"]
        )
        assert all(
            "requested_incremental_stress_loss" not in scope
            for scope in snapshot.input_snapshot["request"]["scope_risks"]
        )
        assert snapshot.execution_eligible is False
        assert snapshot.reservation_created is False
        assert hash_json(snapshot.input_snapshot) == snapshot.input_hash
        assert hash_json(snapshot.decision) == snapshot.decision_hash
        assert (
            session.execute(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.event_type == "RiskPrecheckDecisionRecorded")
            ).scalar_one()
            == 1
        )
        assert (
            session.execute(
                select(func.count())
                .select_from(OutboxMessage)
                .where(OutboxMessage.event_type == "RiskPrecheckDecisionRecorded")
            ).scalar_one()
            == 1
        )
        assert session.execute(text("SELECT count(*) FROM risk_reservations")).scalar_one() == 0
        assert session.execute(text("SELECT count(*) FROM order_intents")).scalar_one() == 0


def test_precheck_denies_when_exact_catalog_version_is_missing(database: Database) -> None:
    now = datetime.now(UTC)
    base = make_request(now=now)
    request = base.model_copy(
        update={
            "binding": base.binding.model_copy(
                update={
                    "catalog_version": "catalog-missing-test-v1",
                    "instrument_scope_version": "instrument-scope-missing-test-v1",
                    "capability_certificate_ref": "test-only:missing-catalog-certificate",
                }
            )
        }
    )
    certificate = issue_shadow_certificate_for_risk_request(database, request)
    assert certificate.status is CommandStatus.COMPLETED
    seed_policy(database, now=now)
    seed_state(database)

    result = execute_precheck(database, risk_envelope(request), now=now)

    assert result.status is CommandStatus.COMPLETED
    assert result.data["result"] == "DENY"
    assert result.data["primary_reason_code"] == "INSTRUMENT_UNCLASSIFIED"
    assert result.data["catalog_record_id"] is None
    assert result.data["catalog_validation_reason_codes"] == ["INSTRUMENT_CATALOG_RECORD_NOT_FOUND"]
    with database.session_factory.begin() as session:
        snapshot = session.execute(select(RiskDecisionSnapshot)).scalar_one()
        assert snapshot.catalog_record_id is None
        assert snapshot.valid_until == now
        assert snapshot.decision["catalog_validation_reason_codes"] == [
            "INSTRUMENT_CATALOG_RECORD_NOT_FOUND"
        ]


def test_caller_cannot_forge_current_capital_values(database: Database) -> None:
    now = datetime.now(UTC)
    seed_policy(database, now=now)
    seed_state(database)
    forged = make_request(
        now=now,
        capital=make_capital(
            exchange_settled_equity_ex_upnl=Decimal("200000"),
            total_capital_snapshot_0=Decimal("200000"),
            available_margin=Decimal("20000"),
        ),
    )

    result = execute_precheck(database, risk_envelope(forged), now=now)

    assert result.status is CommandStatus.REJECTED
    assert result.error_code == "CAPITAL_INPUT_MISMATCH"
    with database.session_factory.begin() as session:
        assert count_rows(session, RiskDecisionSnapshot) == 0


@pytest.mark.parametrize("forged_field", ("funding", "initial_heat", "scope_usage"))
def test_proposal_precheck_rejects_caller_reported_durable_exposure(
    database: Database,
    forged_field: str,
) -> None:
    now = datetime.now(UTC)
    seed_policy(database, now=now)
    seed_state(database)
    request = make_request(now=now)
    if forged_field == "funding":
        request = request.model_copy(
            update={
                "capital": request.capital.model_copy(update={"funding_reserved": Decimal("1")})
            }
        )
    elif forged_field == "initial_heat":
        request = request.model_copy(
            update={
                "current_trade_loss": request.current_trade_loss.model_copy(
                    update={"open_heat": Decimal("1")}
                )
            }
        )
    else:
        first, *rest = request.scope_risks
        request = request.model_copy(
            update={
                "scope_risks": (
                    first.model_copy(update={"current_planned_loss": Decimal("1")}),
                    *rest,
                )
            }
        )

    result = execute_precheck(database, risk_envelope(request), now=now)

    assert result.status is CommandStatus.REJECTED
    assert result.error_code == "DURABLE_EXPOSURE_INPUT_MISMATCH"
    with database.session_factory.begin() as session:
        assert count_rows(session, RiskDecisionSnapshot) == 0


@pytest.mark.parametrize(
    ("binding_update", "expected_error"),
    (
        ({"manifest_hash": "f" * 64}, "CAPITAL_PROJECTION_BINDING_MISMATCH"),
        (
            {"manifest_id": UUID("00000000-0000-0000-0000-000000000099")},
            "CAPITAL_PROJECTION_UNAVAILABLE",
        ),
    ),
)
def test_manifest_identity_hash_or_projection_binding_cannot_be_substituted(
    database: Database,
    binding_update: dict[str, object],
    expected_error: str,
) -> None:
    now = datetime.now(UTC)
    seed_policy(database, now=now)
    seed_state(database)
    baseline = make_request(now=now)
    changed = baseline.model_copy(
        update={
            "capital_projection_binding": baseline.capital_projection_binding.model_copy(
                update=binding_update
            )
        }
    )

    result = execute_precheck(database, risk_envelope(changed), now=now)

    assert result.status is CommandStatus.REJECTED
    assert result.error_code == expected_error
    with database.session_factory.begin() as session:
        assert count_rows(session, RiskDecisionSnapshot) == 0


def test_trade_account_must_be_exact_member_of_capital_manifest(database: Database) -> None:
    now = datetime.now(UTC)
    seed_policy(database, now=now)
    seed_state(database)
    baseline = make_request(now=now)
    changed = baseline.model_copy(
        update={"binding": baseline.binding.model_copy(update={"account_id": "account-outside"})}
    )

    result = execute_precheck(database, risk_envelope(changed), now=now)

    assert result.status is CommandStatus.REJECTED
    assert result.error_code == "CAPITAL_TRADE_ACCOUNT_OUTSIDE_MANIFEST"


def test_stale_capital_projection_and_expired_manifest_fail_before_risk_math(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    seed_policy(database, now=now)
    seed_state(database)
    request = make_request(now=now)

    stale = execute_precheck(
        database,
        risk_envelope(request),
        now=now + timedelta(seconds=10),
    )
    expired = execute_precheck(
        database,
        risk_envelope(request),
        now=datetime(2101, 1, 1, tzinfo=UTC),
    )

    assert stale.status is CommandStatus.REJECTED
    assert stale.error_code == "CAPITAL_ACCOUNT_SCOPE_INCOMPLETE"
    assert expired.status is CommandStatus.REJECTED
    assert expired.error_code == "CAPITAL_PROJECTION_UNAVAILABLE"
    with database.session_factory.begin() as session:
        assert count_rows(session, RiskDecisionSnapshot) == 0


def test_non_usd_manifest_requires_certified_fx_before_risk_math(database: Database) -> None:
    now = datetime.now(UTC)
    seed_policy(database, now=now)
    seed_state(database)
    fx_scope = _scope(
        margin_mode="CROSS",
        collateral_pool_id="BINANCE:USDT-CROSS-FX",
        settlement_currency="USDT",
    )
    fx_manifest = _manifest_request(
        (fx_scope,),
        now=now,
        manifest_version=2,
    )
    assert _register(database, fx_manifest, now=now).status is CommandStatus.COMPLETED
    sender_scope = make_sender_scope(
        margin_mode="CROSS",
        collateral_pool_id="BINANCE:USDT-CROSS-FX",
    )
    run_id, _, run_time, inputs = _prepare_collecting_run(
        database,
        balance_count=1,
        sender_scope=sender_scope,
    )
    normalized_at = run_time + timedelta(seconds=1)
    _, recorded = _record_account_equity(
        database,
        run_id,
        inputs[ReconciliationSourceType.VENUE_BALANCES],
        now=normalized_at,
        venue_update_id="risk-fx-usdt",
        margin_mode="CROSS",
        collateral_pool_id="BINANCE:USDT-CROSS-FX",
        settlement_currency="USDT",
        wallet_balance=Decimal("100000"),
        exchange_margin_equity=Decimal("100000"),
        available_margin=Decimal("10000"),
        total_unrealized_pnl=Decimal("0"),
    )
    assert recorded.status is CommandStatus.COMPLETED
    baseline = make_request(now=now)
    request = baseline.model_copy(
        update={
            "capital_projection_binding": CapitalProjectionBinding(
                manifest_id=fx_manifest.manifest_id,
                manifest_version=fx_manifest.manifest_version,
                manifest_hash=fx_manifest.manifest_hash,
                projection_version="portfolio-mtm-v2",
            ),
            "binding": baseline.binding.model_copy(
                update={
                    "collateral_pool_id": "BINANCE:USDT-CROSS-FX",
                    "settlement_asset": "USDT",
                }
            ),
        }
    )

    result = execute_precheck(database, risk_envelope(request), now=normalized_at)

    assert result.status is CommandStatus.REJECTED
    assert result.error_code == "CAPITAL_FX_FACTS_REQUIRED"
    with database.session_factory.begin() as session:
        assert count_rows(session, RiskDecisionSnapshot) == 0


def test_stale_precheck_is_durable_deny_not_silent_failure(database: Database) -> None:
    now = datetime.now(UTC)
    seed_policy(database, now=now)
    seed_state(database)

    result = execute_precheck(
        database,
        risk_envelope(make_request(now=now, fact_age=timedelta(seconds=6))),
    )

    assert result.status is CommandStatus.COMPLETED
    assert result.data["result"] == "DENY"
    assert result.data["primary_reason_code"] == "FACTS_STALE"
    assert result.data["max_safe_quantity"] == "0"
    with database.session_factory.begin() as session:
        snapshot = session.execute(select(RiskDecisionSnapshot)).scalar_one()
        assert snapshot.result == "DENY"
        assert snapshot.valid_until == snapshot.decided_at


def test_missing_system_state_is_persisted_as_fail_closed_unknown(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    seed_policy(database, now=now)

    result = execute_precheck(database, risk_envelope(make_request(now=now)))

    assert result.data["result"] == "DENY"
    assert result.data["primary_reason_code"] == "SYSTEM_RISK_STATE_DENY"
    with database.session_factory.begin() as session:
        snapshot = session.execute(select(RiskDecisionSnapshot)).scalar_one()
        assert snapshot.system_risk_state == "UNKNOWN"


def test_missing_durable_certificate_is_persisted_as_fail_closed_deny(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    seed_policy(database, now=now)
    seed_state(database)
    baseline = make_request(now=now)
    missing = baseline.model_copy(
        update={
            "binding": baseline.binding.model_copy(
                update={"capability_certificate_ref": "capability:missing-durable-fact"}
            )
        }
    )

    result = execute_precheck(database, risk_envelope(missing))

    assert result.status is CommandStatus.COMPLETED
    assert result.data["result"] == "DENY"
    assert result.data["primary_reason_code"] == "CAPABILITY_CERTIFICATE_INVALID"
    with database.session_factory.begin() as session:
        snapshot = session.execute(select(RiskDecisionSnapshot)).scalar_one()
        validation = snapshot.input_snapshot["capability_validation"]
        assert validation["valid"] is False
        assert validation["reason_codes"] == ["CAPABILITY_CERTIFICATE_NOT_FOUND"]


def test_idempotent_replay_never_creates_second_risk_snapshot(database: Database) -> None:
    now = datetime.now(UTC)
    seed_policy(database, now=now)
    seed_state(database)
    envelope = risk_envelope(make_request(now=now))

    first = execute_precheck(database, envelope)
    replay = execute_precheck(database, envelope)

    assert first.status is CommandStatus.COMPLETED
    assert replay.status is CommandStatus.ALREADY_PROCESSED
    assert replay.replayed is True
    with database.session_factory.begin() as session:
        assert count_rows(session, RiskDecisionSnapshot) == 1
        assert (
            session.execute(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.event_type == "RiskPrecheckDecisionRecorded")
            ).scalar_one()
            == 1
        )
        assert (
            session.execute(
                select(func.count())
                .select_from(OutboxMessage)
                .where(OutboxMessage.event_type == "RiskPrecheckDecisionRecorded")
            ).scalar_one()
            == 1
        )


def test_missing_or_tampered_policy_fails_before_any_allow_snapshot(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    seed_state(database)
    request = make_request(now=now)

    missing = execute_precheck(database, risk_envelope(request))
    seed_policy(database, now=now, tampered_hash=True)
    tampered = execute_precheck(database, risk_envelope(request))

    assert missing.status is CommandStatus.REJECTED
    assert missing.error_code == "RISK_POLICY_UNAVAILABLE"
    assert tampered.status is CommandStatus.REJECTED
    assert tampered.error_code == "RISK_POLICY_INTEGRITY_FAILED"
    with database.session_factory.begin() as session:
        assert count_rows(session, RiskDecisionSnapshot) == 0


def test_web_actor_cannot_call_internal_risk_precheck_handler_directly(
    database: Database,
) -> None:
    request = make_request()
    internal = risk_envelope(request)
    direct = internal.model_copy(
        update={
            "actor_id": str(uuid4()),
            "service_principal": None,
            "channel": CommandChannel.WEB,
        }
    )

    result = execute_precheck(database, direct)

    assert result.status is CommandStatus.REJECTED
    assert result.error_code == "INTERNAL_SERVICE_REQUIRED"
    with database.session_factory.begin() as session:
        assert count_rows(session, RiskDecisionSnapshot) == 0


def test_risk_precheck_legacy_commands_and_wrong_v6_schema_are_rejected(
    database: Database,
) -> None:
    v1 = risk_envelope(make_request()).model_copy(
        update={"command_type": "risk.precheck.evaluate.v1"}
    )
    v2 = risk_envelope(make_request()).model_copy(
        update={"command_type": "risk.precheck.evaluate.v2"}
    )
    v3 = risk_envelope(make_request()).model_copy(
        update={"command_type": "risk.precheck.evaluate.v3"}
    )
    v4 = risk_envelope(make_request()).model_copy(
        update={"command_type": "risk.precheck.evaluate.v4"}
    )
    v5 = risk_envelope(make_request()).model_copy(
        update={"command_type": "risk.precheck.evaluate.v5"}
    )
    old_field = risk_envelope(make_request())
    old_payload = dict(old_field.payload)
    old_scopes = [dict(scope) for scope in old_payload["scope_risks"]]
    old_scopes[0]["requested_incremental_stress_loss"] = "1"
    old_payload["scope_risks"] = old_scopes
    old_field = old_field.model_copy(update={"payload": old_payload})
    caller_boolean = risk_envelope(make_request())
    caller_boolean_payload = dict(caller_boolean.payload)
    caller_boolean_payload["instrument_classified"] = True
    caller_boolean = caller_boolean.model_copy(update={"payload": caller_boolean_payload})
    wrong_schema = risk_envelope(make_request()).model_copy(update={"payload_schema_version": 2})

    v1_result = execute_precheck(database, v1)
    v2_result = execute_precheck(database, v2)
    v3_result = execute_precheck(database, v3)
    v4_result = execute_precheck(database, v4)
    v5_result = execute_precheck(database, v5)
    old_field_result = execute_precheck(database, old_field)
    caller_boolean_result = execute_precheck(database, caller_boolean)
    schema_result = execute_precheck(database, wrong_schema)

    assert v1_result.status is CommandStatus.REJECTED
    assert v1_result.error_code == "COMMAND_TYPE_MISMATCH"
    assert v2_result.status is CommandStatus.REJECTED
    assert v2_result.error_code == "COMMAND_TYPE_MISMATCH"
    assert v3_result.status is CommandStatus.REJECTED
    assert v3_result.error_code == "COMMAND_TYPE_MISMATCH"
    assert v4_result.status is CommandStatus.REJECTED
    assert v4_result.error_code == "COMMAND_TYPE_MISMATCH"
    assert v5_result.status is CommandStatus.REJECTED
    assert v5_result.error_code == "COMMAND_TYPE_MISMATCH"
    assert old_field_result.status is CommandStatus.REJECTED
    assert old_field_result.error_code == "RISK_INPUT_INVALID"
    assert caller_boolean_result.status is CommandStatus.REJECTED
    assert caller_boolean_result.error_code == "RISK_INPUT_INVALID"
    assert schema_result.status is CommandStatus.REJECTED
    assert schema_result.error_code == "PAYLOAD_SCHEMA_VERSION_MISMATCH"


def test_policy_decision_and_state_history_are_database_immutable(database: Database) -> None:
    now = datetime.now(UTC)
    policy_id = seed_policy(database, now=now)
    seed_state(database)
    execute_precheck(database, risk_envelope(make_request(now=now)))

    with pytest.raises(DBAPIError, match="risk_policies is immutable"):
        with database.session_factory.begin() as session:
            session.execute(
                update(RiskPolicyRecord)
                .where(RiskPolicyRecord.risk_policy_id == policy_id)
                .values(policy_version="mutated")
            )
    with pytest.raises(DBAPIError, match="risk_decision_snapshots is immutable"):
        with database.session_factory.begin() as session:
            session.execute(
                update(RiskDecisionSnapshot).values(capital_scope_manifest_hash="f" * 64)
            )
    with pytest.raises(DBAPIError, match="append-only"):
        with database.session_factory.begin() as session:
            session.execute(update(SystemRiskStateTransition).values(reason_code="mutated"))


def test_system_risk_state_only_tightens_and_records_every_transition(
    database: Database,
) -> None:
    seed_state(database)
    service = SystemRiskStateService()
    with database.session_factory.begin() as session:
        first = service.tighten(
            session,
            organization_id="org-1",
            target_status=SystemRiskState.NO_PYRAMID,
            reason_code="STALE_MARKET_DATA",
            policy_version="risk-state-test-v1",
            source_ref="monitor:test-1",
        )
        second = service.tighten(
            session,
            organization_id="org-1",
            target_status=SystemRiskState.NO_NEW_POSITION,
            reason_code="ACCOUNT_RECONCILIATION_FAILED",
            policy_version="risk-state-test-v1",
            source_ref="monitor:test-2",
        )

    assert first.changed is True
    assert second.current_status is SystemRiskState.NO_NEW_POSITION
    assert second.version == 3
    with database.session_factory.begin() as session:
        transitions = (
            session.execute(
                select(SystemRiskStateTransition).order_by(SystemRiskStateTransition.state_version)
            )
            .scalars()
            .all()
        )
        assert [item.transition_kind for item in transitions] == [
            "INITIAL",
            "AUTOMATIC_TIGHTEN",
            "AUTOMATIC_TIGHTEN",
        ]
        assert [item.to_status for item in transitions] == [
            "NORMAL",
            "NO_PYRAMID",
            "NO_NEW_POSITION",
        ]

    with pytest.raises(
        CommandRejected,
        match="automatic risk state recovery is forbidden",
    ):
        with database.session_factory.begin() as session:
            service.tighten(
                session,
                organization_id="org-1",
                target_status=SystemRiskState.NO_PYRAMID,
                reason_code="UNSAFE_RECOVERY_ATTEMPT",
                policy_version="risk-state-test-v1",
                source_ref="monitor:test-3",
            )
    with pytest.raises(DBAPIError, match="invalid automatic system_risk_state transition"):
        with database.session_factory.begin() as session:
            session.execute(
                update(SystemRiskStateRecord)
                .where(SystemRiskStateRecord.organization_id == "org-1")
                .values(status="NORMAL", version=4)
            )


def test_state_tightening_command_atomically_writes_history_audit_and_outbox(
    database: Database,
) -> None:
    seed_state(database)
    envelope = state_envelope(target=SystemRiskState.NO_PYRAMID)
    executor = IdempotentCommandExecutor(database.session_factory)

    result = executor.execute(envelope, SystemRiskStateCommandService().tighten)
    replay = executor.execute(envelope, SystemRiskStateCommandService().tighten)

    assert result.status is CommandStatus.COMPLETED
    assert result.object_version == 2
    assert result.data["current_status"] == "NO_PYRAMID"
    assert replay.status is CommandStatus.ALREADY_PROCESSED
    with database.session_factory.begin() as session:
        assert count_rows(session, SystemRiskStateTransition) == 2
        assert (
            session.execute(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.event_type == "SystemRiskStateTightened")
            ).scalar_one()
            == 1
        )
        assert (
            session.execute(
                select(func.count())
                .select_from(OutboxMessage)
                .where(OutboxMessage.event_type == "SystemRiskStateTightened")
            ).scalar_one()
            == 1
        )


def test_state_tightening_command_rejects_stale_expected_version(
    database: Database,
) -> None:
    seed_state(database)
    envelope = state_envelope(
        target=SystemRiskState.NO_NEW_POSITION,
        expected_version=2,
    )

    result = IdempotentCommandExecutor(database.session_factory).execute(
        envelope,
        SystemRiskStateCommandService().tighten,
    )

    assert result.status is CommandStatus.REJECTED
    assert result.error_code == "VERSION_CONFLICT"
    with database.session_factory.begin() as session:
        state = session.get(SystemRiskStateRecord, "org-1")
        assert state is not None
        assert (state.status, state.version) == ("NORMAL", 1)
        assert count_rows(session, SystemRiskStateTransition) == 1


def test_concurrent_automatic_tightening_cannot_end_in_weaker_state(
    database: Database,
) -> None:
    seed_state(database)

    def tighten(target: SystemRiskState) -> str:
        try:
            with database.session_factory.begin() as session:
                result = SystemRiskStateService().tighten(
                    session,
                    organization_id="org-1",
                    target_status=target,
                    reason_code=f"CONCURRENT_{target.value}",
                    policy_version="risk-state-test-v1",
                    source_ref=f"monitor:{target.value.lower()}",
                )
                return result.current_status.value
        except CommandRejected as exc:
            return exc.error_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(
                tighten,
                (SystemRiskState.NO_PYRAMID, SystemRiskState.REDUCE_ONLY),
            )
        )

    with database.session_factory.begin() as session:
        state = session.get(SystemRiskStateRecord, "org-1")
        assert state is not None
        assert state.status == "REDUCE_ONLY"
        transitions = session.execute(select(SystemRiskStateTransition)).scalars().all()
        assert all(
            item.to_status != "NORMAL" or item.transition_kind == "INITIAL" for item in transitions
        )
        assert len(transitions) in {2, 3}
    assert "REDUCE_ONLY" in outcomes
