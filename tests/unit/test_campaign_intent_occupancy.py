from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from trading_control_plane.campaign_intent_occupancy import (
    BlockingCampaignOrderIntent,
)


def test_stable_intent_cannot_be_reported_as_reduction_blocker() -> None:
    with pytest.raises(ValidationError, match="stable OrderIntent"):
        BlockingCampaignOrderIntent(
            order_intent_id=uuid4(),
            intent_kind="INITIAL",
            candidate_ref="initial-stable",
            status="POSITION_RECONCILED",
            state_version=3,
            intent_quantity=Decimal("0.5"),
            cumulative_filled_quantity=Decimal("0.5"),
            known_remaining_quantity=Decimal("0"),
            zero_fill_confirmed=False,
            venue_order_terminal=True,
            position_reconciled=True,
            protection_confirmed=False,
        )
