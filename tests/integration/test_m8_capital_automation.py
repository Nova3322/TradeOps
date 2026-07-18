from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from test_m7_capital_center import build_app, login, seed

from trading_control_plane.database import Database
from trading_control_plane.domain import (
    CampaignStatus,
    CapabilityStatus,
    Direction,
    DomainRejected,
    ExecutionEnvironment,
    IntentKind,
    OrderIntentStatus,
    ProposalSource,
    ReservationStatus,
    ReviewDecision,
    RiskTier,
    Role,
)
from trading_control_plane.models import (
    Campaign,
    CapitalAutomationPolicy,
    OrderIntent,
    RiskReservation,
    TransferProposal,
)
from trading_control_plane.service import TradingService
from trading_control_plane.telegram import MockTelegramGateway


def policy(service: TradingService, actor_id, *, now: datetime) -> object:
    return service.set_capital_automation_policy(
        actor_id=actor_id,
        environment=ExecutionEnvironment.TESTNET,
        account_id="acct-1",
        venue="BINANCE",
        vault_id="vault-1",
        asset="USDT",
        network="TESTNET",
        vault_destination_reference="approved-test-vault",
        venue_destination_reference="approved-test-venue",
        operating_low=Decimal("400"),
        operating_target=Decimal("500"),
        operating_high=Decimal("600"),
        vault_minimum_reserve=Decimal("500"),
        minimum_transfer=Decimal("10"),
        maximum_transfer=Decimal("200"),
        max_fee=Decimal("1"),
        idempotency_key="m8-policy",
        now=now,
    )


def close_profitable_testnet_campaign(
    database: Database,
    service: TradingService,
    ids: dict[str, object],
    *,
    now: datetime,
) -> None:
    proposer = ids["proposer"]
    reviewer = ids["reviewer_one"]
    operator = ids["operator"]
    instrument = ids["instrument"]
    admin = ids["admin"]
    service.assign_role(proposer, Role.PROPOSER, admin, "acct-1", "BINANCE", now=now)
    service.assign_role(reviewer, Role.REVIEWER, admin, "acct-1", "BINANCE", now=now)
    proposal_id = service.create_proposal(
        actor_id=proposer,
        source=ProposalSource.MANUAL,
        risk_tier=RiskTier.LOW,
        account_id="acct-1",
        venue="BINANCE",
        instrument_id=instrument,
        direction=Direction.LONG,
        quantity=Decimal("1"),
        max_risk=Decimal("10"),
        expires_at=now + timedelta(hours=2),
        idempotency_key="m8-trade-proposal",
        environment=ExecutionEnvironment.TESTNET,
        now=now,
    )
    service.submit_proposal(proposal_id, proposer, now=now)
    service.review_proposal(
        proposal_id,
        reviewer,
        ReviewDecision.APPROVE,
        "independent test review",
        now=now,
    )
    service.decide_risk(
        proposal_id=proposal_id,
        actor_id=operator,
        kind=IntentKind.INITIAL,
        idempotency_key="m8-risk",
        now=now,
    )
    authorization_id = service.issue_authorization(
        proposal_id=proposal_id,
        actor_id=operator,
        expires_at=now + timedelta(minutes=30),
        allowed_adds=0,
        idempotency_key="m8-trade-authorization",
        now=now,
    )
    created = service.create_order_intent(
        authorization_id,
        operator,
        IntentKind.INITIAL,
        "acct-1",
        "BINANCE",
        instrument,
        Direction.LONG,
        Decimal("1"),
        "m8-initial-intent",
        now=now,
    )
    with database.session_factory.begin() as session:
        campaign = session.get(Campaign, created.campaign_id)
        intent = session.get(OrderIntent, created.intent_id)
        reservation = session.get(RiskReservation, created.reservation_id)
        assert campaign is not None and intent is not None and reservation is not None
        campaign.status = CampaignStatus.CLOSED.value
        campaign.realized_pnl = Decimal("180")
        campaign.unrealized_pnl = Decimal(0)
        campaign.final_pnl = Decimal("180")
        campaign.updated_at = now
        intent.status = OrderIntentStatus.CANCELLED.value
        intent.updated_at = now
        reservation.status = ReservationStatus.RELEASED.value
        reservation.updated_at = now


