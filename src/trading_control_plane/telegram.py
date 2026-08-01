# Telegram's user-facing Chinese copy intentionally uses full-width punctuation.
# ruff: noqa: RUF001

from __future__ import annotations

import hashlib
import html
import json
import logging
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, ClassVar, Protocol
from uuid import UUID

from trading_control_plane.domain import CampaignStatus, ProposalStatus

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
    status: str = "PENDING_REVIEW"
    expires_at: str | None = None


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
    status: str | None = None
    auto_add_available: bool | None = None
    position_reduction_available: bool | None = None


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
    environment: str = "SHADOW"
    event_type: str = "UNKNOWN"


@dataclass(frozen=True)
class _ActionPrompt:
    action: TelegramCampaignAction
    source_callback_key: str
    original_text: str
    original_reply_markup: dict[str, Any]


MAX_TELEGRAM_TEXT = 3_500

_EVENT_LABELS: dict[str, str] = {
    "ADD_INTENT_READY": "加仓意图已就绪",
    "RISK_REDUCTION_READY": "减仓意图已就绪",
    "AUTOMATIC_EXIT_READY": "自动退出意图已就绪",
    "CAMPAIGN_AUTO_ADD_DISABLED": "后续加仓已关闭",
    "SHADOW_FILL_RECORDED": "SHADOW 成交事实已记录",
    "ORDER_INTENT_UNKNOWN": "订单结果未知",
    "PROTECTION_ACTIVE": "保护事实有效",
    "PROTECTION_EXCEPTION": "保护事实异常",
    "RECONCILIATION_EXCEPTION": "对账异常",
    "CAMPAIGN_CLOSED": "Campaign 已关闭",
    "POSITION_UPDATED": "仓位状态已更新",
    "PENDING_REVIEW": "等待审核",
    "REVIEW_APPROVE": "审核通过",
    "REVIEW_REJECT": "审核拒绝",
    "SOURCE_RESERVED": "源端资金已预留",
    "IN_TRANSIT": "资金在途",
    "DESTINATION_CONFIRMED": "目的端已确认",
    "RECONCILED": "资金对账完成",
    "UNKNOWN": "状态未知",
}

_STATUS_LABELS: dict[str, str] = {
    CampaignStatus.OPENING.value: "建仓中",
    CampaignStatus.OPEN.value: "持仓中",
    CampaignStatus.REDUCING.value: "减仓中",
    CampaignStatus.CLOSING.value: "平仓中",
    CampaignStatus.CLOSED.value: "已关闭",
    CampaignStatus.UNKNOWN.value: "未知",
    "CANCELLED": "已取消",
    ProposalStatus.DRAFT.value: "草稿",
    ProposalStatus.PENDING_REVIEW.value: "等待审核",
    ProposalStatus.APPROVED.value: "已批准",
    ProposalStatus.REJECTED.value: "已拒绝",
    ProposalStatus.EXPIRED.value: "已过期",
}

_NO_ACTION_EVENTS = {
    "ADD_INTENT_READY",
    "RISK_REDUCTION_READY",
    "AUTOMATIC_EXIT_READY",
    "CAMPAIGN_AUTO_ADD_DISABLED",
    "PROTECTION_ACTIVE",
    "CAMPAIGN_CLOSED",
}

_POSITION_REDUCTION_STATUSES = {
    CampaignStatus.OPENING.value,
    CampaignStatus.OPEN.value,
    CampaignStatus.REDUCING.value,
    CampaignStatus.CLOSING.value,
}

_ERROR_LABELS: dict[str, str] = {
    "VERSION_CONFLICT": "对象版本已变化，请重新打开最新通知",
    "ACTION_REFERENCE_EXPIRED": "操作凭证已过期，请使用最新通知",
    "ACTION_REFERENCE_SCOPE_INVALID": "操作凭证不属于当前用户或对象",
    "CAMPAIGN_NOT_FOUND": "Campaign 不存在或已不可见",
    "CAMPAIGN_NOT_ACTIVE": "Campaign 当前状态不允许该操作",
    "ORDER_INTENT_UNKNOWN": "订单结果未知，系统已阻止重复动作",
    "RBAC_DENIED": "当前身份没有执行该操作的权限",
}


