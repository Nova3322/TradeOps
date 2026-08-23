from __future__ import annotations

import pytest

from trading_control_plane.domain import DomainRejected
from trading_control_plane.safe_spending import _gateway_rejection_code


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Safe account is not deployed on Arbitrum.", "SAFE_ACCOUNT_NOT_DEPLOYED"),
        ("Safe Allowance Module is not enabled.", "SAFE_ALLOWANCE_MODULE_DISABLED"),
        (
            "Requested amount exceeds the current Safe spending limit or balance.",
            "SAFE_ALLOWANCE_OR_BALANCE_INSUFFICIENT",
        ),
        ("RPC rejected the request.", "SAFE_PREFLIGHT_REJECTED"),
    ],
)
def test_gateway_rejection_code_is_actionable(message: str, expected: str) -> None:
    assert _gateway_rejection_code(message) == expected


def test_domain_rejected_preserves_classified_safe_code() -> None:
    error = DomainRejected(
        _gateway_rejection_code(
            "Requested amount exceeds the current Safe spending limit or balance."
        ),
        "Requested amount exceeds the current Safe spending limit or balance.",
    )

    assert error.code == "SAFE_ALLOWANCE_OR_BALANCE_INSUFFICIENT"
