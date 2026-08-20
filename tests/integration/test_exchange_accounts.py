from __future__ import annotations

import asyncio
import base64
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from trading_control_plane.api import create_app
from trading_control_plane.config import Settings
from trading_control_plane.credentials import CredentialCipher
from trading_control_plane.database import Database
from trading_control_plane.domain import DomainRejected, ExecutionEnvironment, Role
from trading_control_plane.freqtrade import FreqtradeWorkerClient, FreqtradeWorkerSpec
from trading_control_plane.models import (
    AuditEvent,
    DirectCapitalOperation,
    ExchangeAccount,
    Instrument,
    OrderIntent,
    Position,
    RoleAssignment,
    RuntimeSourceHealth,
    User,
    VenueOrder,
)
from trading_control_plane.models import (
    VenueFill as PersistedVenueFill,
)
from trading_control_plane.perptape import PerptapeClient
from trading_control_plane.queries import TradingQueries
from trading_control_plane.runtime_contracts import ConnectionProbeResult
from trading_control_plane.service import TradingService
from trading_control_plane.venue_read_only import (
    VenueEquity,
    VenueFill,
    VenueInstrument,
    VenuePosition,
    VenueReadOnlySnapshot,
)
from trading_control_plane.venue_read_only import (
    VenueOrder as ReadOnlyVenueOrder,
)


