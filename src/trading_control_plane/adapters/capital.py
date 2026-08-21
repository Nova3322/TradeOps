from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar, Literal, Protocol

from trading_control_plane.domain import DomainRejected

CapitalVenue = Literal["BINANCE", "HYPERLIQUID", "OKX", "BYBIT"]


class CapitalOperation(StrEnum):
    TRANSFER = "TRANSFER"
    WITHDRAW = "WITHDRAW"
    FETCH_DEPOSITS = "FETCH_DEPOSITS"
    FETCH_WITHDRAWALS = "FETCH_WITHDRAWALS"
    ADD_MARGIN = "ADD_MARGIN"
    REDUCE_MARGIN = "REDUCE_MARGIN"
    BINANCE_PREPARE_DEPOSIT = "BINANCE_PREPARE_DEPOSIT"
    BINANCE_PREPARE_WITHDRAWAL = "BINANCE_PREPARE_WITHDRAWAL"
    BINANCE_SUBMIT_WITHDRAWAL = "BINANCE_SUBMIT_WITHDRAWAL"
    BINANCE_VERIFY_DEPOSIT = "BINANCE_VERIFY_DEPOSIT"
    BINANCE_COMPLETE_DEPOSIT = "BINANCE_COMPLETE_DEPOSIT"
    BINANCE_VERIFY_WITHDRAWAL = "BINANCE_VERIFY_WITHDRAWAL"
    HYPERLIQUID_RESOLVE_MAIN = "HYPERLIQUID_RESOLVE_MAIN"
    HYPERLIQUID_ARBITRUM_BALANCE = "HYPERLIQUID_ARBITRUM_BALANCE"
    HYPERLIQUID_PREPARE_DEPOSIT = "HYPERLIQUID_PREPARE_DEPOSIT"
    HYPERLIQUID_PREPARE_WITHDRAWAL = "HYPERLIQUID_PREPARE_WITHDRAWAL"
    HYPERLIQUID_PREPARE_ARBITRUM_TRANSFER = "HYPERLIQUID_PREPARE_ARBITRUM_TRANSFER"
    HYPERLIQUID_VERIFY_LEDGER = "HYPERLIQUID_VERIFY_LEDGER"
    HYPERLIQUID_VERIFY_ARBITRUM_TRANSFER = "HYPERLIQUID_VERIFY_ARBITRUM_TRANSFER"
    HYPERLIQUID_VERIFY_ARBITRUM_CREDIT = "HYPERLIQUID_VERIFY_ARBITRUM_CREDIT"
    HYPERLIQUID_VERIFY_ARBITRUM_CREDIT_ANY = "HYPERLIQUID_VERIFY_ARBITRUM_CREDIT_ANY"
    HYPERLIQUID_FIND_ARBITRUM_CREDIT = "HYPERLIQUID_FIND_ARBITRUM_CREDIT"
    HYPERLIQUID_FIND_CCTP_CREDIT = "HYPERLIQUID_FIND_CCTP_CREDIT"


@dataclass(frozen=True, slots=True)
class CapitalScope:
    workspace_id: str
    team_id: str
    account_id: str
    venue: CapitalVenue
    environment: Literal["TESTNET", "LIVE"]
    account_mode: str = "STANDARD"

    def __post_init__(self) -> None:
        for name, value in (
            ("workspace_id", self.workspace_id),
            ("team_id", self.team_id),
            ("account_id", self.account_id),
        ):
            if not value or value != value.strip() or len(value) > 160:
                raise ValueError(f"capital adapter {name} is invalid")
        allowed_modes = (
            {"STANDARD", "PORTFOLIO_MARGIN"} if self.venue == "BINANCE" else {"STANDARD"}
        )
        if self.account_mode not in allowed_modes:
            raise DomainRejected(
                "CAPITAL_ACCOUNT_MODE_UNSUPPORTED",
                "the account mode is not supported for the selected capital exchange",
            )


@dataclass(frozen=True, slots=True)
class CapitalCredential:
    account_id: str
    venue: CapitalVenue
    purpose: Literal["CAPITAL"]
    values: Mapping[str, str] = field(repr=False)
    permissions: frozenset[str] = frozenset({"READ", "TRANSFER"})


CapitalExchangeFactory = Callable[[CapitalScope, CapitalCredential], Any]
CapitalAdapterFactory = Callable[[CapitalScope], "CapitalAdapter"]
CapitalCredentialResolver = Callable[[CapitalScope], Mapping[str, str]]


