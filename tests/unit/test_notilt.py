from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from subprocess import TimeoutExpired
from typing import Any
from urllib.error import URLError

import pytest

import trading_control_plane.notilt as notilt_module
from trading_control_plane.domain import DomainRejected
from trading_control_plane.notilt import NoTiltGateway, NoTiltUsdValuator

VAULT = "0x1111111111111111111111111111111111111111"
AGENT = "0x2222222222222222222222222222222222222222"
OWNER = "0x3333333333333333333333333333333333333333"
ASSET = "0xaf88d065e77c8cc2239327c5edb3a432268e5831"
REQUEST_ID = f"0x{'a' * 64}"


def transaction(function_name: str, *, value: str = "0") -> dict[str, Any]:
    return {
        "chainId": 42161,
        "to": VAULT,
        "data": "0x1234",
        "value": value,
        "contract": "vault",
        "functionName": function_name,
        "summary": f"Prepare {function_name}",
    }


def budget() -> dict[str, Any]:
    return {
        "blockNumber": "123",
        "blockTimestamp": "1785480000",
        "vault": VAULT,
        "agent": AGENT,
        "asset": {
            "address": ASSET,
            "symbol": "USDC",
            "decimals": 6,
            "native": False,
        },
        "owner": OWNER,
        "isOfficialVault": True,
        "isActiveWhitelist": True,
        "assignedWhitelistVault": VAULT,
        "balance": "10000000",
        "maxReleaseNet": "2500000",
        "pendingNet": "500000",
        "panicLocked": False,
        "dailyReleaseRate": "100000000000000000",
        "dailyFeeRate": "50000000000000000",
    }


def executor(payload: dict[str, Any]) -> dict[str, Any]:
    operation = payload["operation"]
    if operation == "resolve-assignment":
        return {"assignedVault": VAULT, "active": True}
    if operation == "verify-deployment":
        return {"chainId": 42161, "chain": "arbitrum", "result": {"active": True}}
    if operation == "read-vault":
        return {
            "chainId": 42161,
            "chain": "arbitrum",
            "vault": VAULT,
            "agent": AGENT,
            "budgets": [budget()],
        }
    if operation == "prepare-deposit":
        return {
            "transactions": [
                {
                    **transaction("approve"),
                    "contract": "erc20",
                    "to": ASSET,
                },
                transaction("deposit"),
            ]
        }
    if operation == "prepare-release-request":
        return {"transaction": transaction("requestWhitelistRelease")}
    if operation == "prepare-release-execution":
        return transaction("executeWhitelistRelease")
    if operation == "prepare-release-cancellation":
        return transaction("cancelWhitelistRelease")
    raise AssertionError(operation)


def test_gateway_maps_registry_deployment_and_vault_budget() -> None:
    gateway = NoTiltGateway(executor=executor)

    assert gateway.resolve_assignment(42161, AGENT) == (VAULT, True)
    assert gateway.verify_deployment(42161)["result"] == {"active": True}
    snapshot = gateway.read_vault(42161, VAULT, AGENT)

    assert snapshot.chain == "ARBITRUM"
    assert snapshot.vault == VAULT
    assert len(snapshot.budgets) == 1
    item = snapshot.budgets[0]
    assert item.balance == Decimal("10")
    assert item.max_release_net == Decimal("2.5")
    assert item.pending_net == Decimal("0.5")
    assert item.daily_release_rate == Decimal("0.1")
    assert item.daily_fee_rate == Decimal("0.05")
    assert item.block_timestamp == datetime.fromtimestamp(1_785_480_000, UTC)


def test_gateway_maps_only_constrained_unsigned_transactions() -> None:
    gateway = NoTiltGateway(executor=executor)

    deposit = gateway.prepare_deposit(
        chain_id=42161,
        vault=VAULT,
        agent=AGENT,
        asset="USDC",
        amount="1",
    )
    request = gateway.prepare_release_request(
        chain_id=42161,
        vault=VAULT,
        agent=AGENT,
        asset="USDC",
        amount="1",
    )
    execution = gateway.prepare_release_execution(
        chain_id=42161,
        vault=VAULT,
        agent=AGENT,
        request_id=REQUEST_ID,
    )
    cancellation = gateway.prepare_release_cancellation(
        chain_id=42161,
        vault=VAULT,
        agent=AGENT,
        request_id=REQUEST_ID,
    )

    assert [item.function_name for item in deposit] == ["approve", "deposit"]
    assert request.function_name == "requestWhitelistRelease"
    assert execution.function_name == "executeWhitelistRelease"
    assert cancellation.function_name == "cancelWhitelistRelease"
    assert all(item.value == 0 for item in (*deposit, request, execution, cancellation))


@pytest.mark.parametrize("chain_id", [0, 10, 421614])
def test_gateway_rejects_non_production_chains_before_external_call(chain_id: int) -> None:
    gateway = NoTiltGateway(executor=lambda _payload: pytest.fail("executor must not run"))
    with pytest.raises(DomainRejected, match="NOTILT_CHAIN_UNSUPPORTED"):
        gateway.resolve_assignment(chain_id, AGENT)


