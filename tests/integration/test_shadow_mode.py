from __future__ import annotations

import asyncio
import base64
import secrets
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
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
    AuditEvent,
    Campaign,
    Position,
    ProtectionOrder,
    ShadowFill,
    ShadowOrder,
    ShadowPosition,
    Team,
    TeamShadowAccount,
    VenueFill,
    VenueOrder,
)
from trading_control_plane.perptape import PerptapeClient
from trading_control_plane.queries import TradingQueries
from trading_control_plane.request_context import (
    ApiClientRequestContext,
    bind_api_client_context,
    reset_api_client_context,
)
from trading_control_plane.service import TradingService

NOW = datetime(2026, 8, 10, 8, tzinfo=UTC)

VENUE_WRITE_ENTRYPOINTS = (
    "prepare_binance_testnet_send",
    "prepare_binance_testnet_cancel",
    "prepare_binance_testnet_recovery",
    "prepare_binance_testnet_protection",
    "prepare_binance_live_send",
    "prepare_binance_live_cancel",
    "prepare_binance_live_recovery",
    "prepare_binance_live_protection",
    "prepare_hyperliquid_testnet_send",
    "prepare_hyperliquid_testnet_cancel",
    "prepare_hyperliquid_testnet_recovery",
    "prepare_hyperliquid_testnet_protection",
    "prepare_hyperliquid_live_send",
    "prepare_hyperliquid_live_cancel",
    "prepare_hyperliquid_live_recovery",
    "prepare_hyperliquid_live_protection",
    # OKX and Bybit writes are routed exclusively through an account-bound Freqtrade worker.
    "prepare_freqtrade_live_order",
    "prepare_live_protection_cancel",
)


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
        initial_equity=Decimal("100000"),
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
            initial_equity=Decimal("100000"),
            idempotency_key="initialize-shadow-scope",
            now=NOW,
        )
        == initialized
    )

    assert initialized["scope_initialization_retired"] is True
    assert initialized["generation"] == 1
    with pytest.raises(DomainRejected, match="SHADOW_INITIAL_EQUITY_FIXED"):
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
    token = service.acquire_sender(
        "paper-1:BINANCE", "retired-shadow-worker", ids["operator"], NOW
    )
    with pytest.raises(DomainRejected) as retired_send:
        service.record_shadow_order(
            intent.intent_id,
            ids["operator"],
            "paper-1:BINANCE",
            "retired-shadow-worker",
            token,
            "legacy-shadow-order",
            now=NOW + timedelta(seconds=1),
        )
    assert retired_send.value.code == "SHADOW_LEGACY_EXECUTION_RETIRED"

    with ExitStack() as stack:
        write_guards = [
            stack.enter_context(patch.object(service, name))
            for name in VENUE_WRITE_ENTRYPOINTS
        ]
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
        assert all(guard.call_count == 0 for guard in write_guards)

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
    status = service.trading_mode_status(actor_id=ids["admin"], now=NOW)
    assert Decimal(status["shadow_account"]["equity"]) == Decimal(
        "99999.859960000000000000"
    )
    assert len(workspace["campaigns"]) == 1
    shadow_results = queries.actual_results(ids["admin"], "SHADOW")
    assert len(shadow_results["campaigns"]) == 1
    assert shadow_results["campaigns"][0]["fill_count"] == 1
    assert Decimal(shadow_results["campaigns"][0]["fees"]) > 0
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
            idempotency_key="testnet-proposal-blocked",
            environment=ExecutionEnvironment.TESTNET,
            now=NOW,
        )

    with database.session_factory() as session:
        campaign = session.get(Campaign, intent.campaign_id)
        assert campaign is not None and campaign.environment == "SHADOW"
        shadow_account = session.get(
            TeamShadowAccount, UUID(initialized["shadow_account_id"])
        )
        position = session.scalar(select(ShadowPosition))
        assert shadow_account is not None and shadow_account.generation == 1
        assert position is not None and position.generation == 1
        assert position.quantity == Decimal("1")
        assert session.scalar(select(func.count()).select_from(ShadowOrder)) == 2
        assert session.scalar(select(func.count()).select_from(ShadowFill)) == 1
        assert session.scalar(select(func.count()).select_from(VenueOrder)) == 0
        assert session.scalar(select(func.count()).select_from(VenueFill)) == 0
        assert session.scalar(select(func.count()).select_from(ProtectionOrder)) == 0
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
                json={
                    "confirmation": "SWITCH_TO_SHADOW",
                    "expected_version": 1,
                    "idempotency_key": "api-shadow-activation",
                },
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
                    "initial_equity": "100000",
                    "idempotency_key": "api-shadow-scope",
                },
            )
            assert initialized.status_code == 200, initialized.text
            payload = initialized.json()
            assert payload["result"]["scope_initialization_retired"] is True
            assert payload["result"]["generation"] == 1
            assert payload["data"]["safety_boundary"]["venue_connectors_used"] is False

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
            assert "/assets/app.js?v=170" in page.text

    asyncio.run(scenario())


