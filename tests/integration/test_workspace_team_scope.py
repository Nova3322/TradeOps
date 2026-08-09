from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from trading_control_plane.api import create_app
from trading_control_plane.config import Settings
from trading_control_plane.database import Database
from trading_control_plane.models import AuditEvent, RoleAssignment, Team, User, Workspace
from trading_control_plane.perptape import PerptapeClient
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
            assert workspace.json()["session"]["active_team"] is None
            assert workspace.json()["session"]["roles"] == []

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
