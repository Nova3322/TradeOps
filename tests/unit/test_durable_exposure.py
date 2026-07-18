from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from trading_control_plane.commands import CommandRejected
from trading_control_plane.durable_exposure import DurableExposureSnapshotService


def test_reservation_and_state_total_heat_must_match_before_aggregation() -> None:
    reservation = SimpleNamespace(reserved_heat=Decimal("110"))
    state = SimpleNamespace(total_heat=Decimal("111"))
    row_result = MagicMock()
    row_result.all.return_value = [(reservation, state)]
    session = MagicMock()
    session.execute.side_effect = [None, row_result]

    with pytest.raises(CommandRejected) as exc_info:
        DurableExposureSnapshotService.query(
            session,
            organization_id="org-1",
            campaign_id=None,
            scope_keys=(),
            raw_available_margin=Decimal("10000"),
        )

    assert exc_info.value.error_code == "DURABLE_EXPOSURE_INTEGRITY_FAILED"
