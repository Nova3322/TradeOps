from __future__ import annotations

import hashlib
import hmac
import io
import urllib.error
import urllib.parse
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from trading_control_plane import binance_execution
from trading_control_plane.binance_execution import (
    BinancePortfolioMarginClient,
    BinanceTestnetClient,
    BinanceTestnetOrderCommand,
    BinanceTestnetProtectionCommand,
    ProtectionCancelCommand,
)
from trading_control_plane.domain import DomainRejected

NOW = datetime(2026, 7, 19, 12, tzinfo=UTC)


class UrlResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> UrlResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


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


def test_default_server_time_and_http_error_transports_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        binance_execution.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: UrlResponse(b'{"serverTime": 1234}'),
    )
    assert binance_execution._default_server_time_fetcher(1.0) == 1234

    monkeypatch.setattr(
        binance_execution.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: UrlResponse(b"not-json"),
    )
    with pytest.raises(DomainRejected, match="BINANCE_LIVE_UNAVAILABLE"):
        binance_execution._default_server_time_fetcher(1.0)
    monkeypatch.setattr(
        binance_execution.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: UrlResponse(b'{"wrong": true}'),
    )
    with pytest.raises(DomainRejected, match="BINANCE_LIVE_RESPONSE_INVALID"):
        binance_execution._default_server_time_fetcher(1.0)

    def http_error(body: bytes) -> None:
        raise urllib.error.HTTPError(
            "https://example.invalid",
            400,
            "rejected",
            {},
            io.BytesIO(body),
        )

    monkeypatch.setattr(
        binance_execution.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: http_error(b'{"code": -1}'),
    )
    assert binance_execution._default_requester("POST", "https://example.invalid", {}, 1.0) == {
        "code": -1
    }
    for body in (b"[]", b"not-json"):
        monkeypatch.setattr(
            binance_execution.urllib.request,
            "urlopen",
            lambda *_args, body=body, **_kwargs: http_error(body),
        )
        with pytest.raises(DomainRejected, match="BINANCE_TESTNET_RESPONSE_INVALID"):
            binance_execution._default_requester("POST", "https://example.invalid", {}, 1.0)


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


class PortfolioMarginVenue:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, str], float]] = []
        self.orders: dict[str, dict[str, Any]] = {}
        self.algos: dict[str, dict[str, Any]] = {}
        self.next_id = 900

    def __call__(
        self, method: str, url: str, headers: dict[str, str], timeout: float
    ) -> dict[str, Any]:
        self.calls.append((method, url, headers, timeout))
        parsed = urllib.parse.urlparse(url)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        if parsed.path == "/papi/v1/um/order":
            client_id = query.get("origClientOrderId") or query.get("newClientOrderId")
            assert client_id is not None
            if method == "GET":
                return self.orders.get(client_id, {"code": -2013, "msg": "not found"})
            self.next_id += 1
            order = {
                "symbol": query["symbol"],
                "orderId": self.next_id,
                "clientOrderId": client_id,
                "status": "FILLED",
                "side": query["side"],
                "type": "MARKET",
                "origQty": query["quantity"],
                "executedQty": query["quantity"],
                "stopPrice": "0",
                "reduceOnly": query.get("reduceOnly") == "true",
                "closePosition": False,
                "updateTime": int(NOW.timestamp() * 1_000),
            }
            self.orders[client_id] = order
            return order
        if parsed.path == "/papi/v1/um/algo/algoOrder":
            client_id = query["clientAlgoId"]
            return self.algos.get(client_id, {"code": -2013, "msg": "not found"})
        assert parsed.path == "/papi/v1/um/algo/order"
        client_id = query["clientAlgoId"]
        if method == "DELETE":
            self.algos[client_id]["algoStatus"] = "CANCELED"
            return {"complete": True}
        self.next_id += 1
        algo = {
            "algoId": self.next_id,
            "clientAlgoId": client_id,
            "algoStatus": "NEW",
            "side": query["side"],
            "orderType": query["type"],
            "quantity": query["quantity"],
            "triggerPrice": query["triggerPrice"],
            "reduceOnly": query["reduceOnly"] == "true",
            "updateTime": int(NOW.timestamp() * 1_000),
        }
        self.algos[client_id] = algo
        return algo


