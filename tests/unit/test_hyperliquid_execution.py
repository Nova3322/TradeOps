from __future__ import annotations

import io
import urllib.error
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from trading_control_plane import hyperliquid_execution
from trading_control_plane.binance_execution import ProtectionCancelCommand
from trading_control_plane.domain import DomainRejected
from trading_control_plane.hyperliquid_execution import (
    HyperliquidLiveClient,
    HyperliquidTestnetClient,
    HyperliquidTestnetOrder,
    HyperliquidTestnetOrderCommand,
    HyperliquidTestnetProtectionCommand,
    build_hyperliquid_signer,
)

NOW = datetime(2026, 7, 19, 14, tzinfo=UTC)
ACCOUNT = "0x1111111111111111111111111111111111111111"
SUBACCOUNT = "0x2222222222222222222222222222222222222222"
SIGNATURE = {"r": "0x01", "s": "0x02", "v": 27}


class ContractVenue:
    def __init__(self, *, fill_orders: bool = True) -> None:
        self.calls: list[tuple[str, dict[str, Any], float]] = []
        self.orders: dict[str, dict[str, Any]] = {}
        self.next_order_id = 100
        self.fill_orders = fill_orders

    def __call__(
        self, url: str, payload: dict[str, Any], timeout: float
    ) -> dict[str, Any] | list[Any]:
        self.calls.append((url, payload, timeout))
        if url.endswith("/info"):
            if payload["type"] == "metaAndAssetCtxs":
                return [
                    {"universe": [{"name": "BTC", "szDecimals": 5}]},
                    [{"markPx": "61000"}],
                ]
            return self.orders.get(str(payload["oid"]), {"status": "unknownOid"})
        action = payload["action"]
        if action["type"] == "cancelByCloid":
            cloid = str(action["cancels"][0]["cloid"])
            self.orders[cloid]["order"]["status"] = "canceled"
            self.orders[cloid]["order"]["statusTimestamp"] = int(NOW.timestamp() * 1_000)
            return {
                "status": "ok",
                "response": {"type": "cancel", "data": {"statuses": ["success"]}},
            }
        order_action = action["orders"][0]
        cloid = str(order_action["c"])
        self.next_order_id += 1
        trigger = "trigger" in order_action["t"]
        status = "filled" if self.fill_orders and not trigger else "open"
        remaining = "0" if status == "filled" else order_action["s"]
        order = {
            "coin": "BTC",
            "oid": self.next_order_id,
            "cloid": cloid,
            "side": "B" if order_action["b"] else "A",
            "limitPx": order_action["p"],
            "sz": remaining,
            "origSz": order_action["s"],
            "timestamp": int(NOW.timestamp() * 1_000),
            "triggerPx": order_action["t"].get("trigger", {}).get("triggerPx", "0"),
            "isTrigger": trigger,
            "reduceOnly": order_action["r"],
        }
        self.orders[cloid] = {
            "status": "order",
            "order": {
                "order": order,
                "status": status,
                "statusTimestamp": int(NOW.timestamp() * 1_000),
            },
        }
        acknowledgement = (
            {
                "filled": {
                    "totalSz": order_action["s"],
                    "avgPx": order_action["p"],
                    "oid": self.next_order_id,
                }
            }
            if status == "filled"
            else {"resting": {"oid": self.next_order_id}}
        )
        return {
            "status": "ok",
            "response": {"type": "order", "data": {"statuses": [acknowledgement]}},
        }


def signer_calls() -> tuple[list[tuple[dict[str, Any], int]], Any]:
    calls: list[tuple[dict[str, Any], int]] = []

    def signer(action: dict[str, Any], nonce: int) -> dict[str, Any]:
        calls.append((action, nonce))
        return SIGNATURE

    return calls, signer


def client(venue: ContractVenue) -> tuple[HyperliquidTestnetClient, list[Any]]:
    signatures, signer = signer_calls()
    return (
        HyperliquidTestnetClient(
            base_url="https://api.hyperliquid-testnet.xyz",
            account_address=ACCOUNT,
            signer=signer,
            requester=venue,
        ),
        signatures,
    )


