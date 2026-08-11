from __future__ import annotations

import asyncio
import base64
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from trading_control_plane.api import create_app
from trading_control_plane.config import Settings
from trading_control_plane.database import Database
from trading_control_plane.domain import (
    Direction,
    DomainRejected,
    ExecutionEnvironment,
    IntentKind,
    ProposalSource,
    ReviewDecision,
    RiskTier,
    Role,
    SignalSourceMode,
    SystemRiskState,
)
from trading_control_plane.models import (
    AccountEquity,
    AuditEvent,
    Campaign,
    Position,
    ProtectionOrder,
    Team,
    VenueFill,
    VenueOrder,
)
from trading_control_plane.perptape import PerptapeClient
from trading_control_plane.queries import TradingQueries
from trading_control_plane.service import TradingService

NOW = datetime(2026, 8, 10, 8, tzinfo=UTC)


def encryption_key() -> str:
    return base64.urlsafe_b64encode(b"shadow-mode-integration-key-32by"[:32]).decode().rstrip("=")


def shadow_app(database: Database):
    settings = Settings(
        environment="test",
        database_url=str(database.engine.url),
        allow_mock_identity=True,
        session_signing_secret=secrets.token_urlsafe(32),
        credential_encryption_key=encryption_key(),
        public_base_url="http://test",
        runtime_sync_enabled=False,
        _env_file=None,
    )
    perptape = PerptapeClient(
        base_url="https://perptape.com",
        api_key=None,
        contract_version="breakouts-v1",
        cache_ttl=timedelta(minutes=1),
        fetcher=lambda *_args: {"data": []},
    )
    return create_app(settings, database, perptape)


def shadow_team_fixture(database: Database) -> tuple[TradingService, dict[str, UUID]]:
    service = TradingService(database, credential_encryption_key=encryption_key())
    admin = service.bootstrap_admin("shadow-admin", now=NOW)
    instrument = service.register_instrument(
        actor_id=admin,
        venue="BINANCE",
        symbol="BTCUSDT",
        tick_size=Decimal("0.1"),
        lot_size=Decimal("0.001"),
        minimum_notional=Decimal("5"),
        contract_multiplier=Decimal("1"),
        quote_currency="USDT",
        collateral_currency="USDT",
        protection_supported=True,
        now=NOW,
    )
    team = service.create_team(
        actor_id=admin,
        name="Shadow Desk",
        slug="shadow-desk",
        idempotency_key="create-shadow-desk",
        now=NOW,
    )
    proposer = service.create_user("shadow-proposer", admin, now=NOW)
    reviewer = service.create_user("shadow-reviewer", admin, now=NOW)
    operator = service.create_user("shadow-operator", admin, now=NOW)
    service.assign_role(proposer, Role.PROPOSER, admin, "paper-1", "BINANCE", now=NOW)
    service.assign_role(reviewer, Role.REVIEWER, admin, "paper-1", "BINANCE", now=NOW)
    service.assign_role(operator, Role.OPERATOR, admin, "paper-1", "BINANCE", now=NOW)
    return service, {
        "admin": admin,
        "team": team,
        "proposer": proposer,
        "reviewer": reviewer,
        "operator": operator,
        "instrument": instrument,
    }


def configure_shadow_prerequisites(service: TradingService, ids: dict[str, UUID]) -> None:
    service.configure_signal_source(
        actor_id=ids["admin"],
        mode=SignalSourceMode.WEBHOOK,
        secret="shadow-webhook-secret-0123456789abcdef",  # noqa: S106
        enabled=True,
        webhook_max_age_seconds=300,
        expected_version=0,
        idempotency_key="shadow-source",
        now=NOW,
    )
    service.create_exchange_account(
        actor_id=ids["admin"],
        account_id="paper-1",
        venue="BINANCE",
        label="Virtual Binance",
        credentials=None,
        idempotency_key="shadow-account",
        now=NOW,
    )
    service.set_risk_policy(
        actor_id=ids["admin"],
        version="shadow-risk-v1",
        system_state=SystemRiskState.NORMAL,
        max_total_risk=Decimal("1000"),
        max_account_risk=Decimal("500"),
        max_single_loss=Decimal("100"),
        max_consecutive_losses=3,
        loss_cooldown=timedelta(hours=1),
        max_fact_age=timedelta(minutes=5),
        now=NOW,
    )


