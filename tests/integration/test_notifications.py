from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from trading_control_plane.api import create_app
from trading_control_plane.config import Settings
from trading_control_plane.database import Database
from trading_control_plane.domain import DomainRejected, Role, SignalSourceMode
from trading_control_plane.models import AuditEvent, NotificationDelivery, NotificationRoute
from trading_control_plane.notification import (
    NotificationDispatcher,
    NotificationMessage,
    NotificationSendResult,
    NotificationTransportError,
)
from trading_control_plane.queries import TradingQueries
from trading_control_plane.service import TradingService


def encryption_key() -> str:
    return base64.urlsafe_b64encode(b"n" * 32).decode().rstrip("=")


class RecordingSender:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str], NotificationMessage]] = []

    def send(
        self,
        channel: str,
        configuration: dict[str, str],
        message: NotificationMessage,
    ) -> NotificationSendResult:
        self.calls.append((channel, configuration, message))
        return NotificationSendResult("external-test-id")


class RetryThenSend(RecordingSender):
    def send(
        self,
        channel: str,
        configuration: dict[str, str],
        message: NotificationMessage,
    ) -> NotificationSendResult:
        self.calls.append((channel, configuration, message))
        if len(self.calls) == 1:
            raise NotificationTransportError(
                "NOTIFICATION_RATE_LIMITED",
                retryable=True,
                retry_after_seconds=60,
            )
        return NotificationSendResult("retry-success")


class UnknownSender(RecordingSender):
    def send(
        self,
        channel: str,
        configuration: dict[str, str],
        message: NotificationMessage,
    ) -> NotificationSendResult:
        self.calls.append((channel, configuration, message))
        raise NotificationTransportError(
            "NOTIFICATION_OUTCOME_UNKNOWN",
            retryable=False,
            outcome_unknown=True,
        )


def configure_slack_route(
    service: TradingService,
    admin: UUID,
    *,
    now: datetime,
    idempotency_key: str = "route-create",
) -> UUID:
    result = service.configure_notification_route(
        actor_id=admin,
        notification_route_id=None,
        name="Risk and signal alerts",
        channel="SLACK",
        event_types=["RISK_DECISION_RECORDED", "SIGNAL_EVENT_RECEIVED"],
        enabled=True,
        configuration={"webhook_url": "https://hooks.slack.com/services/T/B/private-value"},
        expected_version=0,
        idempotency_key=idempotency_key,
        now=now,
    )
    return UUID(result["notification_route_id"])


def enqueue_signal(
    service: TradingService,
    *,
    team_id: UUID,
    object_id: str,
    idempotency_key: str,
    now: datetime,
) -> dict[str, object]:
    return service.enqueue_notification_event(
        actor_id="integration-test",
        team_id=team_id,
        event_type="SIGNAL_EVENT_RECEIVED",
        payload={
            "summary": "verified signal",
            "provider": "TRADINGVIEW",
            "symbol": "BTCUSDT",
        },
        object_type="SignalEvent",
        object_id=object_id,
        object_version=1,
        idempotency_key=idempotency_key,
        correlation_id=uuid4(),
        environment=None,
        account_id=None,
        venue="BINANCE",
        now=now,
    )


