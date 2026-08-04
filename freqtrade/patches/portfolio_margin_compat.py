"""Pure helpers for the version-locked Binance Portfolio Margin shim."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any


def _non_negative_decimal(value: Any, field_name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"Portfolio Margin field {field_name} is not numeric") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"Portfolio Margin field {field_name} must be finite and non-negative")
    return parsed


def normalize_portfolio_margin_account(
    account: Any, *, stake_currency: str
) -> dict[str, dict[str, float]]:
    """Map Binance's unified account facts to Freqtrade's wallet contract.

    CCXT 4.5.68 maps ``papi + linear`` to the isolated UM wallet balance.  In
    Portfolio Margin that wallet can be negative while cross-margin collateral
    remains available.  Binance's account-level endpoint is the authoritative
    source for both account equity and currently available trading balance.
    """

    if stake_currency != "USDT":
        raise ValueError("Portfolio Margin live worker is restricted to USDT stake currency")
    if not isinstance(account, dict):
        raise ValueError("Portfolio Margin account response must be an object")

    equity = _non_negative_decimal(account.get("accountEquity"), "accountEquity")
    available = _non_negative_decimal(
        account.get("totalAvailableBalance"), "totalAvailableBalance"
    )
    tolerance = Decimal("0.00000001")
    if available > equity + tolerance:
        raise ValueError("Portfolio Margin available balance exceeds account equity")
    if available > equity:
        available = equity
    used = equity - available
    return {
        stake_currency: {
            "free": float(available),
            "used": float(used),
            "total": float(equity),
        }
    }


def upgrade_portfolio_margin_algo_request(request: Any) -> dict[str, Any]:
    """Translate CCXT 4.5.68's retired conditional shape to Binance's algo shape."""

    if not isinstance(request, dict):
        raise ValueError("Portfolio Margin algo request must be an object")
    upgraded = dict(request)
    renames = {
        "strategyType": "type",
        "stopPrice": "triggerPrice",
        "newClientStrategyId": "clientAlgoId",
    }
    for old, new in renames.items():
        if old in upgraded:
            if new in upgraded:
                raise ValueError(f"Portfolio Margin algo request contains both {old} and {new}")
            upgraded[new] = upgraded.pop(old)
    upgraded["algoType"] = "CONDITIONAL"

    required = ("symbol", "side", "type", "quantity", "triggerPrice", "clientAlgoId")
    missing = [field for field in required if not upgraded.get(field)]
    if missing:
        raise ValueError(
            "Portfolio Margin algo request is missing required fields: " + ", ".join(missing)
        )
    if upgraded["type"] not in {"STOP", "STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET"}:
        raise ValueError("Portfolio Margin algo request type is outside the protected allowlist")
    if upgraded.get("reduceOnly") not in {True, "true", "True", "1"}:
        raise ValueError("Portfolio Margin algo request must be reduce-only")
    return upgraded
