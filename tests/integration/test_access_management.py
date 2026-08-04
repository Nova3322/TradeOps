from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from trading_control_plane.api import create_app
from trading_control_plane.config import Settings
from trading_control_plane.database import Database
from trading_control_plane.models import AuditEvent
from trading_control_plane.perptape import PerptapeClient
from trading_control_plane.service import TradingService


def access_app(database: Database):
    settings = Settings(
        environment="test",
        database_url=str(database.engine.url),
        allow_mock_identity=True,
        session_signing_secret="access-test-signing-secret-that-is-long-enough",  # noqa: S106
        public_base_url="http://test",
        _env_file=None,
    )
    perptape = PerptapeClient(
        base_url="https://perptape.com",
        api_key=None,
        contract_version="breakouts-v1",
        cache_ttl=timedelta(minutes=1),
    )
    return create_app(settings, database, perptape)


async def login(client: AsyncClient, username: str) -> None:
    response = await client.post("/api/auth/mock/login", json={"username": username})
    assert response.status_code == 200, response.text


async def exercise_access_management(database: Database) -> None:
    admin_id = TradingService(database).bootstrap_admin("admin", now=datetime.now(UTC))
    async with AsyncClient(
        transport=ASGITransport(app=access_app(database)), base_url="http://test"
    ) as client:
        await login(client, "admin")
        assert (await client.get("/api/capital")).status_code == 200

        created = await client.post(
            "/api/admin/users",
            json={
                "username": "reviewer-only",
                "roles": ["REVIEWER"],
                "account_scope": "acct-live",
                "venue_scope": "BINANCE",
            },
        )
        assert created.status_code == 200, created.text
        reviewer_id = created.json()["user_id"]
        reviewer = next(item for item in created.json()["data"] if item["user_id"] == reviewer_id)
        assert reviewer["roles"] == [
            {
                "role": "REVIEWER",
                "account_scope": "acct-live",
                "venue_scope": "BINANCE",
            }
        ]

        duplicate = await client.post(
            "/api/admin/users",
            json={"username": "reviewer-only", "roles": ["REVIEWER"]},
        )
        assert duplicate.status_code == 409, duplicate.text
        assert duplicate.json()["error"]["code"] == "USERNAME_CONFLICT"

        mixed = await client.post(
            "/api/admin/users",
            json={
                "username": "mixed-non-admin",
                "roles": ["PROPOSER", "REVIEWER", "OPERATOR", "TREASURY_ADMIN"],
            },
        )
        assert mixed.status_code == 200, mixed.text

        self_change = await client.put(
            f"/api/admin/users/{admin_id}/access",
            json={"roles": ["SYSTEM_ADMIN"], "active": True},
        )
        assert self_change.status_code == 403, self_change.text
        assert self_change.json()["error"]["code"] == "SELF_ACCESS_CHANGE_DENIED"

        await login(client, "reviewer-only")
        assert (await client.get("/api/proposals")).status_code == 200
        assert (await client.get("/api/opportunities")).status_code == 403
        assert (await client.get("/api/campaigns")).status_code == 403
        assert (await client.get("/api/capital")).status_code == 403
        assert (await client.get("/api/admin/users")).status_code == 403

        await login(client, "mixed-non-admin")
        assert (await client.get("/api/capital")).status_code == 200
        denied_members = await client.get("/api/admin/users")
        assert denied_members.status_code == 403
        assert "reviewer-only" not in denied_members.text
        assert "mixed-non-admin" not in denied_members.text

        await login(client, "admin")
        disabled = await client.put(
            f"/api/admin/users/{reviewer_id}/access",
            json={
                "roles": ["REVIEWER"],
                "active": False,
                "account_scope": "acct-live",
                "venue_scope": "BINANCE",
            },
        )
        assert disabled.status_code == 200, disabled.text
        assert (
            next(item for item in disabled.json()["data"] if item["user_id"] == reviewer_id)[
                "active"
            ]
            is False
        )

        denied_login = await client.post("/api/auth/mock/login", json={"username": "reviewer-only"})
        assert denied_login.status_code == 401, denied_login.text

    with database.session_factory() as session:
        event_types = set(session.scalars(select(AuditEvent.event_type)).all())
        assert {"USER_ACCESS_CREATED", "USER_ACCESS_UPDATED"} <= event_types


def test_only_system_admin_manages_members_while_admin_keeps_highest_permissions(
    database: Database,
) -> None:
    asyncio.run(exercise_access_management(database))
