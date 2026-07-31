from datetime import UTC, datetime
from typing import Any
from urllib.error import URLError
from uuid import UUID, uuid4

import pytest

from trading_control_plane.telegram import (
    CampaignNotification,
    CapitalNotification,
    ProposalNotification,
    TelegramBotClient,
    TelegramBotGateway,
    TelegramCampaignAction,
    TelegramUnavailable,
    _default_poster,
)


class FakeBotApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def poster(self, url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        del timeout
        self.calls.append((url.rsplit("/", maxsplit=1)[-1], payload))
        return {"ok": True, "result": []}


def gateway(
    fake: FakeBotApi,
) -> tuple[TelegramBotGateway, dict[str, str], list[object], UUID]:
    token = "123456789:abcdefghijklmnopqrstuvwxyz"  # noqa: S105
    bindings: dict[str, str] = {}
    handled: list[object] = []
    recipient_id = uuid4()

    def bind(chat_id: str, telegram_username: str, internal_username: str) -> str:
        assert telegram_username == "kelly_oooo"
        assert internal_username == "kelly_oooo"
        bindings[str(recipient_id)] = chat_id
        return internal_username

    instance = TelegramBotGateway(
        token=token,
        allowed_username="@kelly_oooo",
        internal_username="kelly_oooo",
        binder=bind,
        chat_resolver=lambda user_id: bindings.get(str(user_id)),
        client=TelegramBotClient(
            token,
            base_url="https://telegram.invalid",
            poster=fake.poster,
        ),
    )
    instance.set_action_handler(
        lambda action, update_id: handled.extend([action, update_id]) or "accepted"
    )
    return instance, bindings, handled, recipient_id


def test_private_start_binds_allowlisted_username_to_numeric_chat_id() -> None:
    fake = FakeBotApi()
    bot, bindings, _, recipient_id = gateway(fake)

    bot.handle_update(
        {
            "update_id": 1,
            "message": {
                "text": "/start",
                "from": {"id": 789, "username": "kelly_oooo"},
                "chat": {"id": 789, "type": "private"},
            },
        }
    )

    assert bindings[str(recipient_id)] == "789"
    assert fake.calls[-1][0] == "sendMessage"
    assert "已绑定内部用户" in fake.calls[-1][1]["text"]


def test_start_rejects_a_different_or_group_identity() -> None:
    fake = FakeBotApi()
    bot, bindings, _, _ = gateway(fake)

    bot.handle_update(
        {
            "update_id": 2,
            "message": {
                "text": "/start",
                "from": {"id": 999, "username": "someone_else"},
                "chat": {"id": 999, "type": "private"},
            },
        }
    )
    bot.handle_update(
        {
            "update_id": 3,
            "message": {
                "text": "/start",
                "from": {"id": 789, "username": "kelly_oooo"},
                "chat": {"id": -1001, "type": "group"},
            },
        }
    )

    assert bindings == {}
    assert fake.calls[-1] == (
        "sendMessage",
        {"chat_id": 999, "text": "此 Telegram 账号不在内部用户白名单中。"},
    )


def test_campaign_button_is_compact_bound_and_submitted_to_trading() -> None:
    fake = FakeBotApi()
    bot, bindings, handled, recipient_id = gateway(fake)
    bindings[str(recipient_id)] = "789"
    campaign_id = uuid4()

    bot.send_campaign(
        CampaignNotification(
            notification_id="tg_notification",
            recipient_id=recipient_id,
            campaign_id=campaign_id,
            event_type="POSITION_UPDATED",
            environment="SHADOW",
            summary="position changed",
            campaign_version=4,
            action_references=(("EXIT", "signed-action-reference"),),
            created_at=datetime.now(UTC),
        )
    )
    send_payload = fake.calls[-1][1]
    callback_key = send_payload["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    assert len(callback_key.encode()) <= 64

    bot.handle_update(
        {
            "update_id": 44,
            "callback_query": {
                "id": "callback-1",
                "data": callback_key,
                "from": {"id": 789, "username": "kelly_oooo"},
                "message": {
                    "message_id": 10,
                    "chat": {"id": 789, "type": "private"},
                },
            },
        }
    )

    action = handled[0]
    assert isinstance(action, TelegramCampaignAction)
    assert action.recipient_id == recipient_id
    assert action.campaign_id == campaign_id
    assert action.action == "EXIT"
    assert handled[1] == 44
    assert [method for method, _ in fake.calls[-2:]] == [
        "answerCallbackQuery",
        "editMessageReplyMarkup",
    ]


def test_forwarded_or_cross_account_button_fails_closed() -> None:
    fake = FakeBotApi()
    bot, bindings, handled, recipient_id = gateway(fake)
    bindings[str(recipient_id)] = "789"
    bot.send_campaign(
        CampaignNotification(
            notification_id="tg_notification",
            recipient_id=recipient_id,
            campaign_id=uuid4(),
            event_type="POSITION_UPDATED",
            environment="SHADOW",
            summary="position changed",
            campaign_version=1,
            action_references=(("EXIT", "signed-action-reference"),),
            created_at=datetime.now(UTC),
        )
    )
    callback_key = fake.calls[-1][1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]

    bot.handle_update(
        {
            "update_id": 45,
            "callback_query": {
                "id": "callback-2",
                "data": callback_key,
                "from": {"id": 999},
                "message": {
                    "message_id": 11,
                    "chat": {"id": 999, "type": "private"},
                },
            },
        }
    )

    assert handled == []
    assert fake.calls[-1][0] == "answerCallbackQuery"
    assert fake.calls[-1][1]["show_alert"] is True


def test_proposal_and_capital_notifications_are_delivered_once() -> None:
    fake = FakeBotApi()
    bot, bindings, _, recipient_id = gateway(fake)
    bindings[str(recipient_id)] = "789"
    now = datetime.now(UTC)
    proposal = ProposalNotification(
        notification_id="proposal-notification",
        reviewer_id=recipient_id,
        proposal_id=uuid4(),
        proposal_version=2,
        environment="SHADOW",
        summary="review required",
        review_code="review-reference",
        review_url="http://127.0.0.1:8000/proposals/1",
        created_at=now,
    )
    capital = CapitalNotification(
        notification_id="capital-notification",
        recipient_id=recipient_id,
        object_id=uuid4(),
        object_type="CapitalTransfer",
        event_type="UNKNOWN",
        environment="SHADOW",
        summary="manual review required",
        object_version=3,
        created_at=now,
    )

    bot.send(proposal)
    bot.send(proposal)
    bot.send_capital(capital)
    bot.send_capital(capital)

    assert [method for method, _ in fake.calls] == ["sendMessage", "sendMessage"]
    assert fake.calls[0][1]["reply_markup"]["inline_keyboard"][0][0]["url"] == (proposal.review_url)
    assert "不能批准或执行资金动作" in fake.calls[1][1]["text"]
    assert bot.notifications() == [proposal]
    assert bot.capital_notifications() == [capital]


def test_unbound_notification_is_held_without_network_delivery() -> None:
    fake = FakeBotApi()
    bot, _, _, recipient_id = gateway(fake)
    bot.send(
        ProposalNotification(
            notification_id="unbound-notification",
            reviewer_id=recipient_id,
            proposal_id=uuid4(),
            proposal_version=1,
            environment="SHADOW",
            summary="review required",
            review_code="review-reference",
            review_url="http://127.0.0.1:8000/proposals/1",
            created_at=datetime.now(UTC),
        )
    )
    assert fake.calls == []


def test_binding_failure_and_unavailable_action_fail_closed() -> None:
    fake = FakeBotApi()
    token = "123456789:abcdefghijklmnopqrstuvwxyz"  # noqa: S105
    bot = TelegramBotGateway(
        token=token,
        allowed_username="kelly_oooo",
        internal_username="missing-user",
        binder=lambda _chat_id, _telegram_username, _internal_username: (_ for _ in ()).throw(
            RuntimeError("missing")
        ),
        chat_resolver=lambda _user_id: None,
        client=TelegramBotClient(
            token,
            base_url="https://telegram.invalid",
            poster=fake.poster,
        ),
    )
    bot.handle_update(
        {
            "update_id": 50,
            "message": {
                "text": "/start",
                "from": {"id": 789, "username": "kelly_oooo"},
                "chat": {"id": 789, "type": "private"},
            },
        }
    )
    bot.handle_update(
        {
            "update_id": 51,
            "callback_query": {
                "id": "missing-action",
                "data": "ca_missing",
                "from": {"id": 789},
                "message": {"message_id": 1, "chat": {"id": 789, "type": "private"}},
            },
        }
    )
    assert "绑定失败" in fake.calls[0][1]["text"]
    assert fake.calls[1][0] == "answerCallbackQuery"
    assert fake.calls[1][1]["show_alert"] is True


def test_bot_client_sanitizes_network_and_json_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "trading_control_plane.telegram.urllib.request.urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(URLError("secret URL")),
    )
    with pytest.raises(TelegramUnavailable, match="could not be reached") as exc_info:
        _default_poster(
            "https://api.telegram.invalid/bot-secret/getMe",
            {},
            1,
        )
    assert exc_info.value.__cause__ is None
    assert "secret" not in str(exc_info.value)

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return b"not-json"

    monkeypatch.setattr(
        "trading_control_plane.telegram.urllib.request.urlopen",
        lambda *_args, **_kwargs: Response(),
    )
    with pytest.raises(TelegramUnavailable, match="invalid JSON"):
        _default_poster("https://api.telegram.invalid/bot-redacted/getMe", {}, 1)

    class RejectedResponse(Response):
        def read(self) -> bytes:
            return b'{"ok":false}'

    monkeypatch.setattr(
        "trading_control_plane.telegram.urllib.request.urlopen",
        lambda *_args, **_kwargs: RejectedResponse(),
    )
    with pytest.raises(TelegramUnavailable, match="rejected"):
        _default_poster("https://api.telegram.invalid/bot-redacted/getMe", {}, 1)

    class OkResponse(Response):
        def read(self) -> bytes:
            return b'{"ok":true,"result":[]}'

    monkeypatch.setattr(
        "trading_control_plane.telegram.urllib.request.urlopen",
        lambda *_args, **_kwargs: OkResponse(),
    )
    assert (
        _default_poster(
            "https://api.telegram.invalid/bot-redacted/getMe",
            {},
            1,
        )["ok"]
        is True
    )


def test_long_polling_lifecycle_dispatches_updates_and_stops() -> None:
    fake = FakeBotApi()
    bot, bindings, _, recipient_id = gateway(fake)

    class PollClient:
        def call(
            self,
            method: str,
            payload: dict[str, Any],
            *,
            timeout: float = 10.0,
        ) -> dict[str, Any]:
            del payload, timeout
            if method == "getUpdates":
                bot._stop_event.set()  # type: ignore[attr-defined]
                return {
                    "ok": True,
                    "result": [
                        {
                            "update_id": 60,
                            "message": {
                                "text": "/start",
                                "from": {"id": 789, "username": "kelly_oooo"},
                                "chat": {"id": 789, "type": "private"},
                            },
                        }
                    ],
                }
            return {"ok": True, "result": True}

    bot._client = PollClient()  # type: ignore[assignment]
    bot.start()
    assert bot._poll_thread is not None  # type: ignore[attr-defined]
    bot._poll_thread.join(timeout=2)  # type: ignore[attr-defined]
    bot.stop()

    assert bindings[str(recipient_id)] == "789"
    assert bot.running is False


def test_group_missing_handler_and_handler_failure_callbacks_are_rejected() -> None:
    fake = FakeBotApi()
    bot, bindings, _, recipient_id = gateway(fake)
    bindings[str(recipient_id)] = "789"
    bot.send_campaign(
        CampaignNotification(
            notification_id="callback-branches",
            recipient_id=recipient_id,
            campaign_id=uuid4(),
            event_type="POSITION_UPDATED",
            environment="SHADOW",
            summary="position changed",
            campaign_version=1,
            action_references=(("EXIT", "signed-action-reference"),),
            created_at=datetime.now(UTC),
        )
    )
    callback_key = fake.calls[-1][1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]

    bot.handle_update(
        {
            "update_id": 70,
            "callback_query": {
                "id": "group-callback",
                "data": callback_key,
                "from": {"id": 789},
                "message": {
                    "message_id": 1,
                    "chat": {"id": -1001, "type": "group"},
                },
            },
        }
    )
    assert fake.calls[-1][1]["text"] == "只允许在 Bot 私聊中操作。"

    bot._action_handler = None  # type: ignore[attr-defined]
    private_callback = {
        "id": "private-callback",
        "data": callback_key,
        "from": {"id": 789},
        "message": {"message_id": 2, "chat": {"id": 789, "type": "private"}},
    }
    bot.handle_update({"update_id": 71, "callback_query": private_callback})
    assert fake.calls[-1][1]["text"] == "Trading 暂未准备好。"

    def fail(_action: TelegramCampaignAction, _update_id: int) -> str:
        raise RuntimeError("business unavailable")

    bot.set_action_handler(fail)
    bot.handle_update({"update_id": 72, "callback_query": private_callback})
    assert fake.calls[-1][1]["text"] == "操作失败, 请打开 Web 控制台确认。"

    before = len(fake.calls)
    bot.handle_update({"update_id": "invalid"})
    bot.handle_update(
        {
            "update_id": 73,
            "message": {
                "text": "hello",
                "from": {"id": 789, "username": "kelly_oooo"},
                "chat": {"id": 789, "type": "private"},
            },
        }
    )
    assert len(fake.calls) == before
