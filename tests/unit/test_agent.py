from __future__ import annotations

from uuid import uuid4

import pytest

from trading_control_plane.agent import issue_api_client_token, parse_api_client_token
from trading_control_plane.domain import DomainRejected


def test_api_client_token_is_opaque_parseable_and_hinted_without_full_secret() -> None:
    client_id = uuid4()
    issued = issue_api_client_token(client_id)

    assert parse_api_client_token(issued.token) == client_id
    assert issued.token.startswith(f"tradingops_api_v1.{client_id}.")
    assert issued.hint.startswith("tradingops_api_v1.\u2026")
    assert issued.token not in issued.hint


def test_api_client_parser_accepts_the_persisted_legacy_marker() -> None:
    client_id = uuid4()
    current = issue_api_client_token(client_id).token
    legacy = current.replace("tradingops_api_v1.", "tradingops_agent_v1.", 1)

    assert parse_api_client_token(legacy) == client_id


@pytest.mark.parametrize(
    "token",
    [
        "",
        "Bearer tradingops_agent_v1.invalid.secret",
        "tradingops_agent_v1.invalid.short",
        f"unknown.{uuid4()}.{'a' * 43}",
        f"tradingops_agent_v1.{uuid4()}.{'!' * 43}",
    ],
)
def test_api_client_token_parser_fails_closed(token: str) -> None:
    with pytest.raises(DomainRejected, match="AGENT_TOKEN_INVALID"):
        parse_api_client_token(token)
