from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, Protocol

from trading_control_plane.domain import DomainRejected

CapitalVenue = Literal["BINANCE", "HYPERLIQUID", "OKX", "BYBIT"]


class CapitalOperation(StrEnum):
    TRANSFER = "TRANSFER"
    WITHDRAW = "WITHDRAW"
    FETCH_DEPOSITS = "FETCH_DEPOSITS"
    FETCH_WITHDRAWALS = "FETCH_WITHDRAWALS"
    ADD_MARGIN = "ADD_MARGIN"
    REDUCE_MARGIN = "REDUCE_MARGIN"


@dataclass(frozen=True, slots=True)
class CapitalScope:
    workspace_id: str
    team_id: str
    account_id: str
    venue: CapitalVenue
    environment: Literal["TESTNET", "LIVE"]

    def __post_init__(self) -> None:
        for name, value in (
            ("workspace_id", self.workspace_id),
            ("team_id", self.team_id),
            ("account_id", self.account_id),
        ):
            if not value or value != value.strip() or len(value) > 160:
                raise ValueError(f"capital adapter {name} is invalid")


@dataclass(frozen=True, slots=True)
class CapitalCredential:
    account_id: str
    venue: CapitalVenue
    purpose: Literal["CAPITAL"]
    values: Mapping[str, str] = field(repr=False)
    permissions: frozenset[str] = frozenset({"READ", "TRANSFER"})


CapitalExchangeFactory = Callable[[CapitalScope, CapitalCredential], Any]


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
_VERIFIED_UNIFIED_CONTRACTS: dict[CapitalVenue, frozenset[CapitalOperation]] = {
    "BINANCE": frozenset(
        {
            CapitalOperation.TRANSFER,
            CapitalOperation.WITHDRAW,
            CapitalOperation.FETCH_DEPOSITS,
            CapitalOperation.FETCH_WITHDRAWALS,
            CapitalOperation.ADD_MARGIN,
            CapitalOperation.REDUCE_MARGIN,
        }
    ),
    "HYPERLIQUID": frozenset(
        {
            CapitalOperation.TRANSFER,
            CapitalOperation.WITHDRAW,
            CapitalOperation.FETCH_DEPOSITS,
            CapitalOperation.FETCH_WITHDRAWALS,
        }
    ),
    "OKX": frozenset(
        {
            CapitalOperation.TRANSFER,
            CapitalOperation.WITHDRAW,
            CapitalOperation.FETCH_DEPOSITS,
            CapitalOperation.FETCH_WITHDRAWALS,
            CapitalOperation.ADD_MARGIN,
            CapitalOperation.REDUCE_MARGIN,
        }
    ),
    "BYBIT": frozenset(
        {
            CapitalOperation.TRANSFER,
            CapitalOperation.WITHDRAW,
            CapitalOperation.FETCH_DEPOSITS,
            CapitalOperation.FETCH_WITHDRAWALS,
        }
    ),
}


class CcxtUnifiedCapitalBackend:
    """CCXT unified capital calls gated by an explicit venue contract matrix."""

    name = "CCXT_UNIFIED"

    def __init__(self, exchange: Any) -> None:
        self._exchange = exchange

    def probe(self, scope: CapitalScope, operation: CapitalOperation) -> CapabilityProbe:
        capability = _UNIFIED_CAPABILITIES[operation]
        method_name = _UNIFIED_METHODS[operation]
        advertised = getattr(self._exchange, "has", {})
        callable_method = callable(getattr(self._exchange, method_name, None))
        verified = operation in _VERIFIED_UNIFIED_CONTRACTS[scope.venue]
        supported = (
            isinstance(advertised, Mapping)
            and advertised.get(capability) is True
            and callable_method
            and verified
        )
        return CapabilityProbe(
            supported=supported,
            contract=f"ccxt.unified.{method_name}",
            reason=None if supported else "capability or venue contract is not verified",
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


__all__ = [
    "CallableCapitalBackend",
    "CapabilityProbe",
    "CapitalAdapter",
    "CapitalBackend",
    "CapitalCredential",
    "CapitalOperation",
    "CapitalResult",
    "CapitalScope",
    "CcxtUnifiedCapitalBackend",
    "build_ccxt_capital_backend",
]
