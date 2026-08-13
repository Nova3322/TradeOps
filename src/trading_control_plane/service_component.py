from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from trading_control_plane.credentials import CredentialCipher
from trading_control_plane.database import Database

# ruff: noqa: F403, F405
from trading_control_plane.service_core import *
from trading_control_plane.service_transactions import TransactionService


class ServiceFacade(Protocol):
    def _shadow_activation_blockers(self, session: Session, team: Team) -> list[str]: ...

    def _exchange_account_definition(
        self, account_id: str, venue: str, label: str | None
    ) -> tuple[str, str, str]: ...

    def _ensure_exchange_account_reference(
        self,
        session: Session,
        *,
        team: Team,
        actor_id: UUID,
        account_id: str,
        venue: str,
        environment: str = "LIVE",
        now: datetime,
    ) -> ExchangeAccount: ...

    def _set_internal_principal_active(
        self, session: Session, principal_id: UUID | None, active: bool
    ) -> None: ...

    def _require_exact_runtime_principal(
        self,
        session: Session,
        *,
        principal_id: UUID,
        team: Team,
        role: Role,
        account_id: str | None,
        venue: str | None,
        error_code: str,
        error_message: str,
        lock: bool = False,
        allow_inactive: bool = False,
    ) -> User: ...

    def _lock_runtime_account_binding(
        self, session: Session, binding: PreparedRuntimeAccountBinding
    ) -> ExchangeAccount: ...

    def _lock_perptape_runtime_binding(
        self, session: Session, binding: PreparedPerptapeRuntimeBinding
    ) -> TeamSignalSource: ...

    def _capital_balance(
        self,
        session: Session,
        *,
        team_id: UUID,
        environment: str,
        endpoint_type: str,
        endpoint_id: str,
        venue: str,
        asset: str,
        lock: bool = False,
    ) -> AccountEquity: ...

    def _assert_capital_scope_flat(
        self,
        session: Session,
        *,
        team_id: UUID,
        environment: str,
        account_id: str,
        venue: str,
        now: datetime,
    ) -> None: ...

    def _consume_add_unit(self, session: Session, intent: OrderIntent) -> None: ...

    def _simulate_linked_shadow_intent(
        self,
        session: Session,
        *,
        actor_id: UUID,
        team: Team,
        campaign: Campaign,
        intent: OrderIntent,
        proposal: Proposal,
        catalog: Instrument,
        reference_price: Decimal,
        fee_bps: Decimal,
        slippage_bps: Decimal,
        idempotency_key: str,
        now: datetime,
    ) -> dict[str, Any]: ...

    def _validate_sender(
        self,
        session: Session,
        team_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        now: datetime,
    ) -> None: ...

    def _require_exchange_account_live_ready(
        self, session: Session, *, team_id: UUID, account_id: str, venue: str
    ) -> ExchangeAccount: ...

    def _binance_testnet_command(
        self,
        session: Session,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        now: datetime,
        allowed_statuses: set[str],
        *,
        venue: str = "BINANCE",
        require_limit_price: bool = False,
        environment: ExecutionEnvironment = ExecutionEnvironment.TESTNET,
    ) -> BinanceTestnetOrderCommand: ...

    def _release_zero_fill_in_session(
        self,
        session: Session,
        intent: OrderIntent,
        terminal_status: OrderIntentStatus,
        confirmed_external: bool = False,
        *,
        now: datetime,
    ) -> None: ...

    def record_binance_testnet_order(
        self,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        command: BinanceTestnetOrderCommand,
        result: BinanceTestnetOrder,
        *,
        venue: str = "BINANCE",
        expected_order_type: str = "MARKET",
        expected_limit_price: Decimal | None = None,
        allow_bounded_quantity: bool = False,
        environment: ExecutionEnvironment = ExecutionEnvironment.TESTNET,
        dispatch_external_id: str | None = None,
        now: datetime,
    ) -> UUID: ...

    def record_binance_testnet_unknown(
        self,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        command: BinanceTestnetOrderCommand,
        reason: str,
        *,
        venue: str = "BINANCE",
        order_type: str = "MARKET",
        environment: ExecutionEnvironment = ExecutionEnvironment.TESTNET,
        now: datetime,
    ) -> None: ...

    def prepare_binance_testnet_protection(
        self,
        campaign_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        trigger_price: Decimal,
        *,
        venue: str = "BINANCE",
        environment: ExecutionEnvironment = ExecutionEnvironment.TESTNET,
        now: datetime,
    ) -> BinanceTestnetProtectionCommand: ...

    def record_binance_testnet_protection(
        self,
        campaign_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        command: BinanceTestnetProtectionCommand,
        result: BinanceTestnetOrder,
        *,
        venue: str = "BINANCE",
        expected_order_type: str = "STOP_MARKET",
        expected_close_position: bool = True,
        require_reduce_only: bool = False,
        environment: ExecutionEnvironment = ExecutionEnvironment.TESTNET,
        now: datetime,
    ) -> UUID: ...

    def record_position(
        self,
        account_id: str,
        venue: str,
        instrument_id: UUID,
        quantity: Decimal,
        average_entry_price: Decimal,
        mark_price: Decimal,
        known: bool,
        actor_id: UUID,
        *,
        environment: ExecutionEnvironment = ExecutionEnvironment.SHADOW,
        observed_at: datetime | None = None,
        now: datetime,
    ) -> UUID: ...

    def _record_account_equity_observation(
        self, session: Session, fact: AccountEquity, *, recorded_at: datetime
    ) -> None: ...

    def _apply_shadow_pnl_delta(
        self,
        session: Session,
        *,
        campaign: Campaign,
        previous_pnl: Decimal,
        actor_id: UUID,
        correlation_id: UUID,
        now: datetime,
        equity: AccountEquity | None = None,
    ) -> None: ...

    def _update_campaign_pnl(
        self,
        session: Session,
        campaign: Campaign,
        position: Position,
        *,
        now: datetime,
        fills: Sequence[VenueFill] | None = None,
    ) -> PnlBreakdown: ...

    def _occupied_risk(
        self,
        session: Session,
        team_id: UUID,
        *,
        account_id: str | None = None,
        venue: str | None = None,
    ) -> Decimal: ...

    def _active_risk_policy(self, session: Session, team_id: UUID) -> RiskPolicy: ...

    def _risk_policy_input(
        self, policy: RiskPolicy, *, effective_max_total_risk: Decimal | None = None
    ) -> RiskPolicyInput: ...

    def _managed_capital_context(
        self,
        session: Session,
        *,
        team_id: UUID,
        environment: str,
        now: datetime,
        max_age: timedelta,
    ) -> tuple[bool, Decimal, list[dict[str, Any]], datetime]: ...

    def _server_risk_context(
        self,
        session: Session,
        *,
        proposal: Proposal,
        policy: RiskPolicy,
        kind: IntentKind,
        requested_quantity: Decimal,
        requested_risk: Decimal,
        current_risk: Decimal,
        now: datetime,
    ) -> tuple[RiskEvaluationInput, dict[str, Any], datetime, Decimal]: ...

    def _intent_creation(self, response: dict[str, Any]) -> IntentCreation: ...

    def _proposal_limit_price(self, proposal: Proposal) -> Decimal | None: ...

    def _proposal_detail_decimal(self, proposal: Proposal, key: str) -> Decimal: ...

    def _validate_add_candidate(
        self,
        *,
        proposal: Proposal,
        instrument: Instrument,
        candidate: AddCandidateFacts | None,
        policy: RiskPolicy,
        now: datetime,
    ) -> None: ...

    def _fact_is_stale(self, observed_at: datetime, now: datetime, max_age: timedelta) -> bool: ...


@dataclass(frozen=True, slots=True)
class ServiceRuntime:
    database: Database
    credential_cipher: CredentialCipher
    authoritative_live_accounts: dict[str, str]
    transactions: TransactionService


class ServiceComponent:
    """Typed dependencies shared by concrete domain components."""

    def __init__(self, runtime: ServiceRuntime, facade: ServiceFacade) -> None:
        self.runtime = runtime
        self.facade = facade

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
        return self.runtime.transactions
