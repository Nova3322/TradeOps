from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import smtplib
import urllib.error
import urllib.request
from email.message import EmailMessage
from typing import Any

import pytest

from trading_control_plane.domain import DomainRejected
from trading_control_plane.notification import (
    NotificationMessage,
    NotificationTransportError,
    StdlibNotificationSender,
    normalize_notification_event_types,
    notification_template,
    render_notification_message,
    validate_notification_configuration,
    validate_notification_payload,
)
from trading_control_plane.telegram import TelegramUnavailable


@pytest.mark.parametrize(
    ("channel", "configuration", "destination"),
    [
        (
            "TELEGRAM",
            {"bot_token": "12345678901234567890:token", "chat_id": "-100123456789"},
            "chat ••••6789",
        ),
        (
            "SLACK",
            {"webhook_url": "https://hooks.slack.com/services/T/B/secret"},
            "hooks.slack.com",
        ),
        (
            "LARK",
            {
                "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/example",
                "signing_secret": "lark-signing-secret",
            },
            "open.feishu.cn",
        ),
        (
            "EMAIL",
            {
                "smtp_host": "smtp.example.com",
                "smtp_port": "587",
                "username": "mailer",
                "password": "mailer-password",
                "from_address": "ops@example.com",
                "to_address": "team@example.com",
            },
            "te•••@example.com",
        ),
    ],
)
def test_notification_configuration_is_exact_and_projects_only_a_safe_destination(
    channel: str,
    configuration: dict[str, str],
    destination: str,
) -> None:
    normalized, metadata = validate_notification_configuration(channel, configuration)

    assert normalized == configuration
    assert metadata == {
        "channel": channel,
        "destination_hint": destination,
        "configuration_state": "ENCRYPTED",
    }
    assert not any(secret in str(metadata) for secret in configuration.values())


@pytest.mark.parametrize(
    ("channel", "configuration", "code"),
    [
        (
            "SLACK",
            {"webhook_url": "https://example.com/services/T/B/secret"},
            "NOTIFICATION_WEBHOOK_INVALID",
        ),
        (
            "LARK",
            {"webhook_url": "http://open.feishu.cn/open-apis/bot/v2/hook/example"},
            "NOTIFICATION_WEBHOOK_INVALID",
        ),
        (
            "EMAIL",
            {
                "smtp_host": "127.0.0.1",
                "smtp_port": "587",
                "username": "mailer",
                "password": "password",
                "from_address": "ops@example.com",
                "to_address": "team@example.com",
            },
            "NOTIFICATION_SMTP_HOST_INVALID",
        ),
    ],
)
def test_notification_configuration_rejects_unofficial_or_private_destinations(
    channel: str,
    configuration: dict[str, str],
    code: str,
) -> None:
    with pytest.raises(DomainRejected) as rejected:
        validate_notification_configuration(channel, configuration)

    assert rejected.value.code == code


def test_notification_payload_rejects_secrets_non_finite_values_and_excessive_nesting() -> None:
    with pytest.raises(DomainRejected, match="credential") as secret:
        validate_notification_payload({"summary": "bad", "apiKey": "secret"})
    assert secret.value.code == "NOTIFICATION_PAYLOAD_SECRET_FORBIDDEN"

    with pytest.raises(DomainRejected, match="finite"):
        validate_notification_payload({"summary": "bad", "metric": float("nan")})

    with pytest.raises(DomainRejected, match="nesting"):
        validate_notification_payload({"a": {"b": {"c": {"d": {"e": {"f": 1}}}}}})


def test_notification_validation_rejects_unknown_empty_and_oversized_inputs() -> None:
    with pytest.raises(DomainRejected) as unknown_event:
        notification_template("UNKNOWN_EVENT")
    assert unknown_event.value.code == "NOTIFICATION_EVENT_TYPE_INVALID"

    with pytest.raises(DomainRejected) as empty_payload:
        validate_notification_payload({})
    assert empty_payload.value.code == "NOTIFICATION_PAYLOAD_INVALID"

    with pytest.raises(DomainRejected) as oversized_payload:
        validate_notification_payload({"summary": "x" * 17_000})
    assert oversized_payload.value.code == "NOTIFICATION_PAYLOAD_TOO_LARGE"


