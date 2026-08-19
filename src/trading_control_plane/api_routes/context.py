from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from fastapi import FastAPI

from trading_control_plane.api_core import (
    FreqtradeWorkerClient,
    LoginAttemptLimiter,
    NoTiltGateway,
    PasswordHasher,
    PerptapeCandidate,
    ReadinessDatabase,
    SessionIdentity,
    Settings,
    SignedTokenService,
    TelegramGateway,
    TradingQueries,
    TradingService,
)
from trading_control_plane.capital_configuration_use_cases import (
    CapitalConfigurationUseCases,
)
from trading_control_plane.capital_direct_use_cases import CapitalDirectUseCases
from trading_control_plane.capital_receipt_use_cases import CapitalReceiptUseCases
from trading_control_plane.capital_transfer_use_cases import CapitalTransferUseCases
from trading_control_plane.exchange_connection_verification import ExchangeConnectionVerification
from trading_control_plane.service import PreparedFreqtradeWorkerBinding


class RequireCapability(Protocol):
    def __call__(
        self,
        identity: SessionIdentity,
        action: str,
        account_id: str | None = None,
        venue: str | None = None,
    ) -> None: ...


ServiceFactory = Callable[[], TradingService]
QueryFactory = Callable[[], TradingQueries]
ConfiguredRiskScopes = Callable[[UUID], tuple[tuple[str, str, str], ...]]
CurrentPerptapeCandidate = Callable[..., PerptapeCandidate]
CurrentPerptapeCandidates = Callable[..., list[PerptapeCandidate]]
OpportunitySnapshot = Callable[..., dict[str, Any]]


@dataclass(frozen=True, slots=True)
class AuthenticatedRouteDependencies:
    """Dependencies shared by authenticated route groups."""

    # FastAPI dependency marker objects intentionally occupy a typed parameter's
    # default slot; keeping that framework-owned value as Any avoids weakening
    # the actual SessionIdentity annotations on every route.
    identity: Any
    queries: QueryFactory
    service: ServiceFactory
    settings: Settings
    require_capability: RequireCapability


@dataclass(frozen=True, slots=True)
class SystemRouteDependencies:
    database: ReadinessDatabase


@dataclass(frozen=True, slots=True)
class WorkspaceRouteDependencies:
    common: AuthenticatedRouteDependencies
    configured_risk_scopes: ConfiguredRiskScopes
    is_agent_identity: Callable[[SessionIdentity], bool]
    login_limiter: LoginAttemptLimiter
    password_hasher: PasswordHasher
    telegram: TelegramGateway
    token_service: SignedTokenService


@dataclass(frozen=True, slots=True)
class AccountRouteDependencies:
    common: AuthenticatedRouteDependencies
    freqtrade_client_for_binding: Callable[[PreparedFreqtradeWorkerBinding], FreqtradeWorkerClient]
    connection_verification: ExchangeConnectionVerification


@dataclass(frozen=True, slots=True)
class SignalRouteDependencies:
    common: AuthenticatedRouteDependencies
    current_perptape_candidates: CurrentPerptapeCandidates
    notify_reviewers: Callable[[UUID, int, str], None]


@dataclass(frozen=True, slots=True)
class ProposalRouteDependencies:
    common: AuthenticatedRouteDependencies
    current_perptape_candidate: CurrentPerptapeCandidate
    is_agent_identity: Callable[[SessionIdentity], bool]
    notify_reviewers: Callable[[UUID, int, str], None]
    opportunity_snapshot: OpportunitySnapshot
    token_service: SignedTokenService


@dataclass(frozen=True, slots=True)
class RiskRouteDependencies:
    common: AuthenticatedRouteDependencies
    configured_risk_scopes: ConfiguredRiskScopes
    token_service: SignedTokenService


@dataclass(frozen=True, slots=True)
class ExecutionRouteDependencies:
    common: AuthenticatedRouteDependencies
    current_perptape_candidate: CurrentPerptapeCandidate
    current_perptape_candidates: CurrentPerptapeCandidates
    notify_campaign: Callable[..., None]
    require_freqtrade_enabled: Callable[[], None]
    require_freqtrade_worker: Callable[[PreparedFreqtradeWorkerBinding], FreqtradeWorkerClient]
    freqtrade_workers: tuple[FreqtradeWorkerClient, ...]
    notilt: NoTiltGateway
    telegram: TelegramGateway


@dataclass(frozen=True, slots=True)
class CapitalRouteDependencies:
    common: AuthenticatedRouteDependencies
    configuration: CapitalConfigurationUseCases
    direct: CapitalDirectUseCases
    receipts: CapitalReceiptUseCases
    transfers: CapitalTransferUseCases


@dataclass(frozen=True, slots=True)
class ApiRouteContext:
    """Explicit per-domain route dependencies owned by one application."""

    app: FastAPI
    system: SystemRouteDependencies
    workspace: WorkspaceRouteDependencies
    accounts: AccountRouteDependencies
    signals: SignalRouteDependencies
    proposals: ProposalRouteDependencies
    risk: RiskRouteDependencies
    execution: ExecutionRouteDependencies
    capital: CapitalRouteDependencies


__all__ = [
    "AccountRouteDependencies",
    "ApiRouteContext",
    "AuthenticatedRouteDependencies",
    "CapitalRouteDependencies",
    "ExecutionRouteDependencies",
    "ProposalRouteDependencies",
    "RiskRouteDependencies",
    "SignalRouteDependencies",
    "SystemRouteDependencies",
    "WorkspaceRouteDependencies",
]