def test_query_first_ioc_uses_stable_cloid_and_restart_does_not_send_twice() -> None:
    venue = ContractVenue()
    testnet, signatures = client(venue)
    command = HyperliquidTestnetOrderCommand(
        symbol="BTC",
        side="BUY",
        quantity=Decimal("0.25"),
        limit_price=Decimal("61000"),
        reduce_only=False,
        client_order_id="0x0123456789abcdef0123456789abcdef",
    )

    first = testnet.ensure_order(command, now=NOW)
    restarted = testnet.ensure_order(command, now=NOW)

    assert first == restarted
    assert [
        call[1]["type"] if call[0].endswith("/info") else "exchange" for call in venue.calls
    ] == [
        "orderStatus",
        "metaAndAssetCtxs",
        "exchange",
        "orderStatus",
    ]
    assert len(signatures) == 1
    action, nonce = signatures[0]
    assert nonce == int(NOW.timestamp() * 1_000)
    assert action == {
        "type": "order",
        "orders": [
            {
                "a": 0,
                "b": True,
                "p": "61000",
                "s": "0.25",
                "r": False,
                "t": {"limit": {"tif": "Ioc"}},
                "c": command.client_order_id,
            }
        ],
        "grouping": "na",
    }
    exchange_body = venue.calls[2][1]
    assert exchange_body["signature"] == SIGNATURE
    assert testnet.account_scope == "MAIN_ACCOUNT"
    assert "vaultAddress" not in exchange_body
    assert first.status == "FILLED"


def test_explicit_subaccount_is_used_for_queries_and_exchange_actions() -> None:
    venue = ContractVenue()
    signatures, signer = signer_calls()
    testnet = HyperliquidTestnetClient(
        base_url="https://api.hyperliquid-testnet.xyz",
        account_address=ACCOUNT,
        subaccount_address=SUBACCOUNT,
        signer=signer,
        requester=venue,
    )
    command = HyperliquidTestnetOrderCommand(
        symbol="BTC",
        side="BUY",
        quantity=Decimal("0.25"),
        limit_price=Decimal("61000"),
        reduce_only=False,
        client_order_id="0x0123456789abcdef0123456789abcdef",
    )

    result = testnet.ensure_order(command, now=NOW)

    assert result.status == "FILLED"
    assert testnet.account_scope == "SUBACCOUNT"
    assert venue.calls[0][1]["user"] == SUBACCOUNT
    assert venue.calls[2][1]["vaultAddress"] == SUBACCOUNT
    assert len(signatures) == 1


