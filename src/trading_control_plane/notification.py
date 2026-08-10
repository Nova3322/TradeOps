from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
import smtplib
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Any, Protocol
from urllib.parse import urlparse
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from trading_control_plane.credentials import CredentialCipher
from trading_control_plane.database import Database
from trading_control_plane.domain import DomainRejected
from trading_control_plane.models import AuditEvent, NotificationDelivery, NotificationRoute, Team
from trading_control_plane.telegram import TelegramBotClient, TelegramUnavailable

SUPPORTED_NOTIFICATION_CHANNELS = frozenset({"TELEGRAM", "SLACK", "LARK", "EMAIL"})
ROUTABLE_NOTIFICATION_EVENT_TYPES = frozenset(
    {
        "PROPOSAL_REVIEW_REQUIRED",
        "RISK_DECISION_RECORDED",
        "CAMPAIGN_STATUS_CHANGED",
        "CAPITAL_STATUS_CHANGED",
        "SIGNAL_EVENT_RECEIVED",
        "CONNECTION_CHECK_FAILED",
    }
)

_SENSITIVE_PAYLOAD_KEYS = frozenset(
    {
        "api_key",
        "api_secret",
        "authorization",
        "bot_token",
        "credential",
        "credentials",
        "password",
        "private_key",
        "secret",
        "signature",
        "token",
        "webhook_url",
    }
)
_COMPACT_SENSITIVE_PAYLOAD_KEYS = frozenset(
    item.replace("_", "") for item in _SENSITIVE_PAYLOAD_KEYS
)
_SMTP_HOST_PATTERN = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z]{2,63}$"
)
_EMAIL_PATTERN = re.compile(r"^[^\s@]{1,64}@[^\s@]{1,190}$")


@dataclass(frozen=True, slots=True)
class NotificationTemplate:
    event_type: str
    key: str
    version: int
    title: str


@dataclass(frozen=True, slots=True)
class NotificationMessage:
    subject: str
    text: str
    html: str


@dataclass(frozen=True, slots=True)
class NotificationSendResult:
    external_delivery_id: str | None = None


class NotificationTransportError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        retryable: bool,
        outcome_unknown: bool = False,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.outcome_unknown = outcome_unknown
        self.retry_after_seconds = retry_after_seconds


class NotificationSender(Protocol):
    def send(
        self,
        channel: str,
        configuration: dict[str, str],
        message: NotificationMessage,
    ) -> NotificationSendResult: ...


NOTIFICATION_TEMPLATES = {
    item.event_type: item
    for item in (
        NotificationTemplate(
            "PROPOSAL_REVIEW_REQUIRED",
            "proposal.review-required",
            1,
            "提案等待独立审核",
        ),
        NotificationTemplate(
            "RISK_DECISION_RECORDED",
            "risk.decision-recorded",
            1,
            "风险决策已记录",
        ),
        NotificationTemplate(
            "CAMPAIGN_STATUS_CHANGED",
            "campaign.status-changed",
            1,
            "交易任务状态变化",
        ),
        NotificationTemplate(
            "CAPITAL_STATUS_CHANGED",
            "capital.status-changed",
            1,
            "资金流程状态变化",
        ),
        NotificationTemplate(
            "SIGNAL_EVENT_RECEIVED",
            "signal.event-received",
            1,
            "团队收到新信号",
        ),
        NotificationTemplate(
            "CONNECTION_CHECK_FAILED",
            "venue.connection-check-failed",
            1,
            "交易账户连接验证失败",
        ),
        NotificationTemplate(
            "TEST_NOTIFICATION",
            "notification.test",
            1,
            "通知渠道测试",
        ),
    )
}


def notification_template(event_type: str) -> NotificationTemplate:
    template = NOTIFICATION_TEMPLATES.get(event_type)
    if template is None:
        raise DomainRejected(
            "NOTIFICATION_EVENT_TYPE_INVALID",
            "notification event type is not supported",
        )
    return template


def normalize_notification_event_types(event_types: list[str]) -> list[str]:
    normalized = sorted({item.strip().upper() for item in event_types if item.strip()})
    if not normalized or any(item not in ROUTABLE_NOTIFICATION_EVENT_TYPES for item in normalized):
        raise DomainRejected(
            "NOTIFICATION_EVENT_TYPES_INVALID",
            "notification routes require one or more supported event types",
        )
    return normalized


