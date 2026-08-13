from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from conftest import set_test_team_environment
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from trading_control_plane.api import create_app
from trading_control_plane.config import Settings
from trading_control_plane.database import Database
from trading_control_plane.domain import (
    Direction,
    DomainRejected,
    ExecutionEnvironment,
    ProposalSource,
    RiskTier,
    SystemRiskState,
)
from trading_control_plane.models import (
    AuditEvent,
    Proposal,
    RiskPolicy,
    RoleAssignment,
    Team,
    User,
    Workspace,
)
from trading_control_plane.perptape import PerptapeClient
from trading_control_plane.queries import TradingQueries
from trading_control_plane.service import TradingService


def scope_app(database: Database):
    settings = Settings(
        environment="test",
        database_url=str(database.engine.url),
        allow_mock_identity=True,
        session_signing_secret="scope-test-signing-secret-that-is-long-enough",  # noqa: S106
        public_base_url="http://test",
        runtime_binance_account_id="acct-live",
        runtime_hyperliquid_account_id="acct-hl",
        _env_file=None,
    )
    return create_app(
        settings,
        database,
        PerptapeClient(
            base_url="https://perptape.com",
            api_key=None,
            contract_version="breakouts-v1",
            cache_ttl=timedelta(minutes=1),
        ),
    )


async def login(client: AsyncClient, username: str) -> dict[str, object]:
    response = await client.post("/api/auth/mock/login", json={"username": username})
    assert response.status_code == 200, response.text
    return response.json()["session"]


