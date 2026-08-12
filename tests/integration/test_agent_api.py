from __future__ import annotations

import asyncio
import base64
import json
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from uuid import UUID

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select

from trading_control_plane.api import create_app
from trading_control_plane.config import Settings
from trading_control_plane.database import Database
from trading_control_plane.domain import Role
from trading_control_plane.models import (
    ApiClient,
    AuditEvent,
    CommandReceipt,
    RoleAssignment,
    Team,
    TeamMembership,
    User,
)
from trading_control_plane.perptape import PerptapeClient
from trading_control_plane.queries import TradingQueries
from trading_control_plane.request_context import (
    ApiClientRequestContext,
    bind_api_client_context,
    reset_api_client_context,
)
from trading_control_plane.service import TradingService


def encryption_key() -> str:
    return base64.urlsafe_b64encode(b"api-client-integration-key-32bytes"[:32]).decode().rstrip("=")


def api_client_app(database: Database) -> FastAPI:
    settings = Settings(
        environment="test",
        database_url=str(database.engine.url),
        allow_mock_identity=True,
        session_signing_secret=secrets.token_urlsafe(32),
        credential_encryption_key=encryption_key(),
        public_base_url="http://test",
        runtime_sync_enabled=False,
        _env_file=None,  # type: ignore[call-arg]
    )
    perptape = PerptapeClient(
        base_url="https://perptape.com",
        api_key=None,
        contract_version="breakouts-v1",
        cache_ttl=timedelta(minutes=1),
        fetcher=lambda *_args: {"data": []},
    )
    return create_app(settings, database, perptape)


