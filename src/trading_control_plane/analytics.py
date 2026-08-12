from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from typing import Any
from uuid import UUID

from trading_control_plane.domain import DomainRejected
from trading_control_plane.venue_read_only import VenueReadOnlySnapshot

ANALYTICS_DATASET_VERSION = "analytics-dataset/v1"
CRYPTO_CALENDAR = "UTC_24_7"
PERIODS_PER_YEAR = 365


def _reject(code: str, detail: str) -> None:
    raise DomainRejected(code, detail)


def _utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        _reject("ANALYTICS_TIMEZONE_REQUIRED", f"{field_name} must be timezone-aware UTC")
    return value.astimezone(UTC)


def _finite(value: Decimal, field_name: str) -> Decimal:
    if not value.is_finite():
        _reject("ANALYTICS_VALUE_INVALID", f"{field_name} must be finite")
    return value


def _positive(value: Decimal, field_name: str) -> Decimal:
    if _finite(value, field_name) <= 0:
        _reject("ANALYTICS_VALUE_INVALID", f"{field_name} must be positive")
    return value


@dataclass(frozen=True, slots=True)
class AnalyticsScope:
    workspace_id: UUID
    team_id: UUID
    team_name: str
    environment: str
    account_ids: tuple[str, ...]
    venues: tuple[str, ...]
    account_venues: tuple[tuple[str, str], ...]
    from_time: datetime
    to_time: datetime
    generation: int | None = None

    def __post_init__(self) -> None:
        if self.environment not in {"SHADOW", "LIVE"}:
            _reject("ANALYTICS_ENVIRONMENT_INVALID", "analytics supports only SHADOW or LIVE")
        start = _utc(self.from_time, "from_time")
        end = _utc(self.to_time, "to_time")
        if start >= end:
            _reject("ANALYTICS_TIME_RANGE_INVALID", "from_time must be earlier than to_time")
        if self.environment == "SHADOW" and self.generation is None:
            _reject("ANALYTICS_GENERATION_REQUIRED", "SHADOW analytics requires one generation")
        if self.environment == "SHADOW" and self.account_venues:
            _reject(
                "ANALYTICS_ACCOUNT_SCOPE_INVALID",
                "SHADOW analytics cannot contain LIVE account and venue scopes",
            )
        if self.environment == "LIVE":
            exact_scopes = tuple(sorted(set(self.account_venues)))
            if not exact_scopes:
                _reject(
                    "ANALYTICS_ACCOUNT_SCOPE_REQUIRED",
                    "LIVE analytics requires exact account and venue scopes",
                )
            if exact_scopes != self.account_venues:
                _reject(
                    "ANALYTICS_ACCOUNT_SCOPE_INVALID",
                    "LIVE analytics account and venue scopes must be unique and sorted",
                )
            if tuple(sorted({item[0] for item in exact_scopes})) != self.account_ids:
                _reject(
                    "ANALYTICS_ACCOUNT_SCOPE_INVALID",
                    "account_ids must match exact account and venue scopes",
                )
            if tuple(sorted({item[1] for item in exact_scopes})) != self.venues:
                _reject(
                    "ANALYTICS_ACCOUNT_SCOPE_INVALID",
                    "venues must match exact account and venue scopes",
                )


@dataclass(frozen=True, slots=True)
class CanonicalOrder:
    account_id: str
    venue: str
    environment: str
    symbol: str
    side: str
    order_type: str
    quantity: Decimal
    limit_price: Decimal | None
    reduce_only: bool
    status: str
    order_id: str
    client_order_id: str | None
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class CanonicalFill:
    account_id: str
    venue: str
    environment: str
    symbol: str
    fill_id: str
    order_id: str
    signed_amount: Decimal
    quantity: Decimal
    price: Decimal
    contract_multiplier: Decimal
    notional: Decimal
    fee: Decimal
    fee_currency: str
    realized_pnl: Decimal | None
    settlement_currency: str
    executed_at: datetime

    @property
    def idempotency_key(self) -> str:
        return ":".join((self.environment, self.account_id, self.venue, self.fill_id))


@dataclass(frozen=True, slots=True)
class PositionSnapshot:
    account_id: str
    venue: str
    environment: str
    observed_at: datetime
    symbol: str
    signed_quantity: Decimal
    mark_price: Decimal
    market_value: Decimal
    gross_exposure: Decimal
    net_exposure: Decimal
    source_fill_id: str | None = None


@dataclass(frozen=True, slots=True)
class Cashflow:
    account_id: str
    venue: str
    environment: str
    cashflow_id: str
    cashflow_type: str
    amount: Decimal
    currency: str
    occurred_at: datetime
    performance_impact: bool
    internal_transfer: bool = False


