import pytest

from trading_control_plane.campaign_reduction_validity import _status_for_rejection


@pytest.mark.parametrize(
    ("error_code", "status"),
    (
        ("CAMPAIGN_REDUCTION_INTENT_OCCUPIED", "INTENT_OCCUPIED"),
        ("CAMPAIGN_REDUCTION_STATE_INVALID", "CAMPAIGN_STATE_INVALID"),
        ("CAMPAIGN_REDUCTION_TARGET_NOT_ACTIONABLE", "TARGET_NOT_ACTIONABLE"),
        ("CAMPAIGN_REDUCTION_TARGET_REQUIRES_REFRESH", "POSITION_CHANGED"),
        ("CAMPAIGN_CURRENT_POSITION_UNAVAILABLE", "POSITION_UNAVAILABLE"),
        ("UNRELATED", None),
    ),
)
def test_reduction_plan_rejections_map_to_bounded_validity_status(
    error_code: str,
    status: str | None,
) -> None:
    assert _status_for_rejection(error_code) == status
