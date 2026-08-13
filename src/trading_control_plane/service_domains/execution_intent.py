from __future__ import annotations

from trading_control_plane.service_component import ServiceComponent

# The domain implementation intentionally consumes the explicit service_core export surface.
# ruff: noqa: F403, F405
from trading_control_plane.service_core import *


class IntentExecutionService(ServiceComponent):
    def create_order_intent(
        self,
        authorization_id: UUID,
        actor_id: UUID,
        kind: IntentKind,
        account_id: str,
        venue: str,
        instrument_id: UUID,
        direction: Direction,
        quantity: Decimal,
        idempotency_key: str,
        *,
        add_candidate: AddCandidateFacts | None = None,
        now: datetime,
    ) -> IntentCreation:
        if kind not in {IntentKind.INITIAL, IntentKind.ADD}:
            _reject("NEW_RISK_INTENT_REQUIRED", "this entry point only creates INITIAL or ADD")
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
            authorization = session.get(TradingAuthorization, authorization_id)
            if authorization is None:
                _reject("AUTHORIZATION_INACTIVE", "authorization is missing or inactive")
            active_team = self.transactions._require_role(
                session,
                actor_id,
                operation,
                authorization.account_id,
                authorization.venue,
                team_id=authorization.team_id,
            )
            proposal_environment = ExecutionEnvironment(
                session.scalar(
                    select(Proposal.environment).where(
                        Proposal.proposal_id == authorization.proposal_id
                    )
                )
                or ExecutionEnvironment.SHADOW.value
            )
            self.transactions._require_team_environment(active_team, proposal_environment)
            digest, response = self.transactions._idempotency(
                session,
                caller_id=f"{actor_id}:{active_team.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload={**payload, "team_id": str(active_team.team_id)},
            )
            if response is not None:
                return self.facade._intent_creation(response)
            self.transactions._lock_risk_capacity(session, authorization.team_id)
            authorization = session.get(
                TradingAuthorization,
                authorization_id,
                with_for_update=True,
                populate_existing=True,
            )
            if authorization is None:
                _reject("AUTHORIZATION_INACTIVE", "authorization is missing or inactive")
            policy = self.facade._active_risk_policy(session, authorization.team_id)
            auto_add_gate: CapabilityGate | None = None
            if kind is IntentKind.ADD:
                auto_add_gate = session.get(CapabilityGate, "AUTO_ADD", with_for_update=True)
                if auto_add_gate is None or auto_add_gate.status != CapabilityStatus.ENABLED.value:
                    _reject("AUTO_ADD_DISABLED", "automatic add capability is disabled")
            if not authorization.active:
                _reject("AUTHORIZATION_INACTIVE", "authorization is missing or inactive")
            if authorization.expires_at <= now:
                _reject("AUTHORIZATION_EXPIRED", "authorization expired")
            if (
                authorization.account_id != account_id
                or authorization.venue != venue
                or authorization.instrument_id != instrument_id
                or authorization.direction != direction.value
            ):
                _reject("AUTHORIZATION_SCOPE_MISMATCH", "request exceeds frozen scope")
            if (
                quantity <= 0
                or authorization.used_quantity + quantity > authorization.quantity_limit
            ):
                _reject("AUTHORIZATION_QUANTITY_EXCEEDED", "request exceeds quantity limit")
            proposal = session.get(Proposal, authorization.proposal_id, with_for_update=True)
            if proposal is None or proposal.status != ProposalStatus.APPROVED.value:
                _reject("PROPOSAL_NOT_APPROVED", "authorization proposal is not approved")
            if proposal.expires_at <= now:
                _reject("PROPOSAL_EXPIRED", "authorization proposal expired")
            if (
                authorization.quantity_limit > proposal.quantity
                or authorization.risk_limit > proposal.max_risk
            ):
                _reject("AUTHORIZATION_SCOPE_MISMATCH", "authorization exceeds proposal caps")
            if kind is IntentKind.INITIAL:
                existing_initial = session.scalar(
                    select(OrderIntent.intent_id)
                    .join(Campaign, Campaign.campaign_id == OrderIntent.campaign_id)
                    .where(
                        Campaign.proposal_id == proposal.proposal_id,
                        OrderIntent.kind == IntentKind.INITIAL.value,
                    )
                    .limit(1)
                )
                if existing_initial is not None:
                    _reject(
                        "INITIAL_INTENT_ALREADY_EXISTS",
                        "this frozen proposal already produced its one initial intent",
                    )

            occupied_risk = self.facade._occupied_risk(session, proposal.team_id)
            risk_amount = authorization.risk_limit * quantity / authorization.quantity_limit
            if risk_amount <= 0 or risk_amount > authorization.risk_limit:
                _reject("AUTHORIZATION_RISK_EXCEEDED", "request exceeds risk authorization")
            inputs, _, _, effective_max_total_risk = self.facade._server_risk_context(
                session,
                proposal=proposal,
                policy=policy,
                kind=kind,
                requested_quantity=quantity,
                requested_risk=risk_amount,
                current_risk=occupied_risk,
                now=now,
            )
            final_outcome = evaluate_risk(
                self.facade._risk_policy_input(
                    policy,
                    effective_max_total_risk=(
                        effective_max_total_risk
                        if effective_max_total_risk > 0
                        else policy.max_total_risk
                    ),
                ),
                inputs,
            )
            if final_outcome.result is not RiskResult.ALLOW:
                reason = final_outcome.reasons[0] if final_outcome.reasons else "RISK_REJECTED"
                _reject("FINAL_RISK_CHECK_FAILED", reason)

            position: Position | None = None
            shadow_position: ShadowPosition | None = None
            shadow_account: TeamShadowAccount | None = None
            unified_shadow = (
                proposal.environment == ExecutionEnvironment.SHADOW.value
                and active_team.execution_mode_locked_at is not None
            )
            if unified_shadow:
                shadow_account = session.scalar(
                    select(TeamShadowAccount)
                    .where(
                        TeamShadowAccount.team_id == proposal.team_id,
                        TeamShadowAccount.status == "ACTIVE",
                    )
                    .with_for_update()
                )
                if shadow_account is None:
                    _reject(
                        "SHADOW_ACCOUNT_MISSING",
                        "Team virtual account is not initialized",
                    )
                shadow_instrument = session.scalar(
                    select(ShadowInstrument).where(
                        ShadowInstrument.team_id == proposal.team_id,
                        ShadowInstrument.catalog_instrument_id == instrument_id,
                    )
                )
                if shadow_instrument is not None:
                    shadow_position = session.scalar(
                        select(ShadowPosition)
                        .where(
                            ShadowPosition.shadow_account_id == shadow_account.shadow_account_id,
                            ShadowPosition.generation == shadow_account.generation,
                            ShadowPosition.shadow_instrument_id
                            == shadow_instrument.shadow_instrument_id,
                        )
                        .with_for_update()
                    )
            else:
                position = session.scalar(
                    select(Position)
                    .where(
                        Position.team_id == proposal.team_id,
                        Position.account_id == account_id,
                        Position.venue == venue,
                        Position.environment == proposal.environment,
                        Position.instrument_id == instrument_id,
                    )
                    .with_for_update()
                )
                if position is None or position.fact_status != FactStatus.KNOWN.value:
                    _reject("POSITION_UNKNOWN", "new risk requires a current known position fact")

            campaign = session.scalar(
                select(Campaign)
                .where(Campaign.authorization_id == authorization_id)
                .with_for_update()
            )
            campaign_created = campaign is None
            if kind is IntentKind.INITIAL:
                if unified_shadow:
                    current_quantity = (
                        Decimal(0) if shadow_position is None else shadow_position.quantity
                    )
                else:
                    assert position is not None
                    current_quantity = position.quantity
                if current_quantity != 0:
                    _reject("POSITION_NOT_FLAT", "INITIAL requires a confirmed flat position")
                conflicting_campaign = session.scalar(
                    select(Campaign)
                    .where(
                        Campaign.team_id == proposal.team_id,
                        Campaign.account_id == account_id,
                        Campaign.venue == venue,
                        Campaign.environment == proposal.environment,
                        Campaign.instrument_id == instrument_id,
                        Campaign.status != CampaignStatus.CLOSED.value,
                    )
                    .with_for_update()
                )
                if conflicting_campaign is not None and (
                    campaign is None or conflicting_campaign.campaign_id != campaign.campaign_id
                ):
                    _reject("ACTIVE_CAMPAIGN_EXISTS", "scope already has an unclosed campaign")
                if campaign is None:
                    campaign = Campaign(
                        team_id=proposal.team_id,
                        proposal_id=authorization.proposal_id,
                        authorization_id=authorization_id,
                        account_id=authorization.account_id,
                        venue=authorization.venue,
                        environment=proposal.environment,
                        instrument_id=authorization.instrument_id,
                        direction=authorization.direction,
                        status=CampaignStatus.OPENING.value,
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
                    _reject(
                        "AUTHORIZATION_ADD_REVOKED",
                        "authorization AddUnits were permanently revoked by a tighten action",
                    )
                instrument = session.get(Instrument, proposal.instrument_id)
                if instrument is None:
                    _reject("INSTRUMENT_UNAVAILABLE", "proposal instrument is unavailable")
                self.facade._validate_add_candidate(
                    proposal=proposal,
                    instrument=instrument,
                    candidate=add_candidate,
                    policy=policy,
                    now=now,
                )
                if policy.system_state != SystemRiskState.NORMAL.value:
                    _reject("ADD_RISK_STATE_INVALID", "ADD requires NORMAL risk state")
                if authorization.used_adds >= authorization.allowed_adds:
                    _reject("ADD_LIMIT_EXHAUSTED", "authorization add count is exhausted")
                if campaign is None or campaign.status in {
                    CampaignStatus.CLOSED.value,
                    CampaignStatus.UNKNOWN.value,
                }:
                    _reject("ADD_CAMPAIGN_REQUIRED", "ADD requires an existing known campaign")
                expected_long = campaign.direction == Direction.LONG.value
                current_position = shadow_position if unified_shadow else position
                if (
                    current_position is None
                    or current_position.quantity == 0
                    or (current_position.quantity > 0) != expected_long
                ):
                    _reject("ADD_POSITION_INVALID", "ADD requires an existing aligned position")
                unrealized_pnl = (
                    current_position.mark_price - current_position.average_entry_price
                ) * current_position.quantity
                if unrealized_pnl <= 0:
                    _reject("ADD_NOT_PROFITABLE", "ADD requires strictly positive unrealized PnL")

            active = session.scalar(
                select(OrderIntent.intent_id).where(
                    OrderIntent.campaign_id == campaign.campaign_id,
                    OrderIntent.status.in_(ACTIVE_INTENT_STATUSES),
                )
            )
            if active is not None:
                _reject("ACTIVE_ORDER_INTENT", "campaign already has unresolved intent")
            occupied_account_risk = self.facade._occupied_risk(
                session,
                proposal.team_id,
                account_id=proposal.account_id,
                venue=proposal.venue,
            )
            if unified_shadow:
                assert shadow_account is not None
                risk_available = max(
                    Decimal(0),
                    shadow_account.available_balance - occupied_account_risk,
                )
                if risk_amount > risk_available:
                    _reject(
                        "SHADOW_CAPITAL_INSUFFICIENT",
                        "requested risk exceeds unreserved virtual capital",
                    )
            if (
                policy.max_account_risk is None
                or policy.max_single_loss is None
                or risk_amount > policy.max_single_loss
                or occupied_risk + risk_amount > policy.max_total_risk
                or occupied_account_risk + risk_amount > policy.max_account_risk
            ):
                _reject("RISK_CAPACITY_EXHAUSTED", "atomic risk capacity is exhausted")
            reservation = RiskReservation(
                team_id=proposal.team_id,
                campaign_id=campaign.campaign_id,
                authorization_id=authorization_id,
                status=ReservationStatus.RESERVED.value,
                amount=risk_amount,
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(reservation)
            session.flush()
            side = "BUY" if direction is Direction.LONG else "SELL"
            legacy_position_id = None
            legacy_position_observed_at = None
            if not unified_shadow:
                assert position is not None
                legacy_position_id = position.position_id
                legacy_position_observed_at = position.observed_at
            intent = OrderIntent(
                campaign_id=campaign.campaign_id,
                authorization_id=authorization_id,
                reservation_id=reservation.reservation_id,
                kind=kind.value,
                side=side,
                quantity=quantity,
                limit_price=self.facade._proposal_limit_price(proposal),
                reduce_only=False,
                trigger_source=(
                    None if add_candidate is None else f"PERPTAPE:{add_candidate.candidate_id}"
                ),
                trigger_observed_at=(None if add_candidate is None else add_candidate.observed_at),
                add_unit_consumed=False,
                target_version=None,
                position_id=legacy_position_id,
                position_observed_at=legacy_position_observed_at,
                status=OrderIntentStatus.READY.value,
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
            self.transactions._save_receipt(
                session,
                caller_id=f"{actor_id}:{active_team.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self.transactions._audit(
                session,
                actor_id=str(actor_id),
                event_type="ORDER_INTENT_PREPARED",
                object_type="OrderIntent",
                object_id=intent.intent_id,
                reason=kind.value,
                correlation_id=intent.correlation_id,
                object_version=intent.version,
                idempotency_key=idempotency_key,
                now=now,
            )
            if campaign_created:
                self.transactions._enqueue_campaign_status_notification(
                    session,
                    actor_id=str(actor_id),
                    campaign=campaign,
                    summary="初始订单意图已冻结, 交易任务进入开仓中状态。",
                    idempotency_key=idempotency_key,
                    correlation_id=intent.correlation_id,
                    now=now,
                )
            INTENT_TRANSITIONS.labels("CREATED", intent.status).inc()
            return IntentCreation(
                campaign_id=campaign.campaign_id,
                reservation_id=reservation.reservation_id,
                intent_id=intent.intent_id,
            )

    @staticmethod
    def _consume_add_unit(session: Session, intent: OrderIntent) -> None:
        if intent.kind != IntentKind.ADD.value or intent.add_unit_consumed:
            return
        authorization = session.get(
            TradingAuthorization, intent.authorization_id, with_for_update=True
        )
        if authorization is None or authorization.used_adds >= authorization.allowed_adds:
            _reject(
                "AUTHORIZATION_ADD_LIMIT_INVALID",
                "positive Add execution exceeds the authorized AddUnit count",
            )
        authorization.used_adds += 1
        intent.add_unit_consumed = True

    def mark_intent_unknown(
        self,
        intent_id: UUID,
        actor_id: UUID,
        reason: str,
        *,
        required_environment: ExecutionEnvironment | None = None,
        now: datetime,
    ) -> None:
        with self.database.session_factory.begin() as session:
            intent = session.get(OrderIntent, intent_id, with_for_update=True)
            if intent is None:
                _reject("ORDER_INTENT_NOT_FOUND", "intent does not exist")
            campaign = session.get(Campaign, intent.campaign_id)
            if campaign is None:
                _reject("CAMPAIGN_NOT_FOUND", "intent campaign does not exist")
            if (
                required_environment is not None
                and campaign.environment != required_environment.value
            ):
                _reject(
                    "EXECUTION_ENVIRONMENT_MISMATCH",
                    "intent is outside the requested execution environment",
                )
            self.transactions._require_role(
                session,
                actor_id,
                "order.prepare",
                campaign.account_id,
                campaign.venue,
                team_id=campaign.team_id,
            )
            if intent.status == OrderIntentStatus.UNKNOWN.value:
                return
            if intent.status not in ACTIVE_INTENT_STATUSES:
                _reject("ORDER_INTENT_NOT_ACTIVE", "only an active intent may become UNKNOWN")
            previous = intent.status
            intent.status = OrderIntentStatus.UNKNOWN.value
            intent.updated_at = now
            intent.version += 1
            if intent.reservation_id is not None:
                reservation = session.get(RiskReservation, intent.reservation_id)
                if reservation is not None:
                    reservation.status = ReservationStatus.UNKNOWN.value
                    reservation.updated_at = now
                    reservation.version += 1
            venue_order = session.scalar(
                select(VenueOrder).where(VenueOrder.order_intent_id == intent_id)
            )
            if venue_order is not None:
                venue_order.status = VenueOrderStatus.UNKNOWN.value
                venue_order.observed_at = now
                venue_order.updated_at = now
            campaign.status = CampaignStatus.UNKNOWN.value
            campaign.updated_at = now
            self.transactions._audit(
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
            self.transactions._enqueue_campaign_status_notification(
                session,
                actor_id=str(actor_id),
                campaign=campaign,
                summary="订单结果未知, 交易任务已进入阻断状态, 需先完成对账。",
                idempotency_key=f"intent-unknown-v{intent.version}",
                correlation_id=intent.correlation_id,
                now=now,
            )
            INTENT_TRANSITIONS.labels(previous, intent.status).inc()

    def release_unfilled_intent(
        self,
        intent_id: UUID,
        actor_id: UUID,
        terminal_status: OrderIntentStatus,
        reason: str,
        *,
        required_environment: ExecutionEnvironment | None = None,
        now: datetime,
    ) -> None:
        if terminal_status not in {OrderIntentStatus.CANCELLED, OrderIntentStatus.REJECTED}:
            _reject("INVALID_TERMINAL_STATUS", "only cancelled or rejected may release risk")
        with self.database.session_factory.begin() as session:
            intent = session.get(OrderIntent, intent_id, with_for_update=True)
            if intent is None:
                _reject("ORDER_INTENT_NOT_FOUND", "intent does not exist")
            campaign = session.get(Campaign, intent.campaign_id)
            if campaign is None:
                _reject("CAMPAIGN_NOT_FOUND", "intent campaign does not exist")
            if (
                required_environment is not None
                and campaign.environment != required_environment.value
            ):
                _reject(
                    "EXECUTION_ENVIRONMENT_MISMATCH",
                    "intent is outside the requested execution environment",
                )
            self.transactions._require_role(
                session,
                actor_id,
                "order.prepare",
                campaign.account_id,
                campaign.venue,
                team_id=campaign.team_id,
            )
            reservation = (
                session.get(RiskReservation, intent.reservation_id, with_for_update=True)
                if intent.reservation_id is not None
                else None
            )
            if intent.status == terminal_status.value:
                if reservation is None or reservation.status == ReservationStatus.RELEASED.value:
                    return
                _reject("RISK_RELEASE_INCOMPLETE", "terminal intent still occupies risk")
            if intent.status in {
                OrderIntentStatus.CANCELLED.value,
                OrderIntentStatus.REJECTED.value,
                OrderIntentStatus.FILLED.value,
            }:
                _reject("ORDER_INTENT_TERMINAL", "terminal intent cannot change outcome")
            if intent.status not in RELEASABLE_INTENT_STATUSES:
                _reject("ORDER_INTENT_NOT_RELEASABLE", "unknown intent cannot release risk")
            filled = session.scalar(
                select(func.coalesce(func.sum(VenueFill.quantity), 0)).where(
                    VenueFill.order_intent_id == intent_id
                )
            )
            if filled != 0:
                _reject("FILLED_INTENT_RISK_REQUIRED", "filled intent cannot release all risk")
            previous = intent.status
            intent.status = terminal_status.value
            intent.updated_at = now
            intent.version += 1
            if reservation is not None:
                if reservation.status == ReservationStatus.UNKNOWN.value:
                    _reject("RISK_RESERVATION_UNKNOWN", "unknown risk cannot be released")
                if reservation.status != ReservationStatus.RELEASED.value:
                    reservation.status = ReservationStatus.RELEASED.value
                    reservation.updated_at = now
                    reservation.version += 1
                    authorization = session.get(
                        TradingAuthorization, intent.authorization_id, with_for_update=True
                    )
                    if authorization is None or authorization.used_quantity < intent.quantity:
                        _reject(
                            "AUTHORIZATION_USAGE_INVALID", "authorization usage is inconsistent"
                        )
                    authorization.used_quantity -= intent.quantity
            close_unfilled_campaign = False
            if intent.kind == IntentKind.INITIAL.value:
                policy = session.scalar(
                    select(RiskPolicy).where(
                        RiskPolicy.team_id == campaign.team_id,
                        RiskPolicy.active,
                    )
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
                scope = _scope_key(campaign.environment, campaign.account_id, campaign.venue)
                latest_reconciliation = session.scalar(
                    select(ReconciliationRun)
                    .where(
                        ReconciliationRun.team_id == campaign.team_id,
                        ReconciliationRun.execution_scope == scope,
                    )
                    .order_by(ReconciliationRun.completed_at.desc())
                    .limit(1)
                )
                close_unfilled_campaign = bool(
                    policy is not None
                    and position is not None
                    and position.fact_status == FactStatus.KNOWN.value
                    and position.quantity == 0
                    and not self.facade._fact_is_stale(
                        position.observed_at,
                        now,
                        timedelta(seconds=policy.max_fact_age_seconds),
                    )
                    and latest_reconciliation is not None
                    and latest_reconciliation.status == ReconciliationStatus.MATCH.value
                    and latest_reconciliation.is_computed
                    and latest_reconciliation.completed_at >= position.observed_at
                )
                if close_unfilled_campaign:
                    assert position is not None
                    campaign.status = CampaignStatus.CLOSED.value
                    campaign.current_target_quantity = Decimal(0)
                    campaign.target_reason = reason
                    campaign.updated_at = now
                    authorization = session.get(
                        TradingAuthorization, campaign.authorization_id, with_for_update=True
                    )
                    if authorization is not None:
                        authorization.active = False
                    self.facade._update_campaign_pnl(session, campaign, position, now=now)
            venue_order = session.scalar(
                select(VenueOrder).where(VenueOrder.order_intent_id == intent_id)
            )
            if venue_order is not None:
                venue_order.status = (
                    VenueOrderStatus.CANCELLED.value
                    if terminal_status is OrderIntentStatus.CANCELLED
                    else VenueOrderStatus.REJECTED.value
                )
                venue_order.observed_at = now
                venue_order.updated_at = now
            self.transactions._audit(
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
                self.transactions._audit(
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
                self.transactions._enqueue_campaign_status_notification(
                    session,
                    actor_id=str(actor_id),
                    campaign=campaign,
                    summary="未成交的初始订单已终止, 且新鲜空仓事实确认任务关闭。",
                    idempotency_key=f"intent-terminal-v{intent.version}",
                    correlation_id=intent.correlation_id,
                    now=now,
                )
            INTENT_TRANSITIONS.labels(previous, intent.status).inc()

    def acquire_sender(
        self,
        execution_scope: str,
        owner_id: str,
        actor_id: UUID,
        now: datetime,
        lease_duration: timedelta = DEFAULT_SENDER_LEASE_DURATION,
    ) -> int:
        with self.database.session_factory.begin() as session:
            _environment, account_id, venue = _scope_parts(execution_scope)
            if not owner_id or lease_duration <= timedelta(0):
                _reject("SENDER_LEASE_INVALID", "owner and positive lease duration are required")
            team = self.transactions._require_role(
                session, actor_id, "sender.manage", account_id, venue
            )
            lease = session.get(
                SenderLease,
                (team.team_id, execution_scope),
                with_for_update=True,
            )
            if lease is None:
                token = 1
                session.add(
                    SenderLease(
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
                    _reject("SENDER_LEASE_HELD", "another sender still owns the live lease")
                latest = session.scalar(
                    select(ReconciliationRun)
                    .where(
                        ReconciliationRun.team_id == team.team_id,
                        ReconciliationRun.execution_scope == execution_scope,
                    )
                    .order_by(ReconciliationRun.completed_at.desc())
                    .limit(1)
                )
                policy = session.scalar(
                    select(RiskPolicy).where(
                        RiskPolicy.team_id == team.team_id,
                        RiskPolicy.active,
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
                    or latest.status != ReconciliationStatus.MATCH.value
                    or not latest.is_computed
                    or latest.completed_at <= lease.expires_at
                    or latest.completed_at > now
                    or now - latest.completed_at > max_age
                ):
                    _reject(
                        "RECONCILIATION_REQUIRED",
                        "sender takeover requires a fresh computed MATCH after lease expiry",
                    )
                token = lease.fencing_token + 1
                lease.owner_id = owner_id
                lease.fencing_token = token
                lease.expires_at = now + lease_duration
                lease.updated_at = now
            self.transactions._audit(
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

    def _validate_sender(
        self,
        session: Session,
        team_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        now: datetime,
    ) -> None:
        _scope_parts(execution_scope)
        lease = session.get(SenderLease, (team_id, execution_scope))
        if (
            lease is None
            or lease.owner_id != owner_id
            or lease.fencing_token != fencing_token
            or lease.expires_at <= now
        ):
            FENCING_REJECTIONS.inc()
            _reject("FENCING_TOKEN_REJECTED", "sender lease is stale, expired, or superseded")

    def validate_sender(
        self,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        actor_id: UUID,
        now: datetime,
    ) -> None:
        with self.database.session_factory() as session:
            _environment, account_id, venue = _scope_parts(execution_scope)
            team = self.transactions._require_role(
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

    def _require_exchange_account_live_ready(
        self,
        session: Session,
        *,
        team_id: UUID,
        account_id: str,
        venue: str,
    ) -> ExchangeAccount:
        account = session.scalar(
            select(ExchangeAccount).where(
                ExchangeAccount.team_id == team_id,
                ExchangeAccount.account_id == account_id,
                ExchangeAccount.venue == venue,
            )
        )
        if account is None or not account.active or account.trading_status != "ELIGIBLE":
            _reject(
                "EXCHANGE_ACCOUNT_TRADING_DISABLED",
                "LIVE venue action requires exact account trading eligibility",
            )
        if (
            account.connection_status != "VERIFIED"
            or account.credentials_ciphertext is None
            or account.credential_version < 1
            or not account.runtime_sync_enabled
            or account.runtime_service_principal_id is None
        ):
            _reject(
                "EXCHANGE_ACCOUNT_TRADING_NOT_READY",
                "LIVE venue action requires current account connection and runtime binding",
            )
        team = session.get(Team, team_id)
        if (
            team is None
            or not team.trading_enabled
            or team.execution_mode != TeamExecutionMode.LIVE.value
        ):
            _reject(
                "TEAM_LIVE_MODE_REQUIRED",
                "LIVE venue action requires an active LIVE team",
            )
        assert account.runtime_service_principal_id is not None
        self.facade._require_exact_runtime_principal(
            session,
            principal_id=account.runtime_service_principal_id,
            team=team,
            role=Role.OPERATOR,
            account_id=account.account_id,
            venue=account.venue,
            error_code="EXCHANGE_ACCOUNT_TRADING_NOT_READY",
            error_message="LIVE venue action requires the exact active read-only principal",
        )
        return account
