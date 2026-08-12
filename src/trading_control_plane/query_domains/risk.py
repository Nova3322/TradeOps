from __future__ import annotations

from trading_control_plane.query_component import QueryComponent

# ruff: noqa: F403, F405
from trading_control_plane.query_core import *


class RiskQueries(QueryComponent):
    def list_exceptions(self, user_id: UUID, *, now: datetime) -> list[dict[str, Any]]:
        exceptions: list[dict[str, Any]] = []
        _workspace_id, team_id = self.facade._active_scope_ids(user_id)
        with self.database.session_factory() as session:
            risk_policy = session.scalar(
                select(RiskPolicy).where(
                    RiskPolicy.team_id == team_id,
                    RiskPolicy.active,
                )
            )
            max_fact_age = timedelta(
                seconds=(risk_policy.max_fact_age_seconds if risk_policy is not None else 300)
            )
        for campaign in self.facade.list_campaigns(user_id):
            if campaign["status"] == "CLOSED":
                continue
            detail = self.facade.campaign_detail(user_id, UUID(str(campaign["campaign_id"])))
            campaign_id = str(campaign["campaign_id"])
            campaign_occurred_at = str(campaign["updated_at"] or campaign["created_at"])
            if campaign["status"] == "UNKNOWN":
                exceptions.append(
                    self._exception(
                        campaign_id,
                        "CAMPAIGN_UNKNOWN",
                        "BLOCKING",
                        occurred_at=campaign_occurred_at,
                    )
                )
            for reservation in detail["reservations"]:
                if reservation["status"] == "UNKNOWN":
                    exceptions.append(
                        self._exception(
                            campaign_id,
                            "RISK_RESERVATION_UNKNOWN",
                            "BLOCKING",
                            object_id=str(reservation["reservation_id"]),
                            occurred_at=str(reservation["updated_at"]),
                        )
                    )
            for intent in detail["intents"]:
                if intent["status"] == "DISPATCHING":
                    exceptions.append(
                        self._exception(
                            campaign_id,
                            "ORDER_DISPATCH_UNRESOLVED",
                            "BLOCKING",
                            object_id=str(intent["intent_id"]),
                            occurred_at=str(
                                intent["dispatch"]["started_at"] or intent["updated_at"]
                            ),
                        )
                    )
                if intent["status"] == "UNKNOWN":
                    exceptions.append(
                        self._exception(
                            campaign_id,
                            "ORDER_INTENT_UNKNOWN",
                            "BLOCKING",
                            object_id=str(intent["intent_id"]),
                            occurred_at=str(intent["updated_at"]),
                        )
                    )
            position = detail["position"]
            if position is None or position["fact_status"] == "UNKNOWN":
                exceptions.append(
                    self._exception(
                        campaign_id,
                        "POSITION_UNKNOWN",
                        "BLOCKING",
                        occurred_at=campaign_occurred_at,
                    )
                )
            else:
                position_observed_at = datetime.fromisoformat(str(position["observed_at"]))
                if fact_is_stale(position_observed_at, now, max_fact_age):
                    exceptions.append(
                        self._exception(
                            campaign_id,
                            "POSITION_STALE",
                            "BLOCKING",
                            object_id=str(position["position_id"]),
                            occurred_at=(position_observed_at + max_fact_age).isoformat(),
                            details=[
                                f"observed_at={position['observed_at']}",
                                f"max_age_seconds={int(max_fact_age.total_seconds())}",
                            ],
                        )
                    )
            if (
                position is not None
                and position["fact_status"] != "UNKNOWN"
                and Decimal(str(position["quantity"])) != 0
            ):
                protection = detail["protection"]
                if protection is None or protection["status"] == "UNKNOWN":
                    exceptions.append(
                        self._exception(
                            campaign_id,
                            "PROTECTION_UNKNOWN",
                            "BLOCKING",
                            occurred_at=str(position["observed_at"]),
                        )
                    )
                else:
                    protection_observed_at = datetime.fromisoformat(str(protection["observed_at"]))
                    if fact_is_stale(protection_observed_at, now, max_fact_age):
                        exceptions.append(
                            self._exception(
                                campaign_id,
                                "PROTECTION_STALE",
                                "BLOCKING",
                                object_id=str(protection["protection_id"]),
                                occurred_at=(protection_observed_at + max_fact_age).isoformat(),
                                details=[
                                    f"observed_at={protection['observed_at']}",
                                    f"max_age_seconds={int(max_fact_age.total_seconds())}",
                                ],
                            )
                        )
                    if not protection["fully_covered"]:
                        exceptions.append(
                            self._exception(
                                campaign_id,
                                "PROTECTION_INSUFFICIENT",
                                "BLOCKING",
                                object_id=str(protection["protection_id"]),
                                occurred_at=str(protection["observed_at"]),
                            )
                        )
            reconciliation = detail["reconciliation"]
            if reconciliation is not None and reconciliation["status"] != "MATCH":
                exceptions.append(
                    self._exception(
                        campaign_id,
                        f"RECONCILIATION_{reconciliation['status']}",
                        "BLOCKING",
                        occurred_at=str(reconciliation["completed_at"]),
                        details=list(reconciliation["differences"]),
                    )
                )
            elif reconciliation is not None:
                completed_at = datetime.fromisoformat(str(reconciliation["completed_at"]))
                newer_facts: list[tuple[str, datetime]] = []
                if (
                    position is not None
                    and datetime.fromisoformat(str(position["observed_at"])) > completed_at
                ):
                    newer_facts.append(
                        (
                            "POSITION_FACT_NEWER",
                            datetime.fromisoformat(str(position["observed_at"])),
                        )
                    )
                if (
                    detail["intents"]
                    and datetime.fromisoformat(str(detail["intents"][-1]["updated_at"]))
                    > completed_at
                ):
                    newer_facts.append(
                        (
                            "ORDER_INTENT_NEWER",
                            datetime.fromisoformat(str(detail["intents"][-1]["updated_at"])),
                        )
                    )
                if newer_facts:
                    exceptions.append(
                        self._exception(
                            campaign_id,
                            "RECONCILIATION_STALE",
                            "BLOCKING",
                            object_id=str(reconciliation["reconciliation_id"]),
                            occurred_at=min(value for _name, value in newer_facts).isoformat(),
                            details=[name for name, _value in newer_facts],
                        )
                    )
        guidance = {
            "CAMPAIGN_UNKNOWN": (
                "CRITICAL",
                "交易运维",
                "任务结果未知会阻断新增风险",
                "核对交易所事实并完成计算型对账",
            ),
            "RISK_RESERVATION_UNKNOWN": (
                "CRITICAL",
                "交易运维",
                "风险占用无法安全释放",
                "核对订单结果并重新计算风险预留",
            ),
            "ORDER_INTENT_UNKNOWN": (
                "CRITICAL",
                "交易运维",
                "可能存在未确认订单结果",
                "先同步订单与成交，再处理未知意图",  # noqa: RUF001
            ),
            "POSITION_UNKNOWN": (
                "CRITICAL",
                "交易运维",
                "无法确认真实仓位",
                "同步该账户与标的的仓位事实",
            ),
            "POSITION_STALE": (
                "HIGH",
                "交易运维",
                "仓位事实可能不再代表当前状态",
                "刷新仓位后重新对账",
            ),
            "PROTECTION_UNKNOWN": (
                "CRITICAL",
                "交易运维",
                "无法确认持仓保护是否有效",
                "同步或补齐保护单事实",
            ),
            "PROTECTION_STALE": (
                "HIGH",
                "交易运维",
                "保护事实可能已经变化",
                "刷新保护单并确认足额覆盖",
            ),
            "PROTECTION_INSUFFICIENT": (
                "CRITICAL",
                "交易运维",
                "现有持仓未被足额保护",
                "按交易任务允许的降险路径补齐保护或退出",
            ),
            "RECONCILIATION_STALE": (
                "HIGH",
                "交易运维",
                "旧对账早于最新仓位或订单事实",
                "用最新事实重新运行计算型对账",
            ),
        }
        for item in exceptions:
            code = str(item["code"])
            default = ("HIGH", "交易运维", "运行事实存在不一致", "检查差异并重新运行计算型对账")
            severity, owner_role, impact, next_action = guidance.get(code, default)
            item.update(
                {
                    "severity": severity,
                    "last_checked_at": now.isoformat(),
                    "impact": impact,
                    "owner_role": owner_role,
                    "next_action": next_action,
                    "action_available": False,
                    "action_unavailable_reason": (
                        "告警详情只提供事实与路径；必须回到受影响交易任务按后端安全条件处理"  # noqa: RUF001
                    ),
                }
            )
        return exceptions

    @staticmethod
    def _exception(
        campaign_id: str,
        code: str,
        severity: str,
        *,
        object_id: str | None = None,
        details: list[str] | None = None,
        occurred_at: str | None = None,
    ) -> dict[str, Any]:
        return {
            "campaign_id": campaign_id,
            "object_id": object_id or campaign_id,
            "code": code,
            "severity": severity,
            "details": details or [],
            "occurred_at": occurred_at,
        }
