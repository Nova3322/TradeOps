from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from trading_control_plane import domain, models, notilt, rejections, request_context
from trading_control_plane import execution_scope as scope_rules
from trading_control_plane.service_component import ServiceComponent
from trading_control_plane.service_domains.accounts import ensure_exchange_account_reference
from trading_control_plane.service_domains.execution_facts import record_account_equity_observation


def capital_balance(
    session: Session,
    *,
    team_id: UUID,
    environment: str,
    endpoint_type: str,
    endpoint_id: str,
    venue: str,
    asset: str,
    lock: bool = False,
) -> models.AccountEquity:
    statement = select(models.AccountEquity).where(
        models.AccountEquity.team_id == team_id,
        models.AccountEquity.environment == environment,
        models.AccountEquity.account_id == endpoint_id,
        models.AccountEquity.venue == (venue if endpoint_type == "VENUE" else "VAULT"),
        models.AccountEquity.location_type == endpoint_type,
        models.AccountEquity.currency == asset,
    )
    if lock:
        statement = statement.with_for_update()
    fact = session.scalar(statement)
    if fact is None or fact.fact_status != domain.FactStatus.KNOWN.value:
        rejections.reject("CAPITAL_FACT_UNKNOWN", "source or destination capital fact is unknown")
    return fact


def assert_capital_scope_flat(
    session: Session,
    *,
    team_id: UUID,
    environment: str,
    account_id: str,
    venue: str,
    now: datetime,
) -> None:
    positions = session.scalars(
        select(models.Position).where(
            models.Position.team_id == team_id,
            models.Position.environment == environment,
            models.Position.account_id == account_id,
            models.Position.venue == venue,
        )
    ).all()
    if not positions:
        rejections.reject("CAPITAL_POSITION_UNKNOWN", "flat position facts are required")
    if any(item.fact_status != domain.FactStatus.KNOWN.value for item in positions):
        rejections.reject("CAPITAL_POSITION_UNKNOWN", "unknown position blocks capital transfer")
    policy = session.scalar(
        select(models.RiskPolicy).where(
            models.RiskPolicy.team_id == team_id,
            models.RiskPolicy.active,
        )
    )
    if policy is None or any(
        item.observed_at < now - timedelta(seconds=policy.max_fact_age_seconds)
        for item in positions
    ):
        rejections.reject("CAPITAL_POSITION_UNKNOWN", "fresh flat position facts are required")
    if any(item.quantity != 0 for item in positions):
        rejections.reject(
            "ACTIVE_POSITION_CAPITAL_RESCUE_FORBIDDEN",
            "capital transfer cannot rescue an active position",
        )
    campaigns = session.scalars(
        select(models.Campaign).where(
            models.Campaign.team_id == team_id,
            models.Campaign.environment == environment,
            models.Campaign.account_id == account_id,
            models.Campaign.venue == venue,
        )
    ).all()
    campaign_ids = [item.campaign_id for item in campaigns]
    if campaign_ids:
        active_intent = session.scalar(
            select(models.OrderIntent.intent_id)
            .where(
                models.OrderIntent.campaign_id.in_(campaign_ids),
                models.OrderIntent.status.in_(scope_rules.ACTIVE_INTENT_STATUSES),
            )
            .limit(1)
        )
        if active_intent is not None:
            rejections.reject(
                "CAPITAL_ORDER_UNRESOLVED",
                "active or unknown OrderIntent blocks capital transfer",
            )
    venue_order = session.scalar(
        select(models.VenueOrder.venue_order_fact_id)
        .where(
            models.VenueOrder.team_id == team_id,
            models.VenueOrder.environment == environment,
            models.VenueOrder.account_id == account_id,
            models.VenueOrder.venue == venue,
            models.VenueOrder.status.not_in(
                {
                    domain.VenueOrderStatus.FILLED.value,
                    domain.VenueOrderStatus.CANCELLED.value,
                    domain.VenueOrderStatus.REJECTED.value,
                }
            ),
        )
        .limit(1)
    )
    if venue_order is not None:
        rejections.reject(
            "CAPITAL_ORDER_UNRESOLVED",
            "active or unknown VenueOrder blocks capital transfer",
        )


