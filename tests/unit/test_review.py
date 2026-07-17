from uuid import uuid4

import pytest
from pydantic import ValidationError

from trading_control_plane.review import ReviewChoice, ReviewRequest


def test_review_request_requires_frozen_risk_hash() -> None:
    with pytest.raises(ValidationError, match="risk_summary_hash"):
        ReviewRequest(
            choice=ReviewChoice.APPROVE,
            reason="looks valid",
            risk_summary_hash="not-a-hash",
        )


def test_review_request_preserves_explicit_choice() -> None:
    assurance_id = uuid4()
    request = ReviewRequest(
        choice=ReviewChoice.RETURN,
        reason="needs a new invalidation level",
        risk_summary_hash="a" * 64,
        assurance_id=assurance_id,
        device_ref="device-1",
    )

    assert request.choice is ReviewChoice.RETURN
    assert request.assurance_id == assurance_id
