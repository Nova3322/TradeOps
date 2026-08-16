from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from uuid import UUID

from trading_control_plane.domain import DomainRejected

AGENT_TOKEN_MARKER = "tradingops_agent_v1"  # noqa: S105 - legacy public credential marker
API_CLIENT_TOKEN_MARKER = "tradingops_api_v1"  # noqa: S105 - public credential type marker
AGENT_SECRET_PATTERN = re.compile(r"^[A-Za-z0-9_-]{40,64}$")


@dataclass(frozen=True, slots=True)
class IssuedAgentToken:
    token: str
    hint: str


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
