from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from trading_control_plane.api import create_app
from trading_control_plane.config import Settings
from trading_control_plane.credentials import CredentialCipher
from trading_control_plane.database import Database
from trading_control_plane.domain import DomainRejected
from trading_control_plane.models import AuditEvent, ExchangeAccount
from trading_control_plane.perptape import PerptapeClient
from trading_control_plane.queries import TradingQueries
from trading_control_plane.service import TradingService


def encryption_key() -> str:
    return base64.urlsafe_b64encode(b"exchange-account-test-key-32byt!"[:32]).decode().rstrip("=")


def test_exchange_account_credentials_are_encrypted_scoped_and_never_projected(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    service = TradingService(database, credential_encryption_key=encryption_key())
    admin = service.bootstrap_admin("account-admin", now=now)
    account_uuid = service.create_exchange_account(
        actor_id=admin,
        account_id="binance-main",
        venue="BINANCE",
        label="Main Binance",
        credentials={"api_key": "public-key-7890", "api_secret": "must-not-leak"},
        idempotency_key="account-create-1",
        now=now,
    )

    projection = TradingQueries(database).exchange_accounts(admin)
    assert projection["can_manage"] is True
    assert projection["data"][0]["connection"] == {
        "status": "NOT_VERIFIED",
        "error_code": None,
        "last_verified_at": None,
        "read_only_capability": False,
    }
    assert projection["data"][0]["trading"]["enabled"] is False
    assert projection["data"][0]["credentials"] == {
        "state": "CONFIGURED",
        "version": 1,
        "configured_fields": ["api_key", "api_secret"],
        "key_hint": "••••7890",
        "signing_material_configured": True,
    }
    assert "must-not-leak" not in repr(projection)
    with database.session_factory() as session:
        account = session.get(ExchangeAccount, account_uuid)
        audit = session.scalar(
            select(AuditEvent).where(
                AuditEvent.object_type == "ExchangeAccount",
                AuditEvent.object_id == str(account_uuid),
                AuditEvent.event_type == "EXCHANGE_ACCOUNT_CREATED",
            )
        )
        assert account is not None and account.credentials_ciphertext is not None
        assert "must-not-leak" not in account.credentials_ciphertext
        assert audit is not None and "must-not-leak" not in audit.reason
        assert (
            CredentialCipher(encryption_key()).decrypt(
                account.credentials_ciphertext,
                team_id=account.team_id,
                exchange_account_id=account.exchange_account_id,
                venue=account.venue,
                credential_version=account.credential_version,
            )["api_secret"]
            == "must-not-leak"  # noqa: S105 - inert credential round-trip fixture
        )

    version = service.rotate_exchange_account_credentials(
        account_uuid,
        actor_id=admin,
        credentials={"api_key": "replacement-4321", "api_secret": "replacement-secret"},
        expected_version=1,
        idempotency_key="account-rotate-1",
        now=now + timedelta(seconds=1),
    )
    assert version == 2
    rotated = TradingQueries(database).exchange_accounts(admin)["data"][0]
    assert rotated["version"] == 2
    assert rotated["credentials"]["version"] == 2
    assert rotated["credentials"]["key_hint"] == "••••4321"
    assert rotated["connection"]["status"] == "NOT_VERIFIED"
    assert rotated["trading"]["enabled"] is False


def test_account_setup_is_allowed_before_team_activation_and_cross_team_rotation_is_denied(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    service = TradingService(database, credential_encryption_key=encryption_key())
    admin = service.bootstrap_admin("team-account-admin", now=now)
    initial = TradingQueries(database).user_context(admin)
    default_workspace_id = UUID(initial["active_workspace"]["workspace_id"])
    default_team_id = UUID(initial["active_team"]["team_id"])
    second_team_id = service.create_team(
        actor_id=admin,
        name="Second Team",
        slug="second-team",
        idempotency_key="second-team-create",
        now=now,
    )
    second_account = service.create_exchange_account(
        actor_id=admin,
        account_id="okx-strategy-1",
        venue="OKX",
        label="OKX Strategy 1",
        credentials={"api_key": "key", "api_secret": "secret", "passphrase": "phrase"},
        idempotency_key="second-account-create",
        now=now,
    )
    context = TradingQueries(database).user_context(admin)
    assert context["active_team"]["trading_enabled"] is False
    assert TradingQueries(database).exchange_accounts(admin)["data"][0]["team_id"] == str(
        second_team_id
    )

    service.select_scope(
        actor_id=admin,
        workspace_id=default_workspace_id,
        team_id=default_team_id,
        idempotency_key="return-default-team",
        now=now + timedelta(seconds=1),
    )
    with pytest.raises(DomainRejected, match="EXCHANGE_ACCOUNT_NOT_FOUND"):
        service.rotate_exchange_account_credentials(
            second_account,
            actor_id=admin,
            credentials={"api_key": "new", "api_secret": "new", "passphrase": "new"},
            expected_version=1,
            idempotency_key="cross-team-rotation",
            now=now + timedelta(seconds=2),
        )


def test_exchange_account_api_masks_credentials_and_exposes_connector_truth(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    TradingService(database).bootstrap_admin("api-account-admin", now=now)
    settings = Settings(
        environment="test",
        database_url=str(database.engine.url),
        allow_mock_identity=True,
        session_signing_secret="account-api-signing-secret-that-is-long-enough",  # noqa: S106
        credential_encryption_key=encryption_key(),
        runtime_binance_account_id="binance-api-main",
        public_base_url="http://test",
        _env_file=None,
    )
    app = create_app(
        settings,
        database,
        PerptapeClient(
            base_url="https://perptape.com",
            api_key=None,
            contract_version="breakouts-v1",
            cache_ttl=timedelta(minutes=1),
        ),
    )

    async def scenario() -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            login = await client.post(
                "/api/auth/mock/login", json={"username": "api-account-admin"}
            )
            assert login.status_code == 200
            created = await client.post(
                "/api/exchange-accounts",
                json={
                    "account_id": "binance-api-main",
                    "venue": "BINANCE",
                    "label": "API Main",
                    "credentials": {
                        "api_key": "api-key-2468",
                        "api_secret": "api-secret-never-return",
                    },
                    "idempotency_key": "api-account-create",
                },
            )
            assert created.status_code == 200, created.text
            assert "api-secret-never-return" not in created.text
            listed = await client.get("/api/exchange-accounts")
            assert listed.status_code == 200, listed.text
            assert "api-secret-never-return" not in listed.text
            item = listed.json()["data"]["data"][0]
            assert item["runtime_binding"] == {
                "bound": True,
                "source": "PROCESS_ENVIRONMENT",
                "read_only_connector": "IMPLEMENTED",
                "trading_connector": "FREQTRADE_EXTERNAL",
            }
            assert item["connection"]["status"] == "NOT_VERIFIED"
            assert item["trading"]["enabled"] is False
            page = await client.get("/venues")
            assert page.status_code == 200
            assert "api-secret-never-return" not in page.text

    asyncio.run(scenario())
