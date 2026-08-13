from __future__ import annotations

import urllib.parse
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from trading_control_plane.bybit import BybitReadOnlyClient
from trading_control_plane.domain import DomainRejected
from trading_control_plane.okx import OkxReadOnlyClient

NOW = datetime(2026, 8, 11, 8, 30, tzinfo=UTC)


def _okx_response(data: list[dict[str, Any]]) -> dict[str, Any]:
    return {"code": "0", "msg": "", "data": data}


def test_okx_reader_normalizes_contracts_and_complete_account_facts() -> None:
    calls: list[str] = []

    def fetcher(url: str, _headers: dict[str, str], _timeout: float) -> dict[str, Any]:
        calls.append(url)
        parsed = urllib.parse.urlparse(url)
        if parsed.path == "/api/v5/public/instruments":
            return _okx_response(
                [
                    {
                        "instType": "SWAP",
                        "instId": "BTC-USDT-SWAP",
                        "ctType": "linear",
                        "settleCcy": "USDT",
                        "state": "live",
                        "ctVal": "0.01",
                        "ctValCcy": "BTC",
                        "tickSz": "0.1",
                        "lotSz": "1",
                    }
                ]
            )
        if parsed.path == "/api/v5/public/mark-price":
            return _okx_response([{"instId": "BTC-USDT-SWAP", "markPx": "50000"}])
        if parsed.path == "/api/v5/account/balance":
            return _okx_response([{"totalEq": "1000", "availEq": "750", "uTime": "1786437000000"}])
        if parsed.path == "/api/v5/account/positions":
            return _okx_response(
                [
                    {
                        "instId": "BTC-USDT-SWAP",
                        "pos": "2",
                        "posSide": "long",
                        "avgPx": "49000",
                        "markPx": "50000",
                        "uTime": "1786437000000",
                    }
                ]
            )
        if parsed.path == "/api/v5/trade/orders-pending":
            return _okx_response(
                [
                    {
                        "instId": "BTC-USDT-SWAP",
                        "ordId": "order-1",
                        "clOrdId": "client-1",
                        "side": "sell",
                        "ordType": "conditional",
                        "sz": "2",
                        "accFillSz": "0",
                        "state": "live",
                        "reduceOnly": "true",
                        "slTriggerPx": "48000",
                        "uTime": "1786437000000",
                    }
                ]
            )
        if parsed.path == "/api/v5/trade/orders-algo-pending":
            return _okx_response([])
        if parsed.path == "/api/v5/trade/fills-history":
            return _okx_response(
                [
                    {
                        "instId": "BTC-USDT-SWAP",
                        "tradeId": "trade-1",
                        "ordId": "filled-order-1",
                        "side": "buy",
                        "fillSz": "1",
                        "fillPx": "49000",
                        "fee": "-0.1",
                        "feeCcy": "USDT",
                        "fillTime": "1786436900000",
                    }
                ]
            )
        if parsed.path == "/api/v5/account/bills":
            return _okx_response(
                [
                    {
                        "instId": "BTC-USDT-SWAP",
                        "billId": "bill-1",
                        "subType": "173",
                        "pnl": "-0.5",
                        "ccy": "USDT",
                        "ts": "1786436800000",
                    }
                ]
            )
        raise AssertionError(parsed.path)

    client = OkxReadOnlyClient(
        api_key="okx-key",
        api_secret="okx-secret",  # noqa: S106 - inert fixture
        passphrase="okx-pass",  # noqa: S106 - inert fixture
        fetcher=fetcher,
    )

    instruments = client.read_active_instruments()
    snapshots = client.read_account_snapshots((), now=NOW)

    assert len(instruments) == 1
    assert instruments[0].lot_size == Decimal("0.01")
    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot.position.quantity == Decimal("0.02")
    assert snapshot.equity.equity == Decimal("1000")
    assert snapshot.orders[0].ordered_quantity == Decimal("0.02")
    assert snapshot.fills[0].quantity == Decimal("0.01")
    assert snapshot.funding[0].amount == Decimal("-0.5")
    assert snapshot.protection is not None
    assert snapshot.protection.quantity == Decimal("0.02")
    assert snapshot.history_error_code is None
    assert sum("/api/v5/public/instruments" in item for item in calls) == 1