def test_profit_sweep_candidate_uses_closed_campaign_pnl_and_still_requires_reviews(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)
    now = datetime.now(UTC)
    policy_id = policy(service, ids["proposer"], now=now)
    with pytest.raises(DomainRejected, match="CAPITAL_AUTOMATION_DISABLED"):
        service.create_capital_automation_candidate(
            policy_id, "AUTO_PROFIT_SWEEP", ids["proposer"], "m8-disabled", now=now
        )

    close_profitable_testnet_campaign(database, service, ids, now=now)
    service.record_capital_balance(
        actor_id=ids["proposer"],
        environment=ExecutionEnvironment.TESTNET,
        location_type="VENUE",
        location_id="acct-1",
        venue="BINANCE",
        equity=Decimal("800"),
        available_balance=Decimal("800"),
        withdrawable_balance=Decimal("750"),
        asset="USDT",
        control_status="READ_ONLY",
        deposit_status="READY",
        network="TESTNET",
        address_reference="masked-venue",
        known=True,
        observed_at=now,
        now=now,
    )
    service.record_capital_scope_reconciliation(
        actor_id=ids["proposer"],
        environment=ExecutionEnvironment.TESTNET,
        account_id="acct-1",
        venue="BINANCE",
        now=now,
    )
    service.set_capability_gate(
        "AUTO_PROFIT_SWEEP",
        CapabilityStatus.ENABLED,
        "M8 non-production test",
        ids["admin"],
        now=now,
    )

    proposal_id, reason = service.create_capital_automation_candidate(
        policy_id, "AUTO_PROFIT_SWEEP", ids["proposer"], "m8-sweep", now=now
    )
    assert proposal_id is not None and reason == "CANDIDATE_READY"
    assert service.create_capital_automation_candidate(
        policy_id, "AUTO_PROFIT_SWEEP", ids["proposer"], "m8-sweep", now=now
    ) == (proposal_id, reason)
    with database.session_factory() as session:
        proposal = session.get(TransferProposal, proposal_id)
        assert proposal is not None
        assert proposal.purpose == "AUTO_PROFIT_SWEEP"
        assert proposal.status == "PENDING_REVIEW"
        assert proposal.direction == "VENUE_TO_VAULT"
        assert proposal.amount == Decimal("180")
        assert proposal.frozen_payload["confirmed_realized_pnl"] == "180.000000000000000000"

    with pytest.raises(DomainRejected, match="SELF_REVIEW_FORBIDDEN"):
        service.review_transfer_proposal(
            proposal_id,
            ids["proposer"],
            ReviewDecision.APPROVE,
            "self",
            1,
            now=now,
        )
    assert (
        service.review_transfer_proposal(
            proposal_id,
            ids["reviewer_one"],
            ReviewDecision.APPROVE,
            "first",
            1,
            now=now,
        ).value
        == "PENDING_REVIEW"
    )
    assert (
        service.review_transfer_proposal(
            proposal_id,
            ids["reviewer_two"],
            ReviewDecision.APPROVE,
            "second",
            2,
            now=now,
        ).value
        == "APPROVED"
    )


def test_operating_refill_is_independent_default_off_and_never_rescues_position(
    service: TradingService,
) -> None:
    ids = seed(service)
    now = datetime.now(UTC)
    policy_id = policy(service, ids["proposer"], now=now)
    service.set_capability_gate(
        "AUTO_OPERATING_REFILL",
        CapabilityStatus.ENABLED,
        "M8 non-production test",
        ids["admin"],
        now=now,
    )
    service.record_capital_balance(
        actor_id=ids["proposer"],
        environment=ExecutionEnvironment.TESTNET,
        location_type="VENUE",
        location_id="acct-1",
        venue="BINANCE",
        equity=Decimal("300"),
        available_balance=Decimal("300"),
        withdrawable_balance=Decimal("300"),
        asset="USDT",
        control_status="READ_ONLY",
        deposit_status="READY",
        network="TESTNET",
        address_reference="masked-venue",
        known=True,
        observed_at=now,
        now=now,
    )
    service.record_capital_scope_reconciliation(
        actor_id=ids["proposer"],
        environment=ExecutionEnvironment.TESTNET,
        account_id="acct-1",
        venue="BINANCE",
        now=now,
    )
    service.record_position(
        "acct-1",
        "BINANCE",
        ids["instrument"],
        Decimal("1"),
        Decimal("100"),
        Decimal("90"),
        True,
        ids["operator"],
        environment=ExecutionEnvironment.TESTNET,
        now=now,
    )
    with pytest.raises(DomainRejected, match="ACTIVE_POSITION_CAPITAL_RESCUE_FORBIDDEN"):
        service.create_capital_automation_candidate(
            policy_id, "AUTO_OPERATING_REFILL", ids["proposer"], "m8-refill-blocked", now=now
        )
    service.record_position(
        "acct-1",
        "BINANCE",
        ids["instrument"],
        Decimal(0),
        Decimal(0),
        Decimal("90"),
        True,
        ids["operator"],
        environment=ExecutionEnvironment.TESTNET,
        now=now,
    )
    service.record_capital_scope_reconciliation(
        actor_id=ids["proposer"],
        environment=ExecutionEnvironment.TESTNET,
        account_id="acct-1",
        venue="BINANCE",
        now=now,
    )
    proposal_id, _ = service.create_capital_automation_candidate(
        policy_id, "AUTO_OPERATING_REFILL", ids["proposer"], "m8-refill", now=now
    )
    assert proposal_id is not None
    with service.database.session_factory() as session:
        proposal = session.get(TransferProposal, proposal_id)
        assert proposal is not None
        assert proposal.direction == "VAULT_TO_VENUE"
        assert proposal.amount == Decimal("200")


