from __future__ import annotations

from trading_control_plane.service_component import ServiceComponent

# The domain implementation intentionally consumes the explicit service_core export surface.
# ruff: noqa: F403, F405
from trading_control_plane.service_core import *


class FactIngestionExecutionService(ServiceComponent):
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
        environment: ExecutionEnvironment | None = None,
        observed_at: datetime | None = None,
        now: datetime,
    ) -> UUID:
        fact_time = now if observed_at is None else observed_at
        if fact_time > now:
            _reject("FACT_TIME_INVALID", "position observation cannot be in the future")
        with self.database.session_factory.begin() as session:
            team = self.transactions._require_role(
                session, actor_id, "venue.record", account_id, venue
            )
            if team.execution_mode not in {
                TeamExecutionMode.TESTNET.value,
                TeamExecutionMode.LIVE.value,
            }:
                _reject(
                    "TEAM_SETUP_INCOMPLETE",
                    "team must select TESTNET or LIVE before recording venue facts",
                )
            actual_environment = ExecutionEnvironment(team.execution_mode)
            if environment is not None and environment is not actual_environment:
                _reject(
                    "FACT_ENVIRONMENT_MISMATCH",
                    "position environment must match the server-owned team current mode",
                )
            environment = actual_environment
            self.facade._ensure_exchange_account_reference(
                session,
                team=team,
                actor_id=actor_id,
                account_id=account_id,
                venue=venue,
                environment=environment.value,
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
            self.transactions._audit(
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
            self.transactions._require_role(
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
            self.transactions._audit(
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
        environment: ExecutionEnvironment | None = None,
        observed_at: datetime | None = None,
        now: datetime,
    ) -> UUID:
        fact_time = now if observed_at is None else observed_at
        if fact_time > now:
            _reject("FACT_TIME_INVALID", "equity observation cannot be in the future")
        with self.database.session_factory.begin() as session:
            team = self.transactions._require_role(
                session, actor_id, "venue.record", account_id, venue
            )
            if team.execution_mode not in {
                TeamExecutionMode.TESTNET.value,
                TeamExecutionMode.LIVE.value,
            }:
                _reject(
                    "TEAM_SETUP_INCOMPLETE",
                    "team must select TESTNET or LIVE before recording venue facts",
                )
            actual_environment = ExecutionEnvironment(team.execution_mode)
            if environment is not None and environment is not actual_environment:
                _reject(
                    "FACT_ENVIRONMENT_MISMATCH",
                    "equity environment must match the server-owned team current mode",
                )
            environment = actual_environment
            self.facade._ensure_exchange_account_reference(
                session,
                team=team,
                actor_id=actor_id,
                account_id=account_id,
                venue=venue,
                environment=environment.value,
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
                    control_status="READ_ONLY",
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
                fact.location_type = "VENUE"
                fact.control_status = "READ_ONLY"
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
            self.transactions._require_role(
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
                self.facade._lock_runtime_account_binding(session, runtime_binding)
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
            team = self.transactions._require_role(
                session, actor_id, "venue.record", account_id, venue
            )
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
                    self.transactions._audit(
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
            team = self.transactions._require_role(
                session, actor_id, "venue.record", account_id, venue
            )
            self.facade._ensure_exchange_account_reference(
                session,
                team=team,
                actor_id=actor_id,
                account_id=account_id,
                venue=venue,
                environment=environment.value,
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
                    self.facade._consume_add_unit(session, bound_intent)
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
                    self.facade._release_zero_fill_in_session(
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
            self.transactions._audit(
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