def test_templates_are_frozen_and_include_the_stable_event_identity() -> None:
    message = render_notification_message(
        event_type="RISK_DECISION_RECORDED",
        template_key="risk.decision-recorded",
        template_version=1,
        payload={"summary": "risk decision", "result": "DENY"},
        event_id="event-001",
    )

    assert message.subject == "[TradingOPS] 风险决策已记录"
    assert "risk decision" in message.text
    assert "result: DENY" in message.text
    assert "event_id: event-001" in message.text
    assert "<pre>" in message.html

    with pytest.raises(DomainRejected) as unavailable:
        render_notification_message(
            event_type="RISK_DECISION_RECORDED",
            template_key="risk.decision-recorded",
            template_version=2,
            payload={"summary": "risk decision"},
            event_id="event-001",
        )
    assert unavailable.value.code == "NOTIFICATION_TEMPLATE_UNAVAILABLE"


def test_proposal_review_template_is_a_formal_card_with_required_facts_and_entry() -> None:
    proposal_id = "848cb77b-2ecb-4ad6-a514-c2719b8a196f"
    message = render_notification_message(
        event_type="PROPOSAL_REVIEW_REQUIRED",
        template_key="proposal.review-required",
        template_version=2,
        payload={
            "summary": "冻结提案已提交, 等待团队成员独立审核。",
            "proposal_id": proposal_id,
            "proposal_version": 2,
            "status": "PENDING_REVIEW",
            "environment": "LIVE",
            "account_id": "acct-1",
            "venue": "BINANCE",
            "symbol": "BTCUSDT",
            "direction": "LONG",
            "risk_tier": "MEDIUM",
            "quantity": "0.001",
            "estimated_notional": "100",
            "quote_currency": "USDT",
            "collateral_currency": "USDT",
            "leverage": "2",
            "max_risk": "1",
            "expires_at": "2026-08-22T12:00:00+00:00",
        },
        event_id="event-review-001",
        public_base_url="https://tradeops.example",
    )

    assert message.subject == "[TradingOPS] 提案等待独立审核"
    for required in (
        "BTCUSDT / 做多 · LONG",
        "acct-1 / BINANCE",
        "环境: LIVE",
        "风险等级: 中 · MEDIUM",
        "0.001 / 100 USDT",
        "杠杆: 2x",
        "2026-08-22T12:00:00+00:00",
        proposal_id,
    ):
        assert required in message.text
    assert "<b>待审核提案</b>" in message.html
    assert "打开 Web 安全审核" not in message.html
    assert "审核入口:" not in message.text
    assert f"https://tradeops.example/proposals/{proposal_id}" not in message.html

    legacy = render_notification_message(
        event_type="PROPOSAL_REVIEW_REQUIRED",
        template_key="proposal.review-required",
        template_version=1,
        payload={"summary": "legacy frozen delivery", "environment": "LIVE"},
        event_id="event-review-legacy",
    )
    assert "legacy frozen delivery" in legacy.text
    assert "<pre>" in legacy.html


def test_lark_signature_matches_the_official_empty_message_hmac_contract() -> None:
    fixture_signing_value = "lark-signing-fixture"
    timestamp = "1786291200"
    expected = base64.b64encode(
        hmac.new(
            f"{timestamp}\n{fixture_signing_value}".encode(),
            digestmod=hashlib.sha256,
        ).digest()
    ).decode()

    assert StdlibNotificationSender._lark_signature(fixture_signing_value, timestamp) == expected