def validate_notification_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict) or not payload:
        raise DomainRejected(
            "NOTIFICATION_PAYLOAD_INVALID",
            "notification payload must be a non-empty object",
        )

    def inspect_value(value: Any, *, depth: int = 0) -> None:
        if depth > 4:
            raise DomainRejected(
                "NOTIFICATION_PAYLOAD_INVALID",
                "notification payload nesting is too deep",
            )
        if isinstance(value, float) and not math.isfinite(value):
            raise DomainRejected(
                "NOTIFICATION_PAYLOAD_INVALID",
                "notification payload numbers must be finite",
            )
        if value is None or isinstance(value, bool | int | float | str):
            return
        if isinstance(value, list):
            for item in value:
                inspect_value(item, depth=depth + 1)
            return
        if isinstance(value, dict):
            for key, item in value.items():
                normalized_key = key.casefold().replace("-", "_") if isinstance(key, str) else ""
                compact_key = normalized_key.replace("_", "")
                if (
                    not isinstance(key, str)
                    or normalized_key in _SENSITIVE_PAYLOAD_KEYS
                    or compact_key in _COMPACT_SENSITIVE_PAYLOAD_KEYS
                ):
                    raise DomainRejected(
                        "NOTIFICATION_PAYLOAD_SECRET_FORBIDDEN",
                        "notification payload must not contain credential fields",
                    )
                inspect_value(item, depth=depth + 1)
            return
        raise DomainRejected(
            "NOTIFICATION_PAYLOAD_INVALID",
            "notification payload contains an unsupported value",
        )

    inspect_value(payload)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    if len(canonical.encode()) > 16_384:
        raise DomainRejected(
            "NOTIFICATION_PAYLOAD_TOO_LARGE",
            "notification payload exceeds 16 KiB",
        )
    decoded = json.loads(canonical)
    if not isinstance(decoded, dict):
        raise RuntimeError("validated notification payload did not decode to an object")
    return decoded


