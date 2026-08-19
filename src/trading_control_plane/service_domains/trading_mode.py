from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from trading_control_plane import authorization_policy, domain, models, rejections
from trading_control_plane import execution_scope as scope_rules
from trading_control_plane.service_component import ServiceComponent


class TradingModeService(ServiceComponent):
    """Own the single team execution mode boundary: TESTNET or LIVE."""

    _CONFIRMATIONS: ClassVar[dict[str, str]] = {
        domain.TeamExecutionMode.TESTNET.value: "SWITCH_TO_TESTNET",
        domain.TeamExecutionMode.LIVE.value: "I_CONFIRM_LIVE_PRODUCTION_MONEY",
    }

    @staticmethod
    def _account_environment_valid(account: models.ExchangeAccount) -> bool:
        metadata = account.credential_metadata or {}
        return metadata.get("environment") == account.environment

    @staticmethod
    def _source_blockers(session: Session, team_id: UUID, environment: str) -> list[dict[str, Any]]:
        blockers: list[dict[str, Any]] = []
        active_intents = int(
            session.scalar(
                select(func.count(models.OrderIntent.intent_id))
                .join(
                    models.Campaign, models.Campaign.campaign_id == models.OrderIntent.campaign_id
                )
                .where(
                    models.Campaign.team_id == team_id,
                    models.Campaign.environment == environment,
                    models.OrderIntent.status.in_(scope_rules.ACTIVE_INTENT_STATUSES),
                )
            )
            or 0
        )
        if active_intents:
            blockers.append({"code": "SOURCE_ORDER_INTENTS_ACTIVE", "count": active_intents})

        open_orders = int(
            session.scalar(
                select(func.count(models.VenueOrder.venue_order_fact_id)).where(
                    models.VenueOrder.team_id == team_id,
                    models.VenueOrder.environment == environment,
                    models.VenueOrder.status.in_(
                        (
                            domain.VenueOrderStatus.SENT.value,
                            domain.VenueOrderStatus.PARTIALLY_FILLED.value,
                            domain.VenueOrderStatus.UNKNOWN.value,
                        )
                    ),
                )
            )
            or 0
        )
        if open_orders:
            blockers.append({"code": "SOURCE_ORDERS_OPEN_OR_UNKNOWN", "count": open_orders})

        open_positions = int(
            session.scalar(
                select(func.count(models.Position.position_id)).where(
                    models.Position.team_id == team_id,
                    models.Position.environment == environment,
                    (models.Position.quantity != 0)
                    | (models.Position.fact_status == domain.FactStatus.UNKNOWN.value),
                )
            )
            or 0
        )
        if open_positions:
            blockers.append({"code": "SOURCE_POSITIONS_OPEN_OR_UNKNOWN", "count": open_positions})
        return blockers

    def _target_readiness(
        self, session: Session, team: models.Team, environment: str
    ) -> dict[str, Any]:
        accounts = session.scalars(
            select(models.ExchangeAccount)
            .where(
                models.ExchangeAccount.team_id == team.team_id,
                models.ExchangeAccount.environment == environment,
                models.ExchangeAccount.active,
            )
            .order_by(models.ExchangeAccount.venue, models.ExchangeAccount.label)
        ).all()
        ready_accounts: list[dict[str, Any]] = []
        rejected_accounts: list[dict[str, Any]] = []
        for account in accounts:
            reasons: list[str] = []
            if environment == domain.ExecutionEnvironment.TESTNET.value and account.venue not in {
                "BINANCE",
                "HYPERLIQUID",
            }:
                reasons.append("TESTNET_EXECUTION_UNSUPPORTED")
            if account.connection_status != "VERIFIED" or account.last_verified_at is None:
                reasons.append("CONNECTION_NOT_VERIFIED")
            if account.trading_status != "ELIGIBLE":
                reasons.append("TRADING_NOT_ELIGIBLE")
            if account.credentials_ciphertext is None or account.credential_version < 1:
                reasons.append("CREDENTIALS_MISSING")
            if not self._account_environment_valid(account):
                reasons.append("CREDENTIAL_ENVIRONMENT_MISMATCH")
            if not account.runtime_sync_enabled or account.runtime_service_principal_id is None:
                reasons.append("RUNTIME_SERVICE_NOT_READY")
            payload = {
                "account_id": account.account_id,
                "venue": account.venue,
                "environment": account.environment,
                "connection_status": account.connection_status,
                "trading_status": account.trading_status,
                "last_verified_at": (
                    None
                    if account.last_verified_at is None
                    else account.last_verified_at.isoformat()
                ),
            }
            if reasons:
                rejected_accounts.append({**payload, "reasons": reasons})
            else:
                ready_accounts.append(payload)

        blockers: list[dict[str, Any]] = []
        advisories: list[dict[str, Any]] = []
        if not accounts:
            advisories.append({"code": "TARGET_ACCOUNT_MISSING", "count": 1})
        elif not ready_accounts:
            advisories.append({"code": "TARGET_ACCOUNT_NOT_READY", "count": len(accounts)})
        risk_policy = session.scalar(
            select(models.RiskPolicy).where(
                models.RiskPolicy.team_id == team.team_id, models.RiskPolicy.active
            )
        )
        if risk_policy is None:
            blockers.append({"code": "RISK_POLICY_MISSING", "count": 1})
        if (
            team.execution_mode
            in {
                domain.TeamExecutionMode.TESTNET.value,
                domain.TeamExecutionMode.LIVE.value,
            }
            and team.execution_mode != environment
        ):
            blockers.extend(self._source_blockers(session, team.team_id, team.execution_mode))
        return {
            "environment": environment,
            # Account credentials, connection verification and runtime binding are
            # execution prerequisites, not prerequisites for selecting a team mode.
            # Order execution keeps enforcing those checks independently.
            "ready": bool(ready_accounts) and not blockers,
            "execution_ready": bool(ready_accounts),
            "switch_allowed": not blockers,
            "ready_accounts": ready_accounts,
            "rejected_accounts": rejected_accounts,
            "advisories": advisories,
            "blockers": blockers,
        }

    def trading_mode_status(self, *, actor_id: UUID, now: datetime) -> dict[str, Any]:
        with self.database.session_factory() as session:
            team = self.transactions.require_role(session, actor_id, "venue.view", allow_setup=True)
            actor = session.get(models.User, actor_id)
            updated_by = (
                None
                if team.execution_mode_updated_by is None
                else session.get(models.User, team.execution_mode_updated_by)
            )
            gates = {
                row.capability_key: row.status
                for row in session.scalars(
                    select(models.CapabilityGate).where(
                        models.CapabilityGate.capability_key.in_(
                            ("LIVE_ORDER_SEND", "CAPITAL_TRANSFER", "AUTO_ADD")
                        )
                    )
                ).all()
            }
            readiness = {
                environment: self._target_readiness(session, team, environment)
                for environment in (
                    domain.ExecutionEnvironment.TESTNET.value,
                    domain.ExecutionEnvironment.LIVE.value,
                )
            }
            return {
                "workspace_id": str(team.workspace_id),
                "team_id": str(team.team_id),
                "team_name": team.name,
                "execution_mode": team.execution_mode,
                "version": team.version,
                "trading_enabled": team.trading_enabled,
                "can_manage": bool(
                    actor is not None and self.transactions.can_user(actor_id, "team.manage")
                ),
                "last_switched_by": None if updated_by is None else updated_by.username,
                "last_switched_at": (
                    None
                    if team.execution_mode_locked_at is None
                    else team.execution_mode_locked_at.isoformat()
                ),
                "target_readiness": readiness,
                "dangerous_capabilities": gates,
                "safety_boundary": {
                    "mode_does_not_enable_dangerous_capabilities": True,
                    "proposal_environment_is_immutable": True,
                    "testnet_and_live_history_isolated": True,
                },
                "as_of": now.isoformat(),
            }

    def set_team_execution_mode(
        self,
        *,
        actor_id: UUID,
        team_id: UUID,
        mode: str,
        confirmation: str,
        expected_version: int,
        idempotency_key: str,
        now: datetime,
    ) -> dict[str, Any]:
        authorization_policy.require_human_web_session(
            "execution mode changes require an interactive human web session"
        )
        normalized_mode = mode.strip().upper()
        if normalized_mode not in self._CONFIRMATIONS:
            rejections.reject("TEAM_MODE_INVALID", "execution mode must be TESTNET or LIVE")
        expected_confirmation = self._CONFIRMATIONS[normalized_mode]
        if confirmation != expected_confirmation:
            rejections.reject(
                "SECOND_CONFIRMATION_REQUIRED",
                f"confirmation must exactly equal {expected_confirmation}",
            )

        blocked: list[dict[str, Any]] | None = None
        result: dict[str, Any] | None = None
        with self.database.session_factory.begin() as session:
            authorized_team = self.transactions.require_role(
                session,
                actor_id,
                "team.manage",
                team_id=team_id,
                allow_setup=True,
            )
            team = session.scalar(
                select(models.Team)
                .where(models.Team.team_id == authorized_team.team_id)
                .with_for_update()
            )
            assert team is not None
            operation = f"team.execution-mode:{team.team_id}"
            payload = {
                "mode": normalized_mode,
                "confirmation": confirmation,
                "expected_version": expected_version,
            }
            digest, replay = self.transactions.idempotency(
                session,
                caller_id=f"{actor_id}:{team.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if replay is not None:
                return replay
            if team.version != expected_version:
                rejections.reject("VERSION_CONFLICT", "team version changed before mode switch")
            correlation_id = uuid4()
            if team.execution_mode == normalized_mode:
                result = {
                    "workspace_id": str(team.workspace_id),
                    "team_id": str(team.team_id),
                    "previous_mode": team.execution_mode,
                    "execution_mode": team.execution_mode,
                    "version": team.version,
                    "changed": False,
                    "invalidated_authorizations": 0,
                    "invalidated_order_intents": 0,
                    "dangerous_capabilities_changed": False,
                }
            else:
                readiness = self._target_readiness(session, team, normalized_mode)
                blocked = readiness["blockers"] or None
                if blocked:
                    self.transactions.audit(
                        session,
                        actor_id=str(actor_id),
                        event_type="TEAM_EXECUTION_MODE_BLOCKED",
                        object_type="Team",
                        object_id=team.team_id,
                        reason=";".join(f"{item['code']}={item['count']}" for item in blocked),
                        correlation_id=correlation_id,
                        object_version=team.version,
                        idempotency_key=idempotency_key,
                        workspace_id=team.workspace_id,
                        team_id=team.team_id,
                        environment=normalized_mode,
                        rule_summary={"requested_mode": normalized_mode, "blockers": blocked},
                        now=now,
                    )
                else:
                    previous_mode = team.execution_mode
                    invalidated_authorizations = 0
                    invalidated_intents = 0
                    if previous_mode in {
                        domain.TeamExecutionMode.TESTNET.value,
                        domain.TeamExecutionMode.LIVE.value,
                    }:
                        authorizations = session.scalars(
                            select(models.TradingAuthorization).where(
                                models.TradingAuthorization.team_id == team.team_id,
                                models.TradingAuthorization.environment == previous_mode,
                                models.TradingAuthorization.active,
                            )
                        ).all()
                        for authorization in authorizations:
                            authorization.active = False
                            invalidated_authorizations += 1
                            self.transactions.audit(
                                session,
                                actor_id=str(actor_id),
                                event_type="TRADING_AUTHORIZATION_INVALIDATED_BY_MODE_SWITCH",
                                object_type="TradingAuthorization",
                                object_id=authorization.authorization_id,
                                reason=f"source={previous_mode};target={normalized_mode}",
                                correlation_id=correlation_id,
                                object_version=1,
                                workspace_id=team.workspace_id,
                                team_id=team.team_id,
                                account_id=authorization.account_id,
                                environment=previous_mode,
                                now=now,
                            )
                        intents = session.scalars(
                            select(models.OrderIntent)
                            .join(
                                models.Campaign,
                                models.Campaign.campaign_id == models.OrderIntent.campaign_id,
                            )
                            .where(
                                models.Campaign.team_id == team.team_id,
                                models.Campaign.environment == previous_mode,
                                models.OrderIntent.status.in_(("PENDING", "RESERVED", "READY")),
                            )
                        ).all()
                        for intent in intents:
                            intent.status = domain.OrderIntentStatus.CANCELLED.value
                            intent.version += 1
                            intent.updated_at = now
                            invalidated_intents += 1
                            self.transactions.audit(
                                session,
                                actor_id=str(actor_id),
                                event_type="ORDER_INTENT_INVALIDATED_BY_MODE_SWITCH",
                                object_type="OrderIntent",
                                object_id=intent.intent_id,
                                reason=f"source={previous_mode};target={normalized_mode}",
                                correlation_id=correlation_id,
                                object_version=intent.version,
                                workspace_id=team.workspace_id,
                                team_id=team.team_id,
                                environment=previous_mode,
                                now=now,
                            )
                    team.execution_mode = normalized_mode
                    # Completing SETUP opens the ordinary team workflow only. Venue send,
                    # auto-add and capital transfer remain governed by independent gates.
                    team.trading_enabled = True
                    team.execution_mode_locked_at = now
                    team.execution_mode_updated_by = actor_id
                    team.version += 1
                    team.updated_at = now
                    result = {
                        "workspace_id": str(team.workspace_id),
                        "team_id": str(team.team_id),
                        "previous_mode": previous_mode,
                        "execution_mode": normalized_mode,
                        "version": team.version,
                        "changed": True,
                        "invalidated_authorizations": invalidated_authorizations,
                        "invalidated_order_intents": invalidated_intents,
                        "dangerous_capabilities_changed": False,
                    }
                    self.transactions.audit(
                        session,
                        actor_id=str(actor_id),
                        event_type="TEAM_EXECUTION_MODE_CHANGED",
                        object_type="Team",
                        object_id=team.team_id,
                        reason=f"previous={previous_mode};current={normalized_mode}",
                        correlation_id=correlation_id,
                        object_version=team.version,
                        idempotency_key=idempotency_key,
                        workspace_id=team.workspace_id,
                        team_id=team.team_id,
                        environment=normalized_mode,
                        rule_summary={
                            "previous_mode": previous_mode,
                            "current_mode": normalized_mode,
                            "dangerous_capabilities_changed": False,
                            "historical_proposals_mutated": False,
                            "invalidated_authorizations": invalidated_authorizations,
                            "invalidated_order_intents": invalidated_intents,
                        },
                        now=now,
                    )
            if result is not None:
                self.transactions.save_receipt(
                    session,
                    caller_id=f"{actor_id}:{team.team_id}",
                    operation=operation,
                    idempotency_key=idempotency_key,
                    semantic_hash=digest,
                    response=result,
                    now=now,
                )
        if blocked:
            detail = ", ".join(f"{item['code']}({item['count']})" for item in blocked)
            rejections.reject("TEAM_MODE_SWITCH_BLOCKED", detail)
        assert result is not None
        return result
