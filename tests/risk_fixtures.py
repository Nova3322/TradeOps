from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from trading_control_plane.authorization import RiskTier, SystemRiskState
from trading_control_plane.capability_certificates import CapabilityValidationResult
from trading_control_plane.capital_scope import (
    CapitalEnvironment,
    RegisterManagedCapitalScopeManifestDraft,
    RegisterManagedCapitalScopeManifestRequest,
    managed_capital_scope_evidence_hash,
    managed_capital_scope_manifest_hash,
)
from trading_control_plane.capital_scope import (
    RiskInclusionMode as CapitalScopeRiskInclusionMode,
)
from trading_control_plane.commands import hash_json
from trading_control_plane.projections import CurrentAccountEquityScope
from trading_control_plane.risk import (
    CANONICAL_LOSS_MODEL_VERSION,
    CapitalInput,
    CapitalProjectionBinding,
    CertificationBinding,
    FactFreshnessLimit,
    FactObservation,
    FactStatus,
    FactType,
    MarketRiskInput,
    PositionDirection,
    RequestedRiskIncrease,
    RiskEvaluationInput,
    RiskInclusionMode,
    RiskPolicyParameters,
    RiskPrecheckRequest,
    ScopeLimit,
    ScopeRiskInput,
    ScopeType,
    TradeLossComponents,
)

TEST_CAPITAL_SCOPE_MANIFEST_ID = UUID("00000000-0000-0000-0000-000000000020")
TEST_CAPITAL_SCOPE = CurrentAccountEquityScope(
    organization_id="org-1",
    venue="BINANCE",
    execution_domain="BINANCE_USDM",
    account_id="account-1",
    margin_mode="CROSS",
    collateral_pool_id="BINANCE:USDT-CROSS",
    settlement_currency="USD",
)
_TEST_CAPITAL_SCOPE_MANIFEST_DRAFT = RegisterManagedCapitalScopeManifestDraft(
    manifest_id=TEST_CAPITAL_SCOPE_MANIFEST_ID,
    organization_id="org-1",
    manifest_version=1,
    environment=CapitalEnvironment.SHADOW,
    real_funds_eligible=False,
    risk_inclusion_mode=CapitalScopeRiskInclusionMode.EXCHANGE_ONLY,
    report_currency="USD",
    account_scopes=(TEST_CAPITAL_SCOPE,),
    valid_from=datetime(2020, 1, 1, tzinfo=UTC),
    valid_until=datetime(2100, 1, 1, tzinfo=UTC),
    evidence_refs=("test-only:capital-scope-risk-binding",),
    source_ref="test-only:risk-capital-scope",
)
TEST_CAPITAL_SCOPE_MANIFEST = RegisterManagedCapitalScopeManifestRequest.model_validate(
    {
        **_TEST_CAPITAL_SCOPE_MANIFEST_DRAFT.model_dump(mode="json"),
        "manifest_hash": managed_capital_scope_manifest_hash(_TEST_CAPITAL_SCOPE_MANIFEST_DRAFT),
        "evidence_hash": managed_capital_scope_evidence_hash(_TEST_CAPITAL_SCOPE_MANIFEST_DRAFT),
    }
)
TEST_CAPITAL_PROJECTION_BINDING = CapitalProjectionBinding(
    manifest_id=TEST_CAPITAL_SCOPE_MANIFEST.manifest_id,
    manifest_version=TEST_CAPITAL_SCOPE_MANIFEST.manifest_version,
    manifest_hash=TEST_CAPITAL_SCOPE_MANIFEST.manifest_hash,
    projection_version="portfolio-mtm-v2",
)

