from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from trading_control_plane import domain, models, rejections
from trading_control_plane import execution_scope as scope_rules
from trading_control_plane.repositories.execution import find_position_for_scope
from trading_control_plane.service_component import ServiceComponent
from trading_control_plane.service_domains.notifications import enqueue_campaign_status_notification
from trading_control_plane.service_domains.risk_policy import active_risk_policy


def update_campaign_pnl(
    session: Session,
    campaign: models.Campaign,
    position: models.Position,
    *,
    now: datetime,
    fills: Sequence[models.VenueFill] | None = None,
) -> domain.PnlBreakdown:
    campaign_fills = (
        fills
        if fills is not None
        else list(
            session.scalars(
                select(models.VenueFill)
                .where(models.VenueFill.campaign_id == campaign.campaign_id)
                .order_by(models.VenueFill.executed_at, models.VenueFill.venue_fill_fact_id)
            ).all()
        )
    )
    instrument = session.get(models.Instrument, campaign.instrument_id)
    if instrument is None:
        rejections.reject("INSTRUMENT_UNAVAILABLE", "campaign instrument is missing")
    payments = session.scalars(
        select(models.FundingPayment).where(
            models.FundingPayment.campaign_id == campaign.campaign_id
        )
    ).all()
    if any(fill.fee_currency != instrument.collateral_currency for fill in campaign_fills) or any(
        payment.currency != instrument.collateral_currency for payment in payments
    ):
        rejections.reject("PNL_CURRENCY_MISMATCH", "PnL requires an explicit FX conversion")
    funding = sum((payment.amount for payment in payments), Decimal(0))
    result = domain.compute_pnl(
        fills=tuple(
            domain.EconomicFill(
                fill.side,
                fill.quantity,
                fill.price,
                fill.fee,
                fill.slippage_cost,
            )
            for fill in campaign_fills
        ),
        mark_price=position.mark_price,
        funding=funding,
    )
    campaign.realized_pnl = result.realized_pnl
    campaign.unrealized_pnl = result.unrealized_pnl
    campaign.final_pnl = result.total_pnl
    campaign.updated_at = now
    return result


