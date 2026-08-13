from __future__ import annotations

from trading_control_plane.service_component import ServiceComponent

# The domain implementation intentionally consumes the explicit service_core export surface.
# ruff: noqa: F403, F405
from trading_control_plane.service_core import *


class CampaignExecutionService(ServiceComponent):
    def update_campaign_target(
        self,
        campaign_id: UUID,
        actor_id: UUID,
        candidates: tuple[TargetCandidate, ...],
        *,
        now: datetime,
    ) -> TargetDecision:
        decision = select_target_position(candidates)
        with self.database.session_factory.begin() as session:
            campaign = session.get(Campaign, campaign_id, with_for_update=True)
            if campaign is None:
                _reject("CAMPAIGN_NOT_FOUND", "campaign does not exist")
            self.transactions._require_role(
                session,
                actor_id,
                "order.prepare",
                campaign.account_id,
                campaign.venue,
                team_id=campaign.team_id,
            )
            policy = session.scalar(
                select(RiskPolicy).where(
                    RiskPolicy.team_id == campaign.team_id,
                    RiskPolicy.active,
                )
            )
            if policy is None:
                _reject("RISK_POLICY_MISSING", "target update requires an active policy")
            position = session.scalar(
                select(Position)
                .where(
                    Position.team_id == campaign.team_id,
                    Position.account_id == campaign.account_id,
                    Position.venue == campaign.venue,
                    Position.environment == campaign.environment,
                    Position.instrument_id == campaign.instrument_id,
                )
                .with_for_update()
            )
            if position is None or position.fact_status != FactStatus.KNOWN.value:
                _reject("POSITION_UNKNOWN", "target update requires a known position")
            if now - position.observed_at > timedelta(seconds=policy.max_fact_age_seconds):
                _reject("STALE_FACTS", "target update requires a fresh position")
            if decision.target_quantity > abs(position.quantity):
                _reject("TARGET_EXCEEDS_POSITION", "target cannot exceed the current position")
            campaign.current_target_quantity = decision.target_quantity
            campaign.target_version += 1
            campaign.target_reason = ",".join(decision.reasons)
            campaign.target_urgency = decision.urgency.value
            campaign.target_calculated_at = now
            if decision.target_quantity == 0:
                campaign.status = CampaignStatus.CLOSING.value
            elif decision.target_quantity < abs(position.quantity):
                campaign.status = CampaignStatus.REDUCING.value
            else:
                campaign.status = CampaignStatus.OPEN.value
            campaign.updated_at = now
            self.transactions._audit(
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
        candidates: tuple[TargetCandidate, ...] | None = None,
        expected_target_version: int | None = None,
        limit_price: Decimal | None = None,
        now: datetime,
    ) -> UUID:
        operation = "order.reduce"
        with self.database.session_factory.begin() as session:
            campaign = session.get(Campaign, campaign_id, with_for_update=True)
            if campaign is None:
                _reject("CAMPAIGN_NOT_FOUND", "campaign does not exist")
            self.transactions._require_role(
                session,
                actor_id,
                "order.prepare",
                campaign.account_id,
                campaign.venue,
                team_id=campaign.team_id,
            )
            position = session.scalar(
                select(Position).where(
                    Position.team_id == campaign.team_id,
                    Position.account_id == campaign.account_id,
                    Position.venue == campaign.venue,
                    Position.environment == campaign.environment,
                    Position.instrument_id == campaign.instrument_id,
                )
            )
            if position is None or position.fact_status != FactStatus.KNOWN.value:
                _reject("POSITION_UNKNOWN", "reduction requires current known position")
            policy = session.scalar(
                select(RiskPolicy).where(
                    RiskPolicy.team_id == campaign.team_id,
                    RiskPolicy.active,
                )
            )
            if policy is None:
                _reject("RISK_POLICY_MISSING", "reduction requires an active policy")
            if now - position.observed_at > timedelta(seconds=policy.max_fact_age_seconds):
                _reject("STALE_FACTS", "reduction requires a fresh position")
            expected_long = campaign.direction == Direction.LONG.value
            if position.quantity == 0 or (position.quantity > 0) != expected_long:
                _reject("POSITION_DIRECTION_MISMATCH", "position does not match campaign direction")
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
                _reject("ORDER_LIMIT_PRICE_INVALID", "explicit reduction limit must be positive")
            digest, response = self.transactions._idempotency(
                session,
                caller_id=f"{actor_id}:{campaign.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload={**payload, "team_id": str(campaign.team_id)},
            )
            if response is not None:
                return _as_uuid(str(response["intent_id"]))
            if (
                expected_target_version is not None
                and campaign.target_version != expected_target_version
            ):
                _reject("VERSION_CONFLICT", "Campaign target changed before the action")
            active = session.scalar(
                select(OrderIntent.intent_id).where(
                    OrderIntent.campaign_id == campaign_id,
                    OrderIntent.status.in_(ACTIVE_INTENT_STATUSES),
                )
            )
            if active is not None:
                _reject("ACTIVE_ORDER_INTENT", "campaign already has unresolved intent")
            if candidates is not None:
                effective_candidates = candidates
                if campaign.target_calculated_at is not None:
                    try:
                        existing_urgency = TargetUrgency(
                            campaign.target_urgency or TargetUrgency.NORMAL.value
                        )
                    except ValueError:
                        _reject("CAMPAIGN_TARGET_INVALID", "stored target urgency is invalid")
                    effective_candidates += (
                        TargetCandidate(
                            campaign.current_target_quantity,
                            existing_urgency,
                            campaign.target_reason or "existing campaign target",
                        ),
                    )
                decision = select_target_position(effective_candidates)
                if decision.target_quantity > abs(position.quantity):
                    _reject("TARGET_EXCEEDS_POSITION", "target cannot exceed the current position")
                campaign.current_target_quantity = decision.target_quantity
                campaign.target_version += 1
                campaign.target_reason = ",".join(decision.reasons)
                campaign.target_urgency = decision.urgency.value
                campaign.target_calculated_at = now
                campaign.status = (
                    CampaignStatus.CLOSING.value
                    if decision.target_quantity == 0
                    else CampaignStatus.REDUCING.value
                )
                campaign.updated_at = now
            reduction_quantity = abs(position.quantity) - campaign.current_target_quantity
            if reduction_quantity <= 0:
                _reject("TARGET_NOT_REDUCING", "target does not reduce current position")
            side = "SELL" if position.quantity > 0 else "BUY"
            kind = IntentKind.EXIT if campaign.current_target_quantity == 0 else IntentKind.REDUCE
            intent = OrderIntent(
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
                status=OrderIntentStatus.READY.value,
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
            self.transactions._save_receipt(
                session,
                caller_id=f"{actor_id}:{campaign.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self.transactions._audit(
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
            campaign = session.get(Campaign, campaign_id, with_for_update=True)
            if campaign is None:
                _reject("CAMPAIGN_NOT_FOUND", "campaign does not exist")
            self.transactions._require_role(
                session,
                actor_id,
                "risk.tighten",
                campaign.account_id,
                campaign.venue,
                team_id=campaign.team_id,
            )
            digest, response = self.transactions._idempotency(
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
                    None if intent_value is None else _as_uuid(str(intent_value)),
                )
            if limit_price is not None and (not limit_price.is_finite() or limit_price <= 0):
                _reject("ORDER_LIMIT_PRICE_INVALID", "explicit exit limit must be positive")
            proposal = session.get(Proposal, campaign.proposal_id)
            position = session.scalar(
                select(Position)
                .where(
                    Position.team_id == campaign.team_id,
                    Position.account_id == campaign.account_id,
                    Position.venue == campaign.venue,
                    Position.environment == campaign.environment,
                    Position.instrument_id == campaign.instrument_id,
                )
                .with_for_update()
            )
            policy = self.facade._active_risk_policy(session, campaign.team_id)
            if proposal is None or policy is None:
                _reject("CAMPAIGN_MANAGEMENT_INVALID", "campaign management facts are incomplete")
            if position is None or position.fact_status != FactStatus.KNOWN.value:
                _reject("POSITION_UNKNOWN", "automatic exit requires a known current position")
            if self.facade._fact_is_stale(
                position.observed_at,
                now,
                timedelta(seconds=policy.max_fact_age_seconds),
            ):
                _reject("STALE_FACTS", "automatic exit requires a fresh position")
            if position.quantity == 0:
                result: dict[str, Any] = {"reason": "POSITION_FLAT", "intent_id": None}
                self.transactions._save_receipt(
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
                _reject(
                    "PROPOSAL_EXIT_CONTRACT_INVALID",
                    "frozen proposal lacks an invalidation price",
                )
            try:
                invalidation = Decimal(str(details["invalidation_price"]))
            except (ArithmeticError, TypeError, ValueError):
                _reject(
                    "PROPOSAL_EXIT_CONTRACT_INVALID",
                    "frozen invalidation price is invalid",
                )
            if not invalidation.is_finite() or invalidation <= 0:
                _reject(
                    "PROPOSAL_EXIT_CONTRACT_INVALID",
                    "frozen invalidation price is invalid",
                )
            kill_switch = policy.system_state == SystemRiskState.KILL_SWITCH.value
            invalidated = (
                position.mark_price <= invalidation
                if campaign.direction == Direction.LONG.value
                else position.mark_price >= invalidation
            )
            if not kill_switch and not invalidated:
                result = {"reason": "EXIT_TRIGGER_NOT_MET", "intent_id": None}
                self.transactions._save_receipt(
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
                select(OrderIntent.intent_id).where(
                    OrderIntent.campaign_id == campaign_id,
                    OrderIntent.status.in_(ACTIVE_INTENT_STATUSES),
                )
            )
            if active is not None:
                _reject("ACTIVE_ORDER_INTENT", "campaign already has unresolved intent")
            existing_reason = () if campaign.target_reason is None else (campaign.target_reason,)
            target_reasons = tuple(sorted({*existing_reason, reason}))
            campaign.current_target_quantity = Decimal(0)
            campaign.target_version += 1
            campaign.target_reason = ",".join(target_reasons)
            campaign.target_urgency = TargetUrgency.IMMEDIATE.value
            campaign.target_calculated_at = now
            campaign.status = CampaignStatus.CLOSING.value
            campaign.updated_at = now
            intent = OrderIntent(
                campaign_id=campaign_id,
                authorization_id=campaign.authorization_id,
                reservation_id=None,
                kind=IntentKind.EXIT.value,
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
                status=OrderIntentStatus.READY.value,
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
            self.transactions._save_receipt(
                session,
                caller_id=f"{actor_id}:{campaign.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self.transactions._audit(
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
            campaign = session.get(Campaign, campaign_id, with_for_update=True)
            if campaign is None:
                _reject("CAMPAIGN_NOT_FOUND", "campaign does not exist")
            self.transactions._require_role(
                session,
                actor_id,
                "order.prepare",
                campaign.account_id,
                campaign.venue,
                team_id=campaign.team_id,
            )
            if campaign.status == CampaignStatus.CLOSED.value:
                return
            policy = session.scalar(
                select(RiskPolicy).where(
                    RiskPolicy.team_id == campaign.team_id,
                    RiskPolicy.active,
                )
            )
            position = session.scalar(
                select(Position)
                .where(
                    Position.team_id == campaign.team_id,
                    Position.account_id == campaign.account_id,
                    Position.venue == campaign.venue,
                    Position.environment == campaign.environment,
                    Position.instrument_id == campaign.instrument_id,
                )
                .with_for_update()
            )
            if (
                policy is None
                or position is None
                or position.fact_status != FactStatus.KNOWN.value
                or position.quantity != 0
                or self.facade._fact_is_stale(
                    position.observed_at,
                    now,
                    timedelta(seconds=policy.max_fact_age_seconds),
                )
            ):
                _reject("CAMPAIGN_POSITION_NOT_CLOSED", "campaign requires a fresh flat position")
            exit_intent = session.scalar(
                select(OrderIntent)
                .where(
                    OrderIntent.campaign_id == campaign_id,
                    OrderIntent.kind == IntentKind.EXIT.value,
                )
                .order_by(OrderIntent.created_at.desc())
                .limit(1)
                .with_for_update()
            )
            terminal_statuses = {
                OrderIntentStatus.FILLED.value,
                OrderIntentStatus.CANCELLED.value,
                OrderIntentStatus.REJECTED.value,
            }
            if exit_intent is None or exit_intent.status not in terminal_statuses:
                _reject("CAMPAIGN_EXIT_NOT_TERMINAL", "campaign exit is not terminal")
            scope = _scope_key(campaign.environment, campaign.account_id, campaign.venue)
            latest = session.scalar(
                select(ReconciliationRun)
                .where(
                    ReconciliationRun.team_id == campaign.team_id,
                    ReconciliationRun.execution_scope == scope,
                )
                .order_by(ReconciliationRun.completed_at.desc())
                .limit(1)
            )
            if (
                latest is None
                or latest.status != ReconciliationStatus.MATCH.value
                or not latest.is_computed
                or latest.completed_at < position.observed_at
                or latest.completed_at < exit_intent.updated_at
            ):
                _reject("RECONCILIATION_REQUIRED", "campaign closure requires a current MATCH")
            reservations = session.scalars(
                select(RiskReservation)
                .where(RiskReservation.campaign_id == campaign_id)
                .with_for_update()
            ).all()
            if any(
                reservation.status
                in {ReservationStatus.UNKNOWN.value, ReservationStatus.RESERVED.value}
                for reservation in reservations
            ):
                _reject("RISK_RESERVATION_UNRESOLVED", "campaign risk is not confirmed closed")
            previous_pnl = campaign.final_pnl
            self._update_campaign_pnl(session, campaign, position, now=now)
            self._apply_shadow_pnl_delta(
                session,
                campaign=campaign,
                previous_pnl=previous_pnl,
                actor_id=actor_id,
                correlation_id=uuid4(),
                now=now,
            )
            for reservation in reservations:
                if reservation.status == ReservationStatus.OPEN.value:
                    reservation.status = ReservationStatus.RELEASED.value
                    reservation.version += 1
                    reservation.updated_at = now
            authorization = session.get(
                TradingAuthorization, campaign.authorization_id, with_for_update=True
            )
            if authorization is not None:
                authorization.active = False
            campaign.status = CampaignStatus.CLOSED.value
            campaign.current_target_quantity = Decimal(0)
            campaign.updated_at = now
            correlation_id = uuid4()
            self.transactions._audit(
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
            self.transactions._enqueue_campaign_status_notification(
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
    ) -> PnlBreakdown:
        with self.database.session_factory.begin() as session:
            campaign = session.get(Campaign, campaign_id, with_for_update=True)
            if campaign is None:
                _reject("CAMPAIGN_NOT_FOUND", "campaign does not exist")
            self.transactions._require_role(
                session,
                actor_id,
                "view",
                campaign.account_id,
                campaign.venue,
                team_id=campaign.team_id,
            )
            fills = session.scalars(
                select(VenueFill)
                .where(VenueFill.campaign_id == campaign_id)
                .order_by(VenueFill.executed_at, VenueFill.venue_fill_fact_id)
            ).all()
            position = session.scalar(
                select(Position).where(
                    Position.team_id == campaign.team_id,
                    Position.account_id == campaign.account_id,
                    Position.venue == campaign.venue,
                    Position.environment == campaign.environment,
                    Position.instrument_id == campaign.instrument_id,
                )
            )
            if position is None or position.fact_status != FactStatus.KNOWN.value:
                _reject("POSITION_UNKNOWN", "PnL requires known current position")
            previous_pnl = campaign.final_pnl
            result = self._update_campaign_pnl(
                session,
                campaign,
                position,
                fills=fills,
                now=now,
            )
            self._apply_shadow_pnl_delta(
                session,
                campaign=campaign,
                previous_pnl=previous_pnl,
                actor_id=actor_id,
                correlation_id=uuid4(),
                now=now,
            )
            return result

    def _apply_shadow_pnl_delta(
        self,
        session: Session,
        *,
        campaign: Campaign,
        previous_pnl: Decimal,
        actor_id: UUID,
        correlation_id: UUID,
        now: datetime,
        equity: AccountEquity | None = None,
    ) -> None:
        if campaign.environment != ExecutionEnvironment.SHADOW.value:
            return
        instrument = session.get(Instrument, campaign.instrument_id)
        if instrument is None:
            _reject("INSTRUMENT_UNAVAILABLE", "campaign instrument is missing")
        if equity is None:
            equity = session.scalar(
                select(AccountEquity)
                .where(
                    AccountEquity.team_id == campaign.team_id,
                    AccountEquity.account_id == campaign.account_id,
                    AccountEquity.venue == campaign.venue,
                    AccountEquity.environment == ExecutionEnvironment.SHADOW.value,
                    AccountEquity.currency == instrument.collateral_currency,
                )
                .with_for_update()
            )
        if (
            equity is None
            or equity.fact_status != FactStatus.KNOWN.value
            or equity.control_status != "CONTROLLED"
        ):
            _reject(
                "SHADOW_EQUITY_REQUIRED",
                "SHADOW PnL requires known controlled virtual equity",
            )
        delta = campaign.final_pnl - previous_pnl
        if delta == 0:
            return
        next_equity = equity.equity + delta
        next_available = equity.available_balance + delta
        if next_equity < 0 or next_available < 0:
            _reject(
                "SHADOW_CAPITAL_EXHAUSTED",
                "simulated loss exceeds the remaining virtual capital",
            )
        equity.equity = next_equity
        equity.available_balance = next_available
        equity.valuation_currency = "USD"
        equity.valuation_price = Decimal(1)
        equity.valuation_equity = next_equity
        equity.valuation_observed_at = now
        equity.fact_status = FactStatus.KNOWN.value
        equity.observed_at = now
        equity.updated_at = now
        self.facade._record_account_equity_observation(session, equity, recorded_at=now)
        self.transactions._audit(
            session,
            actor_id=str(actor_id),
            event_type="SHADOW_EQUITY_MARKED_TO_MODEL",
            object_type="AccountEquity",
            object_id=equity.account_equity_id,
            reason=f"campaign={campaign.campaign_id};pnl_delta={delta}",
            correlation_id=correlation_id,
            object_version=campaign.target_version,
            workspace_id=None,
            team_id=campaign.team_id,
            account_id=campaign.account_id,
            now=now,
        )

    def _update_campaign_pnl(
        self,
        session: Session,
        campaign: Campaign,
        position: Position,
        *,
        now: datetime,
        fills: Sequence[VenueFill] | None = None,
    ) -> PnlBreakdown:
        campaign_fills = (
            fills
            if fills is not None
            else list(
                session.scalars(
                    select(VenueFill)
                    .where(VenueFill.campaign_id == campaign.campaign_id)
                    .order_by(VenueFill.executed_at, VenueFill.venue_fill_fact_id)
                ).all()
            )
        )
        instrument = session.get(Instrument, campaign.instrument_id)
        if instrument is None:
            _reject("INSTRUMENT_UNAVAILABLE", "campaign instrument is missing")
        payments = session.scalars(
            select(FundingPayment).where(FundingPayment.campaign_id == campaign.campaign_id)
        ).all()
        if any(
            fill.fee_currency != instrument.collateral_currency for fill in campaign_fills
        ) or any(payment.currency != instrument.collateral_currency for payment in payments):
            _reject("PNL_CURRENCY_MISMATCH", "PnL requires an explicit FX conversion")
        funding = sum((payment.amount for payment in payments), Decimal(0))
        result = compute_pnl(
            fills=tuple(
                EconomicFill(
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
