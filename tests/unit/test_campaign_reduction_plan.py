import pytest

from trading_control_plane.campaign_reduction_plan import _reduction_side


@pytest.mark.parametrize(("direction", "side"), [("LONG", "SELL"), ("SHORT", "BUY")])
def test_reduction_side_closes_without_reversing(direction: str, side: str) -> None:
    assert _reduction_side(direction) == side


def test_reduction_side_rejects_unknown_direction() -> None:
    with pytest.raises(ValueError, match="LONG or SHORT"):
        _reduction_side("UNKNOWN")
