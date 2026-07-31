from __future__ import annotations

import hashlib
import json
import logging
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar, Protocol
from uuid import UUID

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProposalNotification:
    notification_id: str
    reviewer_id: UUID
    proposal_id: UUID
    proposal_version: int
    environment: str
    summary: str
    review_code: str
    review_url: str
    created_at: datetime


@dataclass(frozen=True)
class CampaignNotification:
    notification_id: str
    recipient_id: UUID
    campaign_id: UUID
    event_type: str
    environment: str
    summary: str
    campaign_version: int
    action_references: tuple[tuple[str, str], ...]
    created_at: datetime


@dataclass(frozen=True)
class CapitalNotification:
    notification_id: str
    recipient_id: UUID
    object_id: UUID
    object_type: str
    event_type: str
    environment: str
    summary: str
    object_version: int
    created_at: datetime


@dataclass(frozen=True)
class TelegramCampaignAction:
    callback_key: str
    recipient_id: UUID
    campaign_id: UUID
    action: str
    action_reference: str
    campaign_version: int


class TelegramGateway(Protocol):
    def send(self, notification: ProposalNotification) -> None: ...

    def send_campaign(self, notification: CampaignNotification) -> None: ...

    def send_capital(self, notification: CapitalNotification) -> None: ...

    def notifications(self) -> list[ProposalNotification]: ...

    def campaign_notifications(self) -> list[CampaignNotification]: ...

    def capital_notifications(self) -> list[CapitalNotification]: ...


