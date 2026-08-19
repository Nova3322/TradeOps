from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from trading_control_plane.adapters.capital import (
    CallableCapitalBackend,
    CapitalAdapter,
    CapitalCredential,
    CapitalOperation,
    CapitalScope,
    CcxtUnifiedCapitalBackend,
    build_ccxt_capital_backend,
)
from trading_control_plane.domain import DomainRejected


def _scope(venue: str = "BINANCE", account_id: str = "account-a") -> CapitalScope:
    return CapitalScope(
        workspace_id="workspace-a",
        team_id="team-a",
        account_id=account_id,
        venue=venue,  # type: ignore[arg-type]
        environment="LIVE",
    )


def _credential(
    venue: str = "BINANCE",
    account_id: str = "account-a",
    *,
    permissions: frozenset[str] = frozenset({"READ", "TRANSFER"}),
) -> CapitalCredential:
    return CapitalCredential(
        account_id=account_id,
        venue=venue,  # type: ignore[arg-type]
        purpose="CAPITAL",
        values={"api_key": "capital-only-key", "api_secret": "capital-only-secret"},
        permissions=permissions,
    )


def _write_parameters(**values: Any) -> dict[str, Any]:
    return {"operation_id": "capital-operation-1", **values}


class FakeCcxtCapitalExchange:
    def __init__(self, *, transfer: bool) -> None:
        self.has = {
            "transfer": transfer,
            "withdraw": False,
            "fetchDeposits": False,
            "fetchWithdrawals": False,
            "addMargin": False,
            "reduceMargin": False,
        }
        self.calls: list[tuple[tuple[Any, ...], Mapping[str, Any]]] = []
        self.read_probes = 0

    def load_markets(self) -> Mapping[str, object]:
        self.read_probes += 1
        return {"BTC/USDT:USDT": {}}

    def fetch_balance(self) -> Mapping[str, object]:
        self.read_probes += 1
        return {"USDT": {"free": "1"}}

    def transfer(self, *args: Any, **kwargs: Any) -> Mapping[str, str]:
        self.calls.append((args, kwargs))
        return {"id": "transfer-1", "status": "ok"}


@pytest.mark.parametrize("venue", ["BINANCE", "HYPERLIQUID", "OKX", "BYBIT"])
def test_capital_contract_prefers_verified_ccxt_unified_interface(venue: str) -> None:
    exchange = FakeCcxtCapitalExchange(transfer=True)
    fallbacks: list[str] = []
    implicit = CallableCapitalBackend(
        name="CCXT_IMPLICIT",
        contracts={(venue, CapitalOperation.TRANSFER): "fixture.implicit"},  # type: ignore[dict-item]
        executor=lambda *_args: fallbacks.append("implicit"),
    )
    adapter = CapitalAdapter(
        scope=_scope(venue),
        credential=_credential(venue),
        backends=(CcxtUnifiedCapitalBackend(exchange), implicit),
    )

    result = adapter.execute(
        CapitalOperation.TRANSFER,
        _write_parameters(
            args=("USDT", "1", "spot", "swap"),
            kwargs={"params": {}},
        ),
    )

    assert result.backend == "CCXT_UNIFIED"
    assert result.contract == "ccxt.unified.transfer"
    assert result.value == {"id": "transfer-1", "status": "ok"}
    assert fallbacks == []
    assert len(exchange.calls) == 1
    assert exchange.read_probes == 2


def test_capital_contract_falls_back_only_after_explicit_unsupported_probe() -> None:
    exchange = FakeCcxtCapitalExchange(transfer=False)
    calls: list[str] = []

    def execute_implicit(*_args: object) -> Mapping[str, int]:
        calls.append("implicit")
        return {"tranId": 7}

    implicit = CallableCapitalBackend(
        name="CCXT_IMPLICIT",
        contracts={("BINANCE", CapitalOperation.TRANSFER): "binance.sapi.asset.transfer"},
        executor=execute_implicit,
    )
    adapter = CapitalAdapter(
        scope=_scope(),
        credential=_credential(),
        backends=(CcxtUnifiedCapitalBackend(exchange), implicit),
    )

    result = adapter.execute(CapitalOperation.TRANSFER, _write_parameters())

    assert result.backend == "CCXT_IMPLICIT"
    assert result.value == {"tranId": 7}
    assert calls == ["implicit"]
    assert exchange.calls == []


def test_capital_contract_does_not_fallback_after_unknown_write_result() -> None:
    class UnknownExchange(FakeCcxtCapitalExchange):
        def transfer(self, *args: Any, **kwargs: Any) -> Mapping[str, str]:
            del args, kwargs
            raise TimeoutError("fixture-secret-must-not-leak")

    fallbacks: list[str] = []
    adapter = CapitalAdapter(
        scope=_scope(),
        credential=_credential(),
        backends=(
            CcxtUnifiedCapitalBackend(UnknownExchange(transfer=True)),
            CallableCapitalBackend(
                name="NATIVE_RESTRICTED",
                contracts={("BINANCE", CapitalOperation.TRANSFER): "binance.native.transfer"},
                executor=lambda *_args: fallbacks.append("native"),
            ),
        ),
    )

    with pytest.raises(DomainRejected) as captured:
        adapter.execute(
            CapitalOperation.TRANSFER,
            _write_parameters(args=(), kwargs={}),
        )

    assert captured.value.code == "CAPITAL_RESULT_UNKNOWN"
    assert "fixture-secret" not in str(captured.value)
    assert fallbacks == []