def test_integrated_team_events_can_be_selected_for_routes() -> None:
    assert normalize_notification_event_types(
        ["signal_event_received", "PROPOSAL_REVIEW_REQUIRED", "CAPITAL_STATUS_CHANGED"]
    ) == [
        "CAPITAL_STATUS_CHANGED",
        "PROPOSAL_REVIEW_REQUIRED",
        "SIGNAL_EVENT_RECEIVED",
    ]

    with pytest.raises(DomainRejected) as blocked:
        normalize_notification_event_types(["UNKNOWN_EVENT"])
    assert blocked.value.code == "NOTIFICATION_EVENT_TYPES_INVALID"


class _HttpResponse:
    def __init__(self, *, status: int = 200, body: bytes = b"") -> None:
        self.status = status
        self.body = body

    def __enter__(self) -> _HttpResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return self.body


def test_webhook_sender_maps_rate_limit_and_unknown_network_outcomes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rate_limited = urllib.error.HTTPError(
        "https://hooks.slack.com/services/T/B/fixture",
        429,
        "rate limited",
        {"Retry-After": "75"},
        io.BytesIO(),
    )
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(rate_limited),
    )

    with pytest.raises(NotificationTransportError) as retry:
        StdlibNotificationSender().send(
            "SLACK",
            {"webhook_url": "https://hooks.slack.com/services/T/B/fixture"},
            NotificationMessage("subject", "text", "<p>text</p>"),
        )
    assert retry.value.code == "NOTIFICATION_RATE_LIMITED"
    assert retry.value.retryable is True
    assert retry.value.retry_after_seconds == 75

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(urllib.error.URLError("timeout")),
    )
    with pytest.raises(NotificationTransportError) as unknown:
        StdlibNotificationSender().send(
            "SLACK",
            {"webhook_url": "https://hooks.slack.com/services/T/B/fixture"},
            NotificationMessage("subject", "text", "<p>text</p>"),
        )
    assert unknown.value.code == "NOTIFICATION_OUTCOME_UNKNOWN"
    assert unknown.value.outcome_unknown is True
    assert unknown.value.retryable is False


def test_lark_sender_freezes_official_payload_and_rejects_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, Any] = {}

    def urlopen(request: urllib.request.Request, *, timeout: int) -> _HttpResponse:
        observed["url"] = request.full_url
        observed["timeout"] = timeout
        observed["payload"] = json.loads(request.data or b"{}")
        return _HttpResponse(body=b'{"code":0}')

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr("trading_control_plane.notification.time.time", lambda: 1_786_291_200)
    sender = StdlibNotificationSender()
    sender.send(
        "LARK",
        {
            "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/fixture",
            "signing_secret": "lark-signing-fixture",
        },
        NotificationMessage("subject", "message text", "<p>message text</p>"),
    )

    assert observed["url"] == "https://open.feishu.cn/open-apis/bot/v2/hook/fixture"
    assert observed["timeout"] == 10
    assert observed["payload"] == {
        "msg_type": "text",
        "content": {"text": "message text"},
        "timestamp": "1786291200",
        "sign": StdlibNotificationSender._lark_signature("lark-signing-fixture", "1786291200"),
    }

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _HttpResponse(body=b'{"code":19024}'),
    )
    with pytest.raises(NotificationTransportError) as rejected:
        sender.send(
            "LARK",
            {"webhook_url": "https://open.larksuite.com/open-apis/bot/v2/hook/fixture"},
            NotificationMessage("subject", "text", "<p>text</p>"),
        )
    assert rejected.value.code == "NOTIFICATION_LARK_REJECTED"


