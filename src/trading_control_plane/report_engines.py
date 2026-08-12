from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from trading_control_plane.analytics import AnalyticsDataset
from trading_control_plane.domain import DomainRejected
from trading_control_plane.pyfolio_adapter import PyfolioReportAdapter
from trading_control_plane.quantstats_adapter import QuantStatsReportAdapter

MIN_REPORT_RETURN_POINTS = 30


@dataclass(frozen=True, slots=True)
class ReportArtifact:
    engine: str
    library: str
    library_version: str
    html: str
    metrics: dict[str, str]
    chart_count: int
    readiness: dict[str, bool]


def report_engine_catalog() -> list[dict[str, Any]]:
    catalog = []
    for engine, package, label in (
        ("QUANTSTATS", "quantstats", "QuantStats"),
        ("PYFOLIO", "pyfolio-reloaded", "Pyfolio Reloaded"),
    ):
        try:
            installed_version = version(package)
            available = True
            error_code = None
        except PackageNotFoundError:
            installed_version = None
            available = False
            error_code = f"{engine}_DEPENDENCY_MISSING"
        catalog.append(
            {
                "engine": engine,
                "label": label,
                "package": package,
                "version": installed_version,
                "available": available,
                "error_code": error_code,
            }
        )
    return catalog


def render_report(engine: str, dataset: AnalyticsDataset) -> ReportArtifact:
    if len(dataset.returns) < MIN_REPORT_RETURN_POINTS:
        raise DomainRejected(
            "ANALYTICS_HISTORY_INSUFFICIENT",
            f"at least {MIN_REPORT_RETURN_POINTS} trusted daily returns are required",
        )
    normalized = engine.upper()
    if normalized == "QUANTSTATS":
        quantstats_result = QuantStatsReportAdapter.render(dataset)
        return ReportArtifact(
            engine=normalized,
            library="QuantStats",
            library_version=quantstats_result.version,
            html=quantstats_result.html,
            metrics=quantstats_result.metrics,
            chart_count=quantstats_result.chart_count,
            readiness=quantstats_result.frames.readiness,
        )
    if normalized == "PYFOLIO":
        pyfolio_result = PyfolioReportAdapter.render(dataset)
        return ReportArtifact(
            engine=normalized,
            library="pyfolio-reloaded",
            library_version=pyfolio_result.version,
            html=pyfolio_result.html,
            metrics=pyfolio_result.metrics,
            chart_count=pyfolio_result.chart_count,
            readiness=pyfolio_result.frames.readiness,
        )
    raise DomainRejected("REPORT_ENGINE_INVALID", "report engine is not supported")
