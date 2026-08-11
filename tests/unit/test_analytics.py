from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

import pytest

from trading_control_plane.analytics import (
    AnalyticsDataset,
    AnalyticsScope,
    CanonicalFill,
    Cashflow,
    NavPoint,
    PositionSnapshot,
    ReturnPoint,
    canonicalize_venue_snapshot,
    deduplicate_fills,
    derive_24_7_returns,
)
from trading_control_plane.domain import DomainRejected
from trading_control_plane.quantstats_adapter import (
    QuantStatsReportAdapter,
    analytics_frames,
    sanitize_quantstats_html,
)
from trading_control_plane.venue_read_only import (
    VenueEquity,
    VenueFill,
    VenueFunding,
    VenueInstrument,
    VenueOrder,
    VenuePosition,
    VenueReadOnlySnapshot,
)

NOW = datetime(2026, 8, 1, tzinfo=UTC)


def venue_snapshot() -> VenueReadOnlySnapshot:
    return VenueReadOnlySnapshot(
        symbol="BTCUSDT",
        observed_at=NOW,
        instrument=VenueInstrument(
            symbol="BTCUSDT",
            tick_size=Decimal("0.1"),
            lot_size=Decimal("0.001"),
            minimum_notional=Decimal("5"),
            quote_currency="USDT",
            collateral_currency="USDT",
            active=True,
        ),
        orders=(
            VenueOrder(
                order_id="order-1",
                client_order_id="client-1",
                status="PARTIALLY_FILLED",
                side="BUY",
                order_type="LIMIT",
                ordered_quantity=Decimal("2"),
                filled_quantity=Decimal("1"),
                stop_price=Decimal(0),
                reduce_only=False,
                close_position=False,
                observed_at=NOW,
            ),
        ),
        fills=(
            VenueFill(
                fill_id="fill-1",
                order_id="order-1",
                side="BUY",
                quantity=Decimal("1"),
                price=Decimal("25000"),
                fee=Decimal("10"),
                fee_currency="USDT",
                executed_at=NOW,
            ),
        ),
        position=VenuePosition(
            quantity=Decimal("1"),
            average_entry_price=Decimal("25000"),
            mark_price=Decimal("25100"),
            observed_at=NOW,
        ),
        equity=VenueEquity(
            equity=Decimal("100100"),
            available_balance=Decimal("75000"),
            currency="USDT",
            observed_at=NOW,
        ),
        funding=(
            VenueFunding(
                payment_id="funding-1",
                amount=Decimal("-2"),
                currency="USDT",
                paid_at=NOW,
            ),
        ),
        protection=None,
    )


@pytest.mark.parametrize("venue", ("BINANCE", "HYPERLIQUID", "OKX", "BYBIT"))
def test_venue_adapters_canonicalize_orders_fills_positions_and_cashflows(
    venue: str,
) -> None:
    result = canonicalize_venue_snapshot(
        venue=venue,
        account_id="account-1",
        environment="LIVE",
        snapshot=venue_snapshot(),
        contract_multiplier=Decimal("0.01"),
    )

    assert result.orders[0].venue == venue
    assert result.orders[0].quantity == Decimal("2")
    assert result.orders[0].status == "PARTIALLY_FILLED"
    fill = result.fills[0]
    assert fill.signed_amount == Decimal("1")
    assert fill.notional == Decimal("250")
    assert fill.fee == Decimal("10")
    assert fill.idempotency_key == f"LIVE:account-1:{venue}:fill-1"
    assert result.positions[0] == PositionSnapshot(
        account_id="account-1",
        venue=venue,
        environment="LIVE",
        observed_at=NOW,
        symbol="BTCUSDT",
        signed_quantity=Decimal("1"),
        mark_price=Decimal("25100"),
        market_value=Decimal("251"),
        gross_exposure=Decimal("251"),
        net_exposure=Decimal("251"),
    )
    assert [item.cashflow_type for item in result.cashflows] == ["FEE", "FUNDING"]
    assert all(item.performance_impact for item in result.cashflows)


def test_duplicate_fill_is_processed_at_most_once() -> None:
    fill = CanonicalFill(
        account_id="account-1",
        venue="BINANCE",
        environment="LIVE",
        symbol="BTCUSDT",
        fill_id="fill-1",
        order_id="order-1",
        signed_amount=Decimal("1"),
        quantity=Decimal("1"),
        price=Decimal("100"),
        contract_multiplier=Decimal("1"),
        notional=Decimal("100"),
        fee=Decimal("0.1"),
        fee_currency="USDT",
        realized_pnl=None,
        settlement_currency="USDT",
        executed_at=NOW,
    )

    assert deduplicate_fills((fill, fill)) == (fill,)