def activate_unified_shadow(
    service: TradingService,
    ids: dict[str, UUID],
    *,
    key: str,
) -> dict[str, object]:
    configure_shadow_prerequisites(service, ids)
    return service.set_team_execution_mode(
        actor_id=ids["admin"],
        team_id=ids["team"],
        mode="SHADOW",
        confirmation="SWITCH_TO_SHADOW",
        expected_version=1,
        idempotency_key=key,
        now=NOW,
    )


def create_unified_order(
    service: TradingService,
    ids: dict[str, UUID],
    *,
    key: str,
    order_type: str = "MARKET",
    side: str = "BUY",
    quantity: str = "1",
    latest_price: str | None = "100",
    limit_price: str | None = None,
    observed_at: datetime | None = NOW,
) -> dict[str, object]:
    return service.create_shadow_order(
        actor_id=ids["operator"],
        account_id="paper-1",
        venue="BINANCE",
        symbol="BTCUSDT",
        catalog_instrument_id=ids["instrument"],
        side=side,
        order_type=order_type,
        quantity=Decimal(quantity),
        limit_price=None if limit_price is None else Decimal(limit_price),
        latest_price=None if latest_price is None else Decimal(latest_price),
        observed_at=observed_at,
        price_tick=Decimal("0.1"),
        quantity_step=Decimal("0.001"),
        contract_multiplier=Decimal("1"),
        is_derivative=True,
        fee_bps=Decimal("4"),
        slippage_bps=Decimal("2"),
        idempotency_key=key,
        now=NOW,
    )


