from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from trading_control_plane.binance_capital import BinanceCapitalGateway
from trading_control_plane.binance_errors import classify_binance_rate_limit
from trading_control_plane.binance_state import (
    BINANCE_DEPLOYMENT_SCOPE,
    DatabaseBinanceRequestState,
)
from trading_control_plane.database import Database
from trading_control_plane.domain import DomainRejected
from trading_control_plane.models import BinanceApiState


def test_binance_cooldown_and_time_offset_survive_state_store_restart(
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

    # A response already in flight when another caller is rate-limited must not
    # erase the deployment cooldown. Only the claimed half-open probe may do so.
    DatabaseBinanceRequestState(database).record_success(
        {"X-MBX-USED-WEIGHT-1M": "2399"}, observed_at=now + timedelta(seconds=1)
    )

    restarted = DatabaseBinanceRequestState(database)
    restored = restarted.current_diagnostic()
    assert restored is not None
    assert restored.as_dict() == diagnostic.as_dict()
    assert restarted.current_time_offset() == (123, now)

    calls = 0

    def transport(_method, _path, _params, _timeout):
        nonlocal calls
        calls += 1
        return {"ok": True}

    for api_key in ("account-a", "account-b"):
        gateway = BinanceCapitalGateway(
            api_key=api_key,
            api_secret="secret",  # noqa: S106
            transport=transport,
            request_state=restarted,
        )
        with pytest.raises(DomainRejected, match="BINANCE_CAPITAL_RATE_LIMITED"):
            gateway._request("GET", "/sapi/v1/account/apiRestrictions")
    assert calls == 0

    expired = now - timedelta(seconds=1)
    with database.session_factory.begin() as session:
        row = session.get(BinanceApiState, BINANCE_DEPLOYMENT_SCOPE, with_for_update=True)
        assert row is not None and isinstance(row.diagnostic, dict)
        row.next_retry_at = expired
        row.diagnostic = {**row.diagnostic, "next_retry_at": expired.isoformat()}
    first_probe = DatabaseBinanceRequestState(database)
    competing_probe = DatabaseBinanceRequestState(database)
    assert first_probe.blocked_diagnostic() is None
    assert competing_probe.blocked_diagnostic() is not None
    first_probe.record_success()
    assert BinanceCapitalGateway(
        api_key="account-c",
        api_secret="secret",  # noqa: S106
        transport=transport,
        request_state=DatabaseBinanceRequestState(database),
    )._request("GET", "/sapi/v1/account/apiRestrictions") == {"ok": True}
    assert calls == 1
