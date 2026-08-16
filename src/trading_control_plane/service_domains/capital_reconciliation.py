from __future__ import annotations

from trading_control_plane.service_component import ServiceComponent

# The domain implementation intentionally consumes the explicit service_core export surface.
# ruff: noqa: F403, F405
from trading_control_plane.service_core import *


class ReconciliationCapitalService(ServiceComponent):
    def record_capital_balance(
        self,
        *,
        actor_id: UUID,
        environment: ExecutionEnvironment,
        location_type: str,
        location_id: str,
        venue: str,
        equity: Decimal,
        available_balance: Decimal,
        withdrawable_balance: Decimal,
        asset: str,
        control_status: str,
        deposit_status: str,
        network: str | None,
        address_reference: str | None,
        known: bool,
        observed_at: datetime,
        now: datetime,
        valuation_currency: str | None = None,
        valuation_price: Decimal | None = None,
        valuation_equity: Decimal | None = None,
        valuation_observed_at: datetime | None = None,
    ) -> UUID:
        if location_type not in {"VAULT", "VENUE"}:
            _reject("CAPITAL_LOCATION_INVALID", "capital location must be VAULT or VENUE")
        if observed_at > now:
            _reject("FACT_TIME_INVALID", "capital observation cannot be in the future")
        if withdrawable_balance > available_balance or available_balance > equity:
            _reject(
                "CAPITAL_BALANCE_INVALID",
                "withdrawable, available, and equity balances are inconsistent",
            )
        if valuation_price is not None and valuation_price <= 0:
            _reject("CAPITAL_VALUATION_INVALID", "capital valuation price must be positive")
        if valuation_equity is not None and valuation_equity < 0:
            _reject("CAPITAL_VALUATION_INVALID", "capital valuation cannot be negative")
        if valuation_observed_at is not None and valuation_observed_at > now + MAX_FACT_CLOCK_SKEW:
            _reject("FACT_TIME_INVALID", "capital valuation cannot be in the future")
        if asset.upper() in USD_STABLE_ASSETS and valuation_equity is None:
            valuation_currency = "USD"
            valuation_price = Decimal(1)
            valuation_equity = equity
            valuation_observed_at = observed_at
        fact_venue = venue if location_type == "VENUE" else "VAULT"
        with self.database.session_factory.begin() as session:
            team = self.transactions._require_role(
                session,
                actor_id,
                "capital.fact.record",
                location_id if location_type == "VENUE" else None,
                venue if location_type == "VENUE" else None,
            )
            actor = session.get(User, actor_id)
            if environment is ExecutionEnvironment.LIVE and not (
                (actor is not None and actor.principal_type == PrincipalType.SERVICE.value)
                or current_api_client_context() is not None
            ):
                _reject(
                    "CAPITAL_LIVE_FACT_DISABLED",
                    "LIVE capital facts require a configured read-only adapter",
                )
            if location_type == "VENUE":
                self._ensure_exchange_account_reference(
                    session,
                    team=team,
                    actor_id=actor_id,
                    account_id=location_id,
                    venue=venue,
                    environment=environment.value,
                    now=now,
                )
            fact = session.scalar(
                select(AccountEquity)
                .where(
                    AccountEquity.team_id == team.team_id,
                    AccountEquity.environment == environment.value,
                    AccountEquity.account_id == location_id,
                    AccountEquity.venue == fact_venue,
                    AccountEquity.currency == asset,
                )
                .with_for_update()
            )
            if fact is None:
                fact = AccountEquity(
                    team_id=team.team_id,
                    account_id=location_id,
                    venue=fact_venue,
                    environment=environment.value,
                    equity=equity,
                    available_balance=available_balance,
                    withdrawable_balance=withdrawable_balance,
                    currency=asset,
                    location_type=location_type,
                    control_status=control_status,
                    deposit_status=deposit_status,
                    network=network,
                    address_reference=address_reference,
                    valuation_currency=valuation_currency,
                    valuation_price=valuation_price,
                    valuation_equity=valuation_equity,
                    valuation_observed_at=valuation_observed_at,
                    fact_status=FactStatus.KNOWN.value if known else FactStatus.UNKNOWN.value,
                    observed_at=observed_at,
                    updated_at=now,
                )
                session.add(fact)
                session.flush()
            else:
                fact.equity = equity
                fact.available_balance = available_balance
                fact.withdrawable_balance = withdrawable_balance
                fact.currency = asset
                fact.location_type = location_type
                fact.control_status = control_status
                fact.deposit_status = deposit_status
                fact.network = network
                fact.address_reference = address_reference
                fact.valuation_currency = valuation_currency
                fact.valuation_price = valuation_price
                fact.valuation_equity = valuation_equity
                fact.valuation_observed_at = valuation_observed_at
                fact.fact_status = FactStatus.KNOWN.value if known else FactStatus.UNKNOWN.value
                fact.observed_at = observed_at
                fact.updated_at = now
            self._record_account_equity_observation(session, fact, recorded_at=now)
            self.transactions._audit(
                session,
                actor_id=str(actor_id),
                event_type="CAPITAL_BALANCE_RECORDED",
                object_type="AccountEquity",
                object_id=fact.account_equity_id,
                reason=f"{location_type}:{fact.fact_status}",
                correlation_id=uuid4(),
                object_version=1,
                now=now,
            )
            return fact.account_equity_id

    @staticmethod
    def _capital_balance(
        session: Session,
        *,
        team_id: UUID,
        environment: str,
        endpoint_type: str,
        endpoint_id: str,
        venue: str,
        asset: str,
        lock: bool = False,
    ) -> AccountEquity:
        statement = select(AccountEquity).where(
            AccountEquity.team_id == team_id,
            AccountEquity.environment == environment,
            AccountEquity.account_id == endpoint_id,
            AccountEquity.venue == (venue if endpoint_type == "VENUE" else "VAULT"),
            AccountEquity.location_type == endpoint_type,
            AccountEquity.currency == asset,
        )
        if lock:
            statement = statement.with_for_update()
        fact = session.scalar(statement)
        if fact is None or fact.fact_status != FactStatus.KNOWN.value:
            _reject("CAPITAL_FACT_UNKNOWN", "source or destination capital fact is unknown")
        return fact

    def record_capital_scope_reconciliation(
        self,
        *,
        actor_id: UUID,
        environment: ExecutionEnvironment,
        account_id: str,
        venue: str,
        now: datetime,
    ) -> UUID:
        with self.database.session_factory.begin() as session:
            team = self.transactions._require_role(
                session, actor_id, "capital.reconcile", account_id, venue
            )
            positions = session.scalars(
                select(Position).where(
                    Position.team_id == team.team_id,
                    Position.environment == environment.value,
                    Position.account_id == account_id,
                    Position.venue == venue,
                )
            ).all()
            orders = session.scalars(
                select(VenueOrder).where(
                    VenueOrder.team_id == team.team_id,
                    VenueOrder.environment == environment.value,
                    VenueOrder.account_id == account_id,
                    VenueOrder.venue == venue,
                )
            ).all()
            campaigns = session.scalars(
                select(Campaign).where(
                    Campaign.team_id == team.team_id,
                    Campaign.environment == environment.value,
                    Campaign.account_id == account_id,
                    Campaign.venue == venue,
                )
            ).all()
            campaign_ids = [item.campaign_id for item in campaigns]
            intents = (
                session.scalars(
                    select(OrderIntent).where(OrderIntent.campaign_id.in_(campaign_ids))
                ).all()
                if campaign_ids
                else []
            )
            unknown = (
                not positions
                or any(item.fact_status != FactStatus.KNOWN.value for item in positions)
                or any(item.status == VenueOrderStatus.UNKNOWN.value for item in orders)
            )
            differences: list[str] = []
            if any(item.quantity != 0 for item in positions):
                differences.append("NONZERO_POSITION")
            if any(item.status in ACTIVE_INTENT_STATUSES for item in intents):
                differences.append("ACTIVE_OR_UNKNOWN_INTENT")
            if any(
                item.status
                not in {
                    VenueOrderStatus.FILLED.value,
                    VenueOrderStatus.CANCELLED.value,
                    VenueOrderStatus.REJECTED.value,
                }
                for item in orders
            ):
                differences.append("ACTIVE_OR_UNKNOWN_VENUE_ORDER")
            status = (
                ReconciliationStatus.UNKNOWN.value
                if unknown
                else (
                    ReconciliationStatus.DIFFERENCE.value
                    if differences
                    else ReconciliationStatus.MATCH.value
                )
            )
            run = ReconciliationRun(
                team_id=team.team_id,
                execution_scope=_scope_key(environment.value, account_id, venue),
                campaign_id=None,
                status=status,
                is_computed=True,
                differences=differences,
                resolution_reason=None,
                actor_id=str(actor_id),
                correlation_id=uuid4(),
                started_at=now,
                completed_at=now,
            )
            session.add(run)
            session.flush()
            return run.reconciliation_id

    @staticmethod
    def _assert_capital_scope_flat(
        session: Session,
        *,
        team_id: UUID,
        environment: str,
        account_id: str,
        venue: str,
        now: datetime,
    ) -> None:
        positions = session.scalars(
            select(Position).where(
                Position.team_id == team_id,
                Position.environment == environment,
                Position.account_id == account_id,
                Position.venue == venue,
            )
        ).all()
        if not positions:
            _reject("CAPITAL_POSITION_UNKNOWN", "flat position facts are required")
        if any(item.fact_status != FactStatus.KNOWN.value for item in positions):
            _reject("CAPITAL_POSITION_UNKNOWN", "unknown position blocks capital transfer")
        policy = session.scalar(
            select(RiskPolicy).where(
                RiskPolicy.team_id == team_id,
                RiskPolicy.active,
            )
        )
        if policy is None or any(
            item.observed_at < now - timedelta(seconds=policy.max_fact_age_seconds)
            for item in positions
        ):
            _reject("CAPITAL_POSITION_UNKNOWN", "fresh flat position facts are required")
        if any(item.quantity != 0 for item in positions):
            _reject(
                "ACTIVE_POSITION_CAPITAL_RESCUE_FORBIDDEN",
                "capital transfer cannot rescue an active position",
            )
        campaigns = session.scalars(
            select(Campaign).where(
                Campaign.team_id == team_id,
                Campaign.environment == environment,
                Campaign.account_id == account_id,
                Campaign.venue == venue,
            )
        ).all()
        campaign_ids = [item.campaign_id for item in campaigns]
        if campaign_ids:
            active_intent = session.scalar(
                select(OrderIntent.intent_id)
                .where(
                    OrderIntent.campaign_id.in_(campaign_ids),
                    OrderIntent.status.in_(ACTIVE_INTENT_STATUSES),
                )
                .limit(1)
            )
            if active_intent is not None:
                _reject(
                    "CAPITAL_ORDER_UNRESOLVED",
                    "active or unknown OrderIntent blocks capital transfer",
                )
        venue_order = session.scalar(
            select(VenueOrder.venue_order_fact_id)
            .where(
                VenueOrder.team_id == team_id,
                VenueOrder.environment == environment,
                VenueOrder.account_id == account_id,
                VenueOrder.venue == venue,
                VenueOrder.status.not_in(
                    {
                        VenueOrderStatus.FILLED.value,
                        VenueOrderStatus.CANCELLED.value,
                        VenueOrderStatus.REJECTED.value,
                    }
                ),
            )
            .limit(1)
        )
        if venue_order is not None:
            _reject(
                "CAPITAL_ORDER_UNRESOLVED",
                "active or unknown VenueOrder blocks capital transfer",
            )

    def reconcile_capital_transfer(
        self, capital_transfer_id: UUID, actor_id: UUID, *, now: datetime
    ) -> str:
        with self.database.session_factory.begin() as session:
            transfer = session.get(CapitalTransfer, capital_transfer_id, with_for_update=True)
            if transfer is None:
                _reject("CAPITAL_TRANSFER_NOT_FOUND", "capital transfer does not exist")
            team = self.transactions._require_role(
                session,
                actor_id,
                "capital.reconcile",
                transfer.account_id,
                transfer.venue,
                team_id=transfer.team_id,
            )
            authorization = session.get(TransferAuthorization, transfer.transfer_authorization_id)
            if authorization is None:
                _reject("TRANSFER_AUTHORIZATION_NOT_FOUND", "transfer authorization is missing")
            if (
                transfer.status == CapitalTransferStatus.IN_FLIGHT.value
                and transfer.transport_state == "RELEASE_EXECUTION_CONFIRMED"
                and transfer.fee_amount is not None
            ):
                source_fact = self._capital_balance(
                    session,
                    team_id=team.team_id,
                    environment=authorization.environment,
                    endpoint_type=authorization.source_type,
                    endpoint_id=authorization.source_id,
                    venue=authorization.venue,
                    asset=authorization.asset,
                )
                destination_fact = self._capital_balance(
                    session,
                    team_id=team.team_id,
                    environment=authorization.environment,
                    endpoint_type=authorization.destination_type,
                    endpoint_id=authorization.destination_id,
                    venue=authorization.venue,
                    asset=authorization.asset,
                )
                expected_source = (
                    transfer.source_balance_before
                    - authorization.min_received
                    - transfer.fee_amount
                )
                expected_destination = (
                    transfer.destination_balance_before + authorization.min_received
                )
                if (
                    source_fact.observed_at >= transfer.observed_at
                    and destination_fact.observed_at >= transfer.observed_at
                    and source_fact.available_balance == expected_source
                    and destination_fact.available_balance == expected_destination
                ):
                    transfer.net_received = authorization.min_received
                    transfer.status = CapitalTransferStatus.DESTINATION_CONFIRMED.value
                    transfer.version += 1
            differences: list[str] = []
            if transfer.status in {
                CapitalTransferStatus.UNKNOWN.value,
                CapitalTransferStatus.MANUAL_REQUIRED.value,
            }:
                result = ReconciliationStatus.UNKNOWN.value
                differences.append("TRANSFER_OUTCOME_UNKNOWN")
            elif transfer.status in {
                CapitalTransferStatus.SOURCE_RESERVED.value,
                CapitalTransferStatus.SUBMITTED.value,
                CapitalTransferStatus.IN_FLIGHT.value,
            }:
                result = "IN_FLIGHT"
            else:
                source = self._capital_balance(
                    session,
                    team_id=team.team_id,
                    environment=authorization.environment,
                    endpoint_type=authorization.source_type,
                    endpoint_id=authorization.source_id,
                    venue=authorization.venue,
                    asset=authorization.asset,
                )
                destination = self._capital_balance(
                    session,
                    team_id=team.team_id,
                    environment=authorization.environment,
                    endpoint_type=authorization.destination_type,
                    endpoint_id=authorization.destination_id,
                    venue=authorization.venue,
                    asset=authorization.asset,
                )
                if transfer.status == CapitalTransferStatus.FAILED_SOURCE_RESTORED.value:
                    if source.available_balance < transfer.source_balance_before:
                        differences.append("SOURCE_NOT_RESTORED")
                else:
                    if transfer.net_received is None:
                        differences.append("DESTINATION_NET_UNKNOWN")
                    else:
                        expected_source_debit = transfer.net_received + (
                            transfer.fee_amount or Decimal(0)
                        )
                        if source.available_balance > (
                            transfer.source_balance_before - expected_source_debit
                        ):
                            differences.append("SOURCE_DEBIT_NOT_CONFIRMED")
                        if destination.available_balance < (
                            transfer.destination_balance_before + transfer.net_received
                        ):
                            differences.append("DESTINATION_CREDIT_NOT_CONFIRMED")
                        if source.observed_at < transfer.observed_at:
                            differences.append("SOURCE_FACT_STALE")
                        if destination.observed_at < transfer.observed_at:
                            differences.append("DESTINATION_FACT_STALE")
                result = (
                    ReconciliationStatus.DIFFERENCE.value
                    if differences
                    else ReconciliationStatus.MATCH.value
                )
                if (
                    result == ReconciliationStatus.MATCH.value
                    and transfer.status == CapitalTransferStatus.DESTINATION_CONFIRMED.value
                ):
                    transfer.status = CapitalTransferStatus.SETTLED.value
                    transfer.version += 1
            transfer.reconciliation_status = result
            transfer.reconciliation_details = differences
            transfer.reconciled_at = now
            transfer.updated_at = now
            self.transactions._audit(
                session,
                actor_id=str(actor_id),
                event_type="CAPITAL_TRANSFER_RECONCILED",
                object_type="CapitalTransfer",
                object_id=transfer.capital_transfer_id,
                reason=result,
                correlation_id=transfer.correlation_id,
                object_version=transfer.version,
                now=now,
            )
            return result
