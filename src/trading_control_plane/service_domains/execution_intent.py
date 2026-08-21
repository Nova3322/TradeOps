from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from trading_control_plane import domain, metrics, models, rejections
from trading_control_plane import execution_scope as scope_rules
from trading_control_plane.repositories.execution import find_position_for_scope
from trading_control_plane.service_component import ServiceComponent
from trading_control_plane.service_domains.execution_campaign import update_campaign_pnl
from trading_control_plane.service_domains.notifications import enqueue_campaign_status_notification
from trading_control_plane.service_domains.risk_authorization import (
    intent_creation,
    proposal_limit_price,
    validate_add_candidate,
)
from trading_control_plane.service_domains.risk_policy import (
    active_risk_policy,
    occupied_risk,
    risk_policy_input,
    server_risk_context,
)


def consume_add_unit(session: Session, intent: models.OrderIntent) -> None:
    if intent.kind != domain.IntentKind.ADD.value or intent.add_unit_consumed:
        return
    authorization = session.get(
        models.TradingAuthorization, intent.authorization_id, with_for_update=True
    )
    if authorization is None or authorization.used_adds >= authorization.allowed_adds:
        rejections.reject(
            "AUTHORIZATION_ADD_LIMIT_INVALID",
            "positive Add execution exceeds the authorized AddUnit count",
        )
    authorization.used_adds += 1
    intent.add_unit_consumed = True