@dataclass(frozen=True, slots=True)
class CapabilityProbe:
    supported: bool
    contract: str
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class CapitalResult:
    backend: str
    contract: str
    value: Any


class CapitalBackend(Protocol):
    @property
    def name(self) -> str: ...

    def probe(self, scope: CapitalScope, operation: CapitalOperation) -> CapabilityProbe: ...

    def execute(
        self,
        scope: CapitalScope,
        operation: CapitalOperation,
        parameters: Mapping[str, Any],
    ) -> Any: ...


class CapitalAdapter:
    """Choose one pre-probed capital backend and never double-broadcast an action."""

    def __init__(
        self,
        *,
        scope: CapitalScope,
        credential: CapitalCredential,
        backends: Sequence[CapitalBackend],
    ) -> None:
        if credential.purpose != "CAPITAL":
            raise DomainRejected(
                "CAPITAL_CREDENTIAL_PURPOSE_INVALID",
                "capital adapters require a separately managed capital credential",
            )
        if (credential.account_id, credential.venue) != (scope.account_id, scope.venue):
            raise DomainRejected(
                "CAPITAL_CREDENTIAL_SCOPE_MISMATCH",
                "capital credentials are outside the exact runtime account scope",
            )
        if not backends:
            raise ValueError("capital adapter requires at least one bounded backend")
        self.scope = scope
        self._credential = credential
        self._backends = tuple(backends)

    def execute(
        self,
        operation: CapitalOperation,
        parameters: Mapping[str, Any],
    ) -> CapitalResult:
        write_operations = {
            CapitalOperation.TRANSFER,
            CapitalOperation.WITHDRAW,
            CapitalOperation.ADD_MARGIN,
            CapitalOperation.REDUCE_MARGIN,
            CapitalOperation.BINANCE_SUBMIT_WITHDRAWAL,
            CapitalOperation.BINANCE_COMPLETE_DEPOSIT,
        }
        if operation in write_operations:
            operation_id = parameters.get("operation_id")
            if (
                not isinstance(operation_id, str)
                or not operation_id.strip()
                or len(operation_id) > 160
            ):
                raise DomainRejected(
                    "CAPITAL_IDEMPOTENCY_KEY_REQUIRED",
                    "capital writes require one durable control-plane operation identity",
                )
        if (
            operation is CapitalOperation.WITHDRAW
            and "WITHDRAW" not in self._credential.permissions
        ):
            raise DomainRejected(
                "CAPITAL_WITHDRAW_PERMISSION_MISSING",
                "the dedicated capital credential has no withdrawal permission",
            )
        probes = tuple(
            (backend, backend.probe(self.scope, operation)) for backend in self._backends
        )
        selected = next(
            ((backend, probe) for backend, probe in probes if probe.supported),
            None,
        )
        if selected is None:
            raise DomainRejected(
                "CAPITAL_OPERATION_UNSUPPORTED",
                "no verified capital backend supports the requested operation",
                metadata={
                    "operation": operation.value,
                    "contracts": [probe.contract for _backend, probe in probes],
                },
            )
        backend, probe = selected
        # Selection is final before this call.  An exception or unknown response
        # is returned to the control plane and never triggers a second backend.
        try:
            value = backend.execute(self.scope, operation, parameters)
        except DomainRejected:
            raise
        except Exception as exc:
            raise DomainRejected(
                "CAPITAL_RESULT_UNKNOWN",
                "the selected capital backend did not return a confirmed result",
                metadata={"backend": backend.name, "error_type": type(exc).__name__},
            ) from exc
        if operation in write_operations:
            identifiers = (
                "id",
                "txid",
                "transactionId",
                "tranId",
                "transferId",
                "withdrawalId",
            )
            if not isinstance(value, Mapping) or not any(value.get(key) for key in identifiers):
                raise DomainRejected(
                    "CAPITAL_RESULT_UNKNOWN",
                    "the selected capital backend returned no durable operation identity",
                    metadata={"backend": backend.name},
                )
        elif value is None:
            raise DomainRejected(
                "CAPITAL_RESULT_UNKNOWN",
                "the selected capital backend returned no confirmed read result",
                metadata={"backend": backend.name},
            )
        return CapitalResult(backend=backend.name, contract=probe.contract, value=value)


