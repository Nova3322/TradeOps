from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from tests.integration.test_execution import (
    create_add_envelope,
    prepare_open_add_campaign,
)
from tests.strategy_evaluation_fixtures import (
    register_strategy_evaluation,
    strategy_evaluation_envelope,
)
from trading_control_plane.commands import CommandStatus
from trading_control_plane.database import Database
from trading_control_plane.models import AuditEvent, OutboxMessage
from trading_control_plane.strategy_evaluation_models import StrategyEvaluationRecord
from trading_control_plane.strategy_evaluations import (
    RegisterStrategyEvaluationDraft,
    RegisterStrategyEvaluationRequest,
    StrategyEvaluationOutcome,
    StrategyEvaluationValidationRequest,
    StrategyEvaluationValidator,
    StrategyRuleResult,
    StrategyRuleStatus,
    _record_contract,
    strategy_evaluation_evidence_hash,
    strategy_evaluation_record_hash,
)

pytestmark = pytest.mark.integration


def _database_record(
    request: RegisterStrategyEvaluationRequest,
    *,
    created_at: datetime,
    **updates: Any,
) -> StrategyEvaluationRecord:
    values = {
        **request.model_dump(
            mode="python",
            exclude={"record_hash", "evidence_hash"},
        ),
        "evaluation_kind": request.evaluation_kind.value,
        "rule_results": [item.model_dump(mode="json") for item in request.rule_results],
        "outcome": request.outcome.value,
        "environment": request.environment.value,
        "record_hash": request.record_hash,
        "evidence_refs": list(request.evidence_refs),
        "evidence_hash": request.evidence_hash,
        "created_at": created_at,
    }
    values.update(updates)
    return StrategyEvaluationRecord(**values)


def _variant(
    request: RegisterStrategyEvaluationRequest,
    **updates: Any,
) -> RegisterStrategyEvaluationRequest:
    values = request.model_dump(mode="python", exclude={"record_hash", "evidence_hash"})
    values.update(updates)
    draft = RegisterStrategyEvaluationDraft.model_validate(values)
    return RegisterStrategyEvaluationRequest.model_validate(
        {
            **draft.model_dump(mode="json"),
            "record_hash": strategy_evaluation_record_hash(draft),
            "evidence_hash": strategy_evaluation_evidence_hash(draft),
        }
    )


def _validation_request(
    request: RegisterStrategyEvaluationRequest,
    *,
    validation_time: datetime,
    **updates: Any,
) -> StrategyEvaluationValidationRequest:
    values: dict[str, Any] = {
        "campaign_id": request.campaign_id,
        "organization_id": request.organization_id,
        "strategy_id": request.strategy_id,
        "strategy_version": request.strategy_version,
        "strategy_parameter_version": request.strategy_parameter_version,
        "venue": request.venue,
        "execution_domain": request.execution_domain,
        "account_id": request.account_id,
        "canonical_instrument_id": request.canonical_instrument_id,
        "position_mode": request.position_mode,
        "margin_mode": request.margin_mode,
        "collateral_pool_id": request.collateral_pool_id,
        "risk_fact_set_id": request.risk_fact_set_id,
        "risk_fact_set_version": request.risk_fact_set_version,
        "risk_fact_set_record_hash": request.risk_fact_set_record_hash,
        "position_snapshot_id": request.position_snapshot_id,
        "position_snapshot_hash": request.position_snapshot_hash,
        "protection_snapshot_id": request.protection_snapshot_id,
        "protection_snapshot_hash": request.protection_snapshot_hash,
        "validation_time": validation_time,
    }
    values.update(updates)
    return StrategyEvaluationValidationRequest.model_validate(values)