@dataclass(frozen=True, slots=True)
class NavPoint:
    observed_at: datetime
    equity: Decimal
    currency: str
    source_id: str


@dataclass(frozen=True, slots=True)
class ReturnPoint:
    observed_at: datetime
    value: Decimal


@dataclass(frozen=True, slots=True)
class AnalyticsDataset:
    scope: AnalyticsScope
    nav_series: tuple[NavPoint, ...]
    external_cashflows: tuple[Cashflow, ...]
    returns: tuple[ReturnPoint, ...]
    positions: tuple[PositionSnapshot, ...]
    transactions: tuple[CanonicalFill, ...]
    benchmark_returns: tuple[ReturnPoint, ...] | None
    orders: tuple[CanonicalOrder, ...] = ()
    cashflows: tuple[Cashflow, ...] = ()
    coverage: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CanonicalSnapshot:
    orders: tuple[CanonicalOrder, ...]
    fills: tuple[CanonicalFill, ...]
    positions: tuple[PositionSnapshot, ...]
    cashflows: tuple[Cashflow, ...]


def canonicalize_venue_snapshot(
    *,
    venue: str,
    account_id: str,
    environment: str,
    snapshot: VenueReadOnlySnapshot,
    contract_multiplier: Decimal,
) -> CanonicalSnapshot:
    """Convert any read-only venue Adapter snapshot into report-library-neutral facts."""

    normalized_venue = venue.strip().upper()
    if normalized_venue not in {"BINANCE", "HYPERLIQUID", "OKX", "BYBIT"}:
        _reject("ANALYTICS_VENUE_INVALID", "unsupported venue for canonical analytics")
    if environment not in {"SHADOW", "TESTNET", "LIVE"}:
        _reject("ANALYTICS_ENVIRONMENT_INVALID", "canonical facts require an exact environment")
    multiplier = _positive(contract_multiplier, "contract_multiplier")
    settlement = snapshot.instrument.collateral_currency.upper()
    symbol = snapshot.symbol.upper()
    orders: list[CanonicalOrder] = []
    for order in snapshot.orders:
        raw_limit = getattr(order, "limit_price", None)
        limit_price = None
        if raw_limit is not None:
            candidate = Decimal(str(raw_limit))
            limit_price = candidate if candidate > 0 else None
        orders.append(
            CanonicalOrder(
                account_id=account_id,
                venue=normalized_venue,
                environment=environment,
                symbol=symbol,
                side=order.side.upper(),
                order_type=order.order_type.upper(),
                quantity=_finite(order.ordered_quantity, "ordered_quantity"),
                limit_price=limit_price,
                reduce_only=order.reduce_only,
                status=order.status.upper(),
                order_id=order.order_id,
                client_order_id=order.client_order_id or None,
                observed_at=_utc(order.observed_at, "order.observed_at"),
            )
        )
    fills: list[CanonicalFill] = []
    cashflows: list[Cashflow] = []
    seen_fill_keys: set[str] = set()
    for fill in snapshot.fills:
        quantity = _positive(fill.quantity, "fill.quantity")
        price = _positive(fill.price, "fill.price")
        signed = quantity if fill.side.upper() == "BUY" else -quantity
        canonical = CanonicalFill(
            account_id=account_id,
            venue=normalized_venue,
            environment=environment,
            symbol=symbol,
            fill_id=fill.fill_id,
            order_id=fill.order_id,
            signed_amount=signed,
            quantity=quantity,
            price=price,
            contract_multiplier=multiplier,
            notional=abs(quantity * price * multiplier),
            fee=_finite(fill.fee, "fill.fee"),
            fee_currency=fill.fee_currency.upper(),
            realized_pnl=None,
            settlement_currency=settlement,
            executed_at=_utc(fill.executed_at, "fill.executed_at"),
        )
        if canonical.idempotency_key in seen_fill_keys:
            continue
        seen_fill_keys.add(canonical.idempotency_key)
        fills.append(canonical)
        cashflows.append(
            Cashflow(
                account_id=account_id,
                venue=normalized_venue,
                environment=environment,
                cashflow_id=f"FEE:{canonical.idempotency_key}",
                cashflow_type="FEE",
                amount=-canonical.fee,
                currency=canonical.fee_currency,
                occurred_at=canonical.executed_at,
                performance_impact=True,
            )
        )
    position = snapshot.position
    position_quantity = _finite(position.quantity, "position.quantity")
    mark_price = _positive(position.mark_price, "position.mark_price")
    market_value = position_quantity * mark_price * multiplier
    positions = (
        PositionSnapshot(
            account_id=account_id,
            venue=normalized_venue,
            environment=environment,
            observed_at=_utc(position.observed_at, "position.observed_at"),
            symbol=symbol,
            signed_quantity=position_quantity,
            mark_price=mark_price,
            market_value=market_value,
            gross_exposure=abs(market_value),
            net_exposure=market_value,
        ),
    )
    for funding in snapshot.funding:
        cashflows.append(
            Cashflow(
                account_id=account_id,
                venue=normalized_venue,
                environment=environment,
                cashflow_id=f"FUNDING:{environment}:{account_id}:{normalized_venue}:{funding.payment_id}",
                cashflow_type="FUNDING",
                amount=_finite(funding.amount, "funding.amount"),
                currency=funding.currency.upper(),
                occurred_at=_utc(funding.paid_at, "funding.paid_at"),
                performance_impact=True,
            )
        )
    return CanonicalSnapshot(tuple(orders), tuple(fills), positions, tuple(cashflows))


