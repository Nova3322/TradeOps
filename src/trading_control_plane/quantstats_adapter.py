from __future__ import annotations

import html
import os
import re
import tempfile
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pandas as pd  # type: ignore[import-untyped]

from trading_control_plane.analytics import PERIODS_PER_YEAR, AnalyticsDataset
from trading_control_plane.domain import DomainRejected

os.environ.setdefault("MPLBACKEND", "Agg")
os.environ.setdefault(
    "MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "tradingops-matplotlib")
)


@dataclass(frozen=True, slots=True)
class AnalyticsFrames:
    returns: pd.Series[float]
    positions: pd.DataFrame
    transactions: pd.DataFrame
    benchmark_returns: pd.Series[float] | None
    readiness: dict[str, bool]


@dataclass(frozen=True, slots=True)
class QuantStatsReport:
    html: str
    version: str
    frames: AnalyticsFrames


def _network_download_forbidden(*_args: object, **_kwargs: object) -> None:
    raise DomainRejected(
        "QUANTSTATS_EXTERNAL_DOWNLOAD_FORBIDDEN",
        "QuantStats reports must use persisted TradingOPS returns only",
    )


def analytics_frames(dataset: AnalyticsDataset) -> AnalyticsFrames:
    """Convert Decimal domain facts to Pandas only at the report boundary."""

    returns = pd.Series(
        [float(item.value) for item in dataset.returns],
        index=pd.DatetimeIndex([item.observed_at for item in dataset.returns], tz="UTC"),
        name="returns",
        dtype="float64",
    )
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
            .pivot_table(
                index="date",
                columns="symbol",
                values="market_value",
                aggfunc="last",
            )
            .sort_index()
        )
        positions.index = pd.DatetimeIndex(positions.index, tz="UTC")
    else:
        positions = pd.DataFrame(index=pd.DatetimeIndex([], tz="UTC"))
    transaction_rows = [
        {
            "date": item.executed_at,
            "symbol": item.symbol,
            "amount": float(item.signed_amount),
            "price": float(item.price),
            "txn_dollars": float(-item.signed_amount * item.price * item.contract_multiplier),
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
        transactions.index = pd.DatetimeIndex(transactions.index, tz="UTC")
    else:
        transactions = transactions.drop(columns=["date"])
        transactions.index = pd.DatetimeIndex([], tz="UTC")
    benchmark = None
    if dataset.benchmark_returns is not None:
        benchmark = pd.Series(
            [float(item.value) for item in dataset.benchmark_returns],
            index=pd.DatetimeIndex(
                [item.observed_at for item in dataset.benchmark_returns], tz="UTC"
            ),
            name="benchmark",
            dtype="float64",
        )
    readiness = {
        "RETURNS_READY": not returns.empty,
        "POSITIONS_READY": bool(dataset.coverage.get("positions_complete", False)),
        "TRANSACTIONS_READY": bool(dataset.coverage.get("transactions_complete", False)),
        "BENCHMARK_READY": benchmark is not None and not benchmark.empty,
    }
    return AnalyticsFrames(
        returns=returns,
        positions=positions,
        transactions=transactions,
        benchmark_returns=benchmark,
        readiness=readiness,
    )


_SCRIPT = re.compile(r"<script\b[^>]*>.*?</script\s*>", re.IGNORECASE | re.DOTALL)
_ACTIVE_TAG = re.compile(
    r"</?(?:iframe|object|embed|form|input|button)\b[^>]*>", re.IGNORECASE | re.DOTALL
)
_EVENT_ATTRIBUTE = re.compile(
    r"\s+on[a-z]+\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)", re.IGNORECASE
)
_EXTERNAL_LINK = re.compile(r"<link\b[^>]*>", re.IGNORECASE | re.DOTALL)
_EXTERNAL_HREF = re.compile(
    r"\s+(?:href|target)\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)", re.IGNORECASE
)
_SVG_DOCTYPE = re.compile(r"<!DOCTYPE\s+svg\b.*?>", re.IGNORECASE | re.DOTALL)


def sanitize_quantstats_html(raw: str) -> str:
    sanitized = _SCRIPT.sub("", raw)
    sanitized = _ACTIVE_TAG.sub("", sanitized)
    sanitized = _EVENT_ATTRIBUTE.sub("", sanitized)
    sanitized = _EXTERNAL_LINK.sub("", sanitized)
    sanitized = _EXTERNAL_HREF.sub("", sanitized)
    sanitized = _SVG_DOCTYPE.sub("", sanitized)
    security = """
    <meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:">
    <style id="tradingops-quantstats-boundary">
      html,body{max-width:100%;overflow-x:hidden}body{margin:20px;background:#fff;color:#111}
      .container{width:100%;max-width:1120px}img,svg{max-width:100%;height:auto}
      @media(max-width:800px){body{margin:10px}.container{max-width:100%}#left,#right{float:none;width:100%;margin:0}table{display:block;overflow-x:auto}h1{font-size:20px}}
      @media(prefers-color-scheme:dark){body{background:#101613;color:#eaf2ed}}
      body[data-tradingops-theme="dark"]{background:#101613;color:#eaf2ed}
      body[data-tradingops-theme="dark"] h4{color:#a8b6ae}
      body[data-tradingops-theme="dark"] hr{border-color:#34423a}
      body[data-tradingops-theme="dark"] table thead th{background:#27332c}
      body[data-tradingops-theme="dark"] svg{background:#fff;border-radius:8px}
    </style>
    """
    if "</head>" not in sanitized.lower():
        raise DomainRejected(
            "QUANTSTATS_HTML_INVALID", "QuantStats output is missing a document head"
        )
    sanitized = re.sub(
        r"</head>", security + "</head>", sanitized, count=1, flags=re.IGNORECASE
    )
    lowered = sanitized.lower()
    if any(
        marker in lowered
        for marker in ("<script", " onload=", "<iframe", "<object", "<embed", "href=\"http")
    ):
        raise DomainRejected(
            "QUANTSTATS_HTML_UNSAFE", "QuantStats output failed the active-content boundary"
        )
    return sanitized


class QuantStatsReportAdapter:
    """Read-only report adapter with no exchange, signer, capital, or market-download clients."""

    @staticmethod
    def render(dataset: AnalyticsDataset) -> QuantStatsReport:
        frames = analytics_frames(dataset)
        if not frames.readiness["RETURNS_READY"]:
            raise DomainRejected(
                "ANALYTICS_RETURNS_NOT_READY", "trusted returns are required for QuantStats"
            )
        try:
            import matplotlib.pyplot as plt
            import quantstats as qs

            safe_title = html.escape(
                f"TradingOPS {dataset.scope.team_name} {dataset.scope.environment}", quote=True
            )
            with tempfile.TemporaryDirectory(prefix="tradingops-quantstats-") as directory:
                output = Path(directory) / "report.html"
                with (
                    patch.object(
                        qs.utils,
                        "download_returns",
                        side_effect=_network_download_forbidden,
                    ),
                    warnings.catch_warnings(),
                ):
                    warnings.simplefilter("ignore", RuntimeWarning)
                    warnings.simplefilter("ignore", UserWarning)
                    qs.reports.html(  # type: ignore[no-untyped-call]
                        frames.returns,
                        benchmark=frames.benchmark_returns,
                        periods_per_year=PERIODS_PER_YEAR,
                        output=str(output),
                        title=safe_title,
                        download_filename="tradingops-quantstats.html",
                    )
                raw = output.read_text(encoding="utf-8")
                plt.close("all")
            return QuantStatsReport(
                html=sanitize_quantstats_html(raw),
                version=str(qs.__version__),
                frames=frames,
            )
        except DomainRejected:
            raise
        except Exception as exc:
            raise DomainRejected(
                "QUANTSTATS_REPORT_FAILED",
                "QuantStats could not render the trusted analytics dataset",
            ) from exc


def report_metadata(report: QuantStatsReport) -> dict[str, Any]:
    return {
        "library": "QuantStats",
        "version": report.version,
        "periods_per_year": PERIODS_PER_YEAR,
        "readiness": report.frames.readiness,
        "external_market_downloads": False,
        "exchange_write_adapter_calls": 0,
    }