def test_team_mode_unified_market_limit_protection_and_reset_are_atomic(
    database: Database,
) -> None:
    service, ids = shadow_team_fixture(database)
    activated = activate_unified_shadow(service, ids, key="unified-activate")
    assert activated["execution_mode"] == "SHADOW"
    assert activated["dangerous_capabilities_changed"] is False
    assert activated["shadow_account"]["equity"] == "100000"

    with ExitStack() as stack:
        write_guards = [
            stack.enter_context(patch.object(service, name))
            for name in VENUE_WRITE_ENTRYPOINTS
        ]
        market = create_unified_order(service, ids, key="unified-market")
        open_limit = create_unified_order(
            service,
            ids,
            key="unified-limit",
            order_type="LIMIT",
            limit_price="90",
        )
        assert all(guard.call_count == 0 for guard in write_guards)

    assert market["environment"] == "SHADOW"
    assert market["status"] == "FILLED"
    assert market["filled_quantity"] == market["quantity"] == "1"
    assert market["fill"]["price"] == "100.1"
    assert market["fill"]["fee"] == "0.040040000000000000"
    assert open_limit["status"] == "OPEN"
    assert "fill" not in open_limit

    matched = service.match_shadow_order(
        actor_id=ids["operator"],
        shadow_order_id=UUID(open_limit["shadow_order_id"]),
        expected_version=1,
        latest_price=Decimal("89"),
        observed_at=NOW + timedelta(seconds=1),
        price_tick=Decimal("0.1"),
        quantity_step=Decimal("0.001"),
        contract_multiplier=Decimal("1"),
        is_derivative=True,
        fee_bps=Decimal("4"),
        slippage_bps=Decimal("2"),
        idempotency_key="unified-limit-match",
        now=NOW + timedelta(seconds=1),
    )
    replay = service.match_shadow_order(
        actor_id=ids["operator"],
        shadow_order_id=UUID(open_limit["shadow_order_id"]),
        expected_version=1,
        latest_price=Decimal("89"),
        observed_at=NOW + timedelta(seconds=1),
        price_tick=Decimal("0.1"),
        quantity_step=Decimal("0.001"),
        contract_multiplier=Decimal("1"),
        is_derivative=True,
        fee_bps=Decimal("4"),
        slippage_bps=Decimal("2"),
        idempotency_key="unified-limit-match",
        now=NOW + timedelta(seconds=1),
    )
    assert matched == replay
    assert matched["status"] == "FILLED"
    assert Decimal(matched["fill"]["price"]) == Decimal("90")

    protection = service.create_shadow_protection(
        actor_id=ids["operator"],
        shadow_position_id=UUID(matched["shadow_position_id"]),
        trigger_type="STOP_LOSS",
        execution_type="MARKET",
        trigger_price=Decimal("80"),
        limit_price=None,
        idempotency_key="unified-stop",
        now=NOW + timedelta(seconds=2),
    )
    assert protection["reduce_only"] is True
    assert protection["status"] == "OPEN"
    stopped = service.match_shadow_order(
        actor_id=ids["operator"],
        shadow_order_id=UUID(protection["shadow_order_id"]),
        expected_version=1,
        latest_price=Decimal("79"),
        observed_at=NOW + timedelta(seconds=3),
        price_tick=Decimal("0.1"),
        quantity_step=Decimal("0.001"),
        contract_multiplier=Decimal("1"),
        is_derivative=True,
        fee_bps=Decimal("4"),
        slippage_bps=Decimal("2"),
        idempotency_key="unified-stop-match",
        now=NOW + timedelta(seconds=3),
    )
    assert stopped["status"] == "FILLED"
    assert Decimal(stopped["filled_quantity"]) == Decimal(stopped["quantity"]) == Decimal("2")

    short_order = create_unified_order(
        service,
        ids,
        key="unified-short-market",
        side="SELL",
    )
    take_profit = service.create_shadow_protection(
        actor_id=ids["operator"],
        shadow_position_id=UUID(short_order["shadow_position_id"]),
        trigger_type="TAKE_PROFIT",
        execution_type="LIMIT",
        trigger_price=Decimal("90"),
        limit_price=Decimal("89"),
        idempotency_key="unified-short-tp-limit",
        now=NOW + timedelta(seconds=4),
    )
    triggered = service.match_shadow_order(
        actor_id=ids["operator"],
        shadow_order_id=UUID(take_profit["shadow_order_id"]),
        expected_version=1,
        latest_price=Decimal("90"),
        observed_at=NOW + timedelta(seconds=5),
        price_tick=Decimal("0.1"),
        quantity_step=Decimal("0.001"),
        contract_multiplier=Decimal("1"),
        is_derivative=True,
        fee_bps=Decimal("4"),
        slippage_bps=Decimal("2"),
        idempotency_key="unified-short-tp-trigger",
        now=NOW + timedelta(seconds=5),
    )
    assert triggered["status"] == "TRIGGERED"
    assert "fill" not in triggered
    limit_protected = service.match_shadow_order(
        actor_id=ids["operator"],
        shadow_order_id=UUID(take_profit["shadow_order_id"]),
        expected_version=2,
        latest_price=Decimal("88"),
        observed_at=NOW + timedelta(seconds=6),
        price_tick=Decimal("0.1"),
        quantity_step=Decimal("0.001"),
        contract_multiplier=Decimal("1"),
        is_derivative=True,
        fee_bps=Decimal("4"),
        slippage_bps=Decimal("2"),
        idempotency_key="unified-short-tp-fill",
        now=NOW + timedelta(seconds=6),
    )
    assert limit_protected["status"] == "FILLED"
    assert Decimal(limit_protected["fill"]["price"]) == Decimal("89")

    status = service.trading_mode_status(actor_id=ids["admin"], now=NOW)
    before_reset = status["shadow_account"]
    reset = service.reset_shadow_account(
        actor_id=ids["admin"],
        expected_version=before_reset["version"],
        confirmation="RESET_TO_100000_U",
        idempotency_key="unified-reset",
        now=NOW + timedelta(seconds=7),
    )
    assert reset["previous_generation"] == 1
    assert reset["shadow_account"]["generation"] == 2
    assert reset["shadow_account"]["equity"] == "100000"
    assert reset["shadow_account"]["available_balance"] == "100000"
    assert reset["shadow_account"]["realized_pnl"] == "0"
    assert reset["shadow_account"]["fees_paid"] == "0"

    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ShadowFill)) == 5
        assert session.scalar(select(func.count()).select_from(ShadowOrder)) == 5
        assert session.scalar(select(func.count()).select_from(TeamShadowAccount)) == 2
        archived = session.scalar(
            select(TeamShadowAccount).where(TeamShadowAccount.generation == 1)
        )
        assert archived is not None and archived.status == "ARCHIVED"
        assert session.scalar(
            select(func.count())
            .select_from(ShadowPosition)
            .where(ShadowPosition.status == "ARCHIVED")
        ) == 1
        assert session.scalar(select(func.count()).select_from(VenueOrder)) == 0
        assert session.scalar(select(func.count()).select_from(VenueFill)) == 0


