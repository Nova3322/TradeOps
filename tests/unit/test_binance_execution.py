from __future__ import annotations

import hashlib
import hmac
import urllib.parse
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from trading_control_plane import binance_execution
from trading_control_plane.binance_execution import (
    BinanceTestnetClient,
    BinanceTestnetOrderCommand,
    BinanceTestnetProtectionCommand,
)
from trading_control_plane.domain import DomainRejected

NOW = datetime(2026, 7, 19, 12, tzinfo=UTC)


class ContractVenue:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, str], float]] = []
        self.orders: dict[str, dict[str, Any]] = {}
        self.next_order_id = 100

    def __call__(
        self, method: str, url: str, headers: dict[str, str], timeout: float
    ) -> dict[str, Any]:
        self.calls.append((method, url, headers, timeout))
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        client_id = query.get("origClientOrderId") or query.get("newClientOrderId")
        assert client_id is not None
        if method == "GET":
            return self.orders.get(client_id, {"code": -2013, "msg": "Order does not exist"})
        if method == "DELETE":
            order = self.orders[client_id]
            order["status"] = "CANCELED"
            return order
        self.next_order_id += 1
        protection = query["type"] == "STOP_MARKET"
        order = {
            "symbol": query["symbol"],
            "orderId": self.next_order_id,
            "clientOrderId": client_id,
            "status": "NEW",
            "side": query["side"],
            "type": query["type"],
            "origQty": "0" if protection else query["quantity"],
            "executedQty": "0",
            "stopPrice": query.get("stopPrice", "0"),
            "reduceOnly": query.get("reduceOnly") == "true",
            "closePosition": query.get("closePosition") == "true",
            "updateTime": int(NOW.timestamp() * 1_000),
        }
        self.orders[client_id] = order
        return order


def client(venue: ContractVenue) -> BinanceTestnetClient:
    return BinanceTestnetClient(
        base_url="https://testnet.binancefuture.com",
        api_key="testnet-key",
        api_secret="testnet-secret",  # noqa: S106 - deterministic contract fixture
        requester=venue,
    )


def assert_signed(call: tuple[str, str, dict[str, str], float]) -> None:
    _method, url, headers, timeout = call
    assert headers == {"X-MBX-APIKEY": "testnet-key"}
    assert timeout == 5.0
    query_items = urllib.parse.parse_qsl(urllib.parse.urlparse(url).query)
    signature = dict(query_items)["signature"]
    unsigned = urllib.parse.urlencode(
        [(key, value) for key, value in query_items if key != "signature"]
    )
    assert signature == hmac.new(b"testnet-secret", unsigned.encode(), hashlib.sha256).hexdigest()


def test_query_first_send_uses_stable_identity_and_restart_does_not_post_twice() -> None:
    venue = ContractVenue()
    command = BinanceTestnetOrderCommand(
        symbol="BTCUSDT",
        side="BUY",
        quantity=Decimal("0.25"),
        reduce_only=False,
        client_order_id="tcp-0123456789abcdef0123456789abcdef",
    )

    first = client(venue).ensure_order(command, now=NOW)
    restarted = client(venue).ensure_order(command, now=NOW)

    assert first == restarted
    assert [call[0] for call in venue.calls] == ["GET", "POST", "GET"]
    assert first.client_order_id == command.client_order_id
    assert first.ordered_quantity == Decimal("0.25")
    assert all(
        urllib.parse.urlparse(call[1]).hostname == "testnet.binancefuture.com"
        for call in venue.calls
    )
    for call in venue.calls:
        assert_signed(call)


def test_cancel_and_native_close_position_protection_follow_official_contract() -> None:
    venue = ContractVenue()
    testnet = client(venue)
    order_command = BinanceTestnetOrderCommand(
        symbol="BTCUSDT",
        side="SELL",
        quantity=Decimal("0.5"),
        reduce_only=True,
        client_order_id="tcp-fedcba9876543210fedcba9876543210",
    )
    testnet.ensure_order(order_command, now=NOW)
    cancelled = testnet.cancel_order(order_command, now=NOW)
    protection_command = BinanceTestnetProtectionCommand(
        symbol="BTCUSDT",
        side="SELL",
        trigger_price=Decimal("59000"),
        client_order_id="tpp-0123456789abcdef0123456789abcdef",
    )
    protection = testnet.ensure_protection(protection_command, now=NOW)

    assert cancelled is not None and cancelled.status == "CANCELLED"
    assert protection.close_position is True
    assert protection.order_type == "STOP_MARKET"
    protection_post = next(
        call for call in venue.calls if call[0] == "POST" and "STOP_MARKET" in call[1]
    )
    params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(protection_post[1]).query))
    assert params["closePosition"] == "true"
    assert params["workingType"] == "MARK_PRICE"
    assert "quantity" not in params
    assert "reduceOnly" not in params


def test_live_or_arbitrary_execution_hosts_are_rejected_before_any_request() -> None:
    for base_url in (
        "https://fapi.binance.com",
        "https://testnet.binancefuture.example",
        "http://testnet.binancefuture.com",
    ):
        with pytest.raises(ValueError, match="official USDⓈ-M testnet"):
            BinanceTestnetClient(
                base_url=base_url,
                api_key="key",
                api_secret="fixture-secret",  # noqa: S106 - rejected before use
            )


def test_same_client_identity_with_different_semantics_fails_closed() -> None:
    venue = ContractVenue()
    testnet = client(venue)
    client_order_id = "tcp-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    testnet.ensure_order(
        BinanceTestnetOrderCommand("BTCUSDT", "BUY", Decimal("1"), False, client_order_id),
        now=NOW,
    )

    with pytest.raises(DomainRejected, match="BINANCE_TESTNET_IDENTITY_CONFLICT"):
        testnet.ensure_order(
            BinanceTestnetOrderCommand("BTCUSDT", "BUY", Decimal("2"), False, client_order_id),
            now=NOW,
        )


