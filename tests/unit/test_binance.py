from __future__ import annotations

import hashlib
import hmac
import io
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from trading_control_plane import binance
from trading_control_plane.binance import (
    BinancePortfolioMarginReadOnlyClient,
    BinanceReadOnlyClient,
)
from trading_control_plane.binance_errors import (
    BinanceRequestState,
    classify_binance_rate_limit,
)
from trading_control_plane.domain import DomainRejected

NOW = datetime(2026, 7, 19, 10, tzinfo=UTC)


class UrlResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> UrlResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def payloads() -> dict[str, dict[str, Any] | list[dict[str, Any]]]:
    observed_ms = int(NOW.timestamp() * 1_000)
    return {
        "/fapi/v1/exchangeInfo": {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "contractType": "PERPETUAL",
                    "status": "TRADING",
                    "quoteAsset": "USDT",
                    "marginAsset": "USDT",
                    "filters": [
                        {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                        {"filterType": "LOT_SIZE", "stepSize": "0.001"},
                        {"filterType": "MIN_NOTIONAL", "notional": "5"},
                    ],
                }
            ]
        },
        "/fapi/v3/positionRisk": [
            {
                "symbol": "BTCUSDT",
                "positionSide": "BOTH",
                "positionAmt": "0.25",
                "entryPrice": "60000",
                "markPrice": "61000",
                "updateTime": observed_ms,
            }
        ],
        "/fapi/v3/balance": [
            {
                "asset": "USDT",
                "balance": "1000",
                "crossUnPnl": "25",
                "availableBalance": "800",
                "updateTime": observed_ms,
            }
        ],
        "/fapi/v1/openOrders": [
            {
                "symbol": "BTCUSDT",
                "orderId": 77,
                "clientOrderId": "external-stop",
                "status": "NEW",
                "side": "SELL",
                "type": "STOP_MARKET",
                "origQty": "0",
                "executedQty": "0",
                "stopPrice": "59000",
                "reduceOnly": False,
                "closePosition": True,
                "updateTime": observed_ms,
            }
        ],
        "/fapi/v1/userTrades": [
            {
                "symbol": "BTCUSDT",
                "id": 88,
                "orderId": 70,
                "side": "BUY",
                "qty": "0.25",
                "price": "60000",
                "commission": "1.25",
                "commissionAsset": "USDT",
                "time": observed_ms,
            }
        ],
        "/fapi/v1/income": [
            {
                "symbol": "BTCUSDT",
                "incomeType": "FUNDING_FEE",
                "tranId": 99,
                "income": "-0.75",
                "asset": "USDT",
                "time": observed_ms,
            }
        ],
    }


def client_with_contract(
    responses: dict[str, dict[str, Any] | list[dict[str, Any]]],
) -> tuple[BinanceReadOnlyClient, list[tuple[str, dict[str, str], float]]]:
    calls: list[tuple[str, dict[str, str], float]] = []

    def fetch(
        url: str, headers: dict[str, str], timeout: float
    ) -> dict[str, Any] | list[dict[str, Any]]:
        calls.append((url, headers, timeout))
        path = urllib.parse.urlparse(url).path
        return responses[path]

    return (
        BinanceReadOnlyClient(
            base_url="https://fapi.binance.example",
            api_key="read-only-key",
            api_secret="read-only-secret",  # noqa: S106 - deterministic contract fixture
            fetcher=fetch,
        ),
        calls,
    )


def test_user_data_contract_is_get_only_signed_and_maps_all_required_facts() -> None:
    client, calls = client_with_contract(payloads())

    snapshot = client.read_snapshot("BTCUSDT", now=NOW)

    assert [urllib.parse.urlparse(call[0]).path for call in calls] == [
        "/fapi/v1/exchangeInfo",
        "/fapi/v3/positionRisk",
        "/fapi/v3/balance",
        "/fapi/v1/openOrders",
        "/fapi/v1/userTrades",
        "/fapi/v1/income",
    ]
    assert calls[0][1] == {}
    for url, headers, timeout in calls[1:]:
        assert headers == {"X-MBX-APIKEY": "read-only-key"}
        assert timeout == 5.0
        query_items = urllib.parse.parse_qsl(urllib.parse.urlparse(url).query)
        signature = dict(query_items)["signature"]
        unsigned = urllib.parse.urlencode(
            [(key, value) for key, value in query_items if key != "signature"]
        )
        expected = hmac.new(b"read-only-secret", unsigned.encode(), hashlib.sha256).hexdigest()
        assert signature == expected
    assert snapshot.instrument.tick_size == Decimal("0.10")
    assert snapshot.position.quantity == Decimal("0.25")
    assert snapshot.equity.equity == Decimal("1025")
    assert snapshot.orders[0].order_id == "77"
    assert snapshot.fills[0].fee == Decimal("1.25")
    assert snapshot.funding[0].amount == Decimal("-0.75")
    assert snapshot.protection is not None
    assert snapshot.protection.quantity == Decimal("0.25")
    assert snapshot.protection.trigger_price == Decimal("59000")
    assert not hasattr(client, "send_order")
    assert not hasattr(client, "post")


