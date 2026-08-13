from __future__ import annotations

from decimal import ROUND_FLOOR

from trading_control_plane.domain import MONEY_QUANTUM
from trading_control_plane.service_component import ServiceComponent

# The domain implementation intentionally consumes the explicit service_core export surface.
# ruff: noqa: F403, F405
from trading_control_plane.service_core import *

SHADOW_INITIAL_EQUITY = Decimal("100000")
SHADOW_PRICE_MAX_AGE = timedelta(seconds=60)
SHADOW_PRICE_FUTURE_SKEW = timedelta(seconds=30)


class TradingModeService(ServiceComponent):
    """Team-wide trading mode and the internal-only deterministic SHADOW ledger."""

    @staticmethod
    def _require_human_request() -> None:
        if current_api_client_context() is not None:
            _reject(
                "HUMAN_WEB_CONFIRMATION_REQUIRED",
                "trading mode and virtual capital changes require an interactive web session",
            )

    @staticmethod
    def _account_payload(account: TeamShadowAccount | None) -> dict[str, Any] | None:
        if account is None:
            return None
        return {
            "shadow_account_id": str(account.shadow_account_id),
            "generation": account.generation,
            "initial_equity": str(account.initial_equity),
            "equity": str(account.equity),
            "available_balance": str(account.available_balance),
            "realized_pnl": str(account.realized_pnl),
            "unrealized_pnl": str(account.unrealized_pnl),
            "fees_paid": str(account.fees_paid),
            "status": account.status,
            "version": account.version,
            "created_at": account.created_at.isoformat(),
            "updated_at": account.updated_at.isoformat(),
        }

    @staticmethod
    def _order_payload(order: ShadowOrder, fill: ShadowFill | None = None) -> dict[str, Any]:
        result = {
            "shadow_order_id": str(order.shadow_order_id),
            "team_id": str(order.team_id),
            "generation": order.generation,
            "shadow_instrument_id": str(order.shadow_instrument_id),
            "shadow_position_id": (
                None if order.shadow_position_id is None else str(order.shadow_position_id)
            ),
            "account_id": order.source_account_id,
            "venue": order.venue,
            "environment": ExecutionEnvironment.SHADOW.value,
            "side": order.side,
            "order_type": order.order_type,
            "quantity": str(order.quantity),
            "limit_price": None if order.limit_price is None else str(order.limit_price),
            "trigger_price": None if order.trigger_price is None else str(order.trigger_price),
            "trigger_type": order.trigger_type,
            "execution_type": order.execution_type,
            "reduce_only": order.reduce_only,
            "status": order.status,
            "filled_quantity": str(order.filled_quantity),
            "fill_price": None if order.fill_price is None else str(order.fill_price),
            "fee": str(order.fee),
            "realized_pnl": str(order.realized_pnl),
            "version": order.version,
        }
        if fill is not None:
            result["fill"] = {
                "shadow_fill_id": str(fill.shadow_fill_id),
                "quantity": str(fill.quantity),
                "price": str(fill.price),
                "notional": str(fill.notional),
                "fee": str(fill.fee),
                "realized_pnl": str(fill.realized_pnl),
                "executed_at": fill.executed_at.isoformat(),
            }
        return result

    @staticmethod
    def _position_payload(position: ShadowPosition, symbol: str) -> dict[str, Any]:
        return {
            "shadow_position_id": str(position.shadow_position_id),
            "generation": position.generation,
            "account_id": position.source_account_id,
            "venue": position.venue,
            "symbol": symbol,
            "quantity": str(position.quantity),
            "average_entry_price": str(position.average_entry_price),
            "mark_price": str(position.mark_price),
            "realized_pnl": str(position.realized_pnl),
            "unrealized_pnl": str(position.unrealized_pnl),
            "status": position.status,
            "version": position.version,
        }

    @staticmethod
    def _active_shadow_account(
        session: Session,
        team_id: UUID,
        *,
        lock: bool,
    ) -> TeamShadowAccount | None:
        statement = select(TeamShadowAccount).where(
            TeamShadowAccount.team_id == team_id,
            TeamShadowAccount.status == "ACTIVE",
        )
        if lock:
            statement = statement.with_for_update()
        return session.scalar(statement)

    def _create_shadow_account(
        self,
        session: Session,
        *,
        team: Team,
        generation: int,
        actor_id: UUID,
        correlation_id: UUID,
        idempotency_key: str,
        event_type: str,
        now: datetime,
    ) -> TeamShadowAccount:
        account = TeamShadowAccount(
            team_id=team.team_id,
            generation=generation,
            initial_equity=SHADOW_INITIAL_EQUITY,
            equity=SHADOW_INITIAL_EQUITY,
            available_balance=SHADOW_INITIAL_EQUITY,
            realized_pnl=Decimal(0),
            unrealized_pnl=Decimal(0),
            fees_paid=Decimal(0),
            status="ACTIVE",
            version=1,
            created_at=now,
            updated_at=now,
        )
        session.add(account)
        session.flush()
        self._record_shadow_equity_snapshot(session, account=account, now=now)
        self.transactions._audit(
            session,
            actor_id=str(actor_id),
            event_type=event_type,
            object_type="TeamShadowAccount",
            object_id=account.shadow_account_id,
            reason=f"generation={generation};initial_equity={SHADOW_INITIAL_EQUITY}",
            correlation_id=correlation_id,
            object_version=account.version,
            idempotency_key=idempotency_key,
            workspace_id=team.workspace_id,
            team_id=team.team_id,
            environment=ExecutionEnvironment.SHADOW.value,
            generation=generation,
            rule_summary={
                "initial_equity": str(SHADOW_INITIAL_EQUITY),
                "capital_unit": "USDT_EQUIVALENT",
                "status": "ACTIVE",
            },
            now=now,
        )
        return account

    @staticmethod
    def _record_shadow_equity_snapshot(
        session: Session,
        *,
        account: TeamShadowAccount,
        now: datetime,
    ) -> None:
        session.add(
            AnalyticsEquitySnapshot(
                team_id=account.team_id,
                environment=ExecutionEnvironment.SHADOW.value,
                account_id="TEAM_SHADOW",
                venue="TRADINGOPS",
                generation=account.generation,
                equity=account.equity,
                currency="U",
                source_kind="TEAM_SHADOW_ACCOUNT",
                source_id=f"{account.shadow_account_id}:{account.version}",
                version=account.version,
                fact_metadata={
                    "shadow_account_id": str(account.shadow_account_id),
                    "initial_equity": str(account.initial_equity),
                    "realized_pnl": str(account.realized_pnl),
                    "unrealized_pnl": str(account.unrealized_pnl),
                    "fees_paid": str(account.fees_paid),
                },
                observed_at=now,
                recorded_at=now,
            )
        )

    @staticmethod
    def _shadow_mode_blockers(session: Session, team_id: UUID) -> list[dict[str, Any]]:
        blockers: list[dict[str, Any]] = []
        active_intents = session.scalar(
            select(func.count(OrderIntent.intent_id))
            .join(Campaign, Campaign.campaign_id == OrderIntent.campaign_id)
            .where(
                Campaign.team_id == team_id,
                Campaign.environment == ExecutionEnvironment.LIVE.value,
                OrderIntent.status.in_(ACTIVE_INTENT_STATUSES),
            )
        )
        if active_intents:
            blockers.append({"code": "LIVE_REQUESTS_ACTIVE", "count": int(active_intents)})
        open_orders = session.scalar(
            select(func.count(VenueOrder.venue_order_fact_id)).where(
                VenueOrder.team_id == team_id,
                VenueOrder.environment == ExecutionEnvironment.LIVE.value,
                VenueOrder.status.in_(
                    (
                        VenueOrderStatus.SENT.value,
                        VenueOrderStatus.PARTIALLY_FILLED.value,
                        VenueOrderStatus.UNKNOWN.value,
                    )
                ),
            )
        )
        if open_orders:
            blockers.append({"code": "LIVE_ORDERS_OPEN", "count": int(open_orders)})
        open_positions = session.scalar(
            select(func.count(Position.position_id)).where(
                Position.team_id == team_id,
                Position.environment == ExecutionEnvironment.LIVE.value,
                (Position.quantity != 0) | (Position.fact_status == FactStatus.UNKNOWN.value),
            )
        )
        if open_positions:
            blockers.append({"code": "LIVE_POSITIONS_OPEN", "count": int(open_positions)})
        return blockers

    def trading_mode_status(self, *, actor_id: UUID, now: datetime) -> dict[str, Any]:
        with self.database.session_factory() as session:
            team = self.transactions._require_role(
                session, actor_id, "venue.view", allow_setup=True
            )
            account = self._active_shadow_account(session, team.team_id, lock=False)
            exchange_accounts = session.scalars(
                select(ExchangeAccount)
                .where(ExchangeAccount.team_id == team.team_id, ExchangeAccount.active)
                .order_by(ExchangeAccount.venue, ExchangeAccount.label)
            ).all()
            position_count = 0
            open_order_count = 0
            positions: list[dict[str, Any]] = []
            orders: list[dict[str, Any]] = []
            if account is not None:
                position_rows = session.execute(
                    select(ShadowPosition, ShadowInstrument)
                    .join(
                        ShadowInstrument,
                        ShadowInstrument.shadow_instrument_id
                        == ShadowPosition.shadow_instrument_id,
                    )
                    .where(
                        ShadowPosition.shadow_account_id == account.shadow_account_id,
                        ShadowPosition.status == "OPEN",
                        ShadowPosition.quantity != 0,
                    )
                    .order_by(ShadowInstrument.venue, ShadowInstrument.symbol)
                ).all()
                positions = [
                    self._position_payload(position, instrument.symbol)
                    for position, instrument in position_rows
                ]
                position_count = len(positions)
                open_order_count = int(
                    session.scalar(
                        select(func.count(ShadowOrder.shadow_order_id)).where(
                            ShadowOrder.shadow_account_id == account.shadow_account_id,
                            ShadowOrder.status.in_(("OPEN", "TRIGGERED")),
                        )
                    )
                    or 0
                )
                order_rows = session.execute(
                    select(ShadowOrder, ShadowInstrument)
                    .join(
                        ShadowInstrument,
                        ShadowInstrument.shadow_instrument_id
                        == ShadowOrder.shadow_instrument_id,
                    )
                    .where(
                        ShadowOrder.shadow_account_id == account.shadow_account_id,
                        ShadowOrder.status.in_(("OPEN", "TRIGGERED")),
                    )
                    .order_by(ShadowOrder.created_at.desc())
                ).all()
                orders = [
                    {**self._order_payload(order), "symbol": instrument.symbol}
                    for order, instrument in order_rows
                ]
            gates = {
                row.capability_key: row.status
                for row in session.scalars(
                    select(CapabilityGate).where(
                        CapabilityGate.capability_key.in_(
                            ("LIVE_ORDER_SEND", "CAPITAL_TRANSFER", "AUTO_ADD")
                        )
                    )
                ).all()
            }
            return {
                "workspace_id": str(team.workspace_id),
                "team_id": str(team.team_id),
                "team_name": team.name,
                "execution_mode": team.execution_mode,
                "version": team.version,
                "trading_enabled": team.trading_enabled,
                "shadow_account": self._account_payload(account),
                "position_count": position_count,
                "open_order_count": open_order_count,
                "positions": positions,
                "orders": orders,
                "accounts": [
                    {
                        "account_id": row.account_id,
                        "venue": row.venue,
                        "label": row.label,
                        "connection_status": row.connection_status,
                        "trading_status": row.trading_status,
                    }
                    for row in exchange_accounts
                ],
                "dangerous_capabilities": gates,
                "safety_boundary": {
                    "shadow_is_internal_only": True,
                    "venue_write_adapters_used": False,
                    "live_history_converted": False,
                    "mode_does_not_enable_dangerous_capabilities": True,
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
        self._require_human_request()
        normalized_mode = mode.strip().upper()
        if normalized_mode not in {TeamExecutionMode.LIVE.value, TeamExecutionMode.SHADOW.value}:
            _reject("TEAM_MODE_INVALID", "ordinary users may select only LIVE or SHADOW")
        expected_confirmation = f"SWITCH_TO_{normalized_mode}"
        if confirmation != expected_confirmation:
            _reject(
                "SECOND_CONFIRMATION_REQUIRED",
                f"confirmation must exactly equal {expected_confirmation}",
            )
        blocked: list[dict[str, Any]] | None = None
        result: dict[str, Any] | None = None
        with self.database.session_factory.begin() as session:
            team = self.transactions._require_role(
                session,
                actor_id,
                "team.manage",
                team_id=team_id,
                allow_setup=True,
            )
            locked_team = session.scalar(
                select(Team).where(Team.team_id == team.team_id).with_for_update()
            )
            assert locked_team is not None
            team = locked_team
            operation = f"team.trading-mode:{team.team_id}"
            payload = {
                "mode": normalized_mode,
                "confirmation": confirmation,
                "expected_version": expected_version,
            }
            digest, replay = self.transactions._idempotency(
                session,
                caller_id=f"{actor_id}:{team.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if replay is not None:
                return replay
            if team.version != expected_version:
                _reject("VERSION_CONFLICT", "team version changed before mode switch")
            correlation_id = uuid4()
            if team.execution_mode == TeamExecutionMode.SETUP.value:
                setup_blockers = self.facade._shadow_activation_blockers(session, team)
                if setup_blockers:
                    blocked = [
                        {"code": code, "count": 1} for code in setup_blockers
                    ]
            elif normalized_mode == TeamExecutionMode.LIVE.value and not team.trading_enabled:
                _reject(
                    "TEAM_LIVE_SETUP_INCOMPLETE",
                    "team setup must be operational before selecting LIVE",
                )
            if normalized_mode == TeamExecutionMode.SHADOW.value and not blocked:
                blocked = self._shadow_mode_blockers(session, team.team_id)
            if blocked:
                self.transactions._audit(
                    session,
                    actor_id=str(actor_id),
                    event_type="TEAM_TRADING_MODE_BLOCKED",
                    object_type="Team",
                    object_id=team.team_id,
                    reason=";".join(
                        f"{item['code']}={item['count']}" for item in blocked
                    ),
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
                team.execution_mode = normalized_mode
                team.execution_mode_locked_at = now
                if previous_mode == TeamExecutionMode.SETUP.value:
                    team.trading_enabled = True
                team.version += 1
                team.updated_at = now
                account = self._active_shadow_account(session, team.team_id, lock=True)
                if normalized_mode == TeamExecutionMode.SHADOW.value and account is None:
                    latest_generation = session.scalar(
                        select(func.max(TeamShadowAccount.generation)).where(
                            TeamShadowAccount.team_id == team.team_id
                        )
                    )
                    account = self._create_shadow_account(
                        session,
                        team=team,
                        generation=int(latest_generation or 0) + 1,
                        actor_id=actor_id,
                        correlation_id=correlation_id,
                        idempotency_key=idempotency_key,
                        event_type="SHADOW_ACCOUNT_INITIALIZED",
                        now=now,
                    )
                result = {
                    "workspace_id": str(team.workspace_id),
                    "team_id": str(team.team_id),
                    "previous_mode": previous_mode,
                    "execution_mode": team.execution_mode,
                    "version": team.version,
                    "shadow_account": self._account_payload(account),
                    "dangerous_capabilities_changed": False,
                }
                self.transactions._save_receipt(
                    session,
                    caller_id=f"{actor_id}:{team.team_id}",
                    operation=operation,
                    idempotency_key=idempotency_key,
                    semantic_hash=digest,
                    response=result,
                    now=now,
                )
                self.transactions._audit(
                    session,
                    actor_id=str(actor_id),
                    event_type="TEAM_TRADING_MODE_CHANGED",
                    object_type="Team",
                    object_id=team.team_id,
                    reason=f"previous={previous_mode};current={normalized_mode}",
                    correlation_id=correlation_id,
                    object_version=team.version,
                    idempotency_key=idempotency_key,
                    workspace_id=team.workspace_id,
                    team_id=team.team_id,
                    environment=normalized_mode,
                    generation=None if account is None else account.generation,
                    rule_summary={
                        "previous_mode": previous_mode,
                        "current_mode": normalized_mode,
                        "dangerous_capabilities_changed": False,
                        "historical_environment_mutated": False,
                    },
                    now=now,
                )
        if blocked:
            detail = ", ".join(
                f"{item['code']}({item['count']})" for item in blocked
            )
            _reject("TEAM_MODE_SHADOW_BLOCKED", detail)
        assert result is not None
        return result

    @staticmethod
    def _require_active_exchange_account(
        session: Session,
        *,
        team_id: UUID,
        account_id: str,
        venue: str,
        lock: bool = False,
    ) -> ExchangeAccount:
        statement = select(ExchangeAccount).where(
            ExchangeAccount.team_id == team_id,
            ExchangeAccount.environment == "SHADOW",
            ExchangeAccount.account_id == account_id,
            ExchangeAccount.venue == venue,
            ExchangeAccount.active,
        )
        if lock:
            statement = statement.with_for_update()
        account = session.scalar(statement)
        if account is None:
            _reject(
                "EXCHANGE_ACCOUNT_NOT_FOUND",
                "source account is not active in the current Team",
            )
        return account

    @staticmethod
    def _validate_market_facts(
        *,
        latest_price: Decimal | None,
        observed_at: datetime | None,
        price_tick: Decimal | None,
        quantity_step: Decimal | None,
        contract_multiplier: Decimal | None,
        is_derivative: bool,
        now: datetime,
    ) -> None:
        if latest_price is None:
            _reject("SHADOW_PRICE_MISSING", "latest price is required")
        if not latest_price.is_finite() or latest_price <= 0:
            _reject("SHADOW_PRICE_INVALID", "latest price must be finite and positive")
        if observed_at is None:
            _reject("SHADOW_PRICE_TIMESTAMP_MISSING", "latest price timestamp is required")
        if observed_at < now - SHADOW_PRICE_MAX_AGE:
            _reject("SHADOW_PRICE_STALE", "latest price exceeds the 60 second freshness limit")
        if observed_at > now + SHADOW_PRICE_FUTURE_SKEW:
            _reject("SHADOW_PRICE_TIMESTAMP_INVALID", "latest price timestamp is in the future")
        if price_tick is None:
            _reject("SHADOW_PRICE_PRECISION_MISSING", "price precision is required")
        if not price_tick.is_finite() or price_tick <= 0:
            _reject("SHADOW_PRICE_PRECISION_INVALID", "price precision must be positive")
        if quantity_step is None:
            _reject("SHADOW_QUANTITY_PRECISION_MISSING", "quantity precision is required")
        if not quantity_step.is_finite() or quantity_step <= 0:
            _reject("SHADOW_QUANTITY_PRECISION_INVALID", "quantity precision must be positive")
        if contract_multiplier is None:
            _reject(
                "SHADOW_CONTRACT_MULTIPLIER_MISSING",
                "contract multiplier is required",
            )
        if not contract_multiplier.is_finite() or contract_multiplier <= 0:
            _reject(
                "SHADOW_CONTRACT_MULTIPLIER_INVALID",
                "contract multiplier must be positive",
            )
        if is_derivative and any(
            value is None for value in (price_tick, quantity_step, contract_multiplier)
        ):
            _reject(
                "SHADOW_DERIVATIVE_SPEC_MISSING",
                "derivative contract facts are incomplete",
            )

    def _upsert_instrument(
        self,
        session: Session,
        *,
        team_id: UUID,
        venue: str,
        symbol: str,
        catalog_instrument_id: UUID | None,
        price_tick: Decimal,
        quantity_step: Decimal,
        contract_multiplier: Decimal,
        is_derivative: bool,
        latest_price: Decimal,
        observed_at: datetime,
        now: datetime,
    ) -> ShadowInstrument:
        instrument = session.scalar(
            select(ShadowInstrument)
            .where(
                ShadowInstrument.team_id == team_id,
                ShadowInstrument.venue == venue,
                ShadowInstrument.symbol == symbol,
            )
            .with_for_update()
        )
        if instrument is None:
            instrument = ShadowInstrument(
                team_id=team_id,
                catalog_instrument_id=catalog_instrument_id,
                venue=venue,
                symbol=symbol,
                price_tick=price_tick,
                quantity_step=quantity_step,
                contract_multiplier=contract_multiplier,
                is_derivative=is_derivative,
                latest_price=latest_price,
                price_observed_at=observed_at,
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(instrument)
            session.flush()
        else:
            instrument.catalog_instrument_id = catalog_instrument_id
            instrument.price_tick = price_tick
            instrument.quantity_step = quantity_step
            instrument.contract_multiplier = contract_multiplier
            instrument.is_derivative = is_derivative
            instrument.latest_price = latest_price
            instrument.price_observed_at = observed_at
            instrument.version += 1
            instrument.updated_at = now
        return instrument

    def _resolve_instrument(
        self,
        session: Session,
        *,
        team_id: UUID,
        venue: str,
        symbol: str | None,
        catalog_instrument_id: UUID | None,
        latest_price: Decimal | None,
        observed_at: datetime | None,
        price_tick: Decimal | None,
        quantity_step: Decimal | None,
        contract_multiplier: Decimal | None,
        is_derivative: bool,
        now: datetime,
    ) -> ShadowInstrument:
        catalog = (
            None
            if catalog_instrument_id is None
            else session.get(Instrument, catalog_instrument_id)
        )
        if catalog_instrument_id is not None and (
            catalog is None or not catalog.active or catalog.venue != venue
        ):
            _reject("INSTRUMENT_UNAVAILABLE", "catalog instrument is not active for venue")
        normalized_symbol = (symbol or (None if catalog is None else catalog.symbol) or "").upper()
        if not normalized_symbol:
            _reject("SHADOW_SYMBOL_REQUIRED", "symbol or catalog instrument is required")
        resolved_tick = price_tick if price_tick is not None else (
            None if catalog is None else catalog.tick_size
        )
        resolved_step = quantity_step if quantity_step is not None else (
            None if catalog is None else catalog.lot_size
        )
        resolved_multiplier = contract_multiplier if contract_multiplier is not None else (
            None if catalog is None else catalog.contract_multiplier
        )
        self._validate_market_facts(
            latest_price=latest_price,
            observed_at=observed_at,
            price_tick=resolved_tick,
            quantity_step=resolved_step,
            contract_multiplier=resolved_multiplier,
            is_derivative=is_derivative,
            now=now,
        )
        assert latest_price is not None
        assert observed_at is not None
        assert resolved_tick is not None
        assert resolved_step is not None
        assert resolved_multiplier is not None
        return self._upsert_instrument(
            session,
            team_id=team_id,
            venue=venue,
            symbol=normalized_symbol,
            catalog_instrument_id=catalog_instrument_id,
            price_tick=resolved_tick,
            quantity_step=resolved_step,
            contract_multiplier=resolved_multiplier,
            is_derivative=is_derivative,
            latest_price=latest_price,
            observed_at=observed_at,
            now=now,
        )

    def _refresh_account(
        self,
        session: Session,
        *,
        account: TeamShadowAccount,
        now: datetime,
    ) -> None:
        rows = session.execute(
            select(ShadowPosition, ShadowInstrument)
            .join(
                ShadowInstrument,
                ShadowInstrument.shadow_instrument_id == ShadowPosition.shadow_instrument_id,
            )
            .where(
                ShadowPosition.shadow_account_id == account.shadow_account_id,
                ShadowPosition.status.in_(("OPEN", "CLOSED")),
            )
            .with_for_update()
        ).all()
        total_unrealized = Decimal(0)
        reserved_notional = Decimal(0)
        for position, instrument in rows:
            mark = instrument.latest_price or position.mark_price
            multiplier = instrument.contract_multiplier
            assert multiplier is not None
            position.mark_price = mark
            position.unrealized_pnl = (
                (mark - position.average_entry_price)
                * position.quantity
                * multiplier
                if position.quantity != 0
                else Decimal(0)
            )
            position.updated_at = now
            total_unrealized += position.unrealized_pnl
            reserved_notional += abs(position.quantity * mark * multiplier)
        next_equity = (
            account.initial_equity
            + account.realized_pnl
            + total_unrealized
            - account.fees_paid
        )
        next_available = next_equity - reserved_notional
        if next_equity < 0 or next_available < 0:
            _reject(
                "SHADOW_CAPITAL_INSUFFICIENT",
                "virtual account has insufficient available balance for this fill",
            )
        account.unrealized_pnl = total_unrealized
        account.equity = next_equity
        account.available_balance = next_available
        account.version += 1
        account.updated_at = now

    def _sync_linked_workflow_after_fill(
        self,
        session: Session,
        *,
        actor_id: UUID,
        team: Team,
        account: TeamShadowAccount,
        instrument: ShadowInstrument,
        position: ShadowPosition,
        order: ShadowOrder,
        now: datetime,
    ) -> None:
        """Advance a proposal-backed workflow from the same locked SHADOW ledger fill."""

        campaign = (
            None
            if order.campaign_id is None
            else session.get(Campaign, order.campaign_id, with_for_update=True)
        )
        intent = (
            None
            if order.order_intent_id is None
            else session.get(OrderIntent, order.order_intent_id, with_for_update=True)
        )
        if intent is not None:
            previous_status = intent.status
            self.facade._consume_add_unit(session, intent)
            intent.status = OrderIntentStatus.FILLED.value
            intent.updated_at = now
            intent.version += 1
            if intent.reservation_id is not None:
                reservation = session.get(
                    RiskReservation, intent.reservation_id, with_for_update=True
                )
                if reservation is not None:
                    reservation.status = ReservationStatus.OPEN.value
                    reservation.updated_at = now
                    reservation.version += 1
            INTENT_TRANSITIONS.labels(previous_status, intent.status).inc()
        if campaign is None:
            return

        campaign_fee = session.scalar(
            select(func.coalesce(func.sum(ShadowOrder.fee), 0)).where(
                ShadowOrder.campaign_id == campaign.campaign_id,
                ShadowOrder.generation == account.generation,
                ShadowOrder.status == "FILLED",
            )
        )
        campaign.realized_pnl = position.realized_pnl - Decimal(campaign_fee or 0)
        campaign.unrealized_pnl = position.unrealized_pnl
        campaign.final_pnl = campaign.realized_pnl + campaign.unrealized_pnl
        campaign.status = (
            CampaignStatus.OPEN.value
            if position.quantity != 0
            else CampaignStatus.CLOSED.value
        )
        campaign.updated_at = now

        if position.quantity == 0:
            reservations = session.scalars(
                select(RiskReservation)
                .where(
                    RiskReservation.campaign_id == campaign.campaign_id,
                    RiskReservation.status != ReservationStatus.RELEASED.value,
                )
                .with_for_update()
            ).all()
            for reservation in reservations:
                reservation.status = ReservationStatus.RELEASED.value
                reservation.updated_at = now
                reservation.version += 1
            authorization = session.get(
                TradingAuthorization, campaign.authorization_id, with_for_update=True
            )
            if authorization is not None:
                authorization.active = False
            return

        proposal = session.get(Proposal, campaign.proposal_id)
        if proposal is None or intent is None or intent.kind not in {
            IntentKind.INITIAL.value,
            IntentKind.ADD.value,
        }:
            return
        trigger_price = self.facade._proposal_detail_decimal(proposal, "invalidation_price")
        assert instrument.price_tick is not None
        trigger_price = quantize_shadow_step(trigger_price, instrument.price_tick)
        protection = session.scalar(
            select(ShadowOrder)
            .where(
                ShadowOrder.campaign_id == campaign.campaign_id,
                ShadowOrder.generation == account.generation,
                ShadowOrder.order_type == "PROTECTION",
                ShadowOrder.status.in_(("OPEN", "TRIGGERED")),
            )
            .with_for_update()
        )
        if protection is None:
            protection = ShadowOrder(
                shadow_account_id=account.shadow_account_id,
                team_id=team.team_id,
                generation=account.generation,
                shadow_instrument_id=instrument.shadow_instrument_id,
                source_account_id=order.source_account_id,
                venue=order.venue,
                campaign_id=campaign.campaign_id,
                order_intent_id=None,
                shadow_position_id=position.shadow_position_id,
                side="SELL" if position.quantity > 0 else "BUY",
                order_type="PROTECTION",
                quantity=abs(position.quantity),
                limit_price=None,
                trigger_price=trigger_price,
                trigger_type="STOP_LOSS",
                execution_type="MARKET",
                reduce_only=True,
                status="OPEN",
                filled_quantity=Decimal(0),
                fill_price=None,
                fee=Decimal(0),
                realized_pnl=Decimal(0),
                correlation_id=order.correlation_id,
                idempotency_key=f"{order.idempotency_key}:stop-loss",
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(protection)
            session.flush()
            self.transactions._audit(
                session,
                actor_id=str(actor_id),
                event_type="SHADOW_PROTECTION_CREATED",
                object_type="ShadowOrder",
                object_id=protection.shadow_order_id,
                reason="proposal invalidation price;execution_type=MARKET;reduce_only=true",
                correlation_id=protection.correlation_id,
                object_version=protection.version,
                idempotency_key=protection.idempotency_key,
                workspace_id=team.workspace_id,
                team_id=team.team_id,
                account_id=order.source_account_id,
                environment=ExecutionEnvironment.SHADOW.value,
                generation=account.generation,
                rule_summary={
                    "campaign_id": str(campaign.campaign_id),
                    "trigger_type": "STOP_LOSS",
                    "execution_type": "MARKET",
                    "trigger_price": str(trigger_price),
                    "quantity": str(abs(position.quantity)),
                    "reduce_only": True,
                    "venue_write_adapter_calls": 0,
                },
                now=now,
            )
        else:
            protection.shadow_position_id = position.shadow_position_id
            protection.side = "SELL" if position.quantity > 0 else "BUY"
            protection.quantity = abs(position.quantity)
            protection.trigger_price = trigger_price
            protection.status = "OPEN"
            protection.version += 1
            protection.updated_at = now

    def _fill_order(
        self,
        session: Session,
        *,
        actor_id: UUID,
        team: Team,
        account: TeamShadowAccount,
        instrument: ShadowInstrument,
        order: ShadowOrder,
        fill_price: Decimal,
        fee_bps: Decimal,
        event_type: str,
        now: datetime,
    ) -> ShadowFill:
        existing_fill = session.scalar(
            select(ShadowFill).where(ShadowFill.shadow_order_id == order.shadow_order_id)
        )
        if existing_fill is not None:
            return existing_fill
        assert instrument.contract_multiplier is not None
        assert instrument.latest_price is not None
        notional = abs(fill_price * order.quantity * instrument.contract_multiplier)
        fee = (notional * fee_bps / Decimal(10000)).quantize(MONEY_QUANTUM)
        position = session.scalar(
            select(ShadowPosition)
            .where(
                ShadowPosition.team_id == team.team_id,
                ShadowPosition.generation == account.generation,
                ShadowPosition.shadow_instrument_id == instrument.shadow_instrument_id,
            )
            .with_for_update()
        )
        if position is None:
            if order.reduce_only:
                _reject(
                    "SHADOW_REDUCE_ONLY_VIOLATION",
                    "reduce-only order has no open virtual position",
                )
            position = ShadowPosition(
                shadow_account_id=account.shadow_account_id,
                team_id=team.team_id,
                generation=account.generation,
                shadow_instrument_id=instrument.shadow_instrument_id,
                source_account_id=order.source_account_id,
                venue=order.venue,
                quantity=Decimal(0),
                average_entry_price=Decimal(0),
                mark_price=instrument.latest_price,
                realized_pnl=Decimal(0),
                unrealized_pnl=Decimal(0),
                status="CLOSED",
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(position)
            session.flush()
        state = apply_shadow_ledger_fill(
            current_quantity=position.quantity,
            current_average_entry_price=position.average_entry_price,
            side=order.side,
            fill_quantity=order.quantity,
            fill_price=fill_price,
            contract_multiplier=instrument.contract_multiplier,
            reduce_only=order.reduce_only,
        )
        position.quantity = state.quantity
        position.average_entry_price = state.average_entry_price
        position.mark_price = instrument.latest_price
        position.realized_pnl += state.realized_pnl
        position.status = "OPEN" if state.quantity != 0 else "CLOSED"
        position.version += 1
        position.updated_at = now
        order.shadow_position_id = position.shadow_position_id
        order.status = "FILLED"
        order.filled_quantity = order.quantity
        order.fill_price = fill_price
        order.fee = fee
        order.realized_pnl = state.realized_pnl
        order.version += 1
        order.updated_at = now
        fill = ShadowFill(
            shadow_order_id=order.shadow_order_id,
            shadow_account_id=account.shadow_account_id,
            team_id=team.team_id,
            generation=account.generation,
            shadow_instrument_id=instrument.shadow_instrument_id,
            side=order.side,
            quantity=order.quantity,
            price=fill_price,
            notional=notional,
            fee=fee,
            realized_pnl=state.realized_pnl,
            executed_at=now,
        )
        session.add(fill)
        account.realized_pnl += state.realized_pnl
        account.fees_paid += fee
        self._refresh_account(session, account=account, now=now)
        self._sync_linked_workflow_after_fill(
            session,
            actor_id=actor_id,
            team=team,
            account=account,
            instrument=instrument,
            position=position,
            order=order,
            now=now,
        )
        self._record_shadow_equity_snapshot(session, account=account, now=now)
        session.flush()
        rule_summary = {
            "side": order.side,
            "order_type": order.order_type,
            "execution_type": order.execution_type,
            "quantity": str(order.quantity),
            "latest_price": str(instrument.latest_price),
            "fill_price": str(fill_price),
            "contract_multiplier": str(instrument.contract_multiplier),
            "notional": str(notional),
            "fee_bps": str(fee_bps),
            "fee": str(fee),
            "realized_pnl": str(state.realized_pnl),
            "fully_filled": True,
            "venue_write_adapter_calls": 0,
        }
        self.transactions._audit(
            session,
            actor_id=str(actor_id),
            event_type=event_type,
            object_type="ShadowOrder",
            object_id=order.shadow_order_id,
            reason=f"status=FILLED;price={fill_price};quantity={order.quantity}",
            correlation_id=order.correlation_id,
            object_version=order.version,
            idempotency_key=order.idempotency_key,
            workspace_id=team.workspace_id,
            team_id=team.team_id,
            account_id=order.source_account_id,
            environment=ExecutionEnvironment.SHADOW.value,
            generation=account.generation,
            rule_summary=rule_summary,
            now=now,
        )
        self.transactions._audit(
            session,
            actor_id=str(actor_id),
            event_type="SHADOW_FILL_RECORDED",
            object_type="ShadowFill",
            object_id=fill.shadow_fill_id,
            reason=f"notional={notional};fee={fee};realized_pnl={state.realized_pnl}",
            correlation_id=order.correlation_id,
            object_version=1,
            idempotency_key=order.idempotency_key,
            workspace_id=team.workspace_id,
            team_id=team.team_id,
            account_id=order.source_account_id,
            environment=ExecutionEnvironment.SHADOW.value,
            generation=account.generation,
            rule_summary=rule_summary,
            now=now,
        )
        return fill

    def _match_order(
        self,
        session: Session,
        *,
        actor_id: UUID,
        team: Team,
        account: TeamShadowAccount,
        instrument: ShadowInstrument,
        order: ShadowOrder,
        fee_bps: Decimal,
        slippage_bps: Decimal,
        now: datetime,
    ) -> ShadowFill | None:
        if order.status == "FILLED":
            return session.scalar(
                select(ShadowFill).where(ShadowFill.shadow_order_id == order.shadow_order_id)
            )
        if order.status not in {"OPEN", "TRIGGERED"}:
            return None
        assert instrument.latest_price is not None
        assert instrument.price_tick is not None
        assert instrument.contract_multiplier is not None
        if order.order_type == "PROTECTION":
            position = None if order.shadow_position_id is None else session.get(
                ShadowPosition, order.shadow_position_id, with_for_update=True
            )
            if position is None or position.quantity == 0:
                order.status = "CANCELLED"
                order.version += 1
                order.updated_at = now
                return None
            assert order.trigger_price is not None
            if order.status == "OPEN" and not shadow_protection_triggered(
                position_quantity=position.quantity,
                trigger_type=str(order.trigger_type),
                latest_price=instrument.latest_price,
                trigger_price=order.trigger_price,
            ):
                return None
            if order.status == "OPEN":
                order.status = "TRIGGERED"
                order.version += 1
                order.updated_at = now
                self.transactions._audit(
                    session,
                    actor_id=str(actor_id),
                    event_type="SHADOW_PROTECTION_TRIGGERED",
                    object_type="ShadowOrder",
                    object_id=order.shadow_order_id,
                    reason=(
                        f"trigger_type={order.trigger_type};latest={instrument.latest_price};"
                        f"trigger={order.trigger_price}"
                    ),
                    correlation_id=order.correlation_id,
                    object_version=order.version,
                    idempotency_key=order.idempotency_key,
                    workspace_id=team.workspace_id,
                    team_id=team.team_id,
                    account_id=order.source_account_id,
                    environment=ExecutionEnvironment.SHADOW.value,
                    generation=account.generation,
                    rule_summary={
                        "trigger_type": order.trigger_type,
                        "execution_type": order.execution_type,
                        "position_direction": "LONG" if position.quantity > 0 else "SHORT",
                        "latest_price": str(instrument.latest_price),
                        "trigger_price": str(order.trigger_price),
                        "triggered": True,
                    },
                    now=now,
                )
            if order.execution_type == "LIMIT":
                assert order.limit_price is not None
                if not shadow_limit_crossed(
                    side=order.side,
                    latest_price=instrument.latest_price,
                    limit_price=order.limit_price,
                ):
                    return None
            effective_type = str(order.execution_type)
        else:
            effective_type = order.order_type
        if effective_type == "LIMIT":
            assert order.limit_price is not None
            if not shadow_limit_crossed(
                side=order.side,
                latest_price=instrument.latest_price,
                limit_price=order.limit_price,
            ):
                return None
            fill_price = order.limit_price
            event_type = "SHADOW_LIMIT_ORDER_FILLED"
        else:
            quote = quote_shadow_execution(
                side=order.side,
                quantity=order.quantity,
                reference_price=instrument.latest_price,
                tick_size=instrument.price_tick,
                contract_multiplier=instrument.contract_multiplier,
                fee_bps=fee_bps,
                slippage_bps=slippage_bps,
            )
            fill_price = quote.fill_price
            event_type = "SHADOW_MARKET_ORDER_FILLED"
        return self._fill_order(
            session,
            actor_id=actor_id,
            team=team,
            account=account,
            instrument=instrument,
            order=order,
            fill_price=fill_price,
            fee_bps=fee_bps,
            event_type=event_type,
            now=now,
        )

    def _simulate_linked_shadow_intent(
        self,
        session: Session,
        *,
        actor_id: UUID,
        team: Team,
        campaign: Campaign,
        intent: OrderIntent,
        proposal: Proposal,
        catalog: Instrument,
        reference_price: Decimal,
        fee_bps: Decimal,
        slippage_bps: Decimal,
        idempotency_key: str,
        now: datetime,
    ) -> dict[str, Any]:
        """Create and match one proposal-backed order in the active Team generation."""

        account = self._active_shadow_account(session, team.team_id, lock=True)
        if account is None:
            _reject("SHADOW_ACCOUNT_MISSING", "Team virtual account is not initialized")
        self._require_active_exchange_account(
            session,
            team_id=team.team_id,
            account_id=campaign.account_id,
            venue=campaign.venue,
            lock=True,
        )
        existing = session.scalar(
            select(ShadowOrder).where(ShadowOrder.order_intent_id == intent.intent_id)
        )
        if existing is not None:
            _reject("SHADOW_ORDER_EXISTS", "intent already belongs to a unified shadow order")
        instrument = self._resolve_instrument(
            session,
            team_id=team.team_id,
            venue=campaign.venue,
            symbol=catalog.symbol,
            catalog_instrument_id=catalog.instrument_id,
            latest_price=reference_price,
            observed_at=now,
            price_tick=catalog.tick_size,
            quantity_step=catalog.lot_size,
            contract_multiplier=catalog.contract_multiplier,
            is_derivative=True,
            now=now,
        )
        assert instrument.quantity_step is not None
        quantity = quantize_shadow_step(
            intent.quantity, instrument.quantity_step, rounding=ROUND_FLOOR
        )
        if quantity <= 0:
            _reject("SHADOW_QUANTITY_BELOW_PRECISION", "intent quantity rounds to zero")
        limit_price = None
        if intent.limit_price is not None:
            assert instrument.price_tick is not None
            limit_price = quantize_shadow_step(intent.limit_price, instrument.price_tick)
        order = ShadowOrder(
            shadow_account_id=account.shadow_account_id,
            team_id=team.team_id,
            generation=account.generation,
            shadow_instrument_id=instrument.shadow_instrument_id,
            source_account_id=campaign.account_id,
            venue=campaign.venue,
            campaign_id=campaign.campaign_id,
            order_intent_id=intent.intent_id,
            shadow_position_id=None,
            side=intent.side,
            order_type="LIMIT" if limit_price is not None else "MARKET",
            quantity=quantity,
            limit_price=limit_price,
            trigger_price=None,
            trigger_type=None,
            execution_type=None,
            reduce_only=intent.reduce_only,
            status="OPEN",
            filled_quantity=Decimal(0),
            fill_price=None,
            fee=Decimal(0),
            realized_pnl=Decimal(0),
            correlation_id=intent.correlation_id,
            idempotency_key=idempotency_key,
            version=1,
            created_at=now,
            updated_at=now,
        )
        session.add(order)
        session.flush()
        self.transactions._audit(
            session,
            actor_id=str(actor_id),
            event_type="SHADOW_ORDER_CREATED",
            object_type="ShadowOrder",
            object_id=order.shadow_order_id,
            reason=f"proposal_intent={intent.intent_id};type={order.order_type};quantity={quantity}",
            correlation_id=order.correlation_id,
            object_version=order.version,
            idempotency_key=idempotency_key,
            workspace_id=team.workspace_id,
            team_id=team.team_id,
            account_id=campaign.account_id,
            environment=ExecutionEnvironment.SHADOW.value,
            generation=account.generation,
            rule_summary={
                "proposal_id": str(proposal.proposal_id),
                "campaign_id": str(campaign.campaign_id),
                "intent_id": str(intent.intent_id),
                "order_type": order.order_type,
                "side": intent.side,
                "quantity": str(quantity),
                "limit_price": None if limit_price is None else str(limit_price),
                "reference_price": str(reference_price),
                "fee_bps": str(fee_bps),
                "slippage_bps": str(slippage_bps),
                "venue_write_adapter_calls": 0,
            },
            now=now,
        )
        fill = self._match_order(
            session,
            actor_id=actor_id,
            team=team,
            account=account,
            instrument=instrument,
            order=order,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
            now=now,
        )
        if fill is None:
            previous_status = intent.status
            intent.status = OrderIntentStatus.SENT.value
            intent.updated_at = now
            intent.version += 1
            INTENT_TRANSITIONS.labels(previous_status, intent.status).inc()
        result = self._order_payload(order, fill)
        result.update(
            {
                "campaign_id": str(campaign.campaign_id),
                "intent_id": str(intent.intent_id),
                "intent_version": intent.version,
                "shadow_account_id": str(account.shadow_account_id),
                "generation": account.generation,
                "shadow_account": self._account_payload(account),
            }
        )
        return result

    def create_shadow_order(
        self,
        *,
        actor_id: UUID,
        account_id: str,
        venue: str,
        symbol: str | None,
        catalog_instrument_id: UUID | None,
        side: str,
        order_type: str,
        quantity: Decimal,
        limit_price: Decimal | None,
        latest_price: Decimal | None,
        observed_at: datetime | None,
        price_tick: Decimal | None,
        quantity_step: Decimal | None,
        contract_multiplier: Decimal | None,
        is_derivative: bool,
        fee_bps: Decimal,
        slippage_bps: Decimal,
        idempotency_key: str,
        now: datetime,
    ) -> dict[str, Any]:
        try:
            return self._create_shadow_order(
                actor_id=actor_id,
                account_id=account_id,
                venue=venue,
                symbol=symbol,
                catalog_instrument_id=catalog_instrument_id,
                side=side,
                order_type=order_type,
                quantity=quantity,
                limit_price=limit_price,
                latest_price=latest_price,
                observed_at=observed_at,
                price_tick=price_tick,
                quantity_step=quantity_step,
                contract_multiplier=contract_multiplier,
                is_derivative=is_derivative,
                fee_bps=fee_bps,
                slippage_bps=slippage_bps,
                idempotency_key=idempotency_key,
                now=now,
            )
        except DomainRejected as error:
            self._audit_shadow_blocked(
                actor_id=actor_id,
                account_id=account_id,
                venue=venue,
                idempotency_key=idempotency_key,
                error=error,
                summary={
                    "operation": "CREATE_ORDER",
                    "symbol": symbol,
                    "side": side,
                    "order_type": order_type,
                    "quantity": str(quantity),
                    "has_latest_price": latest_price is not None,
                    "has_observed_at": observed_at is not None,
                    "has_price_tick": price_tick is not None,
                    "has_quantity_step": quantity_step is not None,
                    "has_contract_multiplier": contract_multiplier is not None,
                },
                now=now,
            )
            raise

    def _audit_shadow_blocked(
        self,
        *,
        actor_id: UUID,
        account_id: str | None,
        venue: str | None,
        idempotency_key: str,
        error: DomainRejected,
        summary: dict[str, Any],
        now: datetime,
    ) -> None:
        try:
            with self.database.session_factory.begin() as session:
                _user, workspace, team = self.transactions._active_scope(session, actor_id)
                assert team is not None
                account = self._active_shadow_account(session, team.team_id, lock=False)
                self.transactions._audit(
                    session,
                    actor_id=str(actor_id),
                    event_type="SHADOW_EXECUTION_BLOCKED",
                    object_type="Team",
                    object_id=team.team_id,
                    reason=f"code={error.code};detail={error.detail}",
                    correlation_id=uuid4(),
                    object_version=team.version,
                    idempotency_key=idempotency_key,
                    workspace_id=workspace.workspace_id,
                    team_id=team.team_id,
                    account_id=account_id,
                    environment=ExecutionEnvironment.SHADOW.value,
                    generation=None if account is None else account.generation,
                    rule_summary={
                        **summary,
                        "error_code": error.code,
                        "blocked": True,
                        "venue": venue,
                        "venue_write_adapter_calls": 0,
                    },
                    now=now,
                )
        except DomainRejected:
            return

    def _create_shadow_order(
        self,
        *,
        actor_id: UUID,
        account_id: str,
        venue: str,
        symbol: str | None,
        catalog_instrument_id: UUID | None,
        side: str,
        order_type: str,
        quantity: Decimal,
        limit_price: Decimal | None,
        latest_price: Decimal | None,
        observed_at: datetime | None,
        price_tick: Decimal | None,
        quantity_step: Decimal | None,
        contract_multiplier: Decimal | None,
        is_derivative: bool,
        fee_bps: Decimal,
        slippage_bps: Decimal,
        idempotency_key: str,
        now: datetime,
    ) -> dict[str, Any]:
        normalized_venue = venue.strip().upper()
        normalized_side = side.strip().upper()
        normalized_type = order_type.strip().upper()
        if normalized_side not in {"BUY", "SELL"}:
            _reject("SHADOW_SIDE_INVALID", "shadow side must be BUY or SELL")
        if normalized_type not in {"MARKET", "LIMIT"}:
            _reject("SHADOW_ORDER_TYPE_INVALID", "shadow order type must be MARKET or LIMIT")
        if not quantity.is_finite() or quantity <= 0:
            _reject("SHADOW_QUANTITY_INVALID", "quantity must be finite and positive")
        if normalized_type == "LIMIT" and (
            limit_price is None or not limit_price.is_finite() or limit_price <= 0
        ):
            _reject("SHADOW_LIMIT_PRICE_REQUIRED", "limit order requires a positive limit price")
        if normalized_type == "MARKET" and limit_price is not None:
            _reject("SHADOW_MARKET_LIMIT_FORBIDDEN", "market order cannot include a limit price")
        with self.database.session_factory.begin() as session:
            team = self.transactions._require_role(
                session, actor_id, "venue.record", account_id, normalized_venue
            )
            self.transactions._require_team_environment(team, ExecutionEnvironment.SHADOW)
            account = self._active_shadow_account(session, team.team_id, lock=True)
            if account is None:
                _reject("SHADOW_ACCOUNT_MISSING", "Team virtual account is not initialized")
            self._require_active_exchange_account(
                session,
                team_id=team.team_id,
                account_id=account_id,
                venue=normalized_venue,
                lock=True,
            )
            caller = f"{actor_id}:{team.team_id}"
            operation = "shadow.order.create"
            payload = {
                "team_id": str(team.team_id),
                "generation": account.generation,
                "account_id": account_id,
                "venue": normalized_venue,
                "symbol": symbol,
                "catalog_instrument_id": (
                    None if catalog_instrument_id is None else str(catalog_instrument_id)
                ),
                "side": normalized_side,
                "order_type": normalized_type,
                "quantity": str(quantity),
                "limit_price": None if limit_price is None else str(limit_price),
                "latest_price": None if latest_price is None else str(latest_price),
                "observed_at": None if observed_at is None else observed_at.isoformat(),
                "price_tick": None if price_tick is None else str(price_tick),
                "quantity_step": None if quantity_step is None else str(quantity_step),
                "contract_multiplier": (
                    None if contract_multiplier is None else str(contract_multiplier)
                ),
                "is_derivative": is_derivative,
                "fee_bps": str(fee_bps),
                "slippage_bps": str(slippage_bps),
            }
            digest, replay = self.transactions._idempotency(
                session,
                caller_id=caller,
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if replay is not None:
                return replay
            instrument = self._resolve_instrument(
                session,
                team_id=team.team_id,
                venue=normalized_venue,
                symbol=symbol,
                catalog_instrument_id=catalog_instrument_id,
                latest_price=latest_price,
                observed_at=observed_at,
                price_tick=price_tick,
                quantity_step=quantity_step,
                contract_multiplier=contract_multiplier,
                is_derivative=is_derivative,
                now=now,
            )
            assert instrument.quantity_step is not None
            quantized_quantity = quantize_shadow_step(
                quantity, instrument.quantity_step, rounding=ROUND_FLOOR
            )
            if quantized_quantity <= 0:
                _reject("SHADOW_QUANTITY_BELOW_PRECISION", "quantity rounds to zero")
            quantized_limit = None
            if limit_price is not None:
                assert instrument.price_tick is not None
                quantized_limit = quantize_shadow_step(limit_price, instrument.price_tick)
            correlation_id = uuid4()
            order = ShadowOrder(
                shadow_account_id=account.shadow_account_id,
                team_id=team.team_id,
                generation=account.generation,
                shadow_instrument_id=instrument.shadow_instrument_id,
                source_account_id=account_id,
                venue=normalized_venue,
                campaign_id=None,
                order_intent_id=None,
                shadow_position_id=None,
                side=normalized_side,
                order_type=normalized_type,
                quantity=quantized_quantity,
                limit_price=quantized_limit,
                trigger_price=None,
                trigger_type=None,
                execution_type=None,
                reduce_only=False,
                status="OPEN",
                filled_quantity=Decimal(0),
                fill_price=None,
                fee=Decimal(0),
                realized_pnl=Decimal(0),
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(order)
            session.flush()
            self.transactions._audit(
                session,
                actor_id=str(actor_id),
                event_type="SHADOW_ORDER_CREATED",
                object_type="ShadowOrder",
                object_id=order.shadow_order_id,
                reason=f"type={normalized_type};side={normalized_side};quantity={quantized_quantity}",
                correlation_id=correlation_id,
                object_version=order.version,
                idempotency_key=idempotency_key,
                workspace_id=team.workspace_id,
                team_id=team.team_id,
                account_id=account_id,
                environment=ExecutionEnvironment.SHADOW.value,
                generation=account.generation,
                rule_summary={
                    **payload,
                    "quantity_quantized": str(quantized_quantity),
                    "limit_price_quantized": (
                        None if quantized_limit is None else str(quantized_limit)
                    ),
                    "venue_write_adapter_calls": 0,
                },
                now=now,
            )
            fill = self._match_order(
                session,
                actor_id=actor_id,
                team=team,
                account=account,
                instrument=instrument,
                order=order,
                fee_bps=fee_bps,
                slippage_bps=slippage_bps,
                now=now,
            )
            result = self._order_payload(order, fill)
            result["shadow_account"] = self._account_payload(account)
            self.transactions._save_receipt(
                session,
                caller_id=caller,
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            return result

    def match_shadow_order(
        self,
        *,
        actor_id: UUID,
        shadow_order_id: UUID,
        expected_version: int,
        latest_price: Decimal | None,
        observed_at: datetime | None,
        price_tick: Decimal | None,
        quantity_step: Decimal | None,
        contract_multiplier: Decimal | None,
        is_derivative: bool,
        fee_bps: Decimal,
        slippage_bps: Decimal,
        idempotency_key: str,
        now: datetime,
    ) -> dict[str, Any]:
        with self.database.session_factory.begin() as session:
            order = session.get(ShadowOrder, shadow_order_id, with_for_update=True)
            if order is None:
                _reject("SHADOW_ORDER_NOT_FOUND", "shadow order does not exist")
            team = self.transactions._require_role(
                session,
                actor_id,
                "venue.record",
                order.source_account_id,
                order.venue,
                team_id=order.team_id,
            )
            self.transactions._require_team_environment(team, ExecutionEnvironment.SHADOW)
            account = self._active_shadow_account(session, team.team_id, lock=True)
            if account is None or account.shadow_account_id != order.shadow_account_id:
                _reject("SHADOW_GENERATION_ARCHIVED", "order belongs to an archived generation")
            caller = f"{actor_id}:{team.team_id}"
            operation = f"shadow.order.match:{order.shadow_order_id}"
            payload = {
                "expected_version": expected_version,
                "latest_price": None if latest_price is None else str(latest_price),
                "observed_at": None if observed_at is None else observed_at.isoformat(),
                "price_tick": None if price_tick is None else str(price_tick),
                "quantity_step": None if quantity_step is None else str(quantity_step),
                "contract_multiplier": (
                    None if contract_multiplier is None else str(contract_multiplier)
                ),
                "is_derivative": is_derivative,
                "fee_bps": str(fee_bps),
                "slippage_bps": str(slippage_bps),
            }
            digest, replay = self.transactions._idempotency(
                session,
                caller_id=caller,
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if replay is not None:
                return replay
            if order.version != expected_version:
                _reject("VERSION_CONFLICT", "shadow order version changed before matching")
            existing = session.get(ShadowInstrument, order.shadow_instrument_id)
            if existing is None:
                _reject("SHADOW_INSTRUMENT_MISSING", "shadow instrument facts are missing")
            instrument = self._resolve_instrument(
                session,
                team_id=team.team_id,
                venue=existing.venue,
                symbol=existing.symbol,
                catalog_instrument_id=existing.catalog_instrument_id,
                latest_price=latest_price,
                observed_at=observed_at,
                price_tick=price_tick if price_tick is not None else existing.price_tick,
                quantity_step=(
                    quantity_step if quantity_step is not None else existing.quantity_step
                ),
                contract_multiplier=(
                    contract_multiplier
                    if contract_multiplier is not None
                    else existing.contract_multiplier
                ),
                is_derivative=is_derivative,
                now=now,
            )
            fill = self._match_order(
                session,
                actor_id=actor_id,
                team=team,
                account=account,
                instrument=instrument,
                order=order,
                fee_bps=fee_bps,
                slippage_bps=slippage_bps,
                now=now,
            )
            result = self._order_payload(order, fill)
            result["shadow_account"] = self._account_payload(account)
            self.transactions._save_receipt(
                session,
                caller_id=caller,
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            return result

    def create_shadow_protection(
        self,
        *,
        actor_id: UUID,
        shadow_position_id: UUID,
        trigger_type: str,
        execution_type: str,
        trigger_price: Decimal,
        limit_price: Decimal | None,
        idempotency_key: str,
        now: datetime,
    ) -> dict[str, Any]:
        normalized_trigger = trigger_type.strip().upper()
        normalized_execution = execution_type.strip().upper()
        if normalized_trigger not in {"STOP_LOSS", "TAKE_PROFIT"}:
            _reject("SHADOW_TRIGGER_TYPE_INVALID", "trigger type is invalid")
        if normalized_execution not in {"MARKET", "LIMIT"}:
            _reject("SHADOW_EXECUTION_TYPE_INVALID", "execution type is invalid")
        if not trigger_price.is_finite() or trigger_price <= 0:
            _reject("SHADOW_TRIGGER_PRICE_INVALID", "trigger price must be positive")
        if normalized_execution == "LIMIT" and (
            limit_price is None or not limit_price.is_finite() or limit_price <= 0
        ):
            _reject("SHADOW_LIMIT_PRICE_REQUIRED", "LIMIT protection requires limit price")
        if normalized_execution == "MARKET" and limit_price is not None:
            _reject("SHADOW_MARKET_LIMIT_FORBIDDEN", "MARKET protection cannot include limit")
        with self.database.session_factory.begin() as session:
            position = session.get(ShadowPosition, shadow_position_id, with_for_update=True)
            if position is None or position.status != "OPEN" or position.quantity == 0:
                _reject("SHADOW_POSITION_NOT_OPEN", "virtual position is not open")
            team = self.transactions._require_role(
                session,
                actor_id,
                "venue.record",
                position.source_account_id,
                position.venue,
                team_id=position.team_id,
            )
            self.transactions._require_team_environment(team, ExecutionEnvironment.SHADOW)
            account = self._active_shadow_account(session, team.team_id, lock=True)
            if account is None or account.shadow_account_id != position.shadow_account_id:
                _reject("SHADOW_GENERATION_ARCHIVED", "position is outside active generation")
            instrument = session.get(ShadowInstrument, position.shadow_instrument_id)
            if instrument is None or instrument.price_tick is None:
                _reject("SHADOW_PRICE_PRECISION_MISSING", "price precision is required")
            caller = f"{actor_id}:{team.team_id}"
            operation = f"shadow.protection.create:{position.shadow_position_id}"
            payload = {
                "trigger_type": normalized_trigger,
                "execution_type": normalized_execution,
                "trigger_price": str(trigger_price),
                "limit_price": None if limit_price is None else str(limit_price),
                "generation": account.generation,
            }
            digest, replay = self.transactions._idempotency(
                session,
                caller_id=caller,
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if replay is not None:
                return replay
            side = "SELL" if position.quantity > 0 else "BUY"
            order = ShadowOrder(
                shadow_account_id=account.shadow_account_id,
                team_id=team.team_id,
                generation=account.generation,
                shadow_instrument_id=position.shadow_instrument_id,
                source_account_id=position.source_account_id,
                venue=position.venue,
                campaign_id=None,
                order_intent_id=None,
                shadow_position_id=position.shadow_position_id,
                side=side,
                order_type="PROTECTION",
                quantity=abs(position.quantity),
                limit_price=(
                    None
                    if limit_price is None
                    else quantize_shadow_step(limit_price, instrument.price_tick)
                ),
                trigger_price=quantize_shadow_step(trigger_price, instrument.price_tick),
                trigger_type=normalized_trigger,
                execution_type=normalized_execution,
                reduce_only=True,
                status="OPEN",
                filled_quantity=Decimal(0),
                fill_price=None,
                fee=Decimal(0),
                realized_pnl=Decimal(0),
                correlation_id=uuid4(),
                idempotency_key=idempotency_key,
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(order)
            session.flush()
            result = self._order_payload(order)
            self.transactions._save_receipt(
                session,
                caller_id=caller,
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self.transactions._audit(
                session,
                actor_id=str(actor_id),
                event_type="SHADOW_PROTECTION_CREATED",
                object_type="ShadowOrder",
                object_id=order.shadow_order_id,
                reason=(
                    f"trigger_type={normalized_trigger};execution_type={normalized_execution};"
                    f"reduce_only=true"
                ),
                correlation_id=order.correlation_id,
                object_version=order.version,
                idempotency_key=idempotency_key,
                workspace_id=team.workspace_id,
                team_id=team.team_id,
                account_id=position.source_account_id,
                environment=ExecutionEnvironment.SHADOW.value,
                generation=account.generation,
                rule_summary={**payload, "reduce_only": True, "side": side},
                now=now,
            )
            return result

    def reset_shadow_account(
        self,
        *,
        actor_id: UUID,
        expected_version: int,
        confirmation: str,
        idempotency_key: str,
        now: datetime,
    ) -> dict[str, Any]:
        self._require_human_request()
        if confirmation != "RESET_TO_100000_U":
            _reject(
                "SECOND_CONFIRMATION_REQUIRED",
                "confirmation must exactly equal RESET_TO_100000_U",
            )
        with self.database.session_factory.begin() as session:
            team = self.transactions._require_role(
                session, actor_id, "team.manage", allow_setup=True
            )
            if team.execution_mode != TeamExecutionMode.SHADOW.value:
                _reject("SHADOW_RESET_MODE_REQUIRED", "virtual capital can reset only in SHADOW")
            account = self._active_shadow_account(session, team.team_id, lock=True)
            if account is None:
                _reject("SHADOW_ACCOUNT_MISSING", "Team virtual account is not initialized")
            caller = f"{actor_id}:{team.team_id}"
            operation = f"shadow.account.reset:{team.team_id}"
            payload = {
                "expected_version": expected_version,
                "confirmation": confirmation,
                "generation": account.generation,
            }
            digest, replay = self.transactions._idempotency(
                session,
                caller_id=caller,
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if replay is not None:
                return replay
            if account.version != expected_version:
                _reject("VERSION_CONFLICT", "virtual account version changed before reset")
            orders = session.scalars(
                select(ShadowOrder)
                .where(
                    ShadowOrder.shadow_account_id == account.shadow_account_id,
                    ShadowOrder.status.in_(("OPEN", "TRIGGERED")),
                )
                .with_for_update()
            ).all()
            for order in orders:
                order.status = "CANCELLED"
                order.version += 1
                order.updated_at = now
                if order.order_intent_id is not None:
                    intent = session.get(
                        OrderIntent, order.order_intent_id, with_for_update=True
                    )
                    if intent is not None and intent.status in ACTIVE_INTENT_STATUSES:
                        previous_status = intent.status
                        intent.status = OrderIntentStatus.CANCELLED.value
                        intent.version += 1
                        intent.updated_at = now
                        if intent.reservation_id is not None:
                            reservation = session.get(
                                RiskReservation,
                                intent.reservation_id,
                                with_for_update=True,
                            )
                            if reservation is not None:
                                reservation.status = ReservationStatus.RELEASED.value
                                reservation.version += 1
                                reservation.updated_at = now
                        INTENT_TRANSITIONS.labels(previous_status, intent.status).inc()
            positions = session.scalars(
                select(ShadowPosition)
                .where(ShadowPosition.shadow_account_id == account.shadow_account_id)
                .with_for_update()
            ).all()
            for position in positions:
                position.status = "ARCHIVED"
                position.version += 1
                position.updated_at = now
            previous_generation = account.generation
            account.status = "ARCHIVED"
            account.version += 1
            account.updated_at = now
            correlation_id = uuid4()
            new_account = self._create_shadow_account(
                session,
                team=team,
                generation=previous_generation + 1,
                actor_id=actor_id,
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
                event_type="SHADOW_ACCOUNT_RESET_GENERATION_CREATED",
                now=now,
            )
            result = {
                "workspace_id": str(team.workspace_id),
                "team_id": str(team.team_id),
                "previous_generation": previous_generation,
                "cancelled_order_count": len(orders),
                "archived_position_count": len(positions),
                "shadow_account": self._account_payload(new_account),
            }
            self.transactions._save_receipt(
                session,
                caller_id=caller,
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self.transactions._audit(
                session,
                actor_id=str(actor_id),
                event_type="SHADOW_ACCOUNT_RESET",
                object_type="TeamShadowAccount",
                object_id=new_account.shadow_account_id,
                reason=(
                    f"previous_generation={previous_generation};generation={new_account.generation};"
                    f"cancelled_orders={len(orders)};archived_positions={len(positions)}"
                ),
                correlation_id=correlation_id,
                object_version=new_account.version,
                idempotency_key=idempotency_key,
                workspace_id=team.workspace_id,
                team_id=team.team_id,
                environment=ExecutionEnvironment.SHADOW.value,
                generation=new_account.generation,
                rule_summary={
                    "initial_equity": str(SHADOW_INITIAL_EQUITY),
                    "cancelled_orders": len(orders),
                    "archived_positions": len(positions),
                    "history_deleted": False,
                },
                now=now,
            )
            return result