def test_portfolio_margin_live_order_protection_and_cancel_use_papi_contract() -> None:
    venue = PortfolioMarginVenue()
    observed_ms = int(NOW.timestamp() * 1_000)
    client = BinancePortfolioMarginClient(
        base_url="https://papi.binance.com",
        api_key="unified-key",
        api_secret="unified-secret",  # noqa: S106
        requester=venue,
        server_time_fetcher=lambda _timeout: observed_ms,
    )
    order = BinanceTestnetOrderCommand(
        "XRPUSDT",
        "BUY",
        Decimal(5),
        False,
        "tcp-ASNFZ4mrze8BI0VniavN7w",
    )
    protection = BinanceTestnetProtectionCommand(
        "XRPUSDT",
        "SELL",
        Decimal("0.95"),
        "tpp-ASNFZ4mrze8BI0VniavN7w",
        Decimal(5),
    )

    first = client.ensure_order(order, now=NOW.replace(year=2020))
    duplicate = client.ensure_order(order, now=NOW.replace(year=2020))
    protected = client.ensure_protection(protection, now=NOW.replace(year=2020))
    protected_duplicate = client.ensure_protection(protection, now=NOW.replace(year=2020))
    cancelled = client.cancel_protection(
        ProtectionCancelCommand(protection.symbol, protection.client_order_id),
        now=NOW.replace(year=2020),
    )

    assert first == duplicate
    assert first.status == "FILLED"
    assert protected == protected_duplicate
    assert protected.status == "SENT"
    assert cancelled is not None and cancelled.status == "CANCELLED"
    assert [urllib.parse.urlparse(call[1]).path for call in venue.calls] == [
        "/papi/v1/um/order",
        "/papi/v1/um/order",
        "/papi/v1/um/order",
        "/papi/v1/um/algo/algoOrder",
        "/papi/v1/um/algo/order",
        "/papi/v1/um/algo/algoOrder",
        "/papi/v1/um/algo/algoOrder",
        "/papi/v1/um/algo/order",
    ]
    assert sum(call[0] == "POST" for call in venue.calls) == 2
    for _method, url, headers, timeout in venue.calls:
        assert urllib.parse.urlparse(url).hostname == "papi.binance.com"
        assert headers == {"X-MBX-APIKEY": "unified-key"}
        assert timeout == 5.0
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        assert query["timestamp"] == str(observed_ms)
        assert query["recvWindow"] == "10000"
        assert "signature" in query


def test_portfolio_margin_execution_rejects_non_papi_hosts() -> None:
    for base_url in ("https://fapi.binance.com", "http://papi.binance.com"):
        with pytest.raises(ValueError, match="official LIVE PAPI"):
            BinancePortfolioMarginClient(
                base_url=base_url,
                api_key=None,
                api_secret=None,
            )


def test_portfolio_margin_execution_translates_core_errors_to_live_codes() -> None:
    client = BinancePortfolioMarginClient(
        base_url="https://papi.binance.com",
        api_key="unified-key",
        api_secret="unified-secret",  # noqa: S106
        requester=lambda *_args: {"code": -1000, "msg": "controlled rejection"},
        server_time_fetcher=lambda _timeout: int(NOW.timestamp() * 1_000),
    )

    with pytest.raises(DomainRejected, match="BINANCE_LIVE_REJECTED"):
        client.ensure_order(
            BinanceTestnetOrderCommand(
                "XRPUSDT",
                "BUY",
                Decimal(5),
                False,
                "tcp-ASNFZ4mrze8BI0VniavN7w",
            ),
            now=NOW,
        )