_UNIFIED_METHODS: dict[CapitalOperation, str] = {
    CapitalOperation.TRANSFER: "transfer",
    CapitalOperation.WITHDRAW: "withdraw",
    CapitalOperation.FETCH_DEPOSITS: "fetch_deposits",
    CapitalOperation.FETCH_WITHDRAWALS: "fetch_withdrawals",
    CapitalOperation.ADD_MARGIN: "add_margin",
    CapitalOperation.REDUCE_MARGIN: "reduce_margin",
}
_UNIFIED_CAPABILITIES: dict[CapitalOperation, str] = {
    CapitalOperation.TRANSFER: "transfer",
    CapitalOperation.WITHDRAW: "withdraw",
    CapitalOperation.FETCH_DEPOSITS: "fetchDeposits",
    CapitalOperation.FETCH_WITHDRAWALS: "fetchWithdrawals",
    CapitalOperation.ADD_MARGIN: "addMargin",
    CapitalOperation.REDUCE_MARGIN: "reduceMargin",
}


class CcxtUnifiedCapitalBackend:
    """CCXT unified calls gated by advertised methods and a live read-only probe."""

    name = "CCXT_UNIFIED"

    def __init__(self, exchange: Any) -> None:
        self._exchange = exchange
        self._read_probe: bool | None = None

    def probe(self, scope: CapitalScope, operation: CapitalOperation) -> CapabilityProbe:
        capability = _UNIFIED_CAPABILITIES.get(operation)
        method_name = _UNIFIED_METHODS.get(operation)
        if capability is None or method_name is None:
            return CapabilityProbe(
                supported=False,
                contract="ccxt.unified.unsupported",
                reason="operation has no CCXT unified contract",
            )
        advertised = getattr(self._exchange, "has", {})
        callable_method = callable(getattr(self._exchange, method_name, None))
        declared = (
            isinstance(advertised, Mapping)
            and advertised.get(capability) is True
            and callable_method
        )
        if declared and self._read_probe is None:
            try:
                load_markets = self._exchange.load_markets
                fetch_balance = self._exchange.fetch_balance
                markets = load_markets()
                balance = fetch_balance()
                self._read_probe = isinstance(markets, Mapping) and isinstance(balance, Mapping)
            except Exception:
                self._read_probe = False
        supported = declared and self._read_probe is True
        return CapabilityProbe(
            supported=supported,
            contract=f"ccxt.unified.{method_name}",
            reason=None if supported else "capability or live read-only probe is not verified",
        )

    def execute(
        self,
        scope: CapitalScope,
        operation: CapitalOperation,
        parameters: Mapping[str, Any],
    ) -> Any:
        del scope
        method = getattr(self._exchange, _UNIFIED_METHODS[operation])
        args = parameters.get("args", ())
        kwargs = parameters.get("kwargs", {})
        if not isinstance(args, (list, tuple)) or not isinstance(kwargs, Mapping):
            raise DomainRejected(
                "CAPITAL_PARAMETERS_INVALID",
                "capital adapter arguments do not match the verified contract",
            )
        return method(*args, **dict(kwargs))


def build_ccxt_capital_backend(
    scope: CapitalScope,
    credential: CapitalCredential,
    *,
    exchange_factory: CapitalExchangeFactory | None = None,
) -> CcxtUnifiedCapitalBackend:
    """Create CCXT only inside the isolated capital boundary.

    The credential object is purpose-bound and exact-account-bound before an
    exchange client is created.  This prevents a Freqtrade/fact credential from
    being silently reused for a capital call.
    """

    if credential.purpose != "CAPITAL" or (
        credential.account_id,
        credential.venue,
    ) != (scope.account_id, scope.venue):
        raise DomainRejected(
            "CAPITAL_CREDENTIAL_SCOPE_MISMATCH",
            "CCXT capital construction requires the exact dedicated capital credential",
        )
    if exchange_factory is None:
        import ccxt  # type: ignore[import-untyped]

        def exchange_factory(
            selected_scope: CapitalScope,
            selected_credential: CapitalCredential,
        ) -> Any:
            values = selected_credential.values
            configuration: dict[str, Any] = {
                "enableRateLimit": True,
                "apiKey": values.get("api_key", ""),
                "secret": values.get("api_secret", ""),
                "options": {"defaultType": "swap"},
            }
            if (
                selected_scope.venue == "BINANCE"
                and selected_scope.account_mode == "PORTFOLIO_MARGIN"
            ):
                configuration["options"].update({"papi": True, "portfolioMargin": True})
            if selected_scope.venue == "OKX":
                configuration["password"] = values.get("passphrase", "")
            if selected_scope.venue == "HYPERLIQUID":
                configuration["walletAddress"] = values.get("account_address", "")
                configuration["privateKey"] = values.get("private_key", "")
                configuration["options"]["fetchMarkets"] = {"types": ["swap"]}
            constructors: dict[CapitalVenue, Callable[[dict[str, Any]], Any]] = {
                "BINANCE": ccxt.binanceusdm,
                "HYPERLIQUID": ccxt.hyperliquid,
                "OKX": ccxt.okx,
                "BYBIT": ccxt.bybit,
            }
            exchange = constructors[selected_scope.venue](configuration)
            if selected_scope.environment == "TESTNET":
                exchange.set_sandbox_mode(True)
            return exchange

    return CcxtUnifiedCapitalBackend(exchange_factory(scope, credential))


