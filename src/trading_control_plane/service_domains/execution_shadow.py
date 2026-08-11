from __future__ import annotations

from trading_control_plane.service_component import ServiceComponent

# The domain implementation intentionally consumes the explicit service_core export surface.
# ruff: noqa: F403, F405
from trading_control_plane.service_core import *


class ShadowExecutionService(ServiceComponent):
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
            team = self.transactions._require_role(
                session, actor_id, "account.manage", account_id, venue
            )
            self.transactions._require_team_environment(team, ExecutionEnvironment.SHADOW)
            digest, replay = self.transactions._idempotency(
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
                self.facade._record_account_equity_observation(session, equity, recorded_at=now)
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
            team = self.transactions._require_role(
                session,
                actor_id,
                "venue.record",
                campaign.account_id,
                campaign.venue,
                team_id=campaign.team_id,
            )
            self.transactions._require_team_environment(team, ExecutionEnvironment.SHADOW)
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
            digest, replay = self.transactions._idempotency(
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
            self.facade._consume_add_unit(session, intent)
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
                trigger_price = self.facade._proposal_detail_decimal(proposal, "invalidation_price")
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
            pnl = self.facade._update_campaign_pnl(session, campaign, position, now=now)
            self.facade._apply_shadow_pnl_delta(
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
            self.facade._validate_sender(
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
            team = self.transactions._require_role(
                session,
                actor_id,
                "venue.record",
                campaign.account_id,
                campaign.venue,
                team_id=campaign.team_id,
            )
            self.transactions._require_team_environment(team, ExecutionEnvironment.SHADOW)
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
            self.transactions._audit(
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
