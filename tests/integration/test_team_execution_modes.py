from __future__ import annotations

import asyncio
import base64
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import inspect, select

from trading_control_plane.api import create_app
from trading_control_plane.config import Settings
from trading_control_plane.database import Database
from trading_control_plane.domain import (
    Direction,
    DomainRejected,
    ExecutionEnvironment,
    ProposalSource,
    RiskTier,
    Role,
    SystemRiskState,
    TeamExecutionMode,
)
from trading_control_plane.models import AuditEvent, CapabilityGate, ExchangeAccount, Proposal, Team
from trading_control_plane.perptape import PerptapeClient
from trading_control_plane.service import TradingService


def encryption_key() -> str:
    return base64.urlsafe_b64encode(b"team-mode-integration-key-32-byte"[:32]).decode()


def mode_app(database: Database) -> FastAPI:
    settings = Settings(
        environment="test",
        database_url=str(database.engine.url),
        allow_mock_identity=True,
        session_signing_secret=secrets.token_urlsafe(32),
        credential_encryption_key=encryption_key(),
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


def prepare_testnet_mode(database: Database) -> tuple[TradingService, dict[str, object]]:
    now = datetime.now(UTC)
    service = TradingService(database, credential_encryption_key=encryption_key())
    admin = service.bootstrap_admin("mode-admin", now=now)
    with database.session_factory.begin() as session:
        team = session.scalar(select(Team).where(Team.created_by == admin).with_for_update())
        assert team is not None
        team.execution_mode = TeamExecutionMode.SETUP.value
        team.execution_mode_locked_at = None
        team.execution_mode_updated_by = None
        team.trading_enabled = False
        team_id = team.team_id
        team_version = team.version

    account_uuid = service.create_exchange_account(
        actor_id=admin,
        environment=ExecutionEnvironment.TESTNET,
        account_id="mode-testnet",
        venue="BINANCE",
        label="Mode Testnet",
        credentials={"api_key": "testnet-key", "api_secret": "testnet-secret"},
        idempotency_key="mode-testnet-account",
        now=now,
    )
    runtime_principal = service.create_service_principal("mode-runtime", admin, now=now)
    service.assign_role(
        runtime_principal,
        Role.OPERATOR,
        admin,
        "mode-testnet",
        "BINANCE",
        now=now,
    )
    with database.session_factory.begin() as session:
        account = session.get(ExchangeAccount, account_uuid, with_for_update=True)
        assert account is not None
        account.connection_status = "VERIFIED"
        account.last_verified_at = now
        account.runtime_sync_enabled = True
        account.runtime_service_principal_id = runtime_principal
        account.trading_status = "ELIGIBLE"

    service.set_risk_policy(
        actor_id=admin,
        version="mode-risk-v1",
        system_state=SystemRiskState.NORMAL,
        max_total_risk=Decimal("100"),
        max_account_risk=Decimal("50"),
        max_single_loss=Decimal("10"),
        max_consecutive_losses=3,
        loss_cooldown=timedelta(hours=1),
        max_fact_age=timedelta(minutes=5),
        now=now,
    )
    member = service.create_managed_user(
        "mode-member",
        [Role.OBSERVER],
        admin,
        None,
        None,
        "mode-member-password",
        now=now,
    )
    return service, {
        "now": now,
        "admin": admin,
        "member": member,
        "team_id": team_id,
        "team_version": team_version,
        "account_uuid": account_uuid,
    }


def test_mode_switch_is_team_managed_versioned_audited_and_keeps_dangerous_gates_closed(
    database: Database,
) -> None:
    service, ids = prepare_testnet_mode(database)
    now = ids["now"]
    assert isinstance(now, datetime)
    status = service.trading_mode_status(actor_id=ids["member"], now=now)
    assert status["execution_mode"] == "SETUP"
    assert status["can_manage"] is False
    assert status["target_readiness"]["TESTNET"]["ready"] is True

    with pytest.raises(DomainRejected, match="RBAC_DENIED"):
        service.set_team_execution_mode(
            actor_id=ids["member"],
            team_id=ids["team_id"],
            mode="TESTNET",
            confirmation="SWITCH_TO_TESTNET",
            expected_version=ids["team_version"],
            idempotency_key="member-switch-denied",
            now=now,
        )
    with pytest.raises(DomainRejected, match="SECOND_CONFIRMATION_REQUIRED"):
        service.set_team_execution_mode(
            actor_id=ids["admin"],
            team_id=ids["team_id"],
            mode="TESTNET",
            confirmation="yes",
            expected_version=ids["team_version"],
            idempotency_key="wrong-confirmation",
            now=now,
        )

    with database.session_factory() as session:
        gates_before = {
            gate.capability_key: gate.status
            for gate in session.scalars(select(CapabilityGate)).all()
        }
    changed = service.set_team_execution_mode(
        actor_id=ids["admin"],
        team_id=ids["team_id"],
        mode="TESTNET",
        confirmation="SWITCH_TO_TESTNET",
        expected_version=ids["team_version"],
        idempotency_key="admin-switch-testnet",
        now=now,
    )
    assert changed["previous_mode"] == "SETUP"
    assert changed["execution_mode"] == "TESTNET"
    assert changed["dangerous_capabilities_changed"] is False

    with database.session_factory() as session:
        team = session.get(Team, ids["team_id"])
        assert team is not None
        assert team.execution_mode == "TESTNET"
        assert team.trading_enabled is True
        gates_after = {
            gate.capability_key: gate.status
            for gate in session.scalars(select(CapabilityGate)).all()
        }
        assert gates_after == gates_before
        audit = session.scalar(
            select(AuditEvent).where(AuditEvent.event_type == "TEAM_EXECUTION_MODE_CHANGED")
        )
        assert audit is not None
        assert audit.environment == "TESTNET"


def test_runtime_enums_proposal_environment_and_removed_endpoints_are_testnet_live_only(
    database: Database,
) -> None:
    assert {item.value for item in ExecutionEnvironment} == {"TESTNET", "LIVE"}
    assert {item.value for item in TeamExecutionMode} == {"SETUP", "TESTNET", "LIVE"}
    service, ids = prepare_testnet_mode(database)
    now = ids["now"]
    assert isinstance(now, datetime)
    service.set_team_execution_mode(
        actor_id=ids["admin"],
        team_id=ids["team_id"],
        mode="TESTNET",
        confirmation="SWITCH_TO_TESTNET",
        expected_version=ids["team_version"],
        idempotency_key="proposal-testnet-mode",
        now=now,
    )
    instrument = service.register_instrument(
        actor_id=ids["admin"],
        venue="BINANCE",
        symbol="BTCUSDT",
        tick_size=Decimal("0.1"),
        lot_size=Decimal("0.001"),
        minimum_notional=Decimal("5"),
        contract_multiplier=Decimal(1),
        quote_currency="USDT",
        collateral_currency="USDT",
        protection_supported=True,
        now=now,
    )
    with pytest.raises(DomainRejected, match="PROPOSAL_ENVIRONMENT_MISMATCH"):
        service.create_proposal(
            actor_id=ids["admin"],
            source=ProposalSource.MANUAL,
            risk_tier=RiskTier.LOW,
            account_id="mode-testnet",
            venue="BINANCE",
            instrument_id=instrument,
            direction=Direction.LONG,
            quantity=Decimal("0.01"),
            max_risk=Decimal("1"),
            expires_at=now + timedelta(hours=8),
            idempotency_key="mismatched-live-proposal",
            environment=ExecutionEnvironment.LIVE,
            now=now,
        )
    proposal_id = service.create_proposal(
        actor_id=ids["admin"],
        source=ProposalSource.MANUAL,
        risk_tier=RiskTier.LOW,
        account_id="mode-testnet",
        venue="BINANCE",
        instrument_id=instrument,
        direction=Direction.LONG,
        quantity=Decimal("0.01"),
        max_risk=Decimal("1"),
        expires_at=now + timedelta(hours=8),
        idempotency_key="server-owned-testnet-proposal",
        now=now,
    )
    with database.session_factory() as session:
        proposal = session.get(Proposal, proposal_id)
        assert proposal is not None and proposal.environment == "TESTNET"
    table_names = set(inspect(database.engine).get_table_names())
    assert not any(
        name.startswith("shadow_") or name == "team_shadow_accounts" for name in table_names
    )

    async def assert_removed_routes() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=mode_app(database)),
            base_url="http://test",
        ) as client:
            login = await client.post("/api/auth/mock/login", json={"username": "mode-admin"})
            assert login.status_code == 200
            for path in ("/api/shadow", "/api/shadow/account", "/api/shadow/orders"):
                response = await client.get(path)
                assert response.status_code == 404

    asyncio.run(assert_removed_routes())
