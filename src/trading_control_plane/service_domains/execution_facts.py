from __future__ import annotations

import hashlib
from contextlib import AbstractContextManager, nullcontext
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from trading_control_plane import (
    credentials,
    domain,
    execution_scope,
    metrics,
    models,
    notilt,
    rejections,
    runtime_contracts,
    venue_read_only,
)
from trading_control_plane.service_component import ServiceComponent
from trading_control_plane.service_domains.accounts import (
    ensure_exchange_account_reference,
    lock_runtime_account_binding,
)
from trading_control_plane.service_domains.execution_intent import consume_add_unit

CAPITAL_HISTORY_MIN_INTERVAL = timedelta(minutes=1)
MAX_FACT_CLOCK_SKEW = execution_scope.MAX_FACT_CLOCK_SKEW
PreparedRuntimeAccountBinding = runtime_contracts.PreparedRuntimeAccountBinding
_reject = rejections.reject


def release_zero_fill_in_session(
    session: Session,
    intent: models.OrderIntent,
    terminal_status: domain.OrderIntentStatus,
    confirmed_external: bool = False,
    *,
    now: datetime,
) -> None:
    reservation = (
        session.get(models.RiskReservation, intent.reservation_id, with_for_update=True)
        if intent.reservation_id is not None
        else None
    )
    if reservation is not None and reservation.status != domain.ReservationStatus.RELEASED.value:
        if reservation.status == domain.ReservationStatus.UNKNOWN.value and not confirmed_external:
            _reject("RISK_RESERVATION_UNKNOWN", "unknown risk cannot be released")
        reservation.status = domain.ReservationStatus.RELEASED.value
        reservation.updated_at = now
        reservation.version += 1
        authorization = session.get(
            models.TradingAuthorization, intent.authorization_id, with_for_update=True
        )
        if authorization is None or authorization.used_quantity < intent.quantity:
            _reject("AUTHORIZATION_USAGE_INVALID", "authorization usage is inconsistent")
        authorization.used_quantity -= intent.quantity
    intent.status = terminal_status.value
    intent.updated_at = now
    intent.version += 1


