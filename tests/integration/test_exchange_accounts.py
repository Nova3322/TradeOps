from __future__ import annotations

import asyncio
import base64
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from trading_control_plane.api import create_app
from trading_control_plane.config import Settings
from trading_control_plane.credentials import CredentialCipher
from trading_control_plane.database import Database
from trading_control_plane.domain import DomainRejected, ExecutionEnvironment, Role
from trading_control_plane.exchange_connection import ConnectionProbeResult
from trading_control_plane.models import AuditEvent, ExchangeAccount, RoleAssignment, Team, User
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
        "checked_at": None,
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
    with pytest.raises(DomainRejected, match="EXCHANGE_ACCOUNT_NOT_FOUND"):
        service.prepare_exchange_account_connection_verification(
            second_account,
            actor_id=admin,
            expected_version=1,
            idempotency_key="cross-team-connection-check",
        )


def test_account_facts_and_reconciliation_are_isolated_by_active_team(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    service = TradingService(database)
    admin = service.bootstrap_admin("fact-scope-admin", now=now)
    initial = TradingQueries(database).user_context(admin)
    workspace_id = UUID(initial["active_workspace"]["workspace_id"])
    default_team_id = UUID(initial["active_team"]["team_id"])
    instrument_id = service.register_instrument(
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
        now=now,
    )
    default_position_id = service.record_position(
        "shared-account",
        "BINANCE",
        instrument_id,
        Decimal(0),
        Decimal(0),
        Decimal("100"),
        True,
        admin,
        environment=ExecutionEnvironment.SHADOW,
        now=now,
    )
    service.record_account_equity(
        "shared-account",
        "BINANCE",
        Decimal("1000"),
        Decimal("900"),
        "USDT",
        True,
        admin,
        environment=ExecutionEnvironment.SHADOW,
        now=now,
    )
    default_reconciliation_id = service.reconcile_scope(
        "SHADOW:shared-account:BINANCE", admin, now=now
    )

    second_team_id = service.create_team(
        actor_id=admin,
        name="Fact Scope Two",
        slug="fact-scope-two",
        idempotency_key="fact-scope-two-create",
        now=now,
    )
    with database.session_factory.begin() as session:
        second_team = session.get(Team, second_team_id, with_for_update=True)
        assert second_team is not None
        second_team.trading_enabled = True

    second_position_id = service.record_position(
        "shared-account",
        "BINANCE",
        instrument_id,
        Decimal(0),
        Decimal(0),
        Decimal("200"),
        True,
        admin,
        environment=ExecutionEnvironment.SHADOW,
        now=now,
    )
    service.record_account_equity(
        "shared-account",
        "BINANCE",
        Decimal("2000"),
        Decimal("1800"),
        "USDT",
        True,
        admin,
        environment=ExecutionEnvironment.SHADOW,
        now=now,
    )
    second_reconciliation_id = service.reconcile_scope(
        "SHADOW:shared-account:BINANCE", admin, now=now
    )
    second_facts = TradingQueries(database).venue_facts(
        admin, "shared-account", "BINANCE", "SHADOW"
    )
    assert second_facts["team_id"] == str(second_team_id)
    assert second_facts["positions"][0]["position_id"] == str(second_position_id)
    assert second_facts["positions"][0]["mark_price"] == "200.000000000000000000"
    assert second_facts["equity"]["equity"] == "2000.000000000000000000"
    assert second_facts["reconciliation"]["reconciliation_id"] == str(second_reconciliation_id)
    with pytest.raises(DomainRejected, match="TEAM_SCOPE_DENIED"):
        service.record_protection(
            default_position_id,
            "cross-team-protection",
            Decimal(0),
            Decimal(0),
            True,
            admin,
            now=now,
        )

    service.select_scope(
        actor_id=admin,
        workspace_id=workspace_id,
        team_id=default_team_id,
        idempotency_key="fact-scope-return-default",
        now=now,
    )
    default_facts = TradingQueries(database).venue_facts(
        admin, "shared-account", "BINANCE", "SHADOW"
    )
    assert default_facts["team_id"] == str(default_team_id)
    assert default_facts["positions"][0]["position_id"] == str(default_position_id)
    assert default_facts["positions"][0]["mark_price"] == "100.000000000000000000"
    assert default_facts["equity"]["equity"] == "1000.000000000000000000"
    assert default_facts["reconciliation"]["reconciliation_id"] == str(default_reconciliation_id)


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
                "bound": False,
                "source": "DATABASE_ENVELOPE",
                "read_only_connector": "IMPLEMENTED",
                "connection_verification_connector": "IMPLEMENTED",
                "connection_verification_source": "DATABASE_ENVELOPE",
                "service_principal_configured": False,
                "trading_connector": "FREQTRADE_EXTERNAL",
            }
            assert item["connection"]["status"] == "NOT_VERIFIED"
            assert item["trading"]["enabled"] is False
            page = await client.get("/venues")
            assert page.status_code == 200
            assert "api-secret-never-return" not in page.text

    asyncio.run(scenario())