def _escaped(value: object, *, max_length: int = 480) -> str:
    raw = str(value).strip()
    escaped = html.escape(raw, quote=True)
    if len(escaped) <= max_length:
        return escaped
    low = 0
    high = len(raw)
    while low < high:
        midpoint = (low + high + 1) // 2
        if len(html.escape(raw[:midpoint], quote=True)) <= max_length - 1:
            low = midpoint
        else:
            high = midpoint - 1
    return html.escape(raw[:low], quote=True) + "…"


def _optional(value: object | None, *, fallback: str = "未提供") -> str:
    return _escaped(fallback if value is None or str(value).strip() == "" else value)


def _short_id(value: UUID) -> str:
    return str(value).split("-", maxsplit=1)[0]


def _format_time(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _labeled_code(value: str | None, labels: dict[str, str]) -> str:
    if value is None or value.strip() == "":
        return "未提供"
    label = labels.get(value, value)
    if label == value:
        return f"<code>{_escaped(value)}</code>"
    return f"{_escaped(label)} · <code>{_escaped(value)}</code>"


def _ensure_message_limit(text: str) -> str:
    if len(text) <= MAX_TELEGRAM_TEXT:
        return text
    return "<b>通知内容过长</b>\n为避免截断权威字段，请打开 Web 控制台查看完整信息。"


def campaign_action_references(
    notification: CampaignNotification,
) -> tuple[tuple[str, str], ...]:
    """Return only actions that are meaningful for the current notification."""

    if notification.status in {"CLOSED", "CANCELLED"}:
        return ()
    if notification.event_type in _NO_ACTION_EVENTS:
        return ()
    allowed = {action for action, _reference in notification.action_references}
    if notification.auto_add_available is False:
        allowed.discard("DISABLE_CAMPAIGN_AUTO_ADD")
    if notification.position_reduction_available is False:
        allowed -= {"EMERGENCY_REDUCE", "EXIT"}
    if notification.event_type == "ORDER_INTENT_UNKNOWN":
        allowed -= {"EMERGENCY_REDUCE", "EXIT"}
    return tuple(
        (action, reference)
        for action, reference in notification.action_references
        if action in allowed
    )


def campaign_position_reduction_available(
    status: str,
    current_target_quantity: Decimal,
) -> bool:
    """Return whether Telegram may offer Campaign reduction actions."""

    return status in _POSITION_REDUCTION_STATUSES and current_target_quantity > 0


def render_proposal_notification(notification: ProposalNotification) -> str:
    facts = [
        f"<b>环境</b>　<code>{_escaped(notification.environment)}</code>",
        f"<b>对象</b>　提案 <code>{_short_id(notification.proposal_id)}</code>",
        f"<b>状态</b>　{_labeled_code(notification.status, _STATUS_LABELS)} "
        f"· v{notification.proposal_version}",
        f"<b>截止</b>　{_optional(notification.expires_at)}",
    ]
    summary = _escaped(notification.summary, max_length=1_800)
    return _ensure_message_limit(
        "🟠 <b>待审核提案</b>\n"
        + "\n".join(facts)
        + f"\n\n<b>说明</b>\n{summary}"
        + f"\n\n<b>通知时间</b>　{_format_time(notification.created_at)}"
        + "\n\n⚠️ Telegram 只负责通知并打开 Web；不会在聊天中批准提案。"
    )


def render_campaign_notification(notification: CampaignNotification) -> str:
    attention = notification.event_type in {
        "ORDER_INTENT_UNKNOWN",
        "PROTECTION_EXCEPTION",
        "RECONCILIATION_EXCEPTION",
    }
    heading = "🔴 <b>风险事件</b>" if attention else "🔵 <b>Campaign 状态更新</b>"
    return _ensure_message_limit(
        f"{heading}\n"
        f"<b>事件</b>　{_labeled_code(notification.event_type, _EVENT_LABELS)}\n"
        f"<b>环境</b>　<code>{_escaped(notification.environment)}</code>\n"
        f"<b>对象</b>　Campaign <code>{_short_id(notification.campaign_id)}</code>\n"
        f"<b>状态</b>　{_labeled_code(notification.status, _STATUS_LABELS)} "
        f"· v{notification.campaign_version}\n"
        "<b>详情</b>　请在 Web 控制台查看完整交易事实\n\n"
        f"<b>说明</b>\n{_escaped(notification.summary, max_length=1_800)}\n\n"
        f"<b>通知时间</b>　{_format_time(notification.created_at)}\n\n"
        "⚠️ 所有按钮仅提交只收紧风险的请求；权威结果以 Web 控制台为准。"
    )


def render_capital_notification(notification: CapitalNotification) -> str:
    return _ensure_message_limit(
        "🟣 <b>资金状态通知</b>\n"
        f"<b>环境</b>　<code>{_escaped(notification.environment)}</code>\n"
        f"<b>对象</b>　{_escaped(notification.object_type)} "
        f"<code>{_short_id(notification.object_id)}</code>\n"
        f"<b>事件</b>　{_labeled_code(notification.event_type, _EVENT_LABELS)}\n"
        f"<b>状态版本</b>　v{notification.object_version}\n"
        "<b>详情</b>　请在 Web 控制台查看完整资金事实\n\n"
        f"<b>说明</b>\n{_escaped(notification.summary, max_length=1_800)}\n\n"
        f"<b>通知时间</b>　{_format_time(notification.created_at)}\n\n"
        "🔒 Telegram 不提供资金批准、签名、广播或执行入口。"
    )


def render_help() -> str:
    return (
        "<b>Trading Bot 帮助</b>\n\n"
        "/start　绑定或确认内部用户\n"
        "/status　查看 Bot 能力和安全边界\n"
        "/help　查看本帮助\n\n"
        "<b>可以做什么</b>\n"
        "• 接收提案、Campaign 和资金状态通知\n"
        "• 打开 Web 安全审核页\n"
        "• 提交经二次确认的只收紧风险动作\n\n"
        "<b>不会做什么</b>\n"
        "• 不在 Telegram 中批准增险提案\n"
        "• 不批准、签名或执行资金操作\n"
        "• 不绕过 Trading 的权限、版本和风险校验"
    )


def render_status() -> str:
    return (
        "🟢 <b>Trading Bot 正常运行</b>\n\n"
        "<b>会话</b>　仅限已允许的私聊账号\n"
        "<b>审批</b>　只打开 Web，不在聊天中批准\n"
        "<b>风险动作</b>　仅收紧风险，必须二次确认\n"
        "<b>资金动作</b>　不支持批准、签名或执行\n"
        "<b>权威状态</b>　以 Trading Web 与 PostgreSQL 为准\n\n"
        "此状态不会显示账户余额、密钥、Token 或私钥。"
    )


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
    _ACTION_IMPACTS: ClassVar[dict[str, str]] = {
        "DISABLE_CAMPAIGN_AUTO_ADD": "关闭此 Campaign 后续 AddUnit，不影响已有减仓与退出能力。",
        "PAUSE_NEW_RISK": "将全局系统切换为只减仓，阻止全部新增风险。",
        "EMERGENCY_REDUCE": "提交 reduce-only 意图，将当前目标仓位减少 50%。",
        "EXIT": "提交 reduce-only 退出意图，将目标仓位降至 0。",
    }
    _COMMANDS: ClassVar[list[dict[str, str]]] = [
        {"command": "start", "description": "绑定或确认内部用户"},
        {"command": "status", "description": "查看 Bot 能力和安全边界"},
        {"command": "help", "description": "查看使用帮助"},
    ]

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
        self._source_prompts: dict[str, _ActionPrompt] = {}
        self._confirmations: dict[str, _ActionPrompt] = {}
        self._cancellations: dict[str, _ActionPrompt] = {}
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
        if any(
            item.notification_id == notification.notification_id for item in self.notifications()
        ):
            return
        keyboard = {
            "inline_keyboard": [
                [{"text": "打开 Web 安全审核", "url": notification.review_url}],
            ]
        }
        delivered = self._send_to_user(
            notification.reviewer_id,
            render_proposal_notification(notification),
            keyboard,
        )
        if delivered:
            super().send(notification)

    def send_campaign(self, notification: CampaignNotification) -> None:
        if any(
            item.notification_id == notification.notification_id
            for item in self.campaign_notifications()
        ):
            return
        rows: list[list[dict[str, str]]] = []
        pending_actions: dict[str, TelegramCampaignAction] = {}
        for action, reference in campaign_action_references(notification):
            callback_key = (
                "ca_"
                + hashlib.sha256(
                    f"{notification.notification_id}:{action}:{reference}".encode()
                ).hexdigest()[:24]
            )
            pending_actions[callback_key] = TelegramCampaignAction(
                callback_key=callback_key,
                recipient_id=notification.recipient_id,
                campaign_id=notification.campaign_id,
                action=action,
                action_reference=reference,
                campaign_version=notification.campaign_version,
                environment=notification.environment,
                event_type=notification.event_type,
            )
            rows.append(
                [
                    {
                        "text": f"需确认 · {self._ACTION_LABELS[action]}",
                        "callback_data": callback_key,
                    }
                ]
            )
        text = render_campaign_notification(notification)
        keyboard = {"inline_keyboard": rows}
        delivered = self._send_to_user(
            notification.recipient_id,
            text,
            keyboard if rows else None,
        )
        if not delivered:
            return
        super().send_campaign(notification)
        with self._lock:
            self._actions.update(pending_actions)
            for callback_key, pending_action in pending_actions.items():
                self._source_prompts[callback_key] = _ActionPrompt(
                    action=pending_action,
                    source_callback_key=callback_key,
                    original_text=text,
                    original_reply_markup=keyboard,
                )

    def send_capital(self, notification: CapitalNotification) -> None:
        if any(
            item.notification_id == notification.notification_id
            for item in self.capital_notifications()
        ):
            return
        delivered = self._send_to_user(
            notification.recipient_id,
            render_capital_notification(notification),
        )
        if delivered:
            super().send_capital(notification)

    def _send_to_user(
        self,
        user_id: UUID,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> bool:
        chat_id = self._chat_resolver(user_id)
        if chat_id is None:
            logger.info(
                "Telegram notification held because the internal user is not bound",
                extra={"event": "telegram_user_unbound", "recipient_id": str(user_id)},
            )
            return False
        payload = self._message_payload(chat_id, text, reply_markup)
        try:
            self._client.call("sendMessage", payload)
        except TelegramUnavailable:
            logger.exception(
                "Telegram notification delivery failed",
                extra={"event": "telegram_delivery_failed", "recipient_id": str(user_id)},
            )
            return False
        return True

    @staticmethod
    def _message_payload(
        chat_id: str | int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": _ensure_message_limit(text),
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return payload

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
        self._safe_call("setMyCommands", {"commands": self._COMMANDS})
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
        if not isinstance(chat, dict) or not isinstance(sender, dict) or not isinstance(text, str):
            return
        chat_id = chat.get("id")
        command_token = text.split(maxsplit=1)[0]
        if not command_token.startswith("/"):
            return
        command = command_token.split("@", maxsplit=1)[0].casefold()
        if chat.get("type") != "private":
            if isinstance(chat_id, int):
                self._safe_call(
                    "sendMessage",
                    self._message_payload(chat_id, "此 Bot 仅支持一对一私聊。"),
                )
            return
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
                    self._message_payload(
                        chat_id,
                        "此 Telegram 账号不在内部用户白名单中。",
                    ),
                )
            return
        if command == "/help":
            self._safe_call("sendMessage", self._message_payload(chat_id, render_help()))
            return
        if command == "/status":
            self._safe_call("sendMessage", self._message_payload(chat_id, render_status()))
            return
        if command != "/start":
            self._safe_call(
                "sendMessage",
                self._message_payload(
                    chat_id,
                    "<b>不支持该命令</b>\n\n" + render_help(),
                ),
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
                self._message_payload(
                    chat_id,
                    "<b>绑定失败</b>\n请在 Web 控制台检查内部用户状态后重试。",
                ),
            )
            return
        self._safe_call(
            "sendMessage",
            self._message_payload(
                chat_id,
                (
                    "🟢 <b>绑定成功</b>\n"
                    f"内部用户　<code>{_escaped(bound_username)}</code>\n\n"
                    "本 Bot 只提供通知、Web 审核跳转和经二次确认的只收紧风险操作。\n"
                    "输入 /help 查看完整能力边界。"
                ),
            ),
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
            source_prompt = self._source_prompts.get(callback_key)
            confirmation = self._confirmations.get(callback_key)
            cancellation = self._cancellations.get(callback_key)
        prompt = source_prompt or confirmation or cancellation
        resolved_action = action if prompt is None else prompt.action
        if (
            resolved_action is None
            or prompt is None
            or self._chat_resolver(resolved_action.recipient_id) != str(chat_id)
        ):
            self._answer_callback(callback_id, "按钮已过期或不属于当前用户。", show_alert=True)
            return
        if cancellation is not None:
            self._cancel_confirmation(
                callback_id,
                chat_id,
                message.get("message_id"),
                cancellation,
            )
            return
        if confirmation is not None:
            self._execute_confirmation(
                callback_id,
                chat_id,
                message.get("message_id"),
                confirmation,
                update_id,
            )
            return
        if self._action_handler is None or source_prompt is None:
            self._answer_callback(callback_id, "Trading 暂未准备好。", show_alert=True)
            return
        self._show_confirmation(
            callback_id,
            chat_id,
            message.get("message_id"),
            source_prompt,
        )

    def _show_confirmation(
        self,
        callback_id: str,
        chat_id: int,
        message_id: object,
        prompt: _ActionPrompt,
    ) -> None:
        suffix = prompt.source_callback_key.removeprefix("ca_")
        confirm_key = "cc_" + suffix
        cancel_key = "cx_" + suffix
        with self._lock:
            self._confirmations[confirm_key] = prompt
            self._cancellations[cancel_key] = prompt
        action_label = self._ACTION_LABELS[prompt.action.action]
        keyboard = {
            "inline_keyboard": [
                [{"text": f"确认 · {action_label}", "callback_data": confirm_key}],
                [{"text": "取消并返回", "callback_data": cancel_key}],
            ]
        }
        self._answer_callback(callback_id, "请核对对象、影响范围和版本。", show_alert=False)
        self._edit_message(
            chat_id,
            message_id,
            self._render_confirmation(prompt.action),
            keyboard,
        )

    def _cancel_confirmation(
        self,
        callback_id: str,
        chat_id: int,
        message_id: object,
        prompt: _ActionPrompt,
    ) -> None:
        self._discard_confirmation(prompt)
        self._answer_callback(callback_id, "已取消，未提交任何操作。", show_alert=False)
        self._edit_message(
            chat_id,
            message_id,
            prompt.original_text,
            prompt.original_reply_markup,
        )

    def _execute_confirmation(
        self,
        callback_id: str,
        chat_id: int,
        message_id: object,
        prompt: _ActionPrompt,
        update_id: int,
    ) -> None:
        if self._action_handler is None:
            self._answer_callback(callback_id, "Trading 暂未准备好。", show_alert=True)
            return
        try:
            result = self._action_handler(prompt.action, update_id)
        except Exception:
            logger.exception(
                "Telegram campaign action failed",
                extra={"event": "telegram_action_failed", "action": prompt.action.action},
            )
            result = "未执行: TELEGRAM_ACTION_FAILED"
            self._answer_callback(
                callback_id,
                "操作未执行，请在 Web 控制台确认。",
                show_alert=True,
            )
        else:
            self._answer_callback(callback_id, "请求已处理，请核对权威结果。", show_alert=False)
        self._discard_action_buttons(prompt)
        self._edit_message(
            chat_id,
            message_id,
            self._render_action_result(prompt.action, result),
            {"inline_keyboard": []},
        )

    def _discard_confirmation(self, prompt: _ActionPrompt) -> None:
        suffix = prompt.source_callback_key.removeprefix("ca_")
        with self._lock:
            self._confirmations.pop("cc_" + suffix, None)
            self._cancellations.pop("cx_" + suffix, None)

    def _discard_action_buttons(self, prompt: _ActionPrompt) -> None:
        with self._lock:
            for row in prompt.original_reply_markup.get("inline_keyboard", []):
                for button in row:
                    callback_key = button.get("callback_data")
                    if isinstance(callback_key, str):
                        self._actions.pop(callback_key, None)
                        self._source_prompts.pop(callback_key, None)
                        suffix = callback_key.removeprefix("ca_")
                        self._confirmations.pop("cc_" + suffix, None)
                        self._cancellations.pop("cx_" + suffix, None)

    def _render_confirmation(self, action: TelegramCampaignAction) -> str:
        return _ensure_message_limit(
            "🟠 <b>确认只收紧风险操作</b>\n"
            f"<b>操作</b>　{_escaped(self._ACTION_LABELS[action.action])}\n"
            f"<b>环境</b>　<code>{_escaped(action.environment)}</code>\n"
            f"<b>对象</b>　Campaign <code>{_short_id(action.campaign_id)}</code>\n"
            f"<b>权威版本</b>　v{action.campaign_version}\n\n"
            f"<b>影响范围</b>\n{_escaped(self._ACTION_IMPACTS[action.action])}\n\n"
            "确认时 Trading 会重新校验身份、权限、对象版本和业务不变量；"
            "Telegram 不会绕过权威状态。"
        )

    def _render_action_result(self, action: TelegramCampaignAction, result: str) -> str:
        code: str | None = None
        if result.startswith("未执行:"):
            code = result.partition(":")[2].strip()
        if code:
            result_text = (
                f"{_ERROR_LABELS.get(code, '权威状态拒绝了该操作。')}\n"
                f"错误码　<code>{_escaped(code)}</code>"
            )
            heading = "🔴 <b>操作未执行</b>"
        else:
            result_text = _escaped(result, max_length=1_200)
            heading = "🟢 <b>请求已处理</b>"
        return _ensure_message_limit(
            f"{heading}\n"
            f"<b>操作</b>　{_escaped(self._ACTION_LABELS[action.action])}\n"
            f"<b>对象</b>　Campaign <code>{_short_id(action.campaign_id)}</code>\n"
            f"<b>提交版本</b>　v{action.campaign_version}\n\n"
            f"{result_text}\n\n"
            "按钮已失效。请在 Web 控制台核对最新权威状态。"
        )

    def _edit_message(
        self,
        chat_id: int,
        message_id: object,
        text: str,
        reply_markup: dict[str, Any],
    ) -> None:
        self._safe_call(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": _ensure_message_limit(text),
                "parse_mode": "HTML",
                "link_preview_options": {"is_disabled": True},
                "reply_markup": reply_markup,
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