class CallableCapitalBackend:
    """Explicit implicit-API, official-SDK, or minimal-native fallback slot."""

    def __init__(
        self,
        *,
        name: Literal["CCXT_IMPLICIT", "OFFICIAL_SDK", "NATIVE_RESTRICTED"],
        contracts: Mapping[tuple[CapitalVenue, CapitalOperation], str],
        executor: Callable[[CapitalScope, CapitalOperation, Mapping[str, Any]], Any],
    ) -> None:
        self.name = name
        self._contracts = dict(contracts)
        self._executor = executor

    def probe(self, scope: CapitalScope, operation: CapitalOperation) -> CapabilityProbe:
        contract = self._contracts.get((scope.venue, operation))
        return CapabilityProbe(
            supported=contract is not None,
            contract=contract or f"{self.name.lower()}.unsupported",
            reason=None if contract is not None else "no verified venue contract",
        )

    def execute(
        self,
        scope: CapitalScope,
        operation: CapitalOperation,
        parameters: Mapping[str, Any],
    ) -> Any:
        return self._executor(scope, operation, parameters)


class ProductionCapitalAdapterFactory:
    """Build an exact-scope adapter from dedicated capital-only custody."""

    _NATIVE_METHODS: ClassVar[dict[CapitalOperation, tuple[CapitalVenue, str]]] = {
        CapitalOperation.BINANCE_PREPARE_DEPOSIT: ("BINANCE", "prepare_deposit"),
        CapitalOperation.BINANCE_PREPARE_WITHDRAWAL: ("BINANCE", "prepare_withdrawal"),
        CapitalOperation.BINANCE_SUBMIT_WITHDRAWAL: ("BINANCE", "submit_withdrawal"),
        CapitalOperation.BINANCE_VERIFY_DEPOSIT: ("BINANCE", "verify_deposit"),
        CapitalOperation.BINANCE_COMPLETE_DEPOSIT: ("BINANCE", "complete_deposit_to_usdm"),
        CapitalOperation.BINANCE_VERIFY_WITHDRAWAL: ("BINANCE", "verify_withdrawal"),
        CapitalOperation.HYPERLIQUID_RESOLVE_MAIN: ("HYPERLIQUID", "resolve_main_account"),
        CapitalOperation.HYPERLIQUID_ARBITRUM_BALANCE: (
            "HYPERLIQUID",
            "arbitrum_usdc_balance",
        ),
        CapitalOperation.HYPERLIQUID_PREPARE_DEPOSIT: (
            "HYPERLIQUID",
            "prepare_deposit",
        ),
        CapitalOperation.HYPERLIQUID_PREPARE_WITHDRAWAL: (
            "HYPERLIQUID",
            "prepare_withdrawal",
        ),
        CapitalOperation.HYPERLIQUID_PREPARE_ARBITRUM_TRANSFER: (
            "HYPERLIQUID",
            "prepare_arbitrum_usdc_transfer",
        ),
        CapitalOperation.HYPERLIQUID_VERIFY_LEDGER: (
            "HYPERLIQUID",
            "verify_hyperliquid_ledger",
        ),
        CapitalOperation.HYPERLIQUID_VERIFY_ARBITRUM_TRANSFER: (
            "HYPERLIQUID",
            "verify_arbitrum_usdc_transfer",
        ),
        CapitalOperation.HYPERLIQUID_VERIFY_ARBITRUM_CREDIT: (
            "HYPERLIQUID",
            "verify_arbitrum_usdc_credit",
        ),
        CapitalOperation.HYPERLIQUID_VERIFY_ARBITRUM_CREDIT_ANY: (
            "HYPERLIQUID",
            "verify_arbitrum_usdc_credit_from_any_sender",
        ),
        CapitalOperation.HYPERLIQUID_FIND_ARBITRUM_CREDIT: (
            "HYPERLIQUID",
            "find_arbitrum_usdc_credit",
        ),
        CapitalOperation.HYPERLIQUID_FIND_CCTP_CREDIT: (
            "HYPERLIQUID",
            "find_cctp_withdrawal_credit",
        ),
    }
    _GENERIC_CHAIN_OPERATIONS: ClassVar[frozenset[CapitalOperation]] = frozenset(
        {
            CapitalOperation.HYPERLIQUID_PREPARE_ARBITRUM_TRANSFER,
            CapitalOperation.HYPERLIQUID_VERIFY_ARBITRUM_TRANSFER,
            CapitalOperation.HYPERLIQUID_VERIFY_ARBITRUM_CREDIT,
            CapitalOperation.HYPERLIQUID_VERIFY_ARBITRUM_CREDIT_ANY,
            CapitalOperation.HYPERLIQUID_FIND_ARBITRUM_CREDIT,
        }
    )

    def __init__(
        self,
        *,
        binance_account_id: str | None,
        binance_api_key: str | None,
        binance_api_secret: str | None,
        binance_gateway: Any,
        hyperliquid_gateway: Any,
        exchange_factory: CapitalExchangeFactory | None = None,
        credential_resolver: CapitalCredentialResolver | None = None,
    ) -> None:
        self.binance_account_id = binance_account_id
        self.binance_api_key = binance_api_key
        self.binance_api_secret = binance_api_secret
        self.binance_gateway = binance_gateway
        self.hyperliquid_gateway = hyperliquid_gateway
        self.exchange_factory = exchange_factory
        self.credential_resolver = credential_resolver

    def __call__(self, scope: CapitalScope) -> CapitalAdapter:
        if scope.venue == "BINANCE":
            credential_values: Mapping[str, str] | None = None
            resolver_error: DomainRejected | None = None
            if self.credential_resolver is not None:
                try:
                    resolved_values = dict(self.credential_resolver(scope))
                except DomainRejected as exc:
                    resolver_error = exc
                else:
                    if resolved_values.get("api_key") and resolved_values.get("api_secret"):
                        credential_values = resolved_values
                    else:
                        resolver_error = DomainRejected(
                            "CAPITAL_ACCOUNT_CREDENTIALS_NOT_READY",
                            "the verified Binance account credential is incomplete",
                        )
            if credential_values is None:
                configured = any(
                    value is not None
                    for value in (
                        self.binance_account_id,
                        self.binance_api_key,
                        self.binance_api_secret,
                    )
                )
                credentials_ready = all(
                    bool(value)
                    for value in (
                        self.binance_account_id,
                        self.binance_api_key,
                        self.binance_api_secret,
                    )
                )
                if configured and (
                    not credentials_ready or scope.account_id != self.binance_account_id
                ):
                    if resolver_error is not None:
                        raise resolver_error
                    raise DomainRejected(
                        "BINANCE_CAPITAL_CREDENTIALS_NOT_READY",
                        "dedicated Binance capital credentials do not match the exact account",
                    )
                if credentials_ready:
                    credential_values = {
                        "api_key": str(self.binance_api_key),
                        "api_secret": str(self.binance_api_secret),
                    }
            credentials_ready = credential_values is not None
            credential = CapitalCredential(
                account_id=scope.account_id,
                venue="BINANCE",
                purpose="CAPITAL",
                values=credential_values or {},
                permissions=(
                    frozenset({"READ", "TRANSFER", "WITHDRAW"})
                    if credentials_ready
                    else frozenset({"READ"})
                ),
            )
            binance_gateway = self.binance_gateway
            if credentials_ready:
                with_credentials = getattr(binance_gateway, "with_credentials", None)
                if not callable(with_credentials):
                    raise DomainRejected(
                        "CAPITAL_ACCOUNT_CREDENTIALS_NOT_READY",
                        "the Binance capital gateway cannot bind exact-account credentials",
                    )
                binance_gateway = with_credentials(
                    api_key=str(credential.values["api_key"]),
                    api_secret=str(credential.values["api_secret"]),
                )
            gateways: dict[CapitalVenue, Any] = {
                "BINANCE": binance_gateway,
                "HYPERLIQUID": self.hyperliquid_gateway,
            }
        elif scope.venue == "HYPERLIQUID":
            credential = CapitalCredential(
                account_id=scope.account_id,
                venue="HYPERLIQUID",
                purpose="CAPITAL",
                values={},
                permissions=frozenset({"READ", "TRANSFER"}),
            )
            gateways = {"HYPERLIQUID": self.hyperliquid_gateway}
        else:
            raise DomainRejected(
                "CAPITAL_CREDENTIALS_NOT_READY",
                "no dedicated capital credential is configured for the exact account",
            )

        contracts = {
            (
                scope.venue if operation in self._GENERIC_CHAIN_OPERATIONS else venue,
                operation,
            ): f"native.restricted.{method}"
            for operation, (venue, method) in self._NATIVE_METHODS.items()
            if venue == scope.venue or operation in self._GENERIC_CHAIN_OPERATIONS
        }

        def execute_native(
            selected_scope: CapitalScope,
            operation: CapitalOperation,
            parameters: Mapping[str, Any],
        ) -> Any:
            venue, method_name = self._NATIVE_METHODS[operation]
            if venue != selected_scope.venue and operation not in self._GENERIC_CHAIN_OPERATIONS:
                raise DomainRejected(
                    "CAPITAL_BACKEND_SCOPE_MISMATCH",
                    "capital fallback is outside the exact venue scope",
                )
            kwargs = dict(parameters)
            if operation in {
                CapitalOperation.BINANCE_SUBMIT_WITHDRAWAL,
                CapitalOperation.BINANCE_COMPLETE_DEPOSIT,
            }:
                kwargs.pop("operation_id", None)
            return getattr(gateways[venue], method_name)(**kwargs)

        backends: list[CapitalBackend] = []
        if credential.values:
            backends.append(
                build_ccxt_capital_backend(
                    scope,
                    credential,
                    exchange_factory=self.exchange_factory,
                )
            )
        backends.append(
            CallableCapitalBackend(
                name="NATIVE_RESTRICTED",
                contracts=contracts,
                executor=execute_native,
            )
        )
        return CapitalAdapter(scope=scope, credential=credential, backends=backends)


