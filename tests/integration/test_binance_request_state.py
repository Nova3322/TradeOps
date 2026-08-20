from __future__ import annotations

from datetime import UTC, datetime

import pytest

from trading_control_plane.adapters.binance_capital import BinanceCapitalGateway
from trading_control_plane.binance_errors import classify_binance_rate_limit
from trading_control_plane.binance_state import DatabaseBinanceRequestState
from trading_control_plane.database import Database
from trading_control_plane.domain import DomainRejected


def test_binance_cooldown_is_ephemeral_while_time_offset_survives_state_store_restart(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    first = DatabaseBinanceRequestState(database)
    diagnostic = classify_binance_rate_limit(
        http_status=418,
        binance_error_code=-1003,
        binance_error_message="IP banned",
        headers={"Retry-After": "120", "X-MBX-USED-WEIGHT-1M": "2400"},
        failed_at=now,
    )
    first.record_rate_limit(diagnostic, host="api.binance.com")
    first.record_time_offset(123, synchronized_at=now)

    calls = 0

    def transport(_method, _path, _params, _timeout):
        nonlocal calls
        calls += 1
        return {"ok": True}

    gateway = BinanceCapitalGateway(
        api_key="account-a",
        api_secret="secret",  # noqa: S106
        transport=transport,
        request_state=first,
    )
    with pytest.raises(DomainRejected, match="BINANCE_CAPITAL_RATE_LIMITED"):
        gateway._request("GET", "/sapi/v1/account/apiRestrictions")
    assert calls == 0

    restarted = DatabaseBinanceRequestState(database)
    assert restarted.current_diagnostic() is None
    assert restarted.current_headers() == ({}, None)
    assert restarted.current_time_offset() == (123, now)
    assert BinanceCapitalGateway(
        api_key="account-b",
        api_secret="secret",  # noqa: S106
        transport=transport,
        request_state=restarted,
    )._request("GET", "/sapi/v1/account/apiRestrictions") == {"ok": True}
    assert calls == 1
