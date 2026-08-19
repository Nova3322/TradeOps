from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from trading_control_plane.config import Settings


def _failure_category(error_code: str | None) -> tuple[str, str, str]:
    code = error_code or "READ_ONLY_PROBE_FAILED"
    if any(token in code for token in ("AUTH", "PERMISSION", "UNAUTHORIZED", "SIGNATURE")):
        return (
            "AUTH_OR_PERMISSION_FAILED",
            "只读鉴权或账户权限校验失败。",
            "检查该凭据是否属于目标生产账户且仅具备所需读取权限。",
        )
    if any(token in code for token in ("TARGET_MISSING", "NOT_CONFIGURED", "UNRESOLVED")):
        return (
            "CONFIG_INCOMPLETE",
            "连接配置不完整或生产账户映射无法确认。",
            "补齐明确的内部账户映射或由平台确认账户归属后重试。",
        )
    if "ENVIRONMENT_MISMATCH" in code:
        return (
            "CONFIG_INCOMPLETE",
            "生产环境与只读 API 主机不一致。",
            "由系统管理员核对生产环境和官方只读主机配置。",
        )
    if "RATE_LIMITED" in code:
        return (
            "UPSTREAM_RATE_LIMITED",
            "上游只读接口正在限流，本轮未采信新的账户事实。",  # noqa: RUF001
            "等待系统按退避策略自动重试；持续失败时由系统管理员检查上游配额。",  # noqa: RUF001
        )
    if any(token in code for token in ("UNAVAILABLE", "TIMEOUT", "GATEWAY")):
        return (
            "NETWORK_OR_UPSTREAM_FAILED",
            "官方只读接口或本地只读网关当前不可达。",
            "检查网络、上游状态并执行一次只读重试。",
        )
    if "RESPONSE_INVALID" in code:
        return (
            "UPSTREAM_RESPONSE_INVALID",
            "上游响应未通过严格格式校验, 未采信该数据。",
            "检查上游接口版本和账户类型后重试。",
        )
    return (
        "READ_ONLY_PROBE_FAILED",
        "最近一次只读探针失败, 数据未被标记为可用。",
        "查看无敏感信息的错误代码并重新运行只读探针。",
    )


def _latest_health(
    source_health: Mapping[str, Mapping[str, Any]],
    source: str,
) -> Mapping[str, Any] | None:
    direct = source_health.get(source)
    if direct is not None:
        return direct
    matches = [value for key, value in source_health.items() if key.startswith(f"{source}:")]
    if not matches:
        return None
    if any(item.get("status") == "FAILED" for item in matches):
        return next(item for item in matches if item.get("status") == "FAILED")
    if all(item.get("status") == "SUCCESS" for item in matches):
        return max(matches, key=lambda item: str(item.get("checked_at") or ""))
    return max(matches, key=lambda item: str(item.get("checked_at") or ""))


def _fresh_exchange_health(
    health: Mapping[str, Any] | None,
    *,
    now: datetime | None,
    stale_after_seconds: int | None,
) -> Mapping[str, Any] | None:
    if health is None or now is None or stale_after_seconds is None:
        return health
    raw_checked_at = health.get("checked_at")
    try:
        checked_at = (
            raw_checked_at
            if isinstance(raw_checked_at, datetime)
            else datetime.fromisoformat(str(raw_checked_at))
        )
    except (TypeError, ValueError):
        return {**health, "status": "FAILED", "error_code": "FACT_ADAPTER_STALE"}
    if checked_at.utcoffset() is None or now - checked_at > timedelta(seconds=stale_after_seconds):
        return {**health, "status": "FAILED", "error_code": "FACT_ADAPTER_STALE"}
    return health


