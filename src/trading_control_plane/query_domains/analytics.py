from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from hmac import compare_digest
from typing import Any
from uuid import UUID

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from trading_control_plane import domain, models, notilt
from trading_control_plane.analytics import (
    ANALYTICS_DATASET_VERSION,
    CRYPTO_CALENDAR,
    PERIODS_PER_YEAR,
    AnalyticsDataset,
    AnalyticsScope,
    CanonicalFill,
    CanonicalOrder,
    Cashflow,
    NavPoint,
    PositionSnapshot,
    ReturnPoint,
    deduplicate_fills,
    derive_24_7_returns,
)
from trading_control_plane.query_component import QueryComponent


class AnalyticsQueries(QueryComponent):
    @staticmethod
    def _report_summary(report: models.AnalyticsReport) -> dict[str, Any]:
        return {
            "report_id": str(report.report_id),
            "engine": report.engine,
            "library": report.library_name,
            "library_version": report.library_version,
            "dataset_version": report.dataset_version,
            "status": report.status,
            "workspace_id": str(report.workspace_id),
            "team_id": str(report.team_id),
            "environment": report.environment,
            "generation": report.generation,
            "account_ids": report.account_ids,
            "venues": report.venues,
            "account_scopes": report.account_scopes,
            "from_time": report.from_time.astimezone(UTC).isoformat(),
            "to_time": report.to_time.astimezone(UTC).isoformat(),
            "generated_at": report.generated_at.astimezone(UTC).isoformat(),
            "metrics": report.metrics,
            "chart_count": report.chart_count,
            "coverage": report.coverage,
            "metadata": report.report_metadata,
            "artifact": {
                "content_type": "text/html; charset=utf-8",
                "sha256": report.artifact_sha256,
                "size_bytes": len(report.artifact_html.encode("utf-8")),
                "view_url": f"/api/results/reports/{report.report_id}/artifact",
                "download_url": f"/api/results/reports/{report.report_id}/download",
            },
        }

    def analytics_report(self, user_id: UUID, report_id: UUID) -> dict[str, Any]:
        workspace_id, team_id = self.active_scope_ids(user_id)
        with self.database.session_factory() as session:
            report = session.scalar(
                select(models.AnalyticsReport).where(
                    models.AnalyticsReport.report_id == report_id,
                    models.AnalyticsReport.workspace_id == workspace_id,
                    models.AnalyticsReport.team_id == team_id,
                )
            )
            if report is None:
                raise domain.DomainRejected(
                    "ANALYTICS_REPORT_NOT_FOUND",
                    "report is outside the active Workspace and Team scope",
                )
            if report.environment in {"TESTNET", "LIVE"}:
                exact_scopes = report.account_scopes
                if not exact_scopes or any(
                    not isinstance(item, dict)
                    or not isinstance(item.get("account_id"), str)
                    or not isinstance(item.get("venue"), str)
                    for item in exact_scopes
                ):
                    raise domain.DomainRejected(
                        "ANALYTICS_ACCOUNT_SCOPE_UNAVAILABLE",
                        "report does not contain verified exact account and venue scopes",
                    )
                if any(
                    not self.service.can_user(
                        user_id,
                        "view",
                        item["account_id"],
                        item["venue"],
                    )
                    for item in exact_scopes
                ):
                    raise domain.DomainRejected(
                        "ANALYTICS_ACCOUNT_SCOPE_DENIED",
                        "report account and venue scope is outside current user RBAC",
                    )
            return self._report_summary(report)

    def analytics_report_artifact(self, user_id: UUID, report_id: UUID) -> tuple[str, str]:
        self.analytics_report(user_id, report_id)
        with self.database.session_factory() as session:
            report = session.get(models.AnalyticsReport, report_id)
            assert report is not None
            actual = hashlib.sha256(report.artifact_html.encode("utf-8")).hexdigest()
            if not compare_digest(actual, report.artifact_sha256):
                raise domain.DomainRejected(
                    "ANALYTICS_ARTIFACT_INTEGRITY_FAILED",
                    "persisted report artifact failed integrity validation",
                )
            return report.artifact_html, report.engine.lower()

    @staticmethod
    def _validate_request(
        environment: str,
        from_time: datetime | None,
        to_time: datetime | None,
    ) -> tuple[datetime, datetime]:
        if environment not in {"TESTNET", "LIVE"}:
            raise domain.DomainRejected(
                "ANALYTICS_ENVIRONMENT_INVALID",
                "analytics history requires TESTNET or LIVE",
            )
        if from_time is None or to_time is None:
            raise domain.DomainRejected(
                "ANALYTICS_TIME_BOUNDARY_REQUIRED",
                "QuantStats requires explicit from_time and to_time boundaries",
            )
        if (
            from_time.tzinfo is None
            or from_time.utcoffset() is None
            or to_time.tzinfo is None
            or to_time.utcoffset() is None
        ):
            raise domain.DomainRejected(
                "ANALYTICS_TIMEZONE_REQUIRED",
                "QuantStats boundaries must be timezone-aware",
            )
        start = from_time.astimezone(UTC)
        end = to_time.astimezone(UTC)
        if start >= end:
            raise domain.DomainRejected(
                "ANALYTICS_TIME_RANGE_INVALID", "from_time must be earlier than to_time"
            )
        return start, end

    def analytics_report_options(self, user_id: UUID) -> dict[str, Any]:
        workspace_id, team_id = self.active_scope_ids(user_id)
        with self.database.session_factory() as session:
            team = session.get(models.Team, team_id)
            workspace = session.get(models.Workspace, workspace_id)
            assert team is not None and workspace is not None
            accounts = [
                account
                for account in session.scalars(
                    select(models.ExchangeAccount)
                    .where(models.ExchangeAccount.team_id == team_id, models.ExchangeAccount.active)
                    .order_by(models.ExchangeAccount.venue, models.ExchangeAccount.account_id)
                ).all()
                if self.service.can_user(user_id, "view", account.account_id, account.venue)
            ]
            return {
                "scope": {
                    "workspace_id": str(workspace_id),
                    "workspace_name": workspace.name,
                    "team_id": str(team_id),
                    "team_name": team.name,
                },
                "current_trading_mode": team.execution_mode,
                "environments": ["TESTNET", "LIVE"],
                "accounts": [
                    {
                        "account_id": item.account_id,
                        "venue": item.venue,
                        "label": item.label,
                        "environment": item.environment,
                    }
                    for item in accounts
                ],
                "dataset_version": ANALYTICS_DATASET_VERSION,
                "calendar": CRYPTO_CALENDAR,
            }

    def analytics_dataset(
        self,
        user_id: UUID,
        environment: str,
        *,
        account_id: str | None,
        venue: str | None,
        generation: int | None,
        from_time: datetime | None,
        to_time: datetime | None,
    ) -> AnalyticsDataset:
        start, end = self._validate_request(environment, from_time, to_time)
        workspace_id, team_id = self.active_scope_ids(user_id)
        with self.database.session_factory() as session:
            team = session.get(models.Team, team_id)
            if team is None or not team.active:
                raise domain.DomainRejected("TEAM_SCOPE_DENIED", "active Team is unavailable")
            return self._venue_dataset(
                session,
                user_id=user_id,
                workspace_id=workspace_id,
                team=team,
                environment=environment,
                account_id=account_id,
                venue=venue,
                generation=generation,
                start=start,
                end=end,
            )

    def _venue_dataset(
        self,
        session: Session,
        *,
        user_id: UUID,
        workspace_id: UUID,
        team: models.Team,
        environment: str,
        account_id: str | None,
        venue: str | None,
        generation: int | None,
        start: datetime,
        end: datetime,
    ) -> AnalyticsDataset:
        if generation is not None:
            raise domain.DomainRejected(
                "ANALYTICS_GENERATION_FORBIDDEN", "TESTNET and LIVE history has no generation"
            )
        account_query = select(models.ExchangeAccount).where(
            models.ExchangeAccount.team_id == team.team_id,
            models.ExchangeAccount.environment == environment,
            models.ExchangeAccount.active,
        )
        if account_id is not None:
            account_query = account_query.where(models.ExchangeAccount.account_id == account_id)
        if venue is not None:
            account_query = account_query.where(models.ExchangeAccount.venue == venue.upper())
        accounts = [
            item
            for item in session.scalars(
                account_query.order_by(
                    models.ExchangeAccount.venue, models.ExchangeAccount.account_id
                )
            ).all()
            if self.service.can_user(user_id, "view", item.account_id, item.venue)
        ]
        if not accounts:
            raise domain.DomainRejected(
                "ANALYTICS_ACCOUNT_SCOPE_EMPTY",
                "no authorized requested-environment account is available for this report",
            )
        if account_id is not None and not any(item.account_id == account_id for item in accounts):
            raise domain.DomainRejected(
                "ANALYTICS_ACCOUNT_SCOPE_DENIED", "account is outside the authorized Team scope"
            )
        account_keys = {(item.account_id, item.venue) for item in accounts}
        observations = session.scalars(
            select(models.AccountEquityObservation)
            .where(
                models.AccountEquityObservation.team_id == team.team_id,
                models.AccountEquityObservation.environment == environment,
                models.AccountEquityObservation.location_type == "VENUE",
                tuple_(
                    models.AccountEquityObservation.account_id,
                    models.AccountEquityObservation.venue,
                ).in_(account_keys),
                models.AccountEquityObservation.observed_at >= start,
                models.AccountEquityObservation.observed_at <= end,
            )
            .order_by(
                models.AccountEquityObservation.observed_at,
                models.AccountEquityObservation.observation_id,
            )
        ).all()
        nav = self._aggregate_venue_nav(observations)
        external_cashflows = self._venue_external_cashflows(
            session,
            team_id=team.team_id,
            environment=environment,
            account_keys=account_keys,
            start=start,
            end=end,
        )
        returns = derive_24_7_returns(
            nav_series=nav,
            external_cashflows=external_cashflows,
            from_time=start,
            to_time=end,
        )
        instruments = {
            item.instrument_id: item for item in session.scalars(select(models.Instrument)).all()
        }
        position_facts = session.scalars(
            select(models.Position)
            .where(
                models.Position.team_id == team.team_id,
                models.Position.environment == environment,
                tuple_(models.Position.account_id, models.Position.venue).in_(account_keys),
                models.Position.observed_at >= start,
                models.Position.observed_at <= end,
            )
            .order_by(models.Position.observed_at, models.Position.position_id)
        ).all()
        if any(item.fact_status != "KNOWN" for item in position_facts):
            raise domain.DomainRejected(
                "ANALYTICS_POSITION_VALUATION_MISSING",
                "venue position valuation is unknown",
            )
        positions: list[PositionSnapshot] = []
        for position_fact in position_facts:
            instrument = instruments.get(position_fact.instrument_id)
            if instrument is None or position_fact.mark_price <= 0:
                raise domain.DomainRejected(
                    "ANALYTICS_POSITION_VALUATION_MISSING",
                    "venue position instrument or mark price is missing",
                )
            market_value = (
                position_fact.quantity * position_fact.mark_price * instrument.contract_multiplier
            )
            positions.append(
                PositionSnapshot(
                    account_id=position_fact.account_id,
                    venue=position_fact.venue,
                    environment=environment,
                    observed_at=position_fact.observed_at,
                    symbol=instrument.symbol,
                    signed_quantity=position_fact.quantity,
                    mark_price=position_fact.mark_price,
                    market_value=market_value,
                    gross_exposure=abs(market_value),
                    net_exposure=market_value,
                )
            )
        fill_facts = session.scalars(
            select(models.VenueFill)
            .where(
                models.VenueFill.team_id == team.team_id,
                models.VenueFill.environment == environment,
                tuple_(models.VenueFill.account_id, models.VenueFill.venue).in_(account_keys),
                models.VenueFill.executed_at >= start,
                models.VenueFill.executed_at <= end,
            )
            .order_by(models.VenueFill.executed_at, models.VenueFill.venue_fill_fact_id)
        ).all()
        fills: list[CanonicalFill] = []
        for fill_fact in fill_facts:
            instrument = instruments.get(fill_fact.instrument_id)
            if instrument is None:
                raise domain.DomainRejected(
                    "ANALYTICS_INSTRUMENT_MISSING", "venue Fill instrument is missing"
                )
            fills.append(
                CanonicalFill(
                    account_id=fill_fact.account_id,
                    venue=fill_fact.venue,
                    environment=environment,
                    symbol=instrument.symbol,
                    fill_id=fill_fact.venue_fill_id,
                    order_id=(
                        str(fill_fact.order_intent_id)
                        if fill_fact.order_intent_id is not None
                        else fill_fact.venue_fill_id
                    ),
                    signed_amount=(
                        fill_fact.quantity if fill_fact.side == "BUY" else -fill_fact.quantity
                    ),
                    quantity=fill_fact.quantity,
                    price=fill_fact.price,
                    contract_multiplier=instrument.contract_multiplier,
                    notional=abs(
                        fill_fact.quantity * fill_fact.price * instrument.contract_multiplier
                    ),
                    fee=fill_fact.fee,
                    fee_currency=fill_fact.fee_currency,
                    realized_pnl=None,
                    settlement_currency=instrument.collateral_currency,
                    executed_at=fill_fact.executed_at,
                )
            )
        canonical_fills = deduplicate_fills(tuple(fills))
        order_facts = session.scalars(
            select(models.VenueOrder)
            .where(
                models.VenueOrder.team_id == team.team_id,
                models.VenueOrder.environment == environment,
                tuple_(models.VenueOrder.account_id, models.VenueOrder.venue).in_(account_keys),
                models.VenueOrder.observed_at >= start,
                models.VenueOrder.observed_at <= end,
            )
            .order_by(models.VenueOrder.observed_at, models.VenueOrder.venue_order_fact_id)
        ).all()
        orders = tuple(
            CanonicalOrder(
                account_id=fact.account_id,
                venue=fact.venue,
                environment=environment,
                symbol=(
                    instruments[fact.instrument_id].symbol
                    if fact.instrument_id in instruments
                    else str(fact.instrument_id)
                ),
                side=fact.side,
                order_type=fact.order_type,
                quantity=fact.ordered_quantity,
                limit_price=None,
                reduce_only=fact.reduce_only,
                status=fact.status,
                order_id=fact.venue_order_id,
                client_order_id=fact.client_order_id,
                observed_at=fact.observed_at,
            )
            for fact in order_facts
        )
        funding = session.scalars(
            select(models.FundingPayment)
            .where(
                models.FundingPayment.team_id == team.team_id,
                models.FundingPayment.environment == environment,
                tuple_(models.FundingPayment.account_id, models.FundingPayment.venue).in_(
                    account_keys
                ),
                models.FundingPayment.paid_at >= start,
                models.FundingPayment.paid_at <= end,
            )
            .order_by(models.FundingPayment.paid_at, models.FundingPayment.funding_payment_id)
        ).all()
        performance_cashflows = tuple(
            [
                Cashflow(
                    account_id=fill.account_id,
                    venue=fill.venue,
                    environment=environment,
                    cashflow_id=f"FEE:{fill.idempotency_key}",
                    cashflow_type="FEE",
                    amount=-fill.fee,
                    currency=fill.fee_currency,
                    occurred_at=fill.executed_at,
                    performance_impact=True,
                )
                for fill in canonical_fills
            ]
            + [
                Cashflow(
                    account_id=item.account_id,
                    venue=item.venue,
                    environment=environment,
                    cashflow_id=(
                        f"FUNDING:LIVE:{item.account_id}:{item.venue}:{item.venue_payment_id}"
                    ),
                    cashflow_type="FUNDING",
                    amount=item.amount,
                    currency=item.currency,
                    occurred_at=item.paid_at,
                    performance_impact=True,
                )
                for item in funding
            ]
        )
        scope = AnalyticsScope(
            workspace_id=workspace_id,
            team_id=team.team_id,
            team_name=team.name,
            environment=environment,
            account_ids=tuple(sorted({item.account_id for item in accounts})),
            venues=tuple(sorted({item.venue for item in accounts})),
            account_venues=tuple(sorted({(item.account_id, item.venue) for item in accounts})),
            generation=None,
            from_time=start,
            to_time=end,
        )
        return self._dataset(
            scope=scope,
            nav=nav,
            external_cashflows=external_cashflows,
            returns=returns,
            positions=tuple(positions),
            fills=canonical_fills,
            orders=orders,
            cashflows=external_cashflows + performance_cashflows,
            sources={
                "ACCOUNT_EQUITY_OBSERVATION",
                "VENUE_ORDER",
                "VENUE_FILL",
                "POSITION",
                "FUNDING_PAYMENT",
                "CAPITAL_TRANSFER",
            },
            positions_complete=False,
        )

    @staticmethod
    def _aggregate_venue_nav(
        observations: Sequence[models.AccountEquityObservation],
    ) -> tuple[NavPoint, ...]:
        by_source_day: dict[tuple[str, str, str], dict[Any, models.AccountEquityObservation]] = (
            defaultdict(dict)
        )
        for item in observations:
            if item.usd_equity is None:
                raise domain.DomainRejected(
                    "ANALYTICS_CURRENCY_CONVERSION_MISSING",
                    "LIVE equity requires persisted USD valuation",
                )
            key = (item.account_id, item.venue, item.currency)
            by_source_day[key][item.observed_at.astimezone(UTC).date()] = item
        if not by_source_day:
            raise domain.DomainRejected(
                "ANALYTICS_NAV_CONTINUITY_MISSING", "LIVE equity history is empty"
            )
        day_sets = [set(items) for items in by_source_day.values()]
        first_days = day_sets[0]
        if any(days != first_days for days in day_sets[1:]):
            raise domain.DomainRejected(
                "ANALYTICS_EQUITY_COVERAGE_INCOMPLETE",
                "LIVE accounts do not share complete UTC daily equity coverage",
            )
        result: list[NavPoint] = []
        for day in sorted(first_days):
            rows = [items[day] for items in by_source_day.values()]
            equity = sum((item.usd_equity or Decimal(0) for item in rows), Decimal(0))
            observed_at = max(item.observed_at for item in rows)
            result.append(
                NavPoint(
                    observed_at=observed_at,
                    equity=equity,
                    currency="USD",
                    source_id="|".join(sorted(str(item.observation_id) for item in rows)),
                )
            )
        return tuple(result)

    @staticmethod
    def _venue_external_cashflows(
        session: Session,
        *,
        team_id: UUID,
        environment: str,
        account_keys: set[tuple[str, str]],
        start: datetime,
        end: datetime,
    ) -> tuple[Cashflow, ...]:
        rows = session.scalars(
            select(models.CapitalTransfer)
            .where(
                models.CapitalTransfer.team_id == team_id,
                models.CapitalTransfer.environment == environment,
                models.CapitalTransfer.status == "SETTLED",
                tuple_(models.CapitalTransfer.account_id, models.CapitalTransfer.venue).in_(
                    account_keys
                ),
                models.CapitalTransfer.observed_at >= start,
                models.CapitalTransfer.observed_at <= end,
            )
            .order_by(
                models.CapitalTransfer.observed_at, models.CapitalTransfer.capital_transfer_id
            )
        ).all()
        cashflows: list[Cashflow] = []
        for item in rows:
            if item.asset.upper() not in notilt.USD_STABLE_ASSETS:
                raise domain.DomainRejected(
                    "ANALYTICS_CURRENCY_CONVERSION_MISSING",
                    "capital transfer requires persisted conversion to LIVE NAV currency",
                )
            if item.direction == "VAULT_TO_VENUE":
                if item.net_received is None:
                    raise domain.DomainRejected(
                        "ANALYTICS_CASHFLOW_AMOUNT_MISSING",
                        "settled venue deposit is missing net received amount",
                    )
                amount = item.net_received
                cashflow_type = "DEPOSIT"
            else:
                amount = -item.gross_amount
                cashflow_type = "WITHDRAWAL"
            cashflows.append(
                Cashflow(
                    account_id=item.account_id,
                    venue=item.venue,
                    environment=environment,
                    cashflow_id=f"CAPITAL_TRANSFER:{item.capital_transfer_id}",
                    cashflow_type=cashflow_type,
                    amount=amount,
                    currency="USD",
                    occurred_at=item.observed_at,
                    performance_impact=False,
                    internal_transfer=True,
                )
            )
        return tuple(cashflows)

    @staticmethod
    def _dataset(
        *,
        scope: AnalyticsScope,
        nav: tuple[NavPoint, ...],
        external_cashflows: tuple[Cashflow, ...],
        returns: tuple[ReturnPoint, ...],
        positions: tuple[PositionSnapshot, ...],
        fills: tuple[CanonicalFill, ...],
        orders: tuple[CanonicalOrder, ...],
        cashflows: tuple[Cashflow, ...],
        sources: set[str],
        positions_complete: bool,
    ) -> AnalyticsDataset:
        coverage = {
            "nav_point_count": len(nav),
            "return_count": len(returns),
            "external_cashflow_count": len(external_cashflows),
            "position_snapshot_count": len(positions),
            "transaction_count": len(fills),
            "order_count": len(orders),
            "positions_complete": positions_complete,
            "transactions_complete": True,
        }
        metadata = {
            "version": ANALYTICS_DATASET_VERSION,
            "calendar": CRYPTO_CALENDAR,
            "timezone": "UTC",
            "periods_per_year": PERIODS_PER_YEAR,
            "source_facts": sorted(sources),
            "returns_source": "TRUSTED_NAV_MINUS_NON_PERFORMANCE_CASHFLOWS",
            "fill_pnl_used_as_returns": False,
            "pandas_is_source_of_truth": False,
            "quantstats_is_source_of_truth": False,
            "position_history_source": (
                "PERSISTED_SNAPSHOTS"
                if positions_complete
                else "CURRENT_STATE_ONLY_NO_HISTORICAL_SNAPSHOTS"
            ),
        }
        return AnalyticsDataset(
            scope=scope,
            nav_series=nav,
            external_cashflows=external_cashflows,
            returns=returns,
            positions=positions,
            transactions=fills,
            benchmark_returns=None,
            orders=orders,
            cashflows=cashflows,
            coverage=coverage,
            metadata=metadata,
        )