@pytest.mark.parametrize("smtp_port", ["465", "587"])
def test_email_sender_requires_tls_and_maps_authentication_failure(
    monkeypatch: pytest.MonkeyPatch,
    smtp_port: str,
) -> None:
    calls: list[str] = []

    class FakeSmtp:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            calls.append("connect")

        def __enter__(self) -> FakeSmtp:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def starttls(self, **_kwargs: object) -> None:
            calls.append("starttls")

        def login(self, username: str, password: str) -> None:
            calls.append(f"login:{username}:{password}")

        def send_message(self, message: EmailMessage) -> None:
            calls.append(f"send:{message['To']}")

    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSmtp)
    monkeypatch.setattr(smtplib, "SMTP", FakeSmtp)
    configuration = {
        "smtp_host": "smtp.example.com",
        "smtp_port": smtp_port,
        "username": "mailer",
        "password": "fixture-password",
        "from_address": "ops@example.com",
        "to_address": "team@example.com",
    }
    StdlibNotificationSender(email_smtp_allowed_hosts=("smtp.example.com",)).send(
        "EMAIL",
        configuration,
        NotificationMessage("subject", "text", "<p>text</p>"),
    )

    assert calls[0] == "connect"
    assert ("starttls" in calls) is (smtp_port == "587")
    assert calls[-1] == "send:team@example.com"

    class RejectedSmtp(FakeSmtp):
        def login(self, _username: str, _password: str) -> None:
            raise smtplib.SMTPAuthenticationError(535, b"invalid")

    monkeypatch.setattr(smtplib, "SMTP_SSL", RejectedSmtp)
    monkeypatch.setattr(smtplib, "SMTP", RejectedSmtp)
    with pytest.raises(NotificationTransportError) as rejected:
        StdlibNotificationSender(email_smtp_allowed_hosts=("smtp.example.com",)).send(
            "EMAIL",
            configuration,
            NotificationMessage("subject", "text", "<p>text</p>"),
        )
    assert rejected.value.code == "NOTIFICATION_EMAIL_AUTH_FAILED"


def test_email_sender_rejects_unallowlisted_smtp_host_before_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        smtplib,
        "SMTP_SSL",
        lambda *_args, **_kwargs: pytest.fail("SMTP must not open for an unallowlisted host"),
    )
    configuration = {
        "smtp_host": "smtp.example.com",
        "smtp_port": "465",
        "username": "mailer",
        "password": "fixture-password",
        "from_address": "ops@example.com",
        "to_address": "team@example.com",
    }

    with pytest.raises(NotificationTransportError) as rejected:
        StdlibNotificationSender().send(
            "EMAIL",
            configuration,
            NotificationMessage("subject", "text", "<p>text</p>"),
        )

    assert rejected.value.code == "NOTIFICATION_SMTP_HOST_NOT_ALLOWED"
    assert rejected.value.retryable is False


def test_telegram_sender_preserves_external_identity_and_classifies_network_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def call(_client: object, method: str, payload: dict[str, object]) -> dict[str, Any]:
        calls.append((method, payload))
        return {"result": {"message_id": 42}}

    monkeypatch.setattr("trading_control_plane.notification.TelegramBotClient.call", call)
    result = StdlibNotificationSender().send(
        "TELEGRAM",
        {"bot_token": "12345678901234567890:token", "chat_id": "-100123456789"},
        NotificationMessage("subject", "text", "<pre>text</pre>"),
    )

    assert result.external_delivery_id == "42"
    assert calls == [
        (
            "sendMessage",
            {
                "chat_id": "-100123456789",
                "text": "<pre>text</pre>",
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )
    ]

    def unavailable(*_args: object, **_kwargs: object) -> dict[str, Any]:
        raise TelegramUnavailable(
            "network unavailable",
            code="TELEGRAM_NETWORK_UNAVAILABLE",
        )

    monkeypatch.setattr("trading_control_plane.notification.TelegramBotClient.call", unavailable)
    with pytest.raises(NotificationTransportError) as unknown:
        StdlibNotificationSender().send(
            "TELEGRAM",
            {"bot_token": "12345678901234567890:token", "chat_id": "-100123456789"},
            NotificationMessage("subject", "text", "<pre>text</pre>"),
        )
    assert unknown.value.outcome_unknown is True
