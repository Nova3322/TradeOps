from __future__ import annotations

from trading_control_plane.service_core import (
    ACTIVE_INTENT_STATUSES,
    CAPITAL_HISTORY_MIN_INTERVAL,
    FENCING_REJECTIONS,
    INTENT_TRANSITIONS,
    MAX_FACT_CLOCK_SKEW,
    PROTECTION_ISSUES,
    RELEASABLE_INTENT_STATUSES,
    USD_STABLE_ASSETS,
    UUID,
    AbstractContextManager,
    AccountEquity,
    AccountEquityObservation,
    AddCandidateFacts,
    Any,
    BinanceReadOnlySnapshot,
    BinanceTestnetOrder,
    BinanceTestnetOrderCommand,
    BinanceTestnetProtectionCommand,
    Campaign,
    CampaignStatus,
    CapabilityGate,
    CapabilityStatus,
    Decimal,
    Direction,
    EconomicFill,
    ExchangeAccount,
    ExecutionEnvironment,
    FactStatus,
    FreqtradeEntryCommand,
    FreqtradeExitCommand,
    FreqtradeTrade,
    FundingPayment,
    HyperliquidReadOnlySnapshot,
    HyperliquidTestnetOrder,
    HyperliquidTestnetOrderCommand,
    HyperliquidTestnetProtectionCommand,
    IdempotencyConflict,
    Instrument,
    IntentCreation,
    IntentKind,
    OrderIntent,
    OrderIntentStatus,
    PnlBreakdown,
    Position,
    PreparedRuntimeAccountBinding,
    Proposal,
    ProposalStatus,
    ProtectionCancelCommand,
    ProtectionOrder,
    ProtectionStatus,
    ReconciliationRun,
    ReconciliationStatus,
    ReservationStatus,
    RiskPolicy,
    RiskReservation,
    RiskResult,
    Role,
    RoleAssignment,
    RuntimeSourceHealth,
    SenderLease,
    Sequence,
    ServiceMixinBase,
    Session,
    SystemRiskState,
    TargetCandidate,
    TargetDecision,
    TargetUrgency,
    Team,
    TeamExecutionMode,
    TradingAuthorization,
    VenueFill,
    VenueOrder,
    VenueOrderStatus,
    VenueReadOnlySnapshot,
    _as_uuid,
    _reject,
    _scope_key,
    _scope_parts,
    apply_shadow_fill,
    base64,
    binascii,
    compute_pnl,
    datetime,
    evaluate_risk,
    freqtrade_pair,
    func,
    hashlib,
    nullcontext,
    quote_shadow_execution,
    select,
    select_target_position,
    timedelta,
    uuid4,
)

DEFAULT_SENDER_LEASE_DURATION = timedelta(minutes=1)
DEFAULT_FREQTRADE_LEVERAGE = Decimal(1)