class CampaignExecutionService(ServiceComponent):
    def update_campaign_target(
        self,
        campaign_id: UUID,
        actor_id: UUID,
        candidates: tuple[domain.TargetCandidate, ...],
        *,
        now: datetime,
    ) -> domain.TargetDecision:
        decision = domain.select_target_position(candidates)
        with self.database.session_factory.begin() as session:
            campaign = session.get(models.Campaign, campaign_id, with_for_update=True)
            if campaign is None:
                rejections.reject("CAMPAIGN_NOT_FOUND", "campaign does not exist")
            self.transactions.require_role(
                session,
                actor_id,
                "order.prepare",
                campaign.account_id,
                campaign.venue,
                team_id=campaign.team_id,
            )
            policy = session.scalar(
                select(models.RiskPolicy).where(
                    models.RiskPolicy.team_id == campaign.team_id,
                    models.RiskPolicy.active,
                )
            )
            if policy is None:
                rejections.reject("RISK_POLICY_MISSING", "target update requires an active policy")
            position = find_position_for_scope(session, campaign, for_update=True)
            if position is None or position.fact_status != domain.FactStatus.KNOWN.value:
                rejections.reject("POSITION_UNKNOWN", "target update requires a known position")
            if now - position.observed_at > timedelta(seconds=policy.max_fact_age_seconds):
                rejections.reject("STALE_FACTS", "target update requires a fresh position")
            if decision.target_quantity > abs(position.quantity):
                rejections.reject(
                    "TARGET_EXCEEDS_POSITION", "target cannot exceed the current position"
                )
            campaign.current_target_quantity = decision.target_quantity
            campaign.target_version += 1
            campaign.target_reason = ",".join(decision.reasons)
            campaign.target_urgency = decision.urgency.value
            campaign.target_calculated_at = now
            if decision.target_quantity == 0:
                campaign.status = domain.CampaignStatus.CLOSING.value
            elif decision.target_quantity < abs(position.quantity):
                campaign.status = domain.CampaignStatus.REDUCING.value
            else:
                campaign.status = domain.CampaignStatus.OPEN.value
            campaign.updated_at = now
            self.transactions.audit(
                session,
                actor_id=str(actor_id),
                event_type="CAMPAIGN_TARGET_UPDATED",
                object_type="Campaign",
                object_id=campaign.campaign_id,
                reason=campaign.target_reason,
                correlation_id=uuid4(),
                object_version=campaign.target_version,
                now=now,
            )
        return decision

    def create_reduction_intent(
        self,
        campaign_id: UUID,
        actor_id: UUID,
        idempotency_key: str,
        *,
        candidates: tuple[domain.TargetCandidate, ...] | None = None,
        expected_target_version: int | None = None,
        limit_price: Decimal | None = None,
        now: datetime,
    ) -> UUID:
        operation = "order.reduce"
        with self.database.session_factory.begin() as session:
            campaign = session.get(models.Campaign, campaign_id, with_for_update=True)
            if campaign is None:
                rejections.reject("CAMPAIGN_NOT_FOUND", "campaign does not exist")
            self.transactions.require_role(
                session,
                actor_id,
                "order.prepare",
                campaign.account_id,
                campaign.venue,
                team_id=campaign.team_id,
            )
            position = find_position_for_scope(session, campaign)
            if position is None or position.fact_status != domain.FactStatus.KNOWN.value:
                rejections.reject("POSITION_UNKNOWN", "reduction requires current known position")
            policy = session.scalar(
                select(models.RiskPolicy).where(
                    models.RiskPolicy.team_id == campaign.team_id,
                    models.RiskPolicy.active,
                )
            )
            if policy is None:
                rejections.reject("RISK_POLICY_MISSING", "reduction requires an active policy")
            if now - position.observed_at > timedelta(seconds=policy.max_fact_age_seconds):
                rejections.reject("STALE_FACTS", "reduction requires a fresh position")
            expected_long = campaign.direction == domain.Direction.LONG.value
            if position.quantity == 0 or (position.quantity > 0) != expected_long:
                rejections.reject(
                    "POSITION_DIRECTION_MISMATCH", "position does not match campaign direction"
                )
            if candidates is None:
                payload = {
                    "campaign_id": str(campaign_id),
                    "target_version": campaign.target_version,
                    "target_quantity": str(campaign.current_target_quantity),
                    "position_quantity": str(position.quantity),
                    "limit_price": None if limit_price is None else str(limit_price),
                    "expected_target_version": expected_target_version,
                }
            else:
                payload = {
                    "campaign_id": str(campaign_id),
                    "candidates": [
                        {
                            "target_quantity": str(candidate.target_quantity),
                            "urgency": candidate.urgency.value,
                            "reason": candidate.reason,
                        }
                        for candidate in candidates
                    ],
                    "limit_price": None if limit_price is None else str(limit_price),
                    "expected_target_version": expected_target_version,
                }
            if limit_price is not None and (not limit_price.is_finite() or limit_price <= 0):
                rejections.reject(
                    "ORDER_LIMIT_PRICE_INVALID", "explicit reduction limit must be positive"
                )
            digest, response = self.transactions.idempotency(
                session,
                caller_id=f"{actor_id}:{campaign.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload={**payload, "team_id": str(campaign.team_id)},
            )
            if response is not None:
                return UUID(str(response["intent_id"]))
            if (
                expected_target_version is not None
                and campaign.target_version != expected_target_version
            ):
                rejections.reject("VERSION_CONFLICT", "Campaign target changed before the action")
            active = session.scalar(
                select(models.OrderIntent.intent_id).where(
                    models.OrderIntent.campaign_id == campaign_id,
                    models.OrderIntent.status.in_(scope_rules.ACTIVE_INTENT_STATUSES),
                )
            )
            if active is not None:
                rejections.reject("ACTIVE_ORDER_INTENT", "campaign already has unresolved intent")
            if candidates is not None:
                effective_candidates = candidates
                if campaign.target_calculated_at is not None:
                    try:
                        existing_urgency = domain.TargetUrgency(
                            campaign.target_urgency or domain.TargetUrgency.NORMAL.value
                        )
                    except ValueError:
                        rejections.reject(
                            "CAMPAIGN_TARGET_INVALID", "stored target urgency is invalid"
                        )
                    effective_candidates += (
                        domain.TargetCandidate(
                            campaign.current_target_quantity,
                            existing_urgency,
                            campaign.target_reason or "existing campaign target",
                        ),
                    )
                decision = domain.select_target_position(effective_candidates)
                if decision.target_quantity > abs(position.quantity):
                    rejections.reject(
                        "TARGET_EXCEEDS_POSITION", "target cannot exceed the current position"
                    )
                campaign.current_target_quantity = decision.target_quantity
                campaign.target_version += 1
                campaign.target_reason = ",".join(decision.reasons)
                campaign.target_urgency = decision.urgency.value
                campaign.target_calculated_at = now
                campaign.status = (
                    domain.CampaignStatus.CLOSING.value
                    if decision.target_quantity == 0
                    else domain.CampaignStatus.REDUCING.value
                )
                campaign.updated_at = now
            reduction_quantity = abs(position.quantity) - campaign.current_target_quantity
            if reduction_quantity <= 0:
                rejections.reject("TARGET_NOT_REDUCING", "target does not reduce current position")
            side = "SELL" if position.quantity > 0 else "BUY"
            kind = (
                domain.IntentKind.EXIT
                if campaign.current_target_quantity == 0
                else domain.IntentKind.REDUCE
            )
            intent = models.OrderIntent(
                campaign_id=campaign_id,
                authorization_id=campaign.authorization_id,
                reservation_id=None,
                kind=kind.value,
                side=side,
                quantity=reduction_quantity,
                limit_price=limit_price,
                reduce_only=True,
                trigger_source="CAMPAIGN_TARGET",
                trigger_observed_at=position.observed_at,
                add_unit_consumed=False,
                target_version=campaign.target_version,
                position_id=position.position_id,
                position_observed_at=position.observed_at,
                status=domain.OrderIntentStatus.READY.value,
                semantic_hash=digest,
                actor_id=str(actor_id),
                correlation_id=uuid4(),
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(intent)
            session.flush()
            result = {"intent_id": str(intent.intent_id)}
            self.transactions.save_receipt(
                session,
                caller_id=f"{actor_id}:{campaign.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self.transactions.audit(
                session,
                actor_id=str(actor_id),
                event_type="REDUCTION_INTENT_PREPARED",
                object_type="OrderIntent",
                object_id=intent.intent_id,
                reason=campaign.target_reason or kind.value,
                correlation_id=intent.correlation_id,
                object_version=intent.version,
                idempotency_key=idempotency_key,
                now=now,
            )
            return intent.intent_id

    def create_automatic_exit_intent(
        self,
        campaign_id: UUID,
        actor_id: UUID,
        idempotency_key: str,
        *,
        limit_price: Decimal | None = None,
        now: datetime,
    ) -> tuple[str, UUID | None]:
        operation = "campaign.auto_exit"
        payload = {
            "campaign_id": str(campaign_id),
            "limit_price": None if limit_price is None else str(limit_price),
        }
        with self.database.session_factory.begin() as session:
            campaign = session.get(models.Campaign, campaign_id, with_for_update=True)
            if campaign is None:
                rejections.reject("CAMPAIGN_NOT_FOUND", "campaign does not exist")
            self.transactions.require_role(
                session,
                actor_id,
                "risk.tighten",
                campaign.account_id,
                campaign.venue,
                team_id=campaign.team_id,
            )
            digest, response = self.transactions.idempotency(
                session,
                caller_id=f"{actor_id}:{campaign.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload={**payload, "team_id": str(campaign.team_id)},
            )
            if response is not None:
                intent_value = response.get("intent_id")
                return (
                    str(response["reason"]),
                    None if intent_value is None else UUID(str(intent_value)),
                )
            if limit_price is not None and (not limit_price.is_finite() or limit_price <= 0):
                rejections.reject(
                    "ORDER_LIMIT_PRICE_INVALID", "explicit exit limit must be positive"
                )
            proposal = session.get(models.Proposal, campaign.proposal_id)
            position = find_position_for_scope(session, campaign, for_update=True)
            policy = active_risk_policy(session, campaign.team_id)
            if proposal is None or policy is None:
                rejections.reject(
                    "CAMPAIGN_MANAGEMENT_INVALID", "campaign management facts are incomplete"
                )
            if position is None or position.fact_status != domain.FactStatus.KNOWN.value:
                rejections.reject(
                    "POSITION_UNKNOWN", "automatic exit requires a known current position"
                )
            if scope_rules.fact_is_stale(
                position.observed_at,
                now,
                timedelta(seconds=policy.max_fact_age_seconds),
            ):
                rejections.reject("STALE_FACTS", "automatic exit requires a fresh position")
            if position.quantity == 0:
                result: dict[str, Any] = {"reason": "POSITION_FLAT", "intent_id": None}
                self.transactions.save_receipt(
                    session,
                    caller_id=f"{actor_id}:{campaign.team_id}",
                    operation=operation,
                    idempotency_key=idempotency_key,
                    semantic_hash=digest,
                    response=result,
                    now=now,
                )
                return "POSITION_FLAT", None
            details = proposal.frozen_payload.get("details")
            if not isinstance(details, dict) or details.get("invalidation_price") is None:
                rejections.reject(
                    "PROPOSAL_EXIT_CONTRACT_INVALID",
                    "frozen proposal lacks an invalidation price",
                )
            try:
                invalidation = Decimal(str(details["invalidation_price"]))
            except (ArithmeticError, TypeError, ValueError):
                rejections.reject(
                    "PROPOSAL_EXIT_CONTRACT_INVALID",
                    "frozen invalidation price is invalid",
                )
            if not invalidation.is_finite() or invalidation <= 0:
                rejections.reject(
                    "PROPOSAL_EXIT_CONTRACT_INVALID",
                    "frozen invalidation price is invalid",
                )
            kill_switch = policy.system_state == domain.SystemRiskState.KILL_SWITCH.value
            invalidated = (
                position.mark_price <= invalidation
                if campaign.direction == domain.Direction.LONG.value
                else position.mark_price >= invalidation
            )
            if not kill_switch and not invalidated:
                result = {"reason": "EXIT_TRIGGER_NOT_MET", "intent_id": None}
                self.transactions.save_receipt(
                    session,
                    caller_id=f"{actor_id}:{campaign.team_id}",
                    operation=operation,
                    idempotency_key=idempotency_key,
                    semantic_hash=digest,
                    response=result,
                    now=now,
                )
                return "EXIT_TRIGGER_NOT_MET", None
            reason = "KILL_SWITCH" if kill_switch else "FROZEN_INVALIDATION_REACHED"
            active = session.scalar(
                select(models.OrderIntent.intent_id).where(
                    models.OrderIntent.campaign_id == campaign_id,
                    models.OrderIntent.status.in_(scope_rules.ACTIVE_INTENT_STATUSES),
                )
            )
            if active is not None:
                rejections.reject("ACTIVE_ORDER_INTENT", "campaign already has unresolved intent")
            existing_reason = () if campaign.target_reason is None else (campaign.target_reason,)
            target_reasons = tuple(sorted({*existing_reason, reason}))
            campaign.current_target_quantity = Decimal(0)
            campaign.target_version += 1
            campaign.target_reason = ",".join(target_reasons)
            campaign.target_urgency = domain.TargetUrgency.IMMEDIATE.value
            campaign.target_calculated_at = now
            campaign.status = domain.CampaignStatus.CLOSING.value
            campaign.updated_at = now
            intent = models.OrderIntent(
                campaign_id=campaign_id,
                authorization_id=campaign.authorization_id,
                reservation_id=None,
                kind=domain.IntentKind.EXIT.value,
                side="SELL" if position.quantity > 0 else "BUY",
                quantity=abs(position.quantity),
                limit_price=limit_price,
                reduce_only=True,
                trigger_source=reason,
                trigger_observed_at=position.observed_at,
                add_unit_consumed=False,
                target_version=campaign.target_version,
                position_id=position.position_id,
                position_observed_at=position.observed_at,
                status=domain.OrderIntentStatus.READY.value,
                semantic_hash=digest,
                actor_id=str(actor_id),
                correlation_id=uuid4(),
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(intent)
            session.flush()
            result = {"reason": reason, "intent_id": str(intent.intent_id)}
            self.transactions.save_receipt(
                session,
                caller_id=f"{actor_id}:{campaign.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self.transactions.audit(
                session,
                actor_id=str(actor_id),
                event_type="AUTOMATIC_EXIT_PREPARED",
                object_type="OrderIntent",
                object_id=intent.intent_id,
                reason=reason,
                correlation_id=intent.correlation_id,
                object_version=intent.version,
                idempotency_key=idempotency_key,
                now=now,
            )
            return reason, intent.intent_id

    def close_campaign(self, campaign_id: UUID, actor_id: UUID, *, now: datetime) -> None:
        with self.database.session_factory.begin() as session:
            campaign = session.get(models.Campaign, campaign_id, with_for_update=True)
            if campaign is None:
                rejections.reject("CAMPAIGN_NOT_FOUND", "campaign does not exist")
            self.transactions.require_role(
                session,
                actor_id,
                "order.prepare",
                campaign.account_id,
                campaign.venue,
                team_id=campaign.team_id,
            )
            if campaign.status == domain.CampaignStatus.CLOSED.value:
                return
            policy = session.scalar(
                select(models.RiskPolicy).where(
                    models.RiskPolicy.team_id == campaign.team_id,
                    models.RiskPolicy.active,
                )
            )
            position = find_position_for_scope(session, campaign, for_update=True)
            if (
                policy is None
                or position is None
                or position.fact_status != domain.FactStatus.KNOWN.value
                or position.quantity != 0
                or scope_rules.fact_is_stale(
                    position.observed_at,
                    now,
                    timedelta(seconds=policy.max_fact_age_seconds),
                )
            ):
                rejections.reject(
                    "CAMPAIGN_POSITION_NOT_CLOSED", "campaign requires a fresh flat position"
                )
            exit_intent = session.scalar(
                select(models.OrderIntent)
                .where(
                    models.OrderIntent.campaign_id == campaign_id,
                    models.OrderIntent.kind == domain.IntentKind.EXIT.value,
                )
                .order_by(models.OrderIntent.created_at.desc())
                .limit(1)
                .with_for_update()
            )
            terminal_statuses = {
                domain.OrderIntentStatus.FILLED.value,
                domain.OrderIntentStatus.CANCELLED.value,
                domain.OrderIntentStatus.REJECTED.value,
            }
            if exit_intent is None or exit_intent.status not in terminal_statuses:
                rejections.reject("CAMPAIGN_EXIT_NOT_TERMINAL", "campaign exit is not terminal")
            scope = scope_rules.scope_key(campaign.environment, campaign.account_id, campaign.venue)
            latest = session.scalar(
                select(models.ReconciliationRun)
                .where(
                    models.ReconciliationRun.team_id == campaign.team_id,
                    models.ReconciliationRun.execution_scope == scope,
                )
                .order_by(models.ReconciliationRun.completed_at.desc())
                .limit(1)
            )
            if (
                latest is None
                or latest.status != domain.ReconciliationStatus.MATCH.value
                or not latest.is_computed
                or latest.completed_at < position.observed_at
                or latest.completed_at < exit_intent.updated_at
            ):
                rejections.reject(
                    "RECONCILIATION_REQUIRED", "campaign closure requires a current MATCH"
                )
            reservations = session.scalars(
                select(models.RiskReservation)
                .where(models.RiskReservation.campaign_id == campaign_id)
                .with_for_update()
            ).all()
            if any(
                reservation.status
                in {domain.ReservationStatus.UNKNOWN.value, domain.ReservationStatus.RESERVED.value}
                for reservation in reservations
            ):
                rejections.reject(
                    "RISK_RESERVATION_UNRESOLVED", "campaign risk is not confirmed closed"
                )
            update_campaign_pnl(session, campaign, position, now=now)
            for reservation in reservations:
                if reservation.status == domain.ReservationStatus.OPEN.value:
                    reservation.status = domain.ReservationStatus.RELEASED.value
                    reservation.version += 1
                    reservation.updated_at = now
            authorization = session.get(
                models.TradingAuthorization, campaign.authorization_id, with_for_update=True
            )
            if authorization is not None:
                authorization.active = False
            campaign.status = domain.CampaignStatus.CLOSED.value
            campaign.current_target_quantity = Decimal(0)
            campaign.updated_at = now
            correlation_id = uuid4()
            self.transactions.audit(
                session,
                actor_id=str(actor_id),
                event_type="CAMPAIGN_CLOSED",
                object_type="Campaign",
                object_id=campaign.campaign_id,
                reason="flat position, terminal exit, and computed reconciliation MATCH",
                correlation_id=correlation_id,
                object_version=campaign.target_version,
                now=now,
            )
            enqueue_campaign_status_notification(
                self.transactions,
                session,
                actor_id=str(actor_id),
                campaign=campaign,
                summary="空仓、终态退出及计算型对账一致, 交易任务已关闭。",
                idempotency_key=f"campaign-closed:{latest.reconciliation_id}",
                correlation_id=correlation_id,
                now=now,
            )

    def refresh_campaign_pnl(
        self, campaign_id: UUID, actor_id: UUID, *, now: datetime
    ) -> domain.PnlBreakdown:
        with self.database.session_factory.begin() as session:
            campaign = session.get(models.Campaign, campaign_id, with_for_update=True)
            if campaign is None:
                rejections.reject("CAMPAIGN_NOT_FOUND", "campaign does not exist")
            self.transactions.require_role(
                session,
                actor_id,
                "view",
                campaign.account_id,
                campaign.venue,
                team_id=campaign.team_id,
            )
            fills = session.scalars(
                select(models.VenueFill)
                .where(models.VenueFill.campaign_id == campaign_id)
                .order_by(models.VenueFill.executed_at, models.VenueFill.venue_fill_fact_id)
            ).all()
            position = find_position_for_scope(session, campaign)
            if position is None or position.fact_status != domain.FactStatus.KNOWN.value:
                rejections.reject("POSITION_UNKNOWN", "PnL requires known current position")
            result = update_campaign_pnl(
                session,
                campaign,
                position,
                fills=fills,
                now=now,
            )
            return result