def test_notification_outbox_encrypts_routes_retries_known_failures_and_fences_versions(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    service = TradingService(database, credential_encryption_key=encryption_key())
    admin = service.bootstrap_admin("notification-admin", now=now)
    context = TradingQueries(database).user_context(admin)
    team_id = UUID(context["active_team"]["team_id"])
    route_id = configure_slack_route(service, admin, now=now)

    with database.session_factory() as session:
        route = session.get(NotificationRoute, route_id)
        assert route is not None
        assert "hooks.slack.com" not in route.configuration_ciphertext
        assert "private-value" not in route.configuration_ciphertext
        assert route.configuration_metadata == {
            "channel": "SLACK",
            "configuration_state": "ENCRYPTED",
            "destination_hint": "hooks.slack.com",
        }

    queued = enqueue_signal(
        service,
        team_id=team_id,
        object_id="signal-001",
        idempotency_key="signal-event-001",
        now=now,
    )
    delivery_id = UUID(queued["notification_delivery_ids"][0])  # type: ignore[index]
    replay = enqueue_signal(
        service,
        team_id=team_id,
        object_id="signal-001",
        idempotency_key="signal-event-001",
        now=now,
    )
    assert replay == queued

    retry_sender = RetryThenSend()
    dispatcher = NotificationDispatcher(
        database,
        credential_encryption_key=encryption_key(),
        sender=retry_sender,
    )
    assert dispatcher.dispatch_one(delivery_id, now=now) == "RETRY_WAIT"
    assert dispatcher.dispatch_one(delivery_id, now=now + timedelta(seconds=59)) == "SKIPPED"
    assert dispatcher.dispatch_one(delivery_id, now=now + timedelta(seconds=60)) == "SENT"
    assert len(retry_sender.calls) == 2
    assert retry_sender.calls[0][0] == "SLACK"
    assert retry_sender.calls[0][1] == {
        "webhook_url": "https://hooks.slack.com/services/T/B/private-value"
    }
    assert "event_id:" in retry_sender.calls[0][2].text

    pending = enqueue_signal(
        service,
        team_id=team_id,
        object_id="signal-002",
        idempotency_key="signal-event-002",
        now=now + timedelta(minutes=2),
    )
    pending_id = UUID(pending["notification_delivery_ids"][0])  # type: ignore[index]
    service.configure_notification_route(
        actor_id=admin,
        notification_route_id=route_id,
        name="Risk and signal alerts",
        channel="SLACK",
        event_types=["RISK_DECISION_RECORDED", "SIGNAL_EVENT_RECEIVED"],
        enabled=True,
        configuration=None,
        expected_version=1,
        idempotency_key="route-update-v2",
        now=now + timedelta(minutes=2),
    )
    assert dispatcher.dispatch_one(pending_id, now=now + timedelta(minutes=2)) == "CANCELLED"

    unknown = enqueue_signal(
        service,
        team_id=team_id,
        object_id="signal-003",
        idempotency_key="signal-event-003",
        now=now + timedelta(minutes=3),
    )
    unknown_id = UUID(unknown["notification_delivery_ids"][0])  # type: ignore[index]
    unknown_sender = UnknownSender()
    unknown_dispatcher = NotificationDispatcher(
        database,
        credential_encryption_key=encryption_key(),
        sender=unknown_sender,
    )
    assert (
        unknown_dispatcher.dispatch_one(unknown_id, now=now + timedelta(minutes=3))
        == "OUTCOME_UNKNOWN"
    )
    assert unknown_dispatcher.dispatch_due(now=now + timedelta(hours=1), limit=50)["selected"] == 0

    observer = service.create_user("notification-observer", admin, now=now)
    service.assign_role(observer, Role.OBSERVER, admin, now=now)
    center = TradingQueries(database).notification_center(observer)
    assert center["channel_permissions"] == {
        "trading": False,
        "funding": False,
        "signing": False,
        "broadcast": False,
    }
    assert center["routes"][0]["configuration_state"] == "ENCRYPTED"
    assert "configuration_ciphertext" not in center["routes"][0]
    assert center["delivery_status_counts"] == {
        "CANCELLED": 1,
        "OUTCOME_UNKNOWN": 1,
        "SENT": 1,
    }
    with pytest.raises(DomainRejected) as denied:
        service.configure_notification_route(
            actor_id=observer,
            notification_route_id=route_id,
            name="Observer mutation",
            channel="SLACK",
            event_types=["SIGNAL_EVENT_RECEIVED"],
            enabled=False,
            configuration=None,
            expected_version=2,
            idempotency_key="observer-denied",
            now=now + timedelta(minutes=4),
        )
    assert denied.value.code == "RBAC_DENIED"

    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(NotificationDelivery)) == 3
        audit_types = set(session.scalars(select(AuditEvent.event_type)).all())
    assert {
        "NOTIFICATION_EVENT_QUEUED",
        "NOTIFICATION_RETRY_SCHEDULED",
        "NOTIFICATION_SENT",
        "NOTIFICATION_CANCELLED",
        "NOTIFICATION_OUTCOME_UNKNOWN",
    }.issubset(audit_types)