def test_runtime_snapshot_uses_cached_catalog_and_incremental_history_budget() -> None:
    client, calls = client_with_contract(payloads())
    cursor = NOW - timedelta(minutes=2)
    client.set_history_cursors({"BTCUSDT": {"fills": cursor, "funding": cursor}})

    assert [item.symbol for item in client.read_active_instruments()] == ["BTCUSDT"]
    first = client.read_account_snapshots(("BTCUSDT",), now=NOW)
    first_count = len(calls)
    second = client.read_account_snapshots(("BTCUSDT",), now=NOW + timedelta(minutes=1))

    assert first_count == 6
    assert len(calls) - first_count == 3
    assert first[0].orders and first[0].fills and first[0].funding
    assert second[0].orders and second[0].fills == () and second[0].funding == ()
    paths = [urllib.parse.urlparse(url).path for url, _headers, _timeout in calls]
    assert paths.count("/fapi/v1/exchangeInfo") == 1
    assert paths.count("/fapi/v1/userTrades") == 1
    assert paths.count("/fapi/v1/income") == 1
    history_queries = [
        dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        for url, _headers, _timeout in calls
        if urllib.parse.urlparse(url).path in {"/fapi/v1/userTrades", "/fapi/v1/income"}
    ]
    expected_start = str(int((cursor - timedelta(minutes=5)).timestamp() * 1000))
    assert all(query["startTime"] == expected_start for query in history_queries)


def test_shared_binance_cooldown_blocks_every_account_until_expiry() -> None:
    calls = 0

    def fetch(_url: str, _headers: dict[str, str], _timeout: float) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"ok": True}

    now = datetime.now(UTC)
    state = BinanceRequestState()
    state.record_rate_limit(
        classify_binance_rate_limit(
            http_status=429,
            binance_error_code=-1003,
            binance_error_message="Too much request weight used",
            headers={"retry-after": "60", "x-mbx-used-weight-1m": "2400"},
            failed_at=now,
        ),
        host="fapi.binance.com",
    )
    for api_key in ("account-a", "account-b"):
        client = BinanceReadOnlyClient(
            base_url="https://fapi.binance.example",
            api_key=api_key,
            api_secret="secret",  # noqa: S106
            fetcher=fetch,
            request_state=state,
        )
        with pytest.raises(DomainRejected, match="BINANCE_RATE_LIMITED"):
            client._signed_get("/fapi/v3/balance", {}, timestamp_ms=1)
    assert calls == 0

    expired = BinanceRequestState()
    expired.record_rate_limit(
        classify_binance_rate_limit(
            http_status=429,
            binance_error_code=-1003,
            binance_error_message="Too much request weight used",
            headers={"Retry-After": "1"},
            failed_at=now - timedelta(seconds=2),
        )
    )
    client = BinanceReadOnlyClient(
        base_url="https://fapi.binance.example",
        api_key="account-c",
        api_secret="secret",  # noqa: S106
        fetcher=fetch,
        request_state=expired,
    )
    assert client._signed_get("/fapi/v3/balance", {}, timestamp_ms=1) == {"ok": True}
    assert calls == 1


def test_active_catalog_uses_complete_official_exchange_info_and_omits_inactive() -> None:
    responses = payloads()
    exchange = responses["/fapi/v1/exchangeInfo"]
    assert isinstance(exchange, dict)
    exchange["symbols"].extend(
        [
            {
                "symbol": "TUTUSDT",
                "contractType": "PERPETUAL",
                "status": "TRADING",
                "quoteAsset": "USDT",
                "marginAsset": "USDT",
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.00001"},
                    {"filterType": "LOT_SIZE", "stepSize": "1"},
                    {"filterType": "MIN_NOTIONAL", "notional": "5"},
                ],
            },
            {
                "symbol": "OLDUSDT",
                "contractType": "PERPETUAL",
                "status": "SETTLING",
                "quoteAsset": "USDT",
                "marginAsset": "USDT",
                "filters": [],
            },
            {
                "symbol": "BTCUSDT_260925",
                "contractType": "CURRENT_QUARTER",
                "status": "TRADING",
                "quoteAsset": "USDT",
                "marginAsset": "USDT",
                "filters": [],
            },
        ]
    )
    client, calls = client_with_contract(responses)

    instruments = client.read_active_instruments()

    assert [instrument.symbol for instrument in instruments] == ["BTCUSDT", "TUTUSDT"]
    assert all(instrument.active for instrument in instruments)
    assert urllib.parse.parse_qs(urllib.parse.urlparse(calls[0][0]).query) == {}
    assert calls[0][1] == {}


