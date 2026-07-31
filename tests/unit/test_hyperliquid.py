from __future__ import annotations

import io
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from trading_control_plane import hyperliquid
from trading_control_plane.domain import DomainRejected
from trading_control_plane.hyperliquid import (
    HyperliquidReadOnlyClient,
    resolve_hyperliquid_main_account,
)

NOW = datetime(2026, 7, 19, 13, tzinfo=UTC)
ACCOUNT = "0x1111111111111111111111111111111111111111"


def contract_payloads() -> dict[str, dict[str, Any] | list[Any] | str]:
    observed_ms = int(NOW.timestamp() * 1_000)
    return {
        "metaAndAssetCtxs": [
            {"universe": [{"name": "BTC", "szDecimals": 5}]},
            [{"markPx": "61000", "funding": "0.0001"}],
        ],
        "clearinghouseState": {
            "marginSummary": {"accountValue": "1025"},
            "withdrawable": "800",
            "time": observed_ms,
            "assetPositions": [
                {
                    "type": "oneWay",
                    "position": {
                        "coin": "BTC",
                        "szi": "0.25",
                        "entryPx": "60000",
                        "positionValue": "15250",
                        "unrealizedPnl": "250",
                    },
                }
            ],
        },
        "userAbstraction": "disabled",
        "frontendOpenOrders": [
            {
                "coin": "BTC",
                "oid": 77,
                "cloid": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "side": "A",
                "limitPx": "58900",
                "sz": "0.25",
                "origSz": "0.25",
                "timestamp": observed_ms,
                "triggerPx": "59000",
                "isTrigger": True,
                "reduceOnly": True,
                "orderType": "Stop Market",
            }
        ],
        "userFillsByTime": [
            {
                "coin": "BTC",
                "oid": 70,
                "tid": 88,
                "side": "B",
                "px": "60000",
                "sz": "0.25",
                "fee": "1.25",
                "feeToken": "USDC",
                "time": observed_ms,
            }
        ],
        "userFunding": [
            {
                "time": observed_ms,
                "hash": "0xfunding",
                "delta": {"coin": "BTC", "usdc": "-0.75", "fundingRate": "0.0001"},
            }
        ],
    }


def client_with_contract(
    responses: dict[str, dict[str, Any] | list[Any] | str],
) -> tuple[HyperliquidReadOnlyClient, list[tuple[str, dict[str, Any], float]]]:
    calls: list[tuple[str, dict[str, Any], float]] = []

    def fetch(
        url: str, payload: dict[str, Any], timeout: float
    ) -> dict[str, Any] | list[Any] | str:
        calls.append((url, payload, timeout))
        return responses[str(payload["type"])]

    return (
        HyperliquidReadOnlyClient(
            base_url="https://api.hyperliquid-testnet.xyz",
            account_address=ACCOUNT,
            fetcher=fetch,
        ),
        calls,
    )


def test_core_info_contract_maps_required_facts_without_exchange_actions() -> None:
    client, calls = client_with_contract(contract_payloads())

    snapshot = client.read_snapshot("BTC", now=NOW)

    assert [call[1]["type"] for call in calls] == [
        "metaAndAssetCtxs",
        "clearinghouseState",
        "userAbstraction",
        "frontendOpenOrders",
        "userFillsByTime",
        "userFunding",
    ]
    assert calls[0][1]["dex"] == ""
    assert calls[1][1] == {
        "type": "clearinghouseState",
        "user": ACCOUNT,
        "dex": "",
    }
    assert calls[4][1]["startTime"] == int((NOW - hyperliquid.HISTORY_WINDOW).timestamp() * 1_000)
    assert "endTime" not in calls[4][1]
    assert calls[5][1]["startTime"] == calls[4][1]["startTime"]
    assert all(call[0] == "https://api.hyperliquid-testnet.xyz/info" for call in calls)
    assert all(call[2] == 5.0 for call in calls)
    assert snapshot.instrument.lot_size == Decimal("0.00001")
    assert snapshot.instrument.tick_size == Decimal("0.1")
    assert snapshot.instrument.minimum_notional == Decimal(10)
    assert snapshot.position.quantity == Decimal("0.25")
    assert snapshot.position.mark_price == Decimal("61000")
    assert snapshot.equity.equity == Decimal("1025")
    assert snapshot.fills[0].fill_id == "88"
    assert snapshot.funding[0].amount == Decimal("-0.75")
    assert snapshot.protection is not None
    assert snapshot.protection.order_id == "77"
    assert snapshot.protection.quantity == Decimal("0.25")
    assert not hasattr(client, "send_order")
    assert not hasattr(client, "exchange")


