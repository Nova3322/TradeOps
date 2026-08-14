from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from trading_control_plane.credentials import CredentialCipher
from trading_control_plane.database import Database
from trading_control_plane.service_transactions import TransactionService


@dataclass(frozen=True, slots=True)
class ServiceRuntime:
    database: Database
    credential_cipher: CredentialCipher
    authoritative_live_accounts: dict[str, str]


class ServiceComponent:
    """Shared runtime access for the domain methods composed by TradingService."""

    runtime: ServiceRuntime

    @property
    def database(self) -> Database:
        return self.runtime.database

    @property
    def credential_cipher(self) -> CredentialCipher:
        return self.runtime.credential_cipher

    @property
    def authoritative_live_accounts(self) -> dict[str, str]:
        return self.runtime.authoritative_live_accounts

    @property
    def transactions(self) -> TransactionService:
        return cast(TransactionService, self)