@pytest.mark.parametrize(
    ("latest_price", "observed_at", "price_tick", "quantity_step", "multiplier", "code"),
    [
        (None, NOW, "0.1", "0.001", "1", "SHADOW_PRICE_MISSING"),
        ("0", NOW, "0.1", "0.001", "1", "SHADOW_PRICE_INVALID"),
        ("100", NOW - timedelta(minutes=2), "0.1", "0.001", "1", "SHADOW_PRICE_STALE"),
        ("100", NOW, None, "0.001", "1", "SHADOW_PRICE_PRECISION_MISSING"),
        ("100", NOW, "0.1", None, "1", "SHADOW_QUANTITY_PRECISION_MISSING"),
        ("100", NOW, "0.1", "0.001", None, "SHADOW_CONTRACT_MULTIPLIER_MISSING"),
    ],
)
def test_shadow_market_facts_fail_closed_with_stable_codes_and_audit(
    database: Database,
    latest_price: str | None,
    observed_at: datetime,
    price_tick: str | None,
    quantity_step: str | None,
    multiplier: str | None,
    code: str,
) -> None:
    service, ids = shadow_team_fixture(database)
    activate_unified_shadow(service, ids, key=f"activate-{code}")
    with pytest.raises(DomainRejected) as rejected:
        service.create_shadow_order(
            actor_id=ids["operator"],
            account_id="paper-1",
            venue="BINANCE",
            symbol="ETHUSDT",
            catalog_instrument_id=None,
            side="BUY",
            order_type="MARKET",
            quantity=Decimal("1"),
            limit_price=None,
            latest_price=None if latest_price is None else Decimal(latest_price),
            observed_at=observed_at,
            price_tick=None if price_tick is None else Decimal(price_tick),
            quantity_step=None if quantity_step is None else Decimal(quantity_step),
            contract_multiplier=None if multiplier is None else Decimal(multiplier),
            is_derivative=True,
            fee_bps=Decimal("4"),
            slippage_bps=Decimal("2"),
            idempotency_key=f"blocked-{code}",
            now=NOW,
        )
    assert rejected.value.code == code
    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ShadowOrder)) == 0
        audit = session.scalar(
            select(AuditEvent).where(
                AuditEvent.event_type == "SHADOW_EXECUTION_BLOCKED",
                AuditEvent.idempotency_key == f"blocked-{code}",
            )
        )
        assert audit is not None
        assert audit.rule_summary["error_code"] == code


