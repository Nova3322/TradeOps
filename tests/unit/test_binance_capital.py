from __future__ import annotations

import io
import time
import urllib.error
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from trading_control_plane.binance_capital import BinanceCapitalGateway
from trading_control_plane.domain import DomainRejected

DESTINATION = "0x1111111111111111111111111111111111111111"
SOURCE = "0x2222222222222222222222222222222222222222"
TX_HASH = "0x" + "ab" * 32
NOW = datetime(2026, 8, 8, 12, tzinfo=UTC)


def responses(*, ip_restrict: bool = True, destination: str = DESTINATION) -> dict[str, Any]:
    return {
        "/sapi/v1/account/apiRestrictions": {
            "ipRestrict": ip_restrict,
            "enableReading": True,
            "enableWithdrawals": True,
        },
        "/sapi/v1/capital/config/getall": [
            {
                "coin": "USDC",
                "free": "250",
                "networkList": [
                    {
                        "network": "ARBITRUM",
                        "depositEnable": True,
                        "withdrawEnable": True,
                        "busy": False,
                        "withdrawTag": False,
                        "withdrawFee": "1",
                        "withdrawMin": "5",
                        "withdrawMax": "1000",
                    }
                ],
            }
        ],
        "/sapi/v1/capital/deposit/address": {
            "coin": "USDC",
            "address": destination,
            "tag": "",
        },
        "/sapi/v1/capital/withdraw/address/list": [
            {
                "coin": "USDC",
                "network": "ARBITRUM",
                "address": destination,
                "whiteStatus": True,
            }
        ],
        "/sapi/v1/localentity/questionnaire-requirements": "NIL",
        "/sapi/v1/capital/withdraw/quota": {"wdQuota": "1000", "usedWdQuota": "20"},
    }


def gateway(
    values: dict[str, Any], calls: list[tuple[str, str, dict[str, str]]] | None = None
) -> BinanceCapitalGateway:
    def transport(method: str, path: str, params: dict[str, str], _: float) -> Any:
        if calls is not None:
            calls.append((method, path, params))
        response = values[path]
        return response(method, params) if callable(response) else response

    return BinanceCapitalGateway(
        api_key="capital-key",
        api_secret="capital-secret",  # noqa: S106 - inert fixture credential
        transport=transport,
        clock_ms=lambda: 1_786_190_400_000,
    )


def test_deposit_preflight_matches_live_address_without_exposing_credentials() -> None:
    calls: list[tuple[str, str, dict[str, str]]] = []
    client = gateway(responses(), calls)

    artifact = client.prepare_deposit(
        expected_address=DESTINATION,
        amount=Decimal("10.5"),
        source_address=SOURCE,
        now=NOW,
    )

    assert artifact["kind"] == "BINANCE_ARBITRUM_DEPOSIT_PREFLIGHT"
    assert artifact["destination"] == DESTINATION
    assert artifact["signing"] is False
    assert artifact["broadcast"] is False
    assert "capital-key" not in repr(client)
    assert "capital-secret" not in repr(client)
    assert all("signature" not in params for _, _, params in calls)
    assert all("capital-key" not in str(params) for _, _, params in calls)


def test_deposit_preflight_rejects_runtime_address_drift() -> None:
    values = responses(destination=SOURCE)
    with pytest.raises(DomainRejected, match="frozen configured address") as caught:
        gateway(values).prepare_deposit(
            expected_address=DESTINATION,
            amount=Decimal("10"),
            source_address=SOURCE,
            now=NOW,
        )
    assert caught.value.code == "BINANCE_CAPITAL_DEPOSIT_ADDRESS_MISMATCH"


def test_withdrawal_preflight_checks_ip_permission_allowlist_fee_balance_and_quota() -> None:
    artifact = gateway(responses()).prepare_withdrawal(
        destination=DESTINATION,
        amount=Decimal("25"),
        max_fee=Decimal("2"),
        operation_id="operation-1",
        now=NOW,
    )

    assert artifact["kind"] == "BINANCE_RESTRICTED_WITHDRAWAL_PREFLIGHT"
    assert artifact["withdrawOrderId"] == "operation-1"
    assert artifact["fee"] == "1"
    assert artifact["minReceived"] == "24"
    assert artifact["credentialMaterialIncluded"] is False

    with pytest.raises(DomainRejected) as caught:
        gateway(responses(ip_restrict=False)).prepare_withdrawal(
            destination=DESTINATION,
            amount=Decimal("25"),
            max_fee=Decimal("2"),
            operation_id="operation-2",
            now=NOW,
        )
    assert caught.value.code == "BINANCE_CAPITAL_IP_RESTRICTION_REQUIRED"


