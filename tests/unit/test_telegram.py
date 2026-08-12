from datetime import UTC, datetime
from io import BytesIO
from threading import Event
from typing import Any
from urllib.error import HTTPError, URLError
from uuid import UUID, uuid4

import pytest

from trading_control_plane.telegram import (
    MAX_TELEGRAM_TEXT,
    CampaignNotification,
    CapitalNotification,
    ProposalNotification,
    TelegramBotClient,
    TelegramBotGateway,
    TelegramProposalReviewAction,
    TelegramUnavailable,
    _default_poster,
    render_help,
    render_proposal_notification,
    render_status,
)


class FakeBotApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_send = False

    def poster(self, url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        del timeout
        method = url.rsplit("/", maxsplit=1)[-1]
        self.calls.append((method, payload))
        if self.fail_send and method == "sendMessage":
            raise TelegramUnavailable("offline")
        return {"ok": True, "result": []}


def proposal(recipient_id: UUID, *, status: str = "PENDING_REVIEW") -> ProposalNotification:
    return ProposalNotification(
        notification_id="proposal-notification",
        reviewer_id=recipient_id,
        proposal_id=uuid4(),
        proposal_version=2,
        environment="LIVE",
        summary="frozen proposal awaiting independent review",
        review_code="review-reference",
        review_url="http://127.0.0.1:8014/proposals/1",
        created_at=datetime.now(UTC),
        status=status,
        expires_at="2026-08-04T20:00:00+00:00",
        symbol="BTCUSDT",
        direction="LONG",
        risk_tier="MEDIUM",
        quantity="0.001",
        max_risk="1",
    )


def gateway(
    fake: FakeBotApi,
    *,
    todo_items: list[ProposalNotification] | None = None,
) -> tuple[TelegramBotGateway, dict[str, str], list[object], UUID]:
    token = "123456789:abcdefghijklmnopqrstuvwxyz"  # noqa: S105
    bindings: dict[str, str] = {}
    handled: list[object] = []
    recipient_id = uuid4()

    def bind(chat_id: str, telegram_username: str, internal_username: str) -> str:
        assert telegram_username == "telegram-owner"
        assert internal_username == "telegram-owner"
        bindings[str(recipient_id)] = chat_id
        return internal_username

    instance = TelegramBotGateway(
        token=token,
        allowed_username="@telegram-owner",
        internal_username="telegram-owner",
        binder=bind,
        chat_resolver=lambda user_id: bindings.get(str(user_id)),
        todo_resolver=lambda chat_id: (
            (todo_items if todo_items is not None else [proposal(recipient_id)])
            if chat_id == "789"
            else []
        ),
        review_queue_url="http://127.0.0.1:8014/reviews",
        client=TelegramBotClient(
            token,
            base_url="https://telegram.invalid",
            poster=fake.poster,
        ),
    )
    instance.set_action_handler(
        lambda action, update_id: handled.extend([action, update_id]) or "review recorded"
    )
    return instance, bindings, handled, recipient_id


def bind(bot: TelegramBotGateway) -> None:
    bot.handle_update(
        {
            "update_id": 1,
            "message": {
                "text": "/start",
                "from": {"id": 789, "username": "telegram-owner"},
                "chat": {"id": 789, "type": "private"},
            },
        }
    )


def callback(callback_id: str, key: str, *, sender_id: int = 789) -> dict[str, Any]:
    return {
        "id": callback_id,
        "data": key,
        "from": {"id": sender_id},
        "message": {"message_id": 11, "chat": {"id": 789, "type": "private"}},
    }


def test_bot_requires_two_clicks_and_exposes_only_proposal_review() -> None:
    fake = FakeBotApi()
    bot, _bindings, handled, recipient_id = gateway(fake)
    bind(bot)
    notification = proposal(recipient_id)
    bot.send(notification)

    keyboard = fake.calls[-1][1]["reply_markup"]["inline_keyboard"]
    assert [row[0]["text"] for row in keyboard] == [
        "需确认 · 批准",
        "需确认 · 拒绝",
        "查看完整冻结快照",
    ]
    assert "BTCUSDT" in fake.calls[-1][1]["text"]
    assert "2026年8月4日 20:00 UTC" in fake.calls[-1][1]["text"]
    assert "2026-08-04T20:00:00+00:00" not in fake.calls[-1][1]["text"]
    assert "资金" not in " ".join(row[0]["text"] for row in keyboard)

    bot.send_campaign(
        CampaignNotification(
            notification_id="campaign",
            recipient_id=recipient_id,
            campaign_id=uuid4(),
            event_type="POSITION_UPDATED",
            environment="LIVE",
            summary="must stay outside review bot",
            campaign_version=1,
            action_references=(("EXIT", "must-not-be-exposed"),),
            created_at=datetime.now(UTC),
        )
    )
    bot.send_capital(
        CapitalNotification(
            notification_id="capital",
            recipient_id=recipient_id,
            object_id=uuid4(),
            object_type="CapitalTransfer",
            event_type="IN_TRANSIT",
            environment="LIVE",
            summary="must stay outside review bot",
            object_version=1,
            created_at=datetime.now(UTC),
        )
    )
    assert [method for method, _payload in fake.calls].count("sendMessage") == 2
    assert bot.campaign_notifications() == []
    assert bot.capital_notifications() == []

    approve_key = keyboard[0][0]["callback_data"]
    bot.handle_update({"update_id": 10, "callback_query": callback("approve", approve_key)})
    assert handled == []
    confirmation = next(
        payload for method, payload in reversed(fake.calls) if method == "editMessageText"
    )
    assert "BTCUSDT" in confirmation["text"]
    assert "做多" in confirmation["text"]
    assert "中 · <code>MEDIUM</code>" in confirmation["text"]
    assert "最大风险 1" in confirmation["text"]
    assert "2026年8月4日 20:00 UTC" in confirmation["text"]
    confirm_key = confirmation["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    bot.handle_update({"update_id": 11, "callback_query": callback("confirm", confirm_key)})
    assert isinstance(handled[0], TelegramProposalReviewAction)
    assert handled[0].action == "APPROVE_PROPOSAL"
    assert handled[1] == 11
    receipt = next(
        payload for method, payload in reversed(fake.calls) if method == "editMessageText"
    )
    assert "审核已记录" in receipt["text"]
    assert "BTCUSDT" in receipt["text"]


def test_authoritative_rejection_is_an_alert_and_never_claims_a_write() -> None:
    fake = FakeBotApi()
    bot, _bindings, _handled, recipient_id = gateway(fake)
    bind(bot)
    bot.set_action_handler(lambda _action, _update_id: "未执行: VERSION_CONFLICT")
    bot.send(proposal(recipient_id))
    source_key = fake.calls[-1][1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    bot.handle_update({"update_id": 20, "callback_query": callback("approve", source_key)})
    confirmation = next(
        payload for method, payload in reversed(fake.calls) if method == "editMessageText"
    )
    confirm_key = confirmation["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    bot.handle_update({"update_id": 21, "callback_query": callback("confirm", confirm_key)})

    alert = next(
        payload for method, payload in reversed(fake.calls) if method == "answerCallbackQuery"
    )
    assert alert["show_alert"] is True
    assert "操作未执行" in alert["text"]
    assert "写入" not in alert["text"]
    receipt = next(
        payload for method, payload in reversed(fake.calls) if method == "editMessageText"
    )
    assert "操作未执行" in receipt["text"]
    assert "对象版本已变化" in receipt["text"]


def test_authoritative_self_review_rejection_explains_the_exact_reason() -> None:
    fake = FakeBotApi()
    bot, _bindings, _handled, recipient_id = gateway(fake)
    bind(bot)
    bot.set_action_handler(lambda _action, _update_id: "未执行: SELF_REVIEW_FORBIDDEN")
    bot.send(proposal(recipient_id))
    source_key = fake.calls[-1][1]["reply_markup"]["inline_keyboard"][1][0]["callback_data"]
    bot.handle_update({"update_id": 30, "callback_query": callback("reject", source_key)})
    confirmation = next(
        payload for method, payload in reversed(fake.calls) if method == "editMessageText"
    )
    confirm_key = confirmation["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    bot.handle_update({"update_id": 31, "callback_query": callback("confirm", confirm_key)})

    receipt = next(
        payload for method, payload in reversed(fake.calls) if method == "editMessageText"
    )
    assert "操作未执行" in receipt["text"]
    assert "创建者不能审核自己的提案" in receipt["text"]
    assert "SELF_REVIEW_FORBIDDEN" in receipt["text"]


def test_private_start_binds_allowlisted_username_and_todo_is_available() -> None:
    fake = FakeBotApi()
    bot, bindings, _handled, recipient_id = gateway(fake)
    bind(bot)
    assert bindings[str(recipient_id)] == "789"
    assert "绑定成功" in fake.calls[-1][1]["text"]

    bot.handle_update(
        {
            "update_id": 2,
            "message": {
                "text": "/todo",
                "from": {"id": 789, "username": "telegram-owner"},
                "chat": {"id": 789, "type": "private"},
            },
        }
    )
    assert "待我审核 · 1 项" in fake.calls[-1][1]["text"]
    assert fake.calls[-1][1]["reply_markup"] == {
        "inline_keyboard": [[{"text": "打开 Web 审核队列", "url": "http://127.0.0.1:8014/reviews"}]]
    }


def test_todo_limits_message_to_earliest_ten_and_reports_omitted_count() -> None:
    fake = FakeBotApi()
    recipient_id = uuid4()
    todo_items = [
        ProposalNotification(
            **{
                **proposal(recipient_id).__dict__,
                "notification_id": f"todo-{index}",
                "symbol": f"PAIR{index}",
            }
        )
        for index in range(12)
    ]
    bot, _bindings, _handled, _gateway_recipient_id = gateway(fake, todo_items=todo_items)
    bind(bot)

    bot.handle_update(
        {
            "update_id": 2,
            "message": {
                "text": "/todo",
                "from": {"id": 789, "username": "telegram-owner"},
                "chat": {"id": 789, "type": "private"},
            },
        }
    )

    payload = fake.calls[-1][1]
    assert "待我审核 · 12 项" in payload["text"]
    assert "另有 2 项" in payload["text"]
    assert "PAIR9" in payload["text"]
    assert "PAIR10" not in payload["text"]
    assert "截止 2026年8月4日 20:00 UTC" in payload["text"]


def test_start_rejects_different_username_and_group_chat() -> None:
    fake = FakeBotApi()
    bot, bindings, _handled, _recipient_id = gateway(fake)
    for update_id, username, chat_type in (
        (1, "intruder", "private"),
        (2, "telegram-owner", "group"),
    ):
        bot.handle_update(
            {
                "update_id": update_id,
                "message": {
                    "text": "/start",
                    "from": {"id": 789, "username": username},
                    "chat": {"id": 789, "type": chat_type},
                },
            }
        )
    assert bindings == {}
    texts = [payload["text"] for method, payload in fake.calls if method == "sendMessage"]
    assert any("白名单" in text for text in texts)
    assert any("一对一私聊" in text for text in texts)


def test_forwarded_or_cross_account_button_fails_closed() -> None:
    fake = FakeBotApi()
    bot, _bindings, handled, recipient_id = gateway(fake)
    bind(bot)
    bot.send(proposal(recipient_id))
    key = fake.calls[-1][1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    bot.handle_update(
        {"update_id": 3, "callback_query": callback("wrong-sender", key, sender_id=790)}
    )
    assert handled == []
    alert = next(
        payload for method, payload in reversed(fake.calls) if method == "answerCallbackQuery"
    )
    assert alert["show_alert"] is True
    assert "身份不匹配" in alert["text"]


def test_proposal_delivery_is_deduplicated_and_non_pending_has_no_action() -> None:
    fake = FakeBotApi()
    bot, _bindings, _handled, recipient_id = gateway(fake)
    bind(bot)
    item = proposal(recipient_id)
    bot.send(item)
    bot.send(item)
    assert len(bot.notifications()) == 1
    assert [method for method, _payload in fake.calls].count("sendMessage") == 2

    expired = proposal(recipient_id, status="EXPIRED")
    expired = ProposalNotification(**{**expired.__dict__, "notification_id": "expired"})
    bot.send(expired)
    rows = fake.calls[-1][1]["reply_markup"]["inline_keyboard"]
    assert rows == [[{"text": "打开 Web 安全审核", "url": expired.review_url}]]

    shadow = proposal(recipient_id)
    shadow = ProposalNotification(
        **{
            **shadow.__dict__,
            "notification_id": "shadow",
            "environment": "SHADOW",
        }
    )
    bot.send(shadow)
    assert all(item.notification_id != "shadow" for item in bot.notifications())
    assert [method for method, _payload in fake.calls].count("sendMessage") == 3


def test_unbound_notification_is_held_without_network_delivery() -> None:
    fake = FakeBotApi()
    bot, _bindings, _handled, recipient_id = gateway(fake)
    bot.send(proposal(recipient_id))
    assert fake.calls == []
    assert bot.notifications() == []


def test_binding_failure_and_missing_handler_fail_closed() -> None:
    fake = FakeBotApi()
    token = "123456789:abcdefghijklmnopqrstuvwxyz"  # noqa: S105
    recipient_id = uuid4()
    bot = TelegramBotGateway(
        token=token,
        allowed_username="telegram-owner",
        internal_username="telegram-owner",
        binder=lambda *_args: (_ for _ in ()).throw(RuntimeError("no binding")),
        chat_resolver=lambda _user_id: "789",
        client=TelegramBotClient(token, base_url="https://telegram.invalid", poster=fake.poster),
    )
    bind(bot)
    assert "绑定失败" in fake.calls[-1][1]["text"]

    bot.send(proposal(recipient_id))
    key = fake.calls[-1][1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    bot.handle_update({"update_id": 3, "callback_query": callback("no-handler", key)})
    alert = next(
        payload for method, payload in reversed(fake.calls) if method == "answerCallbackQuery"
    )
    assert alert["show_alert"] is True
    assert "暂未准备好" in alert["text"]


def test_default_poster_sanitizes_network_failure(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(URLError("secret-url")),
    )
    with pytest.raises(TelegramUnavailable, match="could not be reached") as error:
        _default_poster("https://example.invalid/bot-secret/send", {}, 1)
    assert "secret" not in str(error.value)


def test_default_poster_classifies_polling_conflict_without_exposing_bot_url(
    monkeypatch: Any,
) -> None:
    response = BytesIO(
        b'{"ok":false,"error_code":409,'
        b'"description":"Conflict: terminated by other getUpdates request"}'
    )
    rejection = HTTPError(
        "https://example.invalid/bot-secret/getUpdates",
        409,
        "Conflict",
        None,
        response,
    )
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(rejection),
    )

    with pytest.raises(TelegramUnavailable) as error:
        _default_poster("https://example.invalid/bot-secret/getUpdates", {}, 1)

    assert error.value.code == "TELEGRAM_POLLING_CONFLICT"
    assert "secret" not in str(error.value)


def test_polling_health_reports_successful_long_poll_without_enabling_actions() -> None:
    fake = FakeBotApi()
    polled = Event()
    original_poster = fake.poster

    def poster(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        result = original_poster(url, payload, timeout)
        if url.endswith("/getUpdates"):
            polled.set()
        return result

    token = "123456789:abcdefghijklmnopqrstuvwxyz"  # noqa: S105
    bot = TelegramBotGateway(
        token=token,
        allowed_username="telegram-owner",
        internal_username="telegram-owner",
        binder=lambda *_args: "telegram-owner",
        chat_resolver=lambda _user_id: None,
        client=TelegramBotClient(token, base_url="https://telegram.invalid", poster=poster),
    )

    bot.start()
    assert polled.wait(1)
    bot.stop()

    health = bot.polling_health()
    assert health["state"] == "HEALTHY"
    assert health["running"] is False
    assert health["last_success_at"] is not None
    assert health["consecutive_failures"] == 0


def test_default_poster_rejects_invalid_json_without_exposing_url(monkeypatch: Any) -> None:
    class Response:
        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return b"not-json"

    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: Response())
    with pytest.raises(TelegramUnavailable, match="invalid JSON"):
        _default_poster("https://example.invalid/bot-secret/send", {}, 1)


def test_default_poster_rejects_unsuccessful_response(monkeypatch: Any) -> None:
    class Response:
        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"ok":false}'

    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: Response())
    with pytest.raises(TelegramUnavailable, match="rejected"):
        _default_poster("https://example.invalid/bot-secret/send", {}, 1)


def test_help_status_and_unknown_commands_explain_narrow_boundary() -> None:
    assert "唯一可执行动作" in render_help()
    assert "不看资金、不下单" in render_help()
    assert "冻结提案批准 / 拒绝" in render_status()
    assert "风险开关" in render_status()

    fake = FakeBotApi()
    bot, _bindings, _handled, _recipient_id = gateway(fake)
    bind(bot)
    for update_id, command in enumerate(("/help", "/status", "/unknown"), start=2):
        bot.handle_update(
            {
                "update_id": update_id,
                "message": {
                    "text": command,
                    "from": {"id": 789, "username": "telegram-owner"},
                    "chat": {"id": 789, "type": "private"},
                },
            }
        )
    texts = [payload["text"] for method, payload in fake.calls if method == "sendMessage"]
    assert any("不支持该命令" in text for text in texts)
    assert all(
        payload["parse_mode"] == "HTML" for method, payload in fake.calls if method == "sendMessage"
    )


def test_proposal_render_is_escaped_bounded_and_hierarchical() -> None:
    item = proposal(uuid4())
    item = ProposalNotification(
        **{
            **item.__dict__,
            "summary": "<script>alert(1)</script>" + "x" * 4_000,
            "symbol": "<BTC>",
        }
    )
    text = render_proposal_notification(item)
    assert len(text) <= MAX_TELEGRAM_TEXT
    assert "<script>" not in text
    assert "&lt;BTC&gt;" in text or "通知内容过长" in text


def test_confirmation_can_be_cancelled_without_calling_handler() -> None:
    fake = FakeBotApi()
    bot, _bindings, handled, recipient_id = gateway(fake)
    bind(bot)
    bot.send(proposal(recipient_id))
    original = fake.calls[-1][1]
    key = original["reply_markup"]["inline_keyboard"][1][0]["callback_data"]
    bot.handle_update({"update_id": 3, "callback_query": callback("reject", key)})
    confirmation = next(
        payload for method, payload in reversed(fake.calls) if method == "editMessageText"
    )
    cancel_key = confirmation["reply_markup"]["inline_keyboard"][1][0]["callback_data"]
    bot.handle_update({"update_id": 4, "callback_query": callback("cancel", cancel_key)})
    assert handled == []
    restored = next(
        payload for method, payload in reversed(fake.calls) if method == "editMessageText"
    )
    assert restored["text"] == original["text"]
    assert restored["reply_markup"] == original["reply_markup"]


def test_failed_delivery_can_retry_and_is_not_deduplicated() -> None:
    fake = FakeBotApi()
    bot, _bindings, _handled, recipient_id = gateway(fake)
    bind(bot)
    item = proposal(recipient_id)
    fake.fail_send = True
    bot.send(item)
    assert bot.notifications() == []
    fake.fail_send = False
    bot.send(item)
    assert len(bot.notifications()) == 1
