from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from trading_control_plane.api import create_app
from trading_control_plane.binance import (
    BinanceEquity,
    BinanceFill,
    BinanceFunding,
    BinanceInstrument,
    BinanceOrder,
    BinancePosition,
    BinanceProtection,
    BinanceReadOnlySnapshot,
)
from trading_control_plane.config import Settings
from trading_control_plane.database import Database
from trading_control_plane.domain import ExecutionEnvironment, Role, SystemRiskState
from trading_control_plane.models import (
    AccountEquity,
    FundingPayment,
    Instrument,
    Position,
    ProtectionOrder,
    VenueFill,
    VenueOrder,
)
from trading_control_plane.perptape import PerptapeClient
from trading_control_plane.service import TradingService


class StaticBinanceReadOnlyClient:
    configured = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, datetime]] = []

    def read_snapshot(self, symbol: str, *, now: datetime) -> BinanceReadOnlySnapshot:
        self.calls.append((symbol, now))
        return BinanceReadOnlySnapshot(
            symbol=symbol,
            observed_at=now,
            instrument=BinanceInstrument(
                symbol=symbol,
                tick_size=Decimal("0.1"),
                lot_size=Decimal("0.001"),
                minimum_notional=Decimal("5"),
                quote_currency="USDT",
                collateral_currency="USDT",
                active=True,
            ),
            orders=(
                BinanceOrder(
                    order_id="m3-external-stop",
                    client_order_id="external-owner",
                    status="SENT",
                    side="SELL",
                    order_type="STOP_MARKET",
                    ordered_quantity=Decimal("0.5"),
                    filled_quantity=Decimal(0),
                    stop_price=Decimal("59000"),
                    reduce_only=True,
                    close_position=False,
                    observed_at=now,
                ),
            ),
            fills=(
                BinanceFill(
                    fill_id="m3-external-fill",
                    order_id="m3-completed-order",
                    side="BUY",
                    quantity=Decimal("0.5"),
                    price=Decimal("60000"),
                    fee=Decimal("1.25"),
                    fee_currency="USDT",
                    executed_at=now,
                ),
            ),
            position=BinancePosition(
                quantity=Decimal("0.5"),
                average_entry_price=Decimal("60000"),
                mark_price=Decimal("61000"),
                observed_at=now,
            ),
            equity=BinanceEquity(
                equity=Decimal("10500"),
                available_balance=Decimal("9000"),
                currency="USDT",
                observed_at=now,
            ),
            funding=(
                BinanceFunding(
                    payment_id="m3-funding",
                    amount=Decimal("-0.75"),
                    currency="USDT",
                    paid_at=now,
                ),
            ),
            protection=BinanceProtection(
                order_id="m3-external-stop",
                quantity=Decimal("0.5"),
                trigger_price=Decimal("59000"),
                observed_at=now,
            ),
        )


def seed(service: TradingService) -> dict[str, UUID]:
    now = datetime.now(UTC)
    admin = service.bootstrap_admin("admin", now=now)
    operator = service.create_user("operator", admin, now=now)
    observer = service.create_user("observer", admin, now=now)
    service.assign_role(operator, Role.OPERATOR, admin, "acct-live", "BINANCE", now=now)
    service.assign_role(observer, Role.OBSERVER, admin, "acct-other", "BINANCE", now=now)
    service.set_risk_policy(
        actor_id=admin,
        version="m3-risk-v1",
        system_state=SystemRiskState.NORMAL,
        max_total_risk=Decimal("100"),
        max_account_risk=Decimal("100"),
        max_single_loss=Decimal("100"),
        max_consecutive_losses=3,
        loss_cooldown=timedelta(hours=1),
        max_fact_age=timedelta(minutes=5),
        now=now,
    )
    return {"admin": admin, "operator": operator, "observer": observer}


def m3_app(
    database: Database,
    client: StaticBinanceReadOnlyClient,
    *,
    enabled: bool,
    account_id: str | None = "acct-live",
) -> FastAPI:
    settings = Settings(
        environment="test",
        database_url=str(database.engine.url),
        allow_mock_identity=True,
        session_signing_secret="m3-test-signing-secret-that-is-long-enough",  # noqa: S106
        public_base_url="http://test",
        binance_read_only_enabled=enabled,
        binance_fact_environment="LIVE",
        runtime_binance_account_id=account_id,
        binance_api_key="fixture-read-only-key",
        binance_api_secret="fixture-read-only-secret",  # noqa: S106
        _env_file=None,
    )
    perptape = PerptapeClient(
        base_url="https://perptape.com",
        api_key=None,
        contract_version="breakouts-v1",
        cache_ttl=timedelta(minutes=1),
    )
    return create_app(settings, database, perptape, binance_client=client)  # type: ignore[arg-type]


async def login(client: AsyncClient, username: str) -> None:
    response = await client.post("/api/auth/mock/login", json={"username": username})
    assert response.status_code == 200, response.text


async def logout(client: AsyncClient) -> None:
    response = await client.post("/api/auth/logout")
    assert response.status_code == 200, response.text


