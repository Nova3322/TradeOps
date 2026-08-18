from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from trading_control_plane.credentials import CredentialCipher
from trading_control_plane.database import Database
from trading_control_plane.service_transactions import TransactionService


@dataclass(frozen=True, slots=True)
class ServiceRuntime:
    database: Database
    credential_cipher: CredentialCipher
    transactions: TransactionService


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
    def transactions(self) -> TransactionService:
        return self.runtime.transactions

    def can_user(
        self,
        user_id: UUID,
        action: str,
        account_id: str | None = None,
        venue: str | None = None,
    ) -> bool:
        return self.transactions.can_user(user_id, action, account_id, venue)
