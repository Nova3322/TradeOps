from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import select

from trading_control_plane import domain, metrics, models, rejections
from trading_control_plane import execution_scope as scope_rules
from trading_control_plane.repositories.execution import find_position_for_scope
from trading_control_plane.service_component import ServiceComponent


class ReconciliationRiskService(ServiceComponent):
    def record_scope_reconciliation(
        self,
        execution_scope: str,
        actor_id: UUID,
        status: domain.ReconciliationStatus,
        differences: tuple[str, ...],
        *,
        now: datetime,
        campaign_id: UUID | None = None,
    ) -> UUID:
        if status in {domain.ReconciliationStatus.MATCH, domain.ReconciliationStatus.RESOLVED}:
            rejections.reject(
                "RECONCILIATION_STATUS_NOT_TRUSTED",
                "MATCH must be computed and RESOLVED requires a manual transition",
            )
        with self.database.session_factory.begin() as session:
            _environment, account_id, venue = scope_rules.scope_parts(execution_scope)
            team = self.transactions.require_role(session, actor_id, "reconcile", account_id, venue)
            if campaign_id is not None:
                campaign = session.get(models.Campaign, campaign_id)
                if campaign is None:
                    rejections.reject("CAMPAIGN_NOT_FOUND", "campaign does not exist")
                if campaign.team_id != team.team_id:
                    rejections.reject(
                        "TEAM_SCOPE_DENIED", "campaign is outside the active team scope"
                    )
            run = models.ReconciliationRun(
                team_id=team.team_id,
                execution_scope=execution_scope,
                campaign_id=campaign_id,
                status=status.value,
                is_computed=False,
                differences=list(differences),
                resolution_reason=None,
                actor_id=str(actor_id),
                correlation_id=uuid4(),
                started_at=now,
                completed_at=now,
            )
            session.add(run)
            session.flush()
            metrics.RECONCILIATION_RESULTS.labels(status.value).inc()
            return run.reconciliation_id

    def require_manual_reconciliation(
        self, reconciliation_id: UUID, actor_id: UUID, reason: str, *, now: datetime
    ) -> UUID:
        with self.database.session_factory.begin() as session:
            run = session.get(models.ReconciliationRun, reconciliation_id, with_for_update=True)
            if run is None:
                rejections.reject("RECONCILIATION_NOT_FOUND", "run does not exist")
            _environment, account_id, venue = scope_rules.scope_parts(run.execution_scope)
            self.transactions.require_role(
                session,
                actor_id,
                "reconcile",
                account_id,
                venue,
                team_id=run.team_id,
            )
            if run.status not in {
                domain.ReconciliationStatus.DIFFERENCE.value,
                domain.ReconciliationStatus.UNKNOWN.value,
            }:
                rejections.reject(
                    "RECONCILIATION_TRANSITION_INVALID",
                    "only DIFFERENCE or UNKNOWN may require manual handling",
                )
            run.status = domain.ReconciliationStatus.MANUAL_REQUIRED.value
            run.resolution_reason = reason
            run.completed_at = now
            return run.reconciliation_id

    def resolve_reconciliation(
        self, reconciliation_id: UUID, actor_id: UUID, reason: str, *, now: datetime
    ) -> UUID:
        with self.database.session_factory.begin() as session:
            run = session.get(models.ReconciliationRun, reconciliation_id, with_for_update=True)
            if run is None:
                rejections.reject("RECONCILIATION_NOT_FOUND", "run does not exist")
            _environment, account_id, venue = scope_rules.scope_parts(run.execution_scope)
            self.transactions.require_role(
                session,
                actor_id,
                "reconcile",
                account_id,
                venue,
                team_id=run.team_id,
            )
            if run.status != domain.ReconciliationStatus.MANUAL_REQUIRED.value:
                rejections.reject(
                    "RECONCILIATION_TRANSITION_INVALID",
                    "only MANUAL_REQUIRED may be resolved",
                )
            run.status = domain.ReconciliationStatus.RESOLVED.value
            run.resolution_reason = reason
            run.completed_at = now
            return run.reconciliation_id

    def reconciliation_status(self, reconciliation_id: UUID) -> domain.ReconciliationStatus:
        with self.database.session_factory() as session:
            run = session.get(models.ReconciliationRun, reconciliation_id)
            if run is None:
                rejections.reject("RECONCILIATION_NOT_FOUND", "run does not exist")
            return domain.ReconciliationStatus(run.status)

    def reconcile_scope(
        self,
        execution_scope: str,
        actor_id: UUID,
        *,
        now: datetime,
    ) -> UUID:
        environment, account_id, venue = scope_rules.scope_parts(execution_scope)
        with self.database.session_factory.begin() as session:
            team = self.transactions.require_role(session, actor_id, "reconcile", account_id, venue)
            policy = session.scalar(
                select(models.RiskPolicy).where(
                    models.RiskPolicy.team_id == team.team_id,
                    models.RiskPolicy.active,
                )
            )
            max_age = (
                timedelta(seconds=policy.max_fact_age_seconds)
                if policy is not None
                else timedelta(0)
            )
            campaigns = session.scalars(
                select(models.Campaign)
                .where(
                    models.Campaign.team_id == team.team_id,
                    models.Campaign.account_id == account_id,
                    models.Campaign.venue == venue,
                    models.Campaign.environment == environment.value,
                    models.Campaign.status != domain.CampaignStatus.CLOSED.value,
                )
                .order_by(models.Campaign.created_at, models.Campaign.campaign_id)
                .with_for_update()
            ).all()
            equity = session.scalar(
                select(models.AccountEquity)
                .where(
                    models.AccountEquity.team_id == team.team_id,
                    models.AccountEquity.account_id == account_id,
                    models.AccountEquity.venue == venue,
                    models.AccountEquity.environment == environment.value,
                )
                .with_for_update()
            )
            differences: list[str] = []
            unknown: list[str] = []
            if policy is None:
                unknown.append("RISK_POLICY_UNKNOWN")
            if equity is None or equity.fact_status != domain.FactStatus.KNOWN.value:
                unknown.append("ACCOUNT_EQUITY_UNKNOWN")
            elif scope_rules.fact_is_stale(equity.observed_at, now, max_age):
                unknown.append("ACCOUNT_EQUITY_STALE")

            protection_order_ids = set(
                session.scalars(
                    select(models.ProtectionOrder.venue_order_id)
                    .join(
                        models.Position,
                        models.ProtectionOrder.position_id == models.Position.position_id,
                    )
                    .where(
                        models.Position.team_id == team.team_id,
                        models.Position.account_id == account_id,
                        models.Position.venue == venue,
                        models.Position.environment == environment.value,
                    )
                ).all()
            )
            unbound_orders = session.scalars(
                select(models.VenueOrder).where(
                    models.VenueOrder.team_id == team.team_id,
                    models.VenueOrder.account_id == account_id,
                    models.VenueOrder.venue == venue,
                    models.VenueOrder.environment == environment.value,
                    models.VenueOrder.order_intent_id.is_(None),
                    models.VenueOrder.status.in_(
                        {
                            domain.VenueOrderStatus.SENT.value,
                            domain.VenueOrderStatus.PARTIALLY_FILLED.value,
                            domain.VenueOrderStatus.UNKNOWN.value,
                        }
                    ),
                )
            ).all()
            for unbound_order in unbound_orders:
                if unbound_order.venue_order_id in protection_order_ids:
                    continue
                if unbound_order.status == domain.VenueOrderStatus.UNKNOWN.value:
                    unknown.append(f"EXTERNAL_ORDER_UNKNOWN:{unbound_order.venue_order_id}")
                else:
                    differences.append(f"EXTERNAL_ORDER_UNBOUND:{unbound_order.venue_order_id}")

            active_instrument_ids = {campaign.instrument_id for campaign in campaigns}
            exchange_account = session.scalar(
                select(models.ExchangeAccount).where(
                    models.ExchangeAccount.team_id == team.team_id,
                    models.ExchangeAccount.account_id == account_id,
                    models.ExchangeAccount.venue == venue,
                    models.ExchangeAccount.environment == environment.value,
                )
            )
            hyperliquid_dexes = {
                str(item).lower()
                for item in (
                    []
                    if exchange_account is None
                    else exchange_account.freqtrade_hip3_dexes or []
                )
            }
            scope_positions = session.scalars(
                select(models.Position).where(
                    models.Position.team_id == team.team_id,
                    models.Position.account_id == account_id,
                    models.Position.venue == venue,
                    models.Position.environment == environment.value,
                )
            ).all()
            for scope_position in scope_positions:
                instrument = session.get(models.Instrument, scope_position.instrument_id)
                hip3_dex = (
                    None
                    if instrument is None or ":" not in instrument.symbol
                    else instrument.symbol.split(":", 1)[0].lower()
                )
                if (
                    venue == "HYPERLIQUID"
                    and hip3_dex is not None
                    and hip3_dex not in hyperliquid_dexes
                    and scope_position.fact_status == domain.FactStatus.KNOWN.value
                    and scope_position.quantity == 0
                ):
                    continue
                if scope_position.instrument_id not in active_instrument_ids:
                    if scope_position.fact_status != domain.FactStatus.KNOWN.value:
                        unknown.append(f"POSITION_UNKNOWN:{scope_position.instrument_id}")
                    elif scope_rules.fact_is_stale(scope_position.observed_at, now, max_age):
                        unknown.append(f"POSITION_STALE:{scope_position.instrument_id}")
                if (
                    scope_position.quantity != 0
                    and scope_position.instrument_id not in active_instrument_ids
                ):
                    differences.append(f"EXTERNAL_POSITION_UNBOUND:{scope_position.instrument_id}")

            for campaign in campaigns:
                scope_suffix = str(campaign.campaign_id)
                instrument = session.get(models.Instrument, campaign.instrument_id)
                if instrument is None:
                    unknown.append(f"INSTRUMENT_UNKNOWN:{scope_suffix}")
                elif equity is not None and equity.currency != instrument.collateral_currency:
                    differences.append(f"EQUITY_CURRENCY_MISMATCH:{scope_suffix}")
                intents = session.scalars(
                    select(models.OrderIntent)
                    .where(models.OrderIntent.campaign_id == campaign.campaign_id)
                    .order_by(models.OrderIntent.created_at, models.OrderIntent.intent_id)
                    .with_for_update()
                ).all()
                fills = session.scalars(
                    select(models.VenueFill).where(
                        models.VenueFill.campaign_id == campaign.campaign_id
                    )
                ).all()
                reservations = session.scalars(
                    select(models.RiskReservation)
                    .where(models.RiskReservation.campaign_id == campaign.campaign_id)
                    .with_for_update()
                ).all()
                if not intents:
                    differences.append(f"ORDER_INTENT_MISSING:{scope_suffix}")
                for reservation in reservations:
                    if reservation.status == domain.ReservationStatus.UNKNOWN.value:
                        unknown.append(f"RISK_RESERVATION_UNKNOWN:{reservation.reservation_id}")

                for intent in intents:
                    intent_fills = [
                        fill for fill in fills if fill.order_intent_id == intent.intent_id
                    ]
                    intent_fill_quantity = sum((fill.quantity for fill in intent_fills), Decimal(0))
                    intent_order = session.scalar(
                        select(models.VenueOrder)
                        .where(models.VenueOrder.order_intent_id == intent.intent_id)
                        .with_for_update()
                    )
                    order_required = intent.status in {
                        domain.OrderIntentStatus.SENT.value,
                        domain.OrderIntentStatus.PARTIALLY_FILLED.value,
                        domain.OrderIntentStatus.FILLED.value,
                        domain.OrderIntentStatus.UNKNOWN.value,
                    }
                    if intent_order is None and order_required:
                        differences.append(f"VENUE_ORDER_MISSING:{intent.intent_id}")
                    elif intent_order is not None:
                        if intent_order.venue != venue:
                            differences.append(f"VENUE_ORDER_SCOPE_MISMATCH:{intent.intent_id}")
                        if intent_order.filled_quantity != intent_fill_quantity:
                            differences.append(f"ORDER_FILL_MISMATCH:{intent.intent_id}")
                        if intent_order.status == domain.VenueOrderStatus.UNKNOWN.value:
                            unknown.append(f"VENUE_ORDER_UNKNOWN:{intent.intent_id}")
                        elif intent_order.status not in {
                            domain.VenueOrderStatus.FILLED.value,
                            domain.VenueOrderStatus.CANCELLED.value,
                            domain.VenueOrderStatus.REJECTED.value,
                        } and scope_rules.fact_is_stale(intent_order.observed_at, now, max_age):
                            unknown.append(f"VENUE_ORDER_STALE:{intent.intent_id}")
                    if intent_fill_quantity > intent.quantity:
                        differences.append(f"ORDER_INTENT_OVERFILLED:{intent.intent_id}")
                    if intent.status == domain.OrderIntentStatus.UNKNOWN.value:
                        unknown.append(f"ORDER_INTENT_UNKNOWN:{intent.intent_id}")
                    elif intent.status == domain.OrderIntentStatus.DISPATCHING.value:
                        unknown.append(f"ORDER_DISPATCH_UNRESOLVED:{intent.intent_id}")
                    if (
                        intent.status == domain.OrderIntentStatus.FILLED.value
                        and intent_order is not None
                        and intent_fill_quantity != intent_order.filled_quantity
                    ):
                        differences.append(f"INTENT_FILL_STATE_MISMATCH:{intent.intent_id}")

                position = find_position_for_scope(session, campaign, for_update=True)
                if position is None or position.fact_status != domain.FactStatus.KNOWN.value:
                    unknown.append(f"POSITION_UNKNOWN:{scope_suffix}")
                    continue
                if scope_rules.fact_is_stale(position.observed_at, now, max_age):
                    unknown.append(f"POSITION_STALE:{scope_suffix}")
                signed_fills = sum(
                    (fill.quantity if fill.side == "BUY" else -fill.quantity for fill in fills),
                    Decimal(0),
                )
                if signed_fills != position.quantity:
                    differences.append(f"POSITION_QUANTITY_MISMATCH:{scope_suffix}")
                if position.quantity != 0:
                    protection = session.scalar(
                        select(models.ProtectionOrder)
                        .where(models.ProtectionOrder.position_id == position.position_id)
                        .with_for_update()
                    )
                    if (
                        protection is None
                        or protection.status == domain.ProtectionStatus.UNKNOWN.value
                    ):
                        unknown.append(f"PROTECTION_UNKNOWN:{scope_suffix}")
                    elif scope_rules.fact_is_stale(protection.observed_at, now, max_age):
                        unknown.append(f"PROTECTION_STALE:{scope_suffix}")
                    elif (
                        protection.status != domain.ProtectionStatus.ACTIVE.value
                        or not protection.fully_covered
                        or protection.quantity < abs(position.quantity)
                    ):
                        differences.append(f"PROTECTION_INSUFFICIENT:{scope_suffix}")

            if unknown:
                status = domain.ReconciliationStatus.UNKNOWN
                result_differences = sorted(set(unknown + differences))
            elif differences:
                status = domain.ReconciliationStatus.DIFFERENCE
                result_differences = sorted(set(differences))
            else:
                status = domain.ReconciliationStatus.MATCH
                result_differences = []
            run = models.ReconciliationRun(
                team_id=team.team_id,
                execution_scope=execution_scope,
                campaign_id=None,
                status=status.value,
                is_computed=True,
                differences=result_differences,
                resolution_reason=None,
                actor_id=str(actor_id),
                correlation_id=uuid4(),
                started_at=now,
                completed_at=now,
            )
            session.add(run)
            session.flush()
            metrics.RECONCILIATION_RESULTS.labels(status.value).inc()
            return run.reconciliation_id

    def reconcile_campaign(
        self,
        campaign_id: UUID,
        execution_scope: str,
        actor_id: UUID,
        *,
        now: datetime,
    ) -> UUID:
        environment, account_id, venue = scope_rules.scope_parts(execution_scope)
        with self.database.session_factory() as session:
            campaign = session.get(models.Campaign, campaign_id)
            if campaign is None:
                rejections.reject("CAMPAIGN_NOT_FOUND", "campaign does not exist")
            self.transactions.require_role(
                session,
                actor_id,
                "reconcile",
                campaign.account_id,
                campaign.venue,
                team_id=campaign.team_id,
            )
            if (
                campaign.account_id != account_id
                or campaign.venue != venue
                or campaign.environment != environment.value
            ):
                rejections.reject(
                    "EXECUTION_SCOPE_MISMATCH", "campaign is outside reconciliation scope"
                )
        return self.reconcile_scope(execution_scope, actor_id, now=now)