def test_cancel_by_cloid_and_native_trigger_protection_use_official_actions() -> None:
    venue = ContractVenue(fill_orders=False)
    testnet, _ = client(venue)
    order_command = HyperliquidTestnetOrderCommand(
        "BTC",
        "SELL",
        Decimal("0.5"),
        Decimal("60000"),
        True,
        "0xfedcba9876543210fedcba9876543210",
    )
    testnet.ensure_order(order_command, now=NOW)
    cancelled = testnet.cancel_order(order_command, now=NOW)
    protection_command = HyperliquidTestnetProtectionCommand(
        "BTC",
        "SELL",
        Decimal("0.25"),
        Decimal("59000"),
        Decimal("58900"),
        "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    )
    protection = testnet.ensure_protection(protection_command, now=NOW)
    cancelled_protection = testnet.cancel_protection(
        ProtectionCancelCommand(
            symbol=protection_command.symbol,
            client_order_id=protection_command.client_order_id,
        ),
        now=NOW,
    )

    assert cancelled is not None and cancelled.status == "CANCELLED"
    cancel_action = next(
        call[1]["action"]
        for call in venue.calls
        if call[0].endswith("/exchange") and call[1]["action"]["type"] == "cancelByCloid"
    )
    assert cancel_action == {
        "type": "cancelByCloid",
        "cancels": [{"asset": 0, "cloid": order_command.client_order_id}],
    }
    trigger_action = next(
        call[1]["action"]["orders"][0]
        for call in venue.calls
        if call[0].endswith("/exchange")
        and call[1]["action"]["type"] == "order"
        and "trigger" in call[1]["action"]["orders"][0]["t"]
    )
    assert trigger_action["p"] == "58900"
    assert trigger_action["r"] is True
    assert trigger_action["t"] == {
        "trigger": {"isMarket": True, "triggerPx": "59000", "tpsl": "sl"}
    }
    assert protection.order_type == "TRIGGER_MARKET"
    assert protection.stop_price == Decimal("59000")
    assert cancelled_protection is not None
    assert cancelled_protection.status == "CANCELLED"


def test_default_off_signer_and_non_testnet_hosts_fail_closed() -> None:
    for base_url in (
        "https://api.hyperliquid.xyz",
        "https://api.hyperliquid.example",
        "http://api.hyperliquid-testnet.xyz",
    ):
        with pytest.raises(ValueError, match="official testnet"):
            HyperliquidTestnetClient(
                base_url=base_url,
                account_address=ACCOUNT,
                signer=None,
            )

    testnet = HyperliquidTestnetClient(
        base_url="https://api.hyperliquid-testnet.xyz",
        account_address=ACCOUNT,
        signer=None,
        requester=lambda _url, _payload, _timeout: pytest.fail("network must not be called"),
    )
    with pytest.raises(DomainRejected, match="HYPERLIQUID_TESTNET_NOT_CONFIGURED"):
        testnet.ensure_order(
            HyperliquidTestnetOrderCommand(
                "BTC",
                "BUY",
                Decimal("0.25"),
                Decimal("61000"),
                False,
                "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            ),
            now=NOW,
        )


def test_explicit_price_precision_is_required_without_implicit_slippage() -> None:
    venue = ContractVenue()
    testnet, _ = client(venue)
    with pytest.raises(DomainRejected, match="HYPERLIQUID_ORDER_PRECISION_INVALID"):
        testnet.ensure_order(
            HyperliquidTestnetOrderCommand(
                "BTC",
                "BUY",
                Decimal("0.25"),
                Decimal("61000.12"),
                False,
                "0xcccccccccccccccccccccccccccccccc",
            ),
            now=NOW,
        )
    assert not any(call[0].endswith("/exchange") for call in venue.calls)


def test_network_timeout_distinguishes_info_unavailable_from_exchange_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timeout(*_args: object, **_kwargs: object) -> None:
        raise TimeoutError

    monkeypatch.setattr(hyperliquid_execution.urllib.request, "urlopen", timeout)

    with pytest.raises(DomainRejected, match="HYPERLIQUID_TESTNET_UNAVAILABLE"):
        hyperliquid_execution._default_requester("https://example.invalid/info", {}, 1.0)
    with pytest.raises(DomainRejected, match="HYPERLIQUID_TESTNET_OUTCOME_UNKNOWN"):
        hyperliquid_execution._default_requester("https://example.invalid/exchange", {}, 1.0)


def order_status(
    command: HyperliquidTestnetOrderCommand,
    status: str = "open",
    **overrides: Any,
) -> dict[str, Any]:
    order = {
        "coin": command.symbol,
        "oid": 42,
        "cloid": command.client_order_id,
        "side": "B" if command.side == "BUY" else "A",
        "limitPx": str(command.limit_price),
        "sz": str(command.quantity),
        "origSz": str(command.quantity),
        "timestamp": int(NOW.timestamp() * 1_000),
        "triggerPx": "0",
        "isTrigger": False,
        "reduceOnly": command.reduce_only,
        **overrides,
    }
    return {
        "status": "order",
        "order": {"order": order, "status": status, "statusTimestamp": 0},
    }


def fixed_client(response: Any, *, signer: Any = None) -> HyperliquidTestnetClient:
    return HyperliquidTestnetClient(
        base_url="https://api.hyperliquid-testnet.xyz",
        account_address=ACCOUNT,
        signer=(lambda _action, _nonce: SIGNATURE) if signer is None else signer,
        requester=lambda _url, _payload, _timeout: response,
    )


def test_order_status_families_and_identity_shapes_fail_closed() -> None:
    command = HyperliquidTestnetOrderCommand(
        "BTC",
        "BUY",
        Decimal("0.25"),
        Decimal("61000"),
        False,
        "0xdddddddddddddddddddddddddddddddd",
    )
    for venue_status, expected in (
        ("canceled", "CANCELLED"),
        ("marginCanceled", "CANCELLED"),
        ("rejected", "REJECTED"),
        ("tickRejected", "REJECTED"),
        ("unexpectedStatus", "UNKNOWN"),
    ):
        result = fixed_client(order_status(command, venue_status)).query_order(
            "BTC", command.client_order_id, expected_order_type="IOC_LIMIT", now=NOW
        )
        assert result is not None and result.status == expected

    invalid_responses: tuple[tuple[Any, str], ...] = (
        ([], "RESPONSE_INVALID"),
        ({"status": "unexpected"}, "RESPONSE_INVALID"),
        ({"status": "order", "order": {}}, "RESPONSE_INVALID"),
        (order_status(command, coin="ETH"), "IDENTITY_CONFLICT"),
        (order_status(command, side="X"), "RESPONSE_INVALID"),
        (order_status(command, isTrigger=True), "IDENTITY_CONFLICT"),
        (order_status(command, origSz="0"), "RESPONSE_INVALID"),
        (order_status(command, sz="0.5"), "RESPONSE_INVALID"),
    )
    for response, code in invalid_responses:
        with pytest.raises(DomainRejected, match=code):
            fixed_client(response).query_order(
                "BTC", command.client_order_id, expected_order_type="IOC_LIMIT", now=NOW
            )

    with pytest.raises(DomainRejected, match="IDENTITY_INVALID"):
        fixed_client({"status": "unknownOid"}).query_order(
            "BTC", "not-a-cloid", expected_order_type="IOC_LIMIT", now=NOW
        )


def test_metadata_signer_precision_and_exchange_shapes_fail_closed() -> None:
    invalid_metadata: tuple[Any, ...] = (
        {},
        [{"universe": "invalid"}, []],
        [{"universe": [{"name": "BTC"}]}, []],
        [{"universe": [{"name": "BTC", "szDecimals": 7}]}, []],
        [{"universe": [{"name": "ETH", "szDecimals": 4}]}, []],
    )
    for response in invalid_metadata:
        with pytest.raises(DomainRejected):
            fixed_client(response)._asset_index("BTC")

    with pytest.raises(ValueError, match="Core only"):
        HyperliquidTestnetClient(
            base_url="https://api.hyperliquid-testnet.xyz",
            account_address=ACCOUNT,
            signer=lambda _action, _nonce: SIGNATURE,
            dex="hip3",
        )
    with pytest.raises(ValueError, match="address is invalid"):
        HyperliquidTestnetClient(
            base_url="https://api.hyperliquid-testnet.xyz",
            account_address=ACCOUNT,
            signer=lambda _action, _nonce: SIGNATURE,
            subaccount_address="invalid",
        )

    invalid_signer = fixed_client({}, signer=lambda _action, _nonce: {})
    with pytest.raises(DomainRejected, match="SIGNER_INVALID"):
        invalid_signer._exchange({"type": "cancelByCloid"}, now=NOW)
    with pytest.raises(DomainRejected, match="RESPONSE_INVALID"):
        fixed_client([])._exchange({"type": "cancelByCloid"}, now=NOW)

    recorded: list[dict[str, Any]] = []
    subaccount_client = HyperliquidTestnetClient(
        base_url="https://api.hyperliquid-testnet.xyz",
        account_address=ACCOUNT,
        signer=lambda _action, _nonce: SIGNATURE,
        subaccount_address=SUBACCOUNT,
        requester=lambda _url, payload, _timeout: recorded.append(payload) or {},
    )
    subaccount_client._exchange({"type": "cancelByCloid"}, now=NOW)
    assert recorded[0]["vaultAddress"] == SUBACCOUNT

    for quantity, price, size_decimals in (
        (Decimal(0), Decimal(1), 5),
        (Decimal("0.000001"), Decimal(1), 5),
        (Decimal(1), Decimal("NaN"), 5),
        (Decimal(1), Decimal("1.23"), 5),
    ):
        with pytest.raises(DomainRejected, match="PRECISION_INVALID"):
            HyperliquidTestnetClient._validate_precision(quantity, price, size_decimals)

    with pytest.raises(DomainRejected, match="RESPONSE_INVALID"):
        hyperliquid_execution._decimal("not-numeric", "field")
    with pytest.raises(DomainRejected, match="RESPONSE_INVALID"):
        hyperliquid_execution._decimal("NaN", "field")
    with pytest.raises(DomainRejected, match="RESPONSE_INVALID"):
        hyperliquid_execution._time("invalid", NOW)


def test_exchange_nonce_is_monotonic_when_actions_share_one_clock_millisecond() -> None:
    nonces: list[int] = []
    client = HyperliquidTestnetClient(
        base_url="https://api.hyperliquid-testnet.xyz",
        account_address=ACCOUNT,
        signer=lambda _action, nonce: nonces.append(nonce) or SIGNATURE,
        requester=lambda _url, _payload, _timeout: {},
    )

    client._exchange({"type": "cancelByCloid"}, now=NOW)
    client._exchange({"type": "cancelByCloid"}, now=NOW)

    assert nonces == [int(NOW.timestamp() * 1_000), int(NOW.timestamp() * 1_000) + 1]


def test_live_wrapper_requires_mainnet_host_and_maps_adapter_errors() -> None:
    with pytest.raises(ValueError, match="official live"):
        HyperliquidLiveClient(
            base_url="https://api.hyperliquid-testnet.xyz",
            account_address=ACCOUNT,
            signer=lambda _action, _nonce: SIGNATURE,
        )

    live = HyperliquidLiveClient(
        base_url="https://api.hyperliquid.xyz",
        account_address=ACCOUNT,
        signer=lambda _action, _nonce: SIGNATURE,
        requester=lambda _url, _payload, _timeout: {"status": "unknownOid"},
    )
    command = HyperliquidTestnetOrderCommand(
        "BTC",
        "BUY",
        Decimal("0.001"),
        Decimal("61000"),
        False,
        "0x66666666666666666666666666666666",
    )

    assert live.recover_order(command, now=NOW) is None


def test_order_acknowledgement_partial_rejection_and_invalid_shapes() -> None:
    command = HyperliquidTestnetOrderCommand(
        "BTC",
        "SELL",
        Decimal(1),
        Decimal(100),
        True,
        "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
    )
    partial = HyperliquidTestnetClient._order_response(
        {
            "status": "ok",
            "response": {
                "type": "order",
                "data": {"statuses": [{"filled": {"totalSz": "0.4", "oid": 7}}]},
            },
        },
        command,
        "IOC_LIMIT",
        now=NOW,
    )
    assert partial.status == "CANCELLED"
    assert partial.filled_quantity == Decimal("0.4")

    invalid: tuple[tuple[dict[str, Any], str], ...] = (
        ({"status": "err"}, "REJECTED"),
        ({"status": "ok", "response": {"type": "order", "data": {}}}, "RESPONSE_INVALID"),
        (
            {
                "status": "ok",
                "response": {"type": "order", "data": {"statuses": ["invalid"]}},
            },
            "RESPONSE_INVALID",
        ),
        (
            {
                "status": "ok",
                "response": {
                    "type": "order",
                    "data": {"statuses": [{"error": "bad tick"}]},
                },
            },
            "REJECTED",
        ),
        (
            {
                "status": "ok",
                "response": {"type": "order", "data": {"statuses": [{}]}},
            },
            "RESPONSE_INVALID",
        ),
    )
    for response, code in invalid:
        with pytest.raises(DomainRejected, match=code):
            HyperliquidTestnetClient._order_response(response, command, "IOC_LIMIT", now=NOW)


def test_rejected_create_races_recover_by_cloid_for_order_and_protection() -> None:
    venue = ContractVenue(fill_orders=False)
    original = venue.__call__
    rejected: set[str] = set()

    def reject_ack_once(
        url: str, payload: dict[str, Any], timeout: float
    ) -> dict[str, Any] | list[Any]:
        result = original(url, payload, timeout)
        if url.endswith("/exchange") and payload["action"]["type"] == "order":
            cloid = str(payload["action"]["orders"][0]["c"])
            if cloid not in rejected:
                rejected.add(cloid)
                return {
                    "status": "ok",
                    "response": {
                        "type": "order",
                        "data": {"statuses": [{"error": "duplicate"}]},
                    },
                }
        return result

    testnet = HyperliquidTestnetClient(
        base_url="https://api.hyperliquid-testnet.xyz",
        account_address=ACCOUNT,
        signer=lambda _action, _nonce: SIGNATURE,
        requester=reject_ack_once,
    )
    order = testnet.ensure_order(
        HyperliquidTestnetOrderCommand(
            "BTC",
            "BUY",
            Decimal("0.25"),
            Decimal(61000),
            False,
            "0xffffffffffffffffffffffffffffffff",
        ),
        now=NOW,
    )
    protection = testnet.ensure_protection(
        HyperliquidTestnetProtectionCommand(
            "BTC",
            "SELL",
            Decimal("0.25"),
            Decimal(59000),
            Decimal(58900),
            "0x99999999999999999999999999999999",
        ),
        now=NOW,
    )
    assert order.status == "SENT"
    assert protection.status == "SENT"


def test_cancel_absence_terminal_ack_failure_and_semantic_conflicts() -> None:
    command = HyperliquidTestnetOrderCommand(
        "BTC",
        "BUY",
        Decimal("0.25"),
        Decimal(61000),
        False,
        "0x88888888888888888888888888888888",
    )
    assert fixed_client({"status": "unknownOid"}).cancel_order(command, now=NOW) is None
    assert fixed_client({"status": "unknownOid"}).recover_order(command, now=NOW) is None

    filled = order_status(command, "filled", sz="0")
    terminal = fixed_client(filled).cancel_order(command, now=NOW)
    assert terminal is not None and terminal.status == "FILLED"

    conflict = HyperliquidTestnetOrder(
        "1",
        command.client_order_id,
        "SENT",
        "SELL",
        "IOC_LIMIT",
        command.quantity,
        Decimal(0),
        command.limit_price,
        Decimal(0),
        False,
        False,
        NOW,
    )
    with pytest.raises(DomainRejected, match="IDENTITY_CONFLICT"):
        HyperliquidTestnetClient._validate_order(conflict, command)

    protection_command = HyperliquidTestnetProtectionCommand(
        "BTC",
        "SELL",
        Decimal("0.25"),
        Decimal(59000),
        Decimal(58900),
        "0x77777777777777777777777777777777",
    )
    with pytest.raises(DomainRejected, match="IDENTITY_CONFLICT"):
        HyperliquidTestnetClient._validate_protection(conflict, protection_command)


def test_default_requester_parses_json_and_rejects_invalid_payloads(
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
        hyperliquid_execution.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: Response(b'{"status":"ok"}'),
    )
    assert hyperliquid_execution._default_requester("https://example.invalid/info", {}, 1.0) == {
        "status": "ok"
    }
    monkeypatch.setattr(
        hyperliquid_execution.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            urllib.error.HTTPError(
                "https://example.invalid/info",
                429,
                "rate limited",
                {},
                io.BytesIO(b'{"status":"error"}'),
            )
        ),
    )
    assert hyperliquid_execution._default_requester("https://example.invalid/info", {}, 1.0) == {
        "status": "error"
    }
    monkeypatch.setattr(
        hyperliquid_execution.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: Response(b"not-json"),
    )
    with pytest.raises(DomainRejected, match="RESPONSE_INVALID"):
        hyperliquid_execution._default_requester("https://example.invalid/info", {}, 1.0)
    monkeypatch.setattr(
        hyperliquid_execution.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: Response(b"1"),
    )
    with pytest.raises(DomainRejected, match="RESPONSE_INVALID"):
        hyperliquid_execution._default_requester("https://example.invalid/info", {}, 1.0)


def test_signer_validates_wallet_identity_and_uses_main_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Wallet:
        address = ACCOUNT

    monkeypatch.setattr(
        hyperliquid_execution.Account,
        "from_key",
        lambda _key: (_ for _ in ()).throw(ValueError("invalid")),
    )
    with pytest.raises(ValueError, match="private key is invalid"):
        build_hyperliquid_signer(
            "invalid", api_wallet_address=None, active_pool=None, is_mainnet=True
        )

    monkeypatch.setattr(hyperliquid_execution.Account, "from_key", lambda _key: Wallet())
    with pytest.raises(ValueError, match="address is invalid"):
        build_hyperliquid_signer(
            "configured", api_wallet_address="invalid", active_pool=None, is_mainnet=True
        )
    with pytest.raises(ValueError, match="does not match"):
        build_hyperliquid_signer(
            "configured", api_wallet_address=SUBACCOUNT, active_pool=None, is_mainnet=True
        )

    signed: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        hyperliquid_execution,
        "sign_l1_action",
        lambda *args: signed.append(args) or SIGNATURE,
    )
    signer = build_hyperliquid_signer(
        "configured", api_wallet_address=ACCOUNT, active_pool=None, is_mainnet=True
    )
    assert signer is not None
    assert signer({"type": "cancel"}, 123) == SIGNATURE
    assert signed[0][2] is None
    assert signed[0][3:] == (123, None, True)