def _registered_evaluation(
    database: Database,
) -> RegisterStrategyEvaluationRequest:
    proposal, campaign, package, unit = prepare_open_add_campaign(
        database,
        unrealized_pnl=Decimal("9.75"),
    )
    now = datetime.now(UTC)
    create_add_envelope(
        database,
        proposal,
        campaign,
        package,
        unit,
        now=now,
        candidate_ref="strategy-evaluation-service-contract",
    )
    with database.session_factory.begin() as session:
        record = session.execute(select(StrategyEvaluationRecord)).scalar_one()
        return RegisterStrategyEvaluationRequest.model_validate(_record_contract(record))


def test_register_validate_audit_and_reject_untrusted_or_duplicate_evaluation(
    database: Database,
) -> None:
    request = _registered_evaluation(database)
    now = request.evaluated_at

    denied = register_strategy_evaluation(
        database,
        request,
        now=now,
        envelope=strategy_evaluation_envelope(
            request,
            now=now,
            service_principal="some-other-service",
        ),
    )
    invalid_hash = request.model_copy(
        update={
            "strategy_evaluation_id": uuid4(),
            "evaluation_version": "strategy-evaluation-invalid-hash-v2",
            "record_hash": "0" * 64,
        }
    )
    rejected = register_strategy_evaluation(database, invalid_hash, now=now)
    duplicate = register_strategy_evaluation(database, request, now=now)
    predates_source = _variant(
        request,
        strategy_evaluation_id=uuid4(),
        evaluation_version="strategy-evaluation-predates-source-v2",
        evaluated_at=now - timedelta(hours=1),
        valid_from=now - timedelta(hours=1),
        valid_until=now + timedelta(minutes=5),
    )
    predates_result = register_strategy_evaluation(database, predates_source, now=now)
    mismatched_source = _variant(
        request,
        strategy_evaluation_id=uuid4(),
        evaluation_version="strategy-evaluation-source-mismatch-v3",
        position_snapshot_hash="f" * 64,
    )
    mismatched_result = register_strategy_evaluation(database, mismatched_source, now=now)
    with database.session_factory.begin() as session:
        validation = StrategyEvaluationValidator().validate(
            session,
            _validation_request(request, validation_time=now),
        )
        assert session.scalar(select(func.count()).select_from(StrategyEvaluationRecord)) == 1
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.event_type == "StrategyEvaluationRegistered")
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxMessage)
                .where(OutboxMessage.event_type == "StrategyEvaluationRegistered")
            )
            == 1
        )

    assert denied.status is CommandStatus.REJECTED
    assert denied.error_code == "STRATEGY_EVALUATOR_SERVICE_REQUIRED"
    assert rejected.status is CommandStatus.REJECTED
    assert rejected.error_code == "STRATEGY_EVALUATION_INVALID"
    assert duplicate.status is CommandStatus.REJECTED
    assert duplicate.error_code == "STRATEGY_EVALUATION_VERSION_EXISTS"
    assert predates_result.status is CommandStatus.REJECTED
    assert predates_result.error_code == "STRATEGY_EVALUATION_PRECEDES_SOURCE_FACT"
    assert mismatched_result.status is CommandStatus.REJECTED
    assert mismatched_result.error_code == "STRATEGY_EVALUATION_SOURCE_FACT_MISMATCH"
    assert validation.valid is True
    assert validation.reason_codes == ()
    assert validation.strategy_evaluation_id == request.strategy_evaluation_id
    assert validation.record_hash == request.record_hash
    assert validation.outcome is StrategyEvaluationOutcome.PASS
    assert len(validation.rule_results) == 3


