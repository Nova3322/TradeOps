from __future__ import annotations

import base64
import html
import io
import math
import os
import tempfile
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, cast

from trading_control_plane.analytics import PERIODS_PER_YEAR, AnalyticsDataset
from trading_control_plane.domain import DomainRejected
from trading_control_plane.reporting_frames import AnalyticsFrames, analytics_frames

os.environ.setdefault("MPLBACKEND", "Agg")
os.environ.setdefault("MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "tradingops-matplotlib"))


@dataclass(frozen=True, slots=True)
class PyfolioReport:
    html: str
    version: str
    frames: AnalyticsFrames
    metrics: dict[str, str]
    chart_count: int


def _finite(value: object, *, name: str) -> str:
    number = float(cast(Any, value))
    if not math.isfinite(number):
        raise DomainRejected(
            "PYFOLIO_METRIC_INVALID", f"Pyfolio returned a non-finite {name} metric"
        )
    return str(number)


def _chart_data(plot: Callable[..., object], *args: object, **kwargs: object) -> str:
    import matplotlib.pyplot as plt

    figure, axis = plt.subplots(figsize=(10.5, 4.2), constrained_layout=True)
    plot(*args, ax=axis, **kwargs)
    buffer = io.BytesIO()
    figure.savefig(buffer, format="png", dpi=120, facecolor="white")
    plt.close(figure)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


class PyfolioReportAdapter:
    """Real pyfolio-reloaded analytics rendered as a persistent single HTML artifact."""

    @staticmethod
    def render(dataset: AnalyticsDataset) -> PyfolioReport:
        frames = analytics_frames(dataset)
        if not frames.readiness["RETURNS_READY"]:
            raise DomainRejected(
                "ANALYTICS_RETURNS_NOT_READY", "trusted returns are required for Pyfolio"
            )
        try:
            dependency_version = version("pyfolio-reloaded")
        except PackageNotFoundError as exc:
            raise DomainRejected(
                "PYFOLIO_DEPENDENCY_MISSING", "pyfolio-reloaded is not installed"
            ) from exc
        try:
            import matplotlib.pyplot as plt
            import pyfolio as pf  # type: ignore[import-untyped]

            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                stats = pf.timeseries.perf_stats(
                    frames.returns,
                    factor_returns=frames.benchmark_returns,
                )
                metrics = {
                    "total_return": _finite(stats["Cumulative returns"], name="total return"),
                    "annual_return": _finite(
                        pf.timeseries.ep.annual_return(
                            frames.returns, annualization=PERIODS_PER_YEAR
                        ),
                        name="annual return",
                    ),
                    "annual_volatility": _finite(
                        pf.timeseries.ep.annual_volatility(
                            frames.returns, annualization=PERIODS_PER_YEAR
                        ),
                        name="annual volatility",
                    ),
                    "sharpe": _finite(
                        pf.timeseries.ep.sharpe_ratio(
                            frames.returns, annualization=PERIODS_PER_YEAR
                        ),
                        name="Sharpe",
                    ),
                    "sortino": _finite(
                        pf.timeseries.ep.sortino_ratio(
                            frames.returns, annualization=PERIODS_PER_YEAR
                        ),
                        name="Sortino",
                    ),
                    "max_drawdown": _finite(stats["Max drawdown"], name="maximum drawdown"),
                    "win_rate": _finite((frames.returns > 0).mean(), name="win rate"),
                    "fees": _finite(frames.transactions["commission"].sum(), name="fees"),
                }
                charts = [
                    (
                        "累计收益",
                        _chart_data(
                            pf.plotting.plot_rolling_returns,
                            frames.returns,
                            factor_returns=frames.benchmark_returns,
                        ),
                    ),
                    (
                        "回撤水位",
                        _chart_data(pf.plotting.plot_drawdown_underwater, frames.returns),
                    ),
                    (
                        "月度收益热力图",
                        _chart_data(pf.plotting.plot_monthly_returns_heatmap, frames.returns),
                    ),
                    (
                        "滚动波动率",
                        _chart_data(
                            pf.plotting.plot_rolling_volatility,
                            frames.returns,
                            factor_returns=frames.benchmark_returns,
                            rolling_window=min(30, max(5, len(frames.returns) // 4)),
                        ),
                    ),
                    (
                        "收益分布",
                        _chart_data(pf.plotting.plot_return_quantiles, frames.returns),
                    ),
                ]
                plt.close("all")
            metric_labels = {
                "total_return": "总收益",
                "annual_return": "年化收益",
                "annual_volatility": "年化波动率",
                "sharpe": "Sharpe",
                "sortino": "Sortino",
                "max_drawdown": "最大回撤",
                "win_rate": "胜率",
                "fees": "手续费",
            }
            metric_cards = "".join(
                f"<article><small>{html.escape(metric_labels[key])}</small>"
                f"<b>{html.escape(value)}</b></article>"
                for key, value in metrics.items()
            )
            chart_html = "".join(
                f'<figure><h2>{html.escape(title)}</h2><img alt="{html.escape(title)}" '
                f'src="data:image/png;base64,{payload}"></figure>'
                for title, payload in charts
            )
            safe_title = html.escape(
                f"TradingOPS {dataset.scope.team_name} {dataset.scope.environment}"
            )
            styles = """
html,body{max-width:100%;overflow-x:hidden}
body{margin:20px;background:#fff;color:#152019;font:14px system-ui}
header{margin-bottom:20px}
.metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}
article,figure{border:1px solid #dfe7e1;border-radius:12px;padding:14px;margin:0}
article small{display:block;color:#65736b}
article b{display:block;font-size:18px;margin-top:5px}
.charts{display:grid;gap:16px;margin-top:16px}
figure h2{font-size:16px}img{display:block;width:100%;height:auto}
@media(max-width:800px){
  body{margin:10px}.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}
}
@media(max-width:430px){.metrics{grid-template-columns:1fr}}
@media(prefers-color-scheme:dark){
  body{background:#101613;color:#eaf2ed}
  article,figure{border-color:#34423a}article small{color:#a8b6ae}
}
"""
            report_html = "".join(
                (
                    '<!doctype html><html><head><meta charset="utf-8">',
                    '<meta http-equiv="Content-Security-Policy" ',
                    "content=\"default-src 'none'; img-src data:; ",
                    "style-src 'unsafe-inline'\">",
                    '<meta name="viewport" content="width=device-width,initial-scale=1">',
                    f"<title>{safe_title}</title><style>{styles}</style></head><body>",
                    f"<header><h1>{safe_title}</h1><p>pyfolio-reloaded ",
                    f"{html.escape(dependency_version)} · UTC 24/7 · ",
                    f"{PERIODS_PER_YEAR} periods/year</p></header>",
                    f'<section class="metrics">{metric_cards}</section>',
                    f'<section class="charts">{chart_html}</section></body></html>',
                )
            )
            return PyfolioReport(
                html=report_html,
                version=dependency_version,
                frames=frames,
                metrics=metrics,
                chart_count=len(charts),
            )
        except DomainRejected:
            raise
        except Exception as exc:
            raise DomainRejected(
                "PYFOLIO_REPORT_FAILED",
                "pyfolio-reloaded could not render the trusted analytics dataset",
            ) from exc


def report_metadata(report: PyfolioReport) -> dict[str, Any]:
    return {
        "library": "pyfolio-reloaded",
        "version": report.version,
        "periods_per_year": PERIODS_PER_YEAR,
        "readiness": report.frames.readiness,
        "metrics": report.metrics,
        "chart_count": report.chart_count,
        "external_market_downloads": False,
        "exchange_write_adapter_calls": 0,
    }
