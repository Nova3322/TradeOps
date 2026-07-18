from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from trading_control_plane.command_executor import IdempotentCommandExecutor
from trading_control_plane.commands import CommandChannel, CommandEnvelope, hash_json
from trading_control_plane.database import Database
from trading_control_plane.risk import RiskPrecheckRequest
from trading_control_plane.risk_fact_sets import RegisterRiskFactSetRequest
from trading_control_plane.strategy_evaluations import (
    STRATEGY_EVALUATOR_SERVICE_PRINCIPAL,
    RegisterStrategyEvaluationDraft,
    RegisterStrategyEvaluationRequest,
    StrategyEvaluationEnvironment,
    StrategyEvaluationKind,
    StrategyEvaluationOutcome,
    StrategyEvaluationService,
    StrategyRuleId,
    StrategyRuleResult,
    StrategyRuleStatus,
    strategy_evaluation_evidence_hash,
    strategy_evaluation_record_hash,
)
from trading_control_plane.trading_authorization_models import Campaign
from trading_control_plane.venue_fact_models import (
    VenuePositionSnapshot,
    VenueProtectionSnapshot,
)


def strategy_evaluation_request_for_add(
    risk_request: RiskPrecheckRequest,
    campaign: Campaign,
    fact_set: RegisterRiskFactSetRequest,
    position: VenuePositionSnapshot,
    protection: VenueProtectionSnapshot,
    *,
    now: datetime,
    evaluation_version: str,
    rule_statuses: dict[StrategyRuleId, StrategyRuleStatus] | None = None,
    **updates: Any,
) -> RegisterStrategyEvaluationRequest:
    binding = risk_request.binding
    statuses = {rule_id: StrategyRuleStatus.PASS for rule_id in StrategyRuleId}
    if rule_statuses is not None:
        statuses.update(rule_statuses)
    rules = tuple(
        StrategyRuleResult(
            rule_id=rule_id,
            status=statuses[rule_id],
            reason_code=f"TEST_{rule_id.value}_{statuses[rule_id].value}",
            evidence_payload_hash=hash_json(
                {
                    "rule_id": rule_id.value,
                    "status": statuses[rule_id].value,
                    "market_payload_hash": risk_request.market.model_dump(mode="json"),
                }
            ),
        )
        for rule_id in sorted(StrategyRuleId, key=lambda item: item.value)
    )
    status_values = {item.status for item in rules}
    outcome = (
        StrategyEvaluationOutcome.FAIL
        if StrategyRuleStatus.FAIL in status_values
        else (
            StrategyEvaluationOutcome.UNKNOWN
            if StrategyRuleStatus.UNKNOWN in status_values
            else StrategyEvaluationOutcome.PASS
        )
    )
    identity = f"{campaign.campaign_id}:{evaluation_version}"
    values: dict[str, Any] = {
        "strategy_evaluation_id": uuid5(NAMESPACE_URL, identity),
        "campaign_id": campaign.campaign_id,
        "organization_id": campaign.organization_id,
        "strategy_id": campaign.strategy_id,
        "strategy_version": campaign.strategy_version,
        "strategy_parameter_version": binding.strategy_parameter_version,
        "venue": campaign.venue,
        "execution_domain": campaign.execution_domain,
        "account_id": campaign.account_id,
        "canonical_instrument_id": campaign.instrument_id,
        "position_mode": binding.position_mode,
        "margin_mode": binding.margin_mode,
        "collateral_pool_id": binding.collateral_pool_id,
        "evaluation_version": evaluation_version,
        "evaluation_kind": StrategyEvaluationKind.ADD_CONTINUATION,
        "rule_results": rules,
        "outcome": outcome,
        "risk_fact_set_id": fact_set.risk_fact_set_id,
        "risk_fact_set_version": fact_set.fact_set_version,
        "risk_fact_set_record_hash": fact_set.record_hash,
        "position_snapshot_id": position.venue_position_snapshot_id,
        "position_snapshot_hash": position.snapshot_hash,
        "protection_snapshot_id": protection.venue_protection_snapshot_id,
        "protection_snapshot_hash": protection.snapshot_hash,
        "evaluator_version": "test-strategy-evaluator-v1",
        "environment": StrategyEvaluationEnvironment.SHADOW,
        "real_funds_eligible": False,
        "evaluated_at": now,
        "valid_from": now,
        "valid_until": now + timedelta(minutes=5),
        "evidence_refs": (
            "test-only:add-continuation-rules",
            "test-only:strategy-evaluation-source",
        ),
        "source_ref": "test-only:strategy-evaluation-service",
    }
    values.update(updates)
    draft = RegisterStrategyEvaluationDraft.model_validate(values)
    return RegisterStrategyEvaluationRequest.model_validate(
        {
            **draft.model_dump(mode="json"),
            "record_hash": strategy_evaluation_record_hash(draft),
            "evidence_hash": strategy_evaluation_evidence_hash(draft),
        }
    )


def strategy_evaluation_envelope(
    request: RegisterStrategyEvaluationRequest,
    *,
    now: datetime,
    idempotency_key: str | None = None,
    service_principal: str = STRATEGY_EVALUATOR_SERVICE_PRINCIPAL,
) -> CommandEnvelope:
    return CommandEnvelope(
        idempotency_key=idempotency_key or f"strategy-evaluation-{uuid4()}",
        command_type=StrategyEvaluationService.command_type,
        object_type="StrategyEvaluation",
        object_id=str(request.strategy_evaluation_id),
        expected_version=1,
        service_principal=service_principal,
        channel=CommandChannel.INTERNAL,
        scope={"organization_id": request.organization_id},
        correlation_id=uuid4(),
        issued_at=now,
        expires_at=now + timedelta(minutes=2),
        auth_context_ref="test-only:strategy-evaluation-service",
        payload_schema_version=1,
        reason="register exact SHADOW-only strategy evaluation",
        payload=request.model_dump(mode="json"),
    )


def register_strategy_evaluation(
    database: Database,
    request: RegisterStrategyEvaluationRequest,
    *,
    now: datetime,
    envelope: CommandEnvelope | None = None,
):
    return IdempotentCommandExecutor(database.session_factory).execute(
        envelope or strategy_evaluation_envelope(request, now=now),
        StrategyEvaluationService(clock=lambda: now).register,
    )
