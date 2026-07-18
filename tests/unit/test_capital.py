from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from trading_control_plane.capital import CapitalTransferCommand, MockCapitalTransferAdapter
from trading_control_plane.domain import (
    CapitalDirection,
    DomainRejected,
    ExecutionEnvironment,
)


def command(environment: ExecutionEnvironment) -> CapitalTransferCommand:
    return CapitalTransferCommand(
        capital_transfer_id=UUID("00000000-0000-0000-0000-000000000701"),
        environment=environment,
        direction=CapitalDirection.VAULT_TO_VENUE,
        source_id="vault-1",
        destination_id="acct-1",
        asset="USDT",
        network="TESTNET",
        destination_reference="approved-test-destination",
        gross_amount=Decimal("100"),
        max_fee=Decimal("1"),
        min_received=Decimal("99"),
    )


def test_mock_capital_adapter_is_deterministic_and_never_live() -> None:
    adapter = MockCapitalTransferAdapter()
    now = datetime(2026, 7, 19, tzinfo=UTC)

    first = adapter.submit(command(ExecutionEnvironment.TESTNET), now=now)
    second = adapter.submit(command(ExecutionEnvironment.TESTNET), now=now)
    assert first == second
    assert first.status == "SUBMITTED"
    assert first.external_transfer_id.startswith("mock-capital-")

    with pytest.raises(DomainRejected, match="CAPITAL_TRANSFER_LIVE_DISABLED"):
        adapter.submit(command(ExecutionEnvironment.LIVE), now=now)
