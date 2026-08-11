from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_results_page_is_quantstats_only_and_keeps_legacy_api_off_the_page() -> None:
    reporting = (ROOT / "src/trading_control_plane/web/reporting.js").read_text()

    assert "/api/results/quantstats/options" in reporting
    assert "/api/results/quantstats?" in reporting
    assert "完整绩效报表" in reporting
    assert "RETURNS_READY" not in reporting  # readiness keys are rendered from server facts
    assert "iframe" in reporting
    assert 'sandbox="allow-same-origin"' in reporting
    assert "/api/results?environment=" not in reporting
    assert "绩效维度" not in reporting
    assert "风险事件" not in reporting
    assert "交易任务结果" not in reporting
    assert "Pyfolio" not in reporting


def test_quantstats_is_the_only_installed_report_library() -> None:
    project = (ROOT / "pyproject.toml").read_text().lower()
    lock = (ROOT / "uv.lock").read_text().lower()

    assert '"quantstats==0.0.81"' in project
    assert 'name = "quantstats"' in lock
    assert "pyfolio" not in project
    assert 'name = "pyfolio' not in lock


def test_quantstats_report_shell_has_responsive_boundaries() -> None:
    styles = (ROOT / "src/trading_control_plane/web/styles.css").read_text()

    assert ".quantstats-page { min-width: 0; overflow-x: clip; }" in styles
    assert ".quantstats-frame {" in styles
    assert "width: 100%" in styles
    assert "@media (max-width: 780px)" in styles