def test_shadow_fill_failure_rolls_back_all_ledger_facts(database: Database) -> None:
    service, ids = shadow_team_fixture(database)
    activate_unified_shadow(service, ids, key="rollback-activate")
    with pytest.raises(DomainRejected, match="SHADOW_CAPITAL_INSUFFICIENT"):
        create_unified_order(
            service,
            ids,
            key="oversized-order",
            quantity="2000",
        )
    with database.session_factory() as session:
        account = session.scalar(select(TeamShadowAccount))
        assert account is not None
        assert account.equity == Decimal("100000")
        assert account.available_balance == Decimal("100000")
        assert session.scalar(select(func.count()).select_from(ShadowOrder)) == 0
        assert session.scalar(select(func.count()).select_from(ShadowFill)) == 0
        assert session.scalar(select(func.count()).select_from(ShadowPosition)) == 0


def test_concurrent_shadow_matching_records_at_most_one_fill(database: Database) -> None:
    service, ids = shadow_team_fixture(database)
    activate_unified_shadow(service, ids, key="concurrent-activate")
    order = create_unified_order(
        service,
        ids,
        key="concurrent-open-limit",
        order_type="LIMIT",
        limit_price="90",
    )

    def match() -> dict[str, object]:
        return service.match_shadow_order(
            actor_id=ids["operator"],
            shadow_order_id=UUID(order["shadow_order_id"]),
            expected_version=1,
            latest_price=Decimal("89"),
            observed_at=NOW + timedelta(seconds=1),
            price_tick=Decimal("0.1"),
            quantity_step=Decimal("0.001"),
            contract_multiplier=Decimal("1"),
            is_derivative=True,
            fee_bps=Decimal("4"),
            slippage_bps=Decimal("2"),
            idempotency_key="concurrent-match-same-key",
            now=NOW + timedelta(seconds=1),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        left, right = tuple(executor.map(lambda _value: match(), range(2)))
    assert left == right
    assert left["status"] == "FILLED"
    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ShadowFill)) == 1
        stored_order = session.get(ShadowOrder, UUID(order["shadow_order_id"]))
        assert stored_order is not None
        assert stored_order.filled_quantity == stored_order.quantity


def test_trading_mode_api_is_persisted_team_scoped_and_ignores_environment_override(
    database: Database,
) -> None:
    _service, ids = shadow_team_fixture(database)
    configure_shadow_prerequisites(_service, ids)

    async def scenario() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=shadow_app(database)),
            base_url="http://test",
        ) as client:
            login = await client.post("/api/auth/mock/login", json={"username": "shadow-admin"})
            assert login.status_code == 200
            before = await client.get("/api/trading-mode")
            assert before.status_code == 200
            assert before.json()["data"]["execution_mode"] == "SETUP"
            switched = await client.put(
                f"/api/teams/{ids['team']}/trading-mode",
                json={
                    "mode": "SHADOW",
                    "confirmation": "SWITCH_TO_SHADOW",
                    "expected_version": 1,
                    "idempotency_key": "api-unified-switch",
                },
            )
            assert switched.status_code == 200, switched.text
            assert switched.json()["data"]["shadow_account"]["equity"] == "100000"
            created = await client.post(
                "/api/trading-mode/shadow/orders",
                json={
                    "account_id": "paper-1",
                    "venue": "BINANCE",
                    "symbol": "BTCUSDT",
                    "catalog_instrument_id": str(ids["instrument"]),
                    "side": "BUY",
                    "order_type": "MARKET",
                    "quantity": "1",
                    "latest_price": "100",
                    "observed_at": datetime.now(UTC).isoformat(),
                    "price_tick": "0.1",
                    "quantity_step": "0.001",
                    "contract_multiplier": "1",
                    "environment": "LIVE",
                    "idempotency_key": "api-forced-environment",
                },
            )
            assert created.status_code == 200, created.text
            assert created.json()["data"]["environment"] == "SHADOW"
            refreshed = await client.get("/api/trading-mode")
            assert refreshed.json()["data"]["execution_mode"] == "SHADOW"
            assert refreshed.json()["data"]["shadow_account"]["equity"] != "100000"
            denied = await client.put(
                "/api/teams/00000000-0000-0000-0000-000000000001/trading-mode",
                json={
                    "mode": "LIVE",
                    "confirmation": "SWITCH_TO_LIVE",
                    "expected_version": 2,
                    "idempotency_key": "cross-team-denied",
                },
            )
            assert denied.status_code in {403, 409}
            assert denied.json()["error"]["code"] == "TEAM_SCOPE_DENIED"

    asyncio.run(scenario())