class MockTelegramGateway:
    """Deterministic nonproduction sink. It never contacts Telegram's network."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._notifications: list[ProposalNotification] = []
        self._campaign_notifications: list[CampaignNotification] = []
        self._capital_notifications: list[CapitalNotification] = []

    def send(self, notification: ProposalNotification) -> None:
        with self._lock:
            if all(
                item.notification_id != notification.notification_id for item in self._notifications
            ):
                self._notifications.append(notification)

    def notifications(self) -> list[ProposalNotification]:
        with self._lock:
            return list(self._notifications)

    def send_campaign(self, notification: CampaignNotification) -> None:
        with self._lock:
            if all(
                item.notification_id != notification.notification_id
                for item in self._campaign_notifications
            ):
                self._campaign_notifications.append(notification)

    def campaign_notifications(self) -> list[CampaignNotification]:
        with self._lock:
            return list(self._campaign_notifications)

    def send_capital(self, notification: CapitalNotification) -> None:
        with self._lock:
            if all(
                item.notification_id != notification.notification_id
                for item in self._capital_notifications
            ):
                self._capital_notifications.append(notification)

    def capital_notifications(self) -> list[CapitalNotification]:
        with self._lock:
            return list(self._capital_notifications)


TelegramPoster = Callable[[str, dict[str, Any], float], dict[str, Any]]
TelegramBinder = Callable[[str, str, str], str]
TelegramChatResolver = Callable[[UUID], str | None]
TelegramActionHandler = Callable[[TelegramCampaignAction, int], str]


class TelegramUnavailable(RuntimeError):
    pass


def _default_poster(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(  # noqa: S310
        url,
        data=json.dumps(payload, separators=(",", ":")).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            raw = response.read()
    except (TimeoutError, urllib.error.URLError):
        # urllib exceptions may include the request URL, which contains the Bot token.
        raise TelegramUnavailable("Telegram Bot API could not be reached") from None
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TelegramUnavailable("Telegram Bot API returned invalid JSON") from exc
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise TelegramUnavailable("Telegram Bot API rejected the request")
    return result


class TelegramBotClient:
    """Small Bot API client. The token never leaves the request URL or enters logs."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = "https://api.telegram.org",
        poster: TelegramPoster = _default_poster,
    ) -> None:
        self._endpoint = f"{base_url.rstrip('/')}/bot{token}"
        self._poster = poster

    def call(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        return self._poster(f"{self._endpoint}/{method}", payload, timeout)


class TelegramBotGateway(MockTelegramGateway):
    """Real private-chat Telegram transport with local long polling.

    Business state remains in Trading. Short-lived callback mappings are intentionally
    process-local: after a restart old buttons fail closed and the user must use a fresh
    notification or the Web/PWA detail page.
    """

    _ACTION_LABELS: ClassVar[dict[str, str]] = {
        "DISABLE_CAMPAIGN_AUTO_ADD": "关闭该仓继续加仓",
        "PAUSE_NEW_RISK": "暂停全部新增风险",
        "EMERGENCY_REDUCE": "紧急减仓 50%",
        "EXIT": "完全退出",
    }

    def __init__(
        self,
        *,
        token: str,
        allowed_username: str,
        internal_username: str,
        binder: TelegramBinder,
        chat_resolver: TelegramChatResolver,
        poll_timeout_seconds: int = 20,
        client: TelegramBotClient | None = None,
    ) -> None:
        super().__init__()
        self._client = client or TelegramBotClient(token)
        self._allowed_username = self._normalize_username(allowed_username)
        self._internal_username = internal_username
        self._binder = binder
        self._chat_resolver = chat_resolver
        self._poll_timeout_seconds = poll_timeout_seconds
        self._action_handler: TelegramActionHandler | None = None
        self._actions: dict[str, TelegramCampaignAction] = {}
        self._stop_event = threading.Event()
        self._poll_thread: threading.Thread | None = None

    @staticmethod
    def _normalize_username(value: str) -> str:
        return value.strip().removeprefix("@").casefold()

    @property
    def running(self) -> bool:
        return self._poll_thread is not None and self._poll_thread.is_alive()

    def set_action_handler(self, handler: TelegramActionHandler) -> None:
        self._action_handler = handler

    def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()
        self._poll_thread = threading.Thread(
            target=self._poll,
            name="trading-telegram-poller",
            daemon=True,
        )
        self._poll_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._poll_thread is not None:
            self._poll_thread.join(timeout=self._poll_timeout_seconds + 2)
        self._poll_thread = None

    def send(self, notification: ProposalNotification) -> None:
        before = len(self.notifications())
        super().send(notification)
        if len(self.notifications()) == before:
            return
        keyboard = {
            "inline_keyboard": [
                [{"text": "打开审核页面", "url": notification.review_url}],
            ]
        }
        self._send_to_user(
            notification.reviewer_id,
            (
                f"待审核提案 · {notification.environment}\n"
                f"{notification.summary}\n"
                f"版本: {notification.proposal_version}"
            ),
            keyboard,
        )

    def send_campaign(self, notification: CampaignNotification) -> None:
        before = len(self.campaign_notifications())
        super().send_campaign(notification)
        if len(self.campaign_notifications()) == before:
            return
        rows: list[list[dict[str, str]]] = []
        with self._lock:
            for action, reference in notification.action_references:
                callback_key = (
                    "ca_"
                    + hashlib.sha256(
                        f"{notification.notification_id}:{action}:{reference}".encode()
                    ).hexdigest()[:24]
                )
                self._actions[callback_key] = TelegramCampaignAction(
                    callback_key=callback_key,
                    recipient_id=notification.recipient_id,
                    campaign_id=notification.campaign_id,
                    action=action,
                    action_reference=reference,
                    campaign_version=notification.campaign_version,
                )
                rows.append(
                    [
                        {
                            "text": self._ACTION_LABELS[action],
                            "callback_data": callback_key,
                        }
                    ]
                )
        self._send_to_user(
            notification.recipient_id,
            (
                f"Campaign 事件 · {notification.environment}\n"
                f"{notification.summary}\n"
                f"版本: {notification.campaign_version}"
            ),
            {"inline_keyboard": rows},
        )

    def send_capital(self, notification: CapitalNotification) -> None:
        before = len(self.capital_notifications())
        super().send_capital(notification)
        if len(self.capital_notifications()) == before:
            return
        self._send_to_user(
            notification.recipient_id,
            (
                f"资金通知 · {notification.environment}\n"
                f"{notification.summary}\n"
                "Telegram 不能批准或执行资金动作。"
            ),
        )

    def _send_to_user(
        self,
        user_id: UUID,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        chat_id = self._chat_resolver(user_id)
        if chat_id is None:
            logger.info(
                "Telegram notification held because the internal user is not bound",
                extra={"event": "telegram_user_unbound", "recipient_id": str(user_id)},
            )
            return
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            self._client.call("sendMessage", payload)
        except TelegramUnavailable:
            logger.exception(
                "Telegram notification delivery failed",
                extra={"event": "telegram_delivery_failed", "recipient_id": str(user_id)},
            )

    def _poll(self) -> None:
        offset = 0
        try:
            self._client.call("deleteWebhook", {"drop_pending_updates": False})
        except TelegramUnavailable:
            logger.exception(
                "Telegram long polling could not clear the webhook",
                extra={"event": "telegram_poll_start_failed"},
            )
            return
        while not self._stop_event.is_set():
            try:
                response = self._client.call(
                    "getUpdates",
                    {
                        "offset": offset,
                        "timeout": self._poll_timeout_seconds,
                        "allowed_updates": ["message", "callback_query"],
                    },
                    timeout=float(self._poll_timeout_seconds + 5),
                )
                result = response.get("result")
                if not isinstance(result, list):
                    raise TelegramUnavailable("Telegram update response is invalid")
                for update in result:
                    if not isinstance(update, dict):
                        continue
                    update_id = update.get("update_id")
                    if isinstance(update_id, int):
                        offset = max(offset, update_id + 1)
                    self.handle_update(update)
            except TelegramUnavailable:
                logger.exception(
                    "Telegram long polling failed",
                    extra={"event": "telegram_poll_failed"},
                )
                self._stop_event.wait(2)
            except Exception:
                logger.exception(
                    "Unexpected Telegram update failure",
                    extra={"event": "telegram_update_failed"},
                )
                self._stop_event.wait(1)

    def handle_update(self, update: dict[str, Any]) -> None:
        update_id = update.get("update_id")
        if not isinstance(update_id, int):
            return
        message = update.get("message")
        if isinstance(message, dict):
            self._handle_message(message)
            return
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            self._handle_callback(callback, update_id)

    def _handle_message(self, message: dict[str, Any]) -> None:
        chat = message.get("chat")
        sender = message.get("from")
        text = message.get("text")
        if (
            not isinstance(chat, dict)
            or chat.get("type") != "private"
            or not isinstance(sender, dict)
            or not isinstance(text, str)
            or not text.split(maxsplit=1)[0].startswith("/start")
        ):
            return
        chat_id = chat.get("id")
        telegram_user_id = sender.get("id")
        username = sender.get("username")
        if (
            not isinstance(chat_id, int)
            or not isinstance(telegram_user_id, int)
            or chat_id != telegram_user_id
            or not isinstance(username, str)
            or self._normalize_username(username) != self._allowed_username
        ):
            if isinstance(chat_id, int):
                self._safe_call(
                    "sendMessage",
                    {"chat_id": chat_id, "text": "此 Telegram 账号不在内部用户白名单中。"},
                )
            return
        try:
            bound_username = self._binder(
                str(chat_id),
                username,
                self._internal_username,
            )
        except Exception:
            logger.exception(
                "Telegram private-chat binding failed",
                extra={"event": "telegram_binding_failed"},
            )
            self._safe_call(
                "sendMessage",
                {"chat_id": chat_id, "text": "绑定失败, 请在 Web 控制台检查内部用户。"},
            )
            return
        self._safe_call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": (
                    f"已绑定内部用户 {bound_username}。\n"
                    "本 Bot 只提供通知、审核页面跳转和只收紧风险操作。"
                ),
            },
        )

    def _handle_callback(self, callback: dict[str, Any], update_id: int) -> None:
        callback_id = callback.get("id")
        sender = callback.get("from")
        message = callback.get("message")
        callback_key = callback.get("data")
        if (
            not isinstance(callback_id, str)
            or not isinstance(sender, dict)
            or not isinstance(message, dict)
            or not isinstance(callback_key, str)
        ):
            return
        chat = message.get("chat")
        if not isinstance(chat, dict) or chat.get("type") != "private":
            self._answer_callback(callback_id, "只允许在 Bot 私聊中操作。", show_alert=True)
            return
        chat_id = chat.get("id")
        sender_id = sender.get("id")
        if not isinstance(chat_id, int) or sender_id != chat_id:
            self._answer_callback(callback_id, "Telegram 身份不匹配。", show_alert=True)
            return
        with self._lock:
            action = self._actions.get(callback_key)
        if action is None or self._chat_resolver(action.recipient_id) != str(chat_id):
            self._answer_callback(callback_id, "按钮已过期或不属于当前用户。", show_alert=True)
            return
        if self._action_handler is None:
            self._answer_callback(callback_id, "Trading 暂未准备好。", show_alert=True)
            return
        try:
            result = self._action_handler(action, update_id)
        except Exception:
            logger.exception(
                "Telegram campaign action failed",
                extra={"event": "telegram_action_failed", "action": action.action},
            )
            self._answer_callback(callback_id, "操作失败, 请打开 Web 控制台确认。", show_alert=True)
            return
        self._answer_callback(callback_id, result, show_alert=False)
        self._safe_call(
            "editMessageReplyMarkup",
            {
                "chat_id": chat_id,
                "message_id": message.get("message_id"),
                "reply_markup": {"inline_keyboard": []},
            },
        )

    def _answer_callback(self, callback_id: str, text: str, *, show_alert: bool) -> None:
        self._safe_call(
            "answerCallbackQuery",
            {
                "callback_query_id": callback_id,
                "text": text[:200],
                "show_alert": show_alert,
            },
        )

    def _safe_call(self, method: str, payload: dict[str, Any]) -> None:
        try:
            self._client.call(method, payload)
        except TelegramUnavailable:
            logger.exception(
                "Telegram response delivery failed",
                extra={"event": "telegram_response_failed", "method": method},
            )