def test_standard_account_snapshot_reads_position_risk_once_for_all_active_symbols() -> None:
    responses = payloads()
    position_rows = responses["/fapi/v3/positionRisk"]
    assert isinstance(position_rows, list)
    position_rows.append(
        {
            "symbol": "ETHUSDT",
            "positionSide": "BOTH",
            "positionAmt": "2",
            "entryPrice": "3000",
            "markPrice": "3100",
            "updateTime": int(NOW.timestamp() * 1_000),
        }
    )
    exchange = responses["/fapi/v1/exchangeInfo"]
    assert isinstance(exchange, dict)
    template = exchange["symbols"][0]
    calls: list[str] = []

    def fetch(
        url: str, _headers: dict[str, str], _timeout: float
    ) -> dict[str, Any] | list[dict[str, Any]]:
        parsed = urllib.parse.urlparse(url)
        calls.append(parsed.path)
        if parsed.path == "/fapi/v1/exchangeInfo":
            symbol = dict(urllib.parse.parse_qsl(parsed.query))["symbol"]
            return {"symbols": [{**template, "symbol": symbol}]}
        return responses[parsed.path]

    client = BinanceReadOnlyClient(
        base_url="https://fapi.binance.example",
        api_key="read-only-key",
        api_secret="read-only-secret",  # noqa: S106
        fetcher=fetch,
    )

    snapshots = client.read_account_snapshots(("BTCUSDT",), now=NOW)

    assert [(item.symbol, item.position.quantity) for item in snapshots] == [
        ("BTCUSDT", Decimal("0.25")),
        ("ETHUSDT", Decimal(2)),
    ]
    assert calls.count("/fapi/v3/positionRisk") == 1
    assert calls.count("/fapi/v3/balance") == 1
    assert calls.count("/fapi/v1/openOrders") == 1


def test_account_snapshot_marks_page_limited_history_incomplete_without_partial_facts() -> None:
    responses = payloads()
    trade = responses["/fapi/v1/userTrades"]
    assert isinstance(trade, list)
    responses["/fapi/v1/userTrades"] = trade * 1_000
    client, _calls = client_with_contract(responses)

    snapshots = client.read_account_snapshots(("BTCUSDT",), now=NOW)

    assert len(snapshots) == 1
    assert snapshots[0].position.quantity == Decimal("0.25")
    assert snapshots[0].history_error_code == "BINANCE_RESPONSE_INCOMPLETE"
    assert snapshots[0].fills == ()
    assert snapshots[0].funding == ()


def test_missing_credentials_fail_before_any_private_fact_is_read() -> None:
    calls: list[str] = []
    client = BinanceReadOnlyClient(
        base_url="https://fapi.binance.example",
        api_key=None,
        api_secret=None,
        fetcher=lambda url, _headers, _timeout: (
            calls.append(url) or payloads()["/fapi/v1/exchangeInfo"]
        ),
    )

    with pytest.raises(DomainRejected, match="BINANCE_READ_ONLY_NOT_CONFIGURED"):
        client.read_snapshot("BTCUSDT", now=NOW)

    assert len(calls) == 1
    assert "/fapi/v1/exchangeInfo" in calls[0]


def test_hedge_mode_nonzero_position_is_not_collapsed_into_false_net_fact() -> None:
    responses = payloads()
    responses["/fapi/v3/positionRisk"] = [
        {
            "symbol": "BTCUSDT",
            "positionSide": "LONG",
            "positionAmt": "0.25",
            "entryPrice": "60000",
            "markPrice": "61000",
            "updateTime": int(NOW.timestamp() * 1_000),
        }
    ]
    client, _ = client_with_contract(responses)

    with pytest.raises(DomainRejected, match="BINANCE_HEDGE_MODE_UNSUPPORTED"):
        client.read_snapshot("BTCUSDT", now=NOW)


