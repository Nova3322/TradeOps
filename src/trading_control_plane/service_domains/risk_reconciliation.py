from __future__ import annotations

from trading_control_plane.service_component import ServiceComponent

# The domain implementation intentionally consumes the explicit service_core export surface.
# ruff: noqa: F403, F405
from trading_control_plane.service_core import *


class ReconciliationRiskService(ServiceComponent):
    def record_scope_reconciliation(
        self,
        execution_scope: str,
        actor_id: UUID,
        status: ReconciliationStatus,
        differences: tuple[str, ...],
        *,
        now: datetime,
        campaign_id: UUID | None = None,
    ) -> UUID:
        if status in {ReconciliationStatus.MATCH, ReconciliationStatus.RESOLVED}:
            _reject(
                "RECONCILIATION_STATUS_NOT_TRUSTED",
                "MATCH must be computed and RESOLVED requires a manual transition",
            )
        with self.database.session_factory.begin() as session:
            _environment, account_id, venue = _scope_parts(execution_scope)
            team = self.transactions._require_role(
                session, actor_id, "reconcile", account_id, venue
            )
            if campaign_id is not None:
                campaign = session.get(Campaign, campaign_id)
                if campaign is None:
                    _reject("CAMPAIGN_NOT_FOUND", "campaign does not exist")
                if campaign.team_id != team.team_id:
                    _reject("TEAM_SCOPE_DENIED", "campaign is outside the active team scope")
            run = ReconciliationRun(
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
            RECONCILIATION_RESULTS.labels(status.value).inc()
            return run.reconciliation_id

    def require_manual_reconciliation(
        self, reconciliation_id: UUID, actor_id: UUID, reason: str, *, now: datetime
    ) -> UUID:
        with self.database.session_factory.begin() as session:
            run = session.get(ReconciliationRun, reconciliation_id, with_for_update=True)
            if run is None:
                _reject("RECONCILIATION_NOT_FOUND", "run does not exist")
            _environment, account_id, venue = _scope_parts(run.execution_scope)
            self.transactions._require_role(
                session,
                actor_id,
                "reconcile",
                account_id,
                venue,
                team_id=run.team_id,
            )
            if run.status not in {
                ReconciliationStatus.DIFFERENCE.value,
                ReconciliationStatus.UNKNOWN.value,
            }:
                _reject(
                    "RECONCILIATION_TRANSITION_INVALID",
                    "only DIFFERENCE or UNKNOWN may require manual handling",
                )
            run.status = ReconciliationStatus.MANUAL_REQUIRED.value
            run.resolution_reason = reason
            run.completed_at = now
            return run.reconciliation_id

    def resolve_reconciliation(
        self, reconciliation_id: UUID, actor_id: UUID, reason: str, *, now: datetime
    ) -> UUID:
        with self.database.session_factory.begin() as session:
            run = session.get(ReconciliationRun, reconciliation_id, with_for_update=True)
            if run is None:
                _reject("RECONCILIATION_NOT_FOUND", "run does not exist")
            _environment, account_id, venue = _scope_parts(run.execution_scope)
            self.transactions._require_role(
                session,
                actor_id,
                "reconcile",
                account_id,
                venue,
                team_id=run.team_id,
            )
            if run.status != ReconciliationStatus.MANUAL_REQUIRED.value:
                _reject(
                    "RECONCILIATION_TRANSITION_INVALID",
                    "only MANUAL_REQUIRED may be resolved",
                )
            run.status = ReconciliationStatus.RESOLVED.value
            run.resolution_reason = reason
            run.completed_at = now
            return run.reconciliation_id

    def reconciliation_status(self, reconciliation_id: UUID) -> ReconciliationStatus:
        with self.database.session_factory() as session:
            run = session.get(ReconciliationRun, reconciliation_id)
            if run is None:
                _reject("RECONCILIATION_NOT_FOUND", "run does not exist")
            return ReconciliationStatus(run.status)

    @staticmethod
    def _fact_is_stale(observed_at: datetime, now: datetime, max_age: timedelta) -> bool:
        return fact_is_stale(observed_at, now, max_age)

    def reconcile_scope(
        self,
        execution_scope: str,
        actor_id: UUID,
        *,
        now: datetime,
    ) -> UUID:
        environment, account_id, venue = _scope_parts(execution_scope)
        with self.database.session_factory.begin() as session:
            team = self.transactions._require_role(
                session, actor_id, "reconcile", account_id, venue
            )
            policy = session.scalar(
                select(RiskPolicy).where(
                    RiskPolicy.team_id == team.team_id,
                    RiskPolicy.active,
                )
            )
            max_age = (
                timedelta(seconds=policy.max_fact_age_seconds)
                if policy is not None
                else timedelta(0)
            )
            campaigns = session.scalars(
                select(Campaign)
                .where(
                    Campaign.team_id == team.team_id,
                    Campaign.account_id == account_id,
                    Campaign.venue == venue,
                    Campaign.environment == environment.value,
                    Campaign.status != CampaignStatus.CLOSED.value,
                )
                .order_by(Campaign.created_at, Campaign.campaign_id)
                .with_for_update()
            ).all()
            equity = session.scalar(
                select(AccountEquity)
                .where(
                    AccountEquity.team_id == team.team_id,
                    AccountEquity.account_id == account_id,
                    AccountEquity.venue == venue,
                    AccountEquity.environment == environment.value,
                )
                .with_for_update()
            )
            differences: list[str] = []
            unknown: list[str] = []
            if policy is None:
                unknown.append("RISK_POLICY_UNKNOWN")
            if equity is None or equity.fact_status != FactStatus.KNOWN.value:
                unknown.append("ACCOUNT_EQUITY_UNKNOWN")
            elif self._fact_is_stale(equity.observed_at, now, max_age):
                unknown.append("ACCOUNT_EQUITY_STALE")

            protection_order_ids = set(
                session.scalars(
                    select(ProtectionOrder.venue_order_id)
                    .join(Position, ProtectionOrder.position_id == Position.position_id)
                    .where(
                        Position.team_id == team.team_id,
                        Position.account_id == account_id,
                        Position.venue == venue,
                        Position.environment == environment.value,
                    )
                ).all()
            )
            unbound_orders = session.scalars(
                select(VenueOrder).where(
                    VenueOrder.team_id == team.team_id,
                    VenueOrder.account_id == account_id,
                    VenueOrder.venue == venue,
                    VenueOrder.environment == environment.value,
                    VenueOrder.order_intent_id.is_(None),
                    VenueOrder.status.in_(
                        {
                            VenueOrderStatus.SENT.value,
                            VenueOrderStatus.PARTIALLY_FILLED.value,
                            VenueOrderStatus.UNKNOWN.value,
                        }
                    ),
                )
            ).all()
            for unbound_order in unbound_orders:
                if unbound_order.venue_order_id in protection_order_ids:
                    continue
                if unbound_order.status == VenueOrderStatus.UNKNOWN.value:
                    unknown.append(f"EXTERNAL_ORDER_UNKNOWN:{unbound_order.venue_order_id}")
                else:
                    differences.append(f"EXTERNAL_ORDER_UNBOUND:{unbound_order.venue_order_id}")

            active_instrument_ids = {campaign.instrument_id for campaign in campaigns}
            scope_positions = session.scalars(
                select(Position).where(
                    Position.team_id == team.team_id,
                    Position.account_id == account_id,
                    Position.venue == venue,
                    Position.environment == environment.value,
                )
            ).all()
            for scope_position in scope_positions:
                if scope_position.instrument_id not in active_instrument_ids:
                    if scope_position.fact_status != FactStatus.KNOWN.value:
                        unknown.append(f"POSITION_UNKNOWN:{scope_position.instrument_id}")
                    elif self._fact_is_stale(scope_position.observed_at, now, max_age):
                        unknown.append(f"POSITION_STALE:{scope_position.instrument_id}")
                if (
                    scope_position.quantity != 0
                    and scope_position.instrument_id not in active_instrument_ids
                ):
                    differences.append(f"EXTERNAL_POSITION_UNBOUND:{scope_position.instrument_id}")

            for campaign in campaigns:
                scope_suffix = str(campaign.campaign_id)
                instrument = session.get(Instrument, campaign.instrument_id)
                if instrument is None:
                    unknown.append(f"INSTRUMENT_UNKNOWN:{scope_suffix}")
                elif equity is not None and equity.currency != instrument.collateral_currency:
                    differences.append(f"EQUITY_CURRENCY_MISMATCH:{scope_suffix}")
                intents = session.scalars(
                    select(OrderIntent)
                    .where(OrderIntent.campaign_id == campaign.campaign_id)
                    .order_by(OrderIntent.created_at, OrderIntent.intent_id)
                    .with_for_update()
                ).all()
                fills = session.scalars(
                    select(VenueFill).where(VenueFill.campaign_id == campaign.campaign_id)
                ).all()
                reservations = session.scalars(
                    select(RiskReservation)
                    .where(RiskReservation.campaign_id == campaign.campaign_id)
                    .with_for_update()
                ).all()
                if not intents:
                    differences.append(f"ORDER_INTENT_MISSING:{scope_suffix}")
                for reservation in reservations:
                    if reservation.status == ReservationStatus.UNKNOWN.value:
                        unknown.append(f"RISK_RESERVATION_UNKNOWN:{reservation.reservation_id}")

                for intent in intents:
                    intent_fills = [
                        fill for fill in fills if fill.order_intent_id == intent.intent_id
                    ]
                    intent_fill_quantity = sum((fill.quantity for fill in intent_fills), Decimal(0))
                    intent_order = session.scalar(
                        select(VenueOrder)
                        .where(VenueOrder.order_intent_id == intent.intent_id)
                        .with_for_update()
                    )
                    order_required = intent.status in {
                        OrderIntentStatus.SENT.value,
                        OrderIntentStatus.PARTIALLY_FILLED.value,
                        OrderIntentStatus.FILLED.value,
                        OrderIntentStatus.UNKNOWN.value,
                    }
                    if intent_order is None and order_required:
                        differences.append(f"VENUE_ORDER_MISSING:{intent.intent_id}")
                    elif intent_order is not None:
                        if intent_order.venue != venue:
                            differences.append(f"VENUE_ORDER_SCOPE_MISMATCH:{intent.intent_id}")
                        if intent_order.filled_quantity != intent_fill_quantity:
                            differences.append(f"ORDER_FILL_MISMATCH:{intent.intent_id}")
                        if intent_order.status == VenueOrderStatus.UNKNOWN.value:
                            unknown.append(f"VENUE_ORDER_UNKNOWN:{intent.intent_id}")
                        elif intent_order.status not in {
                            VenueOrderStatus.FILLED.value,
                            VenueOrderStatus.CANCELLED.value,
                            VenueOrderStatus.REJECTED.value,
                        } and self._fact_is_stale(intent_order.observed_at, now, max_age):
                            unknown.append(f"VENUE_ORDER_STALE:{intent.intent_id}")
                    if intent_fill_quantity > intent.quantity:
                        differences.append(f"ORDER_INTENT_OVERFILLED:{intent.intent_id}")
                    if intent.status == OrderIntentStatus.UNKNOWN.value:
                        unknown.append(f"ORDER_INTENT_UNKNOWN:{intent.intent_id}")
                    elif intent.status == OrderIntentStatus.DISPATCHING.value:
                        unknown.append(f"ORDER_DISPATCH_UNRESOLVED:{intent.intent_id}")
                    if (
                        intent.status == OrderIntentStatus.FILLED.value
                        and intent_order is not None
                        and intent_fill_quantity != intent_order.filled_quantity
                    ):
                        differences.append(f"INTENT_FILL_STATE_MISMATCH:{intent.intent_id}")

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
                if position is None or position.fact_status != FactStatus.KNOWN.value:
                    unknown.append(f"POSITION_UNKNOWN:{scope_suffix}")
                    continue
                if self._fact_is_stale(position.observed_at, now, max_age):
                    unknown.append(f"POSITION_STALE:{scope_suffix}")
                signed_fills = sum(
                    (fill.quantity if fill.side == "BUY" else -fill.quantity for fill in fills),
                    Decimal(0),
                )
                if signed_fills != position.quantity:
                    differences.append(f"POSITION_QUANTITY_MISMATCH:{scope_suffix}")
                if position.quantity != 0:
                    protection = session.scalar(
                        select(ProtectionOrder)
                        .where(ProtectionOrder.position_id == position.position_id)
                        .with_for_update()
                    )
                    if protection is None or protection.status == ProtectionStatus.UNKNOWN.value:
                        unknown.append(f"PROTECTION_UNKNOWN:{scope_suffix}")
                    elif self._fact_is_stale(protection.observed_at, now, max_age):
                        unknown.append(f"PROTECTION_STALE:{scope_suffix}")
                    elif (
                        protection.status != ProtectionStatus.ACTIVE.value
                        or not protection.fully_covered
                        or protection.quantity < abs(position.quantity)
                    ):
                        differences.append(f"PROTECTION_INSUFFICIENT:{scope_suffix}")

            if unknown:
                status = ReconciliationStatus.UNKNOWN
                result_differences = sorted(set(unknown + differences))
            elif differences:
                status = ReconciliationStatus.DIFFERENCE
                result_differences = sorted(set(differences))
            else:
                status = ReconciliationStatus.MATCH
                result_differences = []
            run = ReconciliationRun(
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
            RECONCILIATION_RESULTS.labels(status.value).inc()
            return run.reconciliation_id

    def reconcile_campaign(
        self,
        campaign_id: UUID,
        execution_scope: str,
        actor_id: UUID,
        *,
        now: datetime,
    ) -> UUID:
        environment, account_id, venue = _scope_parts(execution_scope)
        with self.database.session_factory() as session:
            campaign = session.get(Campaign, campaign_id)
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
            if (
                campaign.account_id != account_id
                or campaign.venue != venue
                or campaign.environment != environment.value
            ):
                _reject("EXECUTION_SCOPE_MISMATCH", "campaign is outside reconciliation scope")
        return self.reconcile_scope(execution_scope, actor_id, now=now)