def derive_24_7_returns(
    *,
    nav_series: tuple[NavPoint, ...],
    external_cashflows: tuple[Cashflow, ...],
    from_time: datetime,
    to_time: datetime,
) -> tuple[ReturnPoint, ...]:
    """Build strict UTC daily time-weighted returns without forward fill or guessed facts."""

    start = _utc(from_time, "from_time")
    end = _utc(to_time, "to_time")
    if start >= end:
        _reject("ANALYTICS_TIME_RANGE_INVALID", "from_time must be earlier than to_time")
    by_day: dict[date, NavPoint] = {}
    currencies: set[str] = set()
    for point in sorted(nav_series, key=lambda item: item.observed_at):
        observed = _utc(point.observed_at, "nav.observed_at")
        equity = _finite(point.equity, "nav.equity")
        if equity < 0:
            _reject("ANALYTICS_NAV_INVALID", "NAV must not be negative")
        if start.date() <= observed.date() <= end.date():
            by_day[observed.date()] = NavPoint(
                observed_at=observed,
                equity=equity,
                currency=point.currency.upper(),
                source_id=point.source_id,
            )
            currencies.add(point.currency.upper())
    if len(by_day) < 2:
        _reject(
            "ANALYTICS_NAV_CONTINUITY_MISSING",
            "at least two trusted daily NAV points are required",
        )
    days = sorted(by_day)
    if days[0] != start.date() or days[-1] != end.date():
        _reject(
            "ANALYTICS_TIME_BOUNDARY_MISSING",
            "trusted NAV must cover both requested UTC boundary dates",
        )
    for previous, current in pairwise(days):
        if current - previous != timedelta(days=1):
            _reject(
                "ANALYTICS_NAV_CONTINUITY_MISSING",
                "trusted NAV has a missing UTC crypto-market day",
            )
    if len(currencies) != 1:
        _reject(
            "ANALYTICS_CURRENCY_CONVERSION_MISSING",
            "NAV requires one trusted settlement or converted currency",
        )
    currency = next(iter(currencies))
    flow_by_day: dict[date, Decimal] = defaultdict(Decimal)
    seen_flow_ids: set[str] = set()
    for cashflow in external_cashflows:
        if cashflow.performance_impact:
            continue
        if cashflow.cashflow_id in seen_flow_ids:
            continue
        seen_flow_ids.add(cashflow.cashflow_id)
        occurred = _utc(cashflow.occurred_at, "cashflow.occurred_at")
        if cashflow.currency.upper() != currency:
            _reject(
                "ANALYTICS_CURRENCY_CONVERSION_MISSING",
                "cashflow currency cannot be converted to NAV currency",
            )
        if start.date() < occurred.date() <= end.date():
            flow_by_day[occurred.date()] += _finite(cashflow.amount, "cashflow.amount")
    returns: list[ReturnPoint] = []
    for previous_day, current_day in pairwise(days):
        previous_nav = by_day[previous_day].equity
        current_nav = by_day[current_day].equity
        if previous_nav <= 0:
            _reject("ANALYTICS_NAV_INVALID", "previous NAV must be positive")
        value = (current_nav - previous_nav - flow_by_day[current_day]) / previous_nav
        if not value.is_finite():
            _reject("ANALYTICS_RETURN_INVALID", "derived return is not finite")
        returns.append(
            ReturnPoint(
                observed_at=datetime.combine(current_day, datetime.min.time(), tzinfo=UTC),
                value=value,
            )
        )
    return tuple(returns)


def deduplicate_fills(fills: tuple[CanonicalFill, ...]) -> tuple[CanonicalFill, ...]:
    unique: dict[str, CanonicalFill] = {}
    for fill in sorted(fills, key=lambda item: (item.executed_at, item.idempotency_key)):
        unique.setdefault(fill.idempotency_key, fill)
    return tuple(unique.values())