def test_workspace_and_team_roles_are_selected_and_calculated_independently(
    database: Database,
) -> None:
    TradingService(database).bootstrap_admin("scope-admin", now=datetime.now(UTC))

    async def scenario() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=scope_app(database)),
            base_url="http://test",
        ) as client:
            initial = await login(client, "scope-admin")
            assert initial["active_workspace"]["slug"] == "default"
            assert initial["active_team"]["slug"] == "default"
            assert [item["role"] for item in initial["roles"]] == ["SYSTEM_ADMIN"]

            workspace = await client.post(
                "/api/workspaces",
                json={
                    "name": "Trading Operations",
                    "slug": "trading-operations",
                    "idempotency_key": "workspace-create-1",
                },
            )
            assert workspace.status_code == 200, workspace.text
            workspace_id = workspace.json()["workspace_id"]
            default_team_id = workspace.json()["session"]["active_team"]["team_id"]
            assert workspace.json()["session"]["active_team"]["slug"] == "default"
            assert workspace.json()["session"]["active_team"]["name"] == "Trading Operations"
            assert [item["role"] for item in workspace.json()["session"]["roles"]] == [
                "SYSTEM_ADMIN"
            ]
            created_workspace = next(
                item
                for item in workspace.json()["session"]["workspaces"]
                if item["workspace_id"] == workspace_id
            )
            assert created_workspace == {
                "workspace_id": workspace_id,
                "name": "Trading Operations",
                "slug": "trading-operations",
                "role": "ADMIN",
                "member_count": 1,
                "agent_count": 0,
                "team_count": 1,
                "default_team_id": default_team_id,
            }

            replay = await client.post(
                "/api/workspaces",
                json={
                    "name": "Trading Operations",
                    "slug": "trading-operations",
                    "idempotency_key": "workspace-create-1",
                },
            )
            assert replay.status_code == 200, replay.text
            assert replay.json()["workspace_id"] == workspace_id
            conflict = await client.post(
                "/api/workspaces",
                json={
                    "name": "Different Workspace",
                    "slug": "different-workspace",
                    "idempotency_key": "workspace-create-1",
                },
            )
            assert conflict.status_code == 409, conflict.text
            assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

            alpha = await client.post(
                "/api/teams",
                json={
                    "name": "Alpha",
                    "slug": "alpha",
                    "idempotency_key": "team-alpha-create",
                },
            )
            assert alpha.status_code == 200, alpha.text
            alpha_id = alpha.json()["team_id"]
            assert alpha.json()["session"]["active_team"]["trading_enabled"] is False
            assert [item["role"] for item in alpha.json()["session"]["roles"]] == [
                "SYSTEM_ADMIN"
            ]
            blocked_business = await client.get("/api/proposals")
            assert blocked_business.status_code == 403, blocked_business.text
            assert blocked_business.json()["error"]["code"] == "RBAC_DENIED"

            kelly = await client.post(
                "/api/admin/users",
                json={
                    "username": "kelly-scope",
                    "password": "kelly-scope-password",
                    "roles": ["OBSERVER"],
                },
            )
            assert kelly.status_code == 200, kelly.text
            kelly_id = kelly.json()["user_id"]

            beta = await client.post(
                "/api/teams",
                json={
                    "name": "Beta",
                    "slug": "beta",
                    "idempotency_key": "team-beta-create",
                },
            )
            assert beta.status_code == 200, beta.text
            beta_id = beta.json()["team_id"]
            beta_members = await client.get("/api/admin/users")
            assert beta_members.status_code == 200, beta_members.text
            assert {item["username"] for item in beta_members.json()["data"]} == {
                "scope-admin"
            }

            invited = await client.post(
                "/api/admin/team-members",
                json={
                    "username": "kelly-scope",
                    "roles": ["PROPOSER"],
                    "account_scope": "beta-account",
                    "venue_scope": "BINANCE",
                    "idempotency_key": "invite-kelly-beta",
                },
            )
            assert invited.status_code == 200, invited.text
            assert invited.json()["user_id"] == kelly_id
            kelly_in_beta = next(
                item for item in invited.json()["data"] if item["user_id"] == kelly_id
            )
            assert kelly_in_beta["roles"] == [
                {
                    "role": "PROPOSER",
                    "account_scope": "beta-account",
                    "venue_scope": "BINANCE",
                }
            ]

            kelly_alpha = await login(client, "kelly-scope")
            assert kelly_alpha["active_team"]["team_id"] == alpha_id
            assert [item["role"] for item in kelly_alpha["roles"]] == ["OBSERVER"]
            switched = await client.post(
                "/api/scopes/select",
                json={
                    "workspace_id": workspace_id,
                    "team_id": beta_id,
                    "idempotency_key": "kelly-select-beta",
                },
            )
            assert switched.status_code == 200, switched.text
            assert switched.json()["session"]["active_team"]["team_id"] == beta_id
            assert [item["role"] for item in switched.json()["session"]["roles"]] == [
                "PROPOSER"
            ]

            denied = await client.post(
                "/api/scopes/select",
                json={
                    "workspace_id": workspace_id,
                    "team_id": str(uuid4()),
                    "idempotency_key": "kelly-select-missing",
                },
            )
            assert denied.status_code == 403, denied.text
            assert denied.json()["error"]["code"] == "TEAM_ACCESS_DENIED"

    asyncio.run(scenario())

    with database.session_factory() as session:
        workspace = session.scalar(
            select(Workspace).where(Workspace.slug == "trading-operations")
        )
        assert workspace is not None
        teams = session.scalars(
            select(Team).where(Team.workspace_id == workspace.workspace_id).order_by(Team.slug)
        ).all()
        assert [(team.slug, team.trading_enabled) for team in teams] == [
            ("alpha", False),
            ("beta", False),
            ("default", False),
        ]
        kelly = session.scalar(select(User).where(User.username == "kelly-scope"))
        assert kelly is not None
        assignments = session.scalars(
            select(RoleAssignment)
            .where(RoleAssignment.user_id == kelly.user_id)
            .order_by(RoleAssignment.role)
        ).all()
        assert {(item.team_id, item.role) for item in assignments} == {
            (teams[0].team_id, "OBSERVER"),
            (teams[1].team_id, "PROPOSER"),
        }
        scoped_events = session.scalars(
            select(AuditEvent).where(
                AuditEvent.event_type.in_(
                    {"WORKSPACE_CREATED", "TEAM_CREATED", "TEAM_MEMBER_ADDED"}
                )
            )
        ).all()
        assert scoped_events
        assert all(event.workspace_id == workspace.workspace_id for event in scoped_events)
        assert all(event.idempotency_key for event in scoped_events)


