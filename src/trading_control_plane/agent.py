from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from uuid import UUID

from trading_control_plane.domain import DomainRejected, Role

AGENT_TOKEN_MARKER = "tradingops_agent_v1"  # noqa: S105 - legacy public credential marker
API_CLIENT_TOKEN_MARKER = "tradingops_api_v1"  # noqa: S105 - public credential type marker
AGENT_SECRET_PATTERN = re.compile(r"^[A-Za-z0-9_-]{40,64}$")
AGENT_API_ROLES = frozenset({Role.OBSERVER, Role.PROPOSER, Role.REVIEWER})


@dataclass(frozen=True, slots=True)
class IssuedAgentToken:
    token: str
    hint: str


def validate_agent_roles(roles: tuple[Role, ...], *, active: bool = True) -> tuple[Role, ...]:
    normalized = tuple(dict.fromkeys(roles))
    if active and not normalized:
        raise DomainRejected("AGENT_ACCESS_INVALID", "an active agent requires a role")
    if any(role not in AGENT_API_ROLES for role in normalized):
        raise DomainRejected(
            "AGENT_ROLE_FORBIDDEN",
            "agents are limited to observer, proposer and reviewer API roles",
        )
    return normalized


def issue_api_client_token(api_client_id: UUID) -> IssuedAgentToken:
    secret = secrets.token_urlsafe(32)
    token = f"{API_CLIENT_TOKEN_MARKER}.{api_client_id}.{secret}"
    return IssuedAgentToken(token=token, hint=f"{API_CLIENT_TOKEN_MARKER}.…{secret[-4:]}")


def parse_api_client_token(token: str) -> UUID:
    marker, separator, remainder = token.partition(".")
    raw_id, second_separator, secret = remainder.partition(".")
    if (
        separator != "."
        or second_separator != "."
        or marker not in {API_CLIENT_TOKEN_MARKER, AGENT_TOKEN_MARKER}
        or not AGENT_SECRET_PATTERN.fullmatch(secret)
    ):
        raise DomainRejected("AGENT_TOKEN_INVALID", "agent API credential is invalid")
    try:
        return UUID(raw_id)
    except ValueError as exc:
        raise DomainRejected("AGENT_TOKEN_INVALID", "agent API credential is invalid") from exc


def issue_agent_token(agent_id: UUID) -> IssuedAgentToken:
    """Compatibility wrapper for callers migrating from SERVICE Agent credentials."""

    secret = secrets.token_urlsafe(32)
    token = f"{AGENT_TOKEN_MARKER}.{agent_id}.{secret}"
    return IssuedAgentToken(token=token, hint=f"{AGENT_TOKEN_MARKER}.…{secret[-4:]}")


def parse_agent_token(token: str) -> UUID:
    """Compatibility parser accepting current and legacy credential markers."""

    return parse_api_client_token(token)
