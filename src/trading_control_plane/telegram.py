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
from typing import Any, ClassVar, Protocol
from uuid import UUID

from trading_control_plane.domain import ProposalStatus

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
    symbol: str | None = None
    direction: str | None = None
    risk_tier: str | None = None
    quantity: str | None = None
    max_risk: str | None = None


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
class TelegramProposalReviewAction:
    callback_key: str
    recipient_id: UUID
    proposal_id: UUID
    action: str
    proposal_version: int
    environment: str = "LIVE"
    symbol: str | None = None
    direction: str | None = None
    risk_tier: str | None = None
    max_risk: str | None = None
    expires_at: str | None = None


@dataclass(frozen=True)
class _ActionPrompt:
    action: TelegramProposalReviewAction
    source_callback_key: str
    original_text: str
    original_reply_markup: dict[str, Any]


MAX_TELEGRAM_TEXT = 3_500

_STATUS_LABELS: dict[str, str] = {
    ProposalStatus.DRAFT.value: "草稿",
    ProposalStatus.PENDING_REVIEW.value: "等待审核",
    ProposalStatus.APPROVED.value: "已批准",
    ProposalStatus.REJECTED.value: "已拒绝",
    ProposalStatus.EXPIRED.value: "已过期",
}

_DIRECTION_LABELS = {"LONG": "做多", "SHORT": "做空"}
_RISK_LABELS = {"LOW": "低", "MEDIUM": "中", "HIGH": "高"}