def build_production_capital_adapter_factory(
    *,
    binance_account_id: str | None,
    binance_api_key: str | None,
    binance_api_secret: str | None,
    binance_base_url: str,
    binance_recv_window_ms: int,
    binance_timeout_seconds: float,
    binance_request_state: Any,
    credential_resolver: CapitalCredentialResolver | None = None,
) -> ProductionCapitalAdapterFactory:
    """Construct native fallbacks only inside the isolated capital package."""

    from trading_control_plane.adapters.binance_capital import BinanceCapitalGateway
    from trading_control_plane.adapters.hyperliquid_capital import HyperliquidCapitalGateway

    binance = BinanceCapitalGateway(
        base_url=binance_base_url,
        api_key=binance_api_key,
        api_secret=binance_api_secret,
        recv_window_ms=binance_recv_window_ms,
        timeout_seconds=binance_timeout_seconds,
        request_state=binance_request_state,
    )
    binance.attach_request_state(binance_request_state)
    return ProductionCapitalAdapterFactory(
        binance_account_id=binance_account_id,
        binance_api_key=binance_api_key,
        binance_api_secret=binance_api_secret,
        binance_gateway=binance,
        hyperliquid_gateway=HyperliquidCapitalGateway(timeout_seconds=5),
        credential_resolver=credential_resolver,
    )


__all__ = [
    "CallableCapitalBackend",
    "CapabilityProbe",
    "CapitalAdapter",
    "CapitalAdapterFactory",
    "CapitalBackend",
    "CapitalCredential",
    "CapitalCredentialResolver",
    "CapitalOperation",
    "CapitalResult",
    "CapitalScope",
    "CcxtUnifiedCapitalBackend",
    "ProductionCapitalAdapterFactory",
    "build_ccxt_capital_backend",
    "build_production_capital_adapter_factory",
]