def test_okx_reader_fails_closed_on_unsupported_swap_exposure() -> None:
    def fetcher(url: str, _headers: dict[str, str], _timeout: float) -> dict[str, Any]:
        parsed = urllib.parse.urlparse(url)
        if parsed.path == "/api/v5/public/instruments":
            return _okx_response(
                [
                    {
                        "instType": "SWAP",
                        "instId": "BTC-USDT-SWAP",
                        "ctType": "linear",
                        "settleCcy": "USDT",
                        "state": "live",
                        "ctVal": "0.01",
                        "ctValCcy": "BTC",
                        "tickSz": "0.1",
                        "lotSz": "1",
                    }
                ]
            )
        if parsed.path == "/api/v5/public/mark-price":
            return _okx_response([{"instId": "BTC-USDT-SWAP", "markPx": "50000"}])
        if parsed.path == "/api/v5/account/balance":
            return _okx_response([{"totalEq": "1000", "availEq": "750"}])
        if parsed.path == "/api/v5/account/positions":
            return _okx_response([{"instId": "BTC-USDC-SWAP", "pos": "1", "posSide": "long"}])
        return _okx_response([])

    client = OkxReadOnlyClient(
        api_key="key",
        api_secret="secret",  # noqa: S106 - inert fixture
        passphrase="pass",  # noqa: S106 - inert fixture
        fetcher=fetcher,
    )

    with pytest.raises(DomainRejected, match="OKX_ACCOUNT_SCOPE_UNSUPPORTED"):
        client.read_account_snapshots((), now=NOW)


def test_okx_history_pagination_uses_bill_identity_and_is_bounded() -> None:
    cursors: list[str | None] = []

    def fetcher(url: str, _headers: dict[str, str], _timeout: float) -> dict[str, Any]:
        parsed = urllib.parse.urlparse(url)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        cursors.append(query.get("after"))
        if "after" not in query:
            return _okx_response([{"billId": str(200 - index)} for index in range(100)])
        assert query["after"] == "101"
        return _okx_response([{"billId": "100"}])

    client = OkxReadOnlyClient(
        api_key="key",
        api_secret="secret",  # noqa: S106 - inert fixture
        passphrase="pass",  # noqa: S106 - inert fixture
        fetcher=fetcher,
    )

    rows, incomplete = client._private_pages(
        "/api/v5/trade/fills-history",
        {"instType": "SWAP", "limit": "100"},
        cursor_field="billId",
        now=NOW,
    )

    assert len(rows) == 101
    assert incomplete is False
    assert cursors == [None, "101"]


def _bybit_response(
    rows: list[dict[str, Any]],
    *,
    cursor: str = "",
) -> dict[str, Any]:
    return {
        "retCode": 0,
        "retMsg": "OK",
        "result": {"list": rows, "nextPageCursor": cursor},
        "time": int(NOW.timestamp() * 1000),
    }