def test_shadow_activation_requires_explicit_prerequisites_and_is_idempotent(
    database: Database,
) -> None:
    service, ids = shadow_team_fixture(database)

    blocked = service.shadow_activation_status(ids["admin"])
    assert blocked["execution_mode"] == "SETUP"
    assert set(blocked["blockers"]) == {
        "SIGNAL_SOURCE_REQUIRED",
        "RISK_POLICY_REQUIRED",
        "EXCHANGE_ACCOUNT_REQUIRED",
        "INDEPENDENT_REVIEWER_REQUIRED",
        "OPERATOR_REQUIRED",
    }

    with pytest.raises(DomainRejected, match="TEAM_SHADOW_PREREQUISITES_MISSING"):
        service.activate_team_shadow_mode(
            actor_id=ids["admin"],
            team_id=ids["team"],
            expected_version=1,
            idempotency_key="activate-too-early",
            now=NOW,
        )

    configure_shadow_prerequisites(service, ids)
    activated = service.activate_team_shadow_mode(
        actor_id=ids["admin"],
        team_id=ids["team"],
        expected_version=1,
        idempotency_key="activate-shadow",
        now=NOW,
    )
    replay = service.activate_team_shadow_mode(
        actor_id=ids["admin"],
        team_id=ids["team"],
        expected_version=1,
        idempotency_key="activate-shadow",
        now=NOW,
    )
    no_op = service.activate_team_shadow_mode(
        actor_id=ids["admin"],
        team_id=ids["team"],
        expected_version=2,
        idempotency_key="activate-shadow-again",
        now=NOW,
    )

    assert activated == replay
    assert activated["execution_mode"] == "SHADOW"
    assert activated["version"] == 2
    assert no_op["version"] == 2
    with database.session_factory() as session:
        team = session.get(Team, ids["team"])
        assert team is not None
        assert team.execution_mode == "SHADOW"
        assert team.trading_enabled is True


