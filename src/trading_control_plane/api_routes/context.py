from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from fastapi import FastAPI

from trading_control_plane.adapters.capital import CapitalAdapter, CapitalScope
from trading_control_plane.api_core import (
    ExchangeConnectionVerifier,
    FreqtradeWorkerClient,
    LoginAttemptLimiter,
    MockCapitalTransferAdapter,
    NoTiltGateway,
    PasswordHasher,
    PerptapeCandidate,
    ReadinessDatabase,
    SafeSpendingGateway,
    SessionIdentity,
    Settings,
    SignedTokenService,
    TelegramGateway,
    TradingQueries,
    TradingService,
)
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
CapitalAdapterResolver = Callable[[CapitalScope], CapitalAdapter]
ConfiguredRiskScopes = Callable[[UUID], tuple[tuple[str, str, str], ...]]
CurrentPerptapeCandidate = Callable[..., PerptapeCandidate]
CurrentPerptapeCandidates = Callable[..., list[PerptapeCandidate]]
OpportunitySnapshot = Callable[..., dict[str, Any]]


class EffectiveDirectCapitalSettings(Protocol):
    def __call__(
        self,
        user_id: UUID,
        environment: str = "LIVE",
    ) -> tuple[Settings, dict[str, Any] | None]: ...


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
    exchange_connection_verifier: ExchangeConnectionVerifier


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
    capital_snapshot: Callable[[UUID], dict[str, Any]]
    configured_notilt_scope: Callable[[int], tuple[str, str]]
    effective_direct_capital_settings: EffectiveDirectCapitalSettings
    notify_capital: Callable[..., None]
    notilt_chain_id_for_network: Callable[[str], int]
    capital_adapter_resolver: CapitalAdapterResolver
    capital_transfer: MockCapitalTransferAdapter
    notilt: NoTiltGateway
    safe_spending: SafeSpendingGateway
    sync_configured_notilt_vault: Callable[..., tuple[int, dict[str, Any]]]
    token_service: SignedTokenService
    verify_live_notilt_release_budget: Callable[..., None]


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
