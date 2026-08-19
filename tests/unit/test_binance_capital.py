from __future__ import annotations

import io
import time
import urllib.error
import urllib.parse
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from trading_control_plane.adapters.binance_capital import (
    OFFICIAL_BINANCE_BASE_URLS,
    BinanceCapitalGateway,
)
from trading_control_plane.binance_errors import BinanceApiDiagnostic, BinanceRequestState
from trading_control_plane.domain import DomainRejected

DESTINATION = "0x1111111111111111111111111111111111111111"
SOURCE = "0x2222222222222222222222222222222222222222"
TX_HASH = "0x" + "ab" * 32
NOW = datetime(2026, 8, 8, 12, tzinfo=UTC)


def test_binance_diagnostic_deserialization_rejects_ambiguous_integer_values() -> None:
    valid: dict[str, object] = {
        "category": "REQUEST_WEIGHT_EXCEEDED",
        "http_status": "429",
        "binance_error_code": -1003,
        "binance_error_message": "rate limited",
        "retry_after_seconds": 30,
        "rate_limit_headers": {},
        "failed_at": NOW.isoformat(),
        "next_retry_at": NOW.isoformat(),
    }
    assert BinanceApiDiagnostic.from_dict(valid) is not None
    for field, invalid in (
        ("http_status", True),
        ("http_status", 429.0),
        ("binance_error_code", "not-an-integer"),
        ("retry_after_seconds", object()),
    ):
        assert BinanceApiDiagnostic.from_dict({**valid, field: invalid}) is None


