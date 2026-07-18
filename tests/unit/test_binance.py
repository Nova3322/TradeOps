from __future__ import annotations

import hashlib
import hmac
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from trading_control_plane.binance import BinanceReadOnlyClient
from trading_control_plane.domain import DomainRejected

NOW = datetime(2026, 7, 19, 10, tzinfo=UTC)


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