def test_proposal_chain_defaults_to_active_team_and_denies_cross_team_access(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    service = TradingService(database)
    admin_id = service.bootstrap_admin("team-chain-admin", now=now)
    set_test_team_environment(database, admin_id, "SHADOW")
    context = TradingQueries(database).user_context(admin_id)
    default_workspace_id = UUID(str(context["active_workspace"]["workspace_id"]))
    default_team_id = UUID(str(context["active_team"]["team_id"]))
    instrument_id = service.register_instrument(
        actor_id=admin_id,
        venue="BINANCE",
        symbol="BTCUSDT",
        tick_size=Decimal("0.1"),
        lot_size=Decimal("0.001"),
        minimum_notional=Decimal("5"),
        contract_multiplier=Decimal("1"),
        quote_currency="USDT",
        collateral_currency="USDT",
        protection_supported=True,
        now=now,
    )

    proposal_a = service.create_proposal(
        actor_id=admin_id,
        source=ProposalSource.MANUAL,
        risk_tier=RiskTier.LOW,
        account_id="shared-account",
        venue="BINANCE",
        instrument_id=instrument_id,
        direction=Direction.LONG,
        quantity=Decimal("0.01"),
        max_risk=Decimal("10"),
        expires_at=now + timedelta(hours=1),
        idempotency_key="team-a-proposal",
        environment=ExecutionEnvironment.SHADOW,
        deduplicate_active_manual_semantics=True,
        now=now,
    )

    workspace_b = service.create_workspace(
        actor_id=admin_id,
        name="Workspace B",
        slug="workspace-b",
        idempotency_key="workspace-b-create",
        now=now,
    )
    team_b = service.create_team(
        actor_id=admin_id,
        name="Team B",
        slug="team-b",
        idempotency_key="team-b-create",
        now=now,
    )
    # Account and risk roots are the next migration phase. Enabling the test
    # team here exercises the already implemented proposal-chain boundary.
    with database.session_factory.begin() as session:
        team = session.get(Team, team_b, with_for_update=True)
        assert team is not None
        team.trading_enabled = True
        team.execution_mode = "SHADOW"

    proposal_b = service.create_proposal(
        actor_id=admin_id,
        source=ProposalSource.MANUAL,
        risk_tier=RiskTier.LOW,
        account_id="shared-account",
        venue="BINANCE",
        instrument_id=instrument_id,
        direction=Direction.LONG,
        quantity=Decimal("0.01"),
        max_risk=Decimal("10"),
        expires_at=now + timedelta(hours=1),
        idempotency_key="team-b-proposal",
        environment=ExecutionEnvironment.SHADOW,
        deduplicate_active_manual_semantics=True,
        now=now,
    )
    assert proposal_b != proposal_a

    queries = TradingQueries(database)
    listed = queries.list_proposals(admin_id)
    assert [item["proposal_id"] for item in listed] == [str(proposal_b)]
    assert listed[0]["workspace_id"] == str(workspace_b)
    assert listed[0]["team_id"] == str(team_b)
    assert listed[0]["account_id"] == "shared-account"
    with pytest.raises(DomainRejected, match="TEAM_SCOPE_DENIED"):
        queries.proposal_detail(admin_id, proposal_a)
    with pytest.raises(DomainRejected, match="TEAM_SCOPE_DENIED"):
        service.submit_proposal(proposal_a, admin_id, now=now)

    async def api_scenario() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=scope_app(database)),
            base_url="http://test",
        ) as client:
            await login(client, "team-chain-admin")
            response = await client.get("/api/proposals")
            assert response.status_code == 200, response.text
            assert [item["proposal_id"] for item in response.json()["data"]] == [
                str(proposal_b)
            ]
            denied = await client.get(f"/api/proposals/{proposal_a}")
            assert denied.status_code == 403, denied.text
            assert denied.json()["error"]["code"] == "TEAM_SCOPE_DENIED"

    asyncio.run(api_scenario())

    with database.session_factory() as session:
        persisted_a = session.get(Proposal, proposal_a)
        persisted_b = session.get(Proposal, proposal_b)
        assert persisted_a is not None and persisted_a.team_id == default_team_id
        assert persisted_b is not None and persisted_b.team_id == team_b
        event = session.scalar(
            select(AuditEvent).where(
                AuditEvent.object_type == "Proposal",
                AuditEvent.object_id == str(proposal_b),
                AuditEvent.event_type == "PROPOSAL_CREATED",
            )
        )
        assert event is not None
        assert event.workspace_id == workspace_b
        assert event.team_id == team_b
        assert event.account_id == "shared-account"

    service.select_scope(
        actor_id=admin_id,
        workspace_id=default_workspace_id,
        team_id=default_team_id,
        idempotency_key="return-default-team",
        now=now,
    )
    assert [item["proposal_id"] for item in queries.list_proposals(admin_id)] == [
        str(proposal_a)
    ]