def test_fact_adapter_ingestion_refreshes_exact_account_runtime_health(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    service = TradingService(database, credential_encryption_key=encryption_key())
    admin = service.bootstrap_admin("fact-health-admin", now=now)
    account_uuid = service.create_exchange_account(
        actor_id=admin,
        account_id="fact-health-main",
        venue="BINANCE",
        label="Fact Health Main",
        credentials={"api_key": "fact-health-key", "api_secret": "fact-health-secret"},
        idempotency_key="create-fact-health-account",
        now=now,
    )
    command, replay = service.prepare_exchange_account_connection_verification(
        account_uuid,
        actor_id=admin,
        expected_version=1,
        idempotency_key="verify-fact-health-account",
    )
    assert command is not None and replay is None
    verified = service.record_exchange_account_connection_verification(
        command,
        ConnectionProbeResult(True, None),
        actor_id=admin,
        idempotency_key="record-fact-health-verification",
        now=now,
    )
    service.configure_exchange_account_runtime_sync(
        account_uuid,
        actor_id=admin,
        enabled=True,
        expected_version=int(verified["version"]),
        idempotency_key="enable-fact-health-runtime",
        now=now,
    )
    binding = service.runtime_account_bindings()[0]
    snapshot = VenueReadOnlySnapshot(
        symbol="BTCUSDT",
        observed_at=now,
        instrument=VenueInstrument(
            symbol="BTCUSDT",
            tick_size=Decimal("0.1"),
            lot_size=Decimal("0.001"),
            minimum_notional=Decimal("5"),
            quote_currency="USDT",
            collateral_currency="USDT",
            active=True,
        ),
        orders=(),
        fills=(),
        position=VenuePosition(
            quantity=Decimal(0),
            average_entry_price=Decimal(0),
            mark_price=Decimal("60000"),
            observed_at=now,
        ),
        equity=VenueEquity(
            equity=Decimal("100"),
            available_balance=Decimal("100"),
            currency="USDT",
            observed_at=now,
        ),
        funding=(),
        protection=None,
    )
    eth_snapshot = replace(
        snapshot,
        symbol="ETHUSDT",
        instrument=replace(snapshot.instrument, symbol="ETHUSDT"),
        position=replace(
            snapshot.position,
            quantity=Decimal("0.1"),
            average_entry_price=Decimal("3000"),
            mark_price=Decimal("3010"),
        ),
    )

    service.ingest_normalized_read_only_account_snapshot(
        binding.account_id,
        binding.service_principal_id,
        (snapshot, eth_snapshot),
        venue=binding.venue,
        environment=ExecutionEnvironment.LIVE,
        runtime_binding=binding,
        now=now,
    )
    with database.session_factory() as session:
        health = session.scalar(
            select(RuntimeSourceHealth).where(
                RuntimeSourceHealth.team_id == binding.team_id,
                RuntimeSourceHealth.account_id == binding.account_id,
                RuntimeSourceHealth.venue == binding.venue,
            )
        )
        assert health is not None
        assert health.status == "SUCCESS"
        assert health.checked_at == now
        assert health.last_success_at == now
        assert health.error_code is None

    degraded_at = now + timedelta(seconds=1)
    service.ingest_normalized_read_only_account_snapshot(
        binding.account_id,
        binding.service_principal_id,
        (
            replace(
                snapshot,
                observed_at=degraded_at,
                history_error_code="FACT_ADAPTER_HISTORY_INCOMPLETE",
            ),
        ),
        venue=binding.venue,
        environment=ExecutionEnvironment.LIVE,
        runtime_binding=binding,
        now=degraded_at,
    )
    projected = TradingQueries(database).exchange_account_fact_health(
        admin,
        account_uuid,
        stale_after_seconds=360,
        now=degraded_at,
    )
    assert projected["data_status"] == "CURRENT"
    assert projected["runtime_status"] == "FAILED"
    assert projected["error_code"] == "FACT_ADAPTER_HISTORY_INCOMPLETE"

    with database.session_factory() as session:
        eth_position = session.scalar(
            select(Position)
            .join(Instrument, Position.instrument_id == Instrument.instrument_id)
            .where(
                Position.team_id == binding.team_id,
                Position.account_id == binding.account_id,
                Position.venue == binding.venue,
                Instrument.symbol == "ETHUSDT",
            )
        )
        assert eth_position is not None
        covered_events = session.scalars(
            select(AuditEvent).where(
                AuditEvent.event_type == "BINANCE_POSITION_COVERED",
                AuditEvent.object_id == str(eth_position.position_id),
            )
        ).all()
        assert eth_position.quantity == 0
        assert eth_position.observed_at == degraded_at
        assert len(covered_events) == 1

    refreshed_at = degraded_at + timedelta(seconds=1)
    service.ingest_normalized_read_only_account_snapshot(
        binding.account_id,
        binding.service_principal_id,
        (
            replace(
                snapshot,
                observed_at=refreshed_at,
                history_error_code="FACT_ADAPTER_HISTORY_INCOMPLETE",
            ),
        ),
        venue=binding.venue,
        environment=ExecutionEnvironment.LIVE,
        runtime_binding=binding,
        now=refreshed_at,
    )
    with database.session_factory() as session:
        eth_position = session.scalar(
            select(Position)
            .join(Instrument, Position.instrument_id == Instrument.instrument_id)
            .where(
                Position.team_id == binding.team_id,
                Position.account_id == binding.account_id,
                Position.venue == binding.venue,
                Instrument.symbol == "ETHUSDT",
            )
        )
        assert eth_position is not None
        covered_events = session.scalars(
            select(AuditEvent).where(
                AuditEvent.event_type == "BINANCE_POSITION_COVERED",
                AuditEvent.object_id == str(eth_position.position_id),
            )
        ).all()
        assert eth_position.observed_at == refreshed_at
        assert len(covered_events) == 1


def test_confirmed_external_fill_closes_matching_unbound_order(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    service = TradingService(database, credential_encryption_key=encryption_key())
    admin = service.bootstrap_admin("external-fill-admin", now=now)
    account_uuid = service.create_exchange_account(
        actor_id=admin,
        account_id="external-fill-main",
        venue="BINANCE",
        label="External Fill Main",
        credentials={"api_key": "external-fill-key", "api_secret": "external-fill-secret"},
        idempotency_key="create-external-fill-account",
        now=now,
    )
    command, replay = service.prepare_exchange_account_connection_verification(
        account_uuid,
        actor_id=admin,
        expected_version=1,
        idempotency_key="verify-external-fill-account",
    )
    assert command is not None and replay is None
    verified = service.record_exchange_account_connection_verification(
        command,
        ConnectionProbeResult(True, None),
        actor_id=admin,
        idempotency_key="record-external-fill-verification",
        now=now,
    )
    service.configure_exchange_account_runtime_sync(
        account_uuid,
        actor_id=admin,
        enabled=True,
        expected_version=int(verified["version"]),
        idempotency_key="enable-external-fill-runtime",
        now=now,
    )
    binding = service.runtime_account_bindings()[0]
    base = VenueReadOnlySnapshot(
        symbol="SOLUSDT",
        observed_at=now,
        instrument=VenueInstrument(
            symbol="SOLUSDT",
            tick_size=Decimal("0.01"),
            lot_size=Decimal("0.001"),
            minimum_notional=Decimal("5"),
            quote_currency="USDT",
            collateral_currency="USDT",
            active=True,
        ),
        orders=(
            ReadOnlyVenueOrder(
                order_id="external-close-order",
                client_order_id="external-close-client",
                status="SENT",
                side="SELL",
                order_type="MARKET",
                ordered_quantity=Decimal("0.130"),
                filled_quantity=Decimal(0),
                stop_price=Decimal(0),
                reduce_only=True,
                close_position=False,
                observed_at=now,
            ),
        ),
        fills=(),
        position=VenuePosition(
            quantity=Decimal("0.130"),
            average_entry_price=Decimal("82"),
            mark_price=Decimal("81.84"),
            observed_at=now,
        ),
        equity=VenueEquity(
            equity=Decimal("100"),
            available_balance=Decimal("90"),
            currency="USDT",
            observed_at=now,
        ),
        funding=(),
        protection=None,
    )
    service.ingest_normalized_read_only_account_snapshot(
        binding.account_id,
        binding.service_principal_id,
        (base,),
        venue=binding.venue,
        environment=ExecutionEnvironment.LIVE,
        runtime_binding=binding,
        now=now,
    )

    filled_at = now + timedelta(seconds=1)
    filled = replace(
        base,
        observed_at=filled_at,
        orders=(),
        fills=(
            VenueFill(
                fill_id="external-close-fill",
                order_id="external-close-order",
                side="SELL",
                quantity=Decimal("0.130"),
                price=Decimal("81.84"),
                fee=Decimal("0.004"),
                fee_currency="USDT",
                executed_at=filled_at,
            ),
        ),
        position=replace(
            base.position,
            quantity=Decimal(0),
            average_entry_price=Decimal(0),
            observed_at=filled_at,
        ),
        equity=replace(base.equity, observed_at=filled_at),
    )
    for observed_at in (filled_at, filled_at + timedelta(seconds=1)):
        service.ingest_normalized_read_only_account_snapshot(
            binding.account_id,
            binding.service_principal_id,
            (replace(filled, observed_at=observed_at),),
            venue=binding.venue,
            environment=ExecutionEnvironment.LIVE,
            runtime_binding=binding,
            now=observed_at,
        )

    with database.session_factory() as session:
        order = session.scalar(
            select(VenueOrder).where(VenueOrder.venue_order_id == "external-close-order")
        )
        assert order is not None
        assert order.order_intent_id is None
        assert order.status == "FILLED"
        assert order.ordered_quantity == Decimal("0.130")
        assert order.filled_quantity == Decimal("0.130")
        assert session.query(PersistedVenueFill).count() == 1
        assert session.query(OrderIntent).count() == 0


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


def test_current_positions_aggregate_visible_accounts_with_direction_and_unrealized_pnl(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    service = TradingService(database, credential_encryption_key=encryption_key())
    admin = service.bootstrap_admin("position-admin", now=now)
    service.create_exchange_account(
        actor_id=admin,
        account_id="binance-position-main",
        venue="BINANCE",
        label="Binance Position Main",
        credentials={"api_key": "binance-key", "api_secret": "binance-secret"},
        idempotency_key="create-binance-position-account",
        now=now,
    )
    service.create_exchange_account(
        actor_id=admin,
        account_id="okx-position-main",
        venue="OKX",
        label="OKX Position Main",
        credentials={
            "api_key": "okx-key",
            "api_secret": "okx-secret",
            "passphrase": "okx-passphrase",
        },
        idempotency_key="create-okx-position-account",
        now=now,
    )
    binance_instrument = service.register_instrument(
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
    okx_instrument = service.register_instrument(
        actor_id=admin,
        venue="OKX",
        symbol="ETH-USDT-SWAP",
        tick_size=Decimal("0.01"),
        lot_size=Decimal("0.001"),
        minimum_notional=Decimal("5"),
        contract_multiplier=Decimal("1"),
        quote_currency="USDT",
        collateral_currency="USDT",
        protection_supported=True,
        now=now,
    )
    service.record_position(
        "binance-position-main",
        "BINANCE",
        binance_instrument,
        Decimal("2"),
        Decimal("100"),
        Decimal("110"),
        True,
        admin,
        now=now,
    )
    service.record_position(
        "okx-position-main",
        "OKX",
        okx_instrument,
        Decimal("-3"),
        Decimal("50"),
        Decimal("40"),
        True,
        admin,
        now=now,
    )

    projected = TradingQueries(database).current_positions(admin, "LIVE")
    assert projected["summary"] == {
        "position_count": 2,
        "account_count": 2,
        "long_count": 1,
        "short_count": 1,
        "unknown_count": 0,
    }
    by_symbol = {item["symbol"]: item for item in projected["positions"]}
    assert by_symbol["BTCUSDT"]["direction"] == "LONG"
    assert by_symbol["BTCUSDT"]["quantity"] == "2.000000000000000000"
    assert Decimal(by_symbol["BTCUSDT"]["unrealized_pnl"]) == Decimal("20")
    assert by_symbol["ETH-USDT-SWAP"]["direction"] == "SHORT"
    assert by_symbol["ETH-USDT-SWAP"]["quantity"] == "3.000000000000000000"
    assert Decimal(by_symbol["ETH-USDT-SWAP"]["unrealized_pnl"]) == Decimal("30")

    observer = service.create_user("position-observer", admin, now=now)
    service.assign_role(
        observer,
        Role.OBSERVER,
        admin,
        account_scope="binance-position-main",
        venue_scope="BINANCE",
        now=now,
    )
    observer_projection = TradingQueries(database).current_positions(observer, "LIVE")
    assert [item["symbol"] for item in observer_projection["positions"]] == ["BTCUSDT"]
    assert [item["account_id"] for item in observer_projection["accounts"]] == [
        "binance-position-main"
    ]


def test_exchange_account_delete_is_fail_closed_and_can_be_reconnected(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    service = TradingService(database, credential_encryption_key=encryption_key())
    admin = service.bootstrap_admin("account-delete-admin", now=now)
    account_id = service.create_exchange_account(
        actor_id=admin,
        account_id="okx-removable",
        venue="OKX",
        label="Removable OKX",
        credentials={"api_key": "key-one", "api_secret": "secret-one", "passphrase": "pass"},
        idempotency_key="create-removable-account",
        now=now,
    )

    deleted = service.delete_exchange_account(
        account_id,
        actor_id=admin,
        confirmation="DELETE:LIVE:okx-removable:OKX",
        expected_version=1,
        idempotency_key="delete-removable-account",
        now=now + timedelta(seconds=1),
    )
    assert deleted["status"] == "DELETED"
    assert TradingQueries(database).exchange_accounts(admin)["data"] == []
    assert (
        service.delete_exchange_account(
            account_id,
            actor_id=admin,
            confirmation="DELETE:LIVE:okx-removable:OKX",
            expected_version=1,
            idempotency_key="delete-removable-account",
            now=now + timedelta(seconds=2),
        )
        == deleted
    )
    with database.session_factory() as session:
        archived = session.get(ExchangeAccount, account_id)
        assert archived is not None
        assert archived.active is False
        assert archived.credentials_ciphertext is None
        assert archived.credential_version == 0
        assert archived.runtime_sync_enabled is False
        assert archived.trading_status == "DISABLED"

    restored_id = service.create_exchange_account(
        actor_id=admin,
        account_id="okx-removable",
        venue="OKX",
        label="Reconnected OKX",
        credentials={"api_key": "key-two", "api_secret": "secret-two", "passphrase": "pass"},
        idempotency_key="restore-removable-account",
        now=now + timedelta(seconds=3),
    )
    assert restored_id == account_id
    restored = TradingQueries(database).exchange_accounts(admin)["data"]
    assert len(restored) == 1
    assert restored[0]["label"] == "Reconnected OKX"
    assert restored[0]["credentials"]["state"] == "CONFIGURED"
    assert restored[0]["permissions"]["can_delete"] is True
    with database.session_factory() as session:
        events = set(
            session.scalars(
                select(AuditEvent.event_type).where(AuditEvent.object_id == str(account_id))
            ).all()
        )
        assert {"EXCHANGE_ACCOUNT_DELETED", "EXCHANGE_ACCOUNT_RESTORED"} <= events


def test_expired_unsubmitted_direct_capital_plan_is_projected_blocked_and_not_running(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    service = TradingService(database, credential_encryption_key=encryption_key())
    admin = service.bootstrap_admin("expired-direct-capital-admin", now=now)
    account_pk = service.create_exchange_account(
        actor_id=admin,
        account_id="binance-expired-plan",
        venue="BINANCE",
        label="Expired direct capital plan",
        credentials={"api_key": "expired-key", "api_secret": "expired-secret"},
        idempotency_key="create-expired-direct-capital-account",
        now=now,
    )
    team_id = UUID(TradingQueries(database).user_context(admin)["active_team"]["team_id"])
    operation_id = uuid4()
    with database.session_factory.begin() as session:
        session.add(
            DirectCapitalOperation(
                operation_id=operation_id,
                team_id=team_id,
                path="BINANCE_TO_VAULT",
                treasury_provider="SAFE_SPENDING_LIMIT",
                status="UNSIGNED_PLAN_READY",
                receipt_status="NOT_SUBMITTED",
                account_id="binance-expired-plan",
                venue="BINANCE",
                asset="USDC",
                network="ARBITRUM",
                amount=Decimal("1"),
                max_fee=Decimal("0.1"),
                min_received=Decimal("0.9"),
                stages=[],
                blockers=[],
                expires_at=now - timedelta(seconds=1),
                final_confirmed_at=now - timedelta(minutes=1),
                actor_id=admin,
                correlation_id=uuid4(),
                idempotency_key="expired-direct-capital-operation",
                version=2,
                created_at=now - timedelta(minutes=1),
                updated_at=now - timedelta(minutes=1),
            )
        )

    projected = next(
        item
        for item in TradingQueries(database).capital_center(admin)["direct_operations"]
        if item["operation_id"] == str(operation_id)
    )
    assert projected["status"] == "BLOCKED"
    assert projected["receipt_status"] == "NOT_SUBMITTED"
    assert "CAPITAL_DIRECT_OPERATION_EXPIRED" in projected["blockers"]

    deleted = service.delete_exchange_account(
        account_pk,
        actor_id=admin,
        confirmation="DELETE:LIVE:binance-expired-plan:BINANCE",
        expected_version=1,
        idempotency_key="delete-expired-direct-capital-account",
        now=now,
    )
    assert deleted["status"] == "DELETED"


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


def test_exchange_account_api_masks_credentials_and_exposes_connector_truth(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    service = TradingService(database)
    admin = service.bootstrap_admin("api-account-admin", now=now)
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
        now=now,
    )
    settings = Settings(
        environment="test",
        database_url=str(database.engine.url),
        allow_mock_identity=True,
        session_signing_secret="account-api-signing-secret-that-is-long-enough",  # noqa: S106
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
            service.record_position(
                "binance-api-main",
                "BINANCE",
                instrument,
                Decimal("-2"),
                Decimal("100"),
                Decimal("90"),
                True,
                admin,
                now=now,
            )
            current_positions = await client.get("/api/positions", params={"environment": "LIVE"})
            assert current_positions.status_code == 200, current_positions.text
            position = current_positions.json()["data"]["positions"][0]
            assert position["account_id"] == "binance-api-main"
            assert position["direction"] == "SHORT"
            assert Decimal(position["unrealized_pnl"]) == Decimal("20")
            listed = await client.get("/api/exchange-accounts")
            assert listed.status_code == 200, listed.text
            assert "api-secret-never-return" not in listed.text
            item = listed.json()["data"]["data"][0]
            assert item["runtime_binding"] == {
                "bound": False,
                "source": "DATABASE_ENVELOPE",
                "read_only_connector": "IMPLEMENTED",
                "read_only_scope": "USD_M_PERPETUALS",
                "connection_verification_connector": "IMPLEMENTED",
                "connection_verification_source": "DATABASE_ENVELOPE",
                "service_principal_configured": False,
                "trading_connector": "FREQTRADE_EXTERNAL",
            }
            assert item["connection"]["status"] == "NOT_VERIFIED"
            assert item["trading"]["enabled"] is False
            facts = await client.get(f"/api/exchange-accounts/{item['exchange_account_id']}/facts")
            assert facts.status_code == 200, facts.text
            assert facts.json()["data"]["account_id"] == "binance-api-main"
            service.record_position(
                "binance-api-main",
                "BINANCE",
                instrument,
                Decimal("0"),
                Decimal("0"),
                Decimal("90"),
                True,
                admin,
                now=now + timedelta(seconds=1),
            )
            page = await client.get("/venues")
            assert page.status_code == 200
            assert "api-secret-never-return" not in page.text
            deleted = await client.request(
                "DELETE",
                f"/api/exchange-accounts/{item['exchange_account_id']}",
                json={
                    "confirmation": "DELETE:LIVE:binance-api-main:BINANCE",
                    "expected_version": item["version"],
                    "idempotency_key": "api-account-delete",
                },
            )
            assert deleted.status_code == 200, deleted.text
            assert deleted.json()["status"] == "DELETED"
            assert deleted.json()["data"]["data"] == []

    asyncio.run(scenario())


def test_freqtrade_workers_are_encrypted_and_verified_per_exact_account(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    service = TradingService(database, credential_encryption_key=encryption_key())
    admin = service.bootstrap_admin("worker-binding-admin", now=now)
    context = TradingQueries(database).user_context(admin)
    team_id = context["active_team"]["team_id"]
    first_id = service.create_exchange_account(
        actor_id=admin,
        account_id="binance-worker-a",
        venue="BINANCE",
        label="Binance Worker A",
        credentials={"api_key": "account-key-a", "api_secret": "account-secret-a"},
        idempotency_key="create-worker-account-a",
        now=now,
    )
    second_id = service.create_exchange_account(
        actor_id=admin,
        environment="TESTNET",
        account_id="binance-worker-b",
        venue="BINANCE",
        label="Binance Worker B",
        credentials={"api_key": "account-key-b", "api_secret": "account-secret-b"},
        idempotency_key="create-worker-account-b",
        now=now,
    )
    observer = service.create_user("worker-binding-observer", admin, now=now)
    service.assign_role(
        observer,
        Role.OBSERVER,
        admin,
        account_scope="binance-worker-a",
        venue_scope="BINANCE",
        now=now,
    )
    proposer = service.create_user("worker-binding-proposer", admin, now=now)
    service.assign_role(
        proposer,
        Role.PROPOSER,
        admin,
        account_scope="binance-worker-a",
        venue_scope="BINANCE",
        now=now,
    )
    first_probe = WorkerProbeFixture(exchange="binance", dry_run=False)
    second_probe = WorkerProbeFixture(
        exchange="binance",
        dry_run=False,
        bot_name="tradeops-binance-testnet",
    )
    workers = (
        FreqtradeWorkerClient(
            FreqtradeWorkerSpec(
                name="binance-worker-a",
                venue="BINANCE",
                base_url="http://127.0.0.1:18081",
                username="worker-a-user",
                password="worker-a-password",  # noqa: S106
                exchange_account_id=str(first_id),
                team_id=str(team_id),
                account_id="binance-worker-a",
            ),
            fetcher=first_probe,
        ),
        FreqtradeWorkerClient(
            FreqtradeWorkerSpec(
                name="binance-worker-b",
                venue="BINANCE",
                base_url="http://127.0.0.1:18082",
                username="worker-b-user",
                password="worker-b-password",  # noqa: S106
                exchange_account_id=str(second_id),
                team_id=str(team_id),
                account_id="binance-worker-b",
            ),
            fetcher=second_probe,
        ),
    )
    settings = Settings(
        environment="test",
        database_url=str(database.engine.url),
        allow_mock_identity=True,
        session_signing_secret="worker-binding-api-secret-long-enough",  # noqa: S106
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
        freqtrade_workers=workers,
    )

    async def scenario() -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            login = await client.post(
                "/api/auth/mock/login",
                json={"username": "worker-binding-admin"},
            )
            assert login.status_code == 200
            configurations = (
                (
                    first_id,
                    "binance-worker-a",
                    "http://127.0.0.1:18081",
                    "worker-a-user",
                    "worker-a-password",
                    "LIVE",
                ),
                (
                    second_id,
                    "binance-worker-b",
                    "http://127.0.0.1:18082",
                    "worker-b-user",
                    "worker-b-password",
                    "TESTNET",
                ),
            )
            for index, (account, name, url, username, password, mode) in enumerate(
                configurations,
                start=1,
            ):
                configured = await client.put(
                    f"/api/exchange-accounts/{account}/freqtrade-worker",
                    json={
                        "mode": mode,
                        "name": name,
                        "base_url": url,
                        "username": username,
                        "password": password,
                        "ws_token": f"worker-rpc-token-{index}-fixture",
                        "hip3_dexes": [],
                        "expected_version": 1,
                        "idempotency_key": f"configure-worker-{index}",
                    },
                )
                assert configured.status_code == 200, configured.text
                assert password not in configured.text
                verified = await client.post(
                    f"/api/exchange-accounts/{account}/freqtrade-worker/verifications",
                    json={
                        "expected_version": configured.json()["version"],
                        "idempotency_key": f"verify-worker-{index}",
                    },
                )
                assert verified.status_code == 200, verified.text
                assert verified.json()["worker"]["status"] == "VERIFIED"
                assert password not in verified.text

            assert len(first_probe.calls) == 5
            assert len(second_probe.calls) == 5
            assert all(":18081/" in item for item in first_probe.calls)
            assert all(":18082/" in item for item in second_probe.calls)
            listed = await client.get("/api/exchange-accounts")
            assert listed.status_code == 200
            assert all(
                secret not in listed.text for secret in ("worker-a-password", "worker-b-password")
            )
            by_account = {item["account_id"]: item for item in listed.json()["data"]["data"]}
            assert by_account["binance-worker-a"]["execution_worker"]["scope"] == {
                "team_id": str(team_id),
                "account_id": "binance-worker-a",
                "venue": "BINANCE",
            }
            assert by_account["binance-worker-a"]["execution_worker"]["live_ready"] is True
            assert by_account["binance-worker-b"]["execution_worker"]["live_ready"] is False
            assert by_account["binance-worker-b"]["execution_worker"]["scope_ready"] is True
            assert "default_endpoint" not in by_account["binance-worker-a"]["execution_worker"]
            status = await client.get("/api/execution/freqtrade/status")
            assert status.status_code == 200
            assert status.json()["workers"] == []
            assert len(status.json()["account_bindings"]) == 2

            logout = await client.post("/api/auth/logout")
            assert logout.status_code == 200
            observer_login = await client.post(
                "/api/auth/mock/login",
                json={"username": "worker-binding-observer"},
            )
            assert observer_login.status_code == 200
            observer_listed = await client.get("/api/exchange-accounts")
            assert observer_listed.status_code == 200
            observer_accounts = observer_listed.json()["data"]["data"]
            assert len(observer_accounts) == 1
            assert observer_accounts[0]["account_id"] == "binance-worker-a"
            assert observer_accounts[0]["execution_worker"]["endpoint"] is None
            assert "default_endpoint" not in observer_accounts[0]["execution_worker"]
            assert observer_accounts[0]["execution_worker"]["auth"]["username_hint"] is None

            logout = await client.post("/api/auth/logout")
            assert logout.status_code == 200
            proposer_login = await client.post(
                "/api/auth/mock/login",
                json={"username": "worker-binding-proposer"},
            )
            assert proposer_login.status_code == 200
            proposer_listed = await client.get("/api/exchange-accounts")
            assert proposer_listed.status_code == 200
            proposer_accounts = proposer_listed.json()["data"]["data"]
            assert [item["account_id"] for item in proposer_accounts] == ["binance-worker-a"]
            assert proposer_accounts[0]["execution_worker"]["endpoint"] is None
            assert "default_endpoint" not in proposer_accounts[0]["execution_worker"]

    asyncio.run(scenario())

    with database.session_factory() as session:
        stored = session.scalars(
            select(ExchangeAccount).where(
                ExchangeAccount.exchange_account_id.in_((first_id, second_id))
            )
        ).all()
        assert len(stored) == 2
        assert all(item.freqtrade_auth_ciphertext is not None for item in stored)
        assert all(
            secret not in repr(stored) for secret in ("worker-a-password", "worker-b-password")
        )
        assert {
            (item.account_id, item.freqtrade_worker_status, item.freqtrade_worker_mode)
            for item in stored
        } == {
            ("binance-worker-a", "VERIFIED", "LIVE"),
            ("binance-worker-b", "VERIFIED", "TESTNET"),
        }


def test_freqtrade_probe_cannot_commit_after_worker_rotation(database: Database) -> None:
    now = datetime.now(UTC)
    service = TradingService(database, credential_encryption_key=encryption_key())
    admin = service.bootstrap_admin("worker-race-admin", now=now)
    account_id = service.create_exchange_account(
        actor_id=admin,
        account_id="worker-race-account",
        venue="BINANCE",
        label="Worker Race",
        credentials={"api_key": "account-key", "api_secret": "account-secret"},
        idempotency_key="worker-race-account-create",
        now=now,
    )
    configured = service.configure_exchange_account_freqtrade_worker(
        account_id,
        actor_id=admin,
        mode="LIVE",
        name="worker-race-v1",
        base_url="http://127.0.0.1:18081",
        username="worker-user-v1",
        password="worker-password-v1",  # noqa: S106
        ws_token="worker-rpc-token-v1-fixture",  # noqa: S106
        hip3_dexes=(),
        expected_version=1,
        idempotency_key="worker-race-configure-v1",
        now=now,
    )
    binding, replay = service.prepare_exchange_account_freqtrade_verification(
        account_id,
        actor_id=admin,
        expected_version=int(configured["version"]),
        idempotency_key="worker-race-verify-v1",
    )
    assert replay is None and binding is not None
    rotated = service.configure_exchange_account_freqtrade_worker(
        account_id,
        actor_id=admin,
        mode="LIVE",
        name="worker-race-v2",
        base_url="http://127.0.0.1:18082",
        username="worker-user-v2",
        password="worker-password-v2",  # noqa: S106
        ws_token="worker-rpc-token-v2-fixture",  # noqa: S106
        hip3_dexes=(),
        expected_version=int(configured["version"]),
        idempotency_key="worker-race-configure-v2",
        now=now + timedelta(seconds=1),
    )
    with pytest.raises(DomainRejected, match="VERSION_CONFLICT"):
        service.record_exchange_account_freqtrade_verification(
            binding,
            actor_id=admin,
            error_code=None,
            idempotency_key="worker-race-verify-v1",
            now=now + timedelta(seconds=2),
        )
    projected = TradingQueries(database).exchange_accounts(admin)["data"][0]
    assert projected["version"] == rotated["version"]
    assert projected["execution_worker"]["name"] == "worker-race-v2"
    assert projected["execution_worker"]["status"] == "NOT_VERIFIED"


class FakeExchangeConnectionVerifier:
    def __init__(self, outcomes: Mapping[str, ConnectionProbeResult]) -> None:
        self.outcomes = outcomes
        self.calls: list[tuple[str, dict[str, str]]] = []

    def verify(
        self,
        *,
        workspace_id: str,
        team_id: str,
        account_id: str,
        venue: str,
        environment: str,
        account_mode: str,
        credentials: Mapping[str, str],
        now: datetime,
    ) -> ConnectionProbeResult:
        assert now.utcoffset() is not None
        assert workspace_id and team_id and account_id
        assert environment in {"TESTNET", "LIVE"}
        assert account_mode in {"STANDARD", "PORTFOLIO_MARGIN"}
        self.calls.append((venue, dict(credentials)))
        return self.outcomes[venue]


class WorkerProbeFixture:
    def __init__(
        self,
        *,
        exchange: str,
        dry_run: bool,
        bot_name: str = "tradeops-worker-fixture",
    ) -> None:
        self.exchange = exchange
        self.dry_run = dry_run
        self.bot_name = bot_name
        self.calls: list[str] = []

    def __call__(
        self,
        url: str,
        method: str,
        payload: dict[str, object] | None,
        headers: dict[str, str],
        timeout: float,
    ) -> dict[str, object]:
        del method, payload, timeout
        self.calls.append(url)
        if url.endswith("/ping"):
            return {"status": "pong"}
        if url.endswith("/token/login"):
            assert headers["Authorization"].startswith("Basic ")
            return {"access_token": "scoped-probe-token"}
        assert headers == {"Authorization": "Bearer scoped-probe-token"}
        if url.endswith("/show_config"):
            return {
                "exchange": self.exchange,
                "trading_mode": "futures",
                "dry_run": self.dry_run,
                "demo_trading": False,
                "bot_name": self.bot_name,
                "force_entry_enable": True,
                "position_adjustment_enable": True,
                "state": "RUNNING",
            }
        if url.endswith("/version"):
            return {"version": "2026.8"}
        if url.endswith("/whitelist"):
            return {"whitelist": ["BTC/USDT:USDT"]}
        raise AssertionError(url)


def test_account_trading_eligibility_api_uses_exact_version_and_runtime_boundary(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    TradingService(database).bootstrap_admin("trading-eligibility-api-admin", now=now)
    verifier = FakeExchangeConnectionVerifier({"BINANCE": ConnectionProbeResult(True, None)})
    settings = Settings(
        environment="test",
        database_url=str(database.engine.url),
        allow_mock_identity=True,
        session_signing_secret="trading-eligibility-api-secret-long-enough",  # noqa: S106
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

    async def scenario() -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            login = await client.post(
                "/api/auth/mock/login",
                json={"username": "trading-eligibility-api-admin"},
            )
            assert login.status_code == 200
            created = await client.post(
                "/api/exchange-accounts",
                json={
                    "account_id": "binance-api-eligible",
                    "venue": "BINANCE",
                    "label": "Binance API Eligible",
                    "credentials": {"api_key": "key", "api_secret": "secret"},
                    "idempotency_key": "create-api-eligible",
                },
            )
            assert created.status_code == 200, created.text
            account_id = created.json()["exchange_account_id"]
            verified = await client.post(
                f"/api/exchange-accounts/{account_id}/connection-verifications",
                json={"expected_version": 1, "idempotency_key": "verify-api-eligible"},
            )
            assert verified.status_code == 200, verified.text
            runtime = await client.put(
                f"/api/exchange-accounts/{account_id}/runtime-sync",
                json={
                    "enabled": True,
                    "expected_version": verified.json()["version"],
                    "idempotency_key": "bind-api-eligible",
                },
            )
            assert runtime.status_code == 200, runtime.text
            enabled_payload = {
                "enabled": True,
                "expected_version": runtime.json()["version"],
                "idempotency_key": "enable-api-eligible",
            }
            enabled = await client.put(
                f"/api/exchange-accounts/{account_id}/trading-eligibility",
                json=enabled_payload,
            )
            assert enabled.status_code == 200, enabled.text
            assert enabled.json()["trading_status"] == "ELIGIBLE"
            assert enabled.json()["trading_enabled"] is True
            replay = await client.put(
                f"/api/exchange-accounts/{account_id}/trading-eligibility",
                json=enabled_payload,
            )
            assert replay.status_code == 200
            assert replay.json()["version"] == enabled.json()["version"]

            unbound = await client.put(
                f"/api/exchange-accounts/{account_id}/runtime-sync",
                json={
                    "enabled": False,
                    "expected_version": enabled.json()["version"],
                    "idempotency_key": "unbind-api-eligible",
                },
            )
            assert unbound.status_code == 200, unbound.text
            item = unbound.json()["data"]["data"][0]
            assert item["trading"]["status"] == "BLOCKED"
            assert item["trading"]["enabled"] is False

    asyncio.run(scenario())


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
                    secret not in response.text for secret in credentials_by_venue[venue].values()
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

            cooldown_reuse = await client.post(
                f"/api/exchange-accounts/{account_ids['BINANCE']}/connection-verifications",
                json={
                    "expected_version": results["BINANCE"]["version"],
                    "idempotency_key": "verify-binance-team-account-cooldown-reuse",
                },
            )
            assert cooldown_reuse.status_code == 200, cooldown_reuse.text
            assert cooldown_reuse.json()["version"] == results["BINANCE"]["version"]
            assert cooldown_reuse.json()["connection"] == results["BINANCE"]["connection"]
            assert len(verifier.calls) == 4

            for venue in {"BINANCE", "HYPERLIQUID", "BYBIT"}:
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

            listed = (await client.get("/api/exchange-accounts")).json()["data"]["data"]
            by_venue = {item["venue"]: item for item in listed}
            assert by_venue["BINANCE"]["runtime_binding"]["bound"] is True
            assert by_venue["HYPERLIQUID"]["runtime_binding"]["bound"] is True
            assert by_venue["OKX"]["connection"]["status"] == "FAILED"
            assert by_venue["OKX"]["runtime_binding"]["read_only_connector"] == "IMPLEMENTED"
            assert (
                by_venue["OKX"]["runtime_binding"]["connection_verification_connector"]
                == "IMPLEMENTED"
            )
            assert by_venue["BYBIT"]["permissions"]["can_verify_connection"] is True
            assert by_venue["BYBIT"]["runtime_binding"]["bound"] is True
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
            "secret" not in item.reason and "passphrase" not in item.reason for item in audits
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
        ("BYBIT", "bybit-team-account"),
    }
    assert all("secret" not in repr(item) for item in bindings)
    hyperliquid_binding = next(item for item in bindings if item.venue == "HYPERLIQUID")
    assert "api_wallet_private_key" not in hyperliquid_binding.credentials


def test_binance_connection_rate_limit_diagnostics_are_persisted_and_defer_reprobe(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    service = TradingService(database, credential_encryption_key=encryption_key())
    admin = service.bootstrap_admin("binance-rate-limit-admin", now=now)
    account_id = service.create_exchange_account(
        actor_id=admin,
        account_id="binance-rate-limited",
        venue="BINANCE",
        label="Binance Rate Limited",
        credentials={"api_key": "rate-key", "api_secret": "rate-secret"},
        idempotency_key="binance-rate-limit-create",
        now=now,
    )
    prepared, replay = service.prepare_exchange_account_connection_verification(
        account_id,
        actor_id=admin,
        expected_version=1,
        idempotency_key="binance-rate-limit-first-probe",
        now=now,
    )
    assert replay is None and prepared is not None
    diagnostics: dict[str, object] = {
        "category": "REQUEST_WEIGHT_EXCEEDED",
        "http_status": 429,
        "binance_error_code": -1003,
        "binance_error_message": "Too much request weight used",
        "retry_after_seconds": 60,
        "rate_limit_headers": {
            "Retry-After": "60",
            "X-MBX-USED-WEIGHT-1M": "1200",
        },
        "failed_at": now.isoformat(),
        "next_retry_at": (now + timedelta(seconds=60)).isoformat(),
    }
    result = service.record_exchange_account_connection_verification(
        prepared,
        ConnectionProbeResult(False, "BINANCE_RATE_LIMITED", diagnostics),
        actor_id=admin,
        idempotency_key="binance-rate-limit-first-probe",
        now=now,
    )

    assert result["connection"]["error_code"] == "BINANCE_RATE_LIMITED"
    assert result["connection"]["diagnostics"] == diagnostics
    projected = TradingQueries(database).exchange_accounts(admin)["data"][0]
    assert projected["connection"]["diagnostics"] == diagnostics
    with pytest.raises(DomainRejected) as deferred:
        service.prepare_exchange_account_connection_verification(
            account_id,
            actor_id=admin,
            expected_version=2,
            idempotency_key="binance-rate-limit-early-reprobe",
            now=now + timedelta(seconds=30),
        )
    assert deferred.value.code == "BINANCE_CONNECTION_RETRY_DEFERRED"
    assert deferred.value.metadata == diagnostics


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


def test_account_trading_eligibility_is_versioned_idempotent_and_fails_closed(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    service = TradingService(database, credential_encryption_key=encryption_key())
    admin = service.bootstrap_admin("trading-eligibility-admin", now=now)
    exchange_account_id = service.create_exchange_account(
        actor_id=admin,
        account_id="binance-live-eligible",
        venue="BINANCE",
        label="Binance Live Eligible",
        credentials={"api_key": "fixture-key", "api_secret": "fixture-secret"},
        idempotency_key="create-trading-eligible-account",
        now=now,
    )
    command, replay = service.prepare_exchange_account_connection_verification(
        exchange_account_id,
        actor_id=admin,
        expected_version=1,
        idempotency_key="verify-trading-eligible-account",
    )
    assert replay is None and command is not None
    verified = service.record_exchange_account_connection_verification(
        command,
        ConnectionProbeResult(True, None),
        actor_id=admin,
        idempotency_key="verify-trading-eligible-account",
        now=now + timedelta(seconds=1),
    )
    runtime = service.configure_exchange_account_runtime_sync(
        exchange_account_id,
        actor_id=admin,
        enabled=True,
        expected_version=int(verified["version"]),
        idempotency_key="bind-trading-eligible-account",
        now=now + timedelta(seconds=2),
    )
    eligible = service.configure_exchange_account_trading(
        exchange_account_id,
        actor_id=admin,
        enabled=True,
        expected_version=int(runtime["version"]),
        idempotency_key="enable-exact-account-trading",
        now=now + timedelta(seconds=3),
    )
    assert eligible == {
        "exchange_account_id": str(exchange_account_id),
        "trading_status": "ELIGIBLE",
        "trading_enabled": True,
        "version": 4,
    }
    assert (
        service.configure_exchange_account_trading(
            exchange_account_id,
            actor_id=admin,
            enabled=True,
            expected_version=int(runtime["version"]),
            idempotency_key="enable-exact-account-trading",
            now=now + timedelta(seconds=4),
        )
        == eligible
    )
    projected = TradingQueries(database).exchange_accounts(admin)["data"][0]
    assert projected["trading"]["status"] == "ELIGIBLE"
    assert projected["trading"]["enabled"] is True
    assert projected["permissions"]["can_manage_trading"] is True

    disabled_runtime = service.configure_exchange_account_runtime_sync(
        exchange_account_id,
        actor_id=admin,
        enabled=False,
        expected_version=int(eligible["version"]),
        idempotency_key="unbind-trading-eligible-account",
        now=now + timedelta(seconds=5),
    )
    assert disabled_runtime["version"] == 5
    blocked = TradingQueries(database).exchange_accounts(admin)["data"][0]
    assert blocked["trading"] == {
        "status": "BLOCKED",
        "enabled": False,
        "reason": (
            "account eligibility is blocked because a required connection or runtime fact was lost"
        ),
    }
    with pytest.raises(DomainRejected, match="ACCOUNT_TRADING_NOT_READY"):
        service.configure_exchange_account_trading(
            exchange_account_id,
            actor_id=admin,
            enabled=True,
            expected_version=int(disabled_runtime["version"]),
            idempotency_key="unsafe-reenable-account-trading",
            now=now + timedelta(seconds=6),
        )
    rebound = service.configure_exchange_account_runtime_sync(
        exchange_account_id,
        actor_id=admin,
        enabled=True,
        expected_version=int(disabled_runtime["version"]),
        idempotency_key="rebind-trading-eligible-account",
        now=now + timedelta(seconds=7),
    )
    reeligible = service.configure_exchange_account_trading(
        exchange_account_id,
        actor_id=admin,
        enabled=True,
        expected_version=int(rebound["version"]),
        idempotency_key="reenable-exact-account-trading",
        now=now + timedelta(seconds=8),
    )
    failure_command, failure_replay = service.prepare_exchange_account_connection_verification(
        exchange_account_id,
        actor_id=admin,
        expected_version=int(reeligible["version"]),
        idempotency_key="fail-eligible-account-verification",
    )
    assert failure_replay is None and failure_command is not None
    failed = service.record_exchange_account_connection_verification(
        failure_command,
        ConnectionProbeResult(False, "BINANCE_AUTHENTICATION_FAILED"),
        actor_id=admin,
        idempotency_key="fail-eligible-account-verification",
        now=now + timedelta(seconds=9),
    )
    assert failed["trading"] == {"status": "BLOCKED", "enabled": False}
    failed_projection = TradingQueries(database).exchange_accounts(admin)["data"][0]
    assert failed_projection["runtime_binding"]["bound"] is False
    assert failed_projection["trading"]["status"] == "BLOCKED"
    with database.session_factory() as session:
        audit = session.scalar(
            select(AuditEvent).where(
                AuditEvent.event_type == "EXCHANGE_ACCOUNT_TRADING_ELIGIBILITY_ENABLED"
            )
        )
        assert audit is not None
        assert audit.account_id == "binance-live-eligible"
        assert "global_live_send_gate=unchanged" in audit.reason


@pytest.mark.parametrize("venue", ["OKX", "BYBIT"])
def test_okx_bybit_reuse_exact_account_freqtrade_eligibility(
    database: Database, venue: str
) -> None:
    now = datetime.now(UTC)
    service = TradingService(database, credential_encryption_key=encryption_key())
    slug = venue.lower()
    admin = service.bootstrap_admin(f"{slug}-trading-eligibility-admin", now=now)
    exchange_account_id = service.create_exchange_account(
        actor_id=admin,
        account_id=f"{slug}-live-account",
        venue=venue,
        label=f"{venue} Live Account",
        credentials={
            "api_key": "key",
            "api_secret": "secret",
            **({"passphrase": "phrase"} if venue == "OKX" else {}),
        },
        idempotency_key=f"create-{slug}-live-account",
        now=now,
    )
    command, replay = service.prepare_exchange_account_connection_verification(
        exchange_account_id,
        actor_id=admin,
        expected_version=1,
        idempotency_key=f"verify-{slug}-live-account",
    )
    assert replay is None and command is not None
    verified = service.record_exchange_account_connection_verification(
        command,
        ConnectionProbeResult(True, None),
        actor_id=admin,
        idempotency_key=f"verify-{slug}-live-account",
        now=now + timedelta(seconds=1),
    )
    runtime = service.configure_exchange_account_runtime_sync(
        exchange_account_id,
        actor_id=admin,
        enabled=True,
        expected_version=int(verified["version"]),
        idempotency_key=f"bind-{slug}-live-account",
        now=now + timedelta(seconds=2),
    )
    configured = service.configure_exchange_account_freqtrade_worker(
        exchange_account_id,
        actor_id=admin,
        mode="LIVE",
        name=f"{slug}-account-worker",
        base_url="http://127.0.0.1:18083",
        username="control-plane",
        password="worker-fixture-password",  # noqa: S106
        ws_token=f"{slug}-rpc-token-fixture",
        hip3_dexes=(),
        expected_version=int(runtime["version"]),
        idempotency_key=f"configure-{slug}-account-worker",
        now=now + timedelta(seconds=3),
    )
    binding, worker_replay = service.prepare_exchange_account_freqtrade_verification(
        exchange_account_id,
        actor_id=admin,
        expected_version=int(configured["version"]),
        idempotency_key=f"verify-{slug}-account-worker",
    )
    assert worker_replay is None and binding is not None
    worker_verified = service.record_exchange_account_freqtrade_verification(
        binding,
        actor_id=admin,
        error_code=None,
        idempotency_key=f"verify-{slug}-account-worker",
        now=now + timedelta(seconds=4),
    )
    eligible = service.configure_exchange_account_trading(
        exchange_account_id,
        actor_id=admin,
        enabled=True,
        expected_version=int(worker_verified["version"]),
        idempotency_key=f"enable-{slug}-trading",
        now=now + timedelta(seconds=5),
    )
    assert eligible["trading_status"] == "ELIGIBLE"
    projected = TradingQueries(database).exchange_accounts(admin)["data"][0]
    assert projected["runtime_binding"]["trading_connector"] == "FREQTRADE_EXTERNAL"
    assert projected["execution_worker"]["supported"] is True
    assert projected["execution_worker"]["scope"] == {
        "team_id": str(binding.team_id),
        "account_id": f"{slug}-live-account",
        "venue": venue,
    }
    assert projected["execution_worker"]["live_ready"] is True
    assert projected["trading"]["enabled"] is True


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

    first_account_id = create_verified_binding("First Team Binance", "first-team-secret", 1)
    second_team_id = service.create_team(
        actor_id=admin,
        name="Runtime Accounts Two",
        slug="runtime-accounts-two",
        idempotency_key="create-runtime-accounts-two",
        now=now + timedelta(seconds=2),
    )
    second_account_id = create_verified_binding("Second Team Binance", "second-team-secret", 3)

    bindings = service.runtime_account_bindings()
    assert len(bindings) == 2
    by_team = {item.team_id: item for item in bindings}
    assert set(by_team) == {first_team_id, second_team_id}
    assert all(item.account_id == "shared-binance-main" for item in bindings)
    assert (
        by_team[first_team_id].credentials["api_secret"] == "first-team-secret"  # noqa: S105
    )
    assert (
        by_team[second_team_id].credentials["api_secret"] == "second-team-secret"  # noqa: S105
    )
    assert all("secret" not in repr(item) for item in bindings)

    stale_second_binding = by_team[second_team_id]
    service.configure_exchange_account_freqtrade_worker(
        second_account_id,
        actor_id=admin,
        mode="LIVE",
        name="second-runtime-worker",
        base_url="http://127.0.0.1:18083",
        username="control-plane",
        password="worker-fixture-password",  # noqa: S106
        ws_token="second-runtime-rpc-token",  # noqa: S106
        hip3_dexes=(),
        expected_version=stale_second_binding.account_version,
        idempotency_key="configure-second-runtime-worker",
        now=now + timedelta(seconds=4),
    )
    service.record_runtime_source_health(
        stale_second_binding.service_principal_id,
        {"BINANCE": {"status": "SUCCESS", "items_observed": 1}},
        scopes={"BINANCE": (stale_second_binding.account_id, "BINANCE")},
        runtime_account_binding=stale_second_binding,
        now=now + timedelta(seconds=4, milliseconds=100),
    )

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
        expected_version=4,
        idempotency_key="rotate-second-runtime-account",
        now=now + timedelta(seconds=5),
    )
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
    assert (
        TradingQueries(database).exchange_accounts(admin)["data"][0]["runtime_binding"]["bound"]
        is True
    )
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
