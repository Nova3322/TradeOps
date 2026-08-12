from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd  # type: ignore[import-untyped]

from trading_control_plane.analytics import AnalyticsDataset
from trading_control_plane.domain import DomainRejected


@dataclass(frozen=True, slots=True)
class AnalyticsFrames:
    """Library-bound Pandas views over the immutable analytics domain dataset."""

    returns: pd.Series[float]
    equity: pd.Series[float]
    positions: pd.DataFrame
    transactions: pd.DataFrame
    benchmark_returns: pd.Series[float] | None
    readiness: dict[str, bool]


def _utc_index(values: list[object]) -> pd.DatetimeIndex:
    index = pd.to_datetime(values, utc=True)
    if not isinstance(index, pd.DatetimeIndex):
        index = pd.DatetimeIndex(index, tz="UTC")
    return index


def _validate_series(series: pd.Series[float], *, code: str, label: str) -> None:
    if not series.index.is_unique or not series.index.is_monotonic_increasing:
        raise DomainRejected(code, f"{label} index must be unique and increasing")
    if str(series.index.tz) != "UTC":
        raise DomainRejected(code, f"{label} index must use UTC")
    if series.isna().any() or any(not math.isfinite(float(value)) for value in series):
        raise DomainRejected(code, f"{label} contains a missing or non-finite value")


def analytics_frames(dataset: AnalyticsDataset) -> AnalyticsFrames:
    """Convert Decimal facts to Pandas exactly once at the report boundary."""

    returns = pd.Series(
        [float(item.value) for item in dataset.returns],
        index=_utc_index([item.observed_at for item in dataset.returns]),
        name="returns",
        dtype="float64",
    )
    equity_points = tuple(
        item
        for item in dataset.nav_series
        if item.source_id != f"SHADOW_ACCOUNT:{dataset.scope.generation}:INITIAL"
    )
    equity = pd.Series(
        [float(item.equity) for item in equity_points],
        index=_utc_index([item.observed_at for item in equity_points]),
        name="equity",
        dtype="float64",
    )
    _validate_series(returns, code="ANALYTICS_RETURNS_INVALID", label="returns")
    _validate_series(equity, code="ANALYTICS_EQUITY_INVALID", label="equity")

    position_rows = [
        {
            "date": item.observed_at,
            "symbol": item.symbol,
            "market_value": float(item.market_value),
        }
        for item in dataset.positions
    ]
    if position_rows:
        positions = (
            pd.DataFrame(position_rows)
            .pivot_table(index="date", columns="symbol", values="market_value", aggfunc="last")
            .sort_index()
        )
        positions.index = _utc_index(list(positions.index))
    else:
        positions = pd.DataFrame(index=pd.DatetimeIndex([], tz="UTC"))

    transaction_rows = [
        {
            "date": item.executed_at,
            "symbol": item.symbol,
            "amount": float(item.signed_amount),
            "price": float(item.price),
            "txn_dollars": float(
                -item.signed_amount * item.price * item.contract_multiplier
            ),
            "commission": float(item.fee),
            "fill_id": item.fill_id,
        }
        for item in dataset.transactions
    ]
    transactions = pd.DataFrame(
        transaction_rows,
        columns=(
            "date",
            "symbol",
            "amount",
            "price",
            "txn_dollars",
            "commission",
            "fill_id",
        ),
    )
    if not transactions.empty:
        transactions = transactions.set_index("date").sort_index()
        transactions.index = _utc_index(list(transactions.index))
        numeric = transactions[["amount", "price", "txn_dollars", "commission"]]
        if numeric.isna().any().any() or not numeric.map(math.isfinite).all().all():
            raise DomainRejected(
                "ANALYTICS_TRANSACTIONS_INVALID",
                "transactions contain a missing or non-finite value",
            )
    else:
        transactions = transactions.drop(columns=["date"])
        transactions.index = pd.DatetimeIndex([], tz="UTC")

    benchmark = None
    if dataset.benchmark_returns is not None:
        benchmark = pd.Series(
            [float(item.value) for item in dataset.benchmark_returns],
            index=_utc_index([item.observed_at for item in dataset.benchmark_returns]),
            name="benchmark",
            dtype="float64",
        )
        _validate_series(
            benchmark,
            code="ANALYTICS_BENCHMARK_INVALID",
            label="benchmark returns",
        )
        if not benchmark.index.equals(returns.index):
            raise DomainRejected(
                "ANALYTICS_BENCHMARK_COVERAGE_MISMATCH",
                "benchmark and strategy returns must share the same UTC index",
            )

    readiness = {
        "RETURNS_READY": not returns.empty,
        "POSITIONS_READY": bool(dataset.coverage.get("positions_complete", False)),
        "TRANSACTIONS_READY": bool(dataset.coverage.get("transactions_complete", False)),
        "BENCHMARK_READY": benchmark is not None and not benchmark.empty,
    }
    return AnalyticsFrames(
        returns=returns,
        equity=equity,
        positions=positions,
        transactions=transactions,
        benchmark_returns=benchmark,
        readiness=readiness,
    )