def test_risk_policy_versions_and_status_are_isolated_by_active_team(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    service = TradingService(database)
    admin_id = service.bootstrap_admin("team-risk-admin", now=now)
    default_context = TradingQueries(database).user_context(admin_id)
    default_workspace_id = UUID(str(default_context["active_workspace"]["workspace_id"]))
    default_team_id = UUID(str(default_context["active_team"]["team_id"]))
    default_policy_id = service.set_risk_policy(
        actor_id=admin_id,
        version="shared-risk-v1",
        system_state=SystemRiskState.NORMAL,
        max_total_risk=Decimal("100"),
        max_account_risk=Decimal("80"),
        max_single_loss=Decimal("20"),
        max_consecutive_losses=3,
        loss_cooldown=timedelta(hours=1),
        max_fact_age=timedelta(minutes=5),
        now=now,
    )

    workspace_b = service.create_workspace(
        actor_id=admin_id,
        name="Risk Workspace B",
        slug="risk-workspace-b",
        idempotency_key="risk-workspace-b",
        now=now,
    )
    team_b = service.create_team(
        actor_id=admin_id,
        name="Risk Team B",
        slug="risk-team-b",
        idempotency_key="risk-team-b",
        now=now,
    )
    team_b_policy_id = service.configure_risk_policy(
        actor_id=admin_id,
        version="shared-risk-v1",
        max_total_risk=Decimal("50"),
        max_account_risk=Decimal("40"),
        max_single_loss=Decimal("10"),
        max_consecutive_losses=2,
        loss_cooldown=timedelta(hours=2),
        max_fact_age=timedelta(minutes=2),
        expected_revision=0,
        reason="explicit policy for second isolated team scope",
        idempotency_key="risk-team-b-policy",
        now=now,
    )
    team_b_status = service.risk_control_status(admin_id, (), now=now)
    assert team_b_status["policy"]["policy_id"] == str(team_b_policy_id)
    assert Decimal(team_b_status["policy"]["max_total_risk"]) == Decimal("50")

    service.select_scope(
        actor_id=admin_id,
        workspace_id=default_workspace_id,
        team_id=default_team_id,
        idempotency_key="risk-return-default",
        now=now,
    )
    default_status = service.risk_control_status(admin_id, (), now=now)
    assert default_status["policy"]["policy_id"] == str(default_policy_id)
    assert Decimal(default_status["policy"]["max_total_risk"]) == Decimal("100")

    with database.session_factory() as session:
        policies = session.scalars(
            select(RiskPolicy).where(RiskPolicy.active).order_by(RiskPolicy.team_id)
        ).all()
        assert {policy.team_id for policy in policies} == {default_team_id, team_b}
        assert {policy.version for policy in policies} == {"shared-risk-v1"}
        second_team = session.get(Team, team_b)
        assert second_team is not None
        assert second_team.workspace_id == workspace_b
        assert second_team.trading_enabled is False