TEST_EXECUTION_CAPITAL_SCOPE_MANIFEST_ID = UUID("00000000-0000-0000-0000-000000000022")
TEST_EXECUTION_CAPITAL_SCOPE = CurrentAccountEquityScope(
    organization_id="org-1",
    venue="BINANCE",
    execution_domain="BINANCE_USDM",
    account_id="account-1",
    margin_mode="ISOLATED",
    collateral_pool_id="pool-usdt-1",
    settlement_currency="USD",
)
_TEST_EXECUTION_CAPITAL_SCOPE_MANIFEST_DRAFT = RegisterManagedCapitalScopeManifestDraft(
    manifest_id=TEST_EXECUTION_CAPITAL_SCOPE_MANIFEST_ID,
    organization_id="org-1",
    manifest_version=1,
    environment=CapitalEnvironment.SHADOW,
    real_funds_eligible=False,
    risk_inclusion_mode=CapitalScopeRiskInclusionMode.EXCHANGE_ONLY,
    report_currency="USD",
    account_scopes=(TEST_EXECUTION_CAPITAL_SCOPE,),
    valid_from=datetime(2020, 1, 1, tzinfo=UTC),
    valid_until=datetime(2100, 1, 1, tzinfo=UTC),
    evidence_refs=("test-only:execution-capital-scope-binding",),
    source_ref="test-only:execution-capital-scope",
)
TEST_EXECUTION_CAPITAL_SCOPE_MANIFEST = RegisterManagedCapitalScopeManifestRequest.model_validate(
    {
        **_TEST_EXECUTION_CAPITAL_SCOPE_MANIFEST_DRAFT.model_dump(mode="json"),
        "manifest_hash": managed_capital_scope_manifest_hash(
            _TEST_EXECUTION_CAPITAL_SCOPE_MANIFEST_DRAFT
        ),
        "evidence_hash": managed_capital_scope_evidence_hash(
            _TEST_EXECUTION_CAPITAL_SCOPE_MANIFEST_DRAFT
        ),
    }
)
TEST_EXECUTION_CAPITAL_PROJECTION_BINDING = CapitalProjectionBinding(
    manifest_id=TEST_EXECUTION_CAPITAL_SCOPE_MANIFEST.manifest_id,
    manifest_version=TEST_EXECUTION_CAPITAL_SCOPE_MANIFEST.manifest_version,
    manifest_hash=TEST_EXECUTION_CAPITAL_SCOPE_MANIFEST.manifest_hash,
    projection_version="portfolio-mtm-v2",
)

SCOPE_IDS = {
    ScopeType.UNDERLYING: "BTC",
    ScopeType.RISK_CLUSTER: "CRYPTO_MAJOR",
    ScopeType.SECTOR: "CRYPTO",
    ScopeType.EXECUTION_DOMAIN: "BINANCE_USDM",
    ScopeType.VENUE: "BINANCE",
    ScopeType.COLLATERAL_POOL: "BINANCE:USDT-CROSS",
    ScopeType.PORTFOLIO: "org-1",
}


def make_policy(**updates: Any) -> RiskPolicyParameters:
    values: dict[str, Any] = {
        "one_r_fraction": Decimal("0.005"),
        "low_loss_multiplier": Decimal("1"),
        "medium_loss_multiplier": Decimal("2"),
        "high_loss_multiplier": Decimal("3"),
        "low_max_leverage": Decimal("3"),
        "medium_max_leverage": Decimal("5"),
        "high_max_leverage": Decimal("10"),
        # Test-only shadow input. No migration or runtime default uses this value.
        "trade_funding_pct": Decimal("0.02"),
        "absolute_trade_loss_cap": None,
        "consistency_window_ms": 1_000,
        "max_future_skew_ms": 1_000,
        "fact_freshness_limits": tuple(
            FactFreshnessLimit(fact_type=fact_type, max_age_ms=5_000) for fact_type in FactType
        ),
        "scope_limits": tuple(
            ScopeLimit(
                scope_type=scope_type,
                scope_id=scope_id,
                planned_loss_cap=Decimal("10000"),
                stress_loss_cap=Decimal("15000"),
            )
            for scope_type, scope_id in SCOPE_IDS.items()
        ),
    }
    values.update(updates)
    return RiskPolicyParameters.model_validate(values)