def record_account_equity_observation(
    session: Session,
    fact: models.AccountEquity,
    *,
    recorded_at: datetime,
) -> None:
    session.flush()
    latest_observed_at = session.scalar(
        select(models.AccountEquityObservation.observed_at)
        .where(models.AccountEquityObservation.account_equity_id == fact.account_equity_id)
        .order_by(models.AccountEquityObservation.observed_at.desc())
        .limit(1)
    )
    if latest_observed_at is not None and (
        fact.observed_at < latest_observed_at + CAPITAL_HISTORY_MIN_INTERVAL
    ):
        return
    usd_equity = None
    if fact.fact_status == domain.FactStatus.KNOWN.value:
        usd_equity = (
            fact.equity
            if fact.currency.upper() in notilt.USD_STABLE_ASSETS
            else fact.valuation_equity
        )
    session.add(
        models.AccountEquityObservation(
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


class FactIngestionExecutionService(ServiceComponent):
    @staticmethod
    def _record_fact_adapter_health(
        session: Session,
        *,
        binding: PreparedRuntimeAccountBinding,
        items_observed: int,
        history_error_code: str | None,
        now: datetime,
    ) -> None:
        current = session.scalar(
            select(models.RuntimeSourceHealth)
            .where(
                models.RuntimeSourceHealth.team_id == binding.team_id,
                models.RuntimeSourceHealth.source_name == binding.venue,
                models.RuntimeSourceHealth.environment == binding.environment,
                models.RuntimeSourceHealth.account_id == binding.account_id,
                models.RuntimeSourceHealth.venue == binding.venue,
            )
            .with_for_update()
        )
        status = "FAILED" if history_error_code is not None else "SUCCESS"
        values = {
            "status": status,
            "items_observed": items_observed,
            "error_code": history_error_code,
            "checked_at": now,
            "last_success_at": now,
            "retry_at": None,
            "consecutive_failures": 1 if history_error_code is not None else 0,
            "updated_by": binding.service_principal_id,
        }
        if current is None:
            session.add(
                models.RuntimeSourceHealth(
                    team_id=binding.team_id,
                    source_name=binding.venue,
                    environment=binding.environment,
                    account_id=binding.account_id,
                    venue=binding.venue,
                    **values,
                )
            )
            return
        for field, value in values.items():
            setattr(current, field, value)

    @staticmethod
    def _upsert_read_only_account_equity(
        session: Session,
        *,
        team: models.Team,
        account_id: str,
        venue: str,
        environment: domain.ExecutionEnvironment,
        account_equity: venue_read_only.VenueEquity,
        now: datetime,
    ) -> models.AccountEquity:
        fact = session.scalar(
            select(models.AccountEquity)
            .where(
                models.AccountEquity.team_id == team.team_id,
                models.AccountEquity.account_id == account_id,
                models.AccountEquity.venue == venue,
                models.AccountEquity.environment == environment.value,
                models.AccountEquity.currency == account_equity.currency,
            )
            .with_for_update()
        )
        stable = account_equity.currency.upper() in notilt.USD_STABLE_ASSETS
        if fact is None:
            fact = models.AccountEquity(
                team_id=team.team_id,
                account_id=account_id,
                venue=venue,
                environment=environment.value,
                equity=account_equity.equity,
                available_balance=account_equity.available_balance,
                currency=account_equity.currency,
                valuation_currency="USD" if stable else None,
                valuation_price=Decimal(1) if stable else None,
                valuation_equity=account_equity.equity if stable else None,
                valuation_observed_at=now if stable else None,
                fact_status=domain.FactStatus.KNOWN.value,
                observed_at=now,
                updated_at=now,
            )
            session.add(fact)
        else:
            if fact.updated_at > now:
                _reject(
                    f"{venue}_SNAPSHOT_SUPERSEDED",
                    "an older snapshot cannot overwrite newer equity facts",
                )
            fact.equity = account_equity.equity
            fact.available_balance = account_equity.available_balance
            fact.currency = account_equity.currency
            fact.valuation_currency = "USD" if stable else None
            fact.valuation_price = Decimal(1) if stable else None
            fact.valuation_equity = account_equity.equity if stable else None
            fact.valuation_observed_at = now if stable else None
            fact.fact_status = domain.FactStatus.KNOWN.value
            fact.observed_at = now
            fact.updated_at = now
        record_account_equity_observation(session, fact, recorded_at=now)
        return fact

    def ingest_normalized_read_only_account_snapshot(
        self,
        account_id: str,
        actor_id: UUID,
        snapshots: tuple[venue_read_only.VenueReadOnlySnapshot, ...],
        *,
        venue: str,
        environment: domain.ExecutionEnvironment,
        runtime_binding: PreparedRuntimeAccountBinding,
        account_equities: tuple[venue_read_only.VenueEquity, ...] | None = None,
        now: datetime,
    ) -> dict[str, Any]:
        """Persist the exchange-neutral fact-adapter contract for an exact binding."""

        if venue not in credentials.SUPPORTED_EXCHANGE_VENUES or runtime_binding.venue != venue:
            _reject(
                "FACT_ADAPTER_SCOPE_MISMATCH",
                "normalized facts are outside the exact supported account scope",
            )
        return self._ingest_read_only_account_snapshot(
            account_id,
            actor_id,
            snapshots,
            venue=venue,
            environment=environment,
            runtime_binding=runtime_binding,
            account_equities=account_equities,
            now=now,
        )

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
        environment: domain.ExecutionEnvironment | None = None,
        observed_at: datetime | None = None,
        now: datetime,
    ) -> UUID:
        fact_time = now if observed_at is None else observed_at
        if fact_time > now:
            _reject("FACT_TIME_INVALID", "position observation cannot be in the future")
        with self.database.session_factory.begin() as session:
            team = self.transactions.require_role(
                session, actor_id, "venue.record", account_id, venue
            )
            if team.execution_mode not in {
                domain.TeamExecutionMode.TESTNET.value,
                domain.TeamExecutionMode.LIVE.value,
            }:
                _reject(
                    "TEAM_SETUP_INCOMPLETE",
                    "team must select TESTNET or LIVE before recording venue facts",
                )
            actual_environment = domain.ExecutionEnvironment(team.execution_mode)
            if environment is not None and environment is not actual_environment:
                _reject(
                    "FACT_ENVIRONMENT_MISMATCH",
                    "position environment must match the server-owned team current mode",
                )
            environment = actual_environment
            ensure_exchange_account_reference(
                session,
                team=team,
                actor_id=actor_id,
                account_id=account_id,
                venue=venue,
                environment=environment.value,
                now=now,
            )
            position = session.scalar(
                select(models.Position).where(
                    models.Position.team_id == team.team_id,
                    models.Position.account_id == account_id,
                    models.Position.venue == venue,
                    models.Position.environment == environment.value,
                    models.Position.instrument_id == instrument_id,
                )
            )
            if position is None:
                position = models.Position(
                    team_id=team.team_id,
                    account_id=account_id,
                    venue=venue,
                    environment=environment.value,
                    instrument_id=instrument_id,
                    quantity=quantity,
                    average_entry_price=average_entry_price,
                    mark_price=mark_price,
                    fact_status=domain.FactStatus.KNOWN.value
                    if known
                    else domain.FactStatus.UNKNOWN.value,
                    observed_at=fact_time,
                    updated_at=now,
                )
                session.add(position)
                session.flush()
            else:
                position.quantity = quantity
                position.average_entry_price = average_entry_price
                position.mark_price = mark_price
                position.fact_status = (
                    domain.FactStatus.KNOWN.value if known else domain.FactStatus.UNKNOWN.value
                )
                position.observed_at = fact_time
                position.updated_at = now
            self.transactions.audit(
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
        required_environment: domain.ExecutionEnvironment | None = None,
        known: bool = True,
        observed_at: datetime | None = None,
        now: datetime,
    ) -> UUID:
        fact_time = now if observed_at is None else observed_at
        if fact_time > now:
            _reject("FACT_TIME_INVALID", "protection observation cannot be in the future")
        with self.database.session_factory.begin() as session:
            position = session.get(models.Position, position_id)
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
                campaign = session.get(models.Campaign, campaign_id)
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
            self.transactions.require_role(
                session,
                actor_id,
                "venue.record",
                position.account_id,
                position.venue,
                team_id=position.team_id,
            )
            protection = session.scalar(
                select(models.ProtectionOrder).where(
                    models.ProtectionOrder.position_id == position_id
                )
            )
            effective_coverage = fully_covered and known
            status = (
                domain.ProtectionStatus.UNKNOWN
                if not known
                else domain.ProtectionStatus.ACTIVE
                if effective_coverage
                else domain.ProtectionStatus.DEGRADED
            )
            if protection is None:
                protection = models.ProtectionOrder(
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
                metrics.PROTECTION_ISSUES.inc()
            self.transactions.audit(
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
        environment: domain.ExecutionEnvironment | None = None,
        observed_at: datetime | None = None,
        now: datetime,
    ) -> UUID:
        fact_time = now if observed_at is None else observed_at
        if fact_time > now:
            _reject("FACT_TIME_INVALID", "equity observation cannot be in the future")
        with self.database.session_factory.begin() as session:
            team = self.transactions.require_role(
                session, actor_id, "venue.record", account_id, venue
            )
            if team.execution_mode not in {
                domain.TeamExecutionMode.TESTNET.value,
                domain.TeamExecutionMode.LIVE.value,
            }:
                _reject(
                    "TEAM_SETUP_INCOMPLETE",
                    "team must select TESTNET or LIVE before recording venue facts",
                )
            actual_environment = domain.ExecutionEnvironment(team.execution_mode)
            if environment is not None and environment is not actual_environment:
                _reject(
                    "FACT_ENVIRONMENT_MISMATCH",
                    "equity environment must match the server-owned team current mode",
                )
            environment = actual_environment
            ensure_exchange_account_reference(
                session,
                team=team,
                actor_id=actor_id,
                account_id=account_id,
                venue=venue,
                environment=environment.value,
                now=now,
            )
            fact = session.scalar(
                select(models.AccountEquity).where(
                    models.AccountEquity.team_id == team.team_id,
                    models.AccountEquity.account_id == account_id,
                    models.AccountEquity.venue == venue,
                    models.AccountEquity.environment == environment.value,
                    models.AccountEquity.currency == currency,
                )
            )
            stable = currency.upper() in notilt.USD_STABLE_ASSETS
            if fact is None:
                fact = models.AccountEquity(
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
                    fact_status=domain.FactStatus.KNOWN.value
                    if known
                    else domain.FactStatus.UNKNOWN.value,
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
                fact.fact_status = (
                    domain.FactStatus.KNOWN.value if known else domain.FactStatus.UNKNOWN.value
                )
                fact.observed_at = fact_time
                fact.updated_at = now
            record_account_equity_observation(session, fact, recorded_at=now)
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
        required_environment: domain.ExecutionEnvironment | None = None,
        now: datetime,
    ) -> UUID:
        with self.database.session_factory.begin() as session:
            campaign = session.get(models.Campaign, campaign_id)
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
            self.transactions.require_role(
                session,
                actor_id,
                "venue.record",
                campaign.account_id,
                campaign.venue,
                team_id=campaign.team_id,
            )
            instrument = session.get(models.Instrument, campaign.instrument_id)
            if venue != campaign.venue:
                _reject("VENUE_SCOPE_MISMATCH", "funding venue does not match campaign")
            if instrument is None or currency != instrument.collateral_currency:
                _reject("PNL_CURRENCY_MISMATCH", "funding currency lacks an FX conversion")
            existing = session.scalar(
                select(models.FundingPayment).where(
                    models.FundingPayment.team_id == campaign.team_id,
                    models.FundingPayment.environment == campaign.environment,
                    models.FundingPayment.account_id == campaign.account_id,
                    models.FundingPayment.venue == venue,
                    models.FundingPayment.venue_payment_id == venue_payment_id,
                )
            )
            if existing is not None:
                if (
                    existing.campaign_id == campaign_id
                    and existing.amount == amount
                    and existing.currency == currency
                ):
                    return existing.funding_payment_id
                raise domain.IdempotencyConflict
            payment = models.FundingPayment(
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

    def _ingest_read_only_account_snapshot(
        self,
        account_id: str,
        actor_id: UUID,
        snapshots: tuple[venue_read_only.VenueReadOnlySnapshot, ...],
        *,
        venue: str,
        environment: domain.ExecutionEnvironment,
        runtime_binding: PreparedRuntimeAccountBinding | None = None,
        account_equities: tuple[venue_read_only.VenueEquity, ...] | None = None,
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
        if account_equities is not None:
            if venue != "BINANCE":
                _reject(
                    "FACT_ADAPTER_SCOPE_MISMATCH",
                    "complete wallet equity coverage is only supported for Binance",
                )
            currencies = [item.currency.upper() for item in account_equities]
            if len(currencies) != len(set(currencies)):
                _reject(
                    "BINANCE_RESPONSE_INVALID",
                    "account wallet snapshot contains duplicate currencies",
                )
            if any(
                item.observed_at != observed_at
                or item.currency.upper() not in notilt.USD_STABLE_ASSETS
                or item.equity < 0
                or item.available_balance < 0
                for item in account_equities
            ):
                _reject(
                    "BINANCE_RESPONSE_INVALID",
                    "account wallet snapshot is outside the supported stablecoin scope",
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
                lock_runtime_account_binding(session, runtime_binding)
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
            if account_equities is not None:
                team = self.transactions.require_role(
                    session,
                    actor_id,
                    "venue.record",
                    account_id,
                    venue,
                )
                reported_currencies = {item.currency.upper() for item in account_equities}
                for account_equity in account_equities:
                    self._upsert_read_only_account_equity(
                        session,
                        team=team,
                        account_id=account_id,
                        venue=venue,
                        environment=environment,
                        account_equity=account_equity,
                        now=now,
                    )
                missing = session.scalars(
                    select(models.AccountEquity)
                    .where(
                        models.AccountEquity.team_id == team.team_id,
                        models.AccountEquity.account_id == account_id,
                        models.AccountEquity.venue == venue,
                        models.AccountEquity.environment == environment.value,
                        models.AccountEquity.currency.in_(tuple(notilt.USD_STABLE_ASSETS)),
                        models.AccountEquity.currency.not_in(reported_currencies),
                    )
                    .with_for_update()
                ).all()
                for fact in missing:
                    self._upsert_read_only_account_equity(
                        session,
                        team=team,
                        account_id=account_id,
                        venue=venue,
                        environment=environment,
                        account_equity=venue_read_only.VenueEquity(
                            equity=Decimal(0),
                            available_balance=Decimal(0),
                            currency=fact.currency,
                            observed_at=observed_at,
                        ),
                        now=now,
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
            if runtime_binding is not None:
                self._record_fact_adapter_health(
                    session,
                    binding=runtime_binding,
                    items_observed=len(snapshots),
                    history_error_code=history_error_code,
                    now=now,
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
        environment: domain.ExecutionEnvironment,
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
            team = self.transactions.require_role(
                session, actor_id, "venue.record", account_id, venue
            )
            scoped = session.execute(
                select(models.Position, models.Instrument)
                .join(
                    models.Instrument,
                    models.Position.instrument_id == models.Instrument.instrument_id,
                )
                .where(
                    models.Position.team_id == team.team_id,
                    models.Position.account_id == account_id,
                    models.Position.venue == venue,
                    models.Position.environment == environment.value,
                    models.Instrument.venue == venue,
                )
                .order_by(models.Instrument.symbol, models.Position.position_id)
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
                    or position.fact_status != domain.FactStatus.KNOWN.value
                )
                if position.quantity != 0:
                    closed += 1
                position.quantity = Decimal(0)
                position.average_entry_price = Decimal(0)
                position.fact_status = domain.FactStatus.KNOWN.value
                position.observed_at = now
                position.updated_at = now

                protection = session.scalar(
                    select(models.ProtectionOrder)
                    .where(models.ProtectionOrder.position_id == position.position_id)
                    .with_for_update()
                )
                if protection is not None:
                    protection_order = session.scalar(
                        select(models.VenueOrder)
                        .where(
                            models.VenueOrder.team_id == team.team_id,
                            models.VenueOrder.environment == environment.value,
                            models.VenueOrder.account_id == account_id,
                            models.VenueOrder.venue == venue,
                            models.VenueOrder.venue_order_id == protection.venue_order_id,
                        )
                        .with_for_update()
                    )
                    if protection_order is not None and protection_order.status in {
                        domain.VenueOrderStatus.SENT.value,
                        domain.VenueOrderStatus.PARTIALLY_FILLED.value,
                        domain.VenueOrderStatus.UNKNOWN.value,
                    }:
                        still_open = protection_order.venue_order_id in observed_order_ids
                        if not still_open:
                            protection_order.status = domain.VenueOrderStatus.CANCELLED.value
                        protection_order.observed_at = now
                        protection_order.updated_at = now
                        if still_open:
                            protection.quantity = Decimal(0)
                            protection.status = domain.ProtectionStatus.DEGRADED.value
                            protection.fully_covered = False
                            protection.observed_at = now
                            protection.updated_at = now
                        else:
                            session.delete(protection)
                    else:
                        session.delete(protection)
                if changed:
                    self.transactions.audit(
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
            unresolved_orders = session.scalars(
                select(models.VenueOrder)
                .where(
                    models.VenueOrder.team_id == team.team_id,
                    models.VenueOrder.environment == environment.value,
                    models.VenueOrder.account_id == account_id,
                    models.VenueOrder.venue == venue,
                    models.VenueOrder.status.in_(
                        {
                            domain.VenueOrderStatus.SENT.value,
                            domain.VenueOrderStatus.PARTIALLY_FILLED.value,
                            domain.VenueOrderStatus.UNKNOWN.value,
                        }
                    ),
                )
                .with_for_update()
            ).all()
            for order in unresolved_orders:
                if order.venue_order_id in observed_order_ids:
                    continue
                if (
                    order.order_intent_id is None
                    and order.reduce_only
                    and order.order_type == "STOPLOSS"
                ):
                    flat_position = session.scalar(
                        select(models.Position).where(
                            models.Position.team_id == team.team_id,
                            models.Position.environment == environment.value,
                            models.Position.account_id == account_id,
                            models.Position.venue == venue,
                            models.Position.instrument_id == order.instrument_id,
                            models.Position.fact_status == domain.FactStatus.KNOWN.value,
                            models.Position.quantity == 0,
                        )
                    )
                    if flat_position is not None:
                        order.status = domain.VenueOrderStatus.CANCELLED.value
                        order.observed_at = now
                        order.updated_at = now
                        continue
                fills = session.scalars(
                    select(models.VenueFill).where(
                        models.VenueFill.team_id == team.team_id,
                        models.VenueFill.environment == environment.value,
                        models.VenueFill.account_id == account_id,
                        models.VenueFill.venue == venue,
                        models.VenueFill.order_intent_id == order.order_intent_id,
                    )
                ).all()
                confirmed_quantity = sum((item.quantity for item in fills), Decimal(0))
                if (
                    order.order_intent_id is not None
                    and confirmed_quantity >= order.ordered_quantity
                ):
                    order.status = domain.VenueOrderStatus.FILLED.value
                    order.filled_quantity = min(confirmed_quantity, order.ordered_quantity)
                else:
                    # Absence from an account-wide open-order snapshot is not proof
                    # of cancellation.  Keep the terminal state unknown until a
                    # fill or Freqtrade query confirms the outcome.
                    order.status = domain.VenueOrderStatus.UNKNOWN.value
                order.observed_at = now
                order.updated_at = now
            return closed, covered

    def _recover_filled_freqtrade_protection_exit(
        self,
        session: Session,
        *,
        actor_id: UUID,
        team_id: UUID,
        account_id: str,
        venue: str,
        environment: domain.ExecutionEnvironment,
        instrument_id: UUID,
        position: models.Position,
        external_fills: tuple[venue_read_only.VenueFill, ...],
        now: datetime,
    ) -> UUID | None:
        """Bind an exact Freqtrade-managed stop fill without issuing another order."""

        if position.quantity != 0:
            return None
        campaigns = session.scalars(
            select(models.Campaign)
            .where(
                models.Campaign.team_id == team_id,
                models.Campaign.environment == environment.value,
                models.Campaign.account_id == account_id,
                models.Campaign.venue == venue,
                models.Campaign.instrument_id == instrument_id,
                models.Campaign.status != domain.CampaignStatus.CLOSED.value,
            )
            .with_for_update()
        ).all()
        if not campaigns:
            return None
        if len(campaigns) != 1:
            _reject(
                f"{venue}_PROTECTION_EXIT_CAMPAIGN_AMBIGUOUS",
                "filled Freqtrade protection order does not map to one open campaign",
            )
        campaign = campaigns[0]
        authorization = session.get(
            models.TradingAuthorization,
            campaign.authorization_id,
            with_for_update=True,
        )
        if authorization is None:
            _reject(
                f"{venue}_PROTECTION_EXIT_AUTHORIZATION_INVALID",
                "filled Freqtrade protection order lacks its authorization",
            )
        if authorization.leverage is None:
            legacy_entry_intents = session.scalars(
                select(models.OrderIntent).where(
                    models.OrderIntent.campaign_id == campaign.campaign_id,
                    models.OrderIntent.kind.in_(
                        {domain.IntentKind.INITIAL.value, domain.IntentKind.ADD.value}
                    ),
                )
            ).all()
            if not legacy_entry_intents or any(
                intent.leverage is not None for intent in legacy_entry_intents
            ):
                _reject(
                    f"{venue}_PROTECTION_EXIT_AUTHORIZATION_INVALID",
                    "filled Freqtrade protection order has inconsistent leverage history",
                )
        existing_reductions = session.scalars(
            select(models.OrderIntent).where(
                models.OrderIntent.campaign_id == campaign.campaign_id,
                models.OrderIntent.kind.in_(
                    {domain.IntentKind.REDUCE.value, domain.IntentKind.EXIT.value}
                ),
            )
        ).all()
        if existing_reductions:
            if len(existing_reductions) == 1:
                existing_exit = existing_reductions[0]
                bound_order = session.scalar(
                    select(models.VenueOrder).where(
                        models.VenueOrder.order_intent_id == existing_exit.intent_id,
                        models.VenueOrder.client_order_id
                        == f"ftp-{position.position_id.hex[:28]}",
                        models.VenueOrder.venue_order_id.in_(
                            {fill.order_id for fill in external_fills if fill.order_id}
                        ),
                    )
                )
                if (
                    existing_exit.kind == domain.IntentKind.EXIT.value
                    and existing_exit.trigger_source == "FREQTRADE_PROTECTION_FILLED"
                    and existing_exit.status == domain.OrderIntentStatus.FILLED.value
                    and bound_order is not None
                ):
                    return existing_exit.intent_id
            _reject(
                f"{venue}_PROTECTION_EXIT_INTENT_CONFLICT",
                "filled Freqtrade protection order conflicts with an existing reduction intent",
            )
        campaign_fills = session.scalars(
            select(models.VenueFill).where(
                models.VenueFill.campaign_id == campaign.campaign_id,
            )
        ).all()
        signed_campaign_quantity = sum(
            (fill.quantity if fill.side == "BUY" else -fill.quantity for fill in campaign_fills),
            Decimal(0),
        )
        if signed_campaign_quantity == 0:
            return None
        expected_side = "SELL" if signed_campaign_quantity > 0 else "BUY"
        expected_quantity = abs(signed_campaign_quantity)
        external_order_ids = {
            fill.order_id
            for fill in external_fills
            if fill.order_id and fill.side == expected_side
        }
        if not external_order_ids:
            return None
        orders = session.scalars(
            select(models.VenueOrder)
            .where(
                models.VenueOrder.team_id == team_id,
                models.VenueOrder.environment == environment.value,
                models.VenueOrder.account_id == account_id,
                models.VenueOrder.venue == venue,
                models.VenueOrder.instrument_id == instrument_id,
                models.VenueOrder.venue_order_id.in_(external_order_ids),
                models.VenueOrder.order_intent_id.is_(None),
                models.VenueOrder.client_order_id == f"ftp-{position.position_id.hex[:28]}",
                models.VenueOrder.order_type == "STOPLOSS",
                models.VenueOrder.reduce_only,
                models.VenueOrder.status == domain.VenueOrderStatus.FILLED.value,
                models.VenueOrder.ordered_quantity > 0,
                models.VenueOrder.filled_quantity == models.VenueOrder.ordered_quantity,
                models.VenueOrder.filled_quantity == expected_quantity,
            )
            .with_for_update()
        ).all()
        if not orders:
            return None
        if len(orders) != 1:
            _reject(
                f"{venue}_PROTECTION_EXIT_CONFLICT",
                "authoritative fills match multiple Freqtrade protection orders",
            )
        order = orders[0]
        matching_external_fills = tuple(
            fill for fill in external_fills if fill.order_id == order.venue_order_id
        )
        if (
            not matching_external_fills
            or any(fill.side != expected_side for fill in matching_external_fills)
            or sum((fill.quantity for fill in matching_external_fills), Decimal(0))
            != expected_quantity
        ):
            _reject(
                f"{venue}_PROTECTION_EXIT_FILL_CONFLICT",
                "filled Freqtrade protection order lacks exact authoritative fills",
            )
        cleanup_fills: list[models.VenueFill] = []
        for external_fill in matching_external_fills:
            cleanup_fill = session.scalar(
                select(models.VenueFill)
                .where(
                    models.VenueFill.team_id == team_id,
                    models.VenueFill.environment == environment.value,
                    models.VenueFill.account_id == account_id,
                    models.VenueFill.venue == venue,
                    models.VenueFill.venue_fill_id == external_fill.fill_id,
                )
                .with_for_update()
            )
            if (
                cleanup_fill is None
                or cleanup_fill.order_intent_id is not None
                or cleanup_fill.campaign_id is not None
            ):
                _reject(
                    f"{venue}_PROTECTION_EXIT_FILL_CONFLICT",
                    "authoritative Freqtrade protection fill has an incompatible binding",
                )
            cleanup_fills.append(cleanup_fill)

        campaign.target_version += 1
        campaign.current_target_quantity = Decimal(0)
        campaign.target_reason = "FREQTRADE_PROTECTION_FILLED"
        campaign.target_urgency = domain.TargetUrgency.IMMEDIATE.value
        campaign.target_calculated_at = now
        campaign.status = domain.CampaignStatus.CLOSING.value
        campaign.updated_at = now
        semantic_hash = hashlib.sha256(
            (
                f"{campaign.campaign_id}:{order.venue_order_id}:"
                f"{order.filled_quantity}:{order.observed_at.isoformat()}"
            ).encode()
        ).hexdigest()
        exit_intent = models.OrderIntent(
            campaign_id=campaign.campaign_id,
            authorization_id=campaign.authorization_id,
            reservation_id=None,
            kind=domain.IntentKind.EXIT.value,
            side=expected_side,
            quantity=order.filled_quantity,
            leverage=authorization.leverage,
            limit_price=None,
            reduce_only=True,
            trigger_source="FREQTRADE_PROTECTION_FILLED",
            trigger_observed_at=max(fill.executed_at for fill in matching_external_fills),
            add_unit_consumed=False,
            target_version=campaign.target_version,
            position_id=position.position_id,
            position_observed_at=position.observed_at,
            status=domain.OrderIntentStatus.FILLED.value,
            semantic_hash=semantic_hash,
            actor_id=str(actor_id),
            correlation_id=uuid4(),
            version=1,
            created_at=now,
            updated_at=now,
        )
        session.add(exit_intent)
        session.flush()
        order.order_intent_id = exit_intent.intent_id
        for cleanup_fill in cleanup_fills:
            cleanup_fill.order_intent_id = exit_intent.intent_id
            cleanup_fill.campaign_id = campaign.campaign_id
        leverage_audit = (
            "LEGACY_UNAVAILABLE"
            if authorization.leverage is None
            else str(authorization.leverage)
        )
        self.transactions.audit(
            session,
            actor_id=str(actor_id),
            event_type="FREQTRADE_PROTECTION_EXIT_RECOVERED",
            object_type="OrderIntent",
            object_id=exit_intent.intent_id,
            reason=(
                f"order={order.venue_order_id};fills={len(cleanup_fills)};"
                f"leverage={leverage_audit}"
            ),
            correlation_id=exit_intent.correlation_id,
            object_version=1,
            now=now,
        )
        return exit_intent.intent_id

    def _ingest_read_only_snapshot(
        self,
        account_id: str,
        actor_id: UUID,
        snapshot: venue_read_only.VenueReadOnlySnapshot,
        *,
        venue: str,
        environment: domain.ExecutionEnvironment,
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
            team = self.transactions.require_role(
                session, actor_id, "venue.record", account_id, venue
            )
            ensure_exchange_account_reference(
                session,
                team=team,
                actor_id=actor_id,
                account_id=account_id,
                venue=venue,
                environment=environment.value,
                now=now,
            )
            instrument = session.scalar(
                select(models.Instrument)
                .where(
                    models.Instrument.venue == venue,
                    models.Instrument.symbol == snapshot.symbol,
                )
                .with_for_update()
            )
            if instrument is None:
                instrument = models.Instrument(
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
                select(models.Position)
                .where(
                    models.Position.team_id == team.team_id,
                    models.Position.account_id == account_id,
                    models.Position.venue == venue,
                    models.Position.environment == environment.value,
                    models.Position.instrument_id == instrument.instrument_id,
                )
                .with_for_update()
            )
            position_authoritatively_closed = bool(
                position is not None and position.quantity != 0 and snapshot.position.quantity == 0
            )
            if position is None:
                position = models.Position(
                    team_id=team.team_id,
                    account_id=account_id,
                    venue=venue,
                    environment=environment.value,
                    instrument_id=instrument.instrument_id,
                    quantity=snapshot.position.quantity,
                    average_entry_price=snapshot.position.average_entry_price,
                    mark_price=snapshot.position.mark_price,
                    fact_status=domain.FactStatus.KNOWN.value,
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
                position.fact_status = domain.FactStatus.KNOWN.value
                position.observed_at = now
                position.updated_at = now

            equity = session.scalar(
                select(models.AccountEquity)
                .where(
                    models.AccountEquity.team_id == team.team_id,
                    models.AccountEquity.account_id == account_id,
                    models.AccountEquity.venue == venue,
                    models.AccountEquity.environment == environment.value,
                    models.AccountEquity.currency == snapshot.equity.currency,
                )
                .with_for_update()
            )
            stable_equity = snapshot.equity.currency.upper() in notilt.USD_STABLE_ASSETS
            if equity is None:
                equity = models.AccountEquity(
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
                    fact_status=domain.FactStatus.KNOWN.value,
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
                equity.fact_status = domain.FactStatus.KNOWN.value
                equity.observed_at = now
                equity.updated_at = now
            record_account_equity_observation(session, equity, recorded_at=now)

            order_count = 0
            for external_order in snapshot.orders:
                intent: models.OrderIntent | None = None
                current_order = session.scalar(
                    select(models.VenueOrder)
                    .where(
                        models.VenueOrder.team_id == team.team_id,
                        models.VenueOrder.environment == environment.value,
                        models.VenueOrder.account_id == account_id,
                        models.VenueOrder.venue == venue,
                        models.VenueOrder.venue_order_id == external_order.order_id,
                    )
                    .with_for_update()
                )
                if current_order is None:
                    current_order = session.scalar(
                        select(models.VenueOrder)
                        .where(
                            models.VenueOrder.team_id == team.team_id,
                            models.VenueOrder.environment == environment.value,
                            models.VenueOrder.account_id == account_id,
                            models.VenueOrder.venue == venue,
                            models.VenueOrder.client_order_id == external_order.client_order_id,
                        )
                        .with_for_update()
                    )
                if current_order is not None and current_order.order_intent_id is not None:
                    intent = session.get(models.OrderIntent, current_order.order_intent_id)
                    campaign = (
                        None if intent is None else session.get(models.Campaign, intent.campaign_id)
                    )
                    if (
                        intent is None
                        or campaign is None
                        or campaign.team_id != team.team_id
                        or campaign.account_id != account_id
                        or campaign.venue != venue
                        or campaign.environment != environment.value
                        or campaign.instrument_id != instrument.instrument_id
                    ):
                        _reject(
                            f"{venue}_ORDER_BINDING_INVALID",
                            "persisted order identity does not match its internal scope",
                        )
                if current_order is None:
                    current_order = models.VenueOrder(
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
            confirmed_fill_quantity_by_order: dict[str, Decimal] = {}
            for external_fill in snapshot.fills:
                if external_fill.order_id:
                    confirmed_fill_quantity_by_order[external_fill.order_id] = (
                        confirmed_fill_quantity_by_order.get(external_fill.order_id, Decimal(0))
                        + external_fill.quantity
                    )
            for external_fill in snapshot.fills:
                current_fill = session.scalar(
                    select(models.VenueFill).where(
                        models.VenueFill.team_id == team.team_id,
                        models.VenueFill.environment == environment.value,
                        models.VenueFill.account_id == account_id,
                        models.VenueFill.venue == venue,
                        models.VenueFill.venue_fill_id == external_fill.fill_id,
                    )
                )
                venue_order = session.scalar(
                    select(models.VenueOrder).where(
                        models.VenueOrder.team_id == team.team_id,
                        models.VenueOrder.environment == environment.value,
                        models.VenueOrder.account_id == account_id,
                        models.VenueOrder.venue == venue,
                        models.VenueOrder.venue_order_id == external_fill.order_id,
                    )
                )
                if venue_order is None:
                    # Older Freqtrade observations used a synthetic trade identity instead of
                    # the exchange order id. Bind only an exact, unique, recent fully-filled
                    # order; ambiguity remains unbound and therefore fail-closed in reconcile.
                    synthetic_candidates = session.scalars(
                        select(models.VenueOrder).where(
                            models.VenueOrder.team_id == team.team_id,
                            models.VenueOrder.environment == environment.value,
                            models.VenueOrder.account_id == account_id,
                            models.VenueOrder.venue == venue,
                            models.VenueOrder.instrument_id == instrument.instrument_id,
                            models.VenueOrder.order_intent_id.is_not(None),
                            models.VenueOrder.venue_order_id.like("freqtrade:%"),
                            models.VenueOrder.status == domain.VenueOrderStatus.FILLED.value,
                            models.VenueOrder.side == external_fill.side,
                            models.VenueOrder.ordered_quantity == external_fill.quantity,
                            models.VenueOrder.filled_quantity == external_fill.quantity,
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
                        select(models.VenueOrder)
                        .join(
                            models.OrderIntent,
                            models.VenueOrder.order_intent_id == models.OrderIntent.intent_id,
                        )
                        .where(
                            models.VenueOrder.team_id == team.team_id,
                            models.VenueOrder.environment == environment.value,
                            models.VenueOrder.account_id == account_id,
                            models.VenueOrder.venue == venue,
                            models.VenueOrder.instrument_id == instrument.instrument_id,
                            models.VenueOrder.order_intent_id.is_not(None),
                            models.VenueOrder.venue_order_id.like("UNKNOWN:%"),
                            models.VenueOrder.status == domain.VenueOrderStatus.UNKNOWN.value,
                            models.VenueOrder.side == external_fill.side,
                            models.VenueOrder.filled_quantity == 0,
                            models.VenueOrder.ordered_quantity >= external_fill.quantity,
                            models.OrderIntent.status == domain.OrderIntentStatus.UNKNOWN.value,
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
                        venue_order.status = domain.VenueOrderStatus.FILLED.value
                        venue_order.ordered_quantity = external_fill.quantity
                        venue_order.filled_quantity = external_fill.quantity
                        venue_order.observed_at = external_fill.executed_at
                        venue_order.updated_at = now
                if venue_order is not None and venue_order.order_intent_id is None:
                    confirmed_quantity = confirmed_fill_quantity_by_order.get(
                        external_fill.order_id, Decimal(0)
                    )
                    if confirmed_quantity > venue_order.ordered_quantity:
                        _reject(
                            f"{venue}_FACT_CONFLICT",
                            "external order fills exceed the observed order quantity",
                        )
                    if confirmed_quantity > venue_order.filled_quantity:
                        venue_order.filled_quantity = confirmed_quantity
                    if confirmed_quantity == venue_order.ordered_quantity:
                        venue_order.status = domain.VenueOrderStatus.FILLED.value
                    venue_order.observed_at = max(
                        venue_order.observed_at, external_fill.executed_at
                    )
                    venue_order.updated_at = now
                intent = (
                    session.get(models.OrderIntent, venue_order.order_intent_id)
                    if venue_order is not None and venue_order.order_intent_id is not None
                    else None
                )
                campaign_id = None if intent is None else intent.campaign_id
                if current_fill is None:
                    session.add(
                        models.VenueFill(
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
            self._recover_filled_freqtrade_protection_exit(
                session,
                actor_id=actor_id,
                team_id=team.team_id,
                account_id=account_id,
                venue=venue,
                environment=environment,
                instrument_id=instrument.instrument_id,
                position=position,
                external_fills=snapshot.fills,
                now=now,
            )
            bound_orders = session.scalars(
                select(models.VenueOrder)
                .where(
                    models.VenueOrder.team_id == team.team_id,
                    models.VenueOrder.environment == environment.value,
                    models.VenueOrder.account_id == account_id,
                    models.VenueOrder.venue == venue,
                    models.VenueOrder.instrument_id == instrument.instrument_id,
                    models.VenueOrder.order_intent_id.is_not(None),
                )
                .with_for_update()
            ).all()
            for bound_order in bound_orders:
                if bound_order.order_intent_id is None:
                    continue
                bound_intent = session.get(
                    models.OrderIntent, bound_order.order_intent_id, with_for_update=True
                )
                if bound_intent is None:
                    _reject(f"{venue}_ORDER_BINDING_INVALID", "bound order intent is missing")
                bound_campaign = session.get(
                    models.Campaign, bound_intent.campaign_id, with_for_update=True
                )
                if bound_campaign is None:
                    _reject(f"{venue}_ORDER_BINDING_INVALID", "bound order campaign is missing")
                if bound_campaign.team_id != team.team_id:
                    _reject(f"{venue}_ORDER_BINDING_INVALID", "bound order crossed team scope")
                intent_fills = session.scalars(
                    select(models.VenueFill).where(
                        models.VenueFill.team_id == team.team_id,
                        models.VenueFill.order_intent_id == bound_intent.intent_id,
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
                if bound_campaign.status == domain.CampaignStatus.CLOSED.value:
                    if bound_intent.status not in {
                        domain.OrderIntentStatus.FILLED.value,
                        domain.OrderIntentStatus.CANCELLED.value,
                        domain.OrderIntentStatus.REJECTED.value,
                    } or bound_order.status not in {
                        domain.VenueOrderStatus.FILLED.value,
                        domain.VenueOrderStatus.CANCELLED.value,
                        domain.VenueOrderStatus.REJECTED.value,
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
                    consume_add_unit(session, bound_intent)
                previous = bound_intent.status
                release_updated_intent = False
                terminal = {
                    domain.VenueOrderStatus.CANCELLED.value: domain.OrderIntentStatus.CANCELLED,
                    domain.VenueOrderStatus.REJECTED.value: domain.OrderIntentStatus.REJECTED,
                }
                if bound_order.status == domain.VenueOrderStatus.UNKNOWN.value:
                    bound_intent.status = domain.OrderIntentStatus.UNKNOWN.value
                    if bound_intent.reservation_id is not None:
                        reservation = session.get(
                            models.RiskReservation,
                            bound_intent.reservation_id,
                            with_for_update=True,
                        )
                        if reservation is not None:
                            reservation.status = domain.ReservationStatus.UNKNOWN.value
                            reservation.updated_at = now
                            reservation.version += 1
                    bound_campaign.status = domain.CampaignStatus.UNKNOWN.value
                elif bound_order.status in terminal and filled == 0:
                    release_zero_fill_in_session(
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
                            bound_order.status == domain.VenueOrderStatus.FILLED.value
                            and filled == bound_order.ordered_quantity
                        )
                    ):
                        bound_intent.status = domain.OrderIntentStatus.FILLED.value
                        bound_order.status = domain.VenueOrderStatus.FILLED.value
                    elif filled > 0 and bound_order.status not in terminal:
                        bound_intent.status = domain.OrderIntentStatus.PARTIALLY_FILLED.value
                        bound_order.status = domain.VenueOrderStatus.PARTIALLY_FILLED.value
                    elif bound_order.status in terminal:
                        bound_intent.status = terminal[bound_order.status].value
                    if filled > 0 and bound_intent.reservation_id is not None:
                        reservation = session.get(
                            models.RiskReservation,
                            bound_intent.reservation_id,
                            with_for_update=True,
                        )
                        if reservation is not None:
                            reservation.status = domain.ReservationStatus.OPEN.value
                            reservation.updated_at = now
                            reservation.version += 1
                    if filled > 0:
                        if bound_intent.kind in {
                            domain.IntentKind.INITIAL.value,
                            domain.IntentKind.ADD.value,
                        }:
                            if bound_campaign.status not in {
                                domain.CampaignStatus.CLOSING.value,
                                domain.CampaignStatus.REDUCING.value,
                            }:
                                bound_campaign.status = domain.CampaignStatus.OPEN.value
                        elif bound_intent.kind == domain.IntentKind.EXIT.value:
                            bound_campaign.status = domain.CampaignStatus.CLOSING.value
                        else:
                            bound_campaign.status = domain.CampaignStatus.REDUCING.value
                if previous != bound_intent.status:
                    if not release_updated_intent:
                        bound_intent.updated_at = now
                        bound_intent.version += 1
                    metrics.INTENT_TRANSITIONS.labels(previous, bound_intent.status).inc()
                bound_order.updated_at = now
                bound_campaign.updated_at = now

            funding_count = 0
            funding_campaign = session.scalar(
                select(models.Campaign)
                .where(
                    models.Campaign.team_id == team.team_id,
                    models.Campaign.account_id == account_id,
                    models.Campaign.venue == venue,
                    models.Campaign.environment == environment.value,
                    models.Campaign.instrument_id == instrument.instrument_id,
                    models.Campaign.status != domain.CampaignStatus.CLOSED.value,
                )
                .with_for_update()
            )
            for external_funding in snapshot.funding:
                current_funding = session.scalar(
                    select(models.FundingPayment).where(
                        models.FundingPayment.team_id == team.team_id,
                        models.FundingPayment.environment == environment.value,
                        models.FundingPayment.account_id == account_id,
                        models.FundingPayment.venue == venue,
                        models.FundingPayment.venue_payment_id == external_funding.payment_id,
                    )
                )
                if current_funding is None:
                    session.add(
                        models.FundingPayment(
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
                select(models.ProtectionOrder)
                .where(models.ProtectionOrder.position_id == position.position_id)
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
                    protection = models.ProtectionOrder(
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
                            domain.ProtectionStatus.ACTIVE.value
                            if covered
                            else domain.ProtectionStatus.UNKNOWN.value
                            if observed_protection is None
                            else domain.ProtectionStatus.DEGRADED.value
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
                        domain.ProtectionStatus.ACTIVE.value
                        if covered
                        else domain.ProtectionStatus.UNKNOWN.value
                        if observed_protection is None
                        else domain.ProtectionStatus.DEGRADED.value
                    )
                    protection.fully_covered = covered
                    protection.observed_at = now
                    protection.updated_at = now
            elif protection is not None:
                protection_order = session.scalar(
                    select(models.VenueOrder)
                    .where(
                        models.VenueOrder.environment == environment.value,
                        models.VenueOrder.account_id == account_id,
                        models.VenueOrder.venue == venue,
                        models.VenueOrder.venue_order_id == protection.venue_order_id,
                    )
                    .with_for_update()
                )
                observed_order_ids = {item.order_id for item in snapshot.orders}
                if protection_order is not None and protection_order.status in {
                    domain.VenueOrderStatus.SENT.value,
                    domain.VenueOrderStatus.PARTIALLY_FILLED.value,
                    domain.VenueOrderStatus.UNKNOWN.value,
                }:
                    still_open = protection_order.venue_order_id in observed_order_ids
                    if not still_open:
                        protection_order.status = domain.VenueOrderStatus.CANCELLED.value
                    protection_order.observed_at = snapshot.observed_at
                    protection_order.updated_at = now
                    if still_open:
                        protection.quantity = Decimal(0)
                        protection.status = domain.ProtectionStatus.DEGRADED.value
                        protection.fully_covered = False
                        protection.observed_at = snapshot.observed_at
                        protection.updated_at = now
                    else:
                        session.delete(protection)
                else:
                    session.delete(protection)
            self.transactions.audit(
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