async def run_m3_flow(database: Database) -> None:
    service = TradingService(database)
    ids = seed(service)
    client = StaticBinanceReadOnlyClient()
    async with AsyncClient(
        transport=ASGITransport(app=m3_app(database, client, enabled=True)),
        base_url="http://test",
    ) as http:
        await login(http, "operator")
        status = await http.get("/api/venues/binance/status")
        assert status.status_code == 200, status.text
        assert status.json() == {
            "venue": "BINANCE",
            "mode": "USER_DATA_READ_ONLY",
            "enabled": True,
            "configured": True,
            "execution_backend": "FREQTRADE",
            "order_send_available": False,
            "worker_configured": False,
            "account_mode": "PORTFOLIO_MARGIN",
            "fact_environment": "LIVE",
            "automatic_sync_enabled": False,
            "automatic_sync_interval_seconds": 60,
            "default_account_id": "acct-live",
            "environment": "test",
        }

        first = await http.post(
            "/api/venues/binance/sync",
            json={"account_id": "acct-live", "symbol": "BTCUSDT"},
        )
        assert first.status_code == 200, first.text
        result = first.json()
        assert result["environment"] == "LIVE"
        assert result["mode"] == "READ_ONLY"
        assert result["reconciliation"]["execution_scope"] == "LIVE:acct-live:BINANCE"
        assert result["reconciliation"]["status"] == "DIFFERENCE"
        assert result["facts"]["environment"] == "LIVE"
        assert result["facts"]["orders"][0]["intent_id"] is None
        instrument_id = UUID(result["persisted"]["instrument_id"])

        # The same account/instrument can hold independent SHADOW facts without
        # overwriting or being included in the LIVE reconciliation.
        now = datetime.now(UTC)
        service.record_position(
            "acct-live",
            "BINANCE",
            instrument_id,
            Decimal(0),
            Decimal(0),
            Decimal("61000"),
            True,
            ids["operator"],
            environment=ExecutionEnvironment.SHADOW,
            now=now,
        )
        service.record_account_equity(
            "acct-live",
            "BINANCE",
            Decimal("1000"),
            Decimal("1000"),
            "USDT",
            True,
            ids["operator"],
            environment=ExecutionEnvironment.SHADOW,
            now=now,
        )

        duplicate = await http.post(
            "/api/venues/binance/sync",
            json={"account_id": "acct-live", "symbol": "BTCUSDT"},
        )
        assert duplicate.status_code == 200, duplicate.text
        assert duplicate.json()["reconciliation"]["status"] == "DIFFERENCE"
        facts = await http.get("/api/venues/binance/facts", params={"account_id": "acct-live"})
        assert facts.status_code == 200, facts.text
        assert facts.json()["data"]["positions"][0]["quantity"] == "0.500000000000000000"
        assert len(client.calls) == 2

        web = await http.get("/venues/binance")
        assert web.status_code == 200, web.text
        assert "<title>交易控制台</title>" in web.text

        await logout(http)
        await login(http, "observer")
        calls_before_denial = len(client.calls)
        denied = await http.post(
            "/api/venues/binance/sync",
            json={"account_id": "acct-live", "symbol": "BTCUSDT"},
        )
        assert denied.status_code == 403, denied.text
        assert denied.json()["error"]["code"] == "RBAC_DENIED"
        assert len(client.calls) == calls_before_denial
        hidden = await http.get("/api/venues/binance/facts", params={"account_id": "acct-live"})
        assert hidden.status_code == 403, hidden.text

    calls_before_disabled = len(client.calls)
    async with AsyncClient(
        transport=ASGITransport(app=m3_app(database, client, enabled=False)),
        base_url="http://test",
    ) as disabled_http:
        await login(disabled_http, "operator")
        disabled = await disabled_http.post(
            "/api/venues/binance/sync",
            json={"account_id": "acct-live", "symbol": "BTCUSDT"},
        )
        assert disabled.status_code == 503, disabled.text
        assert disabled.json()["error"]["code"] == "BINANCE_READ_ONLY_DISABLED"
        assert len(client.calls) == calls_before_disabled

    unconfigured_client = StaticBinanceReadOnlyClient()
    unconfigured_client.configured = False
    async with AsyncClient(
        transport=ASGITransport(app=m3_app(database, unconfigured_client, enabled=True)),
        base_url="http://test",
    ) as unconfigured_http:
        await login(unconfigured_http, "operator")
        unconfigured = await unconfigured_http.post(
            "/api/venues/binance/sync",
            json={"account_id": "acct-live", "symbol": "BTCUSDT"},
        )
        assert unconfigured.status_code == 503, unconfigured.text
        assert unconfigured.json()["error"]["code"] == ("BINANCE_READ_ONLY_NOT_CONFIGURED")
        assert unconfigured_client.calls == []

    missing_account_client = StaticBinanceReadOnlyClient()
    async with AsyncClient(
        transport=ASGITransport(
            app=m3_app(database, missing_account_client, enabled=True, account_id=None)
        ),
        base_url="http://test",
    ) as missing_account_http:
        await login(missing_account_http, "operator")
        missing_account = await missing_account_http.get(
            "/api/venues/binance/facts", params={"account_id": "acct-live"}
        )
        assert missing_account.status_code == 503, missing_account.text
        assert missing_account.json()["error"]["code"] == "DEFAULT_ACCOUNT_NOT_CONFIGURED"
        assert missing_account_client.calls == []

    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Instrument)) == 1
        assert session.scalar(select(func.count()).select_from(Position)) == 2
        assert session.scalar(select(func.count()).select_from(AccountEquity)) == 2
        assert session.scalar(select(func.count()).select_from(ProtectionOrder)) == 1
        assert session.scalar(select(func.count()).select_from(VenueOrder)) == 1
        assert session.scalar(select(func.count()).select_from(VenueFill)) == 1
        assert session.scalar(select(func.count()).select_from(FundingPayment)) == 1
        live_position = session.scalar(select(Position).where(Position.environment == "LIVE"))
        shadow_position = session.scalar(select(Position).where(Position.environment == "SHADOW"))
        assert live_position is not None and live_position.quantity == Decimal("0.5")
        assert shadow_position is not None and shadow_position.quantity == 0


def test_binance_read_only_sync_persists_dedupes_reconciles_and_isolates_environment(
    database: Database,
) -> None:
    asyncio.run(run_m3_flow(database))
