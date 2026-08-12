from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ApiClientRequestContext:
    """Request-local HUMAN attribution and Team context for one API Key."""

    owner_user_id: UUID
    api_client_id: UUID
    workspace_id: UUID
    team_id: UUID


_api_client_context: ContextVar[ApiClientRequestContext | None] = ContextVar(
    "tradingops_api_client_context",
    default=None,
)


def current_api_client_context() -> ApiClientRequestContext | None:
    return _api_client_context.get()


def bind_api_client_context(
    context: ApiClientRequestContext,
) -> Token[ApiClientRequestContext | None]:
    return _api_client_context.set(context)


def reset_api_client_context(token: Token[ApiClientRequestContext | None]) -> None:
    _api_client_context.reset(token)