def test_capital_credential_scope_and_withdraw_permission_are_fail_closed() -> None:
    backend = CcxtUnifiedCapitalBackend(FakeCcxtCapitalExchange(transfer=True))
    with pytest.raises(DomainRejected, match="CAPITAL_CREDENTIAL_SCOPE_MISMATCH"):
        CapitalAdapter(
            scope=_scope(account_id="account-a"),
            credential=_credential(account_id="account-b"),
            backends=(backend,),
        )

    adapter = CapitalAdapter(
        scope=_scope(),
        credential=_credential(),
        backends=(backend,),
    )
    with pytest.raises(DomainRejected, match="CAPITAL_WITHDRAW_PERMISSION_MISSING"):
        adapter.execute(CapitalOperation.WITHDRAW, _write_parameters())


@pytest.mark.parametrize(
    ("selected_name", "implicit_contracts", "sdk_contracts", "native_contracts"),
    [
        ("CCXT_IMPLICIT", True, True, True),
        ("OFFICIAL_SDK", False, True, True),
        ("NATIVE_RESTRICTED", False, False, True),
    ],
)
def test_capital_contract_uses_the_first_explicitly_verified_fallback(
    selected_name: str,
    implicit_contracts: bool,
    sdk_contracts: bool,
    native_contracts: bool,
) -> None:
    calls: list[str] = []

    def backend(
        name: str,
        enabled: bool,
    ) -> CallableCapitalBackend:
        return CallableCapitalBackend(
            name=name,  # type: ignore[arg-type]
            contracts=(
                {("BINANCE", CapitalOperation.TRANSFER): f"{name}.transfer"} if enabled else {}
            ),
            executor=lambda *_args: calls.append(name) or {"id": f"{name}-1"},
        )

    adapter = CapitalAdapter(
        scope=_scope(),
        credential=_credential(),
        backends=(
            CcxtUnifiedCapitalBackend(FakeCcxtCapitalExchange(transfer=False)),
            backend("CCXT_IMPLICIT", implicit_contracts),
            backend("OFFICIAL_SDK", sdk_contracts),
            backend("NATIVE_RESTRICTED", native_contracts),
        ),
    )

    result = adapter.execute(CapitalOperation.TRANSFER, _write_parameters())

    assert result.backend == selected_name
    assert calls == [selected_name]


def test_capital_contract_rejects_unsupported_and_unidentified_results() -> None:
    unsupported = CapitalAdapter(
        scope=_scope(),
        credential=_credential(),
        backends=(CcxtUnifiedCapitalBackend(FakeCcxtCapitalExchange(transfer=False)),),
    )
    with pytest.raises(DomainRejected, match="CAPITAL_OPERATION_UNSUPPORTED"):
        unsupported.execute(CapitalOperation.TRANSFER, _write_parameters())

    fallbacks: list[str] = []
    unidentified = CapitalAdapter(
        scope=_scope(),
        credential=_credential(),
        backends=(
            CallableCapitalBackend(
                name="CCXT_IMPLICIT",
                contracts={("BINANCE", CapitalOperation.TRANSFER): "binance.sapi.asset.transfer"},
                executor=lambda *_args: {"status": "accepted-without-id"},
            ),
            CallableCapitalBackend(
                name="NATIVE_RESTRICTED",
                contracts={("BINANCE", CapitalOperation.TRANSFER): "binance.native.transfer"},
                executor=lambda *_args: fallbacks.append("native") or {"id": "native-1"},
            ),
        ),
    )
    with pytest.raises(DomainRejected, match="CAPITAL_RESULT_UNKNOWN"):
        unidentified.execute(CapitalOperation.TRANSFER, _write_parameters())
    assert fallbacks == []


def test_ccxt_capital_client_is_built_from_the_dedicated_exact_account_credential() -> None:
    observed: dict[str, object] = {}
    credential = _credential()

    def factory(scope: CapitalScope, supplied: CapitalCredential) -> FakeCcxtCapitalExchange:
        observed.update(scope=scope, credential=supplied)
        return FakeCcxtCapitalExchange(transfer=True)

    backend = build_ccxt_capital_backend(
        _scope(),
        credential,
        exchange_factory=factory,
    )

    assert backend.probe(_scope(), CapitalOperation.TRANSFER).supported is True
    assert observed == {"scope": _scope(), "credential": credential}
    assert "capital-only-secret" not in repr(credential)


def test_capital_write_requires_a_durable_control_plane_operation_identity() -> None:
    adapter = CapitalAdapter(
        scope=_scope(),
        credential=_credential(),
        backends=(CcxtUnifiedCapitalBackend(FakeCcxtCapitalExchange(transfer=True)),),
    )

    with pytest.raises(DomainRejected, match="CAPITAL_IDEMPOTENCY_KEY_REQUIRED"):
        adapter.execute(CapitalOperation.TRANSFER, {})