def test_notification_route_delete_clears_secret_cancels_queue_and_allows_name_reuse(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    service = TradingService(database, credential_encryption_key=encryption_key())
    admin = service.bootstrap_admin("notification-delete-admin", now=now)
    context = TradingQueries(database).user_context(admin)
    team_id = UUID(context["active_team"]["team_id"])
    route_id = configure_slack_route(
        service,
        admin,
        now=now,
        idempotency_key="notification-route-delete-fixture",
    )
    queued = enqueue_signal(
        service,
        team_id=team_id,
        object_id="signal-delete-fixture",
        idempotency_key="signal-delete-fixture",
        now=now,
    )
    delivery_id = UUID(queued["notification_delivery_ids"][0])  # type: ignore[index]

    deleted = service.delete_notification_route(
        actor_id=admin,
        notification_route_id=route_id,
        expected_version=1,
        idempotency_key="delete-notification-route",
        now=now + timedelta(seconds=1),
    )
    assert deleted["status"] == "DELETED"
    assert TradingQueries(database).notification_center(admin)["routes"] == []
    assert (
        service.delete_notification_route(
            actor_id=admin,
            notification_route_id=route_id,
            expected_version=1,
            idempotency_key="delete-notification-route",
            now=now + timedelta(seconds=2),
        )
        == deleted
    )
    with database.session_factory() as session:
        route = session.get(NotificationRoute, route_id)
        delivery = session.get(NotificationDelivery, delivery_id)
        assert route is not None and route.deleted_at is not None
        assert route.enabled is False
        assert route.configuration_ciphertext == "deleted"
        assert route.configuration_metadata == {"deleted": True}
        assert delivery is not None and delivery.status == "CANCELLED"
        assert delivery.last_error_code == "NOTIFICATION_ROUTE_DELETED"

    replacement = configure_slack_route(
        service,
        admin,
        now=now + timedelta(seconds=3),
        idempotency_key="notification-route-reuse-name",
    )
    assert replacement != route_id
    assert len(TradingQueries(database).notification_center(admin)["routes"]) == 1


def test_notification_api_masks_configuration_and_test_send_has_no_business_authority(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    service = TradingService(database, credential_encryption_key=encryption_key())
    admin = service.bootstrap_admin("notification-api-admin", now=now)
    observer = service.create_user("notification-api-observer", admin, now=now)
    service.assign_role(observer, Role.OBSERVER, admin, now=now)
    sender = RecordingSender()
    app = create_app(
        Settings(
            environment="test",
            database_url=str(database.engine.url),
            allow_mock_identity=True,
            session_signing_secret=secrets.token_urlsafe(32),
            credential_encryption_key=encryption_key(),
            public_base_url="http://test",
            _env_file=None,
        ),
        database,
    )

    async def scenario() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as admin_client:
            login = await admin_client.post(
                "/api/auth/mock/login",
                json={"username": "notification-api-admin"},
            )
            assert login.status_code == 200, login.text
            private_webhook_url = "https://hooks.slack.com/services/T/B/api-private"
            created = await admin_client.post(
                "/api/notification-routes",
                json={
                    "name": "API route",
                    "channel": "SLACK",
                    "event_types": ["SIGNAL_EVENT_RECEIVED", "CAPITAL_STATUS_CHANGED"],
                    "enabled": True,
                    "configuration": {"webhook_url": private_webhook_url},
                    "expected_version": 0,
                    "idempotency_key": "api-route-create",
                },
            )
            assert created.status_code == 200, created.text
            assert private_webhook_url not in created.text
            route_id = created.json()["result"]["notification_route_id"]
            center = created.json()["center"]
            assert center["routes"][0]["configuration_metadata"] == {
                "channel": "SLACK",
                "configuration_state": "ENCRYPTED",
                "destination_hint": "hooks.slack.com",
            }
            capital_event = next(
                item
                for item in center["event_catalog"]
                if item["event_type"] == "CAPITAL_STATUS_CHANGED"
            )
            assert capital_event["integration_status"] == "ACTIVE"
            assert capital_event["blocker"] is None
            assert center["channel_permissions"] == {
                "trading": False,
                "funding": False,
                "signing": False,
                "broadcast": False,
            }

            tested = await admin_client.post(
                f"/api/notification-routes/{route_id}/tests",
                json={"idempotency_key": "api-route-test"},
            )
            assert tested.status_code == 200, tested.text
            assert tested.json()["delivery_status"] == "QUEUED"
            assert private_webhook_url not in tested.text
            assert sender.calls == []
            delivery_id = tested.json()["event"]["notification_delivery_ids"][0]
            assert tested.json()["center"]["deliveries"][0]["status"] == "PENDING"

            dispatcher = NotificationDispatcher(
                database,
                credential_encryption_key=encryption_key(),
                sender=sender,
            )
            assert dispatcher.dispatch_one(UUID(delivery_id), now=datetime.now(UTC)) == "SENT"
            delivered = await admin_client.get("/api/notifications")
            assert delivered.status_code == 200, delivered.text
            assert delivered.json()["deliveries"][0]["status"] == "SENT"
            assert len(sender.calls) == 1
            assert sender.calls[0][0] == "SLACK"
            assert sender.calls[0][1]["webhook_url"] == private_webhook_url
            assert "通知渠道测试" in sender.calls[0][2].text

            shell = await admin_client.get("/notifications")
            assert shell.status_code == 200
            assert 'href="/notifications"' in shell.text

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as observer_client:
            login = await observer_client.post(
                "/api/auth/mock/login",
                json={"username": "notification-api-observer"},
            )
            assert login.status_code == 200, login.text
            visible = await observer_client.get("/api/notifications")
            assert visible.status_code == 200, visible.text
            assert visible.json()["can_manage"] is False
            assert private_webhook_url not in visible.text
            denied = await observer_client.post(
                "/api/notification-routes",
                json={
                    "name": "Denied route",
                    "channel": "SLACK",
                    "event_types": ["SIGNAL_EVENT_RECEIVED"],
                    "enabled": True,
                    "configuration": {"webhook_url": private_webhook_url},
                    "expected_version": 0,
                    "idempotency_key": "observer-route-denied",
                },
            )
            assert denied.status_code == 403, denied.text

    asyncio.run(scenario())


def test_signed_webhook_signal_and_notification_outbox_commit_once_together(
    database: Database,
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    service = TradingService(database, credential_encryption_key=encryption_key())
    admin = service.bootstrap_admin("notification-webhook-admin", now=now)
    context = TradingQueries(database).user_context(admin)
    team_id = UUID(context["active_team"]["team_id"])
    configure_slack_route(service, admin, now=now)
    webhook_value = "notification-webhook-signing-value-123456789"
    source_id = service.configure_signal_source(
        actor_id=admin,
        mode=SignalSourceMode.WEBHOOK,
        secret=webhook_value,
        enabled=True,
        webhook_max_age_seconds=300,
        expected_version=1,
        idempotency_key="webhook-source-config",
        now=now,
    )
    payload = {
        "payload_version": 1,
        "provider": "TRADINGVIEW",
        "external_id": "webhook-notification-001",
        "strategy_id": "breakout",
        "strategy_version": "v1",
        "venue": "BINANCE",
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "signal_at": now.isoformat(),
        "timeframe": "1h",
        "reference_price": "100",
        "metadata": {"model": "fixture"},
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    timestamp = str(int(now.timestamp()))
    nonce = "notification-webhook-nonce-001"
    signed = timestamp.encode() + b"." + nonce.encode() + b"." + body
    signature = (
        "v1="
        + hmac.new(
            webhook_value.encode(),
            signed,
            hashlib.sha256,
        ).hexdigest()
    )

    event_id, replayed = service.ingest_webhook_signal(
        source_id,
        raw_body=body,
        payload=payload,
        request_timestamp=timestamp,
        nonce=nonce,
        signature=signature,
        idempotency_key="webhook-notification-event-001",
        now=now,
    )
    assert replayed is False

    with database.session_factory() as session:
        delivery = session.scalar(
            select(NotificationDelivery).where(
                NotificationDelivery.team_id == team_id,
                NotificationDelivery.object_type == "SignalEvent",
                NotificationDelivery.object_id == str(event_id),
            )
        )
        assert delivery is not None
        assert delivery.event_type == "SIGNAL_EVENT_RECEIVED"
        assert delivery.status == "PENDING"
        assert delivery.payload["summary"].endswith("不会自动创建提案。")

    replay_id, replayed = service.ingest_webhook_signal(
        source_id,
        raw_body=body,
        payload=payload,
        request_timestamp=timestamp,
        nonce=nonce,
        signature=signature,
        idempotency_key="webhook-notification-event-001",
        now=now,
    )
    assert replay_id == event_id
    assert replayed is True
    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(NotificationDelivery)) == 1
