from __future__ import annotations

from uuid import uuid4

import pytest

from trading_control_plane.agent import issue_agent_token, parse_agent_token, validate_agent_roles
from trading_control_plane.domain import DomainRejected, Role


def test_agent_token_is_opaque_parseable_and_hinted_without_full_secret() -> None:
    agent_id = uuid4()
    issued = issue_agent_token(agent_id)

    assert parse_agent_token(issued.token) == agent_id
    assert issued.token.startswith(f"tradingops_agent_v1.{agent_id}.")
    assert issued.hint.startswith("tradingops_agent_v1.\u2026")
    assert issued.token not in issued.hint


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
def test_agent_token_parser_fails_closed(token: str) -> None:
    with pytest.raises(DomainRejected, match="AGENT_TOKEN_INVALID"):
        parse_agent_token(token)


def test_agent_roles_are_deduplicated_and_exclude_dangerous_roles() -> None:
    assert validate_agent_roles((Role.PROPOSER, Role.PROPOSER, Role.REVIEWER)) == (
        Role.PROPOSER,
        Role.REVIEWER,
    )
    with pytest.raises(DomainRejected, match="AGENT_ROLE_FORBIDDEN"):
        validate_agent_roles((Role.OPERATOR,))
    with pytest.raises(DomainRejected, match="AGENT_ACCESS_INVALID"):
        validate_agent_roles(())
    assert validate_agent_roles((), active=False) == ()
