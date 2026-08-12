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

from trading_control_plane.analytics import PERIODS_PER_YEAR, AnalyticsDataset
from trading_control_plane.domain import DomainRejected
from trading_control_plane.reporting_frames import AnalyticsFrames, analytics_frames

os.environ.setdefault("MPLBACKEND", "Agg")
os.environ.setdefault(
    "MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "tradingops-matplotlib")
)


@dataclass(frozen=True, slots=True)
class QuantStatsReport:
    html: str
    version: str
    frames: AnalyticsFrames
    metrics: dict[str, str]
    chart_count: int


def _network_download_forbidden(*_args: object, **_kwargs: object) -> None:
    raise DomainRejected(
        "QUANTSTATS_EXTERNAL_DOWNLOAD_FORBIDDEN",
        "QuantStats reports must use persisted TradingOPS returns only",
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
                metrics={
                    "total_return": str(float(qs.stats.comp(frames.returns))),
                    "annual_return": str(
                        float(qs.stats.cagr(frames.returns, periods=PERIODS_PER_YEAR))
                    ),
                    "annual_volatility": str(
                        float(
                            qs.stats.volatility(
                                frames.returns, periods=PERIODS_PER_YEAR
                            )
                        )
                    ),
                    "sharpe": str(
                        float(qs.stats.sharpe(frames.returns, periods=PERIODS_PER_YEAR))
                    ),
                    "sortino": str(
                        float(qs.stats.sortino(frames.returns, periods=PERIODS_PER_YEAR))
                    ),
                    "max_drawdown": str(float(qs.stats.max_drawdown(frames.returns))),
                    "win_rate": str(float(qs.stats.win_rate(frames.returns))),
                    "fees": str(float(frames.transactions["commission"].sum())),
                },
                chart_count=raw.lower().count("<svg") + raw.lower().count("data:image"),
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
        "metrics": report.metrics,
        "chart_count": report.chart_count,
        "external_market_downloads": False,
        "exchange_write_adapter_calls": 0,
    }