def test_live_mode_regression_human_version_idempotency_and_shadow_blockers(
    database: Database,
) -> None:
    service, ids = shadow_team_fixture(database)
    activate_unified_shadow(service, ids, key="mode-regression-shadow")
    live = service.set_team_execution_mode(
        actor_id=ids["admin"],
        team_id=ids["team"],
        mode="LIVE",
        confirmation="SWITCH_TO_LIVE",
        expected_version=2,
        idempotency_key="mode-regression-live",
        now=NOW + timedelta(seconds=1),
    )
    replay = service.set_team_execution_mode(
        actor_id=ids["admin"],
        team_id=ids["team"],
        mode="LIVE",
        confirmation="SWITCH_TO_LIVE",
        expected_version=2,
        idempotency_key="mode-regression-live",
        now=NOW + timedelta(seconds=1),
    )
    assert live == replay
    assert live["execution_mode"] == "LIVE"
    assert live["dangerous_capabilities_changed"] is False

    proposal = service.create_proposal(
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
        idempotency_key="live-path-unchanged",
        environment=ExecutionEnvironment.LIVE,
        details={
            "trigger_price": "100",
            "invalidation_price": "90",
            "initial_quantity": "1",
        },
        now=NOW + timedelta(seconds=2),
    )
    assert proposal is not None
    for blocked_environment in (
        ExecutionEnvironment.SHADOW,
        ExecutionEnvironment.TESTNET,
    ):
        with pytest.raises(DomainRejected) as mismatch:
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
                idempotency_key=f"live-mode-blocks-{blocked_environment.value.lower()}",
                environment=blocked_environment,
                now=NOW + timedelta(seconds=2),
            )
        assert mismatch.value.code == "TEAM_LIVE_ONLY"

    with database.session_factory.begin() as session:
        session.add(
            Position(
                team_id=ids["team"],
                account_id="paper-1",
                venue="BINANCE",
                environment="LIVE",
                instrument_id=ids["instrument"],
                quantity=Decimal("1"),
                average_entry_price=Decimal("100"),
                mark_price=Decimal("100"),
                fact_status="KNOWN",
                observed_at=NOW,
                updated_at=NOW,
            )
        )
    with pytest.raises(DomainRejected) as blocked:
        service.set_team_execution_mode(
            actor_id=ids["admin"],
            team_id=ids["team"],
            mode="SHADOW",
            confirmation="SWITCH_TO_SHADOW",
            expected_version=3,
            idempotency_key="live-position-blocks-shadow",
            now=NOW + timedelta(seconds=3),
        )
    assert blocked.value.code == "TEAM_MODE_SHADOW_BLOCKED"
    assert "LIVE_POSITIONS_OPEN(1)" in blocked.value.detail
    with database.session_factory() as session:
        team = session.get(Team, ids["team"])
        assert team is not None and team.execution_mode == "LIVE"
        audit = session.scalar(
            select(AuditEvent).where(
                AuditEvent.event_type == "TEAM_TRADING_MODE_BLOCKED",
                AuditEvent.idempotency_key == "live-position-blocks-shadow",
            )
        )
        assert audit is not None
        assert audit.rule_summary["blockers"] == [
            {"code": "LIVE_POSITIONS_OPEN", "count": 1}
        ]

    context_token = bind_api_client_context(
        ApiClientRequestContext(
            owner_user_id=ids["admin"],
            api_client_id=UUID("00000000-0000-0000-0000-000000000111"),
            workspace_id=UUID(live["workspace_id"]),
            team_id=ids["team"],
            account_id="paper-1",
            venue="BINANCE",
        )
    )
    try:
        with pytest.raises(DomainRejected) as human_only:
            service.set_team_execution_mode(
                actor_id=ids["admin"],
                team_id=ids["team"],
                mode="SHADOW",
                confirmation="SWITCH_TO_SHADOW",
                expected_version=3,
                idempotency_key="api-client-mode-denied",
                now=NOW + timedelta(seconds=4),
            )
        assert human_only.value.code == "HUMAN_WEB_CONFIRMATION_REQUIRED"
    finally:
        reset_api_client_context(context_token)