def test_unified_account_uses_spot_usdc_total_and_hold_for_equity() -> None:
    responses = contract_payloads()
    responses["userAbstraction"] = "unifiedAccount"
    responses["spotClearinghouseState"] = {
        "balances": [
            {
                "coin": "USDC",
                "token": 0,
                "total": "9.940002",
                "hold": "1.25",
            }
        ]
    }
    client, calls = client_with_contract(responses)

    first = client.read_snapshot("BTC", now=NOW)
    client.read_snapshot("BTC", now=NOW)

    assert first.equity.equity == Decimal("9.940002")
    assert first.equity.available_balance == Decimal("8.690002")
    assert [call[1]["type"] for call in calls].count("userAbstraction") == 1
    assert [call[1]["type"] for call in calls].count("spotClearinghouseState") == 2


def test_core_only_and_official_host_boundaries_fail_before_network() -> None:
    mainnet = HyperliquidReadOnlyClient(
        base_url="https://api.hyperliquid.xyz",
        account_address=ACCOUNT,
    )
    assert mainnet.fact_environment == "LIVE"

    for base_url in (
        "https://api.hyperliquid.example",
        "http://api.hyperliquid-testnet.xyz",
    ):
        with pytest.raises(ValueError, match="official API host"):
            HyperliquidReadOnlyClient(base_url=base_url, account_address=ACCOUNT)

    with pytest.raises(ValueError, match="Core only"):
        HyperliquidReadOnlyClient(
            base_url="https://api.hyperliquid.xyz",
            account_address=ACCOUNT,
            dex="hip3-dex",
        )


def test_invalid_account_and_response_precision_fail_closed() -> None:
    calls: list[dict[str, Any]] = []
    unconfigured = HyperliquidReadOnlyClient(
        base_url="https://api.hyperliquid-testnet.xyz",
        account_address="0xnot-an-address",
        fetcher=lambda _url, payload, _timeout: calls.append(payload) or {},
    )
    with pytest.raises(DomainRejected, match="HYPERLIQUID_READ_ONLY_NOT_CONFIGURED"):
        unconfigured.read_snapshot("BTC", now=NOW)
    assert calls == []

    responses = contract_payloads()
    meta = responses["metaAndAssetCtxs"]
    assert isinstance(meta, list)
    universe = meta[0]
    assert isinstance(universe, dict)
    universe["universe"][0]["szDecimals"] = 7
    client, _ = client_with_contract(responses)
    with pytest.raises(DomainRejected, match="HYPERLIQUID_RESPONSE_INVALID"):
        client.read_snapshot("BTC", now=NOW)


def test_opposite_non_trigger_order_does_not_count_as_native_protection() -> None:
    responses = contract_payloads()
    orders = responses["frontendOpenOrders"]
    assert isinstance(orders, list)
    orders[0]["isTrigger"] = False
    client, _ = client_with_contract(responses)

    assert client.read_snapshot("BTC", now=NOW).protection is None


def test_flat_account_and_invalid_symbols_are_explicit() -> None:
    responses = contract_payloads()
    account = responses["clearinghouseState"]
    assert isinstance(account, dict)
    account["assetPositions"] = []
    orders = responses["frontendOpenOrders"]
    assert isinstance(orders, list)
    orders.clear()
    client, calls = client_with_contract(responses)

    flat = client.read_snapshot("BTC", now=NOW)

    assert flat.position.quantity == 0
    assert flat.position.average_entry_price == 0
    assert flat.protection is None
    before = len(calls)
    with pytest.raises(DomainRejected, match="HYPERLIQUID_SYMBOL_INVALID"):
        client.read_snapshot("btc", now=NOW)
    assert len(calls) == before


