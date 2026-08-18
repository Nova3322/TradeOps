from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from trading_control_plane import authorization_policy, domain, models, rejections
from trading_control_plane import execution_scope as scope_rules
from trading_control_plane.service_component import ServiceComponent
from trading_control_plane.service_domains.risk_policy import active_risk_policy

RISK_RESTORE_COOLDOWN = timedelta(minutes=15)
RISK_RESTORE_TTL = timedelta(hours=24)


class RecoveryRiskService(ServiceComponent):
    @staticmethod
    def _canonical_restore_scopes(
        configured_scopes: tuple[tuple[str, str, str], ...],
        campaigns: list[models.Campaign],
        *,
        required_environment: str | None = None,
    ) -> list[dict[str, str]]:
        scopes = {
            (environment, account_id, venue)
            for environment, account_id, venue in configured_scopes
            if required_environment is None or environment == required_environment
        }
        scopes.update(
            (campaign.environment, campaign.account_id, campaign.venue)
            for campaign in campaigns
            if campaign.status != domain.CampaignStatus.CLOSED.value
            and (required_environment is None or campaign.environment == required_environment)
        )
        return [
            {"environment": environment, "account_id": account_id, "venue": venue}
            for environment, account_id, venue in sorted(scopes)
        ]

    def _risk_restore_blockers(
        self,
        session: Session,
        policy: models.RiskPolicy,
        required_scopes: list[dict[str, str]],
        *,
        require_live_scope: bool = False,
        now: datetime,
    ) -> list[str]:
        blockers: set[str] = set()
        if require_live_scope and not any(
            scope.get("environment") == domain.ExecutionEnvironment.LIVE.value
            for scope in required_scopes
        ):
            blockers.add("LIVE_SCOPE_CONFIGURATION_REQUIRED")
        if policy.system_state == domain.SystemRiskState.KILL_SWITCH.value:
            blockers.add("KILL_SWITCH_MANUAL_RECOVERY_REQUIRED")

        if (
            session.scalar(
                select(models.OrderIntent.intent_id).where(
                    models.OrderIntent.campaign_id.in_(
                        select(models.Campaign.campaign_id).where(
                            models.Campaign.team_id == policy.team_id
                        )
                    ),
                    models.OrderIntent.kind.in_(
                        {domain.IntentKind.INITIAL.value, domain.IntentKind.ADD.value}
                    ),
                    models.OrderIntent.status.in_(scope_rules.ACTIVE_INTENT_STATUSES),
                )
            )
            is not None
        ):
            blockers.add("ACTIVE_NEW_RISK_INTENT")
        if (
            session.scalar(
                select(models.OrderIntent.intent_id).where(
                    models.OrderIntent.campaign_id.in_(
                        select(models.Campaign.campaign_id).where(
                            models.Campaign.team_id == policy.team_id
                        )
                    ),
                    models.OrderIntent.status == domain.OrderIntentStatus.UNKNOWN.value,
                )
            )
            is not None
        ):
            blockers.add("ORDER_INTENT_UNKNOWN")
        if (
            session.scalar(
                select(models.VenueOrder.venue_order_fact_id).where(
                    models.VenueOrder.team_id == policy.team_id,
                    models.VenueOrder.status == domain.VenueOrderStatus.UNKNOWN.value,
                )
            )
            is not None
        ):
            blockers.add("VENUE_ORDER_UNKNOWN")
        if (
            session.scalar(
                select(models.RiskReservation.reservation_id).where(
                    models.RiskReservation.team_id == policy.team_id,
                    models.RiskReservation.status == domain.ReservationStatus.UNKNOWN.value,
                )
            )
            is not None
        ):
            blockers.add("RISK_RESERVATION_UNKNOWN")
        if (
            session.scalar(
                select(models.Campaign.campaign_id).where(
                    models.Campaign.team_id == policy.team_id,
                    models.Campaign.status == domain.CampaignStatus.UNKNOWN.value,
                )
            )
            is not None
        ):
            blockers.add("CAMPAIGN_UNKNOWN")
        if (
            session.scalar(
                select(models.VenueOrder.venue_order_fact_id).where(
                    models.VenueOrder.team_id == policy.team_id,
                    models.VenueOrder.order_intent_id.is_(None),
                    models.VenueOrder.status.in_(
                        {
                            domain.VenueOrderStatus.SENT.value,
                            domain.VenueOrderStatus.PARTIALLY_FILLED.value,
                            domain.VenueOrderStatus.UNKNOWN.value,
                        }
                    ),
                )
            )
            is not None
        ):
            blockers.add("UNBOUND_OPEN_ORDER")

        max_age = timedelta(seconds=policy.max_fact_age_seconds)
        for scope in required_scopes:
            try:
                environment = domain.ExecutionEnvironment(str(scope["environment"]))
                account_id = str(scope["account_id"])
                venue = str(scope["venue"])
            except (KeyError, ValueError):
                blockers.add("CONTROL_SCOPE_INVALID")
                continue
            prefix = f"{environment.value}:{account_id}:{venue}"
            if require_live_scope:
                source_health = session.scalar(
                    select(models.RuntimeSourceHealth).where(
                        models.RuntimeSourceHealth.team_id == policy.team_id,
                        models.RuntimeSourceHealth.source_name == venue,
                        models.RuntimeSourceHealth.account_id == account_id,
                        models.RuntimeSourceHealth.venue == venue,
                    )
                )
                if source_health is None:
                    blockers.add(f"READ_ONLY_SOURCE_MISSING:{prefix}")
                elif source_health.status != "SUCCESS":
                    failure_code = source_health.error_code or "READ_ONLY_PROBE_FAILED"
                    blockers.add(f"READ_ONLY_SOURCE_FAILED:{prefix}:{failure_code}")
                elif scope_rules.fact_is_stale(source_health.checked_at, now, max_age):
                    blockers.add(f"READ_ONLY_SOURCE_STALE:{prefix}")
            equity = session.scalar(
                select(models.AccountEquity).where(
                    models.AccountEquity.team_id == policy.team_id,
                    models.AccountEquity.environment == environment.value,
                    models.AccountEquity.account_id == account_id,
                    models.AccountEquity.venue == venue,
                )
            )
            if equity is None:
                blockers.add(f"ACCOUNT_EQUITY_MISSING:{prefix}")
            elif equity.fact_status != domain.FactStatus.KNOWN.value:
                blockers.add(f"ACCOUNT_EQUITY_UNKNOWN:{prefix}")
            elif scope_rules.fact_is_stale(equity.observed_at, now, max_age):
                blockers.add(f"ACCOUNT_EQUITY_STALE:{prefix}")

            positions = session.scalars(
                select(models.Position).where(
                    models.Position.team_id == policy.team_id,
                    models.Position.environment == environment.value,
                    models.Position.account_id == account_id,
                    models.Position.venue == venue,
                )
            ).all()
            if not positions:
                blockers.add(f"POSITION_FACTS_MISSING:{prefix}")
            for position in positions:
                if position.fact_status != domain.FactStatus.KNOWN.value:
                    blockers.add(f"POSITION_UNKNOWN:{prefix}")
                    continue
                if scope_rules.fact_is_stale(position.observed_at, now, max_age):
                    blockers.add(f"POSITION_STALE:{prefix}")
                if position.quantity == 0:
                    continue
                protection = session.scalar(
                    select(models.ProtectionOrder).where(
                        models.ProtectionOrder.position_id == position.position_id
                    )
                )
                if (
                    protection is None
                    or protection.status != domain.ProtectionStatus.ACTIVE.value
                    or not protection.fully_covered
                    or protection.quantity < abs(position.quantity)
                ):
                    blockers.add(f"PROTECTION_INCOMPLETE:{prefix}")
                elif scope_rules.fact_is_stale(protection.observed_at, now, max_age):
                    blockers.add(f"PROTECTION_STALE:{prefix}")

            execution_scope = scope_rules.scope_key(environment.value, account_id, venue)
            reconciliation = session.scalar(
                select(models.ReconciliationRun)
                .where(
                    models.ReconciliationRun.team_id == policy.team_id,
                    models.ReconciliationRun.execution_scope == execution_scope,
                )
                .order_by(models.ReconciliationRun.completed_at.desc())
                .limit(1)
            )
            latest_source_at = max(
                [
                    policy.updated_at,
                    *([equity.observed_at] if equity is not None else []),
                    *(position.observed_at for position in positions),
                ]
            )
            if (
                reconciliation is None
                or reconciliation.status != domain.ReconciliationStatus.MATCH.value
                or not reconciliation.is_computed
            ):
                blockers.add(f"COMPUTED_RECONCILIATION_MATCH_REQUIRED:{prefix}")
            elif (
                scope_rules.fact_is_stale(reconciliation.completed_at, now, max_age)
                or reconciliation.completed_at < latest_source_at
            ):
                blockers.add(f"RECONCILIATION_STALE:{prefix}")
        return sorted(blockers)

    @staticmethod
    def _risk_restore_condition_details(
        blockers: list[str],
        required_scopes: list[dict[str, str]],
    ) -> list[dict[str, Any]]:
        blocker_set = set(blockers)

        def condition(
            code: str,
            label: str,
            matching: list[str],
            role: str,
            next_action: str,
            scope: dict[str, str] | None = None,
        ) -> dict[str, Any]:
            return {
                "code": code,
                "label": label,
                "status": "BLOCKED" if matching else "PASS",
                "reason": matching if matching else ["CURRENT"],
                "role": role,
                "next_action": next_action if matching else "无需处理",
                "scope": scope,
            }

        details = [
            condition(
                "LIVE_SCOPE_CONFIGURED",
                "生产账户范围已配置",
                [item for item in blockers if item == "LIVE_SCOPE_CONFIGURATION_REQUIRED"],
                "SYSTEM_ADMIN",
                "配置明确的 LIVE 账户与交易所范围后重新检查",
            ),
            condition(
                "SYSTEM_RECOVERABLE",
                "系统不处于需人工处置的紧急停止",
                [item for item in blockers if item == "KILL_SWITCH_MANUAL_RECOVERY_REQUIRED"],
                "SYSTEM_ADMIN",
                "先完成 KILL_SWITCH 人工处置; 不能从本页绕过",
            ),
            condition(
                "NO_ACTIVE_OR_UNKNOWN_OPERATIONS",
                "不存在新增风险在途或结果未知操作",
                [
                    item
                    for item in blockers
                    if item
                    in {
                        "ACTIVE_NEW_RISK_INTENT",
                        "ORDER_INTENT_UNKNOWN",
                        "VENUE_ORDER_UNKNOWN",
                        "RISK_RESERVATION_UNKNOWN",
                        "CAMPAIGN_UNKNOWN",
                        "UNBOUND_OPEN_ORDER",
                    }
                ],
                "OPERATOR",
                "完成订单、仓位、预留与任务对账; 消除未知结果",
            ),
        ]
        for scope in required_scopes:
            prefix = (
                f"{scope.get('environment', '')}:"
                f"{scope.get('account_id', '')}:{scope.get('venue', '')}"
            )
            details.extend(
                [
                    condition(
                        "READ_ONLY_SOURCE_CURRENT",
                        "交易所只读探针已连接且新鲜",
                        [
                            item
                            for item in blockers
                            if item.startswith(
                                (
                                    f"READ_ONLY_SOURCE_MISSING:{prefix}",
                                    f"READ_ONLY_SOURCE_FAILED:{prefix}",
                                    f"READ_ONLY_SOURCE_STALE:{prefix}",
                                )
                            )
                        ],
                        "SYSTEM_ADMIN",
                        "恢复该交易所只读同步并等待一次成功探针",
                        scope,
                    ),
                    condition(
                        "ACCOUNT_EQUITY_CURRENT",
                        "账户权益事实完整且新鲜",
                        [
                            item
                            for item in blockers
                            if item.startswith(
                                (
                                    f"ACCOUNT_EQUITY_MISSING:{prefix}",
                                    f"ACCOUNT_EQUITY_UNKNOWN:{prefix}",
                                    f"ACCOUNT_EQUITY_STALE:{prefix}",
                                )
                            )
                        ],
                        "OPERATOR",
                        "同步该账户最新权益事实",
                        scope,
                    ),
                    condition(
                        "POSITION_AND_PROTECTION_CURRENT",
                        "仓位与保护事实完整且新鲜",
                        [
                            item
                            for item in blockers
                            if item.startswith(
                                (
                                    f"POSITION_FACTS_MISSING:{prefix}",
                                    f"POSITION_UNKNOWN:{prefix}",
                                    f"POSITION_STALE:{prefix}",
                                    f"PROTECTION_INCOMPLETE:{prefix}",
                                    f"PROTECTION_STALE:{prefix}",
                                )
                            )
                        ],
                        "OPERATOR",
                        "同步仓位并补齐有效保护事实",
                        scope,
                    ),
                    condition(
                        "COMPUTED_RECONCILIATION_CURRENT",
                        "最新计算型对账一致",
                        [
                            item
                            for item in blockers
                            if item.startswith(
                                (
                                    f"COMPUTED_RECONCILIATION_MATCH_REQUIRED:{prefix}",
                                    f"RECONCILIATION_STALE:{prefix}",
                                )
                            )
                        ],
                        "OPERATOR",
                        "用最新事实重新运行计算型对账",
                        scope,
                    ),
                ]
            )
        details.append(
            condition(
                "AUTO_ADD_REMAINS_DISABLED",
                "恢复不会开启自动加仓",
                [],
                "SYSTEM",
                "无需处理",
            )
        )
        represented = {
            reason for item in details for reason in item["reason"] if reason != "CURRENT"
        }
        for blocker in sorted(blocker_set - represented):
            details.append(
                condition(
                    blocker.split(":", 1)[0],
                    "其他实时安全条件",
                    [blocker],
                    "OPERATOR",
                    "按精确阻断码处理后重新检查",
                )
            )
        return details

    @staticmethod
    def _risk_restore_request_drifted(
        request: models.RiskControlChangeRequest,
        policy: models.RiskPolicy,
        gate: models.CapabilityGate,
    ) -> bool:
        return bool(
            request.source_policy_id != policy.policy_id
            or request.source_policy_version != policy.version
            or request.source_policy_revision != policy.revision
            or request.source_auto_add_status != gate.status
            or request.source_auto_add_version != gate.version
        )

    def risk_control_status(
        self,
        actor_id: UUID,
        configured_scopes: tuple[tuple[str, str, str], ...],
        *,
        require_live_scope: bool = False,
        now: datetime,
    ) -> dict[str, Any]:
        with self.database.session_factory() as session:
            team = self.transactions.require_role(session, actor_id, "system.view")
            policy = session.scalar(
                select(models.RiskPolicy).where(
                    models.RiskPolicy.team_id == team.team_id,
                    models.RiskPolicy.active,
                )
            )
            gate = session.get(models.CapabilityGate, "AUTO_ADD")
            if gate is None:
                rejections.reject("CAPABILITY_GATE_NOT_FOUND", "AUTO_ADD gate is missing")
            if policy is None:
                return {
                    "policy": None,
                    "auto_add_gate": {
                        "status": gate.status,
                        "version": gate.version,
                        "reason": gate.reason,
                        "operator_id": gate.operator_id,
                        "operator_username": None,
                        "updated_at": gate.updated_at.isoformat(),
                    },
                    "restore_conditions": {
                        "ready": False,
                        "live_scope_required": require_live_scope,
                        "blockers": ["RISK_POLICY_MISSING"],
                        "checks": [],
                        "required_scopes": [],
                        "cooldown_seconds": int(RISK_RESTORE_COOLDOWN.total_seconds()),
                    },
                    "actions": {
                        "configure_policy": {
                            "allowed": any(
                                "risk_policy.manage"
                                in authorization_policy.ROLE_ACTIONS[domain.Role(item.role)]
                                or "*" in authorization_policy.ROLE_ACTIONS[domain.Role(item.role)]
                                for item in session.scalars(
                                    select(models.RoleAssignment).where(
                                        models.RoleAssignment.user_id == actor_id,
                                        models.RoleAssignment.team_id == team.team_id,
                                    )
                                )
                            ),
                            "reason": "RISK_POLICY_MISSING",
                        },
                        "direct_restore": {"allowed": False, "reason": "RISK_POLICY_MISSING"},
                        "request_restore": {"allowed": False, "reason": "RISK_POLICY_MISSING"},
                        "review_restore": {"allowed": False, "reason": "RISK_POLICY_MISSING"},
                        "execute_restore": {"allowed": False, "reason": "RISK_POLICY_MISSING"},
                    },
                    "requests": [],
                    "as_of": now.isoformat(),
                }
            campaigns = session.scalars(
                select(models.Campaign).where(models.Campaign.team_id == team.team_id)
            ).all()
            scopes = self._canonical_restore_scopes(
                configured_scopes,
                list(campaigns),
                required_environment=(
                    domain.ExecutionEnvironment.LIVE.value if require_live_scope else None
                ),
            )
            blockers = self._risk_restore_blockers(
                session,
                policy,
                scopes,
                require_live_scope=require_live_scope,
                now=now,
            )
            if any(
                value is None
                for value in (
                    policy.max_account_risk,
                    policy.max_single_loss,
                    policy.max_consecutive_losses,
                    policy.loss_cooldown_seconds,
                )
            ):
                blockers = ["RISK_LIMITS_UNCONFIGURED", *blockers]
            requests = session.scalars(
                select(models.RiskControlChangeRequest)
                .where(models.RiskControlChangeRequest.team_id == team.team_id)
                .order_by(models.RiskControlChangeRequest.created_at.desc())
                .limit(20)
            ).all()
            request_ids = [item.request_id for item in requests]
            reviews = (
                []
                if not request_ids
                else session.scalars(
                    select(models.Approval)
                    .where(models.Approval.risk_control_change_request_id.in_(request_ids))
                    .order_by(models.Approval.created_at, models.Approval.approval_id)
                ).all()
            )
            identity_ids = {item.requester_id for item in requests}
            identity_ids.update(item.reviewer_id for item in reviews)
            for value in (policy.updated_by, gate.operator_id):
                try:
                    identity_ids.add(UUID(str(value)))
                except (TypeError, ValueError):
                    pass
            usernames = {
                item.user_id: item.username
                for item in session.scalars(
                    select(models.User).where(models.User.user_id.in_(identity_ids))
                ).all()
            }

            def projected_username(value: object) -> str | None:
                try:
                    user_id = UUID(str(value))
                except (TypeError, ValueError):
                    return None
                return usernames.get(user_id)

            reviews_by_request: dict[UUID, list[dict[str, Any]]] = {}
            for review in reviews:
                if review.risk_control_change_request_id is None:
                    continue
                reviews_by_request.setdefault(review.risk_control_change_request_id, []).append(
                    {
                        "reviewer_id": str(review.reviewer_id),
                        "reviewer_username": usernames.get(review.reviewer_id),
                        "decision": review.decision,
                        "reason": review.reason,
                        "created_at": review.created_at.isoformat(),
                    }
                )
            assignments = session.scalars(
                select(models.RoleAssignment).where(
                    models.RoleAssignment.user_id == actor_id,
                    models.RoleAssignment.team_id == team.team_id,
                )
            ).all()
            role_names = {item.role for item in assignments}
            restricted = policy.system_state != domain.SystemRiskState.NORMAL.value

            def request_superseded(item: models.RiskControlChangeRequest) -> bool:
                return bool(
                    (item.change_type == "RESUME_NEW_RISK" and not restricted)
                    or self._risk_restore_request_drifted(item, policy, gate)
                )

            def effective_request_status(item: models.RiskControlChangeRequest) -> str:
                if item.status in {
                    domain.RiskPolicyChangeStatus.PENDING_REVIEW.value,
                    domain.RiskPolicyChangeStatus.APPROVED.value,
                } and (item.expires_at <= now or request_superseded(item)):
                    return domain.RiskPolicyChangeStatus.EXPIRED.value
                return item.status

            active_request = next(
                (
                    item
                    for item in requests
                    if effective_request_status(item)
                    in {
                        domain.RiskPolicyChangeStatus.PENDING_REVIEW.value,
                        domain.RiskPolicyChangeStatus.APPROVED.value,
                    }
                ),
                None,
            )
            is_admin = domain.Role.SYSTEM_ADMIN.value in role_names
            is_operator = domain.Role.OPERATOR.value in role_names
            is_reviewer = domain.Role.REVIEWER.value in role_names
            direct_allowed = is_admin and restricted and not blockers
            request_allowed = is_operator and restricted and active_request is None
            review_allowed = bool(
                is_reviewer
                and active_request is not None
                and active_request.status == domain.RiskPolicyChangeStatus.PENDING_REVIEW.value
                and active_request.requester_id != actor_id
                and not any(
                    review["reviewer_id"] == str(actor_id)
                    for review in reviews_by_request.get(active_request.request_id, [])
                )
            )
            execute_allowed = bool(
                (is_reviewer or is_admin)
                and active_request is not None
                and active_request.status == domain.RiskPolicyChangeStatus.APPROVED.value
                and active_request.requester_id != actor_id
                and (
                    active_request.change_type not in {"RESUME_NEW_RISK", "ENABLE_AUTO_ADD"}
                    or not blockers
                )
                and active_request.execute_after <= now
            )
            return {
                "policy": {
                    "team_id": str(policy.team_id),
                    "policy_id": str(policy.policy_id),
                    "version": policy.version,
                    "revision": policy.revision,
                    "system_state": policy.system_state,
                    "reason": policy.reason,
                    "max_total_risk": str(policy.max_total_risk),
                    "max_account_risk": (
                        None if policy.max_account_risk is None else str(policy.max_account_risk)
                    ),
                    "max_single_loss": (
                        None if policy.max_single_loss is None else str(policy.max_single_loss)
                    ),
                    "max_consecutive_losses": policy.max_consecutive_losses,
                    "loss_cooldown_seconds": policy.loss_cooldown_seconds,
                    "limits_configured": all(
                        value is not None
                        for value in (
                            policy.max_account_risk,
                            policy.max_single_loss,
                            policy.max_consecutive_losses,
                            policy.loss_cooldown_seconds,
                        )
                    ),
                    "max_fact_age_seconds": policy.max_fact_age_seconds,
                    "updated_by": policy.updated_by,
                    "updated_by_username": projected_username(policy.updated_by),
                    "updated_at": policy.updated_at.isoformat(),
                },
                "auto_add_gate": {
                    "status": gate.status,
                    "version": gate.version,
                    "reason": gate.reason,
                    "operator_id": gate.operator_id,
                    "operator_username": projected_username(gate.operator_id),
                    "updated_at": gate.updated_at.isoformat(),
                },
                "restore_conditions": {
                    "ready": not blockers,
                    "live_scope_required": require_live_scope,
                    "blockers": blockers,
                    "checks": self._risk_restore_condition_details(blockers, scopes),
                    "required_scopes": scopes,
                    "cooldown_seconds": int(RISK_RESTORE_COOLDOWN.total_seconds()),
                },
                "actions": {
                    "configure_policy": {
                        "allowed": is_admin,
                        "reason": "READY" if is_admin else "SYSTEM_ADMIN_REQUIRED",
                    },
                    "direct_restore": {
                        "allowed": direct_allowed,
                        "reason": (
                            "READY"
                            if direct_allowed
                            else "SYSTEM_ADMIN_REQUIRED"
                            if not is_admin
                            else "SYSTEM_ALREADY_NORMAL"
                            if not restricted
                            else "REALTIME_CONDITIONS_BLOCKED"
                        ),
                    },
                    "request_restore": {
                        "allowed": request_allowed,
                        "reason": (
                            "READY"
                            if request_allowed
                            else "OPERATOR_REQUIRED"
                            if not is_operator
                            else "SYSTEM_ALREADY_NORMAL"
                            if not restricted
                            else "RESTORE_REQUEST_ALREADY_ACTIVE"
                        ),
                    },
                    "review_restore": {
                        "allowed": review_allowed,
                        "reason": (
                            "READY"
                            if review_allowed
                            else "INDEPENDENT_REVIEWER_REQUIRED"
                            if not is_reviewer
                            else "NO_REVIEWABLE_REQUEST"
                        ),
                    },
                    "execute_restore": {
                        "allowed": execute_allowed,
                        "reason": (
                            "READY" if execute_allowed else "EXECUTION_REQUIREMENTS_NOT_MET"
                        ),
                    },
                },
                "requests": [
                    {
                        "request_id": str(item.request_id),
                        "requester_id": str(item.requester_id),
                        "requester_username": usernames.get(item.requester_id),
                        "status": effective_request_status(item),
                        "superseded_by_control_state": request_superseded(item),
                        "version": item.version,
                        "reason": item.reason,
                        "change_type": item.change_type,
                        "requested_policy": item.requested_policy,
                        "restore_auto_add": item.restore_auto_add,
                        "require_live_scope": item.require_live_scope,
                        "source_policy_id": str(item.source_policy_id),
                        "source_policy_version": item.source_policy_version,
                        "source_policy_revision": item.source_policy_revision,
                        "source_auto_add_status": item.source_auto_add_status,
                        "source_auto_add_version": item.source_auto_add_version,
                        "required_scopes": item.required_scopes,
                        "execute_after": item.execute_after.isoformat(),
                        "expires_at": item.expires_at.isoformat(),
                        "executed_at": (
                            None if item.executed_at is None else item.executed_at.isoformat()
                        ),
                        "resulting_policy_id": (
                            None
                            if item.resulting_policy_id is None
                            else str(item.resulting_policy_id)
                        ),
                        "reviews": reviews_by_request.get(item.request_id, []),
                        "created_at": item.created_at.isoformat(),
                        "updated_at": item.updated_at.isoformat(),
                    }
                    for item in requests
                ],
                "as_of": now.isoformat(),
            }

    def risk_control_change_version(
        self,
        request_id: UUID,
        actor_id: UUID | None = None,
    ) -> int:
        with self.database.session_factory() as session:
            request = session.get(models.RiskControlChangeRequest, request_id)
            if request is None:
                rejections.reject("RISK_RESTORE_NOT_FOUND", "restore request does not exist")
            if actor_id is not None:
                self.transactions.require_role(
                    session,
                    actor_id,
                    "system.view",
                    team_id=request.team_id,
                )
            return request.version

    def create_risk_control_change_request(
        self,
        actor_id: UUID,
        idempotency_key: str,
        *,
        reason: str,
        restore_auto_add: bool,
        change_type: str = "RESUME_NEW_RISK",
        requested_policy: dict[str, str | int] | None = None,
        configured_scopes: tuple[tuple[str, str, str], ...],
        require_live_scope: bool = False,
        now: datetime,
    ) -> UUID:
        operation = "risk.restore.request"
        normalized_change_type = change_type.strip().upper()
        if normalized_change_type not in {
            "POLICY_UPDATE",
            "DISABLE_AUTO_ADD",
            "ENABLE_AUTO_ADD",
            "PAUSE_NEW_RISK",
            "RESUME_NEW_RISK",
        }:
            rejections.reject("RISK_CHANGE_TYPE_INVALID", "risk control change type is unsupported")
        requested_policy = requested_policy or {}
        payload = {
            "reason": reason,
            "restore_auto_add": restore_auto_add,
            "change_type": normalized_change_type,
            "requested_policy": requested_policy,
            "configured_scopes": configured_scopes,
            "require_live_scope": require_live_scope,
        }
        with self.database.session_factory.begin() as session:
            team = self.transactions.require_action_assignment(session, actor_id, operation)
            requester = session.get(models.User, actor_id)
            if requester is None or requester.principal_type != domain.PrincipalType.HUMAN.value:
                rejections.reject(
                    "SERVICE_REQUEST_FORBIDDEN", "risk restoration requires a human requester"
                )
            operator_assignments = session.scalars(
                select(models.RoleAssignment).where(
                    models.RoleAssignment.user_id == actor_id,
                    models.RoleAssignment.team_id == team.team_id,
                    models.RoleAssignment.role == domain.Role.OPERATOR.value,
                )
            ).all()
            if not operator_assignments:
                rejections.reject(
                    "RISK_RESTORE_OPERATOR_REQUIRED",
                    "reviewed risk restoration must be requested by an operator",
                )
            if any(
                not any(
                    (assignment.account_scope is None or assignment.account_scope == account_id)
                    and (assignment.venue_scope is None or assignment.venue_scope == venue)
                    for assignment in operator_assignments
                )
                for _environment, account_id, venue in configured_scopes
            ):
                rejections.reject(
                    "RBAC_DENIED",
                    "risk restoration scope is outside the operator assignment",
                )
            if restore_auto_add:
                rejections.reject(
                    "AUTO_ADD_RESTORE_FORBIDDEN",
                    "risk restoration never enables the AUTO_ADD gate",
                )
            digest, response = self.transactions.idempotency(
                session,
                caller_id=f"{actor_id}:{team.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if response is not None:
                return UUID(str(response["request_id"]))
            self.transactions.lock_risk_capacity(session, team.team_id)
            policy = active_risk_policy(session, team.team_id)
            gate = session.get(models.CapabilityGate, "AUTO_ADD", with_for_update=True)
            if gate is None:
                rejections.reject("CAPABILITY_GATE_NOT_FOUND", "AUTO_ADD gate is missing")
            if (
                normalized_change_type in {"RESUME_NEW_RISK", "ENABLE_AUTO_ADD"}
                and policy.system_state == domain.SystemRiskState.KILL_SWITCH.value
            ):
                rejections.reject(
                    "KILL_SWITCH_MANUAL_RECOVERY_REQUIRED",
                    "KILL_SWITCH cannot be resumed through the reviewed restore workflow",
                )
            if (
                normalized_change_type == "RESUME_NEW_RISK"
                and policy.system_state == domain.SystemRiskState.NORMAL.value
            ):
                rejections.reject(
                    "RISK_CONTROL_ALREADY_NORMAL", "the requested controls are already open"
                )
            if (
                normalized_change_type == "ENABLE_AUTO_ADD"
                and gate.status == domain.CapabilityStatus.ENABLED.value
            ):
                rejections.reject("RISK_CONTROL_ALREADY_NORMAL", "AUTO_ADD is already enabled")
            if (
                normalized_change_type == "DISABLE_AUTO_ADD"
                and gate.status == domain.CapabilityStatus.DISABLED.value
            ):
                rejections.reject("RISK_CONTROL_ALREADY_TIGHTENED", "AUTO_ADD is already disabled")
            if (
                normalized_change_type == "PAUSE_NEW_RISK"
                and policy.system_state != domain.SystemRiskState.NORMAL.value
            ):
                rejections.reject("RISK_CONTROL_ALREADY_TIGHTENED", "new risk is already paused")
            if normalized_change_type == "POLICY_UPDATE":
                required_policy_fields = {
                    "version",
                    "max_total_risk",
                    "max_account_risk",
                    "max_single_loss",
                    "max_consecutive_losses",
                    "loss_cooldown_seconds",
                    "max_fact_age_seconds",
                }
                if set(requested_policy) != required_policy_fields:
                    rejections.reject(
                        "RISK_POLICY_INVALID",
                        "reviewed policy changes require every versioned risk limit",
                    )
            existing_requests = list(
                session.scalars(
                    select(models.RiskControlChangeRequest)
                    .where(
                        models.RiskControlChangeRequest.team_id == team.team_id,
                        models.RiskControlChangeRequest.status.in_(
                            {
                                domain.RiskPolicyChangeStatus.PENDING_REVIEW.value,
                                domain.RiskPolicyChangeStatus.APPROVED.value,
                            }
                        ),
                    )
                    .with_for_update()
                )
            )
            pending = None
            for existing_request in existing_requests:
                superseded = self._risk_restore_request_drifted(existing_request, policy, gate)
                if existing_request.expires_at <= now or superseded:
                    existing_request.status = domain.RiskPolicyChangeStatus.EXPIRED.value
                    existing_request.version += 1
                    existing_request.updated_at = now
                    self.transactions.audit(
                        session,
                        actor_id=str(actor_id),
                        event_type=(
                            "RISK_RESTORE_SUPERSEDED" if superseded else "RISK_RESTORE_EXPIRED"
                        ),
                        object_type="RiskControlChangeRequest",
                        object_id=existing_request.request_id,
                        reason=(
                            "restore request control snapshot was superseded"
                            if superseded
                            else "restore request expired before a replacement was created"
                        ),
                        correlation_id=existing_request.correlation_id,
                        object_version=existing_request.version,
                        idempotency_key=idempotency_key,
                        now=now,
                    )
                else:
                    pending = existing_request.request_id
            if pending is not None:
                rejections.reject(
                    "RISK_RESTORE_ALREADY_PENDING", "a reviewed restore is already active"
                )
            campaigns = list(
                session.scalars(
                    select(models.Campaign).where(models.Campaign.team_id == team.team_id)
                ).all()
            )
            scopes = self._canonical_restore_scopes(
                configured_scopes,
                campaigns,
                required_environment=(
                    domain.ExecutionEnvironment.LIVE.value if require_live_scope else None
                ),
            )
            restoring = normalized_change_type in {"RESUME_NEW_RISK", "ENABLE_AUTO_ADD"}
            last_tighten_at = max(
                policy.updated_at,
                (
                    gate.updated_at
                    if normalized_change_type == "ENABLE_AUTO_ADD"
                    else policy.updated_at
                ),
            )
            request = models.RiskControlChangeRequest(
                team_id=team.team_id,
                requester_id=actor_id,
                status=domain.RiskPolicyChangeStatus.PENDING_REVIEW.value,
                version=1,
                reason=reason,
                restore_auto_add=restore_auto_add,
                change_type=normalized_change_type,
                requested_policy=requested_policy,
                require_live_scope=require_live_scope,
                source_policy_id=policy.policy_id,
                source_policy_version=policy.version,
                source_policy_revision=policy.revision,
                source_auto_add_status=gate.status,
                source_auto_add_version=gate.version,
                required_scopes=scopes,
                resulting_policy_id=None,
                correlation_id=uuid4(),
                execute_after=(
                    max(now, last_tighten_at + RISK_RESTORE_COOLDOWN) if restoring else now
                ),
                expires_at=now + RISK_RESTORE_TTL,
                executed_at=None,
                created_at=now,
                updated_at=now,
            )
            session.add(request)
            session.flush()
            result = {"request_id": str(request.request_id)}
            self.transactions.save_receipt(
                session,
                caller_id=f"{actor_id}:{team.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self.transactions.audit(
                session,
                actor_id=str(actor_id),
                event_type="RISK_RESTORE_REQUESTED",
                object_type="RiskControlChangeRequest",
                object_id=request.request_id,
                reason=reason,
                correlation_id=request.correlation_id,
                object_version=request.version,
                idempotency_key=idempotency_key,
                now=now,
            )
            return request.request_id

    def review_risk_control_change_request(
        self,
        request_id: UUID,
        reviewer_id: UUID,
        decision: domain.ReviewDecision,
        reason: str,
        expected_version: int,
        idempotency_key: str,
        *,
        now: datetime,
    ) -> domain.RiskPolicyChangeStatus:
        operation = "risk.restore.review"
        payload = {
            "request_id": str(request_id),
            "decision": decision.value,
            "reason": reason,
            "expected_version": expected_version,
        }
        expired = False
        result_status: domain.RiskPolicyChangeStatus | None = None
        with self.database.session_factory.begin() as session:
            request = session.get(models.RiskControlChangeRequest, request_id, with_for_update=True)
            if request is None:
                rejections.reject("RISK_RESTORE_NOT_FOUND", "restore request does not exist")
            self.transactions.require_role(
                session, reviewer_id, operation, team_id=request.team_id
            )
            reviewer = session.get(models.User, reviewer_id)
            if reviewer is None or reviewer.principal_type != domain.PrincipalType.HUMAN.value:
                rejections.reject(
                    "SERVICE_REVIEW_FORBIDDEN", "risk restoration requires human reviewers"
                )
            digest, response = self.transactions.idempotency(
                session,
                caller_id=f"{reviewer_id}:{request.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if response is not None:
                return domain.RiskPolicyChangeStatus(str(response["status"]))
            if request.version != expected_version:
                rejections.reject("VERSION_CONFLICT", "restore request changed before review")
            policy = active_risk_policy(session, request.team_id)
            gate = session.get(models.CapabilityGate, "AUTO_ADD")
            if gate is None:
                rejections.reject("CAPABILITY_GATE_NOT_FOUND", "AUTO_ADD gate is missing")
            if (
                request.change_type == "RESUME_NEW_RISK"
                and policy.system_state == domain.SystemRiskState.NORMAL.value
            ) or self._risk_restore_request_drifted(request, policy, gate):
                rejections.reject(
                    "RISK_RESTORE_CONTROL_DRIFT",
                    "restore request no longer matches current controls",
                )
            if request.requester_id == reviewer_id:
                rejections.reject("SELF_REVIEW_FORBIDDEN", "requester cannot review their restore")
            if request.expires_at <= now:
                request.status = domain.RiskPolicyChangeStatus.EXPIRED.value
                request.version += 1
                request.updated_at = now
                expired = True
            else:
                if request.status != domain.RiskPolicyChangeStatus.PENDING_REVIEW.value:
                    rejections.reject(
                        "RISK_RESTORE_NOT_REVIEWABLE", "restore request is not pending"
                    )
                duplicate = session.scalar(
                    select(models.Approval).where(
                        models.Approval.risk_control_change_request_id == request_id,
                        models.Approval.reviewer_id == reviewer_id,
                    )
                )
                if duplicate is not None:
                    rejections.reject("REVIEW_ALREADY_RECORDED", "reviewer already voted")
                session.add(
                    models.Approval(
                        proposal_id=None,
                        transfer_proposal_id=None,
                        risk_control_change_request_id=request_id,
                        reviewer_id=reviewer_id,
                        decision=decision.value,
                        reason=reason,
                        created_at=now,
                    )
                )
                session.flush()
                if decision is domain.ReviewDecision.REJECT:
                    request.status = domain.RiskPolicyChangeStatus.REJECTED.value
                else:
                    approvals = session.scalar(
                        select(func.count())
                        .select_from(models.Approval)
                        .where(
                            models.Approval.risk_control_change_request_id == request_id,
                            models.Approval.decision == domain.ReviewDecision.APPROVE.value,
                        )
                    )
                    if int(approvals or 0) >= 1:
                        request.status = domain.RiskPolicyChangeStatus.APPROVED.value
                request.version += 1
                request.updated_at = now
                result_status = domain.RiskPolicyChangeStatus(request.status)
                response_value = {"status": request.status, "version": request.version}
                self.transactions.save_receipt(
                    session,
                    caller_id=f"{reviewer_id}:{request.team_id}",
                    operation=operation,
                    idempotency_key=idempotency_key,
                    semantic_hash=digest,
                    response=response_value,
                    now=now,
                )
                self.transactions.audit(
                    session,
                    actor_id=str(reviewer_id),
                    event_type="RISK_RESTORE_REVIEWED",
                    object_type="RiskControlChangeRequest",
                    object_id=request.request_id,
                    reason=f"{decision.value}: {reason}",
                    correlation_id=request.correlation_id,
                    object_version=request.version,
                    idempotency_key=idempotency_key,
                    now=now,
                )
        if expired:
            rejections.reject("RISK_RESTORE_EXPIRED", "restore request expired before review")
        if result_status is None:
            raise RuntimeError("risk restore review completed without a status")
        return result_status

    def execute_risk_control_change_request(
        self,
        request_id: UUID,
        actor_id: UUID,
        expected_version: int,
        idempotency_key: str,
        configured_scopes: tuple[tuple[str, str, str], ...],
        *,
        require_live_scope: bool = False,
        now: datetime,
    ) -> UUID:
        operation = "risk.restore.execute"
        payload = {
            "request_id": str(request_id),
            "expected_version": expected_version,
            "configured_scopes": configured_scopes,
            "require_live_scope": require_live_scope,
        }
        with self.database.session_factory.begin() as session:
            request = session.get(models.RiskControlChangeRequest, request_id, with_for_update=True)
            if request is None:
                rejections.reject("RISK_RESTORE_NOT_FOUND", "restore request does not exist")
            self.transactions.require_role(session, actor_id, operation, team_id=request.team_id)
            executor = session.get(models.User, actor_id)
            if executor is None or executor.principal_type != domain.PrincipalType.HUMAN.value:
                rejections.reject(
                    "SERVICE_EXECUTION_FORBIDDEN", "risk restoration requires a human executor"
                )
            digest, response = self.transactions.idempotency(
                session,
                caller_id=f"{actor_id}:{request.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if response is not None:
                return UUID(str(response["policy_id"]))
            if request.version != expected_version:
                rejections.reject("VERSION_CONFLICT", "restore request changed before execution")
            if request.status != domain.RiskPolicyChangeStatus.APPROVED.value:
                rejections.reject(
                    "RISK_RESTORE_NOT_APPROVED", "an independent approval is required"
                )
            if request.expires_at <= now:
                rejections.reject(
                    "RISK_RESTORE_EXPIRED", "restore request expired before execution"
                )
            if request.execute_after > now:
                rejections.reject("RISK_RESTORE_COOLDOWN", "restore cooldown has not completed")
            approvals = session.scalars(
                select(models.Approval).where(
                    models.Approval.risk_control_change_request_id == request_id,
                    models.Approval.decision == domain.ReviewDecision.APPROVE.value,
                )
            ).all()
            if len({approval.reviewer_id for approval in approvals}) < 1:
                rejections.reject(
                    "RISK_RESTORE_NOT_APPROVED", "an independent approval is required"
                )
            if request.requester_id == actor_id:
                rejections.reject(
                    "SELF_EXECUTION_FORBIDDEN", "requester cannot execute their restore"
                )
            self.transactions.lock_risk_capacity(session, request.team_id)
            policy = active_risk_policy(session, request.team_id)
            gate = session.get(models.CapabilityGate, "AUTO_ADD", with_for_update=True)
            if gate is None:
                rejections.reject("CAPABILITY_GATE_NOT_FOUND", "AUTO_ADD gate is missing")
            if self._risk_restore_request_drifted(request, policy, gate):
                rejections.reject(
                    "RISK_RESTORE_CONTROL_DRIFT", "risk controls changed after the request"
                )
            campaigns = list(
                session.scalars(
                    select(models.Campaign).where(models.Campaign.team_id == request.team_id)
                ).all()
            )
            current_scopes = self._canonical_restore_scopes(
                configured_scopes,
                campaigns,
                required_environment=(
                    domain.ExecutionEnvironment.LIVE.value if request.require_live_scope else None
                ),
            )
            if current_scopes != request.required_scopes:
                rejections.reject(
                    "RISK_RESTORE_SCOPE_DRIFT", "controlled scopes changed after the request"
                )
            if request.require_live_scope != require_live_scope:
                rejections.reject(
                    "RISK_RESTORE_SCOPE_DRIFT",
                    "LIVE scope requirement changed after the request",
                )
            if request.change_type in {"RESUME_NEW_RISK", "ENABLE_AUTO_ADD"}:
                blockers = self._risk_restore_blockers(
                    session,
                    policy,
                    request.required_scopes,
                    require_live_scope=request.require_live_scope,
                    now=now,
                )
                if blockers:
                    rejections.reject("RISK_RESTORE_BLOCKED", ",".join(blockers))
            resulting_policy = policy
            if request.change_type == "RESUME_NEW_RISK":
                policy.active = False
                next_revision = policy.revision + 1
                resulting_policy = models.RiskPolicy(
                    team_id=request.team_id,
                    version=f"restore-{next_revision}-{request.request_id.hex[:12]}",
                    revision=next_revision,
                    system_state=domain.SystemRiskState.NORMAL.value,
                    max_total_risk=policy.max_total_risk,
                    max_account_risk=policy.max_account_risk,
                    max_single_loss=policy.max_single_loss,
                    max_consecutive_losses=policy.max_consecutive_losses,
                    loss_cooldown_seconds=policy.loss_cooldown_seconds,
                    max_fact_age_seconds=policy.max_fact_age_seconds,
                    reason=request.reason,
                    active=True,
                    updated_by=str(actor_id),
                    updated_at=now,
                )
                session.add(resulting_policy)
                session.flush()
            elif request.change_type == "POLICY_UPDATE":
                requested = request.requested_policy
                values = (
                    Decimal(str(requested.get("max_total_risk", "0"))),
                    Decimal(str(requested.get("max_account_risk", "0"))),
                    Decimal(str(requested.get("max_single_loss", "0"))),
                )
                total, account, single = values
                consecutive = int(requested.get("max_consecutive_losses", 0))
                cooldown = int(requested.get("loss_cooldown_seconds", 0))
                max_age = int(requested.get("max_fact_age_seconds", 0))
                version = str(requested.get("version", ""))
                if (
                    not version
                    or len(version) > 120
                    or any(not value.is_finite() or value <= 0 for value in values)
                    or account > total
                    or single > account
                    or consecutive <= 0
                    or cooldown <= 0
                    or max_age <= 0
                ):
                    rejections.reject("RISK_POLICY_INVALID", "reviewed risk limits are invalid")
                if session.scalar(
                    select(models.RiskPolicy.policy_id).where(
                        models.RiskPolicy.team_id == request.team_id,
                        models.RiskPolicy.version == version,
                    )
                ):
                    rejections.reject(
                        "RISK_POLICY_VERSION_CONFLICT", "risk policy version already exists"
                    )
                policy.active = False
                resulting_policy = models.RiskPolicy(
                    team_id=request.team_id,
                    version=version,
                    revision=policy.revision + 1,
                    system_state=policy.system_state,
                    max_total_risk=total,
                    max_account_risk=account,
                    max_single_loss=single,
                    max_consecutive_losses=consecutive,
                    loss_cooldown_seconds=cooldown,
                    max_fact_age_seconds=max_age,
                    reason=request.reason,
                    active=True,
                    updated_by=str(actor_id),
                    updated_at=now,
                )
                session.add(resulting_policy)
                session.flush()
            elif request.change_type == "PAUSE_NEW_RISK":
                if policy.system_state == domain.SystemRiskState.KILL_SWITCH.value:
                    rejections.reject(
                        "RISK_CONTROL_ALREADY_TIGHTENED", "KILL_SWITCH is already stricter"
                    )
                policy.system_state = domain.SystemRiskState.REDUCE_ONLY.value
                policy.revision += 1
                policy.reason = request.reason
                policy.updated_by = str(actor_id)
                policy.updated_at = now
            elif request.change_type == "ENABLE_AUTO_ADD":
                if policy.system_state != domain.SystemRiskState.NORMAL.value:
                    rejections.reject("RISK_RESTORE_BLOCKED", "risk policy must be NORMAL")
                gate.status = domain.CapabilityStatus.ENABLED.value
                gate.reason = request.reason
                gate.operator_id = str(actor_id)
                gate.version += 1
                gate.updated_at = now
            elif request.change_type == "DISABLE_AUTO_ADD":
                gate.status = domain.CapabilityStatus.DISABLED.value
                gate.reason = request.reason
                gate.operator_id = str(actor_id)
                gate.version += 1
                gate.updated_at = now
            authorizations = session.scalars(
                select(models.TradingAuthorization)
                .where(
                    models.TradingAuthorization.team_id == request.team_id,
                    models.TradingAuthorization.active,
                )
                .order_by(models.TradingAuthorization.authorization_id)
                .with_for_update()
            ).all()
            for authorization in authorizations:
                authorization.active = False
                if authorization.add_revoked_at is None:
                    authorization.add_revoked_at = now
            request.status = domain.RiskPolicyChangeStatus.EXECUTED.value
            request.resulting_policy_id = resulting_policy.policy_id
            request.executed_at = now
            request.updated_at = now
            request.version += 1
            result = {"policy_id": str(resulting_policy.policy_id), "request_id": str(request_id)}
            self.transactions.save_receipt(
                session,
                caller_id=f"{actor_id}:{request.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self.transactions.audit(
                session,
                actor_id=str(actor_id),
                event_type="RISK_CONTROL_CHANGE_EXECUTED",
                object_type="RiskControlChangeRequest",
                object_id=request.request_id,
                reason=request.reason,
                correlation_id=request.correlation_id,
                object_version=request.version,
                idempotency_key=idempotency_key,
                now=now,
            )
            return resulting_policy.policy_id

    def direct_restore_risk_controls(
        self,
        actor_id: UUID,
        idempotency_key: str,
        *,
        reason: str,
        configured_scopes: tuple[tuple[str, str, str], ...],
        require_live_scope: bool = True,
        now: datetime,
    ) -> UUID:
        operation = "risk.restore.direct"
        payload = {
            "reason": reason,
            "configured_scopes": configured_scopes,
            "require_live_scope": require_live_scope,
        }
        with self.database.session_factory.begin() as session:
            team = self.transactions.require_role(session, actor_id, operation)
            assignments = session.scalars(
                select(models.RoleAssignment).where(
                    models.RoleAssignment.user_id == actor_id,
                    models.RoleAssignment.team_id == team.team_id,
                )
            ).all()
            if not any(item.role == domain.Role.SYSTEM_ADMIN.value for item in assignments):
                rejections.reject(
                    "RISK_RESTORE_ADMIN_REQUIRED",
                    "direct risk restoration requires SYSTEM_ADMIN",
                )
            actor = session.get(models.User, actor_id)
            if actor is None or actor.principal_type != domain.PrincipalType.HUMAN.value:
                rejections.reject(
                    "SERVICE_EXECUTION_FORBIDDEN", "direct restoration requires a human"
                )
            digest, response = self.transactions.idempotency(
                session,
                caller_id=f"{actor_id}:{team.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if response is not None:
                return UUID(str(response["policy_id"]))
            self.transactions.lock_risk_capacity(session, team.team_id)
            policy = active_risk_policy(session, team.team_id)
            if policy.system_state == domain.SystemRiskState.NORMAL.value:
                rejections.reject("RISK_CONTROL_ALREADY_NORMAL", "risk policy is already normal")
            gate = session.get(models.CapabilityGate, "AUTO_ADD", with_for_update=True)
            if gate is None:
                rejections.reject("CAPABILITY_GATE_NOT_FOUND", "AUTO_ADD gate is missing")
            campaigns = list(
                session.scalars(
                    select(models.Campaign).where(models.Campaign.team_id == team.team_id)
                ).all()
            )
            scopes = self._canonical_restore_scopes(
                configured_scopes,
                campaigns,
                required_environment=(
                    domain.ExecutionEnvironment.LIVE.value if require_live_scope else None
                ),
            )
            blockers = self._risk_restore_blockers(
                session,
                policy,
                scopes,
                require_live_scope=require_live_scope,
                now=now,
            )
            if blockers:
                rejections.reject("RISK_RESTORE_BLOCKED", ",".join(blockers))
            policy.active = False
            next_revision = policy.revision + 1
            restored = models.RiskPolicy(
                team_id=team.team_id,
                version=f"direct-restore-{next_revision}-{uuid4().hex[:12]}",
                revision=next_revision,
                system_state=domain.SystemRiskState.NORMAL.value,
                max_total_risk=policy.max_total_risk,
                max_account_risk=policy.max_account_risk,
                max_single_loss=policy.max_single_loss,
                max_consecutive_losses=policy.max_consecutive_losses,
                loss_cooldown_seconds=policy.loss_cooldown_seconds,
                max_fact_age_seconds=policy.max_fact_age_seconds,
                reason=reason,
                active=True,
                updated_by=str(actor_id),
                updated_at=now,
            )
            session.add(restored)
            session.flush()
            pending_requests = session.scalars(
                select(models.RiskControlChangeRequest)
                .where(
                    models.RiskControlChangeRequest.team_id == team.team_id,
                    models.RiskControlChangeRequest.status.in_(
                        {
                            domain.RiskPolicyChangeStatus.PENDING_REVIEW.value,
                            domain.RiskPolicyChangeStatus.APPROVED.value,
                        }
                    ),
                )
                .with_for_update()
            ).all()
            for pending_request in pending_requests:
                pending_request.status = domain.RiskPolicyChangeStatus.EXPIRED.value
                pending_request.version += 1
                pending_request.resulting_policy_id = restored.policy_id
                pending_request.updated_at = now
                self.transactions.audit(
                    session,
                    actor_id=str(actor_id),
                    event_type="RISK_RESTORE_SUPERSEDED",
                    object_type="RiskControlChangeRequest",
                    object_id=pending_request.request_id,
                    reason="direct administrator restoration superseded the request",
                    correlation_id=pending_request.correlation_id,
                    object_version=pending_request.version,
                    idempotency_key=idempotency_key,
                    now=now,
                )
            authorizations = session.scalars(
                select(models.TradingAuthorization)
                .where(
                    models.TradingAuthorization.team_id == team.team_id,
                    models.TradingAuthorization.active,
                )
                .order_by(models.TradingAuthorization.authorization_id)
                .with_for_update()
            ).all()
            for authorization in authorizations:
                authorization.active = False
                if authorization.add_revoked_at is None:
                    authorization.add_revoked_at = now
            result = {"policy_id": str(restored.policy_id)}
            self.transactions.save_receipt(
                session,
                caller_id=f"{actor_id}:{team.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self.transactions.audit(
                session,
                actor_id=str(actor_id),
                event_type="RISK_RESTORE_DIRECT_EXECUTED",
                object_type="RiskPolicy",
                object_id=restored.policy_id,
                reason=reason,
                correlation_id=uuid4(),
                object_version=restored.revision,
                idempotency_key=idempotency_key,
                now=now,
            )
            return restored.policy_id
