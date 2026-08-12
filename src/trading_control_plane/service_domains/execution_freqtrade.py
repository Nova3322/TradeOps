from __future__ import annotations

from trading_control_plane.service_component import ServiceComponent

# The domain implementation intentionally consumes the explicit service_core export surface.
# ruff: noqa: F403, F405
from trading_control_plane.service_core import *


class FreqtradeRecoveryExecutionService(ServiceComponent):
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
            base = self.facade._binance_testnet_command(
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
        fact_id = self.facade.record_binance_testnet_order(
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
        self.facade.record_position(
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
        self.facade.record_binance_testnet_unknown(
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
        command = self.facade.prepare_binance_testnet_protection(
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
        return self.facade.record_binance_testnet_protection(
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
                or self.facade._fact_is_stale(
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
