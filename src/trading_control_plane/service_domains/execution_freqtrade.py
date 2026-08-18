from __future__ import annotations

# TradingService composes this domain with the account/risk/transaction domains.
# mypy: disable-error-code=attr-defined
from trading_control_plane.repositories.execution import find_position_for_scope
from trading_control_plane.service_component import ServiceComponent

# The domain implementation intentionally consumes the explicit service_core export surface.
# ruff: noqa: F403, F405
from trading_control_plane.service_core import *


class FreqtradeRecoveryExecutionService(ServiceComponent):
    _PASSIVE_FREQTRADE_RPC_EVENTS = frozenset({"strategy_msg", "status", "startup", "warning"})

    @staticmethod
    def _rpc_payload_values(payload: dict[str, Any], keys: frozenset[str]) -> set[str]:
        values: set[str] = set()
        pending: list[Any] = [payload]
        visited = 0
        while pending and visited < 128:
            current = pending.pop()
            visited += 1
            if isinstance(current, dict):
                for key, value in current.items():
                    if key in keys and value is not None and not isinstance(value, (dict, list)):
                        rendered = str(value).strip()
                        if rendered:
                            values.add(rendered)
                    elif isinstance(value, (dict, list)):
                        pending.append(value)
            elif isinstance(current, list):
                pending.extend(current[:128])
        return values

    @staticmethod
    def _tagged_intent_id(tag: str) -> UUID | None:
        if not tag.startswith("tcp-"):
            return None
        try:
            return UUID(hex=tag.removeprefix("tcp-"))
        except ValueError:
            return None

    def _freqtrade_rpc_intent(
        self,
        session: Session,
        binding: PreparedFreqtradeWorkerBinding,
        message: FreqtradeRpcMessage,
        trade: FreqtradeTrade,
    ) -> OrderIntent | None:
        """Resolve one exact approved dispatch without guessing among a trade's intents."""

        order_ids = self._rpc_payload_values(
            message.payload,
            frozenset({"order_id", "orderid"}),
        )
        tags = self._rpc_payload_values(
            message.payload,
            frozenset({"enter_tag", "entry_tag", "buy_tag", "ft_order_tag"}),
        )
        for order in trade.orders:
            if order.order_id in order_ids and order.tag:
                tags.add(order.tag)

        exact: dict[UUID, OrderIntent] = {}
        if order_ids:
            order_intents = session.scalars(
                select(OrderIntent)
                .join(VenueOrder, VenueOrder.order_intent_id == OrderIntent.intent_id)
                .join(Campaign, Campaign.campaign_id == OrderIntent.campaign_id)
                .where(
                    VenueOrder.venue_order_id.in_(order_ids),
                    Campaign.team_id == binding.team_id,
                    Campaign.environment == binding.environment,
                    Campaign.account_id == binding.account_id,
                    Campaign.venue == binding.venue,
                )
            ).all()
            exact.update({item.intent_id: item for item in order_intents})
        for tag in tags:
            tagged_id = self._tagged_intent_id(tag)
            if tagged_id is not None:
                tagged = session.get(OrderIntent, tagged_id)
                if tagged is not None:
                    exact[tagged.intent_id] = tagged
        if len(exact) == 1:
            return next(iter(exact.values()))
        if len(exact) > 1:
            return None

        candidates = session.scalars(
            select(OrderIntent)
            .join(Campaign, Campaign.campaign_id == OrderIntent.campaign_id)
            .where(
                OrderIntent.dispatch_backend == "FREQTRADE",
                OrderIntent.dispatch_external_id == trade.trade_id,
                Campaign.team_id == binding.team_id,
                Campaign.environment == binding.environment,
                Campaign.account_id == binding.account_id,
                Campaign.venue == binding.venue,
            )
            .order_by(OrderIntent.updated_at.desc(), OrderIntent.intent_id)
        ).all()
        if len(candidates) == 1:
            return candidates[0]

        entry_event = message.event_type in {"entry", "entry_fill", "entry_cancel"}
        exit_event = message.event_type in {"exit", "exit_fill", "exit_cancel"}
        phase_kinds = (
            {IntentKind.INITIAL.value, IntentKind.ADD.value}
            if entry_event
            else ({IntentKind.REDUCE.value, IntentKind.EXIT.value} if exit_event else None)
        )
        active_statuses = {
            OrderIntentStatus.DISPATCHING.value,
            OrderIntentStatus.SENT.value,
            OrderIntentStatus.PARTIALLY_FILLED.value,
            OrderIntentStatus.UNKNOWN.value,
        }
        active = [
            item
            for item in candidates
            if item.status in active_statuses and (phase_kinds is None or item.kind in phase_kinds)
        ]
        if len(active) == 1:
            return active[0]

        if message.event_type in {"protection_trigger", "protection_trigger_global"}:
            tagged_id = self._tagged_intent_id(trade.enter_tag)
            matches = [item for item in candidates if item.intent_id == tagged_id]
            if len(matches) == 1:
                return matches[0]
        return None

    def record_freqtrade_rpc_event(
        self,
        binding: PreparedFreqtradeWorkerBinding,
        message: FreqtradeRpcMessage,
        trade: FreqtradeTrade | None,
        *,
        now: datetime,
    ) -> str:
        """Idempotently bind an RPC notification to an existing durable dispatch.

        RPC is lifecycle telemetry, not an account-fact source.  A transaction
        that cannot be joined to an approved dispatch blocks that account until
        reconciliation; it never creates an intent.
        """

        if binding.service_principal_id is None:
            _reject(
                "FREQTRADE_RUNTIME_BINDING_INVALID",
                "Freqtrade RPC requires an exact runtime service principal",
            )
        idempotency_key = message.idempotency_key
        with self.database.session_factory.begin() as session:
            account = session.get(
                ExchangeAccount,
                binding.exchange_account_id,
                with_for_update=True,
            )
            team = None if account is None else session.get(Team, account.team_id)
            if (
                account is None
                or team is None
                or account.team_id != binding.team_id
                or team.workspace_id != binding.workspace_id
                or account.account_id != binding.account_id
                or account.venue != binding.venue
                or account.environment != binding.environment
                or account.freqtrade_worker_name != binding.worker_name
                or account.freqtrade_worker_url != binding.worker_url
                or account.freqtrade_worker_mode != binding.worker_mode
                or account.freqtrade_worker_status != "VERIFIED"
                or account.freqtrade_auth_version != binding.auth_version
                or account.runtime_service_principal_id != binding.service_principal_id
            ):
                _reject(
                    "FREQTRADE_WORKER_BINDING_CHANGED",
                    "the exact account-bound Freqtrade RPC binding changed",
                )
            self._require_exact_runtime_principal(
                session,
                principal_id=binding.service_principal_id,
                team=team,
                role=Role.OPERATOR,
                account_id=account.account_id,
                venue=account.venue,
                error_code="FREQTRADE_RUNTIME_BINDING_INVALID",
                error_message="the Freqtrade RPC principal is outside its exact account scope",
            )
            caller = f"freqtrade-rpc:{binding.exchange_account_id}:{binding.auth_version}"
            payload = {
                "event_type": message.event_type,
                "message_hash": idempotency_key,
                "trade_id": None if trade is None else trade.trade_id,
            }
            digest, replay = self.transactions._idempotency(
                session,
                caller_id=caller,
                operation="freqtrade.rpc.observe",
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if replay is not None:
                return str(replay["status"])

            intent: OrderIntent | None = None
            if trade is not None:
                intent = self._freqtrade_rpc_intent(session, binding, message, trade)

            campaign = None if intent is None else session.get(Campaign, intent.campaign_id)
            controlled = bool(
                intent is not None
                and campaign is not None
                and intent.dispatch_backend == "FREQTRADE"
                and intent.dispatch_account_version == binding.account_version
                and intent.dispatch_auth_version == binding.auth_version
                and campaign.team_id == binding.team_id
                and campaign.account_id == binding.account_id
                and campaign.venue == binding.venue
                and campaign.environment == binding.environment
            )
            passive = message.event_type in self._PASSIVE_FREQTRADE_RPC_EVENTS
            if not passive and not controlled:
                account.trading_status = "BLOCKED"
                account.version += 1
                account.updated_by = binding.service_principal_id
                account.updated_at = now
                status = "EXTERNAL_UNBOUND"
                event_type = "FREQTRADE_RPC_EXTERNAL_UNBOUND"
                object_type = "ExchangeAccount"
                object_id = account.exchange_account_id
                object_version = account.version
            else:
                status = "PASSIVE" if passive else "CONTROLLED"
                event_type = "FREQTRADE_RPC_OBSERVED"
                object_type = "ExchangeAccount" if intent is None else "OrderIntent"
                object_id = account.exchange_account_id if intent is None else intent.intent_id
                object_version = account.version if intent is None else intent.version
                if intent is not None and trade is not None and intent.dispatch_external_id is None:
                    intent.dispatch_external_id = trade.trade_id
                    intent.updated_at = now
                    intent.version += 1
                    object_version = intent.version
                if intent is not None and message.event_type in {"entry_cancel", "exit_cancel"}:
                    previous = intent.status
                    intent.status = OrderIntentStatus.UNKNOWN.value
                    intent.updated_at = now
                    intent.version += 1
                    object_version = intent.version
                    if intent.reservation_id is not None:
                        reservation = session.get(
                            RiskReservation,
                            intent.reservation_id,
                            with_for_update=True,
                        )
                        if reservation is not None:
                            reservation.status = ReservationStatus.UNKNOWN.value
                            reservation.updated_at = now
                            reservation.version += 1
                    assert campaign is not None
                    campaign.status = CampaignStatus.UNKNOWN.value
                    campaign.updated_at = now
                    if previous != intent.status:
                        INTENT_TRANSITIONS.labels(previous, intent.status).inc()
                    status = "CONTROLLED_UNKNOWN"

            response = {"status": status}
            self.transactions._save_receipt(
                session,
                caller_id=caller,
                operation="freqtrade.rpc.observe",
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=response,
                now=now,
            )
            self.transactions._audit(
                session,
                actor_id=str(binding.service_principal_id),
                event_type=event_type,
                object_type=object_type,
                object_id=object_id,
                reason=(
                    f"event={message.event_type};status={status};"
                    f"trade={None if trade is None else trade.trade_id}"
                ),
                correlation_id=uuid4() if intent is None else intent.correlation_id,
                object_version=object_version,
                idempotency_key=idempotency_key,
                workspace_id=binding.workspace_id,
                team_id=binding.team_id,
                account_id=binding.account_id,
                now=now,
            )
            return status

    def _validate_freqtrade_intent_boundary(
        self,
        session: Session,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        now: datetime,
        allowed_statuses: set[str],
    ) -> tuple[OrderIntent, Campaign, Instrument]:
        intent = session.get(OrderIntent, intent_id)
        if intent is None:
            _reject("ORDER_INTENT_NOT_FOUND", "intent does not exist")
        campaign = session.get(Campaign, intent.campaign_id)
        if campaign is None:
            _reject("CAMPAIGN_NOT_FOUND", "intent campaign is missing")
        environment, account_id, venue = _scope_parts(execution_scope)
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
            or campaign.account_id != account_id
            or campaign.venue != venue
            or execution_scope
            != _scope_key(campaign.environment, campaign.account_id, campaign.venue)
        ):
            _reject("EXECUTION_SCOPE_MISMATCH", "sender scope does not match campaign scope")
        self.transactions._require_role(
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
            _reject("ORDER_INTENT_STATE_INVALID", "intent state does not allow execution")
        if intent.status == OrderIntentStatus.FILLED.value and intent.updated_at < now - timedelta(
            hours=24
        ):
            _reject("ORDER_INTENT_STATE_INVALID", "filled intent replay window has expired")
        instrument = session.get(Instrument, campaign.instrument_id)
        if instrument is None or not instrument.active or instrument.venue != venue:
            _reject("INSTRUMENT_UNAVAILABLE", "campaign instrument is unavailable")
        if intent.quantity % instrument.lot_size != 0:
            _reject("INSTRUMENT_QUANTITY_INVALID", "intent quantity is not aligned to lot size")
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
                _reject("AUTHORIZATION_EXPIRED", "new-risk authorization is no longer valid")
            if intent.kind == IntentKind.ADD.value and authorization.add_revoked_at is not None:
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
                _reject("KILL_SWITCH", "new-risk execution is blocked")
            if state is SystemRiskState.REDUCE_ONLY:
                _reject("REDUCE_ONLY", "new-risk execution is blocked")
            if state is SystemRiskState.NO_PYRAMID and intent.kind == IntentKind.ADD.value:
                _reject("PYRAMID_DISABLED", "Add execution is blocked")
            position = find_position_for_scope(session, campaign)
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
                _reject("POSITION_UNKNOWN", "new-risk execution requires a fresh position")
            if (
                equity is None
                or equity.fact_status != FactStatus.KNOWN.value
                or self._fact_is_stale(equity.observed_at, now, max_age)
            ):
                _reject("EQUITY_UNKNOWN", "new-risk execution requires fresh equity")
            source_health = session.scalar(
                select(RuntimeSourceHealth).where(
                    RuntimeSourceHealth.team_id == campaign.team_id,
                    RuntimeSourceHealth.source_name == campaign.venue,
                    RuntimeSourceHealth.environment == campaign.environment,
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
                    "new-risk execution requires current account facts",
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
                    "new-risk execution requires fresh total managed capital",
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
                    "current reservations exceed managed risk capacity",
                )
            if intent.kind == IntentKind.INITIAL.value and position.quantity != 0:
                _reject("POSITION_NOT_FLAT", "INITIAL execution requires a flat position")
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
                    _reject("PROTECTION_UNKNOWN", "Add execution requires current protection")
            if (
                session.scalar(
                    select(RiskReservation.reservation_id).where(
                        RiskReservation.team_id == campaign.team_id,
                        RiskReservation.status == ReservationStatus.UNKNOWN.value,
                    )
                )
                is not None
            ):
                _reject("RISK_RESERVATION_UNKNOWN", "unresolved risk blocks new execution")
            notional = intent.quantity * position.mark_price * instrument.contract_multiplier
            if notional < instrument.minimum_notional:
                _reject("MINIMUM_NOTIONAL", "intent is below current instrument minimum notional")
        return intent, campaign, instrument

    def prepare_freqtrade_order(
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
        with self.database.session_factory() as session:
            intent, campaign, instrument = self._validate_freqtrade_intent_boundary(
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
            )
            pair = freqtrade_pair(campaign.venue, instrument.symbol, hip3_dexes=hip3_dexes)
            client_order_id = f"tcp-{intent.intent_id.hex[:28]}"
            if intent.reduce_only:
                return FreqtradeExitCommand(
                    pair=pair,
                    max_quantity=intent.quantity,
                    client_order_id=client_order_id,
                    close_all=intent.kind == IntentKind.EXIT.value,
                )
            position = find_position_for_scope(session, campaign)
            if position is None or position.mark_price <= 0:
                _reject("POSITION_UNKNOWN", "Freqtrade entry requires a current mark price")
            requested_notional = (
                intent.quantity * position.mark_price * instrument.contract_multiplier
            )
            stake_amount = requested_notional * Decimal("0.98") / leverage
            if stake_amount <= 0:
                _reject("FREQTRADE_ORDER_INVALID", "Freqtrade stake amount is invalid")
            return FreqtradeEntryCommand(
                pair=pair,
                side="long" if intent.side == "BUY" else "short",
                stake_amount=stake_amount,
                max_quantity=intent.quantity,
                leverage=leverage,
                enter_tag=f"tcp-{intent.intent_id.hex}",
                client_order_id=client_order_id,
                position_adjustment=intent.kind == IntentKind.ADD.value,
            )

    def record_freqtrade_order(
        self,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        command: FreqtradeEntryCommand | FreqtradeExitCommand,
        trade: FreqtradeTrade,
        *,
        dispatch_started_at: datetime,
        now: datetime,
    ) -> UUID:
        environment, account_id, venue = _scope_parts(execution_scope)
        with self.database.session_factory.begin() as session:
            intent = session.get(OrderIntent, intent_id, with_for_update=True)
            campaign = None if intent is None else session.get(Campaign, intent.campaign_id)
            if intent is None or campaign is None:
                _reject("ORDER_INTENT_NOT_FOUND", "Freqtrade intent campaign is unavailable")
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
                or campaign.account_id != account_id
                or campaign.venue != venue
                or execution_scope
                != _scope_key(campaign.environment, campaign.account_id, campaign.venue)
            ):
                _reject("EXECUTION_SCOPE_MISMATCH", "Freqtrade result is outside intent scope")
            self.transactions._require_role(
                session,
                actor_id,
                "venue.record",
                campaign.account_id,
                campaign.venue,
                team_id=campaign.team_id,
            )
            if trade.pair != command.pair:
                _reject(
                    "FREQTRADE_ORDER_IDENTITY_CONFLICT",
                    "Freqtrade trade changed the frozen pair boundary",
                )
            execution_order = freqtrade_execution_order(
                trade,
                command,
                dispatch_started_at=dispatch_started_at,
            )
            if isinstance(command, FreqtradeEntryCommand):
                if (
                    intent.reduce_only
                    or trade.side != command.side
                    or not trade.is_open
                    or (
                        not command.position_adjustment
                        and trade.enter_tag != command.enter_tag
                    )
                ):
                    _reject(
                        "FREQTRADE_ORDER_IDENTITY_CONFLICT",
                        "Freqtrade entry changed the frozen direction or identity",
                    )
            else:
                if (
                    not intent.reduce_only
                    or command.close_all != (intent.kind == IntentKind.EXIT.value)
                    or (command.close_all and trade.is_open)
                    or (not command.close_all and not trade.is_open)
                ):
                    _reject(
                        "FREQTRADE_ORDER_IDENTITY_CONFLICT",
                        "Freqtrade exit changed the frozen full or partial reduction semantics",
                    )
            native_order_id = execution_order.order_id
            filled_quantity = execution_order.filled
            order_status = (
                VenueOrderStatus.FILLED.value
                if filled_quantity == intent.quantity
                else VenueOrderStatus.PARTIALLY_FILLED.value
            )
            intent_status = (
                OrderIntentStatus.FILLED.value
                if filled_quantity == intent.quantity
                else OrderIntentStatus.PARTIALLY_FILLED.value
            )
            observed_at = execution_order.filled_at or trade.observed_at
            if intent.dispatch_backend != "FREQTRADE":
                _reject(
                    "FREQTRADE_DISPATCH_SNAPSHOT_MISSING",
                    "Freqtrade result requires a durable dispatch snapshot",
                )
            if (
                intent.dispatch_external_id is not None
                and intent.dispatch_external_id != trade.trade_id
            ):
                _reject(
                    "FREQTRADE_DISPATCH_SNAPSHOT_CHANGED",
                    "Freqtrade result changed the persisted trade identity",
                )
            intent.dispatch_external_id = trade.trade_id
            side = intent.side
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
                    venue_order_id=native_order_id,
                    client_order_id=command.client_order_id,
                    side=side,
                    order_type="MARKET",
                    reduce_only=intent.reduce_only,
                    status=order_status,
                    ordered_quantity=intent.quantity,
                    filled_quantity=filled_quantity,
                    observed_at=observed_at,
                    updated_at=now,
                )
                session.add(fact)
            elif (
                fact.client_order_id != command.client_order_id
                or fact.side != side
                or fact.order_type != "MARKET"
                or fact.reduce_only != intent.reduce_only
            ):
                _reject(
                    "FREQTRADE_ORDER_IDENTITY_CONFLICT",
                    "persisted Freqtrade order identity changed semantics",
                )
            else:
                fact.venue_order_id = native_order_id
                fact.status = order_status
                fact.ordered_quantity = intent.quantity
                fact.filled_quantity = filled_quantity
                fact.observed_at = observed_at
                fact.updated_at = now
            if filled_quantity > 0:
                self._consume_add_unit(session, intent)
            previous = intent.status
            intent.status = intent_status
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
                CampaignStatus.OPEN.value
                if not intent.reduce_only
                else (
                    CampaignStatus.CLOSING.value
                    if intent.kind == IntentKind.EXIT.value
                    else CampaignStatus.REDUCING.value
                )
            )
            campaign.updated_at = now
            session.flush()
            self.transactions._audit(
                session,
                actor_id=str(actor_id),
                event_type="FREQTRADE_ORDER_OBSERVED",
                object_type="VenueOrder",
                object_id=fact.venue_order_fact_id,
                reason=f"trade={trade.trade_id};order={native_order_id};status={order_status}",
                correlation_id=intent.correlation_id,
                object_version=intent.version,
                now=now,
            )
            if previous != intent.status:
                INTENT_TRANSITIONS.labels(previous, intent.status).inc()
            return fact.venue_order_fact_id

    def record_freqtrade_unknown(
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
        environment, account_id, venue = _scope_parts(execution_scope)
        with self.database.session_factory.begin() as session:
            intent = session.get(OrderIntent, intent_id, with_for_update=True)
            campaign = None if intent is None else session.get(Campaign, intent.campaign_id)
            if intent is None or campaign is None:
                _reject("ORDER_INTENT_NOT_FOUND", "Freqtrade intent campaign is unavailable")
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
                or campaign.account_id != account_id
                or campaign.venue != venue
                or execution_scope
                != _scope_key(campaign.environment, campaign.account_id, campaign.venue)
            ):
                _reject("EXECUTION_SCOPE_MISMATCH", "sender scope does not match campaign scope")
            self.transactions._require_role(
                session,
                actor_id,
                "venue.record",
                campaign.account_id,
                campaign.venue,
                team_id=campaign.team_id,
            )
            if intent.status == OrderIntentStatus.UNKNOWN.value:
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
                    side=intent.side,
                    order_type="MARKET",
                    reduce_only=intent.reduce_only,
                    status=VenueOrderStatus.UNKNOWN.value,
                    ordered_quantity=command.max_quantity,
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
                    RiskReservation,
                    intent.reservation_id,
                    with_for_update=True,
                )
                if reservation is not None:
                    reservation.status = ReservationStatus.UNKNOWN.value
                    reservation.updated_at = now
                    reservation.version += 1
            campaign.status = CampaignStatus.UNKNOWN.value
            campaign.updated_at = now
            self.transactions._audit(
                session,
                actor_id=str(actor_id),
                event_type="FREQTRADE_OUTCOME_UNKNOWN",
                object_type="OrderIntent",
                object_id=intent.intent_id,
                reason=reason,
                correlation_id=intent.correlation_id,
                object_version=intent.version,
                now=now,
            )
            if previous != intent.status:
                INTENT_TRANSITIONS.labels(previous, intent.status).inc()

    def record_freqtrade_protection(
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
        stop_order = freqtrade_active_stop_order(trade)
        environment, account_id, venue = _scope_parts(execution_scope)
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
                or campaign.account_id != account_id
                or campaign.venue != venue
                or execution_scope
                != _scope_key(campaign.environment, campaign.account_id, campaign.venue)
            ):
                _reject("EXECUTION_SCOPE_MISMATCH", "protection is outside campaign scope")
            self.transactions._require_role(
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
            position = find_position_for_scope(session, campaign, for_update=True)
            if position is None:
                _reject("POSITION_UNKNOWN", "protection requires an account position fact")
            protection = session.scalar(
                select(ProtectionOrder)
                .where(ProtectionOrder.position_id == position.position_id)
                .with_for_update()
            )
            order = session.scalar(
                select(VenueOrder)
                .where(
                    VenueOrder.team_id == campaign.team_id,
                    VenueOrder.environment == campaign.environment,
                    VenueOrder.account_id == campaign.account_id,
                    VenueOrder.venue == campaign.venue,
                    VenueOrder.venue_order_id == trade.stoploss_order_id,
                )
                .with_for_update()
            )
            client_order_id = f"ftp-{position.position_id.hex[:28]}"
            if order is None and protection is not None:
                order = session.scalar(
                    select(VenueOrder)
                    .where(
                        VenueOrder.team_id == campaign.team_id,
                        VenueOrder.environment == campaign.environment,
                        VenueOrder.account_id == campaign.account_id,
                        VenueOrder.venue == campaign.venue,
                        VenueOrder.venue_order_id == protection.venue_order_id,
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
                        VenueOrder.client_order_id == client_order_id,
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
                    venue_order_id=trade.stoploss_order_id,
                    client_order_id=client_order_id,
                    side="SELL" if trade.side == "long" else "BUY",
                    order_type="STOPLOSS",
                    reduce_only=True,
                    status=VenueOrderStatus.SENT.value,
                    ordered_quantity=stop_order.amount,
                    filled_quantity=Decimal(0),
                    observed_at=trade.observed_at,
                    updated_at=now,
                )
                session.add(order)
            elif (
                order.instrument_id != campaign.instrument_id
                or not order.reduce_only
                or order.order_type != "STOPLOSS"
            ):
                _reject(
                    "FREQTRADE_PROTECTION_IDENTITY_CONFLICT",
                    "existing protection changed Freqtrade order identity",
                )
            else:
                order.venue_order_id = trade.stoploss_order_id
                order.status = VenueOrderStatus.SENT.value
                order.ordered_quantity = stop_order.amount
                order.filled_quantity = Decimal(0)
                order.observed_at = trade.observed_at
                order.updated_at = now
            if protection is None:
                protection = ProtectionOrder(
                    position_id=position.position_id,
                    venue_order_id=trade.stoploss_order_id,
                    quantity=stop_order.amount,
                    trigger_price=trade.stop_loss_abs,
                    status=ProtectionStatus.ACTIVE.value,
                    fully_covered=stop_order.amount >= abs(position.quantity),
                    observed_at=trade.observed_at,
                    updated_at=now,
                )
                session.add(protection)
            else:
                protection.venue_order_id = trade.stoploss_order_id
                protection.quantity = stop_order.amount
                protection.trigger_price = trade.stop_loss_abs
                protection.status = ProtectionStatus.ACTIVE.value
                protection.fully_covered = stop_order.amount >= abs(position.quantity)
                protection.observed_at = trade.observed_at
                protection.updated_at = now
            session.flush()
            self.transactions._audit(
                session,
                actor_id=str(actor_id),
                event_type="FREQTRADE_PROTECTION_OBSERVED",
                object_type="ProtectionOrder",
                object_id=protection.protection_id,
                reason=f"trade={trade.trade_id};order={trade.stoploss_order_id}",
                correlation_id=uuid4(),
                object_version=1,
                now=now,
            )
            return protection.protection_id

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
            self.transactions._require_role(
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
            policy = session.scalar(
                select(RiskPolicy).where(
                    RiskPolicy.team_id == campaign.team_id,
                    RiskPolicy.active,
                )
            )
            position = find_position_for_scope(session, campaign, for_update=True)
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
                self.transactions._audit(
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
            self.transactions._audit(
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