def test_invalid_contract_fields_fail_closed() -> None:
    responses = payloads()
    exchange = responses["/fapi/v1/exchangeInfo"]
    assert isinstance(exchange, dict)
    symbols = exchange["symbols"]
    assert isinstance(symbols, list)
    filters = symbols[0]["filters"]
    assert isinstance(filters, list)
    filters[0]["tickSize"] = "not-a-number"
    client, _ = client_with_contract(responses)

    with pytest.raises(DomainRejected, match="BINANCE_RESPONSE_INVALID"):
        client.read_snapshot("BTCUSDT", now=NOW)


def test_zero_precision_rule_fails_closed() -> None:
    responses = payloads()
    exchange = responses["/fapi/v1/exchangeInfo"]
    assert isinstance(exchange, dict)
    symbols = exchange["symbols"]
    assert isinstance(symbols, list)
    filters = symbols[0]["filters"]
    assert isinstance(filters, list)
    filters[1]["stepSize"] = "0"
    client, _ = client_with_contract(responses)

    with pytest.raises(DomainRejected, match="BINANCE_RESPONSE_INVALID"):
        client.read_snapshot("BTCUSDT", now=NOW)


def test_network_error_is_reported_as_read_only_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(*_args: object, **_kwargs: object) -> None:
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(urllib.request, "urlopen", unavailable)
    client = BinanceReadOnlyClient(
        base_url="https://fapi.binance.example",
        api_key="key",
        api_secret="secret",  # noqa: S106 - deterministic network-error fixture
    )

    with pytest.raises(DomainRejected, match="BINANCE_READ_ONLY_UNAVAILABLE"):
        client.read_snapshot("BTCUSDT", now=NOW)


def test_read_only_transport_retries_transient_network_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    delays: list[float] = []

    def transient(*_args: object, **_kwargs: object) -> UrlResponse:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise urllib.error.URLError("temporary")
        return UrlResponse(b'{"ok":true}')

    monkeypatch.setattr(urllib.request, "urlopen", transient)
    monkeypatch.setattr(binance.time, "sleep", delays.append)

    assert binance._default_fetcher("https://example.invalid", {}, 1.0) == {"ok": True}
    assert attempts == 3
    assert delays == [0.25, 0.5]


def test_binance_transport_classifies_signed_api_error_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = urllib.error.HTTPError(
        "https://fapi.binance.com/fapi/v3/balance",
        400,
        "bad request",
        {},
        io.BytesIO(b'{"code":-2015,"msg":"invalid key"}'),
    )

    def reject(*_args: object, **_kwargs: object) -> UrlResponse:
        raise error

    monkeypatch.setattr(urllib.request, "urlopen", reject)

    with pytest.raises(DomainRejected, match="BINANCE_AUTHENTICATION_FAILED"):
        binance._default_fetcher("https://fapi.binance.com/fapi/v3/balance", {}, 1.0)


def test_binance_transport_distinguishes_timestamp_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = urllib.error.HTTPError(
        "https://fapi.binance.com/fapi/v3/balance",
        400,
        "bad request",
        {},
        io.BytesIO(b'{"code":-1021,"msg":"timestamp outside recvWindow"}'),
    )

    def reject(*_args: object, **_kwargs: object) -> UrlResponse:
        raise error

    monkeypatch.setattr(urllib.request, "urlopen", reject)

    with pytest.raises(DomainRejected, match="BINANCE_TIMESTAMP_REJECTED"):
        binance._default_fetcher("https://fapi.binance.com/fapi/v3/balance", {}, 1.0)


def test_binance_read_only_transport_preserves_rate_limit_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = urllib.error.HTTPError(
        "https://fapi.binance.com/fapi/v3/balance",
        418,
        "banned",
        {"Retry-After": "90", "X-MBX-USED-WEIGHT-1M": "2400"},
        io.BytesIO(b'{"code":-1003,"msg":"IP banned until 1786190490000"}'),
    )

    def reject(*_args: object, **_kwargs: object) -> UrlResponse:
        raise error

    monkeypatch.setattr(urllib.request, "urlopen", reject)

    with pytest.raises(DomainRejected) as caught:
        binance._default_fetcher("https://fapi.binance.com/fapi/v3/balance", {}, 1.0)

    assert caught.value.code == "BINANCE_RATE_LIMITED"
    assert caught.value.metadata is not None
    assert caught.value.metadata["category"] == "IP_TEMPORARILY_BANNED"
    assert caught.value.metadata["http_status"] == 418
    assert caught.value.metadata["binance_error_code"] == -1003
    assert caught.value.metadata["retry_after_seconds"] == 90


