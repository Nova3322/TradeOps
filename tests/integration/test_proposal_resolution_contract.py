from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from workflow_builder import ActorSpec, WorkflowFixture

from trading_control_plane.database import Database
from trading_control_plane.domain import ExecutionEnvironment, RiskTier, Role
from trading_control_plane.models import (
    OrderIntent,
    Proposal,
    RiskDecision,
    TradingAuthorization,
)
from trading_control_plane.service import TradingService


@pytest.mark.parametrize(
    ("risk_tier", "expected_leverage"),
    (
        (RiskTier.HIGH, Decimal(10)),
        (RiskTier.MEDIUM, Decimal(5)),
        (RiskTier.LOW, Decimal(3)),
    ),
)
def test_risk_tier_leverage_is_frozen_through_intent_after_environment_change(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
    risk_tier: RiskTier,
    expected_leverage: Decimal,
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    service = TradingService(database)
    fixture = WorkflowFixture.create(
        service,
        now=now,
        admin_username="leverage-admin",
        account_id="leverage-account",
        venue="BINANCE",
        environment=ExecutionEnvironment.TESTNET,
        actors=(
            ActorSpec("proposer", "leverage-proposer", Role.PROPOSER),
            ActorSpec("reviewer_one", "leverage-reviewer-1", Role.REVIEWER),
            ActorSpec("reviewer_two", "leverage-reviewer-2", Role.REVIEWER),
            ActorSpec("operator", "leverage-operator", Role.OPERATOR),
        ),
        symbol="MIRAUSDT",
        tick_size=Decimal("0.0001"),
        lot_size=Decimal(1),
        minimum_notional=Decimal(5),
        quote_currency="USDT",
        risk_version="leverage-risk-v1",
        max_fact_age=timedelta(minutes=10),
        mark_price=Decimal(10),
    )
    proposal_id = fixture.approved_proposal(
        key=f"leverage-{risk_tier.value.lower()}",
        risk_tier=risk_tier,
        max_risk=Decimal(10),
    )

    monkeypatch.setenv("TRADING_FREQTRADE_LIVE_LEVERAGE", "99")
    order = fixture.opening_order(
        proposal=proposal_id,
        key=f"leverage-{risk_tier.value.lower()}",
    )

    with database.session_factory() as session:
        proposal = session.get(Proposal, proposal_id)
        decision = session.scalar(
            select(RiskDecision).where(RiskDecision.proposal_id == proposal_id)
        )
        authorization = session.scalar(
            select(TradingAuthorization).where(
                TradingAuthorization.proposal_id == proposal_id
            )
        )
        intent = session.get(OrderIntent, order.intent_id)
        assert proposal is not None
        assert decision is not None
        assert authorization is not None
        assert intent is not None
        assert {
            proposal.leverage,
            decision.leverage,
            authorization.leverage,
            intent.leverage,
        } == {expected_leverage}
