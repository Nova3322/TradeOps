from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

from conftest import add_exchange_account_fixture, set_test_team_environment

from trading_control_plane.domain import (
    Direction,
    ExecutionEnvironment,
    IntentKind,
    ProposalSource,
    ReviewDecision,
    RiskTier,
    Role,
    SystemRiskState,
)
from trading_control_plane.service import TradingService


@dataclass(frozen=True)
class ActorSpec:
    key: str
    username: str
    role: Role
    scoped: bool = True
    service_principal: bool = False


@dataclass
class WorkflowFixture:
    """One builder for the shared team, account, proposal, and order contract."""

    service: TradingService
    now: datetime
    account_id: str
    venue: str
    environment: ExecutionEnvironment
    ids: dict[str, UUID]

    @classmethod
    def create(
        cls,
        service: TradingService,
        *,
        now: datetime,
        admin_username: str,
        account_id: str,
        venue: str,
        environment: ExecutionEnvironment = ExecutionEnvironment.TESTNET,
        actors: tuple[ActorSpec, ...],
        symbol: str,
        tick_size: Decimal,
        lot_size: Decimal,
        minimum_notional: Decimal,
        quote_currency: str,
        risk_version: str,
        max_fact_age: timedelta,
        mark_price: Decimal = Decimal("100"),
        record_facts: bool = True,
    ) -> WorkflowFixture:
        admin = service.bootstrap_admin(admin_username, now=now)
        set_test_team_environment(service.database, admin, environment.value)
        add_exchange_account_fixture(
            service.database,
            admin,
            account_id,
            venue,
            environment=environment.value,
        )
        ids = {"admin": admin}
        for actor in actors:
            actor_id = (
                service.create_service_principal(actor.username, admin, now=now)
                if actor.service_principal
                else service.create_user(actor.username, admin, now=now)
            )
            service.assign_role(
                actor_id,
                actor.role,
                admin,
                account_id if actor.scoped else None,
                venue if actor.scoped else None,
                now=now,
            )
            ids[actor.key] = actor_id
        instrument = service.register_instrument(
            actor_id=admin,
            venue=venue,
            symbol=symbol,
            tick_size=tick_size,
            lot_size=lot_size,
            minimum_notional=minimum_notional,
            contract_multiplier=Decimal(1),
            quote_currency=quote_currency,
            collateral_currency=quote_currency,
            protection_supported=True,
            now=now,
        )
        ids["instrument"] = instrument
        service.set_risk_policy(
            actor_id=admin,
            version=risk_version,
            system_state=SystemRiskState.NORMAL,
            max_total_risk=Decimal(100),
            max_account_risk=Decimal(100),
            max_single_loss=Decimal(100),
            max_consecutive_losses=3,
            loss_cooldown=timedelta(hours=1),
            max_fact_age=max_fact_age,
            now=now,
        )
        fixture = cls(service, now, account_id, venue, environment, ids)
        if record_facts:
            fixture.record_flat_facts(mark_price=mark_price)
        return fixture

    def record_flat_facts(
        self,
        *,
        mark_price: Decimal,
        equity: Decimal = Decimal("10000"),
        available: Decimal = Decimal("9000"),
    ) -> None:
        operator = self.ids["operator"]
        self.ids["position"] = self.service.record_position(
            self.account_id,
            self.venue,
            self.ids["instrument"],
            Decimal(0),
            Decimal(0),
            mark_price,
            True,
            operator,
            environment=self.environment,
            now=self.now,
        )
        self.service.record_account_equity(
            self.account_id,
            self.venue,
            equity,
            available,
            "USDC" if self.venue == "HYPERLIQUID" else "USDT",
            True,
            operator,
            environment=self.environment,
            now=self.now,
        )

    def approved_proposal(
        self,
        *,
        key: str,
        risk_tier: RiskTier = RiskTier.HIGH,
        direction: Direction = Direction.LONG,
        quantity: Decimal = Decimal(1),
        max_risk: Decimal = Decimal(40),
        details: dict[str, object] | None = None,
    ) -> UUID:
        proposal = self.service.create_proposal(
            actor_id=self.ids["proposer"],
            source=ProposalSource.MANUAL,
            risk_tier=risk_tier,
            account_id=self.account_id,
            venue=self.venue,
            instrument_id=self.ids["instrument"],
            direction=direction,
            quantity=quantity,
            max_risk=max_risk,
            expires_at=self.now + timedelta(hours=2),
            idempotency_key=f"{key}-proposal",
            environment=self.environment,
            details=details,
            now=self.now,
        )
        self.service.submit_proposal(proposal, self.ids["proposer"], now=self.now)
        self.service.review_proposal(
            proposal,
            self.ids["reviewer_one"],
            ReviewDecision.APPROVE,
            "first",
            now=self.now,
        )
        if risk_tier is RiskTier.HIGH:
            self.service.review_proposal(
                proposal,
                self.ids["reviewer_two"],
                ReviewDecision.APPROVE,
                "second",
                now=self.now,
            )
        return proposal

    def opening_order(
        self,
        *,
        proposal: UUID,
        key: str,
        quantity: Decimal = Decimal(1),
        direction: Direction = Direction.LONG,
        allowed_adds: int = 0,
    ):
        self.service.decide_risk(
            proposal_id=proposal,
            actor_id=self.ids["operator"],
            kind=IntentKind.INITIAL,
            idempotency_key=f"{key}-risk",
            now=self.now,
        )
        authorization = self.service.issue_authorization(
            proposal_id=proposal,
            actor_id=self.ids["operator"],
            expires_at=self.now + timedelta(minutes=30),
            allowed_adds=allowed_adds,
            idempotency_key=f"{key}-authorization",
            now=self.now,
        )
        return self.service.create_order_intent(
            authorization,
            self.ids["operator"],
            IntentKind.INITIAL,
            self.account_id,
            self.venue,
            self.ids["instrument"],
            direction,
            quantity,
            f"{key}-opening",
            now=self.now,
        )