def test_user_owned_api_key_dynamic_rbac_scope_audit_and_token_lifecycle(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    service = TradingService(database, credential_encryption_key=encryption_key())
    admin_id = service.bootstrap_admin("api-client-admin", now=now)
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
    first_account_uuid = service.create_exchange_account(
        actor_id=admin_id,
        account_id="paper-api-1",
        venue="BINANCE",
        label="Primary API Key account",
        credentials=None,
        idempotency_key="api-account-one",
        now=now,
    )
    service.create_exchange_account(
        actor_id=admin_id,
        account_id="paper-api-2",
        venue="BINANCE",
        label="Second RBAC account",
        credentials=None,
        idempotency_key="api-account-two",
        now=now,
    )
    with database.session_factory() as session:
        admin = session.get(User, admin_id)
        assert admin is not None and admin.active_team_id is not None
        team = session.get(Team, admin.active_team_id)
        assert team is not None
        assert team.execution_mode == "LIVE"
        assert team.execution_mode_locked_at == now
        team_id = team.team_id
        team_version = team.version
    switched = service.set_team_execution_mode(
        actor_id=admin_id,
        team_id=team_id,
        mode="SHADOW",
        confirmation="SWITCH_TO_SHADOW",
        expected_version=team_version,
        idempotency_key="api-key-shadow-mode",
        now=now,
    )
    assert switched["execution_mode"] == "SHADOW"
    owner_id = service.create_managed_user(
        "api-owner",
        [Role.OBSERVER, Role.PROPOSER, Role.REVIEWER],
        admin_id,
        None,
        None,
        "ordinary-user-password",
        now=now,
    )
    app = api_client_app(database)

    async def bearer_get(token: str, path: str = "/api/api-key/connection") -> Response:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            return await client.get(path, headers={"Authorization": f"Bearer {token}"})

    async def scenario() -> dict[str, str]:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as owner:
            login = await owner.post("/api/auth/mock/login", json={"username": "api-owner"})
            assert login.status_code == 200, login.text
            session = login.json()["session"]
            workspace_id = session["active_workspace"]["workspace_id"]
            team_id = session["active_team"]["team_id"]
            assert session["principal_type"] == "HUMAN"

            page = await owner.get("/profile/api-keys")
            assert page.status_code == 200
            assert 'href="/profile/api-keys"' in page.text
            assert 'href="/admin/agents"' not in page.text
            assert "/assets/app.js?v=175" in page.text

            scopes = await owner.get("/api/profile/api-key-contexts")
            assert scopes.status_code == 200, scopes.text
            assert len(scopes.json()["data"]) == 1
            assert scopes.json()["data"][0]["scope_model"] == "USER_RBAC"
            assert scopes.json()["data"][0]["account_id"] is None
            assert scopes.json()["data"][0]["venue"] is None

            create_payload = {
                "name": "owner-alpha",
                "workspace_id": workspace_id,
                "team_id": team_id,
                "expires_in_days": 30,
                "idempotency_key": "create-owner-alpha",
            }
            created = await owner.post("/api/profile/api-keys", json=create_payload)
            assert created.status_code == 200, created.text
            alpha = created.json()["result"]
            alpha_token = alpha["token"]
            alpha_id = alpha["api_key_id"]
            assert alpha["owner_user_id"] == str(owner_id)
            assert alpha["display_once"] is True
            assert alpha_token.startswith("tradingops_api_v1.")

            replay = await owner.post("/api/profile/api-keys", json=create_payload)
            assert replay.status_code == 200, replay.text
            assert replay.json()["result"]["token"] is None
            assert replay.json()["result"]["display_once"] is False

            second = await owner.post(
                "/api/profile/api-clients",
                json={
                    **create_payload,
                    "name": "owner-beta",
                    "account_id": "paper-api-1",
                    "venue": "BINANCE",
                    "idempotency_key": "create-owner-beta",
                },
            )
            assert second.status_code == 200, second.text
            beta = second.json()["result"]
            beta_token = beta["token"]
            beta_id = beta["api_key_id"]

            listed = await owner.get("/api/profile/api-keys")
            assert listed.status_code == 200, listed.text
            assert {item["permissions_source"] for item in listed.json()["data"]} == {
                "HUMAN_DYNAMIC"
            }
            assert {tuple(item["effective_roles"]) for item in listed.json()["data"]} == {
                ("OBSERVER", "PROPOSER", "REVIEWER")
            }
            for token in (alpha_token, beta_token):
                assert token not in listed.text
            assert "token_digest" not in listed.text

            ambiguous = await owner.get(
                "/api/auth/session",
                headers={"Authorization": f"Bearer {alpha_token}"},
            )
            assert ambiguous.status_code == 400
            assert ambiguous.json()["error"]["code"] == "AUTH_CREDENTIAL_AMBIGUOUS"

        connection = await bearer_get(alpha_token)
        assert connection.status_code == 200, connection.text
        assert connection.json()["owner_user_id"] == str(owner_id)
        assert connection.json()["api_key_id"] == alpha_id
        assert connection.json()["permissions_source"] == "HUMAN_DYNAMIC"
        assert [item["role"] for item in connection.json()["effective_roles"]] == [
            "OBSERVER",
            "PROPOSER",
            "REVIEWER",
        ]
        assert connection.json()["scope"]["account_id"] is None
        assert connection.json()["scope"]["venue"] is None
        assert connection.json()["scope"]["scope_model"] == "USER_RBAC"

        proposal_payload = {
            "environment": "SHADOW",
            "account_id": "paper-api-1",
            "venue": "BINANCE",
            "instrument_id": str(instrument_id),
            "direction": "LONG",
            "risk_tier": "LOW",
            "quantity": "0.01",
            "max_risk": "25",
            "expires_in_minutes": 480,
            "trigger_price": "100000",
            "invalidation_price": "95000",
            "limit_price": "99900",
            "model_id": "alpha-breakout",
            "model_version": "2026.08.1",
            "request_id": "request-api-client-0001",
            "generated_at": datetime.now(UTC).isoformat(),
            "rationale": "current model facts support a frozen long proposal",
            "idempotency_key": "api-client-proposal-0001",
        }
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {alpha_token}"},
        ) as alpha_client:
            proposed = await alpha_client.post("/api/api-key/proposals", json=proposal_payload)
            assert proposed.status_code == 200, proposed.text
            proposal_id = proposed.json()["proposal_id"]
            assert proposed.json()["detail"]["proposer_id"] == str(owner_id)
            assert proposed.json()["detail"]["frozen_payload"]["details"]["agent"] == {
                "owner_user_id": str(owner_id),
                "api_client_id": alpha_id,
                "model_id": "alpha-breakout",
                "model_version": "2026.08.1",
                "request_id": "request-api-client-0001",
                "generated_at": proposed.json()["detail"]["frozen_payload"]["details"]["agent"][
                    "generated_at"
                ],
            }

            cross_scope = await alpha_client.post(
                "/api/agent/proposals",
                json={
                    **proposal_payload,
                    "account_id": "paper-api-2",
                    "request_id": "request-api-client-0002",
                    "idempotency_key": "api-client-cross-scope",
                },
            )
            assert cross_scope.status_code == 200, cross_scope.text
            assert cross_scope.json()["detail"]["account_id"] == "paper-api-2"

            accounts = await alpha_client.get("/api/exchange-accounts")
            assert accounts.status_code == 200, accounts.text
            assert {item["account_id"] for item in accounts.json()["data"]["data"]} == {
                "paper-api-1",
                "paper-api-2",
            }
            approval_without_human_step_up = await alpha_client.post(
                f"/api/proposals/{proposal_id}/reviews",
                json={
                    "decision": "APPROVE",
                    "reason": "API Key must not possess a human action grant",
                    "expected_version": 2,
                    "idempotency_key": "alpha-approval-denied",
                },
            )
            assert approval_without_human_step_up.status_code == 403
            assert approval_without_human_step_up.json()["error"]["code"] == (
                "HUMAN_WEB_CONFIRMATION_REQUIRED"
            )

            client_inventory = await alpha_client.get("/api/profile/api-keys")
            assert client_inventory.status_code == 403
            assert client_inventory.json()["error"]["code"] == ("HUMAN_WEB_CONFIRMATION_REQUIRED")

            credential_write = await alpha_client.put(
                f"/api/exchange-accounts/{first_account_uuid}/credentials",
                json={
                    "credentials": {"api_key": "must-not-pass", "api_secret": "must-not-pass"},
                    "expected_version": 1,
                    "idempotency_key": "client-credential-denied",
                },
            )
            assert credential_write.status_code == 403
            assert credential_write.json()["error"]["code"] == ("HUMAN_WEB_CONFIRMATION_REQUIRED")

            risk_decision = await alpha_client.post(
                f"/api/proposals/{proposal_id}/risk-decisions",
                json={"idempotency_key": "client-risk-decision-denied"},
            )
            assert risk_decision.status_code == 403
            assert risk_decision.json()["error"]["code"] == ("HUMAN_WEB_CONFIRMATION_REQUIRED")

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {beta_token}"},
        ) as beta_client:
            same_subject_review = await beta_client.post(
                f"/api/proposals/{proposal_id}/reviews",
                json={
                    "decision": "REJECT",
                    "reason": "same owner cannot become an independent reviewer",
                    "expected_version": 2,
                    "idempotency_key": "beta-self-review",
                },
            )
            assert same_subject_review.status_code == 403
            assert same_subject_review.json()["error"]["code"] == "SELF_REVIEW_FORBIDDEN"

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as human_owner:
            login = await human_owner.post("/api/auth/mock/login", json={"username": "api-owner"})
            assert login.status_code == 200
            owner_self_review = await human_owner.post(
                f"/api/proposals/{proposal_id}/reviews",
                json={
                    "decision": "REJECT",
                    "reason": "owner is the same approval subject",
                    "expected_version": 2,
                },
            )
            assert owner_self_review.status_code == 403
            assert owner_self_review.json()["error"]["code"] == "SELF_REVIEW_FORBIDDEN"

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as admin:
            login = await admin.post("/api/auth/mock/login", json={"username": "api-client-admin"})
            assert login.status_code == 200
            tightened = await admin.put(
                f"/api/admin/users/{owner_id}/access",
                json={
                    "roles": ["OBSERVER"],
                    "active": True,
                    "account_scope": "paper-api-1",
                    "venue_scope": "BINANCE",
                },
            )
            assert tightened.status_code == 200, tightened.text

        tightened_connection = await bearer_get(alpha_token)
        assert tightened_connection.status_code == 200, tightened_connection.text
        assert [item["role"] for item in tightened_connection.json()["effective_roles"]] == [
            "OBSERVER"
        ]
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {alpha_token}"},
        ) as tightened_client:
            denied = await tightened_client.post(
                "/api/api-key/proposals",
                json={
                    **proposal_payload,
                    "account_id": "paper-api-2",
                    "request_id": "request-api-client-0003",
                    "idempotency_key": "api-client-after-tightening",
                },
            )
            assert denied.status_code == 403
            assert denied.json()["error"]["code"] == "RBAC_DENIED"

        context_token = bind_api_client_context(
            ApiClientRequestContext(
                owner_user_id=owner_id,
                api_client_id=UUID(alpha_id),
                workspace_id=UUID(connection.json()["scope"]["workspace_id"]),
                team_id=UUID(connection.json()["scope"]["team_id"]),
            )
        )
        try:
            queries = TradingQueries(database)
            with patch.object(queries.service, "shadow_activation_status", return_value={}):
                shadow = queries.shadow_workspace(owner_id)
        finally:
            reset_api_client_context(context_token)
        assert [item["account_id"] for item in shadow["accounts"]] == ["paper-api-1"]

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as admin:
            login = await admin.post("/api/auth/mock/login", json={"username": "api-client-admin"})
            assert login.status_code == 200
            restored = await admin.put(
                f"/api/admin/users/{owner_id}/access",
                json={
                    "roles": ["PROPOSER", "REVIEWER"],
                    "active": True,
                    "account_scope": "paper-api-1",
                    "venue_scope": "BINANCE",
                },
            )
            assert restored.status_code == 200, restored.text

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as owner:
            login = await owner.post("/api/auth/mock/login", json={"username": "api-owner"})
            assert login.status_code == 200
            rotated = await owner.post(
                f"/api/profile/api-keys/{beta_id}/rotations",
                json={
                    "expected_token_version": 1,
                    "expires_in_days": 30,
                    "idempotency_key": "rotate-owner-beta",
                },
            )
            assert rotated.status_code == 200, rotated.text
            rotated_beta_token = rotated.json()["result"]["token"]
            assert rotated.json()["result"]["display_once"] is True
            rotate_replay = await owner.post(
                f"/api/profile/api-keys/{beta_id}/rotations",
                json={
                    "expected_token_version": 1,
                    "expires_in_days": 30,
                    "idempotency_key": "rotate-owner-beta",
                },
            )
            assert rotate_replay.status_code == 200
            assert rotate_replay.json()["result"]["token"] is None

            listed = await owner.get("/api/profile/api-keys")
            beta_row = next(
                item for item in listed.json()["data"] if item["api_client_id"] == beta_id
            )
            disabled = await owner.put(
                f"/api/profile/api-keys/{beta_id}/state",
                json={
                    "active": False,
                    "expected_version": beta_row["version"],
                    "idempotency_key": "disable-owner-beta",
                },
            )
            assert disabled.status_code == 200, disabled.text
            disabled_version = disabled.json()["result"]["version"]

        assert (await bearer_get(beta_token)).status_code == 401
        assert (await bearer_get(rotated_beta_token)).status_code == 401

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as owner:
            login = await owner.post("/api/auth/mock/login", json={"username": "api-owner"})
            assert login.status_code == 200
            enabled = await owner.put(
                f"/api/profile/api-keys/{beta_id}/state",
                json={
                    "active": True,
                    "expected_version": disabled_version,
                    "idempotency_key": "enable-owner-beta",
                },
            )
            assert enabled.status_code == 200, enabled.text
            enabled_version = enabled.json()["result"]["version"]

        assert (await bearer_get(rotated_beta_token)).status_code == 200

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as owner:
            login = await owner.post("/api/auth/mock/login", json={"username": "api-owner"})
            assert login.status_code == 200
            revoked = await owner.post(
                f"/api/profile/api-keys/{beta_id}/revoke",
                json={
                    "expected_version": enabled_version,
                    "idempotency_key": "revoke-owner-beta",
                },
            )
            assert revoked.status_code == 200, revoked.text
            revoked_version = revoked.json()["result"]["version"]
            cannot_enable = await owner.put(
                f"/api/profile/api-keys/{beta_id}/state",
                json={
                    "active": True,
                    "expected_version": revoked_version,
                    "idempotency_key": "reenable-revoked-beta",
                },
            )
            assert cannot_enable.status_code == 409
            assert cannot_enable.json()["error"]["code"] == "API_CLIENT_REVOKED"

        assert (await bearer_get(rotated_beta_token)).status_code == 401

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as admin:
            login = await admin.post("/api/auth/mock/login", json={"username": "api-client-admin"})
            assert login.status_code == 200
            left_team = await admin.put(
                f"/api/admin/users/{owner_id}/access",
                json={
                    "roles": ["PROPOSER", "REVIEWER"],
                    "active": False,
                    "account_scope": "paper-api-1",
                    "venue_scope": "BINANCE",
                },
            )
            assert left_team.status_code == 200, left_team.text

        assert (await bearer_get(alpha_token)).status_code == 401

        with database.session_factory.begin() as session:
            db_owner = session.get(User, owner_id, with_for_update=True)
            membership = session.scalar(
                select(TeamMembership).where(TeamMembership.user_id == owner_id)
            )
            assert db_owner is not None and membership is not None
            membership.active = True
            db_owner.active_team_id = membership.team_id
            db_owner.active = False

        assert (await bearer_get(alpha_token)).status_code == 401

        with database.session_factory.begin() as session:
            db_owner = session.get(User, owner_id, with_for_update=True)
            assert db_owner is not None
            db_owner.active = True

        assert (await bearer_get(alpha_token)).status_code == 200
        with database.session_factory.begin() as session:
            alpha_client = session.get(ApiClient, UUID(alpha_id), with_for_update=True)
            assert alpha_client is not None
            alpha_client.token_expires_at = datetime.now(UTC) - timedelta(seconds=1)

        expired = await bearer_get(alpha_token)
        assert expired.status_code == 401
        assert expired.json()["error"]["code"] == "AGENT_TOKEN_EXPIRED"
        return {
            "alpha_id": alpha_id,
            "beta_id": beta_id,
            "alpha_token": alpha_token,
            "beta_token": beta_token,
            "rotated_beta_token": rotated_beta_token,
            "proposal_id": proposal_id,
        }

    evidence = asyncio.run(scenario())

    with database.session_factory() as session:
        api_clients = session.scalars(select(ApiClient).order_by(ApiClient.name)).all()
        service_users = session.scalars(select(User).where(User.principal_type == "SERVICE")).all()
        client_role_rows = session.scalars(
            select(RoleAssignment).where(
                RoleAssignment.user_id.in_([UUID(evidence["alpha_id"]), UUID(evidence["beta_id"])])
            )
        ).all()
        proposal_audit = session.scalar(
            select(AuditEvent).where(
                AuditEvent.event_type == "PROPOSAL_CREATED",
                AuditEvent.object_id == evidence["proposal_id"],
            )
        )
        audits = session.scalars(select(AuditEvent)).all()
        receipts = session.scalars(select(CommandReceipt)).all()
        assert len(api_clients) == 2
        assert all(client.account_id is None and client.venue is None for client in api_clients)
        assert service_users == []
        assert client_role_rows == []
        assert proposal_audit is not None
        assert proposal_audit.actor_id == str(owner_id)
        assert proposal_audit.api_client_id == UUID(evidence["alpha_id"])
        assert any(
            item.caller_id.startswith(f"api-client:{evidence['alpha_id']}:") for item in receipts
        )
        serialized = json.dumps(
            {
                "audits": [
                    {
                        "actor_id": item.actor_id,
                        "api_client_id": (
                            None if item.api_client_id is None else str(item.api_client_id)
                        ),
                        "reason": item.reason,
                    }
                    for item in audits
                ],
                "receipts": [item.response for item in receipts],
            },
            sort_keys=True,
        )
        for token in (
            evidence["alpha_token"],
            evidence["beta_token"],
            evidence["rotated_beta_token"],
        ):
            assert token not in serialized
        assert all(client.token_digest not in serialized for client in api_clients)