def make_capital(**updates: Any) -> CapitalInput:
    values: dict[str, Any] = {
        "risk_inclusion_mode": RiskInclusionMode.EXCHANGE_ONLY,
        "exchange_settled_equity_ex_upnl": Decimal("100000"),
        "current_unrealized_pnl": Decimal("0"),
        "eligible_vault_equity": Decimal("0"),
        "exchange_risk_equity": Decimal("100000"),
        "total_capital_snapshot_0": Decimal("100000"),
        "funding_used": Decimal("0"),
        "funding_reserved": Decimal("0"),
        "available_margin": Decimal("10000"),
    }
    values.update(updates)
    if "exchange_risk_equity" not in updates:
        settled = Decimal(values["exchange_settled_equity_ex_upnl"])
        unrealized = Decimal(values["current_unrealized_pnl"])
        values["exchange_risk_equity"] = max(Decimal("0"), min(settled, settled + unrealized))
    return CapitalInput.model_validate(values)


def make_requested(**updates: Any) -> RequestedRiskIncrease:
    values: dict[str, Any] = {
        "requested_quantity": Decimal("1"),
        "quantity_step": Decimal("0.001"),
        "requested_protected_profit_giveback": Decimal("0"),
        "requested_cost_stress_add_on": Decimal("10"),
        "requested_funding": Decimal("1000"),
        "requested_margin": Decimal("1000"),
        "requested_effective_leverage": Decimal("2"),
        "venue_leverage_cap": Decimal("20"),
        "proposal_requested_loss_cap": Decimal("500"),
    }
    values.update(updates)
    return RequestedRiskIncrease.model_validate(values)


def make_request(
    *,
    now: datetime | None = None,
    capital: CapitalInput | None = None,
    requested: RequestedRiskIncrease | None = None,
    current_trade_loss: TradeLossComponents | None = None,
    market: MarketRiskInput | None = None,
    risk_tier: RiskTier = RiskTier.LOW,
    fact_status: FactStatus = FactStatus.KNOWN,
    fact_age: timedelta = timedelta(milliseconds=100),
    scope_risks: tuple[ScopeRiskInput, ...] | None = None,
    instrument_classified: bool = True,
    protection_available: bool = True,
) -> RiskPrecheckRequest:
    observed_at = now or datetime.now(UTC)
    event_time = observed_at - fact_age
    effective_requested = requested or make_requested()
    effective_market = market or MarketRiskInput(
        direction=PositionDirection.LONG,
        mark_price=Decimal("100"),
        index_price=Decimal("100"),
        executable_price=Decimal("100.5"),
        initial_invalidation_price=Decimal("90"),
        contract_multiplier=Decimal("1"),
        tick_size=Decimal("0.1"),
        minimum_quantity=Decimal("0.001"),
        minimum_notional=Decimal("5"),
        funding_rate=Decimal("0.0001"),
        max_slippage_bps=Decimal("20"),
        contract_rules_version="rules-test-v1",
        loss_model_version=CANONICAL_LOSS_MODEL_VERSION,
        loss_calculation_ref="test-only:loss-calculation-fixture",
    )
    incremental_loss = (
        abs(effective_market.executable_price - effective_market.initial_invalidation_price)
        * effective_requested.requested_quantity
        * effective_market.contract_multiplier
        + effective_requested.requested_protected_profit_giveback
        + effective_requested.requested_cost_stress_add_on
    )
    if scope_risks is None:
        scope_risks = tuple(
            ScopeRiskInput(
                scope_type=scope_type,
                scope_id=scope_id,
                current_planned_loss=Decimal("0"),
                current_stress_loss=Decimal("0"),
                requested_incremental_stress_loss=incremental_loss + Decimal("40"),
            )
            for scope_type, scope_id in SCOPE_IDS.items()
        )
    return RiskPrecheckRequest(
        organization_id="org-1",
        proposal_ref="proposal-candidate-1",
        candidate_version=1,
        originating_caller_ref="user:test-proposer",
        originating_channel="WEB",
        originating_auth_context_ref="test-only:origin-auth",
        policy_version="risk-shadow-test-v1",
        risk_tier=risk_tier,
        binding=CertificationBinding(
            proposal_source="SYSTEM",
            strategy_id="strategy-test",
            strategy_version="strategy-test-v1",
            strategy_parameter_version="strategy-params-test-v1",
            authorization_policy_version="authorization-policy-test-v1",
            instrument_identity="BINANCE:BTCUSDT-PERP",
            contract_multiplier=Decimal("1"),
            underlying_id="BTC",
            sector_id="CRYPTO",
            risk_cluster_id="CRYPTO_MAJOR",
            venue="BINANCE",
            execution_domain="BINANCE_USDM",
            account_id="account-1",
            account_abstraction="STANDARD",
            position_mode="ONE_WAY",
            margin_mode="CROSS",
            collateral_scope="USDT",
            collateral_pool_id="BINANCE:USDT-CROSS",
            settlement_asset="USD",
            adapter_version="adapter-test-v1",
            worker_id="worker-test-1",
            worker_config_hash="a" * 64,
            credential_fingerprint="b" * 64,
            freqtrade_worker_version="worker-test-v1",
            account_capability_version="account-capability-test-v1",
            credential_permission_profile_version="trade-no-withdraw-test-v1",
            venue_client_version="venue-client-test-v1",
            instrument_scope_version="instrument-scope-test-v1",
            catalog_version="catalog-test-v1",
            execution_capability_version="shadow-only-test-v1",
            position_management_template_version="position-template-test-v1",
            add_milestone_policy_version="add-milestones-test-v1",
            requested_add_count=0,
            capability_certificate_ref="test-only:certificate-fixture",
        ),
        market=effective_market,
        capital_projection_binding=TEST_CAPITAL_PROJECTION_BINDING,
        capital=capital or make_capital(),
        current_trade_loss=current_trade_loss
        or TradeLossComponents(
            open_heat=Decimal("0"),
            reserved_heat=Decimal("0"),
            unknown_heat=Decimal("0"),
            protected_profit_giveback=Decimal("0"),
            cost_stress_add_on=Decimal("0"),
        ),
        requested=effective_requested,
        scope_risks=scope_risks,
        facts=tuple(
            FactObservation(
                fact_type=fact_type,
                status=fact_status,
                source_ref=f"test-only:{fact_type.value.lower()}",
                source_version="test-source-v1",
                payload_hash=hash_json(
                    {
                        "fact_type": fact_type.value,
                        "event_time": event_time.isoformat(),
                    }
                ),
                event_time=event_time,
                received_at=event_time + timedelta(milliseconds=10),
            )
            for fact_type in FactType
        ),
        instrument_classified=instrument_classified,
        protection_available=protection_available,
    )


