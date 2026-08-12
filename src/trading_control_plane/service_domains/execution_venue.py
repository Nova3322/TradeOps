from __future__ import annotations

from trading_control_plane.service_component import ServiceComponent

# The domain implementation intentionally consumes the explicit service_core export surface.
# ruff: noqa: F403, F405
from trading_control_plane.service_core import *


class VenueCommandExecutionService(ServiceComponent):
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
        self.facade._validate_sender(
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
            self.facade._require_exchange_account_live_ready(
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
                or self.facade._fact_is_stale(position.observed_at, now, max_age)
            ):
                _reject("POSITION_UNKNOWN", "new-risk venue send requires a fresh position")
            if (
                equity is None
                or equity.fact_status != FactStatus.KNOWN.value
                or self.facade._fact_is_stale(equity.observed_at, now, max_age)
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
                    or self.facade._fact_is_stale(source_health.checked_at, now, max_age)
                ):
                    _reject(
                        "READ_ONLY_SOURCE_UNAVAILABLE",
                        "new-risk venue send requires a current successful read-only probe",
                    )
            capital_known, managed_capital_usd, _, _ = self.facade._managed_capital_context(
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
            occupied_risk = self.facade._occupied_risk(session, campaign.team_id)
            occupied_account_risk = self.facade._occupied_risk(
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
                    or self.facade._fact_is_stale(protection.observed_at, now, max_age)
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
            self.facade._validate_sender(
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
            self.transactions._require_role(
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
                self.facade._consume_add_unit(session, intent)
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
            self.transactions._audit(
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
            self.facade._validate_sender(
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
            self.transactions._require_role(
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
            self.transactions._audit(
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
            self.facade._validate_sender(
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
                self.facade._require_exchange_account_live_ready(
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
                or self.facade._fact_is_stale(
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
            self.facade._validate_sender(
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
            self.transactions._require_role(
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
            self.transactions._audit(
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
            self.facade._validate_sender(
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
            self.transactions._require_role(
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
            self.facade._require_exchange_account_live_ready(
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
            self.facade._validate_sender(
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
            self.transactions._require_role(
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
            self.transactions._audit(
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
