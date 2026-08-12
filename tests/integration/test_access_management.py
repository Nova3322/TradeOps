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
        runtime_binance_account_id="acct-live",
        runtime_hyperliquid_account_id="acct-hl",
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
    app = access_app(database)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client, "admin")
        assert (await client.get("/api/capital")).status_code == 200
        assert (await client.get("/admin/users")).status_code == 200

        created = await client.post(
            "/api/admin/users",
            json={
                "username": "reviewer-only",
                "password": "reviewer-only-password",
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
        assert reviewer["password_configured"] is True

        bybit_observer = await client.post(
            "/api/admin/users",
            json={
                "username": "bybit-observer",
                "password": "bybit-observer-password",
                "roles": ["OBSERVER"],
                "venue_scope": "BYBIT",
            },
        )
        assert bybit_observer.status_code == 200, bybit_observer.text
        assert next(
            item
            for item in bybit_observer.json()["data"]
            if item["user_id"] == bybit_observer.json()["user_id"]
        )["roles"] == [{"role": "OBSERVER", "account_scope": None, "venue_scope": "BYBIT"}]
        invalid_venue_scope = await client.post(
            "/api/admin/users",
            json={
                "username": "invalid-venue-observer",
                "password": "invalid-venue-observer-password",
                "roles": ["OBSERVER"],
                "venue_scope": "UNSUPPORTED",
            },
        )
        assert invalid_venue_scope.status_code == 422, invalid_venue_scope.text

        await client.post("/api/auth/logout")
        wrong_password = await client.post(
            "/api/auth/login",
            json={"username": "reviewer-only", "password": "incorrect-password-value"},
        )
        assert wrong_password.status_code == 401
        assert wrong_password.json()["detail"]["error_code"] == "LOGIN_DENIED"
        assert "reviewer-only" not in wrong_password.text
        password_login = await client.post(
            "/api/auth/login",
            json={"username": "reviewer-only", "password": "reviewer-only-password"},
        )
        assert password_login.status_code == 200, password_login.text
        assert password_login.json()["authentication_method"] == "PASSWORD"
        assert "HttpOnly" in password_login.headers["set-cookie"]
        assert "SameSite=strict" in password_login.headers["set-cookie"]
        password_session = password_login.json()["session"]
        stale_session_client = AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        )
        stale_session_login = await stale_session_client.post(
            "/api/auth/login",
            json={"username": "reviewer-only", "password": "reviewer-only-password"},
        )
        assert stale_session_login.status_code == 200, stale_session_login.text
        rotation_idempotency_key = "reviewer-password-rotation-1"
        wrong_current_password = await client.post(
            "/api/auth/password",
            json={
                "current_password": "incorrect-password-value",
                "new_password": "reviewer-only-password-v2",
                "expected_auth_version": password_session["auth_version"],
                "idempotency_key": "reviewer-password-wrong-current",
            },
        )
        assert wrong_current_password.status_code == 422, wrong_current_password.text
        assert wrong_current_password.json()["error"]["code"] == "CURRENT_PASSWORD_INVALID"
        password_change_payload = {
            "current_password": "reviewer-only-password",
            "new_password": "reviewer-only-password-v2",
            "expected_auth_version": password_session["auth_version"],
            "idempotency_key": rotation_idempotency_key,
        }
        password_change = await client.post("/api/auth/password", json=password_change_payload)
        assert password_change.status_code == 200, password_change.text
        assert password_change.json()["authentication_method"] == "PASSWORD"
        assert password_change.json()["other_sessions_revoked"] is True
        assert password_change.json()["session"]["auth_version"] == (
            password_session["auth_version"] + 1
        )
        assert "HttpOnly" in password_change.headers["set-cookie"]
        assert "SameSite=strict" in password_change.headers["set-cookie"]
        stale_session_check = await stale_session_client.get("/api/auth/session")
        assert stale_session_check.status_code == 401
        await stale_session_client.aclose()
        replay = await client.post("/api/auth/password", json=password_change_payload)
        assert replay.status_code == 200, replay.text
        assert replay.json()["session"]["auth_version"] == password_change.json()["session"][
            "auth_version"
        ]
        assert (await client.get("/api/auth/session")).status_code == 200
        with database.session_factory() as session:
            password_audit = session.scalar(
                select(AuditEvent).where(
                    AuditEvent.event_type == "USER_PASSWORD_CHANGED",
                    AuditEvent.idempotency_key == rotation_idempotency_key,
                )
            )
            assert password_audit is not None
            assert password_audit.actor_id == str(reviewer_id)
            assert "reviewer-only-password" not in password_audit.reason
        await client.post("/api/auth/logout")
        old_password_login = await client.post(
            "/api/auth/login",
            json={"username": "reviewer-only", "password": "reviewer-only-password"},
        )
        assert old_password_login.status_code == 401
        new_password_login = await client.post(
            "/api/auth/login",
            json={"username": "reviewer-only", "password": "reviewer-only-password-v2"},
        )
        assert new_password_login.status_code == 200, new_password_login.text
        await login(client, "admin")

        duplicate = await client.post(
            "/api/admin/users",
            json={
                "username": "reviewer-only",
                "password": "reviewer-only-password",
                "roles": ["REVIEWER"],
            },
        )
        assert duplicate.status_code == 409, duplicate.text
        assert duplicate.json()["error"]["code"] == "USERNAME_CONFLICT"

        mixed = await client.post(
            "/api/admin/users",
            json={
                "username": "mixed-non-admin",
                "password": "mixed-non-admin-password",
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
        assert (await client.get("/admin/users")).status_code == 403

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

        team_disabled_login = await client.post(
            "/api/auth/mock/login", json={"username": "reviewer-only"}
        )
        assert team_disabled_login.status_code == 200, team_disabled_login.text
        assert team_disabled_login.json()["session"]["active_team"] is None
        assert team_disabled_login.json()["session"]["roles"] == []

    with database.session_factory() as session:
        event_types = set(session.scalars(select(AuditEvent.event_type)).all())
        assert {"USER_ACCESS_CREATED", "USER_ACCESS_UPDATED"} <= event_types


def test_only_system_admin_manages_members_while_admin_keeps_highest_permissions(
    database: Database,
) -> None:
    asyncio.run(exercise_access_management(database))


async def exercise_six_identity_permission_matrix(database: Database) -> None:
    service = TradingService(database)
    admin_id = service.bootstrap_admin("matrix-admin", now=datetime.now(UTC))
    now = datetime.now(UTC)
    for venue, account_id in (
        ("BINANCE", "acct-live"),
        ("HYPERLIQUID", "acct-hl"),
        ("OKX", "acct-okx"),
        ("BYBIT", "acct-bybit"),
    ):
        service.create_exchange_account(
            actor_id=admin_id,
            account_id=account_id,
            venue=venue,
            label=f"{venue} matrix account",
            credentials=None,
            idempotency_key=f"matrix-{venue.lower()}-account",
            now=now,
        )
    app = access_app(database)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as admin_http:
        await login(admin_http, "matrix-admin")
        member_ids: dict[str, str] = {}
        for username, roles in {
            "matrix-proposer": ["PROPOSER"],
            "matrix-reviewer": ["REVIEWER"],
            "matrix-treasury": ["TREASURY_ADMIN"],
            "matrix-observer": ["OBSERVER"],
            "matrix-disabled": ["OBSERVER"],
        }.items():
            response = await admin_http.post(
                "/api/admin/users",
                json={
                    "username": username,
                    "password": f"{username}-password",
                    "roles": roles,
                },
            )
            assert response.status_code == 200, response.text
            member_ids[username] = response.json()["user_id"]

        endpoints = (
            "/api/opportunities",
            "/api/proposal-defaults",
            "/api/proposals",
            "/api/campaigns",
            "/api/campaign-exceptions",
            "/api/runtime/status",
            "/api/risk-controls",
            "/api/venues/binance/status",
            "/api/venues/binance/live/status",
            "/api/venues/binance/testnet/status",
            "/api/venues/hyperliquid/status",
            "/api/venues/hyperliquid/live/status",
            "/api/venues/hyperliquid/testnet/status",
            "/api/venues/binance/facts?account_id=acct-live",
            "/api/venues/hyperliquid/facts?account_id=acct-hl",
            "/api/venues/okx/facts?account_id=acct-okx",
            "/api/venues/bybit/facts?account_id=acct-bybit",
            "/api/capital",
            "/api/admin/users",
            "/admin/users",
        )
        allowed = {
            "matrix-admin": set(endpoints),
            "matrix-proposer": {
                "/api/opportunities",
                "/api/proposal-defaults",
                "/api/proposals",
            },
            "matrix-reviewer": {
                "/api/proposals",
                "/api/runtime/status",
                "/api/risk-controls",
            },
            "matrix-treasury": {"/api/capital"},
            "matrix-observer": {
                "/api/opportunities",
                "/api/proposals",
                "/api/campaigns",
                "/api/campaign-exceptions",
                "/api/runtime/status",
                "/api/risk-controls",
                "/api/venues/binance/status",
                "/api/venues/binance/live/status",
                "/api/venues/binance/testnet/status",
                "/api/venues/hyperliquid/status",
                "/api/venues/hyperliquid/live/status",
                "/api/venues/hyperliquid/testnet/status",
                "/api/venues/binance/facts?account_id=acct-live",
                "/api/venues/hyperliquid/facts?account_id=acct-hl",
                "/api/venues/okx/facts?account_id=acct-okx",
                "/api/venues/bybit/facts?account_id=acct-bybit",
            },
        }
        for username, permitted in allowed.items():
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as member_http:
                await login(member_http, username)
                for endpoint in endpoints:
                    response = await member_http.get(endpoint)
                    allowed_status = {"/api/opportunities": 503}.get(endpoint, 200)
                    expected_status = allowed_status if endpoint in permitted else 403
                    assert response.status_code == expected_status, (
                        username,
                        endpoint,
                        response.text,
                    )
                if username != "matrix-admin":
                    denied = await member_http.get("/api/admin/users")
                    assert "matrix-admin" not in denied.text
                    assert "matrix-proposer" not in denied.text

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as disabled_http:
            await login(disabled_http, "matrix-disabled")
            disabled = await admin_http.put(
                f"/api/admin/users/{member_ids['matrix-disabled']}/access",
                json={"roles": ["OBSERVER"], "active": False},
            )
            assert disabled.status_code == 200, disabled.text
            scoped_session = await disabled_http.get("/api/auth/session")
            assert scoped_session.status_code == 200
            assert scoped_session.json()["session"]["active_team"] is None
            assert (await disabled_http.get("/api/admin/users")).status_code == 403
            relogin = await disabled_http.post(
                "/api/auth/mock/login", json={"username": "matrix-disabled"}
            )
            assert relogin.status_code == 200
            assert relogin.json()["session"]["active_team"] is None

    assert service.can_user(admin_id, "user.manage") is True


def test_six_identity_permission_matrix_is_enforced_by_api_and_member_route(
    database: Database,
) -> None:
    asyncio.run(exercise_six_identity_permission_matrix(database))
