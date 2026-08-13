from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from conftest import add_exchange_account_fixture
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from trading_control_plane.api import create_app
from trading_control_plane.config import Settings
from trading_control_plane.database import Database
from trading_control_plane.domain import Role
from trading_control_plane.models import AuditEvent, OrderIntent, TradingAuthorization
from trading_control_plane.perptape import PerptapeClient
from trading_control_plane.queries import TradingQueries
from trading_control_plane.service import TradingService
from trading_control_plane.telegram import TelegramBotClient, TelegramBotGateway


class RecordingBotApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def poster(self, url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        del timeout
        self.calls.append((url.rsplit("/", maxsplit=1)[-1], payload))
        return {"ok": True, "result": {}}


def test_telegram_review_confirmation_writes_audit_without_authorization_or_order(
    database: Database,
) -> None:
    service = TradingService(database)
    now = datetime.now(UTC)
    admin = service.bootstrap_admin("telegram-admin", now=now)
    add_exchange_account_fixture(service.database, admin, "acct-1", "BINANCE")
    proposer = service.create_user("telegram-proposer", admin, now=now)
    reviewer = service.create_user("telegram-reviewer", admin, now=now)
    service.assign_role(proposer, Role.PROPOSER, admin, now=now)
    service.assign_role(reviewer, Role.REVIEWER, admin, now=now)
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
    fake = RecordingBotApi()
    gateway = TelegramBotGateway(
        token="123456789:abcdefghijklmnopqrstuvwxyz",  # noqa: S106
        allowed_username="telegram-reviewer",
        internal_username="telegram-reviewer",
        binder=lambda chat_id, _telegram_username, _internal_username: chat_id,
        chat_resolver=lambda user_id: "789" if user_id == reviewer else None,
        client=TelegramBotClient(
            "123456789:abcdefghijklmnopqrstuvwxyz",
            base_url="https://telegram.invalid",
            poster=fake.poster,
        ),
    )
    settings = Settings(
        environment="test",
        database_url=str(database.engine.url),
        allow_mock_identity=True,
        session_signing_secret="telegram-review-test-signing-secret",  # noqa: S106
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
        telegram_gateway=gateway,
    )

    async def create_proposal() -> UUID:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            login = await client.post(
                "/api/auth/mock/login",
                json={"username": "telegram-proposer"},
            )
            assert login.status_code == 200
            created = await client.post(
                "/api/proposals/manual",
                json={
                    "environment": "LIVE",
                    "account_id": "acct-1",
                    "venue": "BINANCE",
                    "instrument_id": str(instrument_id),
                    "direction": "LONG",
                    "risk_tier": "MEDIUM",
                    "quantity": "0.001",
                    "max_risk": "1",
                    "expires_in_minutes": 480,
                    "trigger_price": "100000",
                    "invalidation_price": "98000",
                    "rationale": "frozen facts for Telegram independent review",
                    "idempotency_key": "telegram-review-proposal",
                },
            )
            assert created.status_code == 200, created.text
            return UUID(created.json()["proposal_id"])

    proposal_id = asyncio.run(create_proposal())
    notification_call = next(payload for method, payload in fake.calls if method == "sendMessage")
    keyboard = notification_call["reply_markup"]["inline_keyboard"]
    approve_key = keyboard[0][0]["callback_data"]
    callback_base = {
        "from": {"id": 789},
        "message": {"message_id": 1, "chat": {"id": 789, "type": "private"}},
    }
    gateway.handle_update(
        {
            "update_id": 100,
            "callback_query": {
                "id": "approve-source",
                "data": approve_key,
                **callback_base,
            },
        }
    )
    assert TradingQueries(database).proposal_detail(admin, proposal_id)["status"] == (
        "PENDING_REVIEW"
    )
    confirmation = next(
        payload for method, payload in reversed(fake.calls) if method == "editMessageText"
    )
    confirm_key = confirmation["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    gateway.handle_update(
        {
            "update_id": 101,
            "callback_query": {
                "id": "approve-confirm",
                "data": confirm_key,
                **callback_base,
            },
        }
    )

    proposal = TradingQueries(database).proposal_detail(admin, proposal_id, now=now)
    assert proposal["status"] == "APPROVED"
    assert proposal["approvals"][0]["reviewer_id"] == str(reviewer)
    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(TradingAuthorization)) == 0
        assert session.scalar(select(func.count()).select_from(OrderIntent)) == 0
        events = set(session.scalars(select(AuditEvent.event_type)).all())
    assert "PROPOSAL_REVIEWED" in events