def _projection(
    *,
    enabled: bool,
    credential_state: str,
    config_complete: bool,
    health: Mapping[str, Any] | None,
    owner_role: str,
    write_process_enabled: bool,
) -> dict[str, Any]:
    checked_at = None if health is None else health.get("checked_at")
    last_success_at = None if health is None else health.get("last_success_at")
    retry_at = None if health is None else health.get("retry_at")
    consecutive_failures = 0 if health is None else int(health.get("consecutive_failures") or 0)
    error_code = None if health is None else health.get("error_code")
    if credential_state == "MISSING":
        category = "CREDENTIALS_NOT_LOADED"
        reason = "当前 API 进程未加载该来源所需的本地凭据或公开身份配置。"
        next_action = "系统管理员应核对启动配置来源; 不要在页面或日志粘贴凭据。"
        available = False
    elif credential_state == "PARTIAL":
        category = "CONFIG_INCOMPLETE"
        reason = "该来源只加载了部分成对配置, 系统已拒绝建立连接。"
        next_action = "系统管理员在同一受保护配置源中补齐成对配置后重启。"
        available = False
    elif not config_complete:
        category = "CONFIG_INCOMPLETE"
        reason = "凭据或公开身份已加载, 但生产账户映射、Vault 或网络参数不完整。"
        next_action = "系统管理员补齐非敏感账户映射及已授权生产范围后重试。"
        available = False
    elif not enabled:
        category = "EXPLICITLY_DISABLED"
        reason = "只读连接被进程配置显式关闭; 这不等同于交易发送被安全关闭。"
        next_action = "系统管理员只开启对应 READ_ONLY 开关并重启读取服务。"
        available = False
    elif health is None:
        category = "NOT_YET_VERIFIED"
        reason = "配置已加载, 但尚无本进程可核验的只读探针结果。"
        next_action = "启动只读同步并等待一次探针完成。"
        available = False
    elif health.get("status") == "SUCCESS":
        category = "READ_ONLY_CONNECTED"
        reason = "最近一次无副作用只读探针成功。"
        next_action = "无需操作; 写入、下单、签名与资金动作仍由独立 Gate 阻断。"
        available = True
    elif health.get("status") == "SKIPPED" and "RATE_LIMITED" not in str(error_code or ""):
        category = "PROBE_SKIPPED"
        reason = "最近一轮只读探针被跳过, 不能把旧结果当作当前可用。"
        next_action = "核对读取开关和目标映射后重新运行只读同步。"
        available = False
    elif error_code is not None and "HISTORY_INCOMPLETE" in str(error_code):
        category = "READ_ONLY_CONNECTED_HISTORY_INCOMPLETE"
        reason = "当前余额、仓位与订单只读事实已连接; 历史成交或资金费补全暂不完整。"
        next_action = "无需开启交易权限; 等待上游恢复后重试历史补全。新增风险继续安全阻断。"
        available = True
    else:
        category, reason, next_action = _failure_category(
            None if error_code is None else str(error_code)
        )
        available = False
    return {
        "available": available,
        "category": category,
        "reason": reason,
        "owner_role": owner_role,
        "next_action": next_action,
        "checked_at": checked_at,
        "last_success_at": last_success_at,
        "retry_at": retry_at,
        "consecutive_failures": consecutive_failures,
        "error_code": error_code,
        "mode": "READ_ONLY",
        "write_process_enabled": write_process_enabled,
    }


def project_runtime_connections(
    settings: Settings,
    source_health: Mapping[str, Mapping[str, Any]],
    *,
    database_binding_counts: Mapping[str, int] | None = None,
    database_perptape_configured: bool = False,
    now: datetime | None = None,
    fact_stale_after_seconds: int | None = None,
) -> dict[str, dict[str, Any]]:
    binding_counts = database_binding_counts or {}
    notilt_identity = "COMPLETE" if settings.notilt_agent_address else "MISSING"
    perptape_credentials = (
        "COMPLETE" if database_perptape_configured or settings.perptape_api_key else "MISSING"
    )
    exchange_connections = {
        venue: _projection(
            enabled=int(binding_counts.get(venue, 0)) > 0,
            credential_state=("COMPLETE" if int(binding_counts.get(venue, 0)) > 0 else "MISSING"),
            config_complete=int(binding_counts.get(venue, 0)) > 0,
            health=_fresh_exchange_health(
                _latest_health(source_health, venue),
                now=now,
                stale_after_seconds=fact_stale_after_seconds,
            ),
            owner_role="系统管理员",
            write_process_enabled=(
                settings.freqtrade_workers_enabled and int(binding_counts.get(venue, 0)) > 0
            ),
        )
        for venue in ("BINANCE", "HYPERLIQUID", "OKX", "BYBIT")
    }
    return {
        **exchange_connections,
        "PERPTAPE": _projection(
            enabled=(
                settings.runtime_sync_enabled
                if database_perptape_configured
                else bool(settings.perptape_api_key)
            ),
            credential_state=perptape_credentials,
            config_complete=True,
            health=_latest_health(source_health, "PERPTAPE"),
            owner_role="系统管理员",
            write_process_enabled=False,
        ),
        "NOTILT": _projection(
            enabled=settings.notilt_enabled,
            credential_state=notilt_identity,
            config_complete=bool(settings.notilt_vaults),
            health=_latest_health(source_health, "NOTILT"),
            owner_role="资金管理员",
            write_process_enabled=False,
        ),
    }