def test_network_timeout_distinguishes_read_unavailable_from_write_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timeout(*_args: object, **_kwargs: object) -> None:
        raise TimeoutError

    monkeypatch.setattr(binance_execution.urllib.request, "urlopen", timeout)

    with pytest.raises(DomainRejected, match="BINANCE_TESTNET_UNAVAILABLE"):
        binance_execution._default_requester("GET", "https://example.invalid", {}, 1.0)
    with pytest.raises(DomainRejected, match="BINANCE_TESTNET_OUTCOME_UNKNOWN"):
        binance_execution._default_requester("POST", "https://example.invalid", {}, 1.0)


def test_duplicate_create_rejection_requeries_identity_before_treating_as_zero_fill() -> None:
    venue = ContractVenue()
    original = venue.__call__
    rejected_once = False

    def concurrent(
        method: str, url: str, headers: dict[str, str], timeout: float
    ) -> dict[str, Any]:
        nonlocal rejected_once
        result = original(method, url, headers, timeout)
        if method == "POST" and not rejected_once:
            rejected_once = True
            return {"code": -2010, "msg": "duplicate client identity"}
        return result

    testnet = BinanceTestnetClient(
        base_url="https://testnet.binancefuture.com",
        api_key="testnet-key",
        api_secret="testnet-secret",  # noqa: S106
        requester=concurrent,
    )
    command = BinanceTestnetOrderCommand(
        "BTCUSDT",
        "BUY",
        Decimal("0.5"),
        False,
        "tcp-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    )

    recovered = testnet.ensure_order(command, now=NOW)

    assert recovered.status == "SENT"
    assert [call[0] for call in venue.calls] == ["GET", "POST", "GET"]


def test_default_requester_requires_json_object_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        def __init__(self, body: bytes) -> None:
            self.body = body

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return self.body

    monkeypatch.setattr(
        binance_execution.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: Response(b'{"ok": true}'),
    )
    assert binance_execution._default_requester("GET", "https://example.invalid", {}, 1.0) == {
        "ok": True
    }

    monkeypatch.setattr(
        binance_execution.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: Response(b"[]"),
    )
    with pytest.raises(DomainRejected, match="BINANCE_TESTNET_RESPONSE_INVALID"):
        binance_execution._default_requester("GET", "https://example.invalid", {}, 1.0)

    monkeypatch.setattr(
        binance_execution.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: Response(b"not-json"),
    )
    with pytest.raises(DomainRejected, match="BINANCE_TESTNET_RESPONSE_INVALID"):
        binance_execution._default_requester("GET", "https://example.invalid", {}, 1.0)


def test_unconfigured_and_malformed_testnet_responses_fail_closed() -> None:
    unconfigured = BinanceTestnetClient(
        base_url="https://testnet.binancefuture.com",
        api_key=None,
        api_secret=None,
    )
    with pytest.raises(DomainRejected, match="BINANCE_TESTNET_NOT_CONFIGURED"):
        unconfigured.query_order("BTCUSDT", "tcp-none", now=NOW)

    for payload in (
        {"code": "not-an-integer"},
        {"orderId": 1},
        {
            "orderId": 1,
            "clientOrderId": "tcp-invalid",
            "side": "INVALID",
            "type": "MARKET",
        },
        {
            "orderId": 1,
            "clientOrderId": "tcp-invalid",
            "side": "BUY",
            "type": "MARKET",
            "origQty": "not-a-number",
        },
        {
            "orderId": 1,
            "clientOrderId": "tcp-invalid",
            "side": "BUY",
            "type": "MARKET",
            "origQty": "-1",
        },
        {
            "orderId": 1,
            "clientOrderId": "tcp-invalid",
            "side": "BUY",
            "type": "MARKET",
            "origQty": "1",
            "updateTime": "not-a-time",
        },
    ):
        with pytest.raises(DomainRejected, match="BINANCE_TESTNET_RESPONSE_INVALID"):
            BinanceTestnetClient._parse_order(payload, now=NOW)


def test_cancel_recover_and_existing_protection_are_query_first() -> None:
    venue = ContractVenue()
    testnet = client(venue)
    command = BinanceTestnetOrderCommand(
        "BTCUSDT",
        "BUY",
        Decimal("0.5"),
        False,
        "tcp-cccccccccccccccccccccccccccccccc",
    )
    assert testnet.cancel_order(command, now=NOW) is None
    assert testnet.recover_order(command, now=NOW) is None
    created = testnet.ensure_order(command, now=NOW)
    venue.orders[command.client_order_id]["status"] = "FILLED"
    assert testnet.cancel_order(command, now=NOW).status == "FILLED"  # type: ignore[union-attr]
    assert testnet.recover_order(command, now=NOW).order_id == created.order_id  # type: ignore[union-attr]

    protection = BinanceTestnetProtectionCommand(
        "BTCUSDT",
        "SELL",
        Decimal("90"),
        "tpp-dddddddddddddddddddddddddddddddd",
    )
    first = testnet.ensure_protection(protection, now=NOW)
    duplicate = testnet.ensure_protection(protection, now=NOW)
    assert duplicate == first
    with pytest.raises(DomainRejected, match="BINANCE_TESTNET_IDENTITY_CONFLICT"):
        testnet.ensure_protection(
            BinanceTestnetProtectionCommand(
                "BTCUSDT", "SELL", Decimal("91"), protection.client_order_id
            ),
            now=NOW,
        )
