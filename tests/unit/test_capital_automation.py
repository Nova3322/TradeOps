from decimal import Decimal

import pytest

from trading_control_plane.capital import evaluate_capital_automation
from trading_control_plane.domain import DomainRejected


def facts(**overrides: Decimal | str) -> dict[str, Decimal | str]:
    values: dict[str, Decimal | str] = {
        "purpose": "AUTO_PROFIT_SWEEP",
        "venue_available": Decimal("800"),
        "venue_withdrawable": Decimal("750"),
        "vault_available": Decimal("1000"),
        "confirmed_realized_pnl": Decimal("180"),
        "operating_low": Decimal("400"),
        "operating_target": Decimal("500"),
        "operating_high": Decimal("600"),
        "vault_minimum_reserve": Decimal("500"),
        "minimum_transfer": Decimal("10"),
        "maximum_transfer": Decimal("200"),
        "max_fee": Decimal("1"),
    }
    values.update(overrides)
    return values


def test_profit_sweep_is_limited_to_confirmed_realized_profit() -> None:
    decision = evaluate_capital_automation(**facts())  # type: ignore[arg-type]
    assert decision.amount == Decimal("180")
    assert decision.reason == "CANDIDATE_READY"

    floating_only = evaluate_capital_automation(
        **facts(confirmed_realized_pnl=Decimal(0))  # type: ignore[arg-type]
    )
    assert floating_only.amount is None
    assert floating_only.reason == "NO_CONFIRMED_REALIZED_PROFIT"


def test_refill_only_restores_next_cycle_target_without_chasing_losses() -> None:
    decision = evaluate_capital_automation(
        **facts(
            purpose="AUTO_OPERATING_REFILL",
            venue_available=Decimal("300"),
            venue_withdrawable=Decimal("300"),
            vault_available=Decimal("800"),
            confirmed_realized_pnl=Decimal(0),
        )  # type: ignore[arg-type]
    )
    assert decision.amount == Decimal("200")

    loss = evaluate_capital_automation(
        **facts(
            purpose="AUTO_OPERATING_REFILL",
            venue_available=Decimal("300"),
            confirmed_realized_pnl=Decimal("-1"),
        )  # type: ignore[arg-type]
    )
    assert loss.amount is None
    assert loss.reason == "REALIZED_LOSS_REFILL_BLOCKED"


def test_capital_automation_rejects_invalid_policy_and_facts() -> None:
    with pytest.raises(DomainRejected, match="CAPITAL_AUTOMATION_POLICY_INVALID"):
        evaluate_capital_automation(
            **facts(operating_low=Decimal("700"))  # type: ignore[arg-type]
        )
    with pytest.raises(DomainRejected, match="CAPITAL_AUTOMATION_FACT_INVALID"):
        evaluate_capital_automation(
            **facts(vault_available=Decimal("-1"))  # type: ignore[arg-type]
        )