def _official_webhook_url(value: str, *, channel: str) -> str:
    parsed = urlparse(value.strip())
    allowed_hosts = {
        "SLACK": {"hooks.slack.com"},
        "LARK": {"open.feishu.cn", "open.larksuite.com"},
    }[channel]
    if (
        parsed.scheme != "https"
        or parsed.hostname not in allowed_hosts
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise DomainRejected(
            "NOTIFICATION_WEBHOOK_INVALID",
            f"{channel} requires an official HTTPS webhook URL",
        )
    return value.strip()


def validate_notification_configuration(
    channel: str,
    configuration: dict[str, str],
) -> tuple[dict[str, str], dict[str, Any]]:
    normalized_channel = channel.strip().upper()
    if normalized_channel not in SUPPORTED_NOTIFICATION_CHANNELS:
        raise DomainRejected(
            "NOTIFICATION_CHANNEL_INVALID",
            "notification channel is not supported",
        )
    clean = {
        key: value.strip()
        for key, value in configuration.items()
        if isinstance(key, str) and isinstance(value, str) and value.strip()
    }
    if len(clean) != len(configuration):
        raise DomainRejected(
            "NOTIFICATION_CONFIGURATION_INVALID",
            "notification configuration fields must be non-empty strings",
        )
    required = {
        "TELEGRAM": {"bot_token", "chat_id"},
        "SLACK": {"webhook_url"},
        "LARK": {"webhook_url"},
        "EMAIL": {
            "smtp_host",
            "smtp_port",
            "username",
            "password",
            "from_address",
            "to_address",
        },
    }[normalized_channel]
    allowed = required | ({"signing_secret"} if normalized_channel == "LARK" else set())
    if set(clean) != required and not (normalized_channel == "LARK" and set(clean) == allowed):
        raise DomainRejected(
            "NOTIFICATION_CONFIGURATION_INVALID",
            f"{normalized_channel} notification configuration fields are incomplete",
        )
    if any(len(value) > 2_048 for value in clean.values()):
        raise DomainRejected(
            "NOTIFICATION_CONFIGURATION_INVALID",
            "notification configuration field is too long",
        )
    if normalized_channel == "TELEGRAM":
        if len(clean["bot_token"]) < 20 or len(clean["chat_id"]) > 120:
            raise DomainRejected(
                "NOTIFICATION_CONFIGURATION_INVALID",
                "Telegram token or chat identity is invalid",
            )
        destination = f"chat ••••{clean['chat_id'][-4:]}"
    elif normalized_channel in {"SLACK", "LARK"}:
        clean["webhook_url"] = _official_webhook_url(
            clean["webhook_url"], channel=normalized_channel
        )
        destination = urlparse(clean["webhook_url"]).hostname or "official webhook"
    else:
        if not _SMTP_HOST_PATTERN.fullmatch(clean["smtp_host"]):
            raise DomainRejected(
                "NOTIFICATION_SMTP_HOST_INVALID",
                "email SMTP host must be a public DNS hostname",
            )
        if clean["smtp_port"] not in {"465", "587"}:
            raise DomainRejected(
                "NOTIFICATION_SMTP_PORT_INVALID",
                "email SMTP port must be 465 or 587",
            )
        if not _EMAIL_PATTERN.fullmatch(clean["from_address"]) or not _EMAIL_PATTERN.fullmatch(
            clean["to_address"]
        ):
            raise DomainRejected(
                "NOTIFICATION_EMAIL_ADDRESS_INVALID",
                "email route requires valid sender and recipient addresses",
            )
        local, _, domain = clean["to_address"].partition("@")
        destination = f"{local[:2]}•••@{domain}"
    metadata = {
        "channel": normalized_channel,
        "destination_hint": destination,
        "configuration_state": "ENCRYPTED",
    }
    return clean, metadata


def render_notification_message(
    *,
    event_type: str,
    template_key: str,
    template_version: int,
    payload: dict[str, Any],
    event_id: str,
) -> NotificationMessage:
    template = notification_template(event_type)
    if template.key != template_key or template.version != template_version:
        raise DomainRejected(
            "NOTIFICATION_TEMPLATE_UNAVAILABLE",
            "frozen notification template is unavailable",
        )
    normalized = validate_notification_payload(payload)
    summary = str(normalized.get("summary") or "团队事件已记录, 请打开 Trading 控制台查看。")
    facts = [
        (str(key), str(value))
        for key, value in normalized.items()
        if key != "summary" and value is not None
    ][:12]
    lines = [template.title, summary]
    lines.extend(f"{key}: {value}" for key, value in facts)
    lines.append(f"event_id: {event_id}")
    text = "\n".join(lines)
    escaped = (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )
    return NotificationMessage(
        subject=f"[TradingOPS] {template.title}",
        text=text,
        html=f"<pre>{escaped}</pre>",
    )


class StdlibNotificationSender:
    """Narrow outbound adapters. They expose no trading, funding, signing, or broadcast API."""

    @staticmethod
    def _post_json(url: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        request = urllib.request.Request(  # noqa: S310
            url,
            data=json.dumps(payload, separators=(",", ":")).encode(),
            headers={"content-type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
                raw = response.read(64 * 1024)
                status = response.status
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                retry_after = exc.headers.get("Retry-After")
                retry_after_seconds = None
                if retry_after is not None and retry_after.isdigit():
                    retry_after_seconds = min(max(int(retry_after), 1), 3_600)
                raise NotificationTransportError(
                    "NOTIFICATION_RATE_LIMITED",
                    retryable=True,
                    retry_after_seconds=retry_after_seconds,
                ) from None
            raise NotificationTransportError(
                "NOTIFICATION_WEBHOOK_REJECTED",
                retryable=False,
            ) from None
        except (TimeoutError, urllib.error.URLError):
            raise NotificationTransportError(
                "NOTIFICATION_OUTCOME_UNKNOWN",
                retryable=False,
                outcome_unknown=True,
            ) from None
        if status < 200 or status >= 300:
            raise NotificationTransportError(
                "NOTIFICATION_WEBHOOK_REJECTED",
                retryable=False,
            )
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _lark_signature(secret: str, timestamp: str) -> str:
        sign_source = f"{timestamp}\n{secret}".encode()
        return base64.b64encode(hmac.new(sign_source, digestmod=hashlib.sha256).digest()).decode()

    def send(
        self,
        channel: str,
        configuration: dict[str, str],
        message: NotificationMessage,
    ) -> NotificationSendResult:
        channel = channel.strip().upper()
        normalized, _ = validate_notification_configuration(channel, configuration)
        if channel == "TELEGRAM":
            try:
                result = TelegramBotClient(normalized["bot_token"]).call(
                    "sendMessage",
                    {
                        "chat_id": normalized["chat_id"],
                        "text": message.html,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                )
            except TelegramUnavailable as exc:
                retryable = exc.code == "TELEGRAM_RATE_LIMITED"
                unknown = exc.code == "TELEGRAM_NETWORK_UNAVAILABLE"
                raise NotificationTransportError(
                    exc.code,
                    retryable=retryable,
                    outcome_unknown=unknown,
                ) from None
            message_id = result.get("result", {}).get("message_id")
            return NotificationSendResult(None if message_id is None else str(message_id))
        if channel == "SLACK":
            self._post_json(normalized["webhook_url"], {"text": message.text})
            return NotificationSendResult()
        if channel == "LARK":
            payload: dict[str, Any] = {
                "msg_type": "text",
                "content": {"text": message.text},
            }
            if secret := normalized.get("signing_secret"):
                timestamp = str(int(time.time()))
                payload.update(
                    timestamp=timestamp,
                    sign=self._lark_signature(secret, timestamp),
                )
            response = self._post_json(normalized["webhook_url"], payload)
            response_code = (
                0 if response is None else response.get("code", response.get("StatusCode", 0))
            )
            if str(response_code) != "0":
                raise NotificationTransportError(
                    "NOTIFICATION_LARK_REJECTED",
                    retryable=False,
                )
            return NotificationSendResult()

        email = EmailMessage()
        email["Subject"] = message.subject
        email["From"] = normalized["from_address"]
        email["To"] = normalized["to_address"]
        email.set_content(message.text)
        email.add_alternative(message.html, subtype="html")
        context = ssl.create_default_context()
        try:
            if normalized["smtp_port"] == "465":
                with smtplib.SMTP_SSL(
                    normalized["smtp_host"], 465, timeout=10, context=context
                ) as client:
                    client.login(normalized["username"], normalized["password"])
                    client.send_message(email)
            else:
                with smtplib.SMTP(normalized["smtp_host"], 587, timeout=10) as client:
                    client.starttls(context=context)
                    client.login(normalized["username"], normalized["password"])
                    client.send_message(email)
        except smtplib.SMTPAuthenticationError:
            raise NotificationTransportError(
                "NOTIFICATION_EMAIL_AUTH_FAILED",
                retryable=False,
            ) from None
        except smtplib.SMTPRecipientsRefused:
            raise NotificationTransportError(
                "NOTIFICATION_EMAIL_RECIPIENT_REJECTED",
                retryable=False,
            ) from None
        except (OSError, smtplib.SMTPException):
            raise NotificationTransportError(
                "NOTIFICATION_EMAIL_OUTCOME_UNKNOWN",
                retryable=False,
                outcome_unknown=True,
            ) from None
        return NotificationSendResult()


@dataclass(frozen=True, slots=True)
class _PreparedDelivery:
    notification_delivery_id: UUID
    attempt_count: int
    channel: str
    configuration: dict[str, str]
    event_type: str
    template_key: str
    template_version: int
    payload: dict[str, Any]
    event_id: UUID


class NotificationDispatcher:
    """Claim, send, and finalize durable notification deliveries without business authority."""

    def __init__(
        self,
        database: Database,
        *,
        credential_encryption_key: str | None,
        sender: NotificationSender | None = None,
    ) -> None:
        self.database = database
        self.cipher = CredentialCipher(credential_encryption_key)
        self.sender = sender or StdlibNotificationSender()

    @staticmethod
    def _audit_delivery(
        session: Session,
        delivery: NotificationDelivery,
        *,
        event_type: str,
        reason: str,
        now: datetime,
    ) -> None:
        team = session.get(Team, delivery.team_id)
        session.add(
            AuditEvent(
                workspace_id=None if team is None else team.workspace_id,
                team_id=delivery.team_id,
                account_id=delivery.account_id,
                actor_id="notification-worker",
                event_type=event_type,
                object_type="NotificationDelivery",
                object_id=str(delivery.notification_delivery_id),
                reason=reason,
                correlation_id=delivery.correlation_id,
                idempotency_key=delivery.idempotency_key,
                object_version=delivery.attempt_count,
                created_at=now,
            )
        )

    def _recover_stale_claims(self, *, now: datetime) -> int:
        cutoff = now - timedelta(minutes=5)
        recovered = 0
        with self.database.session_factory.begin() as session:
            deliveries = session.scalars(
                select(NotificationDelivery)
                .where(
                    NotificationDelivery.status == "SENDING",
                    NotificationDelivery.last_attempt_at < cutoff,
                )
                .with_for_update(skip_locked=True)
            ).all()
            for delivery in deliveries:
                delivery.status = "OUTCOME_UNKNOWN"
                delivery.last_error_code = "NOTIFICATION_WORKER_INTERRUPTED"
                delivery.updated_at = now
                self._audit_delivery(
                    session,
                    delivery,
                    event_type="NOTIFICATION_OUTCOME_UNKNOWN",
                    reason="error_code=NOTIFICATION_WORKER_INTERRUPTED;automatic_retry=false",
                    now=now,
                )
                recovered += 1
        return recovered

    def due_delivery_ids(self, *, now: datetime, limit: int = 50) -> list[UUID]:
        bounded_limit = min(max(limit, 1), 200)
        with self.database.session_factory() as session:
            return list(
                session.scalars(
                    select(NotificationDelivery.notification_delivery_id)
                    .where(
                        NotificationDelivery.status.in_(["PENDING", "RETRY_WAIT"]),
                        NotificationDelivery.next_attempt_at <= now,
                    )
                    .order_by(
                        NotificationDelivery.next_attempt_at,
                        NotificationDelivery.created_at,
                    )
                    .limit(bounded_limit)
                ).all()
            )

    def _claim(
        self,
        delivery_id: UUID,
        *,
        now: datetime,
    ) -> _PreparedDelivery | str | None:
        with self.database.session_factory.begin() as session:
            delivery = session.scalar(
                select(NotificationDelivery)
                .where(NotificationDelivery.notification_delivery_id == delivery_id)
                .with_for_update(skip_locked=True)
            )
            if (
                delivery is None
                or delivery.status not in {"PENDING", "RETRY_WAIT"}
                or delivery.next_attempt_at > now
            ):
                return None
            route = session.scalar(
                select(NotificationRoute)
                .where(
                    NotificationRoute.notification_route_id == delivery.notification_route_id,
                    NotificationRoute.team_id == delivery.team_id,
                )
                .with_for_update()
            )
            cancellation_code = None
            if route is None:
                cancellation_code = "NOTIFICATION_ROUTE_MISSING"
            elif not route.enabled:
                cancellation_code = "NOTIFICATION_ROUTE_DISABLED"
            elif route.version != delivery.route_version or route.channel != delivery.channel:
                cancellation_code = "NOTIFICATION_ROUTE_CHANGED"
            if cancellation_code is not None:
                delivery.status = "CANCELLED"
                delivery.last_error_code = cancellation_code
                delivery.updated_at = now
                self._audit_delivery(
                    session,
                    delivery,
                    event_type="NOTIFICATION_CANCELLED",
                    reason=f"error_code={cancellation_code}",
                    now=now,
                )
                return delivery.status
            assert route is not None
            try:
                raw_configuration = self.cipher.decrypt_secret(
                    route.configuration_ciphertext,
                    team_id=route.team_id,
                    object_id=route.notification_route_id,
                    purpose=f"notification-route:{route.channel.lower()}",
                    credential_version=route.credential_version,
                )
                decoded = json.loads(raw_configuration)
                if not isinstance(decoded, dict) or not all(
                    isinstance(key, str) and isinstance(value, str)
                    for key, value in decoded.items()
                ):
                    raise ValueError("invalid configuration")
                configuration, _metadata = validate_notification_configuration(
                    route.channel,
                    decoded,
                )
            except (DomainRejected, json.JSONDecodeError, ValueError):
                delivery.status = "DEAD_LETTER"
                delivery.last_error_code = "NOTIFICATION_CONFIGURATION_UNAVAILABLE"
                delivery.updated_at = now
                self._audit_delivery(
                    session,
                    delivery,
                    event_type="NOTIFICATION_FAILED",
                    reason=("error_code=NOTIFICATION_CONFIGURATION_UNAVAILABLE;retryable=false"),
                    now=now,
                )
                return delivery.status
            delivery.status = "SENDING"
            delivery.attempt_count += 1
            delivery.last_attempt_at = now
            delivery.last_error_code = None
            delivery.updated_at = now
            return _PreparedDelivery(
                notification_delivery_id=delivery.notification_delivery_id,
                attempt_count=delivery.attempt_count,
                channel=delivery.channel,
                configuration=configuration,
                event_type=delivery.event_type,
                template_key=delivery.template_key,
                template_version=delivery.template_version,
                payload=delivery.payload,
                event_id=delivery.notification_event_id,
            )

    def _finalize_success(
        self,
        prepared: _PreparedDelivery,
        result: NotificationSendResult,
        *,
        now: datetime,
    ) -> str:
        with self.database.session_factory.begin() as session:
            delivery = session.scalar(
                select(NotificationDelivery)
                .where(
                    NotificationDelivery.notification_delivery_id
                    == prepared.notification_delivery_id
                )
                .with_for_update()
            )
            if (
                delivery is None
                or delivery.status != "SENDING"
                or delivery.attempt_count != prepared.attempt_count
            ):
                raise DomainRejected(
                    "NOTIFICATION_DELIVERY_FENCE_REJECTED",
                    "notification delivery changed before send result",
                )
            delivery.status = "SENT"
            delivery.sent_at = now
            delivery.external_delivery_id = result.external_delivery_id
            delivery.last_error_code = None
            delivery.updated_at = now
            self._audit_delivery(
                session,
                delivery,
                event_type="NOTIFICATION_SENT",
                reason=f"channel={delivery.channel};attempt={delivery.attempt_count}",
                now=now,
            )
            return delivery.status

    def _finalize_failure(
        self,
        prepared: _PreparedDelivery,
        error: NotificationTransportError,
        *,
        now: datetime,
    ) -> str:
        with self.database.session_factory.begin() as session:
            delivery = session.scalar(
                select(NotificationDelivery)
                .where(
                    NotificationDelivery.notification_delivery_id
                    == prepared.notification_delivery_id
                )
                .with_for_update()
            )
            if (
                delivery is None
                or delivery.status != "SENDING"
                or delivery.attempt_count != prepared.attempt_count
            ):
                raise DomainRejected(
                    "NOTIFICATION_DELIVERY_FENCE_REJECTED",
                    "notification delivery changed before failure result",
                )
            if error.outcome_unknown:
                delivery.status = "OUTCOME_UNKNOWN"
                event_type = "NOTIFICATION_OUTCOME_UNKNOWN"
            elif error.retryable and delivery.attempt_count < delivery.max_attempts:
                delivery.status = "RETRY_WAIT"
                delay_seconds = error.retry_after_seconds or min(
                    30 * (2 ** (delivery.attempt_count - 1)),
                    3_600,
                )
                delivery.next_attempt_at = now + timedelta(seconds=delay_seconds)
                event_type = "NOTIFICATION_RETRY_SCHEDULED"
            else:
                delivery.status = "DEAD_LETTER"
                event_type = "NOTIFICATION_FAILED"
            delivery.last_error_code = error.code
            delivery.updated_at = now
            self._audit_delivery(
                session,
                delivery,
                event_type=event_type,
                reason=(
                    f"error_code={error.code};attempt={delivery.attempt_count};"
                    f"retryable={str(error.retryable).lower()};"
                    f"outcome_unknown={str(error.outcome_unknown).lower()}"
                ),
                now=now,
            )
            return delivery.status

    def dispatch_one(self, delivery_id: UUID, *, now: datetime) -> str:
        prepared = self._claim(delivery_id, now=now)
        if prepared is None:
            return "SKIPPED"
        if isinstance(prepared, str):
            return prepared
        try:
            message = render_notification_message(
                event_type=prepared.event_type,
                template_key=prepared.template_key,
                template_version=prepared.template_version,
                payload=prepared.payload,
                event_id=str(prepared.event_id),
            )
            result = self.sender.send(
                prepared.channel,
                prepared.configuration,
                message,
            )
        except NotificationTransportError as exc:
            return self._finalize_failure(prepared, exc, now=now)
        except Exception:
            return self._finalize_failure(
                prepared,
                NotificationTransportError(
                    "NOTIFICATION_OUTCOME_UNKNOWN",
                    retryable=False,
                    outcome_unknown=True,
                ),
                now=now,
            )
        return self._finalize_success(prepared, result, now=now)

    def dispatch_due(self, *, now: datetime, limit: int = 50) -> dict[str, Any]:
        recovered_unknown = self._recover_stale_claims(now=now)
        results: dict[str, int] = {}
        delivery_ids = self.due_delivery_ids(now=now, limit=limit)
        for delivery_id in delivery_ids:
            status = self.dispatch_one(delivery_id, now=now)
            results[status] = results.get(status, 0) + 1
        return {
            "selected": len(delivery_ids),
            "results": results,
            "recovered_unknown": recovered_unknown,
        }