def test_withdrawal_submission_is_idempotent_and_uses_fixed_order_id() -> None:
    calls: list[tuple[str, str, dict[str, str]]] = []
    values = responses()
    values["/sapi/v1/capital/withdraw/history"] = []
    values["/sapi/v1/capital/withdraw/apply"] = {"id": "withdrawal-1"}
    client = gateway(values, calls)
    artifact = client.prepare_withdrawal(
        destination=DESTINATION,
        amount=Decimal("25"),
        max_fee=Decimal("2"),
        operation_id="operation-1",
        now=NOW,
    )

    result = client.submit_withdrawal(artifact, now=NOW)

    assert result["withdrawalId"] == "withdrawal-1"
    submit = next(call for call in calls if call[1].endswith("/withdraw/apply"))
    assert submit[0] == "POST"
    assert submit[2]["withdrawOrderId"] == "operation-1"
    assert submit[2]["address"] == DESTINATION


def test_exact_binance_deposit_and_withdrawal_receipts_are_verified() -> None:
    values = responses()
    values["/sapi/v1/capital/deposit/hisrec"] = [
        {
            "id": "deposit-1",
            "coin": "USDC",
            "network": "ARBITRUM",
            "address": DESTINATION,
            "txId": TX_HASH,
            "amount": "10",
            "status": 1,
            "confirmTimes": "20/20",
            "completeTime": 1_786_190_400_000,
        }
    ]
    values["/sapi/v1/capital/withdraw/history"] = [
        {
            "id": "withdrawal-1",
            "withdrawOrderId": "operation-1",
            "coin": "USDC",
            "network": "ARBITRUM",
            "address": DESTINATION,
            "txId": TX_HASH,
            "amount": "25",
            "transactionFee": "1",
            "status": 6,
        }
    ]
    client = gateway(values)

    deposit = client.verify_deposit(
        transaction_hash=TX_HASH,
        destination=DESTINATION,
        amount=Decimal("10"),
    )
    withdrawal = client.verify_withdrawal(
        order_id="operation-1",
        destination=DESTINATION,
        amount=Decimal("25"),
    )

    assert deposit["status"] == "CONFIRMED"
    assert withdrawal["status"] == "CONFIRMED"
    assert withdrawal["transactionHash"] == TX_HASH


def test_travel_rule_scope_fails_closed_instead_of_using_wrong_endpoint() -> None:
    values = responses()
    values["/sapi/v1/localentity/questionnaire-requirements"] = {"questionnaireCountryCode": "AE"}
    with pytest.raises(DomainRejected) as caught:
        gateway(values).prepare_withdrawal(
            destination=DESTINATION,
            amount=Decimal("25"),
            max_fee=Decimal("2"),
            operation_id="operation-1",
            now=NOW,
        )
    assert caught.value.code == "BINANCE_CAPITAL_TRAVEL_RULE_REQUIRED"


def test_read_only_wallet_request_fails_over_once_to_an_official_host(monkeypatch) -> None:
    class Response:
        def __init__(self, body: bytes) -> None:
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self) -> bytes:
            return self.body

    wallet_calls = 0

    def urlopen(request, *, timeout):
        nonlocal wallet_calls
        assert timeout == 8
        if request.full_url.endswith("/api/v3/time"):
            return Response(b'{"serverTime":1786190400000}')
        wallet_calls += 1
        if wallet_calls == 1:
            raise urllib.error.URLError("transient TLS failure")
        return Response(b'{"ok":true}')

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    client = BinanceCapitalGateway(
        api_key="capital-key",
        api_secret="capital-secret",  # noqa: S106 - inert fixture credential
        clock_ms=lambda: 1_786_190_400_000,
    )
    client._clock_synchronized_at = time.monotonic()

    result = client._request("GET", "/sapi/v1/account/apiRestrictions")

    assert result == {"ok": True}
    assert wallet_calls == 2