class ExecutionServiceMixin(ServiceMixinBase):
    """Order intent, venue execution, shadow facts, campaign, and position transactions."""

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
            active_team = self._require_role(
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
            self._require_team_environment(active_team, proposal_environment)
            digest, response = self._idempotency(
                session,
                caller_id=f"{actor_id}:{active_team.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload={**payload, "team_id": str(active_team.team_id)},
            )
            if response is not None:
                return self._intent_creation(response)
            self._lock_risk_capacity(session, authorization.team_id)
            authorization = session.get(
                TradingAuthorization,
                authorization_id,
                with_for_update=True,
                populate_existing=True,
            )
            if authorization is None:
                _reject("AUTHORIZATION_INACTIVE", "authorization is missing or inactive")
            policy = self._active_risk_policy(session, authorization.team_id)
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

            occupied_risk = self._occupied_risk(session, proposal.team_id)
            risk_amount = authorization.risk_limit * quantity / authorization.quantity_limit
            if risk_amount <= 0 or risk_amount > authorization.risk_limit:
                _reject("AUTHORIZATION_RISK_EXCEEDED", "request exceeds risk authorization")
            inputs, _, _, effective_max_total_risk = self._server_risk_context(
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
                self._risk_policy_input(
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
                if position.quantity != 0:
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
                self._validate_add_candidate(
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
                if position.quantity == 0 or (position.quantity > 0) != expected_long:
                    _reject("ADD_POSITION_INVALID", "ADD requires an existing aligned position")
                unrealized_pnl = (
                    position.mark_price - position.average_entry_price
                ) * position.quantity
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
            occupied_account_risk = self._occupied_risk(
                session,
                proposal.team_id,
                account_id=proposal.account_id,
                venue=proposal.venue,
            )
            if proposal.environment == ExecutionEnvironment.SHADOW.value:
                instrument = session.get(Instrument, proposal.instrument_id)
                shadow_equity = (
                    None
                    if instrument is None
                    else session.scalar(
                        select(AccountEquity)
                        .where(
                            AccountEquity.team_id == proposal.team_id,
                            AccountEquity.account_id == proposal.account_id,
                            AccountEquity.venue == proposal.venue,
                            AccountEquity.environment == ExecutionEnvironment.SHADOW.value,
                            AccountEquity.currency == instrument.collateral_currency,
                        )
                        .with_for_update()
                    )
                )
                if (
                    shadow_equity is None
                    or shadow_equity.fact_status != FactStatus.KNOWN.value
                    or shadow_equity.control_status != "CONTROLLED"
                ):
                    _reject(
                        "SHADOW_EQUITY_REQUIRED",
                        "new virtual risk requires controlled SHADOW capital",
                    )
                risk_available = max(
                    Decimal(0),
                    shadow_equity.available_balance - occupied_account_risk,
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
            intent = OrderIntent(
                campaign_id=campaign.campaign_id,
                authorization_id=authorization_id,
                reservation_id=reservation.reservation_id,
                kind=kind.value,
                side=side,
                quantity=quantity,
                limit_price=self._proposal_limit_price(proposal),
                reduce_only=False,
                trigger_source=(
                    None if add_candidate is None else f"PERPTAPE:{add_candidate.candidate_id}"
                ),
                trigger_observed_at=(None if add_candidate is None else add_candidate.observed_at),
                add_unit_consumed=False,
                target_version=None,
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
            authorization.used_quantity += quantity
            session.flush()
            result = {
                "campaign_id": str(campaign.campaign_id),
                "reservation_id": str(reservation.reservation_id),
                "intent_id": str(intent.intent_id),
            }
            self._save_receipt(
                session,
                caller_id=f"{actor_id}:{active_team.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self._audit(
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
                self._enqueue_campaign_status_notification(
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
            self._require_role(
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
            self._audit(
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
            self._enqueue_campaign_status_notification(
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
            self._require_role(
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
                policy = session.scalar(select(RiskPolicy).where(RiskPolicy.active))
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
                    and not self._fact_is_stale(
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
                    self._update_campaign_pnl(session, campaign, position, now=now)
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
            self._audit(
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
                self._audit(
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
                self._enqueue_campaign_status_notification(
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
            team = self._require_role(session, actor_id, "sender.manage", account_id, venue)
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
            self._audit(
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
            team = self._require_role(session, actor_id, "sender.manage", account_id, venue)
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
        self._require_exact_runtime_principal(
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

    @staticmethod
    def _binance_client_order_id(intent_id: UUID) -> str:
        encoded = base64.urlsafe_b64encode(intent_id.bytes).rstrip(b"=").decode("ascii")
        return f"tcp-{encoded}"

    @staticmethod
    def _binance_protection_client_order_id(position_id: UUID) -> str:
        encoded = base64.urlsafe_b64encode(position_id.bytes).rstrip(b"=").decode("ascii")
        return f"tpp-{encoded}"

    @staticmethod
    def _hyperliquid_client_order_id(intent_id: UUID) -> str:
        return f"0x{intent_id.hex}"

    @staticmethod
    def _hyperliquid_protection_client_order_id(position_id: UUID) -> str:
        return f"0x{position_id.hex}"

    def _binance_testnet_command(
        self,
        session: Session,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        now: datetime,
        allowed_statuses: set[str],
        *,
        venue: str = "BINANCE",
        require_limit_price: bool = False,
        environment: ExecutionEnvironment = ExecutionEnvironment.TESTNET,
    ) -> BinanceTestnetOrderCommand:
        intent = session.get(OrderIntent, intent_id)
        if intent is None:
            _reject("ORDER_INTENT_NOT_FOUND", "intent does not exist")
        campaign = session.get(Campaign, intent.campaign_id)
        if campaign is None:
            _reject("CAMPAIGN_NOT_FOUND", "intent campaign is missing")
        self._validate_sender(
            session,
            campaign.team_id,
            execution_scope,
            owner_id,
            fencing_token,
            now,
        )
        if campaign.environment != environment.value or campaign.venue != venue:
            _reject(
                f"{venue}_{environment.value}_SCOPE_REQUIRED",
                f"{venue} execution only accepts its own {environment.value} campaigns",
            )
        if execution_scope != _scope_key(campaign.environment, campaign.account_id, campaign.venue):
            _reject("EXECUTION_SCOPE_MISMATCH", "sender scope does not match campaign scope")
        self._require_role(
            session,
            actor_id,
            "venue.record",
            campaign.account_id,
            campaign.venue,
            team_id=campaign.team_id,
        )
        if environment is ExecutionEnvironment.LIVE:
            live_gate = session.get(CapabilityGate, "LIVE_ORDER_SEND")
            if live_gate is None or live_gate.status != CapabilityStatus.ENABLED.value:
                _reject(
                    "LIVE_ORDER_SEND_DISABLED",
                    "LIVE order send requires the explicit capability gate",
                )
            self._require_exchange_account_live_ready(
                session,
                team_id=campaign.team_id,
                account_id=campaign.account_id,
                venue=campaign.venue,
            )
        if intent.status not in allowed_statuses:
            _reject("ORDER_INTENT_STATE_INVALID", "intent state does not allow this venue action")
        if intent.status == OrderIntentStatus.FILLED.value and intent.updated_at < now - timedelta(
            hours=24
        ):
            _reject(
                "ORDER_INTENT_STATE_INVALID",
                "filled intent replay window has expired",
            )
        instrument = session.get(Instrument, campaign.instrument_id)
        if instrument is None or not instrument.active or instrument.venue != venue:
            _reject("INSTRUMENT_UNAVAILABLE", "campaign instrument is unavailable")
        if intent.quantity % instrument.lot_size != 0:
            _reject("INSTRUMENT_QUANTITY_INVALID", "intent quantity is not aligned to lot size")
        if require_limit_price:
            if intent.limit_price is None or intent.limit_price <= 0:
                _reject(
                    "HYPERLIQUID_LIMIT_PRICE_REQUIRED",
                    "Hyperliquid IOC execution requires an explicit frozen limit price",
                )
            if intent.limit_price % instrument.tick_size != 0:
                _reject("INSTRUMENT_PRICE_INVALID", "intent price exceeds current price precision")
        if intent.status == OrderIntentStatus.READY.value and not intent.reduce_only:
            authorization = session.get(TradingAuthorization, intent.authorization_id)
            proposal = (
                None if authorization is None else session.get(Proposal, authorization.proposal_id)
            )
            policy = session.scalar(
                select(RiskPolicy).where(
                    RiskPolicy.team_id == campaign.team_id,
                    RiskPolicy.active,
                )
            )
            if (
                authorization is None
                or proposal is None
                or not authorization.active
                or authorization.expires_at <= now
                or proposal.expires_at <= now
            ):
                _reject("AUTHORIZATION_EXPIRED", "new-risk intent authorization is no longer valid")
            if (
                intent.kind == IntentKind.ADD.value
                and intent.status == OrderIntentStatus.READY.value
                and authorization.add_revoked_at is not None
            ):
                _reject(
                    "AUTHORIZATION_ADD_REVOKED",
                    "a tighten action permanently revoked this unsent Add intent",
                )
            if policy is None:
                _reject("RISK_POLICY_UNKNOWN", "active risk policy is unavailable")
            if any(
                value is None
                for value in (
                    policy.max_account_risk,
                    policy.max_single_loss,
                    policy.max_consecutive_losses,
                    policy.loss_cooldown_seconds,
                )
            ):
                _reject("RISK_LIMITS_UNCONFIGURED", "team risk limits are not configured")
            state = SystemRiskState(policy.system_state)
            if state is SystemRiskState.KILL_SWITCH:
                _reject("KILL_SWITCH", "new-risk venue send is blocked")
            if state is SystemRiskState.REDUCE_ONLY:
                _reject("REDUCE_ONLY", "new-risk venue send is blocked")
            if state is SystemRiskState.NO_PYRAMID and intent.kind == IntentKind.ADD.value:
                _reject("PYRAMID_DISABLED", "Add venue send is blocked")
            position = session.scalar(
                select(Position).where(
                    Position.team_id == campaign.team_id,
                    Position.account_id == campaign.account_id,
                    Position.venue == campaign.venue,
                    Position.environment == campaign.environment,
                    Position.instrument_id == campaign.instrument_id,
                )
            )
            equity = session.scalar(
                select(AccountEquity).where(
                    AccountEquity.team_id == campaign.team_id,
                    AccountEquity.account_id == campaign.account_id,
                    AccountEquity.venue == campaign.venue,
                    AccountEquity.environment == campaign.environment,
                    AccountEquity.currency == instrument.collateral_currency,
                )
            )
            max_age = timedelta(seconds=policy.max_fact_age_seconds)
            if (
                position is None
                or position.fact_status != FactStatus.KNOWN.value
                or self._fact_is_stale(position.observed_at, now, max_age)
            ):
                _reject("POSITION_UNKNOWN", "new-risk venue send requires a fresh position")
            if (
                equity is None
                or equity.fact_status != FactStatus.KNOWN.value
                or self._fact_is_stale(equity.observed_at, now, max_age)
            ):
                _reject("EQUITY_UNKNOWN", "new-risk venue send requires fresh equity")
            if environment is ExecutionEnvironment.LIVE:
                source_health = session.scalar(
                    select(RuntimeSourceHealth).where(
                        RuntimeSourceHealth.team_id == campaign.team_id,
                        RuntimeSourceHealth.source_name == campaign.venue,
                        RuntimeSourceHealth.account_id == campaign.account_id,
                        RuntimeSourceHealth.venue == campaign.venue,
                    )
                )
                if (
                    source_health is None
                    or source_health.status != "SUCCESS"
                    or self._fact_is_stale(source_health.checked_at, now, max_age)
                ):
                    _reject(
                        "READ_ONLY_SOURCE_UNAVAILABLE",
                        "new-risk venue send requires a current successful read-only probe",
                    )
            capital_known, managed_capital_usd, _, _ = self._managed_capital_context(
                session,
                team_id=campaign.team_id,
                environment=campaign.environment,
                now=now,
                max_age=max_age,
            )
            if not capital_known:
                _reject(
                    "MANAGED_CAPITAL_UNKNOWN",
                    "new-risk venue send requires fresh total managed capital",
                )
            occupied_risk = self._occupied_risk(session, campaign.team_id)
            occupied_account_risk = self._occupied_risk(
                session,
                campaign.team_id,
                account_id=campaign.account_id,
                venue=campaign.venue,
            )
            assert policy.max_account_risk is not None
            if (
                occupied_risk > min(policy.max_total_risk, managed_capital_usd)
                or occupied_account_risk > policy.max_account_risk
            ):
                _reject(
                    "RISK_CAPACITY_EXHAUSTED",
                    "current reservations exceed total managed capital risk capacity",
                )
            if intent.kind == IntentKind.INITIAL.value and position.quantity != 0:
                _reject("POSITION_NOT_FLAT", "INITIAL venue send requires a flat position")
            if intent.kind == IntentKind.ADD.value:
                protection = session.scalar(
                    select(ProtectionOrder).where(
                        ProtectionOrder.position_id == position.position_id
                    )
                )
                if (
                    protection is None
                    or protection.status != ProtectionStatus.ACTIVE.value
                    or not protection.fully_covered
                    or protection.quantity < abs(position.quantity)
                    or self._fact_is_stale(protection.observed_at, now, max_age)
                ):
                    _reject("PROTECTION_UNKNOWN", "Add venue send requires current protection")
            if (
                session.scalar(
                    select(RiskReservation.reservation_id).where(
                        RiskReservation.team_id == campaign.team_id,
                        RiskReservation.status == ReservationStatus.UNKNOWN.value,
                    )
                )
                is not None
            ):
                _reject("RISK_RESERVATION_UNKNOWN", "unresolved risk blocks new venue sends")
            reference_price = intent.limit_price if require_limit_price else position.mark_price
            assert reference_price is not None
            notional = intent.quantity * reference_price * instrument.contract_multiplier
            if notional < instrument.minimum_notional:
                _reject("MINIMUM_NOTIONAL", "intent is below current instrument minimum notional")
        return BinanceTestnetOrderCommand(
            symbol=instrument.symbol,
            side=intent.side,
            quantity=intent.quantity,
            reduce_only=intent.reduce_only,
            client_order_id=(
                self._hyperliquid_client_order_id(intent.intent_id)
                if venue == "HYPERLIQUID"
                else self._binance_client_order_id(intent.intent_id)
            ),
        )

    def prepare_binance_testnet_send(
        self,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        *,
        now: datetime,
    ) -> BinanceTestnetOrderCommand:
        with self.database.session_factory() as session:
            return self._binance_testnet_command(
                session,
                intent_id,
                actor_id,
                execution_scope,
                owner_id,
                fencing_token,
                now,
                {
                    OrderIntentStatus.READY.value,
                    OrderIntentStatus.SENT.value,
                    OrderIntentStatus.PARTIALLY_FILLED.value,
                    OrderIntentStatus.FILLED.value,
                },
            )

    def prepare_binance_testnet_cancel(
        self,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        *,
        now: datetime,
    ) -> BinanceTestnetOrderCommand:
        with self.database.session_factory() as session:
            return self._binance_testnet_command(
                session,
                intent_id,
                actor_id,
                execution_scope,
                owner_id,
                fencing_token,
                now,
                {
                    OrderIntentStatus.SENT.value,
                    OrderIntentStatus.PARTIALLY_FILLED.value,
                },
            )

    def prepare_binance_testnet_recovery(
        self,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        *,
        now: datetime,
    ) -> BinanceTestnetOrderCommand:
        with self.database.session_factory() as session:
            return self._binance_testnet_command(
                session,
                intent_id,
                actor_id,
                execution_scope,
                owner_id,
                fencing_token,
                now,
                {OrderIntentStatus.UNKNOWN.value},
            )

    def prepare_binance_live_send(
        self,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        *,
        now: datetime,
    ) -> BinanceTestnetOrderCommand:
        with self.database.session_factory() as session:
            return self._binance_testnet_command(
                session,
                intent_id,
                actor_id,
                execution_scope,
                owner_id,
                fencing_token,
                now,
                {
                    OrderIntentStatus.READY.value,
                    OrderIntentStatus.SENT.value,
                    OrderIntentStatus.PARTIALLY_FILLED.value,
                    OrderIntentStatus.FILLED.value,
                },
                environment=ExecutionEnvironment.LIVE,
            )

    def prepare_freqtrade_live_order(
        self,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        *,
        hip3_dexes: tuple[str, ...] = (),
        leverage: Decimal = DEFAULT_FREQTRADE_LEVERAGE,
        now: datetime,
    ) -> FreqtradeEntryCommand | FreqtradeExitCommand:
        if leverage <= 0:
            _reject("FREQTRADE_LEVERAGE_INVALID", "Freqtrade leverage must be positive")
        environment, _account_id, venue = _scope_parts(execution_scope)
        if environment is not ExecutionEnvironment.LIVE:
            _reject("FREQTRADE_LIVE_SCOPE_REQUIRED", "Freqtrade LIVE requires a LIVE scope")
        with self.database.session_factory() as session:
            base = self._binance_testnet_command(
                session,
                intent_id,
                actor_id,
                execution_scope,
                owner_id,
                fencing_token,
                now,
                {
                    OrderIntentStatus.READY.value,
                    OrderIntentStatus.DISPATCHING.value,
                    OrderIntentStatus.SENT.value,
                    OrderIntentStatus.PARTIALLY_FILLED.value,
                    OrderIntentStatus.FILLED.value,
                    OrderIntentStatus.UNKNOWN.value,
                },
                venue=venue,
                environment=ExecutionEnvironment.LIVE,
            )
            intent = session.get(OrderIntent, intent_id)
            campaign = None if intent is None else session.get(Campaign, intent.campaign_id)
            instrument = (
                None if campaign is None else session.get(Instrument, campaign.instrument_id)
            )
            if intent is None or campaign is None or instrument is None:
                _reject("ORDER_INTENT_NOT_FOUND", "Freqtrade intent scope is incomplete")
            pair = freqtrade_pair(venue, instrument.symbol, hip3_dexes=hip3_dexes)
            if intent.reduce_only:
                return FreqtradeExitCommand(
                    pair=pair,
                    max_quantity=base.quantity,
                    client_order_id=base.client_order_id,
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
            if position is None or position.mark_price <= 0:
                _reject(
                    "POSITION_UNKNOWN",
                    "Freqtrade entry requires a positive current mark price",
                )
            requested_notional = (
                intent.quantity * position.mark_price * instrument.contract_multiplier
            )
            stake_amount = requested_notional * Decimal("0.98") / leverage
            if stake_amount <= 0:
                _reject("FREQTRADE_ORDER_INVALID", "Freqtrade stake amount is invalid")
            return FreqtradeEntryCommand(
                pair=pair,
                side="long" if base.side == "BUY" else "short",
                stake_amount=stake_amount,
                max_quantity=base.quantity,
                leverage=leverage,
                enter_tag=f"tcp-{intent.intent_id.hex[:24]}",
                client_order_id=base.client_order_id,
            )

    def prepare_binance_live_cancel(
        self,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        *,
        now: datetime,
    ) -> BinanceTestnetOrderCommand:
        with self.database.session_factory() as session:
            return self._binance_testnet_command(
                session,
                intent_id,
                actor_id,
                execution_scope,
                owner_id,
                fencing_token,
                now,
                {
                    OrderIntentStatus.SENT.value,
                    OrderIntentStatus.PARTIALLY_FILLED.value,
                },
                environment=ExecutionEnvironment.LIVE,
            )

    def prepare_binance_live_recovery(
        self,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        *,
        now: datetime,
    ) -> BinanceTestnetOrderCommand:
        with self.database.session_factory() as session:
            return self._binance_testnet_command(
                session,
                intent_id,
                actor_id,
                execution_scope,
                owner_id,
                fencing_token,
                now,
                {OrderIntentStatus.UNKNOWN.value},
                environment=ExecutionEnvironment.LIVE,
            )

    def _hyperliquid_testnet_command(
        self,
        session: Session,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        now: datetime,
        allowed_statuses: set[str],
        *,
        environment: ExecutionEnvironment = ExecutionEnvironment.TESTNET,
    ) -> HyperliquidTestnetOrderCommand:
        base = self._binance_testnet_command(
            session,
            intent_id,
            actor_id,
            execution_scope,
            owner_id,
            fencing_token,
            now,
            allowed_statuses,
            venue="HYPERLIQUID",
            require_limit_price=True,
            environment=environment,
        )
        intent = session.get(OrderIntent, intent_id)
        if intent is None or intent.limit_price is None:
            _reject(
                "HYPERLIQUID_LIMIT_PRICE_REQUIRED",
                "Hyperliquid IOC execution requires an explicit frozen limit price",
            )
        campaign = session.get(Campaign, intent.campaign_id)
        instrument = None if campaign is None else session.get(Instrument, campaign.instrument_id)
        if instrument is None:
            _reject("INSTRUMENT_UNAVAILABLE", "campaign instrument is unavailable")
        notional = intent.quantity * intent.limit_price * instrument.contract_multiplier
        if notional < instrument.minimum_notional:
            _reject("MINIMUM_NOTIONAL", "intent is below current instrument minimum notional")
        return HyperliquidTestnetOrderCommand(
            symbol=base.symbol,
            side=base.side,
            quantity=base.quantity,
            limit_price=intent.limit_price,
            reduce_only=base.reduce_only,
            client_order_id=base.client_order_id,
        )

    def prepare_hyperliquid_testnet_send(
        self,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        *,
        now: datetime,
    ) -> HyperliquidTestnetOrderCommand:
        with self.database.session_factory() as session:
            return self._hyperliquid_testnet_command(
                session,
                intent_id,
                actor_id,
                execution_scope,
                owner_id,
                fencing_token,
                now,
                {
                    OrderIntentStatus.READY.value,
                    OrderIntentStatus.SENT.value,
                    OrderIntentStatus.PARTIALLY_FILLED.value,
                    OrderIntentStatus.FILLED.value,
                },
            )

    def prepare_hyperliquid_testnet_cancel(
        self,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        *,
        now: datetime,
    ) -> HyperliquidTestnetOrderCommand:
        with self.database.session_factory() as session:
            return self._hyperliquid_testnet_command(
                session,
                intent_id,
                actor_id,
                execution_scope,
                owner_id,
                fencing_token,
                now,
                {OrderIntentStatus.SENT.value, OrderIntentStatus.PARTIALLY_FILLED.value},
            )

    def prepare_hyperliquid_testnet_recovery(
        self,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        *,
        now: datetime,
    ) -> HyperliquidTestnetOrderCommand:
        with self.database.session_factory() as session:
            return self._hyperliquid_testnet_command(
                session,
                intent_id,
                actor_id,
                execution_scope,
                owner_id,
                fencing_token,
                now,
                {OrderIntentStatus.UNKNOWN.value},
            )

    def prepare_hyperliquid_live_send(
        self,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        *,
        now: datetime,
    ) -> HyperliquidTestnetOrderCommand:
        with self.database.session_factory() as session:
            return self._hyperliquid_testnet_command(
                session,
                intent_id,
                actor_id,
                execution_scope,
                owner_id,
                fencing_token,
                now,
                {
                    OrderIntentStatus.READY.value,
                    OrderIntentStatus.SENT.value,
                    OrderIntentStatus.PARTIALLY_FILLED.value,
                    OrderIntentStatus.FILLED.value,
                },
                environment=ExecutionEnvironment.LIVE,
            )

    def prepare_hyperliquid_live_cancel(
        self,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        *,
        now: datetime,
    ) -> HyperliquidTestnetOrderCommand:
        with self.database.session_factory() as session:
            return self._hyperliquid_testnet_command(
                session,
                intent_id,
                actor_id,
                execution_scope,
                owner_id,
                fencing_token,
                now,
                {
                    OrderIntentStatus.SENT.value,
                    OrderIntentStatus.PARTIALLY_FILLED.value,
                },
                environment=ExecutionEnvironment.LIVE,
            )

    def prepare_hyperliquid_live_recovery(
        self,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        *,
        now: datetime,
    ) -> HyperliquidTestnetOrderCommand:
        with self.database.session_factory() as session:
            return self._hyperliquid_testnet_command(
                session,
                intent_id,
                actor_id,
                execution_scope,
                owner_id,
                fencing_token,
                now,
                {OrderIntentStatus.UNKNOWN.value},
                environment=ExecutionEnvironment.LIVE,
            )

    @staticmethod
    def _validate_binance_order_result(
        intent: OrderIntent,
        command: BinanceTestnetOrderCommand,
        result: BinanceTestnetOrder,
        *,
        expected_order_type: str = "MARKET",
        identity_code: str = "BINANCE_TESTNET_IDENTITY_CONFLICT",
        allow_bounded_quantity: bool = False,
    ) -> None:
        quantity_invalid = (
            result.ordered_quantity <= 0 or result.ordered_quantity > intent.quantity
            if allow_bounded_quantity
            else result.ordered_quantity != intent.quantity
        )
        if (
            result.client_order_id != command.client_order_id
            or result.side != intent.side
            or result.order_type != expected_order_type
            or quantity_invalid
            or result.reduce_only != intent.reduce_only
            or result.close_position
            or result.filled_quantity > intent.quantity
        ):
            _reject(
                identity_code,
                "testnet order result does not match the frozen order intent",
            )

    def _release_zero_fill_in_session(
        self,
        session: Session,
        intent: OrderIntent,
        terminal_status: OrderIntentStatus,
        confirmed_external: bool = False,
        *,
        now: datetime,
    ) -> None:
        reservation = (
            session.get(RiskReservation, intent.reservation_id, with_for_update=True)
            if intent.reservation_id is not None
            else None
        )
        if reservation is not None and reservation.status != ReservationStatus.RELEASED.value:
            if reservation.status == ReservationStatus.UNKNOWN.value and not confirmed_external:
                _reject("RISK_RESERVATION_UNKNOWN", "unknown risk cannot be released")
            reservation.status = ReservationStatus.RELEASED.value
            reservation.updated_at = now
            reservation.version += 1
            authorization = session.get(
                TradingAuthorization, intent.authorization_id, with_for_update=True
            )
            if authorization is None or authorization.used_quantity < intent.quantity:
                _reject("AUTHORIZATION_USAGE_INVALID", "authorization usage is inconsistent")
            authorization.used_quantity -= intent.quantity
        intent.status = terminal_status.value
        intent.updated_at = now
        intent.version += 1

    def record_binance_testnet_order(
        self,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        command: BinanceTestnetOrderCommand,
        result: BinanceTestnetOrder,
        *,
        venue: str = "BINANCE",
        expected_order_type: str = "MARKET",
        expected_limit_price: Decimal | None = None,
        allow_bounded_quantity: bool = False,
        environment: ExecutionEnvironment = ExecutionEnvironment.TESTNET,
        dispatch_external_id: str | None = None,
        now: datetime,
    ) -> UUID:
        with self.database.session_factory.begin() as session:
            intent = session.get(OrderIntent, intent_id, with_for_update=True)
            if intent is None:
                _reject("ORDER_INTENT_NOT_FOUND", "intent does not exist")
            campaign = session.get(Campaign, intent.campaign_id, with_for_update=True)
            if campaign is None:
                _reject("CAMPAIGN_NOT_FOUND", "intent campaign is missing")
            self._validate_sender(
                session,
                campaign.team_id,
                execution_scope,
                owner_id,
                fencing_token,
                now,
            )
            if (
                campaign.environment != environment.value
                or campaign.venue != venue
                or execution_scope
                != _scope_key(campaign.environment, campaign.account_id, campaign.venue)
            ):
                _reject(
                    "EXECUTION_SCOPE_MISMATCH",
                    f"result is outside the {environment.value} campaign scope",
                )
            self._require_role(
                session,
                actor_id,
                "venue.record",
                campaign.account_id,
                campaign.venue,
                team_id=campaign.team_id,
            )
            if expected_limit_price is not None and intent.limit_price != expected_limit_price:
                _reject(
                    f"{venue}_{environment.value}_IDENTITY_CONFLICT",
                    "result does not match the intent's frozen price boundary",
                )
            identity_code = f"{venue}_{environment.value}_IDENTITY_CONFLICT"
            self._validate_binance_order_result(
                intent,
                command,
                result,
                expected_order_type=expected_order_type,
                identity_code=identity_code,
                allow_bounded_quantity=allow_bounded_quantity,
            )
            if dispatch_external_id is not None:
                if intent.dispatch_backend != "FREQTRADE":
                    _reject(
                        "FREQTRADE_DISPATCH_SNAPSHOT_MISSING",
                        "Freqtrade result requires a durable dispatch snapshot",
                    )
                if (
                    intent.dispatch_external_id is not None
                    and intent.dispatch_external_id != dispatch_external_id
                ):
                    _reject(
                        "FREQTRADE_DISPATCH_SNAPSHOT_CHANGED",
                        "Freqtrade result changed the persisted external trade identity",
                    )
                intent.dispatch_external_id = dispatch_external_id
            fact = session.scalar(
                select(VenueOrder)
                .where(VenueOrder.order_intent_id == intent.intent_id)
                .with_for_update()
            )
            if fact is None:
                fact = VenueOrder(
                    team_id=campaign.team_id,
                    order_intent_id=intent.intent_id,
                    account_id=campaign.account_id,
                    venue=campaign.venue,
                    environment=campaign.environment,
                    instrument_id=campaign.instrument_id,
                    venue_order_id=result.order_id,
                    client_order_id=result.client_order_id,
                    side=result.side,
                    order_type=result.order_type,
                    reduce_only=result.reduce_only,
                    status=result.status,
                    ordered_quantity=result.ordered_quantity,
                    filled_quantity=result.filled_quantity,
                    observed_at=result.observed_at,
                    updated_at=now,
                )
                session.add(fact)
            elif (
                fact.client_order_id != result.client_order_id
                or fact.side != result.side
                or fact.order_type != result.order_type
                or fact.reduce_only != result.reduce_only
            ):
                _reject(
                    identity_code,
                    "persisted client order identity changed semantics",
                )
            else:
                fact.venue_order_id = result.order_id
                fact.status = result.status
                fact.ordered_quantity = result.ordered_quantity
                fact.filled_quantity = result.filled_quantity
                fact.observed_at = result.observed_at
                fact.updated_at = now

            if result.filled_quantity > 0:
                self._consume_add_unit(session, intent)
            previous = intent.status
            terminal = {
                VenueOrderStatus.CANCELLED.value: OrderIntentStatus.CANCELLED,
                VenueOrderStatus.REJECTED.value: OrderIntentStatus.REJECTED,
            }
            if result.status == VenueOrderStatus.UNKNOWN.value:
                intent.status = OrderIntentStatus.UNKNOWN.value
                if intent.reservation_id is not None:
                    reservation = session.get(
                        RiskReservation, intent.reservation_id, with_for_update=True
                    )
                    if reservation is not None:
                        reservation.status = ReservationStatus.UNKNOWN.value
                        reservation.updated_at = now
                        reservation.version += 1
                campaign.status = CampaignStatus.UNKNOWN.value
            elif result.status in terminal and result.filled_quantity == 0:
                self._release_zero_fill_in_session(
                    session,
                    intent,
                    terminal[result.status],
                    confirmed_external=True,
                    now=now,
                )
            else:
                if result.status == VenueOrderStatus.FILLED.value:
                    intent.status = OrderIntentStatus.FILLED.value
                elif result.status in terminal:
                    intent.status = terminal[result.status].value
                elif result.filled_quantity > 0:
                    intent.status = OrderIntentStatus.PARTIALLY_FILLED.value
                else:
                    intent.status = OrderIntentStatus.SENT.value
                intent.updated_at = now
                intent.version += 1
                if result.filled_quantity > 0 and intent.reservation_id is not None:
                    reservation = session.get(
                        RiskReservation, intent.reservation_id, with_for_update=True
                    )
                    if reservation is not None:
                        reservation.status = ReservationStatus.OPEN.value
                        reservation.updated_at = now
                        reservation.version += 1
                elif (
                    previous == OrderIntentStatus.UNKNOWN.value
                    and intent.reservation_id is not None
                ):
                    reservation = session.get(
                        RiskReservation, intent.reservation_id, with_for_update=True
                    )
                    if reservation is not None:
                        reservation.status = ReservationStatus.RESERVED.value
                        reservation.updated_at = now
                        reservation.version += 1
                if result.filled_quantity > 0:
                    if intent.kind in {IntentKind.INITIAL.value, IntentKind.ADD.value}:
                        campaign.status = CampaignStatus.OPEN.value
                    elif intent.kind == IntentKind.EXIT.value:
                        campaign.status = CampaignStatus.CLOSING.value
                    else:
                        campaign.status = CampaignStatus.REDUCING.value
                elif previous == OrderIntentStatus.UNKNOWN.value:
                    if intent.kind in {IntentKind.INITIAL.value, IntentKind.ADD.value}:
                        campaign.status = CampaignStatus.OPENING.value
                    elif intent.kind == IntentKind.EXIT.value:
                        campaign.status = CampaignStatus.CLOSING.value
                    else:
                        campaign.status = CampaignStatus.REDUCING.value
            campaign.updated_at = now
            session.flush()
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type=f"{venue}_{environment.value}_ORDER_OBSERVED",
                object_type="VenueOrder",
                object_id=fact.venue_order_fact_id,
                reason=f"{result.client_order_id}:{result.status}",
                correlation_id=intent.correlation_id,
                object_version=intent.version,
                now=now,
            )
            if previous != intent.status:
                INTENT_TRANSITIONS.labels(previous, intent.status).inc()
            return fact.venue_order_fact_id

    def record_hyperliquid_testnet_order(
        self,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        command: HyperliquidTestnetOrderCommand,
        result: HyperliquidTestnetOrder,
        *,
        environment: ExecutionEnvironment = ExecutionEnvironment.TESTNET,
        now: datetime,
    ) -> UUID:
        if result.limit_price != command.limit_price or result.stop_price != 0:
            _reject(
                f"HYPERLIQUID_{environment.value}_IDENTITY_CONFLICT",
                "Hyperliquid result changed the explicit IOC price boundary",
            )
        return self.record_binance_testnet_order(
            intent_id,
            actor_id,
            execution_scope,
            owner_id,
            fencing_token,
            BinanceTestnetOrderCommand(
                symbol=command.symbol,
                side=command.side,
                quantity=command.quantity,
                reduce_only=command.reduce_only,
                client_order_id=command.client_order_id,
            ),
            BinanceTestnetOrder(
                order_id=result.order_id,
                client_order_id=result.client_order_id,
                status=result.status,
                side=result.side,
                order_type=result.order_type,
                ordered_quantity=result.ordered_quantity,
                filled_quantity=result.filled_quantity,
                stop_price=result.stop_price,
                reduce_only=result.reduce_only,
                close_position=result.close_position,
                observed_at=result.observed_at,
            ),
            venue="HYPERLIQUID",
            expected_order_type="IOC_LIMIT",
            expected_limit_price=command.limit_price,
            environment=environment,
            now=now,
        )

    def record_binance_testnet_unknown(
        self,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        command: BinanceTestnetOrderCommand,
        reason: str,
        *,
        venue: str = "BINANCE",
        order_type: str = "MARKET",
        environment: ExecutionEnvironment = ExecutionEnvironment.TESTNET,
        now: datetime,
    ) -> None:
        with self.database.session_factory.begin() as session:
            intent = session.get(OrderIntent, intent_id, with_for_update=True)
            if intent is None:
                _reject("ORDER_INTENT_NOT_FOUND", "intent does not exist")
            campaign = session.get(Campaign, intent.campaign_id, with_for_update=True)
            if campaign is None:
                _reject("CAMPAIGN_NOT_FOUND", "intent campaign is missing")
            self._validate_sender(
                session,
                campaign.team_id,
                execution_scope,
                owner_id,
                fencing_token,
                now,
            )
            if (
                campaign.environment != environment.value
                or campaign.venue != venue
                or execution_scope
                != _scope_key(campaign.environment, campaign.account_id, campaign.venue)
            ):
                _reject("EXECUTION_SCOPE_MISMATCH", "sender scope does not match campaign scope")
            self._require_role(
                session,
                actor_id,
                "venue.record",
                campaign.account_id,
                campaign.venue,
                team_id=campaign.team_id,
            )
            if (
                intent.status == OrderIntentStatus.UNKNOWN.value
                and intent.dispatch_backend == "FREQTRADE"
            ):
                return
            fact = session.scalar(
                select(VenueOrder)
                .where(VenueOrder.order_intent_id == intent.intent_id)
                .with_for_update()
            )
            if fact is None:
                fact = VenueOrder(
                    team_id=campaign.team_id,
                    order_intent_id=intent.intent_id,
                    account_id=campaign.account_id,
                    venue=campaign.venue,
                    environment=campaign.environment,
                    instrument_id=campaign.instrument_id,
                    venue_order_id=f"UNKNOWN:{command.client_order_id}",
                    client_order_id=command.client_order_id,
                    side=command.side,
                    order_type=order_type,
                    reduce_only=command.reduce_only,
                    status=VenueOrderStatus.UNKNOWN.value,
                    ordered_quantity=command.quantity,
                    filled_quantity=Decimal(0),
                    observed_at=now,
                    updated_at=now,
                )
                session.add(fact)
            else:
                fact.status = VenueOrderStatus.UNKNOWN.value
                fact.observed_at = now
                fact.updated_at = now
            previous = intent.status
            intent.status = OrderIntentStatus.UNKNOWN.value
            intent.updated_at = now
            intent.version += 1
            if intent.reservation_id is not None:
                reservation = session.get(
                    RiskReservation, intent.reservation_id, with_for_update=True
                )
                if reservation is not None:
                    reservation.status = ReservationStatus.UNKNOWN.value
                    reservation.updated_at = now
                    reservation.version += 1
            campaign.status = CampaignStatus.UNKNOWN.value
            campaign.updated_at = now
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type=f"{venue}_{environment.value}_OUTCOME_UNKNOWN",
                object_type="OrderIntent",
                object_id=intent.intent_id,
                reason=reason,
                correlation_id=intent.correlation_id,
                object_version=intent.version,
                now=now,
            )
            if previous != intent.status:
                INTENT_TRANSITIONS.labels(previous, intent.status).inc()

    def record_hyperliquid_testnet_unknown(
        self,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        command: HyperliquidTestnetOrderCommand,
        reason: str,
        *,
        environment: ExecutionEnvironment = ExecutionEnvironment.TESTNET,
        now: datetime,
    ) -> None:
        self.record_binance_testnet_unknown(
            intent_id,
            actor_id,
            execution_scope,
            owner_id,
            fencing_token,
            BinanceTestnetOrderCommand(
                symbol=command.symbol,
                side=command.side,
                quantity=command.quantity,
                reduce_only=command.reduce_only,
                client_order_id=command.client_order_id,
            ),
            reason,
            venue="HYPERLIQUID",
            order_type="IOC_LIMIT",
            environment=environment,
            now=now,
        )

    def prepare_binance_testnet_protection(
        self,
        campaign_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        trigger_price: Decimal,
        *,
        venue: str = "BINANCE",
        environment: ExecutionEnvironment = ExecutionEnvironment.TESTNET,
        now: datetime,
    ) -> BinanceTestnetProtectionCommand:
        with self.database.session_factory() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                _reject("CAMPAIGN_NOT_FOUND", "campaign does not exist")
            self._validate_sender(
                session,
                campaign.team_id,
                execution_scope,
                owner_id,
                fencing_token,
                now,
            )
            if (
                campaign.environment != environment.value
                or campaign.venue != venue
                or execution_scope
                != _scope_key(campaign.environment, campaign.account_id, campaign.venue)
            ):
                _reject(
                    "EXECUTION_SCOPE_MISMATCH",
                    f"protection is outside {environment.value} scope",
                )
            self._require_role(
                session,
                actor_id,
                "venue.record",
                campaign.account_id,
                campaign.venue,
                team_id=campaign.team_id,
            )
            if environment is ExecutionEnvironment.LIVE:
                live_gate = session.get(CapabilityGate, "LIVE_ORDER_SEND")
                if live_gate is None or live_gate.status != CapabilityStatus.ENABLED.value:
                    _reject(
                        "LIVE_ORDER_SEND_DISABLED",
                        "LIVE protection requires the explicit capability gate",
                    )
                self._require_exchange_account_live_ready(
                    session,
                    team_id=campaign.team_id,
                    account_id=campaign.account_id,
                    venue=campaign.venue,
                )
            if trigger_price <= 0:
                _reject("PROTECTION_TRIGGER_INVALID", "protection trigger must be positive")
            position = session.scalar(
                select(Position).where(
                    Position.team_id == campaign.team_id,
                    Position.account_id == campaign.account_id,
                    Position.venue == campaign.venue,
                    Position.environment == campaign.environment,
                    Position.instrument_id == campaign.instrument_id,
                )
            )
            policy = session.scalar(select(RiskPolicy).where(RiskPolicy.active))
            if (
                position is None
                or position.fact_status != FactStatus.KNOWN.value
                or position.quantity == 0
                or policy is None
                or self._fact_is_stale(
                    position.observed_at,
                    now,
                    timedelta(seconds=policy.max_fact_age_seconds),
                )
            ):
                _reject("POSITION_UNKNOWN", "native protection requires a fresh nonzero position")
            instrument = session.get(Instrument, campaign.instrument_id)
            if (
                instrument is None
                or instrument.venue != venue
                or not instrument.protection_supported
            ):
                _reject("PROTECTION_UNSUPPORTED", "instrument does not support native protection")
            if trigger_price % instrument.tick_size != 0:
                _reject("INSTRUMENT_PRICE_INVALID", "protection price exceeds instrument precision")
            side = "SELL" if position.quantity > 0 else "BUY"
            return BinanceTestnetProtectionCommand(
                symbol=instrument.symbol,
                side=side,
                trigger_price=trigger_price,
                client_order_id=(
                    self._hyperliquid_protection_client_order_id(position.position_id)
                    if venue == "HYPERLIQUID"
                    else self._binance_protection_client_order_id(position.position_id)
                ),
                quantity=abs(position.quantity),
            )

    def record_binance_testnet_protection(
        self,
        campaign_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        command: BinanceTestnetProtectionCommand,
        result: BinanceTestnetOrder,
        *,
        venue: str = "BINANCE",
        expected_order_type: str = "STOP_MARKET",
        expected_close_position: bool = True,
        require_reduce_only: bool = False,
        environment: ExecutionEnvironment = ExecutionEnvironment.TESTNET,
        now: datetime,
    ) -> UUID:
        with self.database.session_factory.begin() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                _reject("CAMPAIGN_NOT_FOUND", "campaign does not exist")
            self._validate_sender(
                session,
                campaign.team_id,
                execution_scope,
                owner_id,
                fencing_token,
                now,
            )
            if (
                campaign.environment != environment.value
                or campaign.venue != venue
                or execution_scope
                != _scope_key(campaign.environment, campaign.account_id, campaign.venue)
            ):
                _reject("EXECUTION_SCOPE_MISMATCH", "protection is outside campaign scope")
            self._require_role(
                session,
                actor_id,
                "venue.record",
                campaign.account_id,
                campaign.venue,
                team_id=campaign.team_id,
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
            if position is None or position.quantity == 0:
                _reject("POSITION_UNKNOWN", "protection result requires a nonzero position")
            if (
                result.client_order_id != command.client_order_id
                or result.side != command.side
                or result.order_type != expected_order_type
                or result.stop_price != command.trigger_price
                or result.close_position != expected_close_position
                or (require_reduce_only and not result.reduce_only)
            ):
                _reject(
                    f"{venue}_{environment.value}_IDENTITY_CONFLICT",
                    "venue protection result changed frozen semantics",
                )
            order = session.scalar(
                select(VenueOrder)
                .where(
                    VenueOrder.team_id == campaign.team_id,
                    VenueOrder.environment == campaign.environment,
                    VenueOrder.account_id == campaign.account_id,
                    VenueOrder.venue == campaign.venue,
                    VenueOrder.venue_order_id == result.order_id,
                )
                .with_for_update()
            )
            if order is None:
                order = session.scalar(
                    select(VenueOrder)
                    .where(
                        VenueOrder.team_id == campaign.team_id,
                        VenueOrder.environment == campaign.environment,
                        VenueOrder.account_id == campaign.account_id,
                        VenueOrder.venue == campaign.venue,
                        VenueOrder.client_order_id == command.client_order_id,
                    )
                    .with_for_update()
                )
            if order is None:
                order = VenueOrder(
                    team_id=campaign.team_id,
                    order_intent_id=None,
                    account_id=campaign.account_id,
                    venue=campaign.venue,
                    environment=campaign.environment,
                    instrument_id=campaign.instrument_id,
                    venue_order_id=result.order_id,
                    client_order_id=result.client_order_id,
                    side=result.side,
                    order_type=result.order_type,
                    reduce_only=True,
                    status=result.status,
                    ordered_quantity=result.ordered_quantity,
                    filled_quantity=result.filled_quantity,
                    observed_at=result.observed_at,
                    updated_at=now,
                )
                session.add(order)
            else:
                if (
                    order.instrument_id != campaign.instrument_id
                    or order.venue_order_id != result.order_id
                    or order.side != result.side
                    or order.order_type != result.order_type
                    or not order.reduce_only
                    or order.ordered_quantity != result.ordered_quantity
                ):
                    _reject(
                        f"{venue}_{environment.value}_IDENTITY_CONFLICT",
                        "existing venue protection changed protected order identity",
                    )
                order.status = result.status
                order.filled_quantity = result.filled_quantity
                order.observed_at = result.observed_at
                order.updated_at = now
            active = result.status in {
                VenueOrderStatus.SENT.value,
                VenueOrderStatus.PARTIALLY_FILLED.value,
            }
            protection = session.scalar(
                select(ProtectionOrder)
                .where(ProtectionOrder.position_id == position.position_id)
                .with_for_update()
            )
            if protection is None:
                protection = ProtectionOrder(
                    position_id=position.position_id,
                    venue_order_id=result.order_id,
                    quantity=abs(position.quantity) if active else Decimal(0),
                    trigger_price=result.stop_price,
                    status=(
                        ProtectionStatus.ACTIVE.value
                        if active
                        else ProtectionStatus.UNKNOWN.value
                        if result.status == VenueOrderStatus.UNKNOWN.value
                        else ProtectionStatus.DEGRADED.value
                    ),
                    fully_covered=active,
                    observed_at=result.observed_at,
                    updated_at=now,
                )
                session.add(protection)
            else:
                protection.venue_order_id = result.order_id
                protection.quantity = abs(position.quantity) if active else Decimal(0)
                protection.trigger_price = result.stop_price
                protection.status = (
                    ProtectionStatus.ACTIVE.value
                    if active
                    else ProtectionStatus.UNKNOWN.value
                    if result.status == VenueOrderStatus.UNKNOWN.value
                    else ProtectionStatus.DEGRADED.value
                )
                protection.fully_covered = active
                protection.observed_at = result.observed_at
                protection.updated_at = now
            session.flush()
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type=f"{venue}_{environment.value}_PROTECTION_OBSERVED",
                object_type="ProtectionOrder",
                object_id=protection.protection_id,
                reason=f"{result.client_order_id}:{result.status}",
                correlation_id=uuid4(),
                object_version=1,
                now=now,
            )
            return protection.protection_id

    def prepare_hyperliquid_testnet_protection(
        self,
        campaign_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        trigger_price: Decimal,
        limit_price: Decimal,
        *,
        environment: ExecutionEnvironment = ExecutionEnvironment.TESTNET,
        now: datetime,
    ) -> HyperliquidTestnetProtectionCommand:
        base = self.prepare_binance_testnet_protection(
            campaign_id,
            actor_id,
            execution_scope,
            owner_id,
            fencing_token,
            trigger_price,
            venue="HYPERLIQUID",
            environment=environment,
            now=now,
        )
        if limit_price <= 0:
            _reject(
                "PROTECTION_TRIGGER_INVALID",
                "Hyperliquid protection requires a positive explicit limit price",
            )
        with self.database.session_factory() as session:
            campaign = session.get(Campaign, campaign_id)
            position = (
                None
                if campaign is None
                else session.scalar(
                    select(Position).where(
                        Position.team_id == campaign.team_id,
                        Position.account_id == campaign.account_id,
                        Position.venue == campaign.venue,
                        Position.environment == campaign.environment,
                        Position.instrument_id == campaign.instrument_id,
                    )
                )
            )
            instrument = (
                None if campaign is None else session.get(Instrument, campaign.instrument_id)
            )
            if position is None or instrument is None:
                _reject("POSITION_UNKNOWN", "native protection position is unavailable")
            if limit_price % instrument.tick_size != 0:
                _reject("INSTRUMENT_PRICE_INVALID", "protection price exceeds instrument precision")
            return HyperliquidTestnetProtectionCommand(
                symbol=base.symbol,
                side=base.side,
                quantity=abs(position.quantity),
                trigger_price=base.trigger_price,
                limit_price=limit_price,
                client_order_id=base.client_order_id,
            )

    def record_hyperliquid_testnet_protection(
        self,
        campaign_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        command: HyperliquidTestnetProtectionCommand,
        result: HyperliquidTestnetOrder,
        *,
        environment: ExecutionEnvironment = ExecutionEnvironment.TESTNET,
        now: datetime,
    ) -> UUID:
        if (
            result.ordered_quantity != command.quantity
            or result.limit_price != command.limit_price
            or result.reduce_only is not True
        ):
            _reject(
                f"HYPERLIQUID_{environment.value}_IDENTITY_CONFLICT",
                "Hyperliquid protection changed frozen quantity or price semantics",
            )
        return self.record_binance_testnet_protection(
            campaign_id,
            actor_id,
            execution_scope,
            owner_id,
            fencing_token,
            BinanceTestnetProtectionCommand(
                symbol=command.symbol,
                side=command.side,
                trigger_price=command.trigger_price,
                client_order_id=command.client_order_id,
                quantity=command.quantity,
            ),
            BinanceTestnetOrder(
                order_id=result.order_id,
                client_order_id=result.client_order_id,
                status=result.status,
                side=result.side,
                order_type=result.order_type,
                ordered_quantity=result.ordered_quantity,
                filled_quantity=result.filled_quantity,
                stop_price=result.stop_price,
                reduce_only=result.reduce_only,
                close_position=result.close_position,
                observed_at=result.observed_at,
            ),
            venue="HYPERLIQUID",
            expected_order_type="TRIGGER_MARKET",
            expected_close_position=False,
            require_reduce_only=True,
            environment=environment,
            now=now,
        )

    def record_binance_live_order(
        self,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        command: BinanceTestnetOrderCommand,
        result: BinanceTestnetOrder,
        *,
        now: datetime,
    ) -> UUID:
        return self.record_binance_testnet_order(
            intent_id,
            actor_id,
            execution_scope,
            owner_id,
            fencing_token,
            command,
            result,
            environment=ExecutionEnvironment.LIVE,
            now=now,
        )

    def record_freqtrade_live_order(
        self,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        command: FreqtradeEntryCommand | FreqtradeExitCommand,
        trade: FreqtradeTrade,
        *,
        now: datetime,
    ) -> UUID:
        with self.database.session_factory() as session:
            intent = session.get(OrderIntent, intent_id)
            campaign = None if intent is None else session.get(Campaign, intent.campaign_id)
            if intent is None or campaign is None:
                _reject("ORDER_INTENT_NOT_FOUND", "Freqtrade intent campaign is unavailable")
            if trade.pair != command.pair or trade.amount > command.max_quantity:
                _reject(
                    "FREQTRADE_ORDER_IDENTITY_CONFLICT",
                    "Freqtrade trade exceeds the frozen pair or quantity boundary",
                )
            if isinstance(command, FreqtradeEntryCommand):
                if (
                    intent.reduce_only
                    or trade.enter_tag != command.enter_tag
                    or trade.side != command.side
                    or not trade.is_open
                ):
                    _reject(
                        "FREQTRADE_ORDER_IDENTITY_CONFLICT",
                        "Freqtrade entry changed the frozen direction or identity",
                    )
                phase = "entry"
                venue_order_id = trade.entry_order_id
            else:
                if not intent.reduce_only or trade.is_open:
                    _reject(
                        "FREQTRADE_ORDER_IDENTITY_CONFLICT",
                        "Freqtrade exit did not confirm a closed reduce-only trade",
                    )
                phase = "exit"
                venue_order_id = trade.exit_order_id
            if venue_order_id is None:
                _reject(
                    "FREQTRADE_EXECUTION_ORDER_ID_MISSING",
                    f"Freqtrade did not expose the native {phase} order identity",
                )
            venue = campaign.venue
            side = intent.side
            reduce_only = intent.reduce_only
        fact_id = self.record_binance_testnet_order(
            intent_id,
            actor_id,
            execution_scope,
            owner_id,
            fencing_token,
            BinanceTestnetOrderCommand(
                symbol=trade.pair,
                side=side,
                quantity=command.max_quantity,
                reduce_only=reduce_only,
                client_order_id=command.client_order_id,
            ),
            BinanceTestnetOrder(
                order_id=venue_order_id,
                client_order_id=command.client_order_id,
                status=VenueOrderStatus.FILLED.value,
                side=side,
                order_type="MARKET",
                ordered_quantity=trade.amount,
                filled_quantity=trade.amount,
                stop_price=Decimal(0),
                reduce_only=reduce_only,
                close_position=False,
                observed_at=trade.observed_at,
            ),
            venue=venue,
            allow_bounded_quantity=True,
            environment=ExecutionEnvironment.LIVE,
            dispatch_external_id=trade.trade_id,
            now=now,
        )
        if isinstance(command, FreqtradeEntryCommand):
            position_quantity = trade.amount if trade.side == "long" else -trade.amount
            average_entry_price = trade.open_rate
        else:
            position_quantity = Decimal(0)
            average_entry_price = Decimal(0)
        self.record_position(
            campaign.account_id,
            campaign.venue,
            campaign.instrument_id,
            position_quantity,
            average_entry_price,
            trade.close_rate or trade.current_rate,
            True,
            actor_id,
            environment=ExecutionEnvironment.LIVE,
            observed_at=trade.observed_at,
            now=now,
        )
        return fact_id

    def record_freqtrade_live_unknown(
        self,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        command: FreqtradeEntryCommand | FreqtradeExitCommand,
        reason: str,
        *,
        now: datetime,
    ) -> None:
        with self.database.session_factory() as session:
            intent = session.get(OrderIntent, intent_id)
            campaign = None if intent is None else session.get(Campaign, intent.campaign_id)
            if intent is None or campaign is None:
                _reject("ORDER_INTENT_NOT_FOUND", "Freqtrade intent campaign is unavailable")
            side = intent.side
            reduce_only = intent.reduce_only
            venue = campaign.venue
        self.record_binance_testnet_unknown(
            intent_id,
            actor_id,
            execution_scope,
            owner_id,
            fencing_token,
            BinanceTestnetOrderCommand(
                symbol=command.pair,
                side=side,
                quantity=command.max_quantity,
                reduce_only=reduce_only,
                client_order_id=command.client_order_id,
            ),
            reason,
            venue=venue,
            environment=ExecutionEnvironment.LIVE,
            now=now,
        )

    def record_freqtrade_live_protection(
        self,
        campaign_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        trade: FreqtradeTrade,
        *,
        now: datetime,
    ) -> UUID:
        if not trade.is_open or trade.stop_loss_abs is None or not trade.stoploss_order_id:
            _reject(
                "FREQTRADE_PROTECTION_UNCONFIRMED",
                "Freqtrade did not expose an active exchange stop-loss order",
            )
        environment, _account_id, venue = _scope_parts(execution_scope)
        if environment is not ExecutionEnvironment.LIVE:
            _reject("FREQTRADE_LIVE_SCOPE_REQUIRED", "Freqtrade protection requires LIVE")
        command = self.prepare_binance_testnet_protection(
            campaign_id,
            actor_id,
            execution_scope,
            owner_id,
            fencing_token,
            trade.stop_loss_abs,
            venue=venue,
            environment=ExecutionEnvironment.LIVE,
            now=now,
        )
        return self.record_binance_testnet_protection(
            campaign_id,
            actor_id,
            execution_scope,
            owner_id,
            fencing_token,
            command,
            BinanceTestnetOrder(
                order_id=trade.stoploss_order_id,
                client_order_id=command.client_order_id,
                status=VenueOrderStatus.SENT.value,
                side=command.side,
                order_type="STOP_MARKET",
                ordered_quantity=trade.amount,
                filled_quantity=Decimal(0),
                stop_price=trade.stop_loss_abs,
                reduce_only=True,
                close_position=False,
                observed_at=trade.observed_at,
            ),
            venue=venue,
            expected_close_position=False,
            require_reduce_only=True,
            environment=ExecutionEnvironment.LIVE,
            now=now,
        )

    def record_binance_live_unknown(
        self,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        command: BinanceTestnetOrderCommand,
        reason: str,
        *,
        now: datetime,
    ) -> None:
        self.record_binance_testnet_unknown(
            intent_id,
            actor_id,
            execution_scope,
            owner_id,
            fencing_token,
            command,
            reason,
            environment=ExecutionEnvironment.LIVE,
            now=now,
        )

    def record_hyperliquid_live_order(
        self,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        command: HyperliquidTestnetOrderCommand,
        result: HyperliquidTestnetOrder,
        *,
        now: datetime,
    ) -> UUID:
        return self.record_hyperliquid_testnet_order(
            intent_id,
            actor_id,
            execution_scope,
            owner_id,
            fencing_token,
            command,
            result,
            environment=ExecutionEnvironment.LIVE,
            now=now,
        )

    def record_hyperliquid_live_unknown(
        self,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        command: HyperliquidTestnetOrderCommand,
        reason: str,
        *,
        now: datetime,
    ) -> None:
        self.record_hyperliquid_testnet_unknown(
            intent_id,
            actor_id,
            execution_scope,
            owner_id,
            fencing_token,
            command,
            reason,
            environment=ExecutionEnvironment.LIVE,
            now=now,
        )

    def prepare_binance_live_protection(
        self,
        campaign_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        trigger_price: Decimal,
        *,
        now: datetime,
    ) -> BinanceTestnetProtectionCommand:
        return self.prepare_binance_testnet_protection(
            campaign_id,
            actor_id,
            execution_scope,
            owner_id,
            fencing_token,
            trigger_price,
            environment=ExecutionEnvironment.LIVE,
            now=now,
        )

    def record_binance_live_protection(
        self,
        campaign_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        command: BinanceTestnetProtectionCommand,
        result: BinanceTestnetOrder,
        *,
        now: datetime,
    ) -> UUID:
        return self.record_binance_testnet_protection(
            campaign_id,
            actor_id,
            execution_scope,
            owner_id,
            fencing_token,
            command,
            result,
            expected_close_position=False,
            require_reduce_only=True,
            environment=ExecutionEnvironment.LIVE,
            now=now,
        )

    def prepare_hyperliquid_live_protection(
        self,
        campaign_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        trigger_price: Decimal,
        limit_price: Decimal,
        *,
        now: datetime,
    ) -> HyperliquidTestnetProtectionCommand:
        return self.prepare_hyperliquid_testnet_protection(
            campaign_id,
            actor_id,
            execution_scope,
            owner_id,
            fencing_token,
            trigger_price,
            limit_price,
            environment=ExecutionEnvironment.LIVE,
            now=now,
        )

    def record_hyperliquid_live_protection(
        self,
        campaign_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        command: HyperliquidTestnetProtectionCommand,
        result: HyperliquidTestnetOrder,
        *,
        now: datetime,
    ) -> UUID:
        return self.record_hyperliquid_testnet_protection(
            campaign_id,
            actor_id,
            execution_scope,
            owner_id,
            fencing_token,
            command,
            result,
            environment=ExecutionEnvironment.LIVE,
            now=now,
        )

    def prepare_live_protection_cancel(
        self,
        campaign_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        *,
        venue: str,
        now: datetime,
    ) -> ProtectionCancelCommand:
        with self.database.session_factory() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                _reject("CAMPAIGN_NOT_FOUND", "campaign does not exist")
            self._validate_sender(
                session,
                campaign.team_id,
                execution_scope,
                owner_id,
                fencing_token,
                now,
            )
            if (
                campaign.environment != ExecutionEnvironment.LIVE.value
                or campaign.venue != venue
                or execution_scope
                != _scope_key(campaign.environment, campaign.account_id, campaign.venue)
            ):
                _reject("EXECUTION_SCOPE_MISMATCH", "protection cancel is outside LIVE scope")
            self._require_role(
                session,
                actor_id,
                "venue.record",
                campaign.account_id,
                campaign.venue,
                team_id=campaign.team_id,
            )
            live_gate = session.get(CapabilityGate, "LIVE_ORDER_SEND")
            if live_gate is None or live_gate.status != CapabilityStatus.ENABLED.value:
                _reject(
                    "LIVE_ORDER_SEND_DISABLED",
                    "LIVE protection cancel requires the explicit capability gate",
                )
            self._require_exchange_account_live_ready(
                session,
                team_id=campaign.team_id,
                account_id=campaign.account_id,
                venue=campaign.venue,
            )
            if campaign.current_target_quantity != 0:
                _reject(
                    "PROTECTION_CANCEL_UNSAFE",
                    "native protection can only be removed after the campaign target is zero",
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
            protection = (
                None
                if position is None
                else session.scalar(
                    select(ProtectionOrder).where(
                        ProtectionOrder.position_id == position.position_id
                    )
                )
            )
            order = (
                None
                if protection is None
                else session.scalar(
                    select(VenueOrder).where(
                        VenueOrder.team_id == campaign.team_id,
                        VenueOrder.environment == campaign.environment,
                        VenueOrder.account_id == campaign.account_id,
                        VenueOrder.venue == campaign.venue,
                        VenueOrder.venue_order_id == protection.venue_order_id,
                    )
                )
            )
            instrument = session.get(Instrument, campaign.instrument_id)
            if protection is None or order is None or instrument is None:
                _reject(
                    "PROTECTION_NOT_FOUND",
                    "campaign has no recorded native protection to cancel",
                )
            expected_type = "TRIGGER_MARKET" if venue == "HYPERLIQUID" else "STOP_MARKET"
            if (
                order.order_type != expected_type
                or not order.reduce_only
                or order.client_order_id == ""
            ):
                _reject(
                    f"{venue}_LIVE_IDENTITY_CONFLICT",
                    "recorded protection order identity is inconsistent",
                )
            return ProtectionCancelCommand(
                symbol=instrument.symbol,
                client_order_id=order.client_order_id,
            )

    def record_live_protection_cancel(
        self,
        campaign_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        command: ProtectionCancelCommand,
        result: BinanceTestnetOrder | HyperliquidTestnetOrder | None,
        *,
        venue: str,
        now: datetime,
    ) -> None:
        with self.database.session_factory.begin() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                _reject("CAMPAIGN_NOT_FOUND", "campaign does not exist")
            self._validate_sender(
                session,
                campaign.team_id,
                execution_scope,
                owner_id,
                fencing_token,
                now,
            )
            if (
                campaign.environment != ExecutionEnvironment.LIVE.value
                or campaign.venue != venue
                or execution_scope
                != _scope_key(campaign.environment, campaign.account_id, campaign.venue)
            ):
                _reject("EXECUTION_SCOPE_MISMATCH", "protection cancel is outside LIVE scope")
            self._require_role(
                session,
                actor_id,
                "venue.record",
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
            protection = (
                None
                if position is None
                else session.scalar(
                    select(ProtectionOrder)
                    .where(ProtectionOrder.position_id == position.position_id)
                    .with_for_update()
                )
            )
            order = session.scalar(
                select(VenueOrder)
                .where(
                    VenueOrder.team_id == campaign.team_id,
                    VenueOrder.environment == campaign.environment,
                    VenueOrder.account_id == campaign.account_id,
                    VenueOrder.venue == campaign.venue,
                    VenueOrder.client_order_id == command.client_order_id,
                )
                .with_for_update()
            )
            if protection is None or order is None:
                _reject("PROTECTION_NOT_FOUND", "recorded native protection disappeared")
            if result is not None and (
                result.client_order_id != command.client_order_id
                or result.status not in {"CANCELLED", "REJECTED", "FILLED"}
            ):
                _reject(
                    f"{venue}_LIVE_IDENTITY_CONFLICT",
                    "venue did not return a terminal protection cancellation result",
                )
            order.status = VenueOrderStatus.CANCELLED.value if result is None else result.status
            order.observed_at = now if result is None else result.observed_at
            order.updated_at = now
            protection.quantity = Decimal(0)
            protection.status = ProtectionStatus.DEGRADED.value
            protection.fully_covered = False
            protection.observed_at = order.observed_at
            protection.updated_at = now
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type=f"{venue}_LIVE_PROTECTION_CANCELLED",
                object_type="ProtectionOrder",
                object_id=protection.protection_id,
                reason="NOT_FOUND" if result is None else result.status,
                correlation_id=uuid4(),
                object_version=1,
                now=now,
            )

    def initialize_shadow_scope(
        self,
        *,
        actor_id: UUID,
        account_id: str,
        venue: str,
        instrument_id: UUID,
        currency: str,
        initial_equity: Decimal | None,
        idempotency_key: str,
        now: datetime,
    ) -> dict[str, Any]:
        operation = "shadow.scope.initialize"
        normalized_currency = currency.upper()
        payload = {
            "account_id": account_id,
            "venue": venue,
            "instrument_id": str(instrument_id),
            "currency": normalized_currency,
            "initial_equity": None if initial_equity is None else str(initial_equity),
        }
        if initial_equity is not None and (not initial_equity.is_finite() or initial_equity <= 0):
            _reject("SHADOW_EQUITY_INVALID", "initial virtual equity must be positive")
        with self.database.session_factory.begin() as session:
            team = self._require_role(session, actor_id, "account.manage", account_id, venue)
            self._require_team_environment(team, ExecutionEnvironment.SHADOW)
            digest, replay = self._idempotency(
                session,
                caller_id=f"{actor_id}:{team.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload={**payload, "team_id": str(team.team_id)},
            )
            if replay is not None:
                return replay
            account = session.scalar(
                select(ExchangeAccount)
                .where(
                    ExchangeAccount.team_id == team.team_id,
                    ExchangeAccount.account_id == account_id,
                    ExchangeAccount.venue == venue,
                    ExchangeAccount.active,
                )
                .with_for_update()
            )
            if account is None:
                _reject(
                    "EXCHANGE_ACCOUNT_NOT_FOUND",
                    "shadow scope requires an active account in the current team",
                )
            instrument = session.get(Instrument, instrument_id)
            if instrument is None or not instrument.active or instrument.venue != venue:
                _reject("INSTRUMENT_UNAVAILABLE", "shadow instrument is outside the account venue")
            if instrument.collateral_currency.upper() != normalized_currency:
                _reject(
                    "SHADOW_CURRENCY_MISMATCH",
                    "virtual capital must use the instrument collateral currency",
                )
            if normalized_currency not in USD_STABLE_ASSETS:
                _reject(
                    "SHADOW_CURRENCY_UNSUPPORTED",
                    "shadow capital currently requires an explicit USD stable collateral asset",
                )
            equity = session.scalar(
                select(AccountEquity)
                .where(
                    AccountEquity.team_id == team.team_id,
                    AccountEquity.account_id == account_id,
                    AccountEquity.venue == venue,
                    AccountEquity.environment == ExecutionEnvironment.SHADOW.value,
                    AccountEquity.currency == normalized_currency,
                )
                .with_for_update()
            )
            equity_created = equity is None
            if equity is None:
                if initial_equity is None:
                    _reject(
                        "SHADOW_EQUITY_REQUIRED",
                        "first scope for this account requires explicit virtual equity",
                    )
                equity = AccountEquity(
                    team_id=team.team_id,
                    account_id=account_id,
                    venue=venue,
                    environment=ExecutionEnvironment.SHADOW.value,
                    equity=initial_equity,
                    available_balance=initial_equity,
                    withdrawable_balance=Decimal(0),
                    currency=normalized_currency,
                    location_type="VENUE",
                    control_status="CONTROLLED",
                    deposit_status="READY",
                    network=None,
                    address_reference=None,
                    valuation_currency="USD",
                    valuation_price=Decimal(1),
                    valuation_equity=initial_equity,
                    valuation_observed_at=now,
                    fact_status=FactStatus.KNOWN.value,
                    observed_at=now,
                    updated_at=now,
                )
                session.add(equity)
                session.flush()
                self._record_account_equity_observation(session, equity, recorded_at=now)
            elif initial_equity is not None:
                _reject(
                    "SHADOW_EQUITY_ALREADY_INITIALIZED",
                    "existing virtual capital cannot be reset through scope initialization",
                )
            position = session.scalar(
                select(Position)
                .where(
                    Position.team_id == team.team_id,
                    Position.account_id == account_id,
                    Position.venue == venue,
                    Position.environment == ExecutionEnvironment.SHADOW.value,
                    Position.instrument_id == instrument_id,
                )
                .with_for_update()
            )
            position_created = position is None
            if position is None:
                position = Position(
                    team_id=team.team_id,
                    account_id=account_id,
                    venue=venue,
                    environment=ExecutionEnvironment.SHADOW.value,
                    instrument_id=instrument_id,
                    quantity=Decimal(0),
                    average_entry_price=Decimal(0),
                    mark_price=Decimal(0),
                    fact_status=FactStatus.KNOWN.value,
                    observed_at=now,
                    updated_at=now,
                )
                session.add(position)
                session.flush()
            result = {
                "workspace_id": str(team.workspace_id),
                "team_id": str(team.team_id),
                "environment": ExecutionEnvironment.SHADOW.value,
                "account_equity_id": str(equity.account_equity_id),
                "position_id": str(position.position_id),
                "equity_created": equity_created,
                "position_created": position_created,
            }
            self._save_receipt(
                session,
                caller_id=f"{actor_id}:{team.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="SHADOW_SCOPE_INITIALIZED",
                object_type="Position",
                object_id=position.position_id,
                reason=(
                    f"account={account_id};venue={venue};currency={normalized_currency};"
                    f"equity_created={str(equity_created).lower()}"
                ),
                correlation_id=uuid4(),
                object_version=1,
                idempotency_key=idempotency_key,
                workspace_id=team.workspace_id,
                team_id=team.team_id,
                account_id=account_id,
                now=now,
            )
            return result

    def simulate_shadow_execution(
        self,
        *,
        intent_id: UUID,
        actor_id: UUID,
        expected_version: int,
        reference_price: Decimal,
        fee_bps: Decimal,
        slippage_bps: Decimal,
        idempotency_key: str,
        now: datetime,
    ) -> dict[str, Any]:
        operation = "shadow.execution.simulate"
        with self.database.session_factory.begin() as session:
            intent = session.get(OrderIntent, intent_id, with_for_update=True)
            if intent is None:
                _reject("ORDER_INTENT_NOT_FOUND", "intent does not exist")
            campaign = session.get(Campaign, intent.campaign_id, with_for_update=True)
            if campaign is None:
                _reject("CAMPAIGN_NOT_FOUND", "intent campaign is missing")
            team = self._require_role(
                session,
                actor_id,
                "venue.record",
                campaign.account_id,
                campaign.venue,
                team_id=campaign.team_id,
            )
            self._require_team_environment(team, ExecutionEnvironment.SHADOW)
            if campaign.environment != ExecutionEnvironment.SHADOW.value:
                _reject(
                    "SHADOW_SCOPE_REQUIRED",
                    "the deterministic simulator accepts SHADOW intents only",
                )
            payload = {
                "intent_id": str(intent_id),
                "expected_version": expected_version,
                "reference_price": str(reference_price),
                "fee_bps": str(fee_bps),
                "slippage_bps": str(slippage_bps),
                "team_id": str(team.team_id),
            }
            digest, replay = self._idempotency(
                session,
                caller_id=f"{actor_id}:{team.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if replay is not None:
                return replay
            if intent.version != expected_version:
                _reject("VERSION_CONFLICT", "intent changed before shadow simulation")
            if intent.status != OrderIntentStatus.READY.value:
                _reject("ORDER_INTENT_NOT_READY", "one-shot simulation requires a ready intent")
            if session.scalar(
                select(VenueOrder.venue_order_fact_id).where(
                    VenueOrder.order_intent_id == intent_id
                )
            ):
                _reject("SHADOW_ORDER_EXISTS", "intent already has a venue-order fact")
            instrument = session.get(Instrument, campaign.instrument_id)
            proposal = session.get(Proposal, campaign.proposal_id)
            if instrument is None or proposal is None:
                _reject("SHADOW_SCOPE_INCOMPLETE", "instrument or frozen proposal is missing")
            position = session.scalar(
                select(Position)
                .where(
                    Position.team_id == campaign.team_id,
                    Position.account_id == campaign.account_id,
                    Position.venue == campaign.venue,
                    Position.environment == ExecutionEnvironment.SHADOW.value,
                    Position.instrument_id == campaign.instrument_id,
                )
                .with_for_update()
            )
            if position is None or position.fact_status != FactStatus.KNOWN.value:
                _reject("SHADOW_POSITION_REQUIRED", "initialize a known virtual position first")
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
                _reject("SHADOW_EQUITY_REQUIRED", "known controlled virtual equity is required")
            quote = quote_shadow_execution(
                side=intent.side,
                quantity=intent.quantity,
                reference_price=reference_price,
                tick_size=instrument.tick_size,
                contract_multiplier=instrument.contract_multiplier,
                fee_bps=fee_bps,
                slippage_bps=slippage_bps,
            )
            next_position = apply_shadow_fill(
                current_quantity=position.quantity,
                current_average_entry_price=position.average_entry_price,
                side=intent.side,
                fill_quantity=intent.quantity,
                fill_price=quote.fill_price,
                reduce_only=intent.reduce_only,
            )
            order_identity = f"shadow-order-{intent.intent_id}"
            fill_identity = f"shadow-fill-{intent.intent_id}-v{intent.version}"
            order = VenueOrder(
                team_id=campaign.team_id,
                order_intent_id=intent.intent_id,
                account_id=campaign.account_id,
                venue=campaign.venue,
                environment=ExecutionEnvironment.SHADOW.value,
                instrument_id=campaign.instrument_id,
                venue_order_id=order_identity,
                client_order_id=order_identity,
                side=intent.side,
                order_type="SIMULATED_MARKET",
                reduce_only=intent.reduce_only,
                status=VenueOrderStatus.FILLED.value,
                ordered_quantity=intent.quantity,
                filled_quantity=intent.quantity,
                observed_at=now,
                updated_at=now,
            )
            fill = VenueFill(
                team_id=campaign.team_id,
                venue=campaign.venue,
                venue_fill_id=fill_identity,
                order_intent_id=intent.intent_id,
                campaign_id=campaign.campaign_id,
                account_id=campaign.account_id,
                environment=ExecutionEnvironment.SHADOW.value,
                instrument_id=campaign.instrument_id,
                side=intent.side,
                quantity=intent.quantity,
                price=quote.fill_price,
                fee=quote.fee,
                fee_currency=instrument.collateral_currency,
                slippage_cost=quote.slippage_cost,
                executed_at=now,
            )
            session.add_all([order, fill])
            position.quantity = next_position.quantity
            position.average_entry_price = next_position.average_entry_price
            position.mark_price = quote.fill_price
            position.observed_at = now
            position.updated_at = now
            self._consume_add_unit(session, intent)
            previous_status = intent.status
            intent.status = OrderIntentStatus.FILLED.value
            intent.updated_at = now
            intent.version += 1
            if intent.reservation_id is not None:
                reservation = session.get(
                    RiskReservation,
                    intent.reservation_id,
                    with_for_update=True,
                )
                if reservation is not None:
                    reservation.status = ReservationStatus.OPEN.value
                    reservation.updated_at = now
                    reservation.version += 1
            campaign.status = (
                CampaignStatus.CLOSING.value
                if next_position.quantity == 0
                else CampaignStatus.OPEN.value
            )
            campaign.updated_at = now
            protection = session.scalar(
                select(ProtectionOrder)
                .where(ProtectionOrder.position_id == position.position_id)
                .with_for_update()
            )
            if next_position.quantity != 0:
                trigger_price = self._proposal_detail_decimal(proposal, "invalidation_price")
                if protection is None:
                    protection = ProtectionOrder(
                        position_id=position.position_id,
                        venue_order_id=f"shadow-protection-{campaign.campaign_id}",
                        quantity=abs(next_position.quantity),
                        trigger_price=trigger_price,
                        status=ProtectionStatus.ACTIVE.value,
                        fully_covered=True,
                        observed_at=now,
                        updated_at=now,
                    )
                    session.add(protection)
                else:
                    protection.quantity = abs(next_position.quantity)
                    protection.trigger_price = trigger_price
                    protection.status = ProtectionStatus.ACTIVE.value
                    protection.fully_covered = True
                    protection.observed_at = now
                    protection.updated_at = now
            elif protection is not None:
                protection.quantity = Decimal(0)
                protection.status = ProtectionStatus.DEGRADED.value
                protection.fully_covered = False
                protection.observed_at = now
                protection.updated_at = now
            previous_pnl = campaign.final_pnl
            pnl = self._update_campaign_pnl(session, campaign, position, now=now)
            self._apply_shadow_pnl_delta(
                session,
                campaign=campaign,
                equity=equity,
                previous_pnl=previous_pnl,
                actor_id=actor_id,
                correlation_id=intent.correlation_id,
                now=now,
            )
            session.flush()
            result = {
                "campaign_id": str(campaign.campaign_id),
                "intent_id": str(intent.intent_id),
                "intent_version": intent.version,
                "venue_order_fact_id": str(order.venue_order_fact_id),
                "venue_fill_fact_id": str(fill.venue_fill_fact_id),
                "position_id": str(position.position_id),
                "account_equity_id": str(equity.account_equity_id),
                "environment": ExecutionEnvironment.SHADOW.value,
                "fill_price": str(quote.fill_price),
                "fee": str(quote.fee),
                "slippage_cost": str(quote.slippage_cost),
                "position_quantity": str(position.quantity),
                "equity": str(equity.equity),
                "total_pnl": str(pnl.total_pnl),
            }
            self._save_receipt(
                session,
                caller_id=f"{actor_id}:{team.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="SHADOW_EXECUTION_SIMULATED",
                object_type="VenueFill",
                object_id=fill.venue_fill_fact_id,
                reason=(
                    f"reference={reference_price};fill={quote.fill_price};"
                    f"fee_bps={fee_bps};slippage_bps={slippage_bps}"
                ),
                correlation_id=intent.correlation_id,
                object_version=intent.version,
                idempotency_key=idempotency_key,
                workspace_id=team.workspace_id,
                team_id=team.team_id,
                account_id=campaign.account_id,
                now=now,
            )
            INTENT_TRANSITIONS.labels(previous_status, intent.status).inc()
            return result

    def record_shadow_order(
        self,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        venue_order_id: str,
        *,
        now: datetime,
    ) -> UUID:
        """Record a synthetic SHADOW send; this method never connects to a venue."""

        with self.database.session_factory.begin() as session:
            intent = session.get(OrderIntent, intent_id, with_for_update=True)
            if intent is None or intent.status != OrderIntentStatus.READY.value:
                _reject("ORDER_INTENT_NOT_READY", "only a ready intent can be shadow-sent")
            campaign = session.get(Campaign, intent.campaign_id)
            if campaign is None:
                _reject("CAMPAIGN_NOT_FOUND", "intent campaign is missing")
            self._validate_sender(
                session,
                campaign.team_id,
                execution_scope,
                owner_id,
                fencing_token,
                now,
            )
            if campaign.environment != ExecutionEnvironment.SHADOW.value:
                _reject("SHADOW_SCOPE_REQUIRED", "synthetic order recording is SHADOW-only")
            expected_scope = _scope_key(campaign.environment, campaign.account_id, campaign.venue)
            if execution_scope != expected_scope:
                _reject("EXECUTION_SCOPE_MISMATCH", "sender scope does not match campaign scope")
            team = self._require_role(
                session,
                actor_id,
                "venue.record",
                campaign.account_id,
                campaign.venue,
                team_id=campaign.team_id,
            )
            self._require_team_environment(team, ExecutionEnvironment.SHADOW)
            fact = VenueOrder(
                team_id=campaign.team_id,
                order_intent_id=intent_id,
                account_id=campaign.account_id,
                venue=campaign.venue,
                environment=campaign.environment,
                instrument_id=campaign.instrument_id,
                venue_order_id=venue_order_id,
                client_order_id=venue_order_id,
                side=intent.side,
                order_type="MARKET",
                reduce_only=intent.reduce_only,
                status=VenueOrderStatus.SENT.value,
                ordered_quantity=intent.quantity,
                filled_quantity=Decimal(0),
                observed_at=now,
                updated_at=now,
            )
            session.add(fact)
            previous = intent.status
            intent.status = OrderIntentStatus.SENT.value
            intent.updated_at = now
            intent.version += 1
            session.flush()
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="SHADOW_ORDER_RECORDED",
                object_type="VenueOrder",
                object_id=fact.venue_order_fact_id,
                reason=venue_order_id,
                correlation_id=intent.correlation_id,
                object_version=1,
                now=now,
            )
            INTENT_TRANSITIONS.labels(previous, intent.status).inc()
            return fact.venue_order_fact_id

    def record_fill(
        self,
        intent_id: UUID,
        actor_id: UUID,
        venue_fill_id: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
        fee: Decimal,
        fee_currency: str,
        slippage_cost: Decimal,
        *,
        now: datetime,
    ) -> UUID:
        with self.database.session_factory.begin() as session:
            intent = session.get(OrderIntent, intent_id, with_for_update=True)
            if intent is None:
                _reject("ORDER_INTENT_NOT_FOUND", "intent does not exist")
            campaign = session.get(Campaign, intent.campaign_id)
            if campaign is None:
                _reject("CAMPAIGN_NOT_FOUND", "intent campaign is missing")
            if campaign.environment != ExecutionEnvironment.SHADOW.value:
                _reject("SHADOW_SCOPE_REQUIRED", "synthetic fill recording is SHADOW-only")
            team = self._require_role(
                session,
                actor_id,
                "venue.record",
                campaign.account_id,
                campaign.venue,
                team_id=campaign.team_id,
            )
            self._require_team_environment(team, ExecutionEnvironment.SHADOW)
            existing = session.scalar(
                select(VenueFill).where(
                    VenueFill.team_id == campaign.team_id,
                    VenueFill.environment == campaign.environment,
                    VenueFill.account_id == campaign.account_id,
                    VenueFill.venue == campaign.venue,
                    VenueFill.venue_fill_id == venue_fill_id,
                )
            )
            if existing is not None:
                if (
                    existing.order_intent_id == intent_id
                    and existing.campaign_id == campaign.campaign_id
                    and existing.side == side
                    and existing.quantity == quantity
                    and existing.price == price
                    and existing.fee == fee
                    and existing.fee_currency == fee_currency
                    and existing.slippage_cost == slippage_cost
                ):
                    return existing.venue_fill_fact_id
                raise IdempotencyConflict
            if intent.status not in {
                OrderIntentStatus.SENT.value,
                OrderIntentStatus.PARTIALLY_FILLED.value,
            }:
                _reject("ORDER_INTENT_NOT_FILLABLE", "fill requires a sent active intent")
            if side != intent.side:
                _reject("FILL_SIDE_MISMATCH", "fill side must match the order intent")
            if quantity <= 0 or price <= 0 or fee < 0 or slippage_cost < 0:
                _reject("FILL_INVALID", "fill amounts and price are invalid")
            instrument = session.get(Instrument, campaign.instrument_id)
            if instrument is None or fee_currency != instrument.collateral_currency:
                _reject("PNL_CURRENCY_MISMATCH", "fill fee currency lacks an FX conversion")
            venue_order = session.scalar(
                select(VenueOrder).where(VenueOrder.order_intent_id == intent_id)
            )
            if venue_order is None or venue_order.venue != campaign.venue:
                _reject("VENUE_ORDER_MISSING", "fill must reference a known venue order")
            current_filled = session.execute(
                select(func.coalesce(func.sum(VenueFill.quantity), 0)).where(
                    VenueFill.order_intent_id == intent_id
                )
            ).scalar_one()
            if current_filled + quantity > intent.quantity:
                _reject("ORDER_INTENT_OVERFILLED", "cumulative fill exceeds intent quantity")
            fact = VenueFill(
                team_id=campaign.team_id,
                venue=campaign.venue,
                venue_fill_id=venue_fill_id,
                order_intent_id=intent_id,
                campaign_id=campaign.campaign_id,
                account_id=campaign.account_id,
                environment=campaign.environment,
                instrument_id=campaign.instrument_id,
                side=side,
                quantity=quantity,
                price=price,
                fee=fee,
                fee_currency=fee_currency,
                slippage_cost=slippage_cost,
                executed_at=now,
            )
            session.add(fact)
            session.flush()
            total_filled = current_filled + quantity
            self._consume_add_unit(session, intent)
            previous = intent.status
            if total_filled == intent.quantity:
                intent.status = OrderIntentStatus.FILLED.value
                venue_order.status = VenueOrderStatus.FILLED.value
            else:
                intent.status = OrderIntentStatus.PARTIALLY_FILLED.value
                venue_order.status = VenueOrderStatus.PARTIALLY_FILLED.value
            venue_order.filled_quantity = total_filled
            venue_order.observed_at = now
            venue_order.updated_at = now
            intent.updated_at = now
            intent.version += 1
            if intent.reservation_id is not None:
                reservation = session.get(RiskReservation, intent.reservation_id)
                if reservation is not None:
                    reservation.status = ReservationStatus.OPEN.value
                    reservation.updated_at = now
                    reservation.version += 1
            if intent.kind in {IntentKind.INITIAL.value, IntentKind.ADD.value}:
                campaign.status = CampaignStatus.OPEN.value
            campaign.updated_at = now
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="VENUE_FILL_RECORDED",
                object_type="VenueFill",
                object_id=fact.venue_fill_fact_id,
                reason=venue_fill_id,
                correlation_id=intent.correlation_id,
                object_version=1,
                now=now,
            )
            INTENT_TRANSITIONS.labels(previous, intent.status).inc()
            return fact.venue_fill_fact_id

    def record_position(
        self,
        account_id: str,
        venue: str,
        instrument_id: UUID,
        quantity: Decimal,
        average_entry_price: Decimal,
        mark_price: Decimal,
        known: bool,
        actor_id: UUID,
        *,
        environment: ExecutionEnvironment = ExecutionEnvironment.SHADOW,
        observed_at: datetime | None = None,
        now: datetime,
    ) -> UUID:
        fact_time = now if observed_at is None else observed_at
        if fact_time > now:
            _reject("FACT_TIME_INVALID", "position observation cannot be in the future")
        with self.database.session_factory.begin() as session:
            team = self._require_role(session, actor_id, "venue.record", account_id, venue)
            if (
                environment is ExecutionEnvironment.SHADOW
                and team.execution_mode == TeamExecutionMode.SHADOW.value
            ):
                _reject(
                    "SHADOW_FACTS_SIMULATOR_MANAGED",
                    "active team SHADOW positions are managed by the simulator",
                )
            self._ensure_exchange_account_reference(
                session,
                team=team,
                actor_id=actor_id,
                account_id=account_id,
                venue=venue,
                now=now,
            )
            position = session.scalar(
                select(Position).where(
                    Position.team_id == team.team_id,
                    Position.account_id == account_id,
                    Position.venue == venue,
                    Position.environment == environment.value,
                    Position.instrument_id == instrument_id,
                )
            )
            if position is None:
                position = Position(
                    team_id=team.team_id,
                    account_id=account_id,
                    venue=venue,
                    environment=environment.value,
                    instrument_id=instrument_id,
                    quantity=quantity,
                    average_entry_price=average_entry_price,
                    mark_price=mark_price,
                    fact_status=FactStatus.KNOWN.value if known else FactStatus.UNKNOWN.value,
                    observed_at=fact_time,
                    updated_at=now,
                )
                session.add(position)
                session.flush()
            else:
                position.quantity = quantity
                position.average_entry_price = average_entry_price
                position.mark_price = mark_price
                position.fact_status = FactStatus.KNOWN.value if known else FactStatus.UNKNOWN.value
                position.observed_at = fact_time
                position.updated_at = now
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="POSITION_RECORDED",
                object_type="Position",
                object_id=position.position_id,
                reason=position.fact_status,
                correlation_id=uuid4(),
                object_version=1,
                now=now,
            )
            return position.position_id

    def record_protection(
        self,
        position_id: UUID,
        venue_order_id: str,
        quantity: Decimal,
        trigger_price: Decimal,
        fully_covered: bool,
        actor_id: UUID,
        *,
        campaign_id: UUID | None = None,
        required_environment: ExecutionEnvironment | None = None,
        known: bool = True,
        observed_at: datetime | None = None,
        now: datetime,
    ) -> UUID:
        fact_time = now if observed_at is None else observed_at
        if fact_time > now:
            _reject("FACT_TIME_INVALID", "protection observation cannot be in the future")
        with self.database.session_factory.begin() as session:
            position = session.get(Position, position_id)
            if position is None:
                _reject("POSITION_NOT_FOUND", "protection position is missing")
            if (
                required_environment is not None
                and position.environment != required_environment.value
            ):
                _reject(
                    "EXECUTION_ENVIRONMENT_MISMATCH",
                    "position is outside the requested execution environment",
                )
            if campaign_id is not None:
                campaign = session.get(Campaign, campaign_id)
                if campaign is None:
                    _reject("CAMPAIGN_NOT_FOUND", "protection campaign is missing")
                if (
                    campaign.team_id != position.team_id
                    or campaign.account_id != position.account_id
                    or campaign.venue != position.venue
                    or campaign.environment != position.environment
                    or campaign.instrument_id != position.instrument_id
                ):
                    _reject(
                        "CAMPAIGN_POSITION_SCOPE_MISMATCH",
                        "protection position is outside the campaign scope",
                    )
            self._require_role(
                session,
                actor_id,
                "venue.record",
                position.account_id,
                position.venue,
                team_id=position.team_id,
            )
            protection = session.scalar(
                select(ProtectionOrder).where(ProtectionOrder.position_id == position_id)
            )
            effective_coverage = fully_covered and known
            status = (
                ProtectionStatus.UNKNOWN
                if not known
                else ProtectionStatus.ACTIVE
                if effective_coverage
                else ProtectionStatus.DEGRADED
            )
            if protection is None:
                protection = ProtectionOrder(
                    position_id=position_id,
                    venue_order_id=venue_order_id,
                    quantity=quantity,
                    trigger_price=trigger_price,
                    status=status.value,
                    fully_covered=effective_coverage,
                    observed_at=fact_time,
                    updated_at=now,
                )
                session.add(protection)
                session.flush()
            else:
                protection.venue_order_id = venue_order_id
                protection.quantity = quantity
                protection.trigger_price = trigger_price
                protection.status = status.value
                protection.fully_covered = effective_coverage
                protection.observed_at = fact_time
                protection.updated_at = now
            if not effective_coverage:
                PROTECTION_ISSUES.inc()
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="PROTECTION_RECORDED",
                object_type="ProtectionOrder",
                object_id=protection.protection_id,
                reason=status.value,
                correlation_id=uuid4(),
                object_version=1,
                now=now,
            )
            return protection.protection_id

    @staticmethod
    def _record_account_equity_observation(
        session: Session,
        fact: AccountEquity,
        *,
        recorded_at: datetime,
    ) -> None:
        session.flush()
        latest_observed_at = session.scalar(
            select(AccountEquityObservation.observed_at)
            .where(AccountEquityObservation.account_equity_id == fact.account_equity_id)
            .order_by(AccountEquityObservation.observed_at.desc())
            .limit(1)
        )
        if latest_observed_at is not None and (
            fact.observed_at < latest_observed_at + CAPITAL_HISTORY_MIN_INTERVAL
        ):
            return
        usd_equity = None
        if fact.fact_status == FactStatus.KNOWN.value:
            usd_equity = (
                fact.equity if fact.currency.upper() in USD_STABLE_ASSETS else fact.valuation_equity
            )
        session.add(
            AccountEquityObservation(
                team_id=fact.team_id,
                account_equity_id=fact.account_equity_id,
                environment=fact.environment,
                location_type=fact.location_type,
                account_id=fact.account_id,
                venue=fact.venue,
                currency=fact.currency,
                equity=fact.equity,
                available_balance=fact.available_balance,
                usd_equity=usd_equity,
                observed_at=fact.observed_at,
                recorded_at=recorded_at,
            )
        )

    def record_account_equity(
        self,
        account_id: str,
        venue: str,
        equity: Decimal,
        available_balance: Decimal,
        currency: str,
        known: bool,
        actor_id: UUID,
        *,
        environment: ExecutionEnvironment = ExecutionEnvironment.SHADOW,
        observed_at: datetime | None = None,
        now: datetime,
    ) -> UUID:
        fact_time = now if observed_at is None else observed_at
        if fact_time > now:
            _reject("FACT_TIME_INVALID", "equity observation cannot be in the future")
        with self.database.session_factory.begin() as session:
            team = self._require_role(session, actor_id, "venue.record", account_id, venue)
            if (
                environment is ExecutionEnvironment.SHADOW
                and team.execution_mode == TeamExecutionMode.SHADOW.value
            ):
                _reject(
                    "SHADOW_FACTS_SIMULATOR_MANAGED",
                    "active team SHADOW equity is managed by initialization and the simulator",
                )
            self._ensure_exchange_account_reference(
                session,
                team=team,
                actor_id=actor_id,
                account_id=account_id,
                venue=venue,
                now=now,
            )
            fact = session.scalar(
                select(AccountEquity).where(
                    AccountEquity.team_id == team.team_id,
                    AccountEquity.account_id == account_id,
                    AccountEquity.venue == venue,
                    AccountEquity.environment == environment.value,
                    AccountEquity.currency == currency,
                )
            )
            stable = currency.upper() in USD_STABLE_ASSETS
            if fact is None:
                fact = AccountEquity(
                    team_id=team.team_id,
                    account_id=account_id,
                    venue=venue,
                    environment=environment.value,
                    equity=equity,
                    available_balance=available_balance,
                    currency=currency,
                    location_type="VENUE",
                    control_status=(
                        "CONTROLLED" if environment is ExecutionEnvironment.SHADOW else "READ_ONLY"
                    ),
                    deposit_status="READY",
                    valuation_currency="USD" if stable else None,
                    valuation_price=Decimal(1) if stable else None,
                    valuation_equity=equity if stable else None,
                    valuation_observed_at=fact_time if stable else None,
                    fact_status=FactStatus.KNOWN.value if known else FactStatus.UNKNOWN.value,
                    observed_at=fact_time,
                    updated_at=now,
                )
                session.add(fact)
                session.flush()
            else:
                fact.equity = equity
                fact.available_balance = available_balance
                fact.currency = currency
                if environment is ExecutionEnvironment.SHADOW:
                    fact.location_type = "VENUE"
                    fact.control_status = "CONTROLLED"
                    fact.deposit_status = "READY"
                fact.valuation_currency = "USD" if stable else None
                fact.valuation_price = Decimal(1) if stable else None
                fact.valuation_equity = equity if stable else None
                fact.valuation_observed_at = fact_time if stable else None
                fact.fact_status = FactStatus.KNOWN.value if known else FactStatus.UNKNOWN.value
                fact.observed_at = fact_time
                fact.updated_at = now
            self._record_account_equity_observation(session, fact, recorded_at=now)
            return fact.account_equity_id

    def record_funding(
        self,
        campaign_id: UUID,
        venue: str,
        venue_payment_id: str,
        amount: Decimal,
        currency: str,
        actor_id: UUID,
        *,
        required_environment: ExecutionEnvironment | None = None,
        now: datetime,
    ) -> UUID:
        with self.database.session_factory.begin() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                _reject("CAMPAIGN_NOT_FOUND", "funding campaign is missing")
            if (
                required_environment is not None
                and campaign.environment != required_environment.value
            ):
                _reject(
                    "EXECUTION_ENVIRONMENT_MISMATCH",
                    "campaign is outside the requested execution environment",
                )
            self._require_role(
                session,
                actor_id,
                "venue.record",
                campaign.account_id,
                campaign.venue,
                team_id=campaign.team_id,
            )
            instrument = session.get(Instrument, campaign.instrument_id)
            if venue != campaign.venue:
                _reject("VENUE_SCOPE_MISMATCH", "funding venue does not match campaign")
            if instrument is None or currency != instrument.collateral_currency:
                _reject("PNL_CURRENCY_MISMATCH", "funding currency lacks an FX conversion")
            existing = session.scalar(
                select(FundingPayment).where(
                    FundingPayment.team_id == campaign.team_id,
                    FundingPayment.environment == campaign.environment,
                    FundingPayment.account_id == campaign.account_id,
                    FundingPayment.venue == venue,
                    FundingPayment.venue_payment_id == venue_payment_id,
                )
            )
            if existing is not None:
                if (
                    existing.campaign_id == campaign_id
                    and existing.amount == amount
                    and existing.currency == currency
                ):
                    return existing.funding_payment_id
                raise IdempotencyConflict
            payment = FundingPayment(
                team_id=campaign.team_id,
                campaign_id=campaign_id,
                account_id=campaign.account_id,
                venue=venue,
                environment=campaign.environment,
                instrument_id=campaign.instrument_id,
                venue_payment_id=venue_payment_id,
                amount=amount,
                currency=currency,
                paid_at=now,
            )
            session.add(payment)
            session.flush()
            return payment.funding_payment_id

    def ingest_binance_read_only_snapshot(
        self,
        account_id: str,
        actor_id: UUID,
        snapshot: BinanceReadOnlySnapshot,
        *,
        environment: ExecutionEnvironment,
        now: datetime,
    ) -> dict[str, Any]:
        """Persist one Binance USER_DATA snapshot without any venue-side mutation."""

        return self._ingest_read_only_snapshot(
            account_id,
            actor_id,
            snapshot,
            venue="BINANCE",
            environment=environment,
            now=now,
        )

    def ingest_hyperliquid_read_only_snapshot(
        self,
        account_id: str,
        actor_id: UUID,
        snapshot: HyperliquidReadOnlySnapshot,
        *,
        environment: ExecutionEnvironment,
        now: datetime,
    ) -> dict[str, Any]:
        """Persist one Hyperliquid Core Info snapshot without Exchange actions."""

        return self._ingest_read_only_snapshot(
            account_id,
            actor_id,
            snapshot,
            venue="HYPERLIQUID",
            environment=environment,
            now=now,
        )

    def ingest_binance_read_only_account_snapshot(
        self,
        account_id: str,
        actor_id: UUID,
        snapshots: tuple[BinanceReadOnlySnapshot, ...],
        *,
        environment: ExecutionEnvironment,
        runtime_binding: PreparedRuntimeAccountBinding | None = None,
        now: datetime,
    ) -> dict[str, Any]:
        """Persist a fully parsed Binance account snapshot and cover absent positions."""

        return self._ingest_read_only_account_snapshot(
            account_id,
            actor_id,
            snapshots,
            venue="BINANCE",
            environment=environment,
            runtime_binding=runtime_binding,
            now=now,
        )

    def ingest_hyperliquid_read_only_account_snapshot(
        self,
        account_id: str,
        actor_id: UUID,
        snapshots: tuple[HyperliquidReadOnlySnapshot, ...],
        *,
        environment: ExecutionEnvironment,
        runtime_binding: PreparedRuntimeAccountBinding | None = None,
        now: datetime,
    ) -> dict[str, Any]:
        """Persist a fully parsed Hyperliquid account snapshot and cover absent positions."""

        return self._ingest_read_only_account_snapshot(
            account_id,
            actor_id,
            snapshots,
            venue="HYPERLIQUID",
            environment=environment,
            runtime_binding=runtime_binding,
            now=now,
        )

    def ingest_okx_read_only_account_snapshot(
        self,
        account_id: str,
        actor_id: UUID,
        snapshots: tuple[VenueReadOnlySnapshot, ...],
        *,
        environment: ExecutionEnvironment,
        runtime_binding: PreparedRuntimeAccountBinding | None = None,
        now: datetime,
    ) -> dict[str, Any]:
        """Persist one complete normalized OKX USDT linear SWAP account read."""

        return self._ingest_read_only_account_snapshot(
            account_id,
            actor_id,
            snapshots,
            venue="OKX",
            environment=environment,
            runtime_binding=runtime_binding,
            now=now,
        )

    def ingest_bybit_read_only_account_snapshot(
        self,
        account_id: str,
        actor_id: UUID,
        snapshots: tuple[VenueReadOnlySnapshot, ...],
        *,
        environment: ExecutionEnvironment,
        runtime_binding: PreparedRuntimeAccountBinding | None = None,
        now: datetime,
    ) -> dict[str, Any]:
        """Persist one complete normalized Bybit Unified USDT linear account read."""

        return self._ingest_read_only_account_snapshot(
            account_id,
            actor_id,
            snapshots,
            venue="BYBIT",
            environment=environment,
            runtime_binding=runtime_binding,
            now=now,
        )

    def _ingest_read_only_account_snapshot(
        self,
        account_id: str,
        actor_id: UUID,
        snapshots: tuple[
            BinanceReadOnlySnapshot | HyperliquidReadOnlySnapshot | VenueReadOnlySnapshot, ...
        ],
        *,
        venue: str,
        environment: ExecutionEnvironment,
        runtime_binding: PreparedRuntimeAccountBinding | None = None,
        now: datetime,
    ) -> dict[str, Any]:
        if not snapshots:
            _reject(f"{venue}_RESPONSE_INCOMPLETE", "account snapshot has no configured coverage")
        symbols = [snapshot.symbol for snapshot in snapshots]
        if len(set(symbols)) != len(symbols):
            _reject(f"{venue}_RESPONSE_INVALID", "account snapshot contains duplicate symbols")
        observed_at = snapshots[0].observed_at
        if any(snapshot.observed_at != observed_at for snapshot in snapshots):
            _reject(
                f"{venue}_RESPONSE_INCOMPLETE", "account snapshot observations are inconsistent"
            )
        history_error_codes = {snapshot.history_error_code for snapshot in snapshots}
        if len(history_error_codes) != 1:
            _reject(f"{venue}_RESPONSE_INCOMPLETE", "account history status is inconsistent")
        history_error_code = next(iter(history_error_codes))
        if history_error_code is not None and any(
            snapshot.fills or snapshot.funding for snapshot in snapshots
        ):
            _reject(
                f"{venue}_RESPONSE_INVALID",
                "incomplete account history cannot contain partial facts",
            )
        covered_hyperliquid_dexes = (
            {
                snapshot.symbol.split(":", 1)[0] if ":" in snapshot.symbol else ""
                for snapshot in snapshots
            }
            if venue == "HYPERLIQUID"
            else None
        )
        if (
            venue == "HYPERLIQUID"
            and len(
                {
                    (
                        snapshot.equity.currency,
                        snapshot.equity.equity,
                        snapshot.equity.available_balance,
                    )
                    for snapshot in snapshots
                }
            )
            != 1
        ):
            _reject(
                "HYPERLIQUID_EQUITY_SCOPE_INCONSISTENT",
                "Hyperliquid Core and configured HIP-3 equity facts are not a unified total",
            )

        with self.database.session_factory.begin() as session:
            if runtime_binding is not None:
                self._lock_runtime_account_binding(session, runtime_binding)
            persisted: dict[str, Any] = {}
            for snapshot in sorted(snapshots, key=lambda item: item.symbol):
                persisted[snapshot.symbol] = self._ingest_read_only_snapshot(
                    account_id,
                    actor_id,
                    snapshot,
                    venue=venue,
                    environment=environment,
                    now=now,
                    session=session,
                )
            explicitly_closed = sum(
                1 for item in persisted.values() if item["position_authoritatively_closed"] is True
            )
            active_symbols = {
                snapshot.symbol for snapshot in snapshots if snapshot.position.quantity != 0
            }
            closed, covered = self._cover_absent_positions(
                account_id,
                actor_id,
                venue=venue,
                environment=environment,
                active_symbols=active_symbols,
                observed_order_ids={
                    order.order_id for snapshot in snapshots for order in snapshot.orders
                },
                covered_hyperliquid_dexes=covered_hyperliquid_dexes,
                now=now,
                session=session,
            )
        return {
            "symbols": persisted,
            "positions_covered": covered,
            "positions_authoritatively_closed": explicitly_closed + closed,
            "history_error_code": history_error_code,
        }

    def _cover_absent_positions(
        self,
        account_id: str,
        actor_id: UUID,
        *,
        venue: str,
        environment: ExecutionEnvironment,
        active_symbols: set[str],
        observed_order_ids: set[str],
        covered_hyperliquid_dexes: set[str] | None = None,
        now: datetime,
        session: Session | None = None,
    ) -> tuple[int, int]:
        """Refresh every scoped position; absence in a complete account snapshot means flat."""

        transaction: AbstractContextManager[Session] = (
            self.database.session_factory.begin() if session is None else nullcontext(session)
        )
        with transaction as session:
            team = self._require_role(session, actor_id, "venue.record", account_id, venue)
            scoped = session.execute(
                select(Position, Instrument)
                .join(Instrument, Position.instrument_id == Instrument.instrument_id)
                .where(
                    Position.team_id == team.team_id,
                    Position.account_id == account_id,
                    Position.venue == venue,
                    Position.environment == environment.value,
                    Instrument.venue == venue,
                )
                .order_by(Instrument.symbol, Position.position_id)
                .with_for_update()
            ).all()
            if any(position.updated_at > now for position, _instrument in scoped):
                _reject(
                    f"{venue}_SNAPSHOT_SUPERSEDED",
                    "an older account snapshot cannot overwrite newer position facts",
                )
            closed = 0
            covered = 0
            for position, instrument in scoped:
                if covered_hyperliquid_dexes is not None:
                    instrument_dex = (
                        instrument.symbol.split(":", 1)[0] if ":" in instrument.symbol else ""
                    )
                    if instrument_dex not in covered_hyperliquid_dexes:
                        continue
                covered += 1
                if instrument.symbol in active_symbols:
                    continue
                changed = (
                    position.quantity != 0
                    or position.average_entry_price != 0
                    or position.fact_status != FactStatus.KNOWN.value
                    or position.observed_at != now
                )
                if position.quantity != 0:
                    closed += 1
                position.quantity = Decimal(0)
                position.average_entry_price = Decimal(0)
                position.fact_status = FactStatus.KNOWN.value
                position.observed_at = now
                position.updated_at = now

                protection = session.scalar(
                    select(ProtectionOrder)
                    .where(ProtectionOrder.position_id == position.position_id)
                    .with_for_update()
                )
                if protection is not None:
                    protection_order = session.scalar(
                        select(VenueOrder)
                        .where(
                            VenueOrder.team_id == team.team_id,
                            VenueOrder.environment == environment.value,
                            VenueOrder.account_id == account_id,
                            VenueOrder.venue == venue,
                            VenueOrder.venue_order_id == protection.venue_order_id,
                        )
                        .with_for_update()
                    )
                    if protection_order is not None and protection_order.status in {
                        VenueOrderStatus.SENT.value,
                        VenueOrderStatus.PARTIALLY_FILLED.value,
                        VenueOrderStatus.UNKNOWN.value,
                    }:
                        still_open = protection_order.venue_order_id in observed_order_ids
                        if not still_open:
                            protection_order.status = VenueOrderStatus.CANCELLED.value
                        protection_order.observed_at = now
                        protection_order.updated_at = now
                        if still_open:
                            protection.quantity = Decimal(0)
                            protection.status = ProtectionStatus.DEGRADED.value
                            protection.fully_covered = False
                            protection.observed_at = now
                            protection.updated_at = now
                        else:
                            session.delete(protection)
                    else:
                        session.delete(protection)
                if changed:
                    self._audit(
                        session,
                        actor_id=str(actor_id),
                        event_type=f"{venue}_POSITION_COVERED",
                        object_type="Position",
                        object_id=position.position_id,
                        reason="AUTHORITATIVE_FLAT",
                        correlation_id=uuid4(),
                        object_version=1,
                        now=now,
                    )
            return closed, covered

    @staticmethod
    def _intent_id_from_client_order(venue: str, client_order_id: str) -> UUID | None:
        raw: str | None = None
        if venue == "BINANCE" and client_order_id.startswith("tcp-"):
            raw = client_order_id.removeprefix("tcp-")
        elif venue == "HYPERLIQUID" and client_order_id.startswith("0x"):
            raw = client_order_id.removeprefix("0x")
        if raw is None:
            return None
        try:
            if venue == "BINANCE" and len(raw) == 22:
                return UUID(bytes=base64.urlsafe_b64decode(f"{raw}=="))
            return _as_uuid(raw)
        except (binascii.Error, ValueError):
            return None

    def _ingest_read_only_snapshot(
        self,
        account_id: str,
        actor_id: UUID,
        snapshot: BinanceReadOnlySnapshot | HyperliquidReadOnlySnapshot | VenueReadOnlySnapshot,
        *,
        venue: str,
        environment: ExecutionEnvironment,
        now: datetime,
        session: Session | None = None,
    ) -> dict[str, Any]:
        """Persist one normalized narrow-adapter snapshot into authoritative facts."""

        if snapshot.observed_at > now + MAX_FACT_CLOCK_SKEW:
            _reject("FACT_TIME_INVALID", f"{venue} snapshot is unexpectedly in the future")
        transaction: AbstractContextManager[Session] = (
            self.database.session_factory.begin() if session is None else nullcontext(session)
        )
        with transaction as session:
            team = self._require_role(session, actor_id, "venue.record", account_id, venue)
            self._ensure_exchange_account_reference(
                session,
                team=team,
                actor_id=actor_id,
                account_id=account_id,
                venue=venue,
                now=now,
            )
            instrument = session.scalar(
                select(Instrument)
                .where(
                    Instrument.venue == venue,
                    Instrument.symbol == snapshot.symbol,
                )
                .with_for_update()
            )
            if instrument is None:
                instrument = Instrument(
                    venue=venue,
                    symbol=snapshot.symbol,
                    tick_size=snapshot.instrument.tick_size,
                    lot_size=snapshot.instrument.lot_size,
                    minimum_notional=snapshot.instrument.minimum_notional,
                    contract_multiplier=Decimal(1),
                    quote_currency=snapshot.instrument.quote_currency,
                    collateral_currency=snapshot.instrument.collateral_currency,
                    active=snapshot.instrument.active,
                    protection_supported=True,
                    updated_at=now,
                )
                session.add(instrument)
                session.flush()
            else:
                instrument.tick_size = snapshot.instrument.tick_size
                instrument.lot_size = snapshot.instrument.lot_size
                instrument.minimum_notional = snapshot.instrument.minimum_notional
                instrument.quote_currency = snapshot.instrument.quote_currency
                instrument.collateral_currency = snapshot.instrument.collateral_currency
                instrument.active = snapshot.instrument.active
                instrument.updated_at = now

            position = session.scalar(
                select(Position)
                .where(
                    Position.team_id == team.team_id,
                    Position.account_id == account_id,
                    Position.venue == venue,
                    Position.environment == environment.value,
                    Position.instrument_id == instrument.instrument_id,
                )
                .with_for_update()
            )
            position_authoritatively_closed = bool(
                position is not None and position.quantity != 0 and snapshot.position.quantity == 0
            )
            if position is None:
                position = Position(
                    team_id=team.team_id,
                    account_id=account_id,
                    venue=venue,
                    environment=environment.value,
                    instrument_id=instrument.instrument_id,
                    quantity=snapshot.position.quantity,
                    average_entry_price=snapshot.position.average_entry_price,
                    mark_price=snapshot.position.mark_price,
                    fact_status=FactStatus.KNOWN.value,
                    # A current snapshot's freshness starts when this process
                    # successfully receives it. Exchange event timestamps can be
                    # ahead of the host clock and remain attached to orders/fills.
                    observed_at=now,
                    updated_at=now,
                )
                session.add(position)
                session.flush()
            else:
                if position.updated_at > now:
                    _reject(
                        f"{venue}_SNAPSHOT_SUPERSEDED",
                        "an older snapshot cannot overwrite newer position facts",
                    )
                position.quantity = snapshot.position.quantity
                position.average_entry_price = snapshot.position.average_entry_price
                position.mark_price = snapshot.position.mark_price
                position.fact_status = FactStatus.KNOWN.value
                position.observed_at = now
                position.updated_at = now

            equity = session.scalar(
                select(AccountEquity)
                .where(
                    AccountEquity.team_id == team.team_id,
                    AccountEquity.account_id == account_id,
                    AccountEquity.venue == venue,
                    AccountEquity.environment == environment.value,
                    AccountEquity.currency == snapshot.equity.currency,
                )
                .with_for_update()
            )
            stable_equity = snapshot.equity.currency.upper() in USD_STABLE_ASSETS
            if equity is None:
                equity = AccountEquity(
                    team_id=team.team_id,
                    account_id=account_id,
                    venue=venue,
                    environment=environment.value,
                    equity=snapshot.equity.equity,
                    available_balance=snapshot.equity.available_balance,
                    currency=snapshot.equity.currency,
                    valuation_currency="USD" if stable_equity else None,
                    valuation_price=Decimal(1) if stable_equity else None,
                    valuation_equity=snapshot.equity.equity if stable_equity else None,
                    valuation_observed_at=now if stable_equity else None,
                    fact_status=FactStatus.KNOWN.value,
                    observed_at=now,
                    updated_at=now,
                )
                session.add(equity)
            else:
                if equity.updated_at > now:
                    _reject(
                        f"{venue}_SNAPSHOT_SUPERSEDED",
                        "an older snapshot cannot overwrite newer equity facts",
                    )
                equity.equity = snapshot.equity.equity
                equity.available_balance = snapshot.equity.available_balance
                equity.currency = snapshot.equity.currency
                equity.valuation_currency = "USD" if stable_equity else None
                equity.valuation_price = Decimal(1) if stable_equity else None
                equity.valuation_equity = snapshot.equity.equity if stable_equity else None
                equity.valuation_observed_at = now if stable_equity else None
                equity.fact_status = FactStatus.KNOWN.value
                equity.observed_at = now
                equity.updated_at = now
            self._record_account_equity_observation(session, equity, recorded_at=now)

            order_count = 0
            for external_order in snapshot.orders:
                intent: OrderIntent | None = None
                candidate = self._intent_id_from_client_order(venue, external_order.client_order_id)
                if candidate is not None:
                    intent = session.get(OrderIntent, candidate)
                if intent is not None:
                    campaign = session.get(Campaign, intent.campaign_id)
                    if (
                        campaign is None
                        or campaign.team_id != team.team_id
                        or campaign.account_id != account_id
                        or campaign.venue != venue
                        or campaign.environment != environment.value
                        or campaign.instrument_id != instrument.instrument_id
                    ):
                        _reject(
                            f"{venue}_ORDER_BINDING_INVALID",
                            "client order identity does not match its internal scope",
                        )
                current_order = session.scalar(
                    select(VenueOrder)
                    .where(
                        VenueOrder.team_id == team.team_id,
                        VenueOrder.environment == environment.value,
                        VenueOrder.account_id == account_id,
                        VenueOrder.venue == venue,
                        VenueOrder.venue_order_id == external_order.order_id,
                    )
                    .with_for_update()
                )
                if current_order is None:
                    current_order = session.scalar(
                        select(VenueOrder)
                        .where(
                            VenueOrder.team_id == team.team_id,
                            VenueOrder.environment == environment.value,
                            VenueOrder.account_id == account_id,
                            VenueOrder.venue == venue,
                            VenueOrder.client_order_id == external_order.client_order_id,
                        )
                        .with_for_update()
                    )
                if current_order is None:
                    current_order = VenueOrder(
                        team_id=team.team_id,
                        order_intent_id=None if intent is None else intent.intent_id,
                        account_id=account_id,
                        venue=venue,
                        environment=environment.value,
                        instrument_id=instrument.instrument_id,
                        venue_order_id=external_order.order_id,
                        client_order_id=external_order.client_order_id,
                        side=external_order.side,
                        order_type=external_order.order_type,
                        reduce_only=external_order.reduce_only or external_order.close_position,
                        status=external_order.status,
                        ordered_quantity=external_order.ordered_quantity,
                        filled_quantity=external_order.filled_quantity,
                        observed_at=external_order.observed_at,
                        updated_at=now,
                    )
                    session.add(current_order)
                else:
                    if (
                        current_order.account_id != account_id
                        or current_order.instrument_id != instrument.instrument_id
                        or current_order.client_order_id != external_order.client_order_id
                        or current_order.side != external_order.side
                        or current_order.order_type != external_order.order_type
                        or current_order.reduce_only
                        != (external_order.reduce_only or external_order.close_position)
                        or (
                            current_order.order_intent_id is not None
                            and intent is not None
                            and current_order.order_intent_id != intent.intent_id
                        )
                    ):
                        _reject(f"{venue}_FACT_CONFLICT", "venue order identity changed scope")
                    if current_order.order_intent_id is None and intent is not None:
                        current_order.order_intent_id = intent.intent_id
                    current_order.venue_order_id = external_order.order_id
                    current_order.status = external_order.status
                    current_order.ordered_quantity = external_order.ordered_quantity
                    current_order.filled_quantity = external_order.filled_quantity
                    current_order.observed_at = external_order.observed_at
                    current_order.updated_at = now
                order_count += 1
            session.flush()

            fill_count = 0
            for external_fill in snapshot.fills:
                current_fill = session.scalar(
                    select(VenueFill).where(
                        VenueFill.team_id == team.team_id,
                        VenueFill.environment == environment.value,
                        VenueFill.account_id == account_id,
                        VenueFill.venue == venue,
                        VenueFill.venue_fill_id == external_fill.fill_id,
                    )
                )
                venue_order = session.scalar(
                    select(VenueOrder).where(
                        VenueOrder.team_id == team.team_id,
                        VenueOrder.environment == environment.value,
                        VenueOrder.account_id == account_id,
                        VenueOrder.venue == venue,
                        VenueOrder.venue_order_id == external_fill.order_id,
                    )
                )
                if venue_order is None:
                    # Older Freqtrade observations used a synthetic trade identity instead of
                    # the exchange order id. Bind only an exact, unique, recent fully-filled
                    # order; ambiguity remains unbound and therefore fail-closed in reconcile.
                    synthetic_candidates = session.scalars(
                        select(VenueOrder).where(
                            VenueOrder.team_id == team.team_id,
                            VenueOrder.environment == environment.value,
                            VenueOrder.account_id == account_id,
                            VenueOrder.venue == venue,
                            VenueOrder.instrument_id == instrument.instrument_id,
                            VenueOrder.order_intent_id.is_not(None),
                            VenueOrder.venue_order_id.like("freqtrade:%"),
                            VenueOrder.status == VenueOrderStatus.FILLED.value,
                            VenueOrder.side == external_fill.side,
                            VenueOrder.ordered_quantity == external_fill.quantity,
                            VenueOrder.filled_quantity == external_fill.quantity,
                        )
                    ).all()
                    synthetic_candidates = [
                        item
                        for item in synthetic_candidates
                        if abs(item.observed_at - external_fill.executed_at) <= timedelta(minutes=2)
                    ]
                    if len(synthetic_candidates) == 1:
                        venue_order = synthetic_candidates[0]
                        venue_order.venue_order_id = external_fill.order_id
                        venue_order.observed_at = external_fill.executed_at
                        venue_order.updated_at = now
                if venue_order is None:
                    # A slow Freqtrade fill can arrive after the bounded API confirmation
                    # window. Recover only one exact scoped UNKNOWN intent whose frozen
                    # maximum contains this fill. Ambiguity stays unbound and fail-closed.
                    unknown_candidates = session.scalars(
                        select(VenueOrder)
                        .join(
                            OrderIntent,
                            VenueOrder.order_intent_id == OrderIntent.intent_id,
                        )
                        .where(
                            VenueOrder.team_id == team.team_id,
                            VenueOrder.environment == environment.value,
                            VenueOrder.account_id == account_id,
                            VenueOrder.venue == venue,
                            VenueOrder.instrument_id == instrument.instrument_id,
                            VenueOrder.order_intent_id.is_not(None),
                            VenueOrder.venue_order_id.like("UNKNOWN:%"),
                            VenueOrder.status == VenueOrderStatus.UNKNOWN.value,
                            VenueOrder.side == external_fill.side,
                            VenueOrder.filled_quantity == 0,
                            VenueOrder.ordered_quantity >= external_fill.quantity,
                            OrderIntent.status == OrderIntentStatus.UNKNOWN.value,
                        )
                        .with_for_update()
                    ).all()
                    unknown_candidates = [
                        item
                        for item in unknown_candidates
                        if abs(item.observed_at - external_fill.executed_at) <= timedelta(minutes=5)
                    ]
                    if len(unknown_candidates) == 1:
                        venue_order = unknown_candidates[0]
                        venue_order.venue_order_id = external_fill.order_id
                        venue_order.status = VenueOrderStatus.FILLED.value
                        venue_order.ordered_quantity = external_fill.quantity
                        venue_order.filled_quantity = external_fill.quantity
                        venue_order.observed_at = external_fill.executed_at
                        venue_order.updated_at = now
                intent = (
                    session.get(OrderIntent, venue_order.order_intent_id)
                    if venue_order is not None and venue_order.order_intent_id is not None
                    else None
                )
                campaign_id = None if intent is None else intent.campaign_id
                if current_fill is None:
                    session.add(
                        VenueFill(
                            team_id=team.team_id,
                            venue=venue,
                            venue_fill_id=external_fill.fill_id,
                            order_intent_id=None if intent is None else intent.intent_id,
                            campaign_id=campaign_id,
                            account_id=account_id,
                            environment=environment.value,
                            instrument_id=instrument.instrument_id,
                            side=external_fill.side,
                            quantity=external_fill.quantity,
                            price=external_fill.price,
                            fee=external_fill.fee,
                            fee_currency=external_fill.fee_currency,
                            slippage_cost=Decimal(0),
                            executed_at=external_fill.executed_at,
                        )
                    )
                elif (
                    current_fill.account_id != account_id
                    or current_fill.instrument_id != instrument.instrument_id
                    or current_fill.side != external_fill.side
                    or current_fill.quantity != external_fill.quantity
                    or current_fill.price != external_fill.price
                    or current_fill.fee != external_fill.fee
                    or current_fill.fee_currency != external_fill.fee_currency
                ):
                    _reject(f"{venue}_FACT_CONFLICT", "venue fill identity changed semantics")
                elif current_fill.order_intent_id is None and intent is not None:
                    current_fill.order_intent_id = intent.intent_id
                    current_fill.campaign_id = intent.campaign_id
                elif (
                    current_fill.order_intent_id is not None
                    and intent is not None
                    and current_fill.order_intent_id != intent.intent_id
                ):
                    _reject(f"{venue}_FACT_CONFLICT", "venue fill identity changed binding")
                fill_count += 1

            session.flush()
            bound_orders = session.scalars(
                select(VenueOrder)
                .where(
                    VenueOrder.team_id == team.team_id,
                    VenueOrder.environment == environment.value,
                    VenueOrder.account_id == account_id,
                    VenueOrder.venue == venue,
                    VenueOrder.instrument_id == instrument.instrument_id,
                    VenueOrder.order_intent_id.is_not(None),
                )
                .with_for_update()
            ).all()
            for bound_order in bound_orders:
                if bound_order.order_intent_id is None:
                    continue
                bound_intent = session.get(
                    OrderIntent, bound_order.order_intent_id, with_for_update=True
                )
                if bound_intent is None:
                    _reject(f"{venue}_ORDER_BINDING_INVALID", "bound order intent is missing")
                bound_campaign = session.get(
                    Campaign, bound_intent.campaign_id, with_for_update=True
                )
                if bound_campaign is None:
                    _reject(f"{venue}_ORDER_BINDING_INVALID", "bound order campaign is missing")
                if bound_campaign.team_id != team.team_id:
                    _reject(f"{venue}_ORDER_BINDING_INVALID", "bound order crossed team scope")
                intent_fills = session.scalars(
                    select(VenueFill).where(
                        VenueFill.team_id == team.team_id,
                        VenueFill.order_intent_id == bound_intent.intent_id,
                    )
                ).all()
                if any(fill.side != bound_intent.side for fill in intent_fills):
                    _reject("FILL_SIDE_MISMATCH", f"{venue} fill side changed intent semantics")
                filled = sum((fill.quantity for fill in intent_fills), Decimal(0))
                if (
                    filled > bound_intent.quantity
                    or bound_order.filled_quantity > bound_intent.quantity
                    or filled > bound_order.ordered_quantity
                ):
                    _reject(
                        "ORDER_INTENT_OVERFILLED",
                        f"{venue} cumulative fill exceeds intent",
                    )
                if bound_campaign.status == CampaignStatus.CLOSED.value:
                    if bound_intent.status not in {
                        OrderIntentStatus.FILLED.value,
                        OrderIntentStatus.CANCELLED.value,
                        OrderIntentStatus.REJECTED.value,
                    } or bound_order.status not in {
                        VenueOrderStatus.FILLED.value,
                        VenueOrderStatus.CANCELLED.value,
                        VenueOrderStatus.REJECTED.value,
                    }:
                        _reject(
                            "CLOSED_CAMPAIGN_ORDER_NONTERMINAL",
                            f"{venue} closed campaign contains a non-terminal order fact",
                        )
                    # Repeated historical fills are expected. CLOSED is absorbing:
                    # terminal facts cannot reopen the campaign or its released risk.
                    bound_order.updated_at = now
                    continue
                if filled > bound_order.filled_quantity:
                    bound_order.filled_quantity = filled
                if filled > 0:
                    self._consume_add_unit(session, bound_intent)
                previous = bound_intent.status
                release_updated_intent = False
                terminal = {
                    VenueOrderStatus.CANCELLED.value: OrderIntentStatus.CANCELLED,
                    VenueOrderStatus.REJECTED.value: OrderIntentStatus.REJECTED,
                }
                if bound_order.status == VenueOrderStatus.UNKNOWN.value:
                    bound_intent.status = OrderIntentStatus.UNKNOWN.value
                    if bound_intent.reservation_id is not None:
                        reservation = session.get(
                            RiskReservation, bound_intent.reservation_id, with_for_update=True
                        )
                        if reservation is not None:
                            reservation.status = ReservationStatus.UNKNOWN.value
                            reservation.updated_at = now
                            reservation.version += 1
                    bound_campaign.status = CampaignStatus.UNKNOWN.value
                elif bound_order.status in terminal and filled == 0:
                    self._release_zero_fill_in_session(
                        session,
                        bound_intent,
                        terminal[bound_order.status],
                        confirmed_external=True,
                        now=now,
                    )
                    release_updated_intent = True
                else:
                    if filled > 0 and (
                        filled == bound_intent.quantity
                        or (
                            bound_order.status == VenueOrderStatus.FILLED.value
                            and filled == bound_order.ordered_quantity
                        )
                    ):
                        bound_intent.status = OrderIntentStatus.FILLED.value
                        bound_order.status = VenueOrderStatus.FILLED.value
                    elif filled > 0 and bound_order.status not in terminal:
                        bound_intent.status = OrderIntentStatus.PARTIALLY_FILLED.value
                        bound_order.status = VenueOrderStatus.PARTIALLY_FILLED.value
                    elif bound_order.status in terminal:
                        bound_intent.status = terminal[bound_order.status].value
                    if filled > 0 and bound_intent.reservation_id is not None:
                        reservation = session.get(
                            RiskReservation, bound_intent.reservation_id, with_for_update=True
                        )
                        if reservation is not None:
                            reservation.status = ReservationStatus.OPEN.value
                            reservation.updated_at = now
                            reservation.version += 1
                    if filled > 0:
                        if bound_intent.kind in {IntentKind.INITIAL.value, IntentKind.ADD.value}:
                            if bound_campaign.status not in {
                                CampaignStatus.CLOSING.value,
                                CampaignStatus.REDUCING.value,
                            }:
                                bound_campaign.status = CampaignStatus.OPEN.value
                        elif bound_intent.kind == IntentKind.EXIT.value:
                            bound_campaign.status = CampaignStatus.CLOSING.value
                        else:
                            bound_campaign.status = CampaignStatus.REDUCING.value
                if previous != bound_intent.status:
                    if not release_updated_intent:
                        bound_intent.updated_at = now
                        bound_intent.version += 1
                    INTENT_TRANSITIONS.labels(previous, bound_intent.status).inc()
                bound_order.updated_at = now
                bound_campaign.updated_at = now

            funding_count = 0
            funding_campaign = session.scalar(
                select(Campaign)
                .where(
                    Campaign.team_id == team.team_id,
                    Campaign.account_id == account_id,
                    Campaign.venue == venue,
                    Campaign.environment == environment.value,
                    Campaign.instrument_id == instrument.instrument_id,
                    Campaign.status != CampaignStatus.CLOSED.value,
                )
                .with_for_update()
            )
            for external_funding in snapshot.funding:
                current_funding = session.scalar(
                    select(FundingPayment).where(
                        FundingPayment.team_id == team.team_id,
                        FundingPayment.environment == environment.value,
                        FundingPayment.account_id == account_id,
                        FundingPayment.venue == venue,
                        FundingPayment.venue_payment_id == external_funding.payment_id,
                    )
                )
                if current_funding is None:
                    session.add(
                        FundingPayment(
                            team_id=team.team_id,
                            campaign_id=(
                                None if funding_campaign is None else funding_campaign.campaign_id
                            ),
                            account_id=account_id,
                            venue=venue,
                            environment=environment.value,
                            instrument_id=instrument.instrument_id,
                            venue_payment_id=external_funding.payment_id,
                            amount=external_funding.amount,
                            currency=external_funding.currency,
                            paid_at=external_funding.paid_at,
                        )
                    )
                elif (
                    current_funding.account_id != account_id
                    or current_funding.instrument_id != instrument.instrument_id
                    or current_funding.amount != external_funding.amount
                    or current_funding.currency != external_funding.currency
                ):
                    _reject(f"{venue}_FACT_CONFLICT", "funding identity changed semantics")
                elif current_funding.campaign_id is None and funding_campaign is not None:
                    current_funding.campaign_id = funding_campaign.campaign_id
                funding_count += 1

            protection = session.scalar(
                select(ProtectionOrder)
                .where(ProtectionOrder.position_id == position.position_id)
                .with_for_update()
            )
            if snapshot.position.quantity != 0:
                observed_protection = snapshot.protection
                covered = (
                    observed_protection is not None
                    and observed_protection.quantity >= abs(snapshot.position.quantity)
                    and observed_protection.trigger_price > 0
                )
                if protection is None:
                    protection = ProtectionOrder(
                        position_id=position.position_id,
                        venue_order_id=(
                            f"{venue}:UNKNOWN:{snapshot.symbol}"
                            if observed_protection is None
                            else observed_protection.order_id
                        ),
                        quantity=(
                            Decimal(0)
                            if observed_protection is None
                            else observed_protection.quantity
                        ),
                        trigger_price=(
                            Decimal(0)
                            if observed_protection is None
                            else observed_protection.trigger_price
                        ),
                        status=(
                            ProtectionStatus.ACTIVE.value
                            if covered
                            else ProtectionStatus.UNKNOWN.value
                            if observed_protection is None
                            else ProtectionStatus.DEGRADED.value
                        ),
                        fully_covered=covered,
                        observed_at=now,
                        updated_at=now,
                    )
                    session.add(protection)
                else:
                    protection.venue_order_id = (
                        f"{venue}:UNKNOWN:{snapshot.symbol}"
                        if observed_protection is None
                        else observed_protection.order_id
                    )
                    protection.quantity = (
                        Decimal(0) if observed_protection is None else observed_protection.quantity
                    )
                    protection.trigger_price = (
                        Decimal(0)
                        if observed_protection is None
                        else observed_protection.trigger_price
                    )
                    protection.status = (
                        ProtectionStatus.ACTIVE.value
                        if covered
                        else ProtectionStatus.UNKNOWN.value
                        if observed_protection is None
                        else ProtectionStatus.DEGRADED.value
                    )
                    protection.fully_covered = covered
                    protection.observed_at = now
                    protection.updated_at = now
            elif protection is not None:
                protection_order = session.scalar(
                    select(VenueOrder)
                    .where(
                        VenueOrder.environment == environment.value,
                        VenueOrder.account_id == account_id,
                        VenueOrder.venue == venue,
                        VenueOrder.venue_order_id == protection.venue_order_id,
                    )
                    .with_for_update()
                )
                observed_order_ids = {item.order_id for item in snapshot.orders}
                if protection_order is not None and protection_order.status in {
                    VenueOrderStatus.SENT.value,
                    VenueOrderStatus.PARTIALLY_FILLED.value,
                    VenueOrderStatus.UNKNOWN.value,
                }:
                    still_open = protection_order.venue_order_id in observed_order_ids
                    if not still_open:
                        protection_order.status = VenueOrderStatus.CANCELLED.value
                    protection_order.observed_at = snapshot.observed_at
                    protection_order.updated_at = now
                    if still_open:
                        protection.quantity = Decimal(0)
                        protection.status = ProtectionStatus.DEGRADED.value
                        protection.fully_covered = False
                        protection.observed_at = snapshot.observed_at
                        protection.updated_at = now
                    else:
                        session.delete(protection)
                else:
                    session.delete(protection)
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type=f"{venue}_READ_ONLY_SYNCED",
                object_type="Instrument",
                object_id=instrument.instrument_id,
                reason=(
                    f"orders={order_count},fills={fill_count},funding={funding_count},"
                    f"position={snapshot.position.quantity}"
                ),
                correlation_id=uuid4(),
                object_version=1,
                now=now,
            )
            return {
                "instrument_id": str(instrument.instrument_id),
                "orders": order_count,
                "fills": fill_count,
                "funding": funding_count,
                "position_authoritatively_closed": position_authoritatively_closed,
            }

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
            self._require_role(
                session,
                actor_id,
                "order.prepare",
                campaign.account_id,
                campaign.venue,
                team_id=campaign.team_id,
            )
            policy = session.scalar(select(RiskPolicy).where(RiskPolicy.active))
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
            self._audit(
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
            self._require_role(
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
            policy = session.scalar(select(RiskPolicy).where(RiskPolicy.active))
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
            digest, response = self._idempotency(
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
            self._save_receipt(
                session,
                caller_id=f"{actor_id}:{campaign.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self._audit(
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

    def recover_freqtrade_emergency_exit(
        self,
        campaign_id: UUID,
        actor_id: UUID,
        reason: str,
        *,
        now: datetime,
    ) -> UUID:
        """Bind one unique official cleanup fill after a Freqtrade outcome timeout.

        This is a fact-recovery path only. It never invokes a worker or venue and is
        restricted to a fresh, officially observed flat position.
        """

        if len(reason.strip()) < 8:
            _reject("EMERGENCY_EXIT_RECOVERY_REASON_REQUIRED", "a specific reason is required")
        with self.database.session_factory.begin() as session:
            campaign = session.get(Campaign, campaign_id, with_for_update=True)
            if campaign is None:
                _reject("CAMPAIGN_NOT_FOUND", "campaign does not exist")
            self._require_role(
                session,
                actor_id,
                "reconcile",
                campaign.account_id,
                campaign.venue,
                team_id=campaign.team_id,
            )
            assignments = session.scalars(
                select(RoleAssignment).where(
                    RoleAssignment.user_id == actor_id,
                    RoleAssignment.team_id == campaign.team_id,
                )
            ).all()
            if not any(
                item.role == Role.SYSTEM_ADMIN.value
                and item.account_scope is None
                and item.venue_scope is None
                for item in assignments
            ):
                _reject(
                    "EMERGENCY_EXIT_RECOVERY_ADMIN_REQUIRED",
                    "Freqtrade emergency exit recovery requires SYSTEM_ADMIN",
                )
            existing_exit = session.scalar(
                select(OrderIntent)
                .where(
                    OrderIntent.campaign_id == campaign_id,
                    OrderIntent.kind == IntentKind.EXIT.value,
                    OrderIntent.trigger_source == "FREQTRADE_EMERGENCY_RECOVERY",
                )
                .with_for_update()
            )
            if existing_exit is not None:
                return existing_exit.intent_id
            if campaign.status == CampaignStatus.CLOSED.value:
                _reject("CAMPAIGN_ALREADY_CLOSED", "closed campaigns require no recovery")
            policy = session.scalar(select(RiskPolicy).where(RiskPolicy.active))
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
                or self._fact_is_stale(
                    position.observed_at,
                    now,
                    timedelta(seconds=policy.max_fact_age_seconds),
                )
            ):
                _reject(
                    "EMERGENCY_EXIT_RECOVERY_FLAT_FACT_REQUIRED",
                    "recovery requires a fresh official flat position",
                )
            active_orders = session.scalar(
                select(VenueOrder.venue_order_fact_id).where(
                    VenueOrder.account_id == campaign.account_id,
                    VenueOrder.venue == campaign.venue,
                    VenueOrder.team_id == campaign.team_id,
                    VenueOrder.environment == campaign.environment,
                    VenueOrder.instrument_id == campaign.instrument_id,
                    VenueOrder.status.in_(
                        {
                            VenueOrderStatus.SENT.value,
                            VenueOrderStatus.PARTIALLY_FILLED.value,
                            VenueOrderStatus.UNKNOWN.value,
                        }
                    ),
                )
            )
            if active_orders is not None:
                _reject(
                    "EMERGENCY_EXIT_RECOVERY_OPEN_ORDER",
                    "recovery requires every scoped order outcome to be terminal",
                )
            entries = session.scalars(
                select(OrderIntent)
                .where(
                    OrderIntent.campaign_id == campaign_id,
                    OrderIntent.kind.in_({IntentKind.INITIAL.value, IntentKind.ADD.value}),
                )
                .with_for_update()
            ).all()
            if len(entries) != 1:
                _reject(
                    "EMERGENCY_EXIT_RECOVERY_ENTRY_AMBIGUOUS",
                    "recovery requires exactly one entry and no adds",
                )
            entry = entries[0]
            instrument = session.get(Instrument, campaign.instrument_id)
            if instrument is None:
                _reject("INSTRUMENT_UNAVAILABLE", "campaign instrument is unavailable")
            if entry.status == OrderIntentStatus.READY.value:
                existing_entry_order = session.scalar(
                    select(VenueOrder.venue_order_fact_id).where(
                        VenueOrder.order_intent_id == entry.intent_id
                    )
                )
                if existing_entry_order is not None:
                    _reject(
                        "EMERGENCY_EXIT_RECOVERY_ENTRY_AMBIGUOUS",
                        "a READY recovery entry must not already have an order fact",
                    )
                entry_candidates = session.scalars(
                    select(VenueFill)
                    .where(
                        VenueFill.team_id == campaign.team_id,
                        VenueFill.environment == campaign.environment,
                        VenueFill.account_id == campaign.account_id,
                        VenueFill.venue == campaign.venue,
                        VenueFill.instrument_id == campaign.instrument_id,
                        VenueFill.order_intent_id.is_(None),
                        VenueFill.campaign_id.is_(None),
                        VenueFill.side == entry.side,
                        VenueFill.quantity <= entry.quantity,
                        VenueFill.executed_at >= entry.created_at,
                        VenueFill.executed_at <= entry.created_at + timedelta(minutes=10),
                    )
                    .with_for_update()
                ).all()
                if len(entry_candidates) != 1:
                    _reject(
                        "EMERGENCY_EXIT_RECOVERY_ENTRY_AMBIGUOUS",
                        "interrupted entry recovery requires one unique same-side fill",
                    )
                recovered_entry_fill = entry_candidates[0]
                if recovered_entry_fill.fee_currency != instrument.collateral_currency:
                    _reject("PNL_CURRENCY_MISMATCH", "entry fill currency lacks an FX conversion")
                session.add(
                    VenueOrder(
                        team_id=campaign.team_id,
                        order_intent_id=entry.intent_id,
                        account_id=campaign.account_id,
                        venue=campaign.venue,
                        environment=campaign.environment,
                        instrument_id=campaign.instrument_id,
                        venue_order_id=f"recovered-fill:{recovered_entry_fill.venue_fill_id}",
                        client_order_id=f"recovery-entry-{entry.intent_id.hex[:24]}",
                        side=entry.side,
                        order_type="MARKET",
                        reduce_only=False,
                        status=VenueOrderStatus.FILLED.value,
                        ordered_quantity=recovered_entry_fill.quantity,
                        filled_quantity=recovered_entry_fill.quantity,
                        observed_at=recovered_entry_fill.executed_at,
                        updated_at=now,
                    )
                )
                recovered_entry_fill.order_intent_id = entry.intent_id
                recovered_entry_fill.campaign_id = campaign_id
                entry.status = OrderIntentStatus.FILLED.value
                entry.updated_at = now
                entry.version += 1
                if entry.reservation_id is not None:
                    reservation = session.get(
                        RiskReservation, entry.reservation_id, with_for_update=True
                    )
                    if reservation is not None:
                        reservation.status = ReservationStatus.OPEN.value
                        reservation.updated_at = now
                        reservation.version += 1
                campaign.status = CampaignStatus.OPEN.value
                campaign.updated_at = now
                self._audit(
                    session,
                    actor_id=str(actor_id),
                    event_type="FREQTRADE_INTERRUPTED_ENTRY_RECOVERED",
                    object_type="OrderIntent",
                    object_id=entry.intent_id,
                    reason=reason.strip(),
                    correlation_id=entry.correlation_id,
                    object_version=entry.version,
                    now=now,
                )
            elif entry.status != OrderIntentStatus.FILLED.value:
                _reject(
                    "EMERGENCY_EXIT_RECOVERY_ENTRY_AMBIGUOUS",
                    "recovery requires a READY interrupted entry or confirmed filled entry",
                )
            entry_fills = session.scalars(
                select(VenueFill)
                .where(VenueFill.order_intent_id == entry.intent_id)
                .order_by(VenueFill.executed_at, VenueFill.venue_fill_fact_id)
            ).all()
            if not entry_fills or any(fill.side != entry.side for fill in entry_fills):
                _reject(
                    "EMERGENCY_EXIT_RECOVERY_ENTRY_FACTS_INVALID",
                    "recovery requires complete entry fills",
                )
            entry_quantity = sum((fill.quantity for fill in entry_fills), Decimal(0))
            entry_time = max(fill.executed_at for fill in entry_fills)
            exit_side = "SELL" if entry.side == "BUY" else "BUY"
            candidates = session.scalars(
                select(VenueFill)
                .where(
                    VenueFill.team_id == campaign.team_id,
                    VenueFill.environment == campaign.environment,
                    VenueFill.account_id == campaign.account_id,
                    VenueFill.venue == campaign.venue,
                    VenueFill.instrument_id == campaign.instrument_id,
                    VenueFill.order_intent_id.is_(None),
                    VenueFill.campaign_id.is_(None),
                    VenueFill.side == exit_side,
                    VenueFill.quantity == entry_quantity,
                    VenueFill.executed_at >= entry_time,
                    VenueFill.executed_at <= entry_time + timedelta(minutes=10),
                )
                .with_for_update()
            ).all()
            if len(candidates) != 1:
                _reject(
                    "EMERGENCY_EXIT_RECOVERY_FILL_AMBIGUOUS",
                    "recovery requires one unique opposite cleanup fill",
                )
            cleanup_fill = candidates[0]
            if cleanup_fill.fee_currency != instrument.collateral_currency:
                _reject("PNL_CURRENCY_MISMATCH", "cleanup fill currency lacks an FX conversion")
            campaign.target_version += 1
            campaign.current_target_quantity = Decimal(0)
            campaign.target_reason = f"FREQTRADE_EMERGENCY_RECOVERY:{reason.strip()}"
            campaign.target_urgency = TargetUrgency.IMMEDIATE.value
            campaign.target_calculated_at = now
            campaign.status = CampaignStatus.CLOSING.value
            campaign.updated_at = now
            semantic_hash = hashlib.sha256(
                f"{campaign_id}:{cleanup_fill.venue_fill_id}:{entry_quantity}".encode()
            ).hexdigest()
            exit_intent = OrderIntent(
                campaign_id=campaign_id,
                authorization_id=campaign.authorization_id,
                reservation_id=None,
                kind=IntentKind.EXIT.value,
                side=exit_side,
                quantity=entry_quantity,
                limit_price=None,
                reduce_only=True,
                trigger_source="FREQTRADE_EMERGENCY_RECOVERY",
                trigger_observed_at=position.observed_at,
                add_unit_consumed=False,
                target_version=campaign.target_version,
                position_id=position.position_id,
                position_observed_at=position.observed_at,
                status=OrderIntentStatus.FILLED.value,
                semantic_hash=semantic_hash,
                actor_id=str(actor_id),
                correlation_id=uuid4(),
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(exit_intent)
            session.flush()
            session.add(
                VenueOrder(
                    team_id=campaign.team_id,
                    order_intent_id=exit_intent.intent_id,
                    account_id=campaign.account_id,
                    venue=campaign.venue,
                    environment=campaign.environment,
                    instrument_id=campaign.instrument_id,
                    venue_order_id=f"recovered-fill:{cleanup_fill.venue_fill_id}",
                    client_order_id=f"recovery-{exit_intent.intent_id.hex[:24]}",
                    side=exit_side,
                    order_type="MARKET",
                    reduce_only=True,
                    status=VenueOrderStatus.FILLED.value,
                    ordered_quantity=entry_quantity,
                    filled_quantity=entry_quantity,
                    observed_at=cleanup_fill.executed_at,
                    updated_at=now,
                )
            )
            cleanup_fill.order_intent_id = exit_intent.intent_id
            cleanup_fill.campaign_id = campaign_id
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="FREQTRADE_EMERGENCY_EXIT_RECOVERED",
                object_type="OrderIntent",
                object_id=exit_intent.intent_id,
                reason=reason.strip(),
                correlation_id=exit_intent.correlation_id,
                object_version=1,
                now=now,
            )
            return exit_intent.intent_id

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
            self._require_role(
                session,
                actor_id,
                "risk.tighten",
                campaign.account_id,
                campaign.venue,
                team_id=campaign.team_id,
            )
            digest, response = self._idempotency(
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
            policy = self._active_risk_policy(session, campaign.team_id)
            if proposal is None or policy is None:
                _reject("CAMPAIGN_MANAGEMENT_INVALID", "campaign management facts are incomplete")
            if position is None or position.fact_status != FactStatus.KNOWN.value:
                _reject("POSITION_UNKNOWN", "automatic exit requires a known current position")
            if self._fact_is_stale(
                position.observed_at,
                now,
                timedelta(seconds=policy.max_fact_age_seconds),
            ):
                _reject("STALE_FACTS", "automatic exit requires a fresh position")
            if position.quantity == 0:
                result: dict[str, Any] = {"reason": "POSITION_FLAT", "intent_id": None}
                self._save_receipt(
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
                self._save_receipt(
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
            self._save_receipt(
                session,
                caller_id=f"{actor_id}:{campaign.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self._audit(
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
            self._require_role(
                session,
                actor_id,
                "order.prepare",
                campaign.account_id,
                campaign.venue,
                team_id=campaign.team_id,
            )
            if campaign.status == CampaignStatus.CLOSED.value:
                return
            policy = session.scalar(select(RiskPolicy).where(RiskPolicy.active))
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
                or self._fact_is_stale(
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
            self._audit(
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
            self._enqueue_campaign_status_notification(
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
            self._require_role(
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
        self._record_account_equity_observation(session, equity, recorded_at=now)
        self._audit(
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
