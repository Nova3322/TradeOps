from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class PreparedExchangeConnectionVerification:
    exchange_account_id: UUID
    workspace_id: UUID
    team_id: UUID
    account_id: str
    venue: str
    environment: str
    account_version: int
    credential_version: int
    credentials: dict[str, str] = field(repr=False)
    account_mode: str = "STANDARD"


@dataclass(frozen=True, slots=True)
class ConnectionProbeResult:
    success: bool
    error_code: str | None
    diagnostics: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class PreparedCapitalAccountBinding:
    exchange_account_id: UUID
    workspace_id: UUID
    team_id: UUID
    account_id: str
    venue: str
    environment: str
    account_version: int
    credential_version: int
    credentials: dict[str, str] = field(repr=False)
    account_mode: str = "STANDARD"


@dataclass(frozen=True, slots=True)
class PreparedRuntimeAccountBinding:
    exchange_account_id: UUID
    workspace_id: UUID
    team_id: UUID
    service_principal_id: UUID
    service_principal_username: str
    account_id: str
    venue: str
    environment: str
    account_version: int
    credential_version: int
    credentials: dict[str, str] = field(repr=False)
    account_mode: str = "STANDARD"
    hip3_dexes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PreparedFreqtradeWorkerBinding:
    exchange_account_id: UUID
    workspace_id: UUID
    team_id: UUID
    account_id: str
    venue: str
    environment: str
    account_version: int
    worker_name: str
    worker_url: str
    worker_mode: str
    worker_status: str
    auth_version: int
    username: str = field(repr=False)
    password: str = field(repr=False)
    runtime_fingerprint: str | None = None
    hip3_dexes: tuple[str, ...] = ()
    ws_token: str | None = field(default=None, repr=False)
    service_principal_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class PreparedFreqtradeDispatch:
    mode: str
    external_trade_id: str | None
    intent_version: int
    started_at: datetime


@dataclass(frozen=True, slots=True)
class PreparedPerptapeRuntimeBinding:
    signal_source_id: UUID
    workspace_id: UUID
    team_id: UUID
    service_principal_id: UUID
    service_principal_username: str
    source_version: int
    credential_version: int
    api_key: str = field(repr=False)
