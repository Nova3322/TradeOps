from __future__ import annotations

from decimal import Decimal

from trading_control_plane.queries import _performance_metrics


def report_row(
    campaign_id: str,
    status: str,
    final_pnl: str,
    updated_at: str,
) -> dict[str, object]:
    return {
        "campaign_id": campaign_id,
        "status": status,
        "final_pnl": final_pnl,
        "updated_at": updated_at,
    }


def test_performance_metrics_use_closed_results_and_fail_closed_for_percentages() -> None:
    metrics = _performance_metrics(
        [
            report_row("win", "CLOSED", "20", "2026-08-10T01:00:00+00:00"),
            report_row("loss", "CLOSED", "-10", "2026-08-10T02:00:00+00:00"),
            report_row("flat", "CLOSED", "0", "2026-08-10T03:00:00+00:00"),
            report_row("open", "OPEN", "5", "2026-08-10T04:00:00+00:00"),
        ]
    )

    assert metrics["campaign_count"] == 4
    assert metrics["closed_count"] == 3
    assert metrics["open_count"] == 1
    assert metrics["win_count"] == 1
    assert metrics["loss_count"] == 1
    assert metrics["breakeven_count"] == 1
    assert Decimal(metrics["win_rate"]) == Decimal(1) / Decimal(3)
    assert metrics["profit_loss_ratio"] == "2"
    assert metrics["profit_factor"] == "2"
    assert metrics["net_pnl"] == "15"
    assert metrics["closed_net_pnl"] == "10"
    assert metrics["open_current_pnl"] == "5"
    assert metrics["maximum_drawdown"] == "10"
    assert len(metrics["curve"]) == 3
    assert metrics["percentage_return"] is None
    assert metrics["percentage_drawdown"] is None
    assert metrics["availability"]["percentage_metrics"] == ("OPENING_CAPITAL_UNAVAILABLE")


def test_performance_metrics_distinguish_zero_from_unavailable() -> None:
    metrics = _performance_metrics([report_row("flat", "CLOSED", "0", "2026-08-10T03:00:00+00:00")])

    assert metrics["net_pnl"] == "0"
    assert metrics["closed_net_pnl"] == "0"
    assert metrics["open_current_pnl"] == "0"
    assert metrics["maximum_drawdown"] == "0"
    assert metrics["win_rate"] == "0"
    assert metrics["profit_loss_ratio"] is None
    assert metrics["availability"]["win_rate"] == "AVAILABLE"
    assert metrics["availability"]["profit_loss_ratio"] == "REQUIRES_WIN_AND_LOSS"