def make_evaluation(
    *,
    now: datetime | None = None,
    request: RiskPrecheckRequest | None = None,
    policy: RiskPolicyParameters | None = None,
    system_risk_state: SystemRiskState = SystemRiskState.NORMAL,
    capability_valid: bool = True,
) -> RiskEvaluationInput:
    decision_time = now or datetime.now(UTC)
    certificate_valid_until = (
        decision_time + timedelta(days=1) if capability_valid else decision_time
    )
    return RiskEvaluationInput(
        request=request or make_request(now=decision_time),
        risk_policy_id=UUID("00000000-0000-0000-0000-000000000004"),
        policy=policy or make_policy(),
        policy_valid_from=decision_time - timedelta(days=1),
        policy_valid_until=decision_time + timedelta(days=1),
        system_risk_state=system_risk_state,
        capability_validation=CapabilityValidationResult(
            valid=capability_valid,
            certificate_id="test-only:certificate-fixture",
            status="ACTIVE" if capability_valid else "UNKNOWN",
            reason_codes=() if capability_valid else ("CAPABILITY_CERTIFICATE_NOT_FOUND",),
            certificate_hash="1" * 64 if capability_valid else None,
            scope_hash="2" * 64 if capability_valid else None,
            policy_versions_hash="3" * 64 if capability_valid else None,
            evidence_bundle_hash="4" * 64 if capability_valid else None,
            valid_until=certificate_valid_until,
            validation_snapshot={
                "certificate_id": "test-only:certificate-fixture",
                "valid": capability_valid,
            },
        ),
        decision_time=decision_time,
    )