_ERROR_LABELS: dict[str, str] = {
    "VERSION_CONFLICT": "对象版本已变化，请重新打开最新通知",
    "ACTION_REFERENCE_EXPIRED": "操作凭证已过期，请使用最新通知",
    "ACTION_REFERENCE_SCOPE_INVALID": "操作凭证不属于当前用户或对象",
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


def _format_deadline(value: datetime | str | None) -> str:
    if value is None or str(value).strip() == "":
        return "未提供"
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
    except ValueError:
        return _escaped(value)
    deadline = parsed.astimezone(UTC)
    return f"{deadline.year}年{deadline.month}月{deadline.day}日 {deadline:%H:%M} UTC"


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


def render_proposal_notification(notification: ProposalNotification) -> str:
    facts = [
        f"<b>环境</b>　<code>{_escaped(notification.environment)}</code>",
        f"<b>对象</b>　提案 <code>{_short_id(notification.proposal_id)}</code>",
        f"<b>状态</b>　{_labeled_code(notification.status, _STATUS_LABELS)} "
        f"· v{notification.proposal_version}",
        f"<b>截止</b>　{_format_deadline(notification.expires_at)}",
        f"<b>币对 / 方向</b>　{_optional(notification.symbol)} / "
        f"{_labeled_code(notification.direction, _DIRECTION_LABELS)}",
        f"<b>风险</b>　{_labeled_code(notification.risk_tier, _RISK_LABELS)}"
        f" · 最大风险 {_optional(notification.max_risk)}",
        f"<b>数量</b>　{_optional(notification.quantity)}",
    ]
    summary = _escaped(notification.summary, max_length=1_800)
    return _ensure_message_limit(
        "🟠 <b>待审核提案</b>\n"
        + "\n".join(facts)
        + f"\n\n<b>说明</b>\n{summary}"
        + f"\n\n<b>通知时间</b>　{_format_time(notification.created_at)}"
        + "\n\n⚠️ 批准或拒绝都需再次确认；Trading 会重新校验身份、独立审核、版本和到期时间。"
    )


def render_help() -> str:
    return (
        "<b>Trading 审核 Bot</b>\n\n"
        "/start　绑定或确认内部成员\n"
        "/todo　查看当前可由你独立审核的冻结提案\n"
        "/status　查看 Bot 能力和安全边界\n"
        "/help　查看本帮助\n\n"
        "<b>唯一可执行动作</b>\n"
        "• 对当前冻结提案批准或拒绝\n"
        "• 每次操作都需要第二次明确确认并写入统一审计\n\n"
        "<b>明确不支持</b>\n"
        "• 不看资金、不下单、不划转资金\n"
        "• 不切换风险 Gate、不改成员权限\n"
        "• 不绕过创建者不可自审、对象版本、到期和服务端权限校验"
    )


def render_status() -> str:
    return (
        "🟢 <b>Trading 审核 Bot 正常运行</b>\n\n"
        "<b>会话</b>　仅限已绑定的内部成员私聊\n"
        "<b>动作</b>　冻结提案批准 / 拒绝，必须二次确认\n"
        "<b>复核</b>　服务端重新检查身份、独立审核、版本与到期\n"
        "<b>禁止</b>　资金、订单、风险开关、权限变更\n"
        "<b>权威状态</b>　以 Trading Web 与 PostgreSQL 为准\n\n"
        "此状态不会显示余额、密钥、Token、私钥或地址。"
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
TelegramTodoResolver = Callable[[str], list[ProposalNotification]]
TelegramActionHandler = Callable[[TelegramProposalReviewAction, int], str]


class TelegramUnavailable(RuntimeError):
    def __init__(self, message: str, *, code: str = "TELEGRAM_UNAVAILABLE") -> None:
        super().__init__(message)
        self.code = code


def _telegram_rejection(error_code: object, description: object) -> TelegramUnavailable:
    status_code = error_code if isinstance(error_code, int) else None
    safe_description = description.casefold() if isinstance(description, str) else ""
    if status_code == 409:
        code = (
            "TELEGRAM_POLLING_CONFLICT"
            if "getupdates" in safe_description or "webhook" in safe_description
            else "TELEGRAM_BOT_API_CONFLICT"
        )
        return TelegramUnavailable("Telegram Bot API polling conflict", code=code)
    if status_code in {401, 403}:
        return TelegramUnavailable(
            "Telegram Bot API authentication was rejected",
            code="TELEGRAM_AUTH_FAILED",
        )
    if status_code == 429:
        return TelegramUnavailable(
            "Telegram Bot API rate limit was reached",
            code="TELEGRAM_RATE_LIMITED",
        )
    return TelegramUnavailable(
        "Telegram Bot API rejected the request",
        code="TELEGRAM_BOT_API_REJECTED",
    )


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
    except urllib.error.HTTPError as exc:
        try:
            rejection = json.loads(exc.read())
        except (json.JSONDecodeError, OSError):
            rejection = {}
        raise _telegram_rejection(
            rejection.get("error_code", exc.code) if isinstance(rejection, dict) else exc.code,
            rejection.get("description") if isinstance(rejection, dict) else None,
        ) from None
    except (TimeoutError, urllib.error.URLError):
        # urllib exceptions may include the request URL, which contains the Bot token.
        raise TelegramUnavailable(
            "Telegram Bot API could not be reached",
            code="TELEGRAM_NETWORK_UNAVAILABLE",
        ) from None
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TelegramUnavailable(
            "Telegram Bot API returned invalid JSON",
            code="TELEGRAM_RESPONSE_INVALID",
        ) from exc
    if not isinstance(result, dict) or result.get("ok") is not True:
        if isinstance(result, dict):
            raise _telegram_rejection(result.get("error_code"), result.get("description"))
        raise TelegramUnavailable(
            "Telegram Bot API returned an invalid response",
            code="TELEGRAM_RESPONSE_INVALID",
        )
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
        "APPROVE_PROPOSAL": "批准冻结提案",
        "REJECT_PROPOSAL": "拒绝冻结提案",
    }
    _ACTION_IMPACTS: ClassVar[dict[str, str]] = {
        "APPROVE_PROPOSAL": "记录一次独立批准。仍需风险检查、短期授权和交易 Gate；不会下单。",
        "REJECT_PROPOSAL": "拒绝并终止当前冻结提案；不会创建订单或资金动作。",
    }
    _COMMANDS: ClassVar[list[dict[str, str]]] = [
        {"command": "start", "description": "绑定或确认内部用户"},
        {"command": "todo", "description": "查看待我审核的冻结提案"},
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
        todo_resolver: TelegramTodoResolver | None = None,
        review_queue_url: str | None = None,
        poll_timeout_seconds: int = 20,
        client: TelegramBotClient | None = None,
    ) -> None:
        super().__init__()
        self._client = client or TelegramBotClient(token)
        self._allowed_username = self._normalize_username(allowed_username)
        self._internal_username = internal_username
        self._binder = binder
        self._chat_resolver = chat_resolver
        self._todo_resolver = todo_resolver
        self._review_queue_url = review_queue_url
        self._poll_timeout_seconds = poll_timeout_seconds
        self._action_handler: TelegramActionHandler | None = None
        self._actions: dict[str, TelegramProposalReviewAction] = {}
        self._source_prompts: dict[str, _ActionPrompt] = {}
        self._confirmations: dict[str, _ActionPrompt] = {}
        self._cancellations: dict[str, _ActionPrompt] = {}
        self._stop_event = threading.Event()
        self._poll_thread: threading.Thread | None = None
        self._last_poll_success_at: datetime | None = None
        self._last_poll_error_at: datetime | None = None
        self._last_poll_error_code: str | None = None
        self._consecutive_poll_failures = 0

    @staticmethod
    def _normalize_username(value: str) -> str:
        return value.strip().removeprefix("@").casefold()

    @property
    def running(self) -> bool:
        return self._poll_thread is not None and self._poll_thread.is_alive()

    def polling_health(self) -> dict[str, object]:
        with self._lock:
            if self._last_poll_error_code is not None:
                state = "DEGRADED"
            elif self._last_poll_success_at is not None:
                state = "HEALTHY"
            else:
                state = "STARTING" if self.running else "STOPPED"
            return {
                "state": state,
                "running": self.running,
                "last_success_at": (
                    None
                    if self._last_poll_success_at is None
                    else self._last_poll_success_at.isoformat()
                ),
                "last_error_at": (
                    None
                    if self._last_poll_error_at is None
                    else self._last_poll_error_at.isoformat()
                ),
                "last_error_code": self._last_poll_error_code,
                "consecutive_failures": self._consecutive_poll_failures,
            }

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
        if notification.environment != "LIVE":
            logger.info(
                "Telegram proposal notification suppressed outside LIVE review scope",
                extra={"event": "telegram_non_live_proposal_suppressed"},
            )
            return
        if any(
            item.notification_id == notification.notification_id for item in self.notifications()
        ):
            return
        keyboard: dict[str, Any]
        pending_actions: dict[str, TelegramProposalReviewAction] = {}
        if notification.status == ProposalStatus.PENDING_REVIEW.value:
            rows: list[list[dict[str, str]]] = []
            for action, label in (
                ("APPROVE_PROPOSAL", "需确认 · 批准"),
                ("REJECT_PROPOSAL", "需确认 · 拒绝"),
            ):
                callback_key = (
                    "pr_"
                    + hashlib.sha256(
                        f"{notification.notification_id}:{action}".encode()
                    ).hexdigest()[:24]
                )
                pending_actions[callback_key] = TelegramProposalReviewAction(
                    callback_key=callback_key,
                    recipient_id=notification.reviewer_id,
                    proposal_id=notification.proposal_id,
                    action=action,
                    proposal_version=notification.proposal_version,
                    environment=notification.environment,
                    symbol=notification.symbol,
                    direction=notification.direction,
                    risk_tier=notification.risk_tier,
                    max_risk=notification.max_risk,
                    expires_at=notification.expires_at,
                )
                rows.append([{"text": label, "callback_data": callback_key}])
            rows.append([{"text": "查看完整冻结快照", "url": notification.review_url}])
            keyboard = {"inline_keyboard": rows}
        else:
            keyboard = {
                "inline_keyboard": [
                    [{"text": "打开 Web 安全审核", "url": notification.review_url}],
                ]
            }
        text = render_proposal_notification(notification)
        delivered = self._send_to_user(
            notification.reviewer_id,
            text,
            keyboard,
        )
        if delivered:
            super().send(notification)
            with self._lock:
                self._actions.update(pending_actions)
                for callback_key, pending_action in pending_actions.items():
                    self._source_prompts[callback_key] = _ActionPrompt(
                        action=pending_action,
                        source_callback_key=callback_key,
                        original_text=text,
                        original_reply_markup=keyboard,
                    )

    def send_campaign(self, notification: CampaignNotification) -> None:
        del notification
        logger.info(
            "Telegram campaign notification suppressed by product boundary",
            extra={"event": "telegram_campaign_suppressed"},
        )

    def send_capital(self, notification: CapitalNotification) -> None:
        del notification
        logger.info(
            "Telegram capital notification suppressed by product boundary",
            extra={"event": "telegram_capital_suppressed"},
        )

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
        except TelegramUnavailable as exc:
            self._record_poll_failure(exc)
            logger.exception(
                "Telegram long polling could not clear the webhook",
                extra={"event": "telegram_poll_start_failed", "error_code": exc.code},
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
                    raise TelegramUnavailable(
                        "Telegram update response is invalid",
                        code="TELEGRAM_RESPONSE_INVALID",
                    )
                self._record_poll_success()
                for update in result:
                    if not isinstance(update, dict):
                        continue
                    update_id = update.get("update_id")
                    if isinstance(update_id, int):
                        offset = max(offset, update_id + 1)
                    self.handle_update(update)
            except TelegramUnavailable as exc:
                failures = self._record_poll_failure(exc)
                logger.exception(
                    "Telegram long polling failed",
                    extra={"event": "telegram_poll_failed", "error_code": exc.code},
                )
                self._stop_event.wait(min(30, 2 ** min(failures, 5)))
            except Exception:
                logger.exception(
                    "Unexpected Telegram update failure",
                    extra={"event": "telegram_update_failed"},
                )
                self._stop_event.wait(1)

    def _record_poll_success(self) -> None:
        with self._lock:
            self._last_poll_success_at = datetime.now(UTC)
            self._last_poll_error_at = None
            self._last_poll_error_code = None
            self._consecutive_poll_failures = 0

    def _record_poll_failure(self, error: TelegramUnavailable) -> int:
        with self._lock:
            self._last_poll_error_at = datetime.now(UTC)
            self._last_poll_error_code = error.code
            self._consecutive_poll_failures += 1
            return self._consecutive_poll_failures

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
            self._safe_call(
                "sendMessage",
                self._message_payload(chat_id, render_help()),
            )
            return
        if command == "/status":
            self._safe_call(
                "sendMessage",
                self._message_payload(chat_id, render_status()),
            )
            return
        if command == "/todo":
            todo_markup: dict[str, Any] | None = None
            if self._todo_resolver is None:
                body = "<b>待审核列表暂不可用</b>\n请在 Web 审核队列查看。"
            else:
                try:
                    todos = self._todo_resolver(str(chat_id))
                except Exception:
                    logger.exception(
                        "Telegram review todo lookup failed",
                        extra={"event": "telegram_todo_failed"},
                    )
                    todos = []
                    body = "<b>待审核列表读取失败</b>\n未执行任何操作，请稍后重试。"
                else:
                    if todos:
                        visible_limit = 10
                        rows = [
                            f"• <b>{_optional(item.symbol)}</b> "
                            f"{_labeled_code(item.direction, _DIRECTION_LABELS)}"
                            f" · {_labeled_code(item.risk_tier, _RISK_LABELS)}风险"
                            f" · 截止 {_format_deadline(item.expires_at)}"
                            for item in todos[:visible_limit]
                        ]
                        omitted = max(0, len(todos) - visible_limit)
                        body = (
                            f"<b>待我审核 · {len(todos)} 项</b>\n\n"
                            + "\n".join(rows)
                            + (
                                f"\n\n仅显示最早到期的 {visible_limit} 项，另有 {omitted} 项。"
                                if omitted
                                else ""
                            )
                            + "\n\n请打开审核队列选择提案；批准 / 拒绝仍需再次确认。"
                        )
                        if self._review_queue_url is not None:
                            todo_markup = {
                                "inline_keyboard": [
                                    [{"text": "打开 Web 审核队列", "url": self._review_queue_url}]
                                ]
                            }
                    else:
                        body = "🟢 <b>当前没有可由你独立审核的冻结提案</b>"
            self._safe_call(
                "sendMessage",
                self._message_payload(chat_id, body, todo_markup),
            )
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
                    "本 Bot 只提供提案提醒、待办列表和经二次确认的冻结提案批准 / 拒绝。\n"
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
        suffix = prompt.source_callback_key.removeprefix("pr_")
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
                "Telegram action failed",
                extra={"event": "telegram_action_failed", "action": prompt.action.action},
            )
            result = "未执行: TELEGRAM_ACTION_FAILED"
            self._answer_callback(
                callback_id,
                "操作未执行，请在 Web 控制台确认。",
                show_alert=True,
            )
        else:
            rejected = result.startswith("未执行:")
            self._answer_callback(
                callback_id,
                (
                    "操作未执行；请查看明确原因并刷新待办。"
                    if rejected
                    else "审核结论已写入 Trading；请核对最新状态。"
                ),
                show_alert=rejected,
            )
        self._discard_action_buttons(prompt)
        self._edit_message(
            chat_id,
            message_id,
            self._render_action_result(prompt.action, result),
            {"inline_keyboard": []},
        )

    def _discard_confirmation(self, prompt: _ActionPrompt) -> None:
        suffix = prompt.source_callback_key.removeprefix("pr_")
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
                        suffix = callback_key.removeprefix("pr_")
                        self._confirmations.pop("cc_" + suffix, None)
                        self._cancellations.pop("cx_" + suffix, None)

    def _render_confirmation(
        self,
        action: TelegramProposalReviewAction,
    ) -> str:
        decision_copy = self._ACTION_IMPACTS[action.action]
        return _ensure_message_limit(
            "🟠 <b>确认提案审核结论</b>\n"
            f"<b>结论</b>　{_escaped(self._ACTION_LABELS[action.action])}\n"
            f"<b>币对 / 方向</b>　{_optional(action.symbol)} / "
            f"{_labeled_code(action.direction, _DIRECTION_LABELS)}\n"
            f"<b>风险</b>　{_labeled_code(action.risk_tier, _RISK_LABELS)}"
            f" · 最大风险 {_optional(action.max_risk)}\n"
            f"<b>截止</b>　{_format_deadline(action.expires_at)}\n"
            f"<b>对象</b>　提案 <code>{_short_id(action.proposal_id)}</code>\n"
            f"<b>环境 / 版本</b>　<code>{_escaped(action.environment)}</code> "
            f"· v{action.proposal_version}\n\n"
            f"<b>影响</b>\n{_escaped(decision_copy)}\n\n"
            "确认时 Trading 会重新校验绑定身份、独立审核权限、创建者限制、"
            "对象版本与到期时间。"
        )

    def _render_action_result(
        self,
        action: TelegramProposalReviewAction,
        result: str,
    ) -> str:
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
            heading = "🟢 <b>审核已记录</b>"
        return _ensure_message_limit(
            f"{heading}\n"
            f"<b>审核结论</b>　{_escaped(self._ACTION_LABELS[action.action])}\n"
            f"<b>币对 / 方向</b>　{_optional(action.symbol)} / "
            f"{_labeled_code(action.direction, _DIRECTION_LABELS)}\n"
            f"<b>对象</b>　提案 <code>{_short_id(action.proposal_id)}</code>\n"
            f"<b>提交版本</b>　v{action.proposal_version}\n\n"
            f"{result_text}\n\n"
            "按钮已失效。批准提案不代表风险检查通过、授权已签发或订单已创建。"
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
