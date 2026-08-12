from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from fastapi import FastAPI

from trading_control_plane.api_core import (
    BinanceCapitalGateway,
    BinancePortfolioMarginClient,
    BinancePortfolioMarginReadOnlyClient,
    BinanceReadOnlyClient,
    BinanceTestnetClient,
    BinanceTestnetOrder,
    BinanceTestnetOrderCommand,
    BinanceTestnetProtectionCommand,
    ExchangeConnectionVerifier,
    FreqtradeWorkerClient,
    HyperliquidCapitalGateway,
    HyperliquidLiveClient,
    HyperliquidReadOnlyClient,
    HyperliquidTestnetClient,
    HyperliquidTestnetOrder,
    HyperliquidTestnetOrderCommand,
    HyperliquidTestnetProtectionCommand,
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
ConfiguredRiskScopes = Callable[[], tuple[tuple[str, str, str], ...]]
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
    database_bound_venue_facts: Callable[[str, str, SessionIdentity], dict[str, Any]]
    freqtrade_client_for_binding: Callable[[PreparedFreqtradeWorkerBinding], FreqtradeWorkerClient]
    require_binance_testnet: Callable[[], None]
    require_default_venue_account: Callable[[str, str], None]
    require_registered_or_default_venue_account: Callable[[SessionIdentity, str, str], None]
    binance: BinanceReadOnlyClient | BinancePortfolioMarginReadOnlyClient
    binance_live: BinancePortfolioMarginClient
    binance_testnet: BinanceTestnetClient
    binance_testnet_reader: BinanceReadOnlyClient
    exchange_connection_verifier: ExchangeConnectionVerifier
    hyperliquid: HyperliquidReadOnlyClient
    hyperliquid_live: HyperliquidLiveClient
    hyperliquid_testnet: HyperliquidTestnetClient


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
    rejected_hyperliquid_order: Callable[
        [HyperliquidTestnetOrderCommand, datetime], HyperliquidTestnetOrder
    ]
    rejected_testnet_order: Callable[[BinanceTestnetOrderCommand, datetime], BinanceTestnetOrder]
    require_binance_live: Callable[[], None]
    require_binance_testnet: Callable[[], None]
    require_freqtrade_live_enabled: Callable[[], None]
    require_freqtrade_live_worker: Callable[[PreparedFreqtradeWorkerBinding], FreqtradeWorkerClient]
    require_hyperliquid_live: Callable[[], None]
    require_hyperliquid_testnet: Callable[[], None]
    binance_live: BinancePortfolioMarginClient
    binance_testnet: BinanceTestnetClient
    freqtrade_workers: tuple[FreqtradeWorkerClient, ...]
    hyperliquid: HyperliquidReadOnlyClient
    hyperliquid_live: HyperliquidLiveClient
    hyperliquid_testnet: HyperliquidTestnetClient
    notilt: NoTiltGateway
    telegram: TelegramGateway
    unknown_hyperliquid_protection: Callable[
        [HyperliquidTestnetProtectionCommand, datetime], HyperliquidTestnetOrder
    ]
    unknown_testnet_protection: Callable[
        [BinanceTestnetProtectionCommand, datetime], BinanceTestnetOrder
    ]


@dataclass(frozen=True, slots=True)
class CapitalRouteDependencies:
    common: AuthenticatedRouteDependencies
    capital_snapshot: Callable[[UUID], dict[str, Any]]
    configured_notilt_scope: Callable[[int], tuple[str, str]]
    effective_direct_capital_settings: Callable[[UUID], tuple[Settings, dict[str, Any] | None]]
    notify_capital: Callable[..., None]
    notilt_chain_id_for_network: Callable[[str], int]
    binance_capital: BinanceCapitalGateway
    capital_transfer: MockCapitalTransferAdapter
    hyperliquid_capital: HyperliquidCapitalGateway
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