def test_capital_automation_api_exposes_disabled_gates_and_policy(
    database: Database, service: TradingService
) -> None:
    seed(service)
    telegram = MockTelegramGateway()

    async def scenario() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=build_app(database, telegram)),
            base_url="http://test",
        ) as client:
            await login(client, "treasury-proposer")
            created = await client.post(
                "/api/capital/automation/policies",
                json={
                    "environment": "TESTNET",
                    "account_id": "acct-1",
                    "venue": "BINANCE",
                    "vault_id": "vault-1",
                    "asset": "USDT",
                    "network": "TESTNET",
                    "vault_destination_reference": "approved-test-vault",
                    "venue_destination_reference": "approved-test-venue",
                    "operating_low": "400",
                    "operating_target": "500",
                    "operating_high": "600",
                    "vault_minimum_reserve": "500",
                    "minimum_transfer": "10",
                    "maximum_transfer": "200",
                    "max_fee": "1",
                    "idempotency_key": "m8-api-policy",
                },
            )
            assert created.status_code == 200, created.text
            policy_id = created.json()["policy_id"]
            center = await client.get("/api/capital")
            assert center.status_code == 200
            automation = center.json()["data"]["automation"]
            assert automation["gates"] == {
                "AUTO_PROFIT_SWEEP": "DISABLED",
                "AUTO_OPERATING_REFILL": "DISABLED",
            }
            assert automation["policies"][0]["policy_id"] == policy_id
            denied = await client.post(
                f"/api/capital/automation/policies/{policy_id}/evaluate",
                json={
                    "purpose": "AUTO_PROFIT_SWEEP",
                    "idempotency_key": "m8-api-disabled",
                },
            )
            assert denied.status_code == 422
            assert denied.json()["error"]["code"] == "CAPITAL_AUTOMATION_DISABLED"
            web = await client.get("/capital")
            assert web.status_code == 200
            assert "Trading Console" in web.text

    asyncio.run(scenario())


def test_capital_automation_policy_is_single_authoritative_scope(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)
    now = datetime.now(UTC)
    first = policy(service, ids["proposer"], now=now)
    same = service.set_capital_automation_policy(
        actor_id=ids["proposer"],
        environment=ExecutionEnvironment.TESTNET,
        account_id="acct-1",
        venue="BINANCE",
        vault_id="vault-1",
        asset="USDT",
        network="TESTNET",
        vault_destination_reference="new-test-vault",
        venue_destination_reference="new-test-venue",
        operating_low=Decimal("350"),
        operating_target=Decimal("500"),
        operating_high=Decimal("650"),
        vault_minimum_reserve=Decimal("550"),
        minimum_transfer=Decimal("20"),
        maximum_transfer=Decimal("150"),
        max_fee=Decimal("1"),
        idempotency_key="m8-policy-update",
        now=now,
    )
    assert same == first
    with database.session_factory() as session:
        policies = session.query(CapitalAutomationPolicy).all()
        assert len(policies) == 1
        assert policies[0].version == 2
        assert policies[0].operating_high == Decimal("650")