def test_returns_remove_deposits_withdrawals_and_transfers_but_keep_costs() -> None:
    nav = (
        NavPoint(NOW, Decimal("100"), "USD", "nav-0"),
        NavPoint(NOW + timedelta(days=1), Decimal("209"), "USD", "nav-1"),
        NavPoint(NOW + timedelta(days=2), Decimal("168"), "USD", "nav-2"),
        NavPoint(NOW + timedelta(days=3), Decimal("199"), "USD", "nav-3"),
    )
    external = (
        Cashflow(
            "account-1",
            "BINANCE",
            "LIVE",
            "deposit-1",
            "DEPOSIT",
            Decimal("100"),
            "USD",
            NOW + timedelta(days=1),
            False,
        ),
        Cashflow(
            "account-1",
            "BINANCE",
            "LIVE",
            "withdrawal-1",
            "WITHDRAWAL",
            Decimal("-50"),
            "USD",
            NOW + timedelta(days=2),
            False,
        ),
        Cashflow(
            "account-1",
            "BINANCE",
            "LIVE",
            "transfer-1",
            "INTERNAL_TRANSFER",
            Decimal("25"),
            "USD",
            NOW + timedelta(days=3),
            False,
            internal_transfer=True,
        ),
        Cashflow(
            "account-1",
            "BINANCE",
            "LIVE",
            "fee-1",
            "FEE",
            Decimal("-1"),
            "USD",
            NOW + timedelta(days=1),
            True,
        ),
    )

    result = derive_24_7_returns(
        nav_series=nav,
        external_cashflows=external,
        from_time=NOW,
        to_time=NOW + timedelta(days=3),
    )

    assert result[0].value == Decimal("0.09")
    assert result[1].value == Decimal(9) / Decimal(209)
    assert result[2].value == Decimal(6) / Decimal(168)


@pytest.mark.parametrize(
    ("nav", "from_time", "to_time", "code"),
    (
        (
            (NavPoint(NOW, Decimal("100"), "USD", "one"),),
            NOW,
            NOW + timedelta(days=1),
            "ANALYTICS_NAV_CONTINUITY_MISSING",
        ),
        (
            (
                NavPoint(NOW, Decimal("100"), "USD", "one"),
                NavPoint(NOW + timedelta(days=2), Decimal("101"), "USD", "three"),
            ),
            NOW,
            NOW + timedelta(days=2),
            "ANALYTICS_NAV_CONTINUITY_MISSING",
        ),
        (
            (
                NavPoint(NOW + timedelta(days=1), Decimal("100"), "USD", "two"),
                NavPoint(NOW + timedelta(days=2), Decimal("101"), "USD", "three"),
            ),
            NOW,
            NOW + timedelta(days=2),
            "ANALYTICS_TIME_BOUNDARY_MISSING",
        ),
    ),
)
def test_returns_fail_closed_without_trusted_continuous_nav(
    nav: tuple[NavPoint, ...],
    from_time: datetime,
    to_time: datetime,
    code: str,
) -> None:
    with pytest.raises(DomainRejected) as rejected:
        derive_24_7_returns(
            nav_series=nav,
            external_cashflows=(),
            from_time=from_time,
            to_time=to_time,
        )
    assert rejected.value.code == code


def analytics_dataset(days: int = 31) -> AnalyticsDataset:
    scope = AnalyticsScope(
        workspace_id=uuid4(),
        team_id=uuid4(),
        team_name="Analytics Desk",
        environment="SHADOW",
        account_ids=("TEAM_SHADOW",),
        venues=("TRADINGOPS",),
        generation=2,
        from_time=NOW,
        to_time=NOW + timedelta(days=days),
    )
    return AnalyticsDataset(
        scope=scope,
        nav_series=(),
        external_cashflows=(),
        returns=tuple(
            ReturnPoint(NOW + timedelta(days=offset), Decimal("0.001"))
            for offset in range(1, days + 1)
        ),
        positions=(),
        transactions=(),
        benchmark_returns=None,
        coverage={"positions_complete": True, "transactions_complete": True},
    )


def test_report_boundary_exposes_future_compatible_pandas_frames() -> None:
    frames = analytics_frames(analytics_dataset(days=3))

    assert frames.returns.name == "returns"
    assert str(frames.returns.index.tz) == "UTC"
    assert list(frames.transactions.columns) == [
        "symbol",
        "amount",
        "price",
        "txn_dollars",
        "commission",
        "fill_id",
    ]
    assert frames.readiness == {
        "RETURNS_READY": True,
        "POSITIONS_READY": True,
        "TRANSACTIONS_READY": True,
        "BENCHMARK_READY": False,
    }


def test_quantstats_generates_offline_sanitized_responsive_html() -> None:
    with patch("yfinance.download", side_effect=AssertionError("external download")) as download:
        report = QuantStatsReportAdapter.render(analytics_dataset())

    assert report.version == "0.0.81"
    assert download.call_count == 0
    lowered = report.html.lower()
    assert "<script" not in lowered
    assert " onload=" not in lowered
    assert "content-security-policy" in lowered
    assert "max-width:800px" in lowered
    assert 'data-tradingops-theme="dark"' in lowered


def test_quantstats_html_sanitizer_removes_active_and_external_content() -> None:
    sanitized = sanitize_quantstats_html(
        '<html><head><link href="https://example.test/a.css"></head>'
        '<body onload="steal()"><script>steal()</script>'
        '<a href="https://example.test">external</a><iframe src="x"></iframe></body></html>'
    )

    assert "steal" not in sanitized
    assert "https://example.test" not in sanitized
    assert "<iframe" not in sanitized
    assert "Content-Security-Policy" in sanitized