def test_malformed_order_fill_and_instrument_contracts_fail_closed() -> None:
    with pytest.raises(DomainRejected, match="HYPERLIQUID_RESPONSE_INVALID"):
        HyperliquidReadOnlyClient._parse_instrument({}, "BTC")
    with pytest.raises(DomainRejected, match="HYPERLIQUID_INSTRUMENT_UNAVAILABLE"):
        HyperliquidReadOnlyClient._parse_instrument(
            [{"universe": [{"name": "ETH", "szDecimals": 4}]}, [{"markPx": "1"}]],
            "BTC",
        )
    with pytest.raises(DomainRejected, match="HYPERLIQUID_RESPONSE_INVALID"):
        HyperliquidReadOnlyClient._parse_orders(
            [{"coin": "BTC", "oid": 1, "side": "X", "origSz": "1", "sz": "1"}],
            "BTC",
            NOW,
        )
    with pytest.raises(DomainRejected, match="HYPERLIQUID_RESPONSE_INVALID"):
        HyperliquidReadOnlyClient._parse_orders(
            [{"coin": "BTC", "oid": 1, "side": "B", "origSz": "1", "sz": "2"}],
            "BTC",
            NOW,
        )
    with pytest.raises(DomainRejected, match="HYPERLIQUID_RESPONSE_INVALID"):
        HyperliquidReadOnlyClient._parse_fills(
            [{"coin": "BTC", "oid": 1, "tid": 2, "side": "X"}], "BTC", NOW
        )


def test_default_info_fetcher_parses_json_and_reports_transport_or_shape_errors(
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
        hyperliquid.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: Response(b'{"ok":true}'),
    )
    assert hyperliquid._default_fetcher("https://example.invalid/info", {}, 1.0) == {"ok": True}
    monkeypatch.setattr(
        hyperliquid.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: Response(b"not-json"),
    )
    with pytest.raises(DomainRejected, match="HYPERLIQUID_RESPONSE_INVALID"):
        hyperliquid._default_fetcher("https://example.invalid/info", {}, 1.0)
    monkeypatch.setattr(
        hyperliquid.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: Response(b"1"),
    )
    with pytest.raises(DomainRejected, match="HYPERLIQUID_RESPONSE_INVALID"):
        hyperliquid._default_fetcher("https://example.invalid/info", {}, 1.0)

    def unavailable(*_args: object, **_kwargs: object) -> None:
        raise hyperliquid.urllib.error.URLError("offline")

    monkeypatch.setattr(hyperliquid.urllib.request, "urlopen", unavailable)
    with pytest.raises(DomainRejected, match="HYPERLIQUID_READ_ONLY_UNAVAILABLE"):
        hyperliquid._default_fetcher("https://example.invalid/info", {}, 1.0)


def test_default_info_fetcher_retries_429_with_bounded_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    delays: list[float] = []

    def rate_limited_then_ready(*_args: object, **_kwargs: object) -> object:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise hyperliquid.urllib.error.HTTPError(
                "https://api.hyperliquid.xyz/info",
                429,
                "rate limited",
                {},
                io.BytesIO(b""),
            )

        class Response:
            def __enter__(self) -> Response:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            @staticmethod
            def read() -> bytes:
                return b'{"ok":true}'

        return Response()

    monkeypatch.setattr(hyperliquid.urllib.request, "urlopen", rate_limited_then_ready)
    monkeypatch.setattr(hyperliquid.time, "sleep", delays.append)

    assert hyperliquid._default_fetcher("https://api.hyperliquid.xyz/info", {}, 1.0) == {"ok": True}
    assert attempts == 3
    assert delays == [0.5, 1.0]


def test_api_wallet_role_resolves_owning_main_account_without_using_agent_for_queries() -> None:
    api_wallet = "0x2222222222222222222222222222222222222222"
    calls: list[dict[str, Any]] = []

    resolved = resolve_hyperliquid_main_account(
        base_url="https://api.hyperliquid.xyz",
        account_address=None,
        api_wallet_address=api_wallet,
        fetcher=lambda _url, payload, _timeout: (
            calls.append(payload) or {"role": "agent", "data": {"user": ACCOUNT}}
        ),
    )

    assert resolved == ACCOUNT
    assert calls == [{"type": "userRole", "user": api_wallet}]
    with pytest.raises(DomainRejected, match="HYPERLIQUID_ACCOUNT_UNRESOLVED"):
        resolve_hyperliquid_main_account(
            base_url="https://api.hyperliquid.xyz",
            account_address=None,
            api_wallet_address=api_wallet,
            fetcher=lambda _url, _payload, _timeout: {"role": "user"},
        )