def test_gateway_rejects_invalid_budget_and_transaction_shapes() -> None:
    invalid_budget = NoTiltGateway(
        executor=lambda _payload: {
            "chain": "arbitrum",
            "vault": VAULT,
            "agent": AGENT,
            "budgets": [{**budget(), "balance": "-1"}],
        }
    )
    with pytest.raises(DomainRejected, match="NOTILT_RESPONSE_INVALID"):
        invalid_budget.read_vault(42161, VAULT, AGENT)

    invalid_transaction = NoTiltGateway(
        executor=lambda _payload: {
            "transaction": {
                **transaction("requestWhitelistAdd"),
                "functionName": "requestWhitelistAdd",
            }
        }
    )
    with pytest.raises(DomainRejected, match="NOTILT_RESPONSE_INVALID"):
        invalid_transaction.prepare_release_request(
            chain_id=42161,
            vault=VAULT,
            agent=AGENT,
            asset="USDC",
            amount="1",
        )


def test_usd_valuator_uses_par_for_stables_and_fixed_public_marks_for_native_assets() -> None:
    calls: list[str] = []

    def fetcher(url: str, _timeout: float) -> dict[str, Any]:
        calls.append(url)
        return {
            "symbol": "ETHUSDT",
            "markPrice": "3000",
            "time": 1_785_480_000_000,
        }

    valuator = NoTiltUsdValuator(fetcher)
    now = datetime.fromtimestamp(1_785_480_010, UTC)

    stable = valuator.value("USDC", Decimal("25"), now=now)
    native = valuator.value("ETH", Decimal("0.01"), now=now)
    zero_native = valuator.value("BNB", Decimal(0), now=now)

    assert stable.price == Decimal(1)
    assert stable.value == Decimal(25)
    assert native.price == Decimal(3000)
    assert native.value == Decimal(30)
    assert zero_native.value == Decimal(0)
    assert len(calls) == 1
    assert "symbol=ETHUSDT" in calls[0]


def test_usd_valuator_fails_closed_for_unknown_or_invalid_prices() -> None:
    now = datetime.fromtimestamp(1_785_480_010, UTC)
    valuator = NoTiltUsdValuator(
        lambda _url, _timeout: {
            "symbol": "ETHUSDT",
            "markPrice": "0",
            "time": 1_785_480_000_000,
        }
    )

    with pytest.raises(DomainRejected, match="NOTILT_VALUATION_UNSUPPORTED"):
        valuator.value("UNKNOWN", Decimal(1), now=now)
    with pytest.raises(DomainRejected, match="NOTILT_VALUATION_INVALID"):
        valuator.value("ETH", Decimal(1), now=now)
    with pytest.raises(DomainRejected, match="NOTILT_VALUATION_INVALID"):
        valuator.value("USDC", Decimal("-1"), now=now)


class FakeHttpResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> FakeHttpResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def test_default_price_transport_maps_json_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.fromtimestamp(1_785_480_010, UTC)
    monkeypatch.setattr(
        notilt_module.urllib.request,
        "urlopen",
        lambda _request, timeout: FakeHttpResponse(
            b'{"symbol":"ETHUSDT","markPrice":"3000","time":1785480000000}'
        ),
    )
    assert NoTiltUsdValuator().value("ETH", Decimal("0.01"), now=now).value == Decimal("30")

    monkeypatch.setattr(
        notilt_module.urllib.request,
        "urlopen",
        lambda _request, timeout: FakeHttpResponse(b"[]"),
    )
    with pytest.raises(DomainRejected, match="NOTILT_VALUATION_INVALID"):
        NoTiltUsdValuator().value("ETH", Decimal("0.01"), now=now)

    def unavailable(_request: object, timeout: float) -> FakeHttpResponse:
        raise URLError("offline")

    monkeypatch.setattr(notilt_module.urllib.request, "urlopen", unavailable)
    with pytest.raises(DomainRejected, match="NOTILT_VALUATION_UNAVAILABLE"):
        NoTiltUsdValuator().value("ETH", Decimal("0.01"), now=now)


def test_subprocess_gateway_maps_success_and_failure_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = NoTiltGateway()
    monkeypatch.setattr(notilt_module.shutil, "which", lambda _name: "/usr/bin/node")

    def complete(stdout: str, returncode: int = 0) -> object:
        return type("Completed", (), {"stdout": stdout, "returncode": returncode})()

    monkeypatch.setattr(
        notilt_module.subprocess,
        "run",
        lambda *_args, **_kwargs: complete(
            f'{{"ok":true,"data":{{"assignedVault":"{VAULT}","active":true}}}}'
        ),
    )
    assert gateway.resolve_assignment(42161, AGENT) == (VAULT, True)

    monkeypatch.setattr(
        notilt_module.subprocess,
        "run",
        lambda *_args, **_kwargs: complete(
            '{"ok":false,"error":{"message":"release rejected"}}',
            1,
        ),
    )
    with pytest.raises(DomainRejected, match="NOTILT_OPERATION_REJECTED"):
        gateway.resolve_assignment(42161, AGENT)

    monkeypatch.setattr(
        notilt_module.subprocess,
        "run",
        lambda *_args, **_kwargs: complete("not-json"),
    )
    with pytest.raises(DomainRejected, match="NOTILT_RESPONSE_INVALID"):
        gateway.resolve_assignment(42161, AGENT)

    def timeout(*_args: object, **_kwargs: object) -> object:
        raise TimeoutExpired("node", 1)

    monkeypatch.setattr(notilt_module.subprocess, "run", timeout)
    with pytest.raises(DomainRejected, match="NOTILT_GATEWAY_UNAVAILABLE"):
        gateway.resolve_assignment(42161, AGENT)