class ReconciliationCapitalService(ServiceComponent):
    def record_capital_balance(
        self,
        *,
        actor_id: UUID,
        environment: domain.ExecutionEnvironment,
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
            rejections.reject("CAPITAL_LOCATION_INVALID", "capital location must be VAULT or VENUE")
        if observed_at > now:
            rejections.reject("FACT_TIME_INVALID", "capital observation cannot be in the future")
        if withdrawable_balance > available_balance or available_balance > equity:
            rejections.reject(
                "CAPITAL_BALANCE_INVALID",
                "withdrawable, available, and equity balances are inconsistent",
            )
        if valuation_price is not None and valuation_price <= 0:
            rejections.reject(
                "CAPITAL_VALUATION_INVALID", "capital valuation price must be positive"
            )
        if valuation_equity is not None and valuation_equity < 0:
            rejections.reject("CAPITAL_VALUATION_INVALID", "capital valuation cannot be negative")
        if (
            valuation_observed_at is not None
            and valuation_observed_at > now + scope_rules.MAX_FACT_CLOCK_SKEW
        ):
            rejections.reject("FACT_TIME_INVALID", "capital valuation cannot be in the future")
        if asset.upper() in notilt.USD_STABLE_ASSETS and valuation_equity is None:
            valuation_currency = "USD"
            valuation_price = Decimal(1)
            valuation_equity = equity
            valuation_observed_at = observed_at
        fact_venue = venue if location_type == "VENUE" else "VAULT"
        with self.database.session_factory.begin() as session:
            team = self.transactions.require_role(
                session,
                actor_id,
                "capital.fact.record",
                location_id if location_type == "VENUE" else None,
                venue if location_type == "VENUE" else None,
            )
            actor = session.get(models.User, actor_id)
            if environment is domain.ExecutionEnvironment.LIVE and not (
                (actor is not None and actor.principal_type == domain.PrincipalType.SERVICE.value)
                or request_context.current_api_client_context() is not None
            ):
                rejections.reject(
                    "CAPITAL_LIVE_FACT_DISABLED",
                    "LIVE capital facts require a configured read-only adapter",
                )
            if location_type == "VENUE":
                ensure_exchange_account_reference(
                    session,
                    team=team,
                    actor_id=actor_id,
                    account_id=location_id,
                    venue=venue,
                    environment=environment.value,
                    now=now,
                )
            fact = session.scalar(
                select(models.AccountEquity)
                .where(
                    models.AccountEquity.team_id == team.team_id,
                    models.AccountEquity.environment == environment.value,
                    models.AccountEquity.account_id == location_id,
                    models.AccountEquity.venue == fact_venue,
                    models.AccountEquity.currency == asset,
                )
                .with_for_update()
            )
            if fact is None:
                fact = models.AccountEquity(
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
                    fact_status=domain.FactStatus.KNOWN.value
                    if known
                    else domain.FactStatus.UNKNOWN.value,
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
                fact.fact_status = (
                    domain.FactStatus.KNOWN.value if known else domain.FactStatus.UNKNOWN.value
                )
                fact.observed_at = observed_at
                fact.updated_at = now
            record_account_equity_observation(session, fact, recorded_at=now)
            self.transactions.audit(
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

    def record_capital_scope_reconciliation(
        self,
        *,
        actor_id: UUID,
        environment: domain.ExecutionEnvironment,
        account_id: str,
        venue: str,
        now: datetime,
    ) -> UUID:
        with self.database.session_factory.begin() as session:
            team = self.transactions.require_role(
                session, actor_id, "capital.reconcile", account_id, venue
            )
            positions = session.scalars(
                select(models.Position).where(
                    models.Position.team_id == team.team_id,
                    models.Position.environment == environment.value,
                    models.Position.account_id == account_id,
                    models.Position.venue == venue,
                )
            ).all()
            orders = session.scalars(
                select(models.VenueOrder).where(
                    models.VenueOrder.team_id == team.team_id,
                    models.VenueOrder.environment == environment.value,
                    models.VenueOrder.account_id == account_id,
                    models.VenueOrder.venue == venue,
                )
            ).all()
            campaigns = session.scalars(
                select(models.Campaign).where(
                    models.Campaign.team_id == team.team_id,
                    models.Campaign.environment == environment.value,
                    models.Campaign.account_id == account_id,
                    models.Campaign.venue == venue,
                )
            ).all()
            campaign_ids = [item.campaign_id for item in campaigns]
            intents = (
                session.scalars(
                    select(models.OrderIntent).where(
                        models.OrderIntent.campaign_id.in_(campaign_ids)
                    )
                ).all()
                if campaign_ids
                else []
            )
            unknown = (
                not positions
                or any(item.fact_status != domain.FactStatus.KNOWN.value for item in positions)
                or any(item.status == domain.VenueOrderStatus.UNKNOWN.value for item in orders)
            )
            differences: list[str] = []
            if any(item.quantity != 0 for item in positions):
                differences.append("NONZERO_POSITION")
            if any(item.status in scope_rules.ACTIVE_INTENT_STATUSES for item in intents):
                differences.append("ACTIVE_OR_UNKNOWN_INTENT")
            if any(
                item.status
                not in {
                    domain.VenueOrderStatus.FILLED.value,
                    domain.VenueOrderStatus.CANCELLED.value,
                    domain.VenueOrderStatus.REJECTED.value,
                }
                for item in orders
            ):
                differences.append("ACTIVE_OR_UNKNOWN_VENUE_ORDER")
            status = (
                domain.ReconciliationStatus.UNKNOWN.value
                if unknown
                else (
                    domain.ReconciliationStatus.DIFFERENCE.value
                    if differences
                    else domain.ReconciliationStatus.MATCH.value
                )
            )
            run = models.ReconciliationRun(
                team_id=team.team_id,
                execution_scope=scope_rules.scope_key(environment.value, account_id, venue),
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

    def reconcile_capital_transfer(
        self, capital_transfer_id: UUID, actor_id: UUID, *, now: datetime
    ) -> str:
        with self.database.session_factory.begin() as session:
            transfer = session.get(
                models.CapitalTransfer, capital_transfer_id, with_for_update=True
            )
            if transfer is None:
                rejections.reject("CAPITAL_TRANSFER_NOT_FOUND", "capital transfer does not exist")
            team = self.transactions.require_role(
                session,
                actor_id,
                "capital.reconcile",
                transfer.account_id,
                transfer.venue,
                team_id=transfer.team_id,
            )
            authorization = session.get(
                models.TransferAuthorization, transfer.transfer_authorization_id
            )
            if authorization is None:
                rejections.reject(
                    "TRANSFER_AUTHORIZATION_NOT_FOUND", "transfer authorization is missing"
                )
            if (
                transfer.status == domain.CapitalTransferStatus.IN_FLIGHT.value
                and transfer.transport_state == "RELEASE_EXECUTION_CONFIRMED"
                and transfer.fee_amount is not None
            ):
                source_fact = capital_balance(
                    session,
                    team_id=team.team_id,
                    environment=authorization.environment,
                    endpoint_type=authorization.source_type,
                    endpoint_id=authorization.source_id,
                    venue=authorization.venue,
                    asset=authorization.asset,
                )
                destination_fact = capital_balance(
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
                    transfer.status = domain.CapitalTransferStatus.DESTINATION_CONFIRMED.value
                    transfer.version += 1
            differences: list[str] = []
            if transfer.status in {
                domain.CapitalTransferStatus.UNKNOWN.value,
                domain.CapitalTransferStatus.MANUAL_REQUIRED.value,
            }:
                result = domain.ReconciliationStatus.UNKNOWN.value
                differences.append("TRANSFER_OUTCOME_UNKNOWN")
            elif transfer.status in {
                domain.CapitalTransferStatus.SOURCE_RESERVED.value,
                domain.CapitalTransferStatus.SUBMITTED.value,
                domain.CapitalTransferStatus.IN_FLIGHT.value,
            }:
                result = "IN_FLIGHT"
            else:
                source = capital_balance(
                    session,
                    team_id=team.team_id,
                    environment=authorization.environment,
                    endpoint_type=authorization.source_type,
                    endpoint_id=authorization.source_id,
                    venue=authorization.venue,
                    asset=authorization.asset,
                )
                destination = capital_balance(
                    session,
                    team_id=team.team_id,
                    environment=authorization.environment,
                    endpoint_type=authorization.destination_type,
                    endpoint_id=authorization.destination_id,
                    venue=authorization.venue,
                    asset=authorization.asset,
                )
                if transfer.status == domain.CapitalTransferStatus.FAILED_SOURCE_RESTORED.value:
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
                    domain.ReconciliationStatus.DIFFERENCE.value
                    if differences
                    else domain.ReconciliationStatus.MATCH.value
                )
                if (
                    result == domain.ReconciliationStatus.MATCH.value
                    and transfer.status == domain.CapitalTransferStatus.DESTINATION_CONFIRMED.value
                ):
                    transfer.status = domain.CapitalTransferStatus.SETTLED.value
                    transfer.version += 1
            transfer.reconciliation_status = result
            transfer.reconciliation_details = differences
            transfer.reconciled_at = now
            transfer.updated_at = now
            self.transactions.audit(
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