def test_bybit_reader_paginates_and_normalizes_unified_linear_facts() -> None:
    calls: list[str] = []

    def fetcher(url: str, _headers: dict[str, str], _timeout: float) -> dict[str, Any]:
        calls.append(url)
        parsed = urllib.parse.urlparse(url)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        if parsed.path == "/v5/market/instruments-info":
            return _bybit_response(
                [
                    {
                        "symbol": "BTCUSDT",
                        "contractType": "LinearPerpetual",
                        "settleCoin": "USDT",
                        "status": "Trading",
                        "priceFilter": {"tickSize": "0.1"},
                        "lotSizeFilter": {
                            "qtyStep": "0.001",
                            "minNotionalValue": "5",
                        },
                    }
                ]
            )
        if parsed.path == "/v5/market/tickers":
            return _bybit_response([{"symbol": "BTCUSDT", "markPrice": "50000"}])
        if parsed.path == "/v5/account/wallet-balance":
            return _bybit_response(
                [
                    {
                        "accountType": "UNIFIED",
                        "totalEquity": "1200",
                        "totalAvailableBalance": "800",
                    }
                ]
            )
        if parsed.path == "/v5/position/list":
            if query.get("settleCoin") != "USDT":
                return _bybit_response([])
            return _bybit_response(
                [
                    {
                        "symbol": "BTCUSDT",
                        "side": "Buy",
                        "size": "0.02",
                        "avgPrice": "49000",
                        "markPrice": "50000",
                        "stopLoss": "48000",
                        "positionIdx": 0,
                        "updatedTime": "1786437000000",
                    }
                ]
            )
        if parsed.path == "/v5/order/realtime":
            return _bybit_response([])
        if parsed.path == "/v5/execution/list":
            return _bybit_response(
                [
                    {
                        "symbol": "BTCUSDT",
                        "execId": "exec-1",
                        "orderId": "order-1",
                        "side": "Buy",
                        "execQty": "0.01",
                        "execPrice": "49000",
                        "execFee": "0.2",
                        "execTime": "1786436900000",
                    }
                ]
            )
        if parsed.path == "/v5/account/transaction-log":
            return _bybit_response(
                [
                    {
                        "id": "funding-1",
                        "symbol": "BTCUSDT",
                        "funding": "-0.4",
                        "currency": "USDT",
                        "transactionTime": "1786436800000",
                    }
                ]
            )
        raise AssertionError(parsed.path)

    client = BybitReadOnlyClient(
        api_key="bybit-key",
        api_secret="bybit-secret",  # noqa: S106 - inert fixture
        fetcher=fetcher,
    )

    snapshots = client.read_account_snapshots((), now=NOW)

    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot.instrument.minimum_notional == Decimal("5")
    assert snapshot.position.quantity == Decimal("0.02")
    assert snapshot.equity.available_balance == Decimal("800")
    assert snapshot.fills[0].fill_id == "exec-1"
    assert snapshot.funding[0].amount == Decimal("-0.4")
    assert snapshot.protection is not None
    assert snapshot.protection.trigger_price == Decimal("48000")
    assert any("category=inverse" in item for item in calls)
    assert any("category=option" in item for item in calls)
    assert any("settleCoin=USDC" in item for item in calls)


def test_bybit_reader_fails_closed_on_unsupported_derivative_exposure() -> None:
    def fetcher(url: str, _headers: dict[str, str], _timeout: float) -> dict[str, Any]:
        parsed = urllib.parse.urlparse(url)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        if parsed.path == "/v5/market/instruments-info":
            return _bybit_response(
                [
                    {
                        "symbol": "BTCUSDT",
                        "contractType": "LinearPerpetual",
                        "settleCoin": "USDT",
                        "status": "Trading",
                        "priceFilter": {"tickSize": "0.1"},
                        "lotSizeFilter": {
                            "qtyStep": "0.001",
                            "minNotionalValue": "5",
                        },
                    }
                ]
            )
        if parsed.path == "/v5/market/tickers":
            return _bybit_response([{"symbol": "BTCUSDT", "markPrice": "50000"}])
        if parsed.path == "/v5/account/wallet-balance":
            return _bybit_response(
                [
                    {
                        "accountType": "UNIFIED",
                        "totalEquity": "1200",
                        "totalAvailableBalance": "800",
                    }
                ]
            )
        if parsed.path == "/v5/position/list" and query.get("settleCoin") == "USDC":
            return _bybit_response([{"symbol": "BTCPERP", "size": "1", "side": "Buy"}])
        return _bybit_response([])

    client = BybitReadOnlyClient(
        api_key="key",
        api_secret="secret",  # noqa: S106 - inert fixture
        fetcher=fetcher,
    )

    with pytest.raises(DomainRejected, match="BYBIT_ACCOUNT_SCOPE_UNSUPPORTED"):
        client.read_account_snapshots((), now=NOW)