def test_shadow_virtual_capital_execution_and_reports_are_strictly_isolated(
    database: Database,
) -> None:
    service, ids = shadow_team_fixture(database)
    configure_shadow_prerequisites(service, ids)
    service.activate_team_shadow_mode(
        actor_id=ids["admin"],
        team_id=ids["team"],
        expected_version=1,
        idempotency_key="activate-shadow-flow",
        now=NOW,
    )
    initialized = service.initialize_shadow_scope(
        actor_id=ids["admin"],
        account_id="paper-1",
        venue="BINANCE",
        instrument_id=ids["instrument"],
        currency="USDT",
        initial_equity=Decimal("10000"),
        idempotency_key="initialize-shadow-scope",
        now=NOW,
    )
    assert (
        service.initialize_shadow_scope(
            actor_id=ids["admin"],
            account_id="paper-1",
            venue="BINANCE",
            instrument_id=ids["instrument"],
            currency="USDT",
            initial_equity=Decimal("10000"),
            idempotency_key="initialize-shadow-scope",
            now=NOW,
        )
        == initialized
    )

    with pytest.raises(DomainRejected, match="SHADOW_EQUITY_ALREADY_INITIALIZED"):
        service.initialize_shadow_scope(
            actor_id=ids["admin"],
            account_id="paper-1",
            venue="BINANCE",
            instrument_id=ids["instrument"],
            currency="USDT",
            initial_equity=Decimal("20000"),
            idempotency_key="reset-shadow-capital",
            now=NOW,
        )

    with pytest.raises(DomainRejected, match="SHADOW_FACTS_SIMULATOR_MANAGED"):
        service.record_account_equity(
            account_id="paper-1",
            venue="BINANCE",
            equity=Decimal("20000"),
            available_balance=Decimal("20000"),
            currency="USDT",
            known=True,
            actor_id=ids["admin"],
            environment=ExecutionEnvironment.SHADOW,
            now=NOW,
        )
    with pytest.raises(DomainRejected, match="SHADOW_FACTS_SIMULATOR_MANAGED"):
        service.record_position(
            account_id="paper-1",
            venue="BINANCE",
            instrument_id=ids["instrument"],
            quantity=Decimal("999"),
            average_entry_price=Decimal("1"),
            mark_price=Decimal("1"),
            known=True,
            actor_id=ids["admin"],
            environment=ExecutionEnvironment.SHADOW,
            now=NOW,
        )

    proposal = service.create_proposal(
        actor_id=ids["proposer"],
        source=ProposalSource.MANUAL,
        risk_tier=RiskTier.LOW,
        account_id="paper-1",
        venue="BINANCE",
        instrument_id=ids["instrument"],
        direction=Direction.LONG,
        quantity=Decimal("1"),
        max_risk=Decimal("20"),
        expires_at=NOW + timedelta(hours=8),
        idempotency_key="shadow-proposal",
        environment=ExecutionEnvironment.SHADOW,
        details={
            "trigger_price": "100",
            "invalidation_price": "90",
            "initial_quantity": "1",
            "allow_auto_add": False,
            "requested_adds": 0,
            "rationale": "exercise the isolated shadow lifecycle",
        },
        now=NOW,
    )
    service.submit_proposal(proposal, ids["proposer"], now=NOW)
    service.review_proposal(
        proposal,
        ids["reviewer"],
        ReviewDecision.APPROVE,
        "independent shadow review",
        now=NOW,
    )
    service.decide_risk(
        proposal_id=proposal,
        actor_id=ids["operator"],
        kind=IntentKind.INITIAL,
        idempotency_key="shadow-risk",
        now=NOW,
    )
    authorization = service.issue_authorization(
        proposal_id=proposal,
        actor_id=ids["operator"],
        expires_at=NOW + timedelta(minutes=30),
        allowed_adds=0,
        idempotency_key="shadow-authorization",
        now=NOW,
    )
    intent = service.create_order_intent(
        authorization_id=authorization,
        actor_id=ids["operator"],
        kind=IntentKind.INITIAL,
        account_id="paper-1",
        venue="BINANCE",
        instrument_id=ids["instrument"],
        direction=Direction.LONG,
        quantity=Decimal("1"),
        idempotency_key="shadow-intent",
        now=NOW,
    )
    simulation = service.simulate_shadow_execution(
        intent_id=intent.intent_id,
        actor_id=ids["operator"],
        expected_version=1,
        reference_price=Decimal("100"),
        fee_bps=Decimal("4"),
        slippage_bps=Decimal("2"),
        idempotency_key="shadow-simulation",
        now=NOW + timedelta(seconds=1),
    )
    assert (
        service.simulate_shadow_execution(
            intent_id=intent.intent_id,
            actor_id=ids["operator"],
            expected_version=1,
            reference_price=Decimal("100"),
            fee_bps=Decimal("4"),
            slippage_bps=Decimal("2"),
            idempotency_key="shadow-simulation",
            now=NOW + timedelta(seconds=1),
        )
        == simulation
    )

    queries = TradingQueries(database)
    workspace = queries.shadow_workspace(ids["admin"])
    assert workspace["execution_mode"] == "SHADOW"
    assert workspace["safety_boundary"]["capital"] == "VIRTUAL_ONLY"
    assert workspace["safety_boundary"]["venue_connectors_used"] is False
    assert all(
        workspace["safety_boundary"][key] is False
        for key in ("live_order_send", "funding", "signing", "broadcast")
    )
    assert workspace["accounts"][0]["credential_state"] == "UNCONFIGURED"
    assert workspace["accounts"][0]["virtual_capital"][0]["equity"] == ("9999.859960000000000000")
    assert len(workspace["campaigns"]) == 1
    assert len(queries.actual_results(ids["admin"], "SHADOW")["campaigns"]) == 1
    assert queries.actual_results(ids["admin"], "LIVE")["campaigns"] == []

    with pytest.raises(DomainRejected, match="TEAM_SHADOW_ONLY"):
        service.create_proposal(
            actor_id=ids["proposer"],
            source=ProposalSource.MANUAL,
            risk_tier=RiskTier.LOW,
            account_id="paper-1",
            venue="BINANCE",
            instrument_id=ids["instrument"],
            direction=Direction.LONG,
            quantity=Decimal("1"),
            max_risk=Decimal("10"),
            expires_at=NOW + timedelta(hours=8),
            idempotency_key="live-proposal-blocked",
            environment=ExecutionEnvironment.LIVE,
            now=NOW,
        )

    with database.session_factory() as session:
        campaign = session.get(Campaign, intent.campaign_id)
        equity = session.get(AccountEquity, UUID(initialized["account_equity_id"]))
        position = session.get(Position, UUID(initialized["position_id"]))
        assert campaign is not None and campaign.environment == "SHADOW"
        assert equity is not None and equity.environment == "SHADOW"
        assert position is not None and position.environment == "SHADOW"
        assert position.quantity == Decimal("1")
        assert session.scalar(select(func.count()).select_from(VenueOrder)) == 1
        assert session.scalar(select(func.count()).select_from(VenueFill)) == 1
        assert session.scalar(select(func.count()).select_from(ProtectionOrder)) == 1
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.event_type == "SHADOW_EXECUTION_SIMULATED")
            )
            == 1
        )