class IntentExecutionService(ServiceComponent):
    def create_order_intent(
        self,
        authorization_id: UUID,
        actor_id: UUID,
        kind: domain.IntentKind,
        account_id: str,
        venue: str,
        instrument_id: UUID,
        direction: domain.Direction,
        quantity: Decimal,
        idempotency_key: str,
        *,
        add_candidate: domain.AddCandidateFacts | None = None,
        now: datetime,
    ) -> domain.IntentCreation:
        if kind not in {domain.IntentKind.INITIAL, domain.IntentKind.ADD}:
            rejections.reject(
                "NEW_RISK_INTENT_REQUIRED", "this entry point only creates INITIAL or ADD"
            )
        payload = {
            "authorization_id": str(authorization_id),
            "kind": kind.value,
            "account_id": account_id,
            "venue": venue,
            "instrument_id": str(instrument_id),
            "direction": direction.value,
            "quantity": str(quantity),
            "add_candidate": None
            if add_candidate is None
            else {
                "candidate_id": add_candidate.candidate_id,
                "contract_version": add_candidate.contract_version,
                "venue": add_candidate.venue,
                "symbol": add_candidate.symbol,
                "direction": add_candidate.direction.value,
                "observed_at": add_candidate.observed_at.isoformat(),
                "reference_price": str(add_candidate.reference_price),
                "readiness": add_candidate.readiness,
            },
        }
        operation = "order.prepare"
        with self.database.session_factory.begin() as session:
            authorization = session.get(models.TradingAuthorization, authorization_id)
            if authorization is None:
                rejections.reject("AUTHORIZATION_INACTIVE", "authorization is missing or inactive")
            active_team = self.transactions.require_role(
                session,
                actor_id,
                operation,
                authorization.account_id,
                authorization.venue,
                team_id=authorization.team_id,
            )
            proposal_environment = domain.ExecutionEnvironment(
                session.scalar(
                    select(models.Proposal.environment).where(
                        models.Proposal.proposal_id == authorization.proposal_id
                    )
                )
                or ""
            )
            self.transactions.require_team_environment(active_team, proposal_environment)
            digest, response = self.transactions.idempotency(
                session,
                caller_id=f"{actor_id}:{active_team.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload={**payload, "team_id": str(active_team.team_id)},
            )
            if response is not None:
                return intent_creation(response)
            self.transactions.lock_risk_capacity(session, authorization.team_id)
            authorization = session.get(
                models.TradingAuthorization,
                authorization_id,
                with_for_update=True,
                populate_existing=True,
            )
            if authorization is None:
                rejections.reject("AUTHORIZATION_INACTIVE", "authorization is missing or inactive")
            policy = active_risk_policy(session, authorization.team_id)
            auto_add_gate: models.CapabilityGate | None = None
            if kind is domain.IntentKind.ADD:
                auto_add_gate = session.get(models.CapabilityGate, "AUTO_ADD", with_for_update=True)
                if (
                    auto_add_gate is None
                    or auto_add_gate.status != domain.CapabilityStatus.ENABLED.value
                ):
                    rejections.reject("AUTO_ADD_DISABLED", "automatic add capability is disabled")
            if not authorization.active:
                rejections.reject("AUTHORIZATION_INACTIVE", "authorization is missing or inactive")
            if authorization.expires_at <= now:
                rejections.reject("AUTHORIZATION_EXPIRED", "authorization expired")
            if (
                authorization.account_id != account_id
                or authorization.venue != venue
                or authorization.instrument_id != instrument_id
                or authorization.direction != direction.value
            ):
                rejections.reject("AUTHORIZATION_SCOPE_MISMATCH", "request exceeds frozen scope")
            if (
                quantity <= 0
                or authorization.used_quantity + quantity > authorization.quantity_limit
            ):
                rejections.reject(
                    "AUTHORIZATION_QUANTITY_EXCEEDED", "request exceeds quantity limit"
                )
            proposal = session.get(models.Proposal, authorization.proposal_id, with_for_update=True)
            if proposal is None or proposal.status != domain.ProposalStatus.APPROVED.value:
                rejections.reject("PROPOSAL_NOT_APPROVED", "authorization proposal is not approved")
            if (
                proposal.leverage is None
                or authorization.leverage is None
                or authorization.leverage != proposal.leverage
            ):
                rejections.reject(
                    "LEVERAGE_FREEZE_MISMATCH",
                    "proposal and authorization do not share one frozen leverage",
                )
            if proposal.expires_at <= now:
                rejections.reject("PROPOSAL_EXPIRED", "authorization proposal expired")
            if (
                authorization.quantity_limit > proposal.quantity
                or authorization.risk_limit > proposal.max_risk
            ):
                rejections.reject(
                    "AUTHORIZATION_SCOPE_MISMATCH", "authorization exceeds proposal caps"
                )
            if kind is domain.IntentKind.INITIAL:
                existing_initial = session.scalar(
                    select(models.OrderIntent.intent_id)
                    .join(
                        models.Campaign,
                        models.Campaign.campaign_id == models.OrderIntent.campaign_id,
                    )
                    .where(
                        models.Campaign.proposal_id == proposal.proposal_id,
                        models.OrderIntent.kind == domain.IntentKind.INITIAL.value,
                    )
                    .limit(1)
                )
                if existing_initial is not None:
                    rejections.reject(
                        "INITIAL_INTENT_ALREADY_EXISTS",
                        "this frozen proposal already produced its one initial intent",
                    )

            total_occupied_risk = occupied_risk(session, proposal.team_id)
            risk_amount = authorization.risk_limit * quantity / authorization.quantity_limit
            if risk_amount <= 0 or risk_amount > authorization.risk_limit:
                rejections.reject(
                    "AUTHORIZATION_RISK_EXCEEDED", "request exceeds risk authorization"
                )
            inputs, _, _, effective_max_total_risk = server_risk_context(
                session,
                proposal=proposal,
                policy=policy,
                kind=kind,
                requested_quantity=quantity,
                requested_risk=risk_amount,
                current_risk=total_occupied_risk,
                now=now,
            )
            final_outcome = domain.evaluate_risk(
                risk_policy_input(
                    policy,
                    effective_max_total_risk=(
                        effective_max_total_risk
                        if effective_max_total_risk > 0
                        else policy.max_total_risk
                    ),
                ),
                inputs,
            )
            if final_outcome.result is not domain.RiskResult.ALLOW:
                reason = final_outcome.reasons[0] if final_outcome.reasons else "RISK_REJECTED"
                rejections.reject("FINAL_RISK_CHECK_FAILED", reason)

            position = session.scalar(
                select(models.Position)
                .where(
                    models.Position.team_id == proposal.team_id,
                    models.Position.account_id == account_id,
                    models.Position.venue == venue,
                    models.Position.environment == proposal.environment,
                    models.Position.instrument_id == instrument_id,
                )
                .with_for_update()
            )
            if position is None or position.fact_status != domain.FactStatus.KNOWN.value:
                rejections.reject(
                    "POSITION_UNKNOWN", "new risk requires a current known position fact"
                )

            campaign = session.scalar(
                select(models.Campaign)
                .where(models.Campaign.authorization_id == authorization_id)
                .with_for_update()
            )
            campaign_created = campaign is None
            if kind is domain.IntentKind.INITIAL:
                current_quantity = position.quantity
                if current_quantity != 0:
                    rejections.reject(
                        "POSITION_NOT_FLAT", "INITIAL requires a confirmed flat position"
                    )
                conflicting_campaign = session.scalar(
                    select(models.Campaign)
                    .where(
                        models.Campaign.team_id == proposal.team_id,
                        models.Campaign.account_id == account_id,
                        models.Campaign.venue == venue,
                        models.Campaign.environment == proposal.environment,
                        models.Campaign.instrument_id == instrument_id,
                        models.Campaign.status != domain.CampaignStatus.CLOSED.value,
                    )
                    .with_for_update()
                )
                if conflicting_campaign is not None and (
                    campaign is None or conflicting_campaign.campaign_id != campaign.campaign_id
                ):
                    rejections.reject(
                        "ACTIVE_CAMPAIGN_EXISTS", "scope already has an unclosed campaign"
                    )
                if campaign is None:
                    campaign = models.Campaign(
                        team_id=proposal.team_id,
                        proposal_id=authorization.proposal_id,
                        authorization_id=authorization_id,
                        account_id=authorization.account_id,
                        venue=authorization.venue,
                        environment=proposal.environment,
                        instrument_id=authorization.instrument_id,
                        direction=authorization.direction,
                        status=domain.CampaignStatus.OPENING.value,
                        current_target_quantity=authorization.quantity_limit,
                        target_version=0,
                        target_reason=None,
                        target_urgency=None,
                        target_calculated_at=None,
                        realized_pnl=Decimal(0),
                        unrealized_pnl=Decimal(0),
                        final_pnl=Decimal(0),
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(campaign)
                    session.flush()
            else:
                assert auto_add_gate is not None
                if authorization.add_revoked_at is not None:
                    rejections.reject(
                        "AUTHORIZATION_ADD_REVOKED",
                        "authorization AddUnits were permanently revoked by a tighten action",
                    )
                instrument = session.get(models.Instrument, proposal.instrument_id)
                if instrument is None:
                    rejections.reject(
                        "INSTRUMENT_UNAVAILABLE", "proposal instrument is unavailable"
                    )
                validate_add_candidate(
                    proposal=proposal,
                    instrument=instrument,
                    candidate=add_candidate,
                    policy=policy,
                    now=now,
                )
                if policy.system_state != domain.SystemRiskState.NORMAL.value:
                    rejections.reject("ADD_RISK_STATE_INVALID", "ADD requires NORMAL risk state")
                if authorization.used_adds >= authorization.allowed_adds:
                    rejections.reject("ADD_LIMIT_EXHAUSTED", "authorization add count is exhausted")
                if campaign is None or campaign.status in {
                    domain.CampaignStatus.CLOSED.value,
                    domain.CampaignStatus.UNKNOWN.value,
                }:
                    rejections.reject(
                        "ADD_CAMPAIGN_REQUIRED", "ADD requires an existing known campaign"
                    )
                expected_long = campaign.direction == domain.Direction.LONG.value
                current_position = position
                if (
                    current_position is None
                    or current_position.quantity == 0
                    or (current_position.quantity > 0) != expected_long
                ):
                    rejections.reject(
                        "ADD_POSITION_INVALID", "ADD requires an existing aligned position"
                    )
                unrealized_pnl = (
                    current_position.mark_price - current_position.average_entry_price
                ) * current_position.quantity
                if unrealized_pnl <= 0:
                    rejections.reject(
                        "ADD_NOT_PROFITABLE", "ADD requires strictly positive unrealized PnL"
                    )

            active = session.scalar(
                select(models.OrderIntent.intent_id).where(
                    models.OrderIntent.campaign_id == campaign.campaign_id,
                    models.OrderIntent.status.in_(scope_rules.ACTIVE_INTENT_STATUSES),
                )
            )
            if active is not None:
                rejections.reject("ACTIVE_ORDER_INTENT", "campaign already has unresolved intent")
            occupied_account_risk = occupied_risk(
                session,
                proposal.team_id,
                account_id=proposal.account_id,
                venue=proposal.venue,
            )
            if (
                policy.max_account_risk is None
                or policy.max_single_loss is None
                or risk_amount > policy.max_single_loss
                or total_occupied_risk + risk_amount > policy.max_total_risk
                or occupied_account_risk + risk_amount > policy.max_account_risk
            ):
                rejections.reject("RISK_CAPACITY_EXHAUSTED", "atomic risk capacity is exhausted")
            reservation = models.RiskReservation(
                team_id=proposal.team_id,
                campaign_id=campaign.campaign_id,
                authorization_id=authorization_id,
                status=domain.ReservationStatus.RESERVED.value,
                amount=risk_amount,
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(reservation)
            session.flush()
            side = "BUY" if direction is domain.Direction.LONG else "SELL"
            intent = models.OrderIntent(
                campaign_id=campaign.campaign_id,
                authorization_id=authorization_id,
                reservation_id=reservation.reservation_id,
                kind=kind.value,
                side=side,
                quantity=quantity,
                leverage=authorization.leverage,
                limit_price=proposal_limit_price(proposal),
                reduce_only=False,
                trigger_source=(
                    None if add_candidate is None else f"PERPTAPE:{add_candidate.candidate_id}"
                ),
                trigger_observed_at=(None if add_candidate is None else add_candidate.observed_at),
                add_unit_consumed=False,
                target_version=None,
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
            authorization.used_quantity += quantity
            session.flush()
            result = {
                "campaign_id": str(campaign.campaign_id),
                "reservation_id": str(reservation.reservation_id),
                "intent_id": str(intent.intent_id),
            }
            self.transactions.save_receipt(
                session,
                caller_id=f"{actor_id}:{active_team.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self.transactions.audit(
                session,
                actor_id=str(actor_id),
                event_type="ORDER_INTENT_PREPARED",
                object_type="OrderIntent",
                object_id=intent.intent_id,
                reason=f"{kind.value} quantity={intent.quantity} leverage={intent.leverage}x",
                correlation_id=intent.correlation_id,
                object_version=intent.version,
                idempotency_key=idempotency_key,
                now=now,
            )
            if campaign_created:
                enqueue_campaign_status_notification(
                    self.transactions,
                    session,
                    actor_id=str(actor_id),
                    campaign=campaign,
                    summary="初始订单意图已冻结, 交易任务进入开仓中状态。",
                    idempotency_key=idempotency_key,
                    correlation_id=intent.correlation_id,
                    now=now,
                )
            metrics.INTENT_TRANSITIONS.labels("CREATED", intent.status).inc()
            return domain.IntentCreation(
                campaign_id=campaign.campaign_id,
                reservation_id=reservation.reservation_id,
                intent_id=intent.intent_id,
            )

    def mark_intent_unknown(
        self,
        intent_id: UUID,
        actor_id: UUID,
        reason: str,
        *,
        required_environment: domain.ExecutionEnvironment | None = None,
        now: datetime,
    ) -> None:
        with self.database.session_factory.begin() as session:
            intent = session.get(models.OrderIntent, intent_id, with_for_update=True)
            if intent is None:
                rejections.reject("ORDER_INTENT_NOT_FOUND", "intent does not exist")
            campaign = session.get(models.Campaign, intent.campaign_id)
            if campaign is None:
                rejections.reject("CAMPAIGN_NOT_FOUND", "intent campaign does not exist")
            if (
                required_environment is not None
                and campaign.environment != required_environment.value
            ):
                rejections.reject(
                    "EXECUTION_ENVIRONMENT_MISMATCH",
                    "intent is outside the requested execution environment",
                )
            self.transactions.require_role(
                session,
                actor_id,
                "order.prepare",
                campaign.account_id,
                campaign.venue,
                team_id=campaign.team_id,
            )
            if intent.status == domain.OrderIntentStatus.UNKNOWN.value:
                return
            if intent.status not in scope_rules.ACTIVE_INTENT_STATUSES:
                rejections.reject(
                    "ORDER_INTENT_NOT_ACTIVE", "only an active intent may become UNKNOWN"
                )
            previous = intent.status
            intent.status = domain.OrderIntentStatus.UNKNOWN.value
            intent.updated_at = now
            intent.version += 1
            if intent.reservation_id is not None:
                reservation = session.get(models.RiskReservation, intent.reservation_id)
                if reservation is not None:
                    reservation.status = domain.ReservationStatus.UNKNOWN.value
                    reservation.updated_at = now
                    reservation.version += 1
            venue_order = session.scalar(
                select(models.VenueOrder).where(models.VenueOrder.order_intent_id == intent_id)
            )
            if venue_order is not None:
                venue_order.status = domain.VenueOrderStatus.UNKNOWN.value
                venue_order.observed_at = now
                venue_order.updated_at = now
            campaign.status = domain.CampaignStatus.UNKNOWN.value
            campaign.updated_at = now
            self.transactions.audit(
                session,
                actor_id=str(actor_id),
                event_type="ORDER_INTENT_UNKNOWN",
                object_type="OrderIntent",
                object_id=intent.intent_id,
                reason=reason,
                correlation_id=intent.correlation_id,
                object_version=intent.version,
                now=now,
            )
            enqueue_campaign_status_notification(
                self.transactions,
                session,
                actor_id=str(actor_id),
                campaign=campaign,
                summary="订单结果未知, 交易任务已进入阻断状态, 需先完成对账。",
                idempotency_key=f"intent-unknown-v{intent.version}",
                correlation_id=intent.correlation_id,
                now=now,
            )
            metrics.INTENT_TRANSITIONS.labels(previous, intent.status).inc()

    def release_unfilled_intent(
        self,
        intent_id: UUID,
        actor_id: UUID,
        terminal_status: domain.OrderIntentStatus,
        reason: str,
        *,
        required_environment: domain.ExecutionEnvironment | None = None,
        now: datetime,
    ) -> None:
        if terminal_status not in {
            domain.OrderIntentStatus.CANCELLED,
            domain.OrderIntentStatus.REJECTED,
        }:
            rejections.reject(
                "INVALID_TERMINAL_STATUS", "only cancelled or rejected may release risk"
            )
        with self.database.session_factory.begin() as session:
            intent = session.get(models.OrderIntent, intent_id, with_for_update=True)
            if intent is None:
                rejections.reject("ORDER_INTENT_NOT_FOUND", "intent does not exist")
            campaign = session.get(models.Campaign, intent.campaign_id)
            if campaign is None:
                rejections.reject("CAMPAIGN_NOT_FOUND", "intent campaign does not exist")
            if (
                required_environment is not None
                and campaign.environment != required_environment.value
            ):
                rejections.reject(
                    "EXECUTION_ENVIRONMENT_MISMATCH",
                    "intent is outside the requested execution environment",
                )
            self.transactions.require_role(
                session,
                actor_id,
                "order.prepare",
                campaign.account_id,
                campaign.venue,
                team_id=campaign.team_id,
            )
            reservation = (
                session.get(models.RiskReservation, intent.reservation_id, with_for_update=True)
                if intent.reservation_id is not None
                else None
            )
            if intent.status == terminal_status.value:
                if (
                    reservation is None
                    or reservation.status == domain.ReservationStatus.RELEASED.value
                ):
                    return
                rejections.reject("RISK_RELEASE_INCOMPLETE", "terminal intent still occupies risk")
            if intent.status in {
                domain.OrderIntentStatus.CANCELLED.value,
                domain.OrderIntentStatus.REJECTED.value,
                domain.OrderIntentStatus.FILLED.value,
            }:
                rejections.reject("ORDER_INTENT_TERMINAL", "terminal intent cannot change outcome")
            if intent.status not in scope_rules.RELEASABLE_INTENT_STATUSES:
                rejections.reject(
                    "ORDER_INTENT_NOT_RELEASABLE", "unknown intent cannot release risk"
                )
            filled = session.scalar(
                select(func.coalesce(func.sum(models.VenueFill.quantity), 0)).where(
                    models.VenueFill.order_intent_id == intent_id
                )
            )
            if filled != 0:
                rejections.reject(
                    "FILLED_INTENT_RISK_REQUIRED", "filled intent cannot release all risk"
                )
            previous = intent.status
            intent.status = terminal_status.value
            intent.updated_at = now
            intent.version += 1
            if reservation is not None:
                if reservation.status == domain.ReservationStatus.UNKNOWN.value:
                    rejections.reject("RISK_RESERVATION_UNKNOWN", "unknown risk cannot be released")
                if reservation.status != domain.ReservationStatus.RELEASED.value:
                    reservation.status = domain.ReservationStatus.RELEASED.value
                    reservation.updated_at = now
                    reservation.version += 1
                    authorization = session.get(
                        models.TradingAuthorization, intent.authorization_id, with_for_update=True
                    )
                    if authorization is None or authorization.used_quantity < intent.quantity:
                        rejections.reject(
                            "AUTHORIZATION_USAGE_INVALID", "authorization usage is inconsistent"
                        )
                    authorization.used_quantity -= intent.quantity
            close_unfilled_campaign = False
            if intent.kind == domain.IntentKind.INITIAL.value:
                policy = session.scalar(
                    select(models.RiskPolicy).where(
                        models.RiskPolicy.team_id == campaign.team_id,
                        models.RiskPolicy.active,
                    )
                )
                position = find_position_for_scope(session, campaign)
                scope = scope_rules.scope_key(
                    campaign.environment, campaign.account_id, campaign.venue
                )
                latest_reconciliation = session.scalar(
                    select(models.ReconciliationRun)
                    .where(
                        models.ReconciliationRun.team_id == campaign.team_id,
                        models.ReconciliationRun.execution_scope == scope,
                    )
                    .order_by(models.ReconciliationRun.completed_at.desc())
                    .limit(1)
                )
                close_unfilled_campaign = bool(
                    policy is not None
                    and position is not None
                    and position.fact_status == domain.FactStatus.KNOWN.value
                    and position.quantity == 0
                    and not scope_rules.fact_is_stale(
                        position.observed_at,
                        now,
                        timedelta(seconds=policy.max_fact_age_seconds),
                    )
                    and latest_reconciliation is not None
                    and latest_reconciliation.status == domain.ReconciliationStatus.MATCH.value
                    and latest_reconciliation.is_computed
                    and latest_reconciliation.completed_at >= position.observed_at
                )
                if close_unfilled_campaign:
                    assert position is not None
                    campaign.status = domain.CampaignStatus.CLOSED.value
                    campaign.current_target_quantity = Decimal(0)
                    campaign.target_reason = reason
                    campaign.updated_at = now
                    authorization = session.get(
                        models.TradingAuthorization, campaign.authorization_id, with_for_update=True
                    )
                    if authorization is not None:
                        authorization.active = False
                    update_campaign_pnl(session, campaign, position, now=now)
            venue_order = session.scalar(
                select(models.VenueOrder).where(models.VenueOrder.order_intent_id == intent_id)
            )
            if venue_order is not None:
                venue_order.status = (
                    domain.VenueOrderStatus.CANCELLED.value
                    if terminal_status is domain.OrderIntentStatus.CANCELLED
                    else domain.VenueOrderStatus.REJECTED.value
                )
                venue_order.observed_at = now
                venue_order.updated_at = now
            self.transactions.audit(
                session,
                actor_id=str(actor_id),
                event_type="ORDER_INTENT_TERMINATED",
                object_type="OrderIntent",
                object_id=intent.intent_id,
                reason=reason,
                correlation_id=intent.correlation_id,
                object_version=intent.version,
                now=now,
            )
            if close_unfilled_campaign:
                self.transactions.audit(
                    session,
                    actor_id=str(actor_id),
                    event_type="CAMPAIGN_CLOSED_UNFILLED",
                    object_type="Campaign",
                    object_id=campaign.campaign_id,
                    reason="initial intent terminated with fresh flat position and computed MATCH",
                    correlation_id=intent.correlation_id,
                    object_version=campaign.target_version,
                    now=now,
                )
                enqueue_campaign_status_notification(
                    self.transactions,
                    session,
                    actor_id=str(actor_id),
                    campaign=campaign,
                    summary="未成交的初始订单已终止, 且新鲜空仓事实确认任务关闭。",
                    idempotency_key=f"intent-terminal-v{intent.version}",
                    correlation_id=intent.correlation_id,
                    now=now,
                )
            metrics.INTENT_TRANSITIONS.labels(previous, intent.status).inc()

    def acquire_sender(
        self,
        execution_scope: str,
        owner_id: str,
        actor_id: UUID,
        now: datetime,
        lease_duration: timedelta = scope_rules.DEFAULT_SENDER_LEASE_DURATION,
    ) -> int:
        with self.database.session_factory.begin() as session:
            _environment, account_id, venue = scope_rules.scope_parts(execution_scope)
            if not owner_id or lease_duration <= timedelta(0):
                rejections.reject(
                    "SENDER_LEASE_INVALID", "owner and positive lease duration are required"
                )
            team = self.transactions.require_role(
                session, actor_id, "sender.manage", account_id, venue
            )
            lease = session.get(
                models.SenderLease,
                (team.team_id, execution_scope),
                with_for_update=True,
            )
            if lease is None:
                token = 1
                session.add(
                    models.SenderLease(
                        team_id=team.team_id,
                        execution_scope=execution_scope,
                        owner_id=owner_id,
                        fencing_token=token,
                        expires_at=now + lease_duration,
                        updated_at=now,
                    )
                )
            elif lease.owner_id == owner_id and lease.expires_at > now:
                token = lease.fencing_token
                lease.expires_at = now + lease_duration
                lease.updated_at = now
            else:
                if lease.expires_at > now:
                    rejections.reject(
                        "SENDER_LEASE_HELD", "another sender still owns the live lease"
                    )
                latest = session.scalar(
                    select(models.ReconciliationRun)
                    .where(
                        models.ReconciliationRun.team_id == team.team_id,
                        models.ReconciliationRun.execution_scope == execution_scope,
                    )
                    .order_by(models.ReconciliationRun.completed_at.desc())
                    .limit(1)
                )
                policy = session.scalar(
                    select(models.RiskPolicy).where(
                        models.RiskPolicy.team_id == team.team_id,
                        models.RiskPolicy.active,
                    )
                )
                max_age = (
                    timedelta(seconds=policy.max_fact_age_seconds)
                    if policy is not None
                    else timedelta(0)
                )
                if (
                    latest is None
                    or policy is None
                    or latest.status != domain.ReconciliationStatus.MATCH.value
                    or not latest.is_computed
                    or latest.completed_at <= lease.expires_at
                    or latest.completed_at > now
                    or now - latest.completed_at > max_age
                ):
                    rejections.reject(
                        "RECONCILIATION_REQUIRED",
                        "sender takeover requires a fresh computed MATCH after lease expiry",
                    )
                token = lease.fencing_token + 1
                lease.owner_id = owner_id
                lease.fencing_token = token
                lease.expires_at = now + lease_duration
                lease.updated_at = now
            self.transactions.audit(
                session,
                actor_id=str(actor_id),
                event_type="SENDER_LEASE_ACQUIRED",
                object_type="SenderLease",
                object_id=execution_scope,
                reason=owner_id,
                correlation_id=uuid4(),
                object_version=token,
                workspace_id=team.workspace_id,
                team_id=team.team_id,
                account_id=account_id,
                now=now,
            )
            return token

    def acquire_freqtrade_recovery_sender(
        self,
        intent_id: UUID,
        execution_scope: str,
        owner_id: str,
        actor_id: UUID,
        now: datetime,
        lease_duration: timedelta = scope_rules.DEFAULT_SENDER_LEASE_DURATION,
    ) -> int:
        """Fence one query-only recovery without permitting a new external send."""

        with self.database.session_factory.begin() as session:
            _environment, account_id, venue = scope_rules.scope_parts(execution_scope)
            if not owner_id or lease_duration <= timedelta(0):
                rejections.reject(
                    "SENDER_LEASE_INVALID", "owner and positive lease duration are required"
                )
            intent = session.get(models.OrderIntent, intent_id, with_for_update=True)
            campaign = None if intent is None else session.get(models.Campaign, intent.campaign_id)
            if intent is None or campaign is None:
                rejections.reject("ORDER_INTENT_NOT_FOUND", "recovery intent is unavailable")
            team = self.transactions.require_role(
                session,
                actor_id,
                "sender.manage",
                campaign.account_id,
                campaign.venue,
                team_id=campaign.team_id,
            )
            if (
                execution_scope
                != scope_rules.scope_key(
                    campaign.environment,
                    campaign.account_id,
                    campaign.venue,
                )
                or campaign.account_id != account_id
                or campaign.venue != venue
                or intent.dispatch_backend != "FREQTRADE"
                or intent.dispatch_started_at is None
                or intent.dispatch_owner_id != owner_id
                or intent.status
                not in {
                    domain.OrderIntentStatus.DISPATCHING.value,
                    domain.OrderIntentStatus.SENT.value,
                    domain.OrderIntentStatus.PARTIALLY_FILLED.value,
                    domain.OrderIntentStatus.UNKNOWN.value,
                }
            ):
                rejections.reject(
                    "FREQTRADE_RECOVERY_SCOPE_INVALID",
                    "query-only recovery requires the exact existing Freqtrade dispatch",
                )
            lease = session.get(
                models.SenderLease,
                (team.team_id, execution_scope),
                with_for_update=True,
            )
            if lease is None:
                rejections.reject(
                    "SENDER_LEASE_INVALID",
                    "query-only recovery requires the original durable sender lease",
                )
            if lease.owner_id != owner_id and lease.expires_at > now:
                rejections.reject("SENDER_LEASE_HELD", "another sender still owns the live lease")
            if lease.owner_id == owner_id and lease.expires_at > now:
                token = lease.fencing_token
            else:
                token = lease.fencing_token + 1
                lease.owner_id = owner_id
                lease.fencing_token = token
            lease.expires_at = now + lease_duration
            lease.updated_at = now
            self.transactions.audit(
                session,
                actor_id=str(actor_id),
                event_type="SENDER_QUERY_RECOVERY_LEASE_ACQUIRED",
                object_type="SenderLease",
                object_id=execution_scope,
                reason=f"intent={intent.intent_id};backend=FREQTRADE;external_send=none",
                correlation_id=intent.correlation_id,
                object_version=token,
                workspace_id=team.workspace_id,
                team_id=team.team_id,
                account_id=account_id,
                now=now,
            )
            return token

    def acquire_reduce_only_sender(
        self,
        intent_id: UUID,
        execution_scope: str,
        owner_id: str,
        actor_id: UUID,
        now: datetime,
        lease_duration: timedelta = scope_rules.DEFAULT_SENDER_LEASE_DURATION,
    ) -> int:
        """Fence one exact READY reduce-only send without requiring a MATCH takeover."""

        with self.database.session_factory.begin() as session:
            environment, account_id, venue = scope_rules.scope_parts(execution_scope)
            if not owner_id or lease_duration <= timedelta(0):
                rejections.reject(
                    "SENDER_LEASE_INVALID", "owner and positive lease duration are required"
                )
            intent = session.get(models.OrderIntent, intent_id, with_for_update=True)
            campaign = None if intent is None else session.get(models.Campaign, intent.campaign_id)
            if intent is None or campaign is None:
                rejections.reject("ORDER_INTENT_NOT_FOUND", "reduce-only intent is unavailable")
            team = self.transactions.require_role(
                session,
                actor_id,
                "sender.manage",
                campaign.account_id,
                campaign.venue,
                team_id=campaign.team_id,
            )
            if (
                execution_scope
                != scope_rules.scope_key(
                    campaign.environment,
                    campaign.account_id,
                    campaign.venue,
                )
                or campaign.environment != environment
                or campaign.account_id != account_id
                or campaign.venue != venue
                or intent.status != domain.OrderIntentStatus.READY.value
                or not intent.reduce_only
                or intent.kind
                not in {
                    domain.IntentKind.REDUCE.value,
                    domain.IntentKind.EXIT.value,
                }
                or intent.dispatch_backend is not None
                or intent.dispatch_started_at is not None
            ):
                rejections.reject(
                    "REDUCE_ONLY_SENDER_SCOPE_INVALID",
                    "sender bypass requires the exact unsent READY reduce-only intent",
                )
            lease = session.get(
                models.SenderLease,
                (team.team_id, execution_scope),
                with_for_update=True,
            )
            if lease is None:
                token = 1
                session.add(
                    models.SenderLease(
                        team_id=team.team_id,
                        execution_scope=execution_scope,
                        owner_id=owner_id,
                        fencing_token=token,
                        expires_at=now + lease_duration,
                        updated_at=now,
                    )
                )
            elif lease.owner_id != owner_id and lease.expires_at > now:
                rejections.reject("SENDER_LEASE_HELD", "another sender still owns the live lease")
            elif lease.owner_id == owner_id and lease.expires_at > now:
                token = lease.fencing_token
                lease.expires_at = now + lease_duration
                lease.updated_at = now
            else:
                token = lease.fencing_token + 1
                lease.owner_id = owner_id
                lease.fencing_token = token
                lease.expires_at = now + lease_duration
                lease.updated_at = now
            self.transactions.audit(
                session,
                actor_id=str(actor_id),
                event_type="SENDER_REDUCE_ONLY_LEASE_ACQUIRED",
                object_type="SenderLease",
                object_id=execution_scope,
                reason=f"intent={intent.intent_id};reduce_only=true",
                correlation_id=intent.correlation_id,
                object_version=token,
                workspace_id=team.workspace_id,
                team_id=team.team_id,
                account_id=account_id,
                now=now,
            )
            return token

    def _validate_sender(
        self,
        session: Session,
        team_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        now: datetime,
    ) -> None:
        self.transactions.validate_sender_lease(
            session,
            team_id,
            execution_scope,
            owner_id,
            fencing_token,
            now,
        )

    def validate_sender(
        self,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        actor_id: UUID,
        now: datetime,
    ) -> None:
        with self.database.session_factory() as session:
            _environment, account_id, venue = scope_rules.scope_parts(execution_scope)
            team = self.transactions.require_role(
                session, actor_id, "sender.manage", account_id, venue
            )
            self._validate_sender(
                session,
                team.team_id,
                execution_scope,
                owner_id,
                fencing_token,
                now,
            )