def test_binance_retry_after_preserves_multi_day_ip_ban() -> None:
    diagnostic = classify_binance_rate_limit(
        http_status=418,
        binance_error_code=-1003,
        binance_error_message="IP banned",
        headers={"Retry-After": "259200"},
        failed_at=NOW,
    )

    assert diagnostic.retry_after_seconds == 259_200
    assert diagnostic.next_retry_at == NOW + timedelta(days=3)


def test_binance_server_time_rate_limit_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def reject(*_args: object, **_kwargs: object) -> UrlResponse:
        nonlocal calls
        calls += 1
        raise urllib.error.HTTPError(
            "https://fapi.binance.com/fapi/v1/time",
            429,
            "limited",
            {"Retry-After": "30", "X-MBX-USED-WEIGHT-1M": "1200"},
            io.BytesIO(b'{"code":-1003,"msg":"Too much request weight used"}'),
        )

    monkeypatch.setattr(urllib.request, "urlopen", reject)

    with pytest.raises(DomainRejected) as caught:
        binance._server_time_from("https://fapi.binance.com", 1.0)

    assert caught.value.code == "BINANCE_RATE_LIMITED"
    assert caught.value.metadata is not None
    assert caught.value.metadata["category"] == "REQUEST_WEIGHT_EXCEEDED"
    assert calls == 1


def test_default_binance_read_transports_parse_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        binance.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: UrlResponse(b'{"serverTime": 1234}'),
    )
    assert binance._default_server_time_fetcher(1.0) == 1234
    assert binance._default_fetcher("https://example.invalid", {}, 1.0) == {"serverTime": 1234}

    for body in (b"not-json", b'{"wrong": true}'):
        monkeypatch.setattr(
            binance.urllib.request,
            "urlopen",
            lambda *_args, body=body, **_kwargs: UrlResponse(body),
        )
        with pytest.raises(DomainRejected, match="BINANCE_READ_ONLY_UNAVAILABLE"):
            binance._default_server_time_fetcher(1.0)

    monkeypatch.setattr(
        binance.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: UrlResponse(b"[]"),
    )
    assert binance._default_fetcher("https://example.invalid", {}, 1.0) == []
    monkeypatch.setattr(
        binance.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: UrlResponse(b"1"),
    )
    with pytest.raises(DomainRejected, match="BINANCE_RESPONSE_INVALID"):
        binance._default_fetcher("https://example.invalid", {}, 1.0)
    monkeypatch.setattr(
        binance.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: UrlResponse(b"not-json"),
    )
    with pytest.raises(DomainRejected, match="BINANCE_RESPONSE_INVALID"):
        binance._default_fetcher("https://example.invalid", {}, 1.0)