def test_shadow_http_api_exposes_activation_scope_and_real_page(
    database: Database,
) -> None:
    service, ids = shadow_team_fixture(database)
    configure_shadow_prerequisites(service, ids)

    async def scenario() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=shadow_app(database)),
            base_url="http://test",
        ) as client:
            login = await client.post("/api/auth/mock/login", json={"username": "shadow-admin"})
            assert login.status_code == 200, login.text

            before = await client.get("/api/shadow")
            assert before.status_code == 200, before.text
            assert before.json()["data"]["execution_mode"] == "SETUP"
            assert before.json()["data"]["activation"]["ready"] is True

            activated = await client.post(
                f"/api/teams/{ids['team']}/shadow-activation",
                json={"expected_version": 1, "idempotency_key": "api-shadow-activation"},
            )
            assert activated.status_code == 200, activated.text
            assert activated.json()["session"]["active_team"]["execution_mode"] == "SHADOW"

            initialized = await client.post(
                "/api/shadow/scopes",
                json={
                    "account_id": "paper-1",
                    "venue": "BINANCE",
                    "instrument_id": str(ids["instrument"]),
                    "currency": "usdt",
                    "initial_equity": "10000",
                    "idempotency_key": "api-shadow-scope",
                },
            )
            assert initialized.status_code == 200, initialized.text
            payload = initialized.json()["data"]
            assert payload["accounts"][0]["virtual_capital"][0]["currency"] == "USDT"
            assert payload["safety_boundary"]["venue_connectors_used"] is False

            flow_now = datetime.now(UTC)
            proposal = service.create_proposal(
                actor_id=ids["proposer"],
                source=ProposalSource.MANUAL,
                risk_tier=RiskTier.LOW,
                account_id="paper-1",
                venue="BINANCE",
                instrument_id=ids["instrument"],
                direction=Direction.LONG,
                quantity=Decimal("1"),
                max_risk=Decimal("20"),
                expires_at=flow_now + timedelta(hours=8),
                idempotency_key="api-shadow-proposal",
                environment=ExecutionEnvironment.SHADOW,
                details={
                    "trigger_price": "100",
                    "invalidation_price": "90",
                    "initial_quantity": "1",
                },
                now=flow_now,
            )
            service.submit_proposal(proposal, ids["proposer"], now=flow_now)
            service.review_proposal(
                proposal,
                ids["reviewer"],
                ReviewDecision.APPROVE,
                "reviewed through API fixture",
                now=flow_now,
            )
            service.decide_risk(
                proposal_id=proposal,
                actor_id=ids["operator"],
                kind=IntentKind.INITIAL,
                idempotency_key="api-shadow-risk",
                now=flow_now,
            )
            authorization = service.issue_authorization(
                proposal_id=proposal,
                actor_id=ids["operator"],
                expires_at=flow_now + timedelta(minutes=30),
                allowed_adds=0,
                idempotency_key="api-shadow-authorization",
                now=flow_now,
            )
            intent = service.create_order_intent(
                authorization_id=authorization,
                actor_id=ids["operator"],
                kind=IntentKind.INITIAL,
                account_id="paper-1",
                venue="BINANCE",
                instrument_id=ids["instrument"],
                direction=Direction.LONG,
                quantity=Decimal("1"),
                idempotency_key="api-shadow-intent",
                now=flow_now,
            )
            operator_login = await client.post(
                "/api/auth/mock/login", json={"username": "shadow-operator"}
            )
            assert operator_login.status_code == 200, operator_login.text
            simulated = await client.post(
                f"/api/intents/{intent.intent_id}/shadow-simulations",
                json={
                    "expected_version": 1,
                    "reference_price": "100",
                    "fee_bps": "4",
                    "slippage_bps": "2",
                    "idempotency_key": "api-shadow-simulation",
                },
            )
            assert simulated.status_code == 200, simulated.text
            assert simulated.json()["result"]["environment"] == "SHADOW"
            assert simulated.json()["detail"]["intents"][0]["status"] == "FILLED"

            page = await client.get("/shadow")
            assert page.status_code == 200
            assert 'id="main"' in page.text
            assert "/assets/app.js?v=156" in page.text

    asyncio.run(scenario())