def test_live_wrapper_translates_every_core_operation_error() -> None:
    class RejectingCore:
        @staticmethod
        def reject(*_args: object, **_kwargs: object) -> None:
            raise DomainRejected(
                "HYPERLIQUID_TESTNET_REJECTED", "Hyperliquid testnet controlled rejection"
            )

        query_order = reject
        ensure_order = reject
        cancel_order = reject
        recover_order = reject
        ensure_protection = reject
        cancel_protection = reject

    live = HyperliquidLiveClient(
        base_url="https://api.hyperliquid.xyz",
        account_address=ACCOUNT,
        signer=lambda _action, _nonce: SIGNATURE,
    )
    assert live.account_scope == "MAIN_ACCOUNT"
    live._client = RejectingCore()  # type: ignore[assignment]
    cloid = f"0x{'a' * 32}"
    order = HyperliquidTestnetOrderCommand(
        "BTC", "BUY", Decimal("0.001"), Decimal(61000), False, cloid
    )
    protection = HyperliquidTestnetProtectionCommand(
        "BTC",
        "SELL",
        Decimal("0.001"),
        Decimal(59000),
        Decimal(58900),
        cloid,
    )

    operations = (
        lambda: live.query_order("BTC", cloid, expected_order_type="IOC_LIMIT", now=NOW),
        lambda: live.ensure_order(order, now=NOW),
        lambda: live.cancel_order(order, now=NOW),
        lambda: live.recover_order(order, now=NOW),
        lambda: live.ensure_protection(protection, now=NOW),
        lambda: live.cancel_protection(ProtectionCancelCommand("BTC", cloid), now=NOW),
    )
    for operation in operations:
        with pytest.raises(DomainRejected, match="HYPERLIQUID_LIVE_REJECTED"):
            operation()
