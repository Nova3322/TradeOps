from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from tests.risk_fixtures import make_fact_observations
from trading_control_plane.command_executor import IdempotentCommandExecutor
from trading_control_plane.commands import CommandChannel, CommandEnvelope
from trading_control_plane.database import Database
from trading_control_plane.risk import RiskPrecheckRequest, market_risk_fact_payload_hash
from trading_control_plane.risk_fact_sets import (
    RISK_FACT_AGGREGATOR_SERVICE_PRINCIPAL,
    RegisterRiskFactSetDraft,
    RegisterRiskFactSetRequest,
    RiskFactSetEnvironment,
    RiskFactSetService,
    risk_fact_set_evidence_hash,
    risk_fact_set_record_hash,
)
from trading_control_plane.risk_facts import FactStatus, FactType


def risk_fact_set_request_for_risk(
    risk_request: RiskPrecheckRequest,
    *,
    now: datetime,
    fact_set_version: str = "risk-fact-set-test-v1",
    fact_status: FactStatus = FactStatus.KNOWN,
    fact_age: timedelta = timedelta(milliseconds=100),
    **updates: Any,
) -> RegisterRiskFactSetRequest:
    binding = risk_request.binding
    identity = ":".join(
        (
            risk_request.organization_id,
            binding.venue,
            binding.execution_domain,
            binding.account_id,
            binding.instrument_identity,
            binding.position_mode,
            binding.margin_mode,
            binding.collateral_pool_id,
            fact_set_version,
        )
    )
    values: dict[str, Any] = {
        "risk_fact_set_id": uuid5(NAMESPACE_URL, identity),
        "organization_id": risk_request.organization_id,
        "venue": binding.venue,
        "execution_domain": binding.execution_domain,
        "account_id": binding.account_id,
        "canonical_instrument_id": binding.instrument_identity,
        "position_mode": binding.position_mode,
        "margin_mode": binding.margin_mode,
        "collateral_pool_id": binding.collateral_pool_id,
        "fact_set_version": fact_set_version,
        "observations": make_fact_observations(
            now=now,
            fact_status=fact_status,
            fact_age=fact_age,
            payload_hashes={FactType.MARKET: market_risk_fact_payload_hash(risk_request.market)},
        ),
        "environment": RiskFactSetEnvironment.SHADOW,
        "real_funds_eligible": False,
        "assembled_at": now,
        "valid_from": now,
        "valid_until": now + timedelta(days=1),
        "evidence_refs": (
            "test-only:complete-risk-fact-coverage",
            "test-only:risk-fact-source-health",
        ),
        "source_ref": "test-only:risk-fact-aggregator",
    }
    values.update(updates)
    draft = RegisterRiskFactSetDraft.model_validate(values)
    return RegisterRiskFactSetRequest.model_validate(
        {
            **draft.model_dump(mode="json"),
            "record_hash": risk_fact_set_record_hash(draft),
            "evidence_hash": risk_fact_set_evidence_hash(draft),
        }
    )


def risk_fact_set_envelope(
    request: RegisterRiskFactSetRequest,
    *,
    now: datetime,
    idempotency_key: str | None = None,
    service_principal: str = RISK_FACT_AGGREGATOR_SERVICE_PRINCIPAL,
) -> CommandEnvelope:
    return CommandEnvelope(
        idempotency_key=idempotency_key or f"risk-fact-set-{uuid4()}",
        command_type=RiskFactSetService.command_type,
        object_type="RiskFactSet",
        object_id=str(request.risk_fact_set_id),
        expected_version=1,
        service_principal=service_principal,
        channel=CommandChannel.INTERNAL,
        scope={"organization_id": request.organization_id},
        correlation_id=uuid4(),
        issued_at=now,
        expires_at=now + timedelta(minutes=2),
        auth_context_ref="test-only:risk-fact-aggregator-service",
        payload_schema_version=1,
        reason="register complete SHADOW-only risk fact set",
        payload=request.model_dump(mode="json"),
    )


def register_risk_fact_set(
    database: Database,
    request: RegisterRiskFactSetRequest,
    *,
    now: datetime,
    envelope: CommandEnvelope | None = None,
):
    return IdempotentCommandExecutor(database.session_factory).execute(
        envelope or risk_fact_set_envelope(request, now=now),
        RiskFactSetService(clock=lambda: now).register,
    )