def test_server_time_fails_over_across_documented_official_hosts(monkeypatch) -> None:
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self) -> bytes:
            return b'{"serverTime":1786190400123}'

    calls: list[tuple[str, float]] = []

    def urlopen(request, *, timeout):
        calls.append((request.full_url, timeout))
        if request.full_url.startswith("https://api.binance.com/"):
            raise urllib.error.URLError("transient TLS EOF")
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    client = BinanceCapitalGateway(
        api_key="capital-key",
        api_secret="capital-secret",  # noqa: S106 - inert fixture credential
        clock_ms=lambda: 1_786_190_400_000,
    )

    assert client._timestamp_ms() == 1_786_190_400_123
    assert calls == [
        ("https://api.binance.com/api/v3/time", 4.0),
        ("https://api1.binance.com/api/v3/time", 4.0),
    ]


def test_wallet_get_fails_over_and_reuses_reachable_official_host(monkeypatch) -> None:
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self) -> bytes:
            return b'{"ok":true}'

    calls: list[str] = []

    def urlopen(request, *, timeout):
        assert timeout == 8
        calls.append(request.full_url)
        if request.full_url.startswith("https://api.binance.com/"):
            raise urllib.error.URLError("transient TLS EOF")
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    client = BinanceCapitalGateway(
        api_key="capital-key",
        api_secret="capital-secret",  # noqa: S106 - inert fixture credential
        clock_ms=lambda: 1_786_190_400_000,
    )
    client._clock_synchronized_at = time.monotonic()

    assert client._request("GET", "/sapi/v1/account/apiRestrictions") == {"ok": True}
    assert client._request("GET", "/sapi/v1/capital/config/getall") == {"ok": True}

    assert calls[0].startswith("https://api.binance.com/")
    assert calls[1].startswith("https://api1.binance.com/")
    assert calls[2].startswith("https://api1.binance.com/")


@pytest.mark.parametrize(
    ("http_status", "exchange_code", "expected_code"),
    [
        (400, -2015, "BINANCE_CAPITAL_AUTHORIZATION_REJECTED"),
        (400, -1021, "BINANCE_CAPITAL_TIMESTAMP_REJECTED"),
        (429, -1003, "BINANCE_CAPITAL_RATE_LIMITED"),
        (500, -1000, "BINANCE_CAPITAL_API_UNAVAILABLE"),
    ],
)
def test_wallet_api_rejections_have_actionable_non_sensitive_codes(
    monkeypatch, http_status: int, exchange_code: int, expected_code: str
) -> None:
    def urlopen(request, *, timeout):
        assert timeout == 8
        raise urllib.error.HTTPError(
            request.full_url,
            http_status,
            "rejected",
            {},
            io.BytesIO(f'{{"code":{exchange_code},"msg":"sensitive detail"}}'.encode()),
        )

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    client = BinanceCapitalGateway(
        api_key="capital-key",
        api_secret="capital-secret",  # noqa: S106 - inert fixture credential
        clock_ms=lambda: 1_786_190_400_000,
    )
    client._clock_synchronized_at = time.monotonic()

    with pytest.raises(DomainRejected) as caught:
        client._request("GET", "/sapi/v1/account/apiRestrictions")

    assert caught.value.code == expected_code
    assert "sensitive detail" not in caught.value.detail


def test_withdrawal_post_is_never_retried_after_transport_failure(monkeypatch) -> None:
    calls = 0

    def urlopen(_request, *, timeout):
        nonlocal calls
        assert timeout == 8
        calls += 1
        raise urllib.error.URLError("submission outcome unknown")

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    client = BinanceCapitalGateway(
        api_key="capital-key",
        api_secret="capital-secret",  # noqa: S106 - inert fixture credential
        clock_ms=lambda: 1_786_190_400_000,
    )
    client._clock_synchronized_at = time.monotonic()

    with pytest.raises(DomainRejected) as caught:
        client._request("POST", "/sapi/v1/capital/withdraw/apply")

    assert caught.value.code == "BINANCE_CAPITAL_API_UNAVAILABLE"
    assert calls == 1