def test_validation_fails_closed_for_fact_mismatch_expiry_fail_and_latest_bad_hash(
    database: Database,
) -> None:
    request = _registered_evaluation(database)
    now = request.evaluated_at
    failed_rules = tuple(
        item.model_copy(
            update={
                "status": StrategyRuleStatus.FAIL,
                "reason_code": "TEST_TREND_CONTINUATION_FAIL",
            }
        )
        if item.rule_id.value == "TREND_CONTINUATION"
        else item
        for item in request.rule_results
    )
    failed = _variant(
        request,
        strategy_evaluation_id=uuid4(),
        evaluation_version="strategy-evaluation-failed-v2",
        rule_results=failed_rules,
        outcome=StrategyEvaluationOutcome.FAIL,
        evaluated_at=now + timedelta(seconds=1),
        valid_from=now + timedelta(seconds=1),
        valid_until=now + timedelta(minutes=5),
    )
    assert (
        register_strategy_evaluation(database, failed, now=now + timedelta(seconds=1)).status
        is CommandStatus.COMPLETED
    )
    with database.session_factory.begin() as session:
        fact_mismatch = StrategyEvaluationValidator().validate(
            session,
            _validation_request(
                failed,
                validation_time=now + timedelta(seconds=2),
                position_snapshot_hash="f" * 64,
            ),
        )
        failed_outcome = StrategyEvaluationValidator().validate(
            session,
            _validation_request(failed, validation_time=now + timedelta(seconds=2)),
        )
        expired = StrategyEvaluationValidator().validate(
            session,
            _validation_request(failed, validation_time=now + timedelta(minutes=6)),
        )

    assert fact_mismatch.valid is False
    assert fact_mismatch.reason_codes[0] == "STRATEGY_EVALUATION_FACT_BINDING_MISMATCH"
    assert failed_outcome.valid is False
    assert failed_outcome.reason_codes == ("STRATEGY_EVALUATION_OUTCOME_NOT_PASS",)
    assert expired.valid is False
    assert "STRATEGY_EVALUATION_OUTSIDE_VALID_WINDOW" in expired.reason_codes
    assert "STRATEGY_EVALUATION_OUTCOME_NOT_PASS" in expired.reason_codes

    corrupted = _variant(
        request,
        strategy_evaluation_id=uuid4(),
        evaluation_version="strategy-evaluation-corrupted-v3",
        evaluated_at=now + timedelta(seconds=3),
        valid_from=now + timedelta(seconds=3),
        valid_until=now + timedelta(minutes=5),
    )
    with database.session_factory.begin() as session:
        session.add(_database_record(corrupted, created_at=now, record_hash="0" * 64))
    with database.session_factory.begin() as session:
        integrity_failure = StrategyEvaluationValidator().validate(
            session,
            _validation_request(corrupted, validation_time=now + timedelta(seconds=4)),
        )
    assert integrity_failure.valid is False
    assert integrity_failure.reason_codes == ("STRATEGY_EVALUATION_INTEGRITY_FAILED",)
    assert integrity_failure.rule_results == ()


def test_database_enforces_shadow_only_immutability_and_canonical_rules(
    database: Database,
) -> None:
    request = _registered_evaluation(database)
    now = request.evaluated_at

    with pytest.raises(DBAPIError, match="immutable"):
        with database.session_factory.begin() as session:
            session.execute(
                update(StrategyEvaluationRecord)
                .where(
                    StrategyEvaluationRecord.strategy_evaluation_id
                    == request.strategy_evaluation_id
                )
                .values(source_ref="direct-db-tamper")
            )

    real_funds = _variant(
        request,
        strategy_evaluation_id=uuid4(),
        evaluation_version="strategy-evaluation-real-funds-v2",
    )
    with pytest.raises(IntegrityError, match="shadow_only"):
        with database.session_factory.begin() as session:
            session.add(
                _database_record(
                    real_funds,
                    created_at=now,
                    real_funds_eligible=True,
                )
            )

    noncanonical = _variant(
        request,
        strategy_evaluation_id=uuid4(),
        evaluation_version="strategy-evaluation-noncanonical-v3",
    )
    with pytest.raises(DBAPIError, match="not canonical"):
        with database.session_factory.begin() as session:
            session.add(
                _database_record(
                    noncanonical,
                    created_at=now,
                    rule_results=list(
                        reversed(
                            [
                                StrategyRuleResult.model_validate(item).model_dump(mode="json")
                                for item in noncanonical.rule_results
                            ]
                        )
                    ),
                )
            )