def responses(*, ip_restrict: bool = True, destination: str = DESTINATION) -> dict[str, Any]:
    return {
        "/sapi/v1/account/apiRestrictions": {
            "ipRestrict": ip_restrict,
            "enableReading": True,
            "enableWithdrawals": True,
            "enableInternalTransfer": True,
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
        "/sapi/v1/asset/transfer": {"rows": []},
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
    transfer_submitted = False

    def transfer_response(method: str, params: dict[str, str]) -> dict[str, object]:
        nonlocal transfer_submitted
        if method == "POST":
            transfer_submitted = True
            return {"tranId": 12345}
        return {
            "rows": (
                [
                    {
                        "asset": "USDC",
                        "type": "UMFUTURE_MAIN",
                        "amount": "25",
                        "status": "CONFIRMED",
                        "tranId": 12345,
                        "timestamp": int(NOW.timestamp() * 1000),
                    }
                ]
                if transfer_submitted
                else []
            )
        }

    values["/sapi/v1/asset/transfer"] = transfer_response
    values["/sapi/v3/asset/getUserAsset"] = [{"asset": "USDC", "free": "275"}]
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
    transfer = next(call for call in calls if call[:2] == ("POST", "/sapi/v1/asset/transfer"))
    assert transfer[2]["type"] == "UMFUTURE_MAIN"
    assert result["internalTransfer"]["status"] == "CONFIRMED"


def test_credited_deposit_is_moved_from_spot_to_usdm_once() -> None:
    calls: list[tuple[str, str, dict[str, str]]] = []
    values = responses()
    transfer_submitted = False

    def transfer_response(method: str, params: dict[str, str]) -> dict[str, object]:
        nonlocal transfer_submitted
        if method == "POST":
            transfer_submitted = True
            return {"tranId": 67890}
        return {
            "rows": (
                [
                    {
                        "asset": "USDC",
                        "type": "MAIN_UMFUTURE",
                        "amount": "10",
                        "status": "CONFIRMED",
                        "tranId": 67890,
                        "timestamp": int(NOW.timestamp() * 1000),
                    }
                ]
                if transfer_submitted
                else []
            )
        }

    values["/sapi/v1/asset/transfer"] = transfer_response
    result = gateway(values, calls).complete_deposit_to_usdm(
        amount=Decimal("10"), prepared_at=NOW, now=NOW
    )

    assert result["type"] == "MAIN_UMFUTURE"
    assert result["status"] == "CONFIRMED"
    assert len([call for call in calls if call[:2] == ("POST", "/sapi/v1/asset/transfer")]) == 1


def test_pending_internal_transfer_is_reconciled_without_duplicate_post() -> None:
    calls: list[tuple[str, str, dict[str, str]]] = []
    values = responses()
    values["/sapi/v1/asset/transfer"] = {
        "rows": [
            {
                "asset": "USDC",
                "type": "MAIN_UMFUTURE",
                "amount": "10",
                "status": "PENDING",
                "tranId": 67890,
                "timestamp": int(NOW.timestamp() * 1000),
            }
        ]
    }
    client = gateway(values, calls)

    for _attempt in range(2):
        with pytest.raises(DomainRejected) as caught:
            client.complete_deposit_to_usdm(amount=Decimal("10"), prepared_at=NOW, now=NOW)
        assert caught.value.code == "BINANCE_INTERNAL_TRANSFER_PENDING"

    assert len([call for call in calls if call[:2] == ("POST", "/sapi/v1/asset/transfer")]) == 0
    assert len([call for call in calls if call[:2] == ("GET", "/sapi/v1/asset/transfer")]) == 2


def test_false_api_restriction_flag_does_not_override_actual_transfer_endpoint() -> None:
    values = responses()
    values["/sapi/v1/account/apiRestrictions"]["enableInternalTransfer"] = False
    calls: list[tuple[str, str, dict[str, str]]] = []

    artifact = gateway(values, calls).prepare_deposit(
        expected_address=DESTINATION,
        amount=Decimal("10"),
        source_address=SOURCE,
        now=NOW,
    )

    assert artifact["kind"] == "BINANCE_ARBITRUM_DEPOSIT_PREFLIGHT"
    probe = next(call for call in calls if call[:2] == ("GET", "/sapi/v1/asset/transfer"))
    assert probe[2]["type"] == "MAIN_UMFUTURE"
    assert probe[2]["size"] == "1"


def test_actual_universal_transfer_endpoint_rejection_remains_blocked() -> None:
    values = responses()

    def reject_transfer(_method: str, _params: dict[str, str]) -> object:
        raise DomainRejected(
            "BINANCE_CAPITAL_AUTHORIZATION_REJECTED",
            "fixture endpoint permission rejection",
        )

    values["/sapi/v1/asset/transfer"] = reject_transfer
    with pytest.raises(DomainRejected) as caught:
        gateway(values).prepare_deposit(
            expected_address=DESTINATION,
            amount=Decimal("10"),
            source_address=SOURCE,
            now=NOW,
        )

    assert caught.value.code == "BINANCE_INTERNAL_TRANSFER_PERMISSION_DISABLED"


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


def test_wallet_get_fails_over_after_edge_auth_rejection(monkeypatch) -> None:
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
            raise urllib.error.HTTPError(
                request.full_url,
                400,
                "rejected",
                {},
                io.BytesIO(b'{"code":-2015,"msg":"edge rejected"}'),
            )
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    client = BinanceCapitalGateway(
        api_key="capital-key",
        api_secret="capital-secret",  # noqa: S106 - inert fixture credential
        clock_ms=lambda: 1_786_190_400_000,
    )
    client._clock_synchronized_at = time.monotonic()

    assert client._request("GET", "/sapi/v1/account/apiRestrictions") == {"ok": True}
    assert calls[0].startswith("https://api.binance.com/")
    assert calls[1].startswith("https://api1.binance.com/")
    assert client._active_base_url == "https://api1.binance.com"


def test_wallet_get_preserves_actionable_exchange_rejection_after_edge_outage(
    monkeypatch,
) -> None:
    calls = 0

    def urlopen(request, *, timeout):
        nonlocal calls
        assert timeout == 8
        calls += 1
        if calls == 1:
            raise urllib.error.HTTPError(
                request.full_url,
                400,
                "rejected",
                {},
                io.BytesIO(b'{"code":-2015,"msg":"edge rejected"}'),
            )
        raise urllib.error.URLError("next official edge unavailable")

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    client = BinanceCapitalGateway(
        api_key="capital-key",
        api_secret="capital-secret",  # noqa: S106 - inert fixture credential
        clock_ms=lambda: 1_786_190_400_000,
    )
    client._clock_synchronized_at = time.monotonic()

    with pytest.raises(DomainRejected) as caught:
        client._request("GET", "/sapi/v1/account/apiRestrictions")

    assert caught.value.code == "BINANCE_CAPITAL_AUTHORIZATION_REJECTED"
    assert calls == len(OFFICIAL_BINANCE_BASE_URLS)


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
    class TimeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self) -> bytes:
            return b'{"serverTime":1786190400000}'

    def urlopen(request, *, timeout):
        if request.full_url.endswith("/api/v3/time"):
            assert timeout == 4.0
            return TimeResponse()
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


@pytest.mark.parametrize(
    ("http_status", "exchange_code", "message", "expected_category"),
    [
        (429, -1015, "Too many requests", "ORDINARY_RATE_LIMIT"),
        (429, -1003, "Too much request weight used", "REQUEST_WEIGHT_EXCEEDED"),
        (418, -1003, "IP banned until 1786190520000", "IP_TEMPORARILY_BANNED"),
    ],
)
def test_wallet_api_rate_limits_record_exact_diagnostics_and_enforce_backoff(
    monkeypatch,
    http_status: int,
    exchange_code: int,
    message: str,
    expected_category: str,
) -> None:
    calls = 0

    def urlopen(request, *, timeout):
        nonlocal calls
        calls += 1
        raise urllib.error.HTTPError(
            request.full_url,
            http_status,
            "limited",
            {
                "Retry-After": "45",
                "X-MBX-USED-WEIGHT-1M": "1200",
                "X-MBX-ORDER-COUNT-10S": "8",
                "X-SAPI-USED-IP-WEIGHT-1M": "345",
            },
            io.BytesIO(f'{{"code":{exchange_code},"msg":"{message}"}}'.encode()),
        )

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    client = BinanceCapitalGateway(
        api_key="capital-key",
        api_secret="capital-secret",  # noqa: S106
        clock_ms=lambda: 1_786_190_400_000,
    )
    client._clock_synchronized_at = time.monotonic()

    with pytest.raises(DomainRejected) as caught:
        client._request("GET", "/sapi/v1/account/apiRestrictions")

    assert caught.value.code == "BINANCE_CAPITAL_RATE_LIMITED"
    assert caught.value.metadata is not None
    assert caught.value.metadata == {
        **caught.value.metadata,
        "category": expected_category,
        "http_status": http_status,
        "binance_error_code": exchange_code,
        "binance_error_message": message,
        "retry_after_seconds": 45,
        "rate_limit_headers": {
            "Retry-After": "45",
            "X-MBX-USED-WEIGHT-1M": "1200",
            "X-MBX-ORDER-COUNT-10S": "8",
            "X-SAPI-USED-IP-WEIGHT-1M": "345",
        },
    }
    with pytest.raises(DomainRejected) as deferred:
        client._request("GET", "/sapi/v1/account/apiRestrictions")
    assert deferred.value.metadata == caught.value.metadata
    assert calls == 1


def test_wallet_api_time_sync_rate_limit_stops_failover_and_enforces_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def urlopen(request, *, timeout):
        nonlocal calls
        calls += 1
        raise urllib.error.HTTPError(
            request.full_url,
            418,
            "banned",
            {"Retry-After": "120", "X-MBX-USED-WEIGHT-1M": "2400"},
            io.BytesIO(b'{"code":-1003,"msg":"IP banned until 1786190520000"}'),
        )

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    client = BinanceCapitalGateway(
        api_key="capital-key",
        api_secret="capital-secret",  # noqa: S106
        clock_ms=lambda: 1_786_190_400_000,
    )

    with pytest.raises(DomainRejected) as caught:
        client._request("GET", "/sapi/v1/account/apiRestrictions")
    assert caught.value.code == "BINANCE_CAPITAL_RATE_LIMITED"
    assert caught.value.metadata is not None
    assert caught.value.metadata["category"] == "IP_TEMPORARILY_BANNED"

    with pytest.raises(DomainRejected) as deferred:
        client._request("GET", "/sapi/v1/account/apiRestrictions")
    assert deferred.value.metadata == caught.value.metadata
    assert calls == 1


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


def test_short_lived_account_gateways_share_one_recent_server_time(monkeypatch) -> None:
    class Response:
        def __init__(self, body: bytes) -> None:
            self.body = body
            self.headers = {"X-MBX-USED-WEIGHT-1M": "12"}

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self) -> bytes:
            return self.body

    paths: list[str] = []

    def urlopen(request, *, timeout):
        del timeout
        path = urllib.parse.urlparse(request.full_url).path
        paths.append(path)
        if path == "/api/v3/time":
            return Response(b'{"serverTime":1786190400123}')
        return Response(b'{"ok":true}')

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    state = BinanceRequestState()
    base = BinanceCapitalGateway(
        clock_ms=lambda: 1_786_190_400_000,
        request_state=state,
    )
    first = base.with_credentials(api_key="account-a", api_secret="secret-a")  # noqa: S106
    second = base.with_credentials(api_key="account-b", api_secret="secret-b")  # noqa: S106

    assert first._request("GET", "/sapi/v1/account/apiRestrictions") == {"ok": True}
    assert second._request("GET", "/sapi/v1/account/apiRestrictions") == {"ok": True}
    assert paths.count("/api/v3/time") == 1
    assert paths.count("/sapi/v1/account/apiRestrictions") == 2


def test_receipt_read_is_deferred_before_transport_near_weight_limit() -> None:
    calls = 0

    def transport(_method: str, _path: str, _params: dict[str, str], _timeout: float) -> Any:
        nonlocal calls
        calls += 1
        return []

    state = BinanceRequestState()
    state.record_response_headers({"X-SAPI-USED-IP-WEIGHT-1M": "1920"})
    client = BinanceCapitalGateway(
        api_key="capital-key",
        api_secret="capital-secret",  # noqa: S106
        transport=transport,
        request_state=state,
    )

    with pytest.raises(DomainRejected) as caught:
        client.verify_withdrawal(
            order_id="operation-1",
            destination=DESTINATION,
            amount=Decimal("10"),
        )

    assert caught.value.code == "BINANCE_CAPITAL_WEIGHT_HEADROOM_DEFERRED"
    assert caught.value.metadata is not None and caught.value.metadata["next_retry_at"]
    assert calls == 0