class FakeExchangeConnectionVerifier:
    def __init__(self, outcomes: Mapping[str, ConnectionProbeResult]) -> None:
        self.outcomes = outcomes
        self.calls: list[tuple[str, dict[str, str]]] = []

    def verify(
        self,
        *,
        venue: str,
        credentials: Mapping[str, str],
        now: datetime,
    ) -> ConnectionProbeResult:
        assert now.utcoffset() is not None
        self.calls.append((venue, dict(credentials)))
        return self.outcomes[venue]


def test_four_venue_connection_verification_is_scoped_idempotent_and_never_enables_trading(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    TradingService(database).bootstrap_admin("connection-api-admin", now=now)
    verifier = FakeExchangeConnectionVerifier(
        {
            "BINANCE": ConnectionProbeResult(True, None),
            "HYPERLIQUID": ConnectionProbeResult(True, None),
            "OKX": ConnectionProbeResult(False, "OKX_AUTHENTICATION_FAILED"),
            "BYBIT": ConnectionProbeResult(True, None),
        }
    )
    settings = Settings(
        environment="test",
        database_url=str(database.engine.url),
        allow_mock_identity=True,
        session_signing_secret="connection-api-signing-secret-that-is-long-enough",  # noqa: S106
        credential_encryption_key=encryption_key(),
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
        exchange_connection_verifier=verifier,
    )
    credentials_by_venue = {
        "BINANCE": {"api_key": "binance-key", "api_secret": "binance-secret"},
        "HYPERLIQUID": {
            "account_address": "0x1111111111111111111111111111111111111111",
            "api_wallet_address": "0x2222222222222222222222222222222222222222",
            "api_wallet_private_key": "private-key-must-not-reach-verifier",
        },
        "OKX": {
            "api_key": "okx-key",
            "api_secret": "okx-secret",
            "passphrase": "okx-passphrase",
        },
        "BYBIT": {"api_key": "bybit-key", "api_secret": "bybit-secret"},
    }

    async def scenario() -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            login = await client.post(
                "/api/auth/mock/login", json={"username": "connection-api-admin"}
            )
            assert login.status_code == 200
            account_ids: dict[str, str] = {}
            for venue, credentials in credentials_by_venue.items():
                created = await client.post(
                    "/api/exchange-accounts",
                    json={
                        "account_id": f"{venue.lower()}-team-account",
                        "venue": venue,
                        "label": f"{venue} Team Account",
                        "credentials": credentials,
                        "idempotency_key": f"create-{venue.lower()}-team-account",
                    },
                )
                assert created.status_code == 200, created.text
                account_ids[venue] = created.json()["exchange_account_id"]

            results: dict[str, dict[str, object]] = {}
            for venue, exchange_account_id in account_ids.items():
                response = await client.post(
                    f"/api/exchange-accounts/{exchange_account_id}/connection-verifications",
                    json={
                        "expected_version": 1,
                        "idempotency_key": f"verify-{venue.lower()}-team-account",
                    },
                )
                assert response.status_code == 200, response.text
                assert all(
                    secret not in response.text
                    for secret in credentials_by_venue[venue].values()
                )
                results[venue] = response.json()

            assert results["OKX"]["connection"] == {
                "status": "FAILED",
                "error_code": "OKX_AUTHENTICATION_FAILED",
                "checked_at": results["OKX"]["connection"]["checked_at"],
                "last_verified_at": None,
            }
            for venue in {"BINANCE", "HYPERLIQUID", "BYBIT"}:
                assert results[venue]["connection"]["status"] == "VERIFIED"
                assert results[venue]["connection"]["last_verified_at"] is not None
            assert all(
                result["trading"] == {"status": "DISABLED", "enabled": False}
                for result in results.values()
            )

            replay = await client.post(
                f"/api/exchange-accounts/{account_ids['BINANCE']}/connection-verifications",
                json={
                    "expected_version": 1,
                    "idempotency_key": "verify-binance-team-account",
                },
            )
            assert replay.status_code == 200
            assert replay.json()["version"] == results["BINANCE"]["version"]
            assert len(verifier.calls) == 4

            for venue in {"BINANCE", "HYPERLIQUID"}:
                enabled = await client.put(
                    f"/api/exchange-accounts/{account_ids[venue]}/runtime-sync",
                    json={
                        "enabled": True,
                        "expected_version": results[venue]["version"],
                        "idempotency_key": f"enable-{venue.lower()}-runtime-sync",
                    },
                )
                assert enabled.status_code == 200, enabled.text
                assert enabled.json()["runtime_sync_enabled"] is True

            unsupported = await client.put(
                f"/api/exchange-accounts/{account_ids['BYBIT']}/runtime-sync",
                json={
                    "enabled": True,
                    "expected_version": results["BYBIT"]["version"],
                    "idempotency_key": "enable-bybit-runtime-sync",
                },
            )
            assert unsupported.status_code == 422
            assert unsupported.json()["error"]["code"] == "RUNTIME_CONNECTOR_NOT_IMPLEMENTED"

            listed = (await client.get("/api/exchange-accounts")).json()["data"]["data"]
            by_venue = {item["venue"]: item for item in listed}
            assert by_venue["BINANCE"]["runtime_binding"]["bound"] is True
            assert by_venue["HYPERLIQUID"]["runtime_binding"]["bound"] is True
            assert by_venue["OKX"]["connection"]["status"] == "FAILED"
            assert by_venue["OKX"]["runtime_binding"]["read_only_connector"] == "NOT_IMPLEMENTED"
            assert (
                by_venue["OKX"]["runtime_binding"]["connection_verification_connector"]
                == "IMPLEMENTED"
            )
            assert by_venue["BYBIT"]["permissions"]["can_verify_connection"] is True
            assert all(item["trading"]["enabled"] is False for item in listed)

    asyncio.run(scenario())

    expected_verifier_credentials = {
        **credentials_by_venue,
        "HYPERLIQUID": {
            "account_address": "0x1111111111111111111111111111111111111111",
            "api_wallet_address": "0x2222222222222222222222222222222222222222",
        },
    }
    assert dict(verifier.calls) == expected_verifier_credentials
    assert "private-key-must-not-reach-verifier" not in repr(verifier.calls)
    with database.session_factory() as session:
        audits = session.scalars(
            select(AuditEvent).where(
                AuditEvent.event_type.in_(
                    {
                        "EXCHANGE_ACCOUNT_CONNECTION_VERIFIED",
                        "EXCHANGE_ACCOUNT_CONNECTION_FAILED",
                    }
                )
            )
        ).all()
        assert len(audits) == 4
        assert all(
            "secret" not in item.reason and "passphrase" not in item.reason
            for item in audits
        )
        stored = session.scalars(select(ExchangeAccount)).all()
        assert all(item.trading_status == "DISABLED" for item in stored)
        assert all(item.last_connection_check_at is not None for item in stored)
    bindings = TradingService(
        database, credential_encryption_key=encryption_key()
    ).runtime_account_bindings()
    assert {(item.venue, item.account_id) for item in bindings} == {
        ("BINANCE", "binance-team-account"),
        ("HYPERLIQUID", "hyperliquid-team-account"),
    }
    assert all("secret" not in repr(item) for item in bindings)
    hyperliquid_binding = next(item for item in bindings if item.venue == "HYPERLIQUID")
    assert "api_wallet_private_key" not in hyperliquid_binding.credentials


def test_connection_result_is_not_committed_after_credential_rotation(database: Database) -> None:
    now = datetime.now(UTC)
    service = TradingService(database, credential_encryption_key=encryption_key())
    admin = service.bootstrap_admin("connection-race-admin", now=now)
    exchange_account_id = service.create_exchange_account(
        actor_id=admin,
        account_id="bybit-race",
        venue="BYBIT",
        label="Bybit Race",
        credentials={"api_key": "old-key", "api_secret": "old-secret"},
        idempotency_key="create-bybit-race",
        now=now,
    )
    command, replay = service.prepare_exchange_account_connection_verification(
        exchange_account_id,
        actor_id=admin,
        expected_version=1,
        idempotency_key="verify-before-rotation",
    )
    assert replay is None and command is not None
    service.rotate_exchange_account_credentials(
        exchange_account_id,
        actor_id=admin,
        credentials={"api_key": "new-key", "api_secret": "new-secret"},
        expected_version=1,
        idempotency_key="rotate-during-verification",
        now=now + timedelta(seconds=1),
    )

    with pytest.raises(DomainRejected, match="VERSION_CONFLICT"):
        service.record_exchange_account_connection_verification(
            command,
            ConnectionProbeResult(True, None),
            actor_id=admin,
            idempotency_key="verify-before-rotation",
            now=now + timedelta(seconds=2),
        )

    projection = TradingQueries(database).exchange_accounts(admin)["data"][0]
    assert projection["version"] == 2
    assert projection["connection"]["status"] == "NOT_VERIFIED"
    assert projection["connection"]["checked_at"] is None
    assert projection["trading"]["enabled"] is False


def test_database_runtime_bindings_support_same_account_in_multiple_teams(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    service = TradingService(database, credential_encryption_key=encryption_key())
    admin = service.bootstrap_admin("multi-team-runtime-admin", now=now)
    context = TradingQueries(database).user_context(admin)
    workspace_id = UUID(context["active_workspace"]["workspace_id"])
    first_team_id = UUID(context["active_team"]["team_id"])

    def create_verified_binding(label: str, secret: str, sequence: int) -> UUID:
        exchange_account_id = service.create_exchange_account(
            actor_id=admin,
            account_id="shared-binance-main",
            venue="BINANCE",
            label=label,
            credentials={"api_key": f"key-{sequence}", "api_secret": secret},
            idempotency_key=f"create-runtime-account-{sequence}",
            now=now + timedelta(seconds=sequence),
        )
        command, replay = service.prepare_exchange_account_connection_verification(
            exchange_account_id,
            actor_id=admin,
            expected_version=1,
            idempotency_key=f"verify-runtime-account-{sequence}",
        )
        assert replay is None and command is not None
        verification = service.record_exchange_account_connection_verification(
            command,
            ConnectionProbeResult(True, None),
            actor_id=admin,
            idempotency_key=f"verify-runtime-account-{sequence}",
            now=now + timedelta(seconds=sequence, milliseconds=100),
        )
        configured = service.configure_exchange_account_runtime_sync(
            exchange_account_id,
            actor_id=admin,
            enabled=True,
            expected_version=int(verification["version"]),
            idempotency_key=f"enable-runtime-account-{sequence}",
            now=now + timedelta(seconds=sequence, milliseconds=200),
        )
        assert configured["runtime_sync_enabled"] is True
        return exchange_account_id

    first_account_id = create_verified_binding(
        "First Team Binance", "first-team-secret", 1
    )
    second_team_id = service.create_team(
        actor_id=admin,
        name="Runtime Accounts Two",
        slug="runtime-accounts-two",
        idempotency_key="create-runtime-accounts-two",
        now=now + timedelta(seconds=2),
    )
    second_account_id = create_verified_binding(
        "Second Team Binance", "second-team-secret", 3
    )

    bindings = service.runtime_account_bindings()
    assert len(bindings) == 2
    by_team = {item.team_id: item for item in bindings}
    assert set(by_team) == {first_team_id, second_team_id}
    assert all(item.account_id == "shared-binance-main" for item in bindings)
    assert (
        by_team[first_team_id].credentials["api_secret"] == "first-team-secret"  # noqa: S105
    )
    assert (
        by_team[second_team_id].credentials["api_secret"]
        == "second-team-secret"  # noqa: S105
    )
    assert all("secret" not in repr(item) for item in bindings)

    with pytest.raises(DomainRejected, match="EXCHANGE_ACCOUNT_NOT_FOUND"):
        service.configure_exchange_account_runtime_sync(
            first_account_id,
            actor_id=admin,
            enabled=False,
            expected_version=3,
            idempotency_key="cross-team-disable-runtime-account",
            now=now + timedelta(seconds=4),
        )

    service.rotate_exchange_account_credentials(
        second_account_id,
        actor_id=admin,
        credentials={"api_key": "rotated-key", "api_secret": "rotated-secret"},
        expected_version=3,
        idempotency_key="rotate-second-runtime-account",
        now=now + timedelta(seconds=5),
    )
    stale_second_binding = by_team[second_team_id]
    with pytest.raises(DomainRejected, match="RUNTIME_BINDING_CHANGED"):
        service.record_runtime_source_health(
            stale_second_binding.service_principal_id,
            {"BINANCE": {"status": "FAILED", "items_observed": 0}},
            scopes={"BINANCE": (stale_second_binding.account_id, "BINANCE")},
            runtime_account_binding=stale_second_binding,
            now=now + timedelta(seconds=5, milliseconds=100),
        )
    remaining = service.runtime_account_bindings()
    assert len(remaining) == 1 and remaining[0].team_id == first_team_id
    service.select_scope(
        actor_id=admin,
        workspace_id=workspace_id,
        team_id=first_team_id,
        idempotency_key="select-first-runtime-team",
        now=now + timedelta(seconds=6),
    )
    assert TradingQueries(database).exchange_accounts(admin)["data"][0][
        "runtime_binding"
    ]["bound"] is True
    disabled = service.configure_exchange_account_runtime_sync(
        first_account_id,
        actor_id=admin,
        enabled=False,
        expected_version=remaining[0].account_version,
        idempotency_key="disable-first-runtime-account",
        now=now + timedelta(seconds=6, milliseconds=100),
    )
    with database.session_factory() as session:
        principal = session.get(User, remaining[0].service_principal_id)
        assert principal is not None and principal.active is False
    reenabled = service.configure_exchange_account_runtime_sync(
        first_account_id,
        actor_id=admin,
        enabled=True,
        expected_version=int(disabled["version"]),
        idempotency_key="reenable-first-runtime-account",
        now=now + timedelta(seconds=6, milliseconds=200),
    )
    assert reenabled["runtime_sync_enabled"] is True
    with database.session_factory() as session:
        principal = session.get(User, remaining[0].service_principal_id)
        assert principal is not None and principal.active is True
    with database.session_factory.begin() as session:
        session.add(
            RoleAssignment(
                user_id=remaining[0].service_principal_id,
                team_id=first_team_id,
                role=Role.OBSERVER.value,
                account_scope=None,
                venue_scope=None,
                created_at=now + timedelta(seconds=7),
            )
        )
    with pytest.raises(DomainRejected, match="RUNTIME_BINDING_INVALID"):
        service.runtime_account_bindings()