def test_portfolio_margin_reader_uses_papi_and_maps_unified_account_facts() -> None:
    observed_ms = int(NOW.timestamp() * 1_000)
    responses: dict[str, dict[str, Any] | list[dict[str, Any]]] = {
        "/fapi/v1/exchangeInfo": payloads()["/fapi/v1/exchangeInfo"],
        "/fapi/v1/premiumIndex": {"symbol": "BTCUSDT", "markPrice": "61000"},
        "/papi/v1/um/account": {
            "positions": [
                {
                    "symbol": "BTCUSDT",
                    "positionSide": "BOTH",
                    "positionAmt": "0.25",
                    "entryPrice": "60000",
                }
            ]
        },
        "/papi/v1/account": {
            "accountEquity": "1025",
            "totalAvailableBalance": "800",
            "updateTime": observed_ms,
        },
        "/papi/v1/um/openOrders": [],
        "/papi/v1/um/algo/openAlgoOrders": [
            {
                "symbol": "BTCUSDT",
                "algoId": 77,
                "clientAlgoId": "tpp-fixture",
                "algoStatus": "NEW",
                "side": "SELL",
                "orderType": "STOP_MARKET",
                "quantity": "0.25",
                "triggerPrice": "59000",
                "reduceOnly": True,
                "updateTime": observed_ms,
            }
        ],
        "/papi/v1/um/userTrades": payloads()["/fapi/v1/userTrades"],
        "/papi/v1/um/income": payloads()["/fapi/v1/income"],
    }
    calls: list[tuple[str, dict[str, str], float]] = []

    def fetch(
        url: str, headers: dict[str, str], timeout: float
    ) -> dict[str, Any] | list[dict[str, Any]]:
        calls.append((url, headers, timeout))
        return responses[urllib.parse.urlparse(url).path]

    client = BinancePortfolioMarginReadOnlyClient(
        base_url="https://papi.binance.com",
        api_key="unified-key",
        api_secret="unified-secret",  # noqa: S106
        fetcher=fetch,
        server_time_fetcher=lambda _timeout: observed_ms,
    )

    snapshot = client.read_snapshot("BTCUSDT", now=NOW.replace(year=2020))

    assert snapshot.observed_at == NOW
    assert snapshot.position.quantity == Decimal("0.25")
    assert snapshot.position.mark_price == Decimal("61000")
    assert snapshot.equity.equity == Decimal("1025")
    assert snapshot.equity.available_balance == Decimal("800")
    assert snapshot.protection is not None
    assert snapshot.protection.order_id == "77"
    assert snapshot.protection.quantity == Decimal("0.25")
    assert [urllib.parse.urlparse(call[0]).path for call in calls] == [
        "/fapi/v1/exchangeInfo",
        "/fapi/v1/premiumIndex",
        "/papi/v1/um/account",
        "/papi/v1/account",
        "/papi/v1/um/openOrders",
        "/papi/v1/um/algo/openAlgoOrders",
        "/papi/v1/um/userTrades",
        "/papi/v1/um/income",
    ]
    assert all(
        urllib.parse.urlparse(url).hostname == "fapi.binance.com"
        for url, _headers, _timeout in calls[:2]
    )
    assert all(
        urllib.parse.urlparse(url).hostname == "papi.binance.com"
        and headers == {"X-MBX-APIKEY": "unified-key"}
        for url, headers, _timeout in calls[2:]
    )
    for url, _headers, _timeout in calls[2:]:
        assert dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))["timestamp"] == str(
            observed_ms
        )


def test_portfolio_margin_runtime_request_and_weight_budget_is_reduced() -> None:
    observed_ms = int(NOW.timestamp() * 1_000)
    responses: dict[str, dict[str, Any] | list[dict[str, Any]]] = {
        "/fapi/v1/exchangeInfo": payloads()["/fapi/v1/exchangeInfo"],
        "/fapi/v1/premiumIndex": {"symbol": "BTCUSDT", "markPrice": "61000"},
        "/papi/v1/um/account": {
            "positions": [
                {
                    "symbol": "BTCUSDT",
                    "positionSide": "BOTH",
                    "positionAmt": "0.25",
                    "entryPrice": "60000",
                }
            ]
        },
        "/papi/v1/account": {
            "accountEquity": "1025",
            "totalAvailableBalance": "800",
            "updateTime": observed_ms,
        },
        "/papi/v1/um/openOrders": [],
        "/papi/v1/um/algo/openAlgoOrders": [],
        "/papi/v1/um/userTrades": payloads()["/fapi/v1/userTrades"],
        "/papi/v1/um/income": payloads()["/fapi/v1/income"],
    }
    request_paths: list[str] = []
    time_requests = 0

    def fetch(
        url: str, _headers: dict[str, str], _timeout: float
    ) -> dict[str, Any] | list[dict[str, Any]]:
        path = urllib.parse.urlparse(url).path
        request_paths.append(path)
        return responses[path]

    def server_time(_timeout: float) -> int:
        nonlocal time_requests
        time_requests += 1
        return observed_ms

    state = BinanceRequestState()
    client = BinancePortfolioMarginReadOnlyClient(
        base_url="https://papi.binance.com",
        api_key="unified-key",
        api_secret="unified-secret",  # noqa: S106
        fetcher=fetch,
        server_time_fetcher=server_time,
        request_state=state,
    )

    assert [item.symbol for item in client.read_active_instruments()] == ["BTCUSDT"]
    first = client.read_account_snapshots(("BTCUSDT",), now=NOW)
    first_requests = len(request_paths) + time_requests
    first_paths = list(request_paths)

    # Simulate the next 60-second runtime tick; the shared time cache is only
    # promised for 30 seconds, so this comparison includes one fresh time read.
    state.record_time_offset(0, synchronized_at=datetime.now(UTC) - timedelta(seconds=31))
    client.read_account_snapshots(("BTCUSDT",), now=NOW + timedelta(minutes=1))
    steady_requests = len(request_paths) + time_requests - first_requests
    steady_paths = request_paths[len(first_paths) :]

    assert first[0].orders == () and first[0].fills and first[0].funding
    assert first_requests == 9  # legacy: 10 requests every minute
    assert steady_requests == 6
    assert "/papi/v1/um/userTrades" not in steady_paths
    assert "/papi/v1/um/income" not in steady_paths
    assert first_paths.count("/fapi/v1/exchangeInfo") == 1

    # Current official IP weights for the exact one-symbol path: time 1,
    # exchangeInfo 1, UM account 5, account 20, both all-symbol order reads
    # 40 each, premiumIndex 1, userTrades 5 and income 30.
    legacy_weight_per_minute = 144
    first_weight = 143
    steady_current_weight = 107
    amortized_weight_per_minute = steady_current_weight + (35 / 10) + (1 / 30)
    assert first_weight < legacy_weight_per_minute
    assert amortized_weight_per_minute < 111


def test_portfolio_margin_account_snapshot_covers_all_positions_without_refetching_account() -> (
    None
):
    observed_ms = int(NOW.timestamp() * 1_000)
    calls: list[tuple[str, dict[str, str]]] = []
    collateral_by_symbol: dict[str, str] = {}

    def instrument(symbol: str) -> dict[str, Any]:
        return {
            "symbols": [
                {
                    "symbol": symbol,
                    "contractType": "PERPETUAL",
                    "status": "TRADING",
                    "quoteAsset": "USDT",
                    "marginAsset": collateral_by_symbol.get(symbol, "USDT"),
                    "filters": [
                        {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                        {"filterType": "LOT_SIZE", "stepSize": "0.001"},
                        {"filterType": "MIN_NOTIONAL", "notional": "5"},
                    ],
                }
            ]
        }

    def fetch(
        url: str, headers: dict[str, str], _timeout: float
    ) -> dict[str, Any] | list[dict[str, Any]]:
        parsed = urllib.parse.urlparse(url)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        calls.append((parsed.path, query))
        if parsed.path == "/papi/v1/um/account":
            return {
                "positions": [
                    {
                        "symbol": "BTCUSDT",
                        "positionSide": "BOTH",
                        "positionAmt": "0.25",
                        "entryPrice": "60000",
                    },
                    {
                        "symbol": "ETHUSDT",
                        "positionSide": "BOTH",
                        "positionAmt": "2",
                        "entryPrice": "3000",
                    },
                ]
            }
        if parsed.path == "/papi/v1/account":
            return {
                "accountEquity": "1025",
                "totalAvailableBalance": "800",
                "updateTime": observed_ms,
            }
        if parsed.path == "/fapi/v1/exchangeInfo":
            return instrument(query["symbol"])
        if parsed.path == "/fapi/v1/premiumIndex":
            return {
                "symbol": query["symbol"],
                "markPrice": "61000" if query["symbol"] == "BTCUSDT" else "3100",
            }
        if parsed.path in {
            "/papi/v1/um/openOrders",
            "/papi/v1/um/algo/openAlgoOrders",
            "/papi/v1/um/userTrades",
            "/papi/v1/um/income",
        }:
            assert headers == {"X-MBX-APIKEY": "unified-key"}
            return []
        raise AssertionError(parsed.path)

    client = BinancePortfolioMarginReadOnlyClient(
        base_url="https://papi.binance.com",
        api_key="unified-key",
        api_secret="unified-secret",  # noqa: S106
        fetcher=fetch,
        server_time_fetcher=lambda _timeout: observed_ms,
    )

    snapshots = client.read_account_snapshots(("BTCUSDT",), now=NOW)

    assert [(item.symbol, item.position.quantity) for item in snapshots] == [
        ("BTCUSDT", Decimal("0.25")),
        ("ETHUSDT", Decimal(2)),
    ]
    assert all(item.equity.currency == "USDT" for item in snapshots)
    paths = [path for path, _query in calls]
    assert paths.count("/papi/v1/um/account") == 1
    assert paths.count("/papi/v1/account") == 1
    assert paths.count("/papi/v1/um/openOrders") == 1
    assert paths.count("/papi/v1/um/algo/openAlgoOrders") == 1
    assert dict(calls)["/papi/v1/um/openOrders"].get("symbol") is None

    collateral_by_symbol["ETHUSDT"] = "USDC"
    cached = client.read_account_snapshots(("BTCUSDT",), now=NOW + timedelta(minutes=1))
    assert all(item.equity.currency == "USDT" for item in cached)
    assert [path for path, _query in calls].count("/fapi/v1/exchangeInfo") == 2

    uncached_client = BinancePortfolioMarginReadOnlyClient(
        base_url="https://papi.binance.com",
        api_key="unified-key",
        api_secret="unified-secret",  # noqa: S106
        fetcher=fetch,
        server_time_fetcher=lambda _timeout: observed_ms,
    )
    with pytest.raises(DomainRejected, match="BINANCE_ACCOUNT_EQUITY_AMBIGUOUS"):
        uncached_client.read_account_snapshots(("BTCUSDT",), now=NOW)


@pytest.mark.parametrize("failed_history_leg", [0, 2, 5])
def test_portfolio_margin_history_failure_keeps_complete_current_domain(
    failed_history_leg: int,
) -> None:
    observed_ms = int(NOW.timestamp() * 1_000)
    history_calls: list[tuple[str, str]] = []
    symbols = ("BTCUSDT", "ETHUSDT", "SOLUSDT")

    def instrument(symbol: str) -> dict[str, Any]:
        return {
            "symbols": [
                {
                    "symbol": symbol,
                    "contractType": "PERPETUAL",
                    "status": "TRADING",
                    "quoteAsset": "USDT",
                    "marginAsset": "USDT",
                    "filters": [
                        {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                        {"filterType": "LOT_SIZE", "stepSize": "0.001"},
                        {"filterType": "MIN_NOTIONAL", "notional": "5"},
                    ],
                }
            ]
        }

    def fetch(
        url: str, _headers: dict[str, str], _timeout: float
    ) -> dict[str, Any] | list[dict[str, Any]]:
        parsed = urllib.parse.urlparse(url)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        if parsed.path == "/papi/v1/um/account":
            return {
                "positions": [
                    {
                        "symbol": symbol,
                        "positionSide": "BOTH",
                        "positionAmt": str(index + 1),
                        "entryPrice": "100",
                    }
                    for index, symbol in enumerate(symbols)
                ]
            }
        if parsed.path == "/papi/v1/account":
            return {
                "accountEquity": "1025",
                "totalAvailableBalance": "800",
                "updateTime": observed_ms,
            }
        if parsed.path == "/fapi/v1/exchangeInfo":
            return instrument(query["symbol"])
        if parsed.path == "/fapi/v1/premiumIndex":
            return {"symbol": query["symbol"], "markPrice": "110"}
        if parsed.path in {"/papi/v1/um/openOrders", "/papi/v1/um/algo/openAlgoOrders"}:
            return []
        if parsed.path in {"/papi/v1/um/userTrades", "/papi/v1/um/income"}:
            history_calls.append((parsed.path, query["symbol"]))
            if len(history_calls) - 1 == failed_history_leg:
                raise DomainRejected(
                    "BINANCE_READ_ONLY_UNAVAILABLE", "history endpoint unavailable"
                )
            if parsed.path.endswith("userTrades"):
                return [
                    {
                        "symbol": query["symbol"],
                        "id": len(history_calls),
                        "orderId": len(history_calls),
                        "side": "BUY",
                        "qty": "1",
                        "price": "100",
                        "commission": "0",
                        "commissionAsset": "USDT",
                        "time": observed_ms,
                    }
                ]
            return [
                {
                    "symbol": query["symbol"],
                    "incomeType": "FUNDING_FEE",
                    "tranId": len(history_calls),
                    "income": "0",
                    "asset": "USDT",
                    "time": observed_ms,
                }
            ]
        raise AssertionError(parsed.path)

    client = BinancePortfolioMarginReadOnlyClient(
        base_url="https://papi.binance.com",
        api_key="unified-key",
        api_secret="unified-secret",  # noqa: S106
        fetcher=fetch,
        server_time_fetcher=lambda _timeout: observed_ms,
    )

    snapshots = client.read_account_snapshots(tuple(reversed(symbols)), now=NOW)

    assert [(item.symbol, item.position.quantity) for item in snapshots] == [
        ("BTCUSDT", Decimal(1)),
        ("ETHUSDT", Decimal(2)),
        ("SOLUSDT", Decimal(3)),
    ]
    assert len(history_calls) == failed_history_leg + 1
    assert all(item.history_error_code == "BINANCE_READ_ONLY_UNAVAILABLE" for item in snapshots)
    assert all(item.fills == () and item.funding == () for item in snapshots)


def test_portfolio_margin_reader_rejects_nonofficial_hosts_before_network() -> None:
    for base_url in ("https://fapi.binance.com", "http://papi.binance.com"):
        with pytest.raises(ValueError, match="official LIVE PAPI"):
            BinancePortfolioMarginReadOnlyClient(
                base_url=base_url,
                api_key=None,
                api_secret=None,
            )
