from __future__ import annotations

import asyncio
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import trading_control_plane.freqtrade as freqtrade_module
from trading_control_plane.domain import DomainRejected
from trading_control_plane.freqtrade import (
    FreqtradeEntryCommand,
    FreqtradeRpcMessage,
    FreqtradeWorkerClient,
    FreqtradeWorkerSpec,
    freqtrade_pair,
    parse_freqtrade_rpc_message,
    parse_hip3_dexes,
    validate_worker_url,
)

PATCHES = Path(__file__).resolve().parents[2] / "freqtrade" / "patches"
sys.path.insert(0, str(PATCHES))
from portfolio_margin_compat import (  # noqa: E402
    normalize_portfolio_margin_account,
    upgrade_portfolio_margin_algo_request,
)


class _FakeFreqtradeWebSocket:
    def __init__(self, frames: list[str]) -> None:
        self.frames = frames
        self.sent: list[str] = []

    async def __aenter__(self) -> _FakeFreqtradeWebSocket:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def send(self, value: str) -> None:
        self.sent.append(value)

    def __aiter__(self):
        async def values():
            for frame in self.frames:
                yield frame

        return values()


def test_freqtrade_rpc_websocket_uses_official_message_endpoint() -> None:
    observed: dict[str, object] = {}
    socket = _FakeFreqtradeWebSocket(
        ['{"type":"entry","data":{"trade_id":42,"order_id":"order-1"}}']
    )

    def connector(url: str, timeout: float) -> _FakeFreqtradeWebSocket:
        observed.update(url=url, timeout=timeout)
        return socket

    client = FreqtradeWorkerClient(
        FreqtradeWorkerSpec(
            name="binance-rpc",
            venue="BINANCE",
            base_url="http://127.0.0.1:8083",
            username="control-plane",
            password="fixture-password",  # noqa: S106
            ws_token="fixture-rpc-token-0123456789",  # noqa: S106
        ),
        websocket_connector=connector,
    )

    async def collect() -> list[FreqtradeRpcMessage]:
        return [message async for message in client.rpc_messages()]

    messages = asyncio.run(collect())
    assert observed == {
        "url": (
            "ws://127.0.0.1:8083/api/v1/message/ws?"
            "token=fixture-rpc-token-0123456789"
        ),
        "timeout": 5,
    }
    assert len(messages) == 1
    message = messages[0]
    assert message.event_type == "entry"
    assert message.payload == {"trade_id": 42, "order_id": "order-1"}
    assert len(message.idempotency_key) == 64
    assert "fixture-rpc-token" not in repr(client.spec)
    subscription = json.loads(socket.sent[0])
    assert subscription["type"] == "subscribe"
    assert {"entry", "entry_fill", "exit", "exit_fill", "protection_trigger"}.issubset(
        subscription["data"]
    )


def test_freqtrade_rpc_message_contract_fails_closed() -> None:
    with pytest.raises(DomainRejected, match="FREQTRADE_RPC_MESSAGE_INVALID"):
        parse_freqtrade_rpc_message('{"type":"entry","data":[]}')

    client = FreqtradeWorkerClient(
        FreqtradeWorkerSpec(
            name="binance-rpc",
            venue="BINANCE",
            base_url="http://127.0.0.1:8083",
            username="control-plane",
            password="fixture-password",  # noqa: S106
        )
    )
    with pytest.raises(DomainRejected, match="FREQTRADE_RPC_AUTH_NOT_CONFIGURED"):
        client.rpc_websocket_url()


@pytest.mark.parametrize(
    "config_name",
    [
        "config-binance.json",
        "config-hyperliquid.json",
        "config-binance-live-smoke.json",
        "config-hyperliquid-live-smoke.json",
    ],
)
def test_freqtrade_telegram_is_notification_only(config_name: str) -> None:
    config = json.loads((PATCHES.parent / config_name).read_text())
    telegram = config["telegram"]

    assert telegram["enabled"] is False
    assert telegram["authorized_users"] == []
    assert telegram["reload"] is False
    assert telegram["notification_settings"]["entry_fill"] == "on"
    assert telegram["notification_settings"]["exit_fill"] == "on"
    assert telegram["notification_settings"]["protection_trigger"] == "on"


def test_exact_catalog_symbols_map_to_freqtrade_ccxt_pairs() -> None:
    assert freqtrade_pair("BINANCE", "BTCUSDT") == "BTC/USDT:USDT"
    assert freqtrade_pair("BINANCE", "币安人生USDT") == "币安人生/USDT:USDT"
    assert freqtrade_pair("HYPERLIQUID", "BTC") == "BTC/USDC:USDC"
    assert freqtrade_pair("HYPERLIQUID", "xyz:TSLA", hip3_dexes=("xyz",)) == "XYZ-TSLA/USDC:USDC"
    assert freqtrade_pair("OKX", "BTC-USDT-SWAP") == "BTC/USDT:USDT"
    assert freqtrade_pair("BYBIT", "BTCUSDT") == "BTC/USDT:USDT"


@pytest.mark.parametrize(
    ("venue", "symbol"),
    [
        ("OKX", "BTCUSDT"),
        ("OKX", "btc-USDT-SWAP"),
        ("OKX", "BTC-USDC-SWAP"),
        ("BYBIT", "BTC-USDT-SWAP"),
        ("BYBIT", "btcusdt"),
        ("BYBIT", "BTCUSDC"),
    ],
)
def test_okx_bybit_pair_mapping_rejects_non_exact_linear_symbols(venue: str, symbol: str) -> None:
    with pytest.raises(DomainRejected, match="FREQTRADE_INSTRUMENT_UNSUPPORTED"):
        freqtrade_pair(venue, symbol)


def test_portfolio_margin_balance_uses_unified_account_not_negative_um_subwallet() -> None:
    normalized = normalize_portfolio_margin_account(
        {
            "accountEquity": "9.98196712",
            "totalAvailableBalance": "9.98196712",
            "umWalletBalance": "-0.00639699",
        },
        stake_currency="USDT",
    )

    assert normalized == {"USDT": {"free": 9.98196712, "used": 0.0, "total": 9.98196712}}


@pytest.mark.parametrize(
    ("account", "stake_currency"),
    [
        ({"accountEquity": "9", "totalAvailableBalance": "10"}, "USDT"),
        ({"accountEquity": "NaN", "totalAvailableBalance": "1"}, "USDT"),
        ({"accountEquity": "9", "totalAvailableBalance": "8"}, "USDC"),
    ],
)
def test_portfolio_margin_balance_normalization_fails_closed(
    account: dict[str, str], stake_currency: str
) -> None:
    with pytest.raises(ValueError):
        normalize_portfolio_margin_account(account, stake_currency=stake_currency)


def test_portfolio_margin_conditional_request_upgrades_to_algo_contract() -> None:
    upgraded = upgrade_portfolio_margin_algo_request(
        {
            "symbol": "SOLUSDT",
            "side": "SELL",
            "strategyType": "STOP_MARKET",
            "quantity": "0.08",
            "stopPrice": "72.86",
            "newClientStrategyId": "tcp-stop-fixture",
            "reduceOnly": True,
            "workingType": "CONTRACT_PRICE",
        }
    )

    assert upgraded == {
        "algoType": "CONDITIONAL",
        "symbol": "SOLUSDT",
        "side": "SELL",
        "type": "STOP_MARKET",
        "quantity": "0.08",
        "triggerPrice": "72.86",
        "clientAlgoId": "tcp-stop-fixture",
        "reduceOnly": True,
        "workingType": "CONTRACT_PRICE",
    }


def test_portfolio_margin_algo_upgrade_rejects_non_reduce_only_requests() -> None:
    with pytest.raises(ValueError, match="reduce-only"):
        upgrade_portfolio_margin_algo_request(
            {
                "symbol": "SOLUSDT",
                "side": "SELL",
                "strategyType": "STOP_MARKET",
                "quantity": "0.08",
                "stopPrice": "72.86",
                "newClientStrategyId": "tcp-stop-fixture",
                "reduceOnly": False,
            }
        )


def test_hip3_pair_mapping_requires_an_explicit_dex_allowlist() -> None:
    with pytest.raises(DomainRejected, match="FREQTRADE_HIP3_DEX_NOT_ALLOWED"):
        freqtrade_pair("HYPERLIQUID", "xyz:TSLA")
    with pytest.raises(ValueError, match="unique lowercase identifiers"):
        parse_hip3_dexes("xyz,XYZ")


def test_worker_url_is_loopback_or_https_and_never_embeds_credentials() -> None:
    assert validate_worker_url("http://127.0.0.1:8081/") == "http://127.0.0.1:8081"
    assert validate_worker_url("https://workers.internal.example:8443") == (
        "https://workers.internal.example:8443"
    )
    with pytest.raises(ValueError, match="require HTTPS"):
        validate_worker_url("http://workers.internal.example:8080")
    with pytest.raises(ValueError, match="must not embed credentials"):
        validate_worker_url("https://user:secret@workers.internal.example:8443")


def test_worker_probe_verifies_bound_exchange_without_exposing_credentials() -> None:
    calls: list[tuple[str, str, dict[str, str]]] = []

    def fetcher(
        url: str,
        method: str,
        payload: dict[str, Any] | None,
        headers: dict[str, str],
        timeout: float,
    ) -> dict[str, Any]:
        del payload
        assert timeout == 3
        calls.append((url, method, headers))
        if url.endswith("/ping"):
            return {"status": "pong"}
        if url.endswith("/token/login"):
            assert headers["Authorization"].startswith("Basic ")
            return {"access_token": "short-lived-token"}
        assert headers == {"Authorization": "Bearer short-lived-token"}
        if url.endswith("/show_config"):
            return {
                "exchange": "hyperliquid",
                "trading_mode": "futures",
                "dry_run": True,
                "state": "STOPPED",
            }
        if url.endswith("/version"):
            return {"version": "2026.3"}
        if url.endswith("/whitelist"):
            return {"whitelist": ["BTC/USDC:USDC", "XYZ-TSLA/USDC:USDC"]}
        raise AssertionError(url)

    client = FreqtradeWorkerClient(
        FreqtradeWorkerSpec(
            name="hyperliquid-default",
            venue="HYPERLIQUID",
            base_url="http://127.0.0.1:8082",
            username="control-plane",
            password="fixture-password",  # noqa: S106
            hip3_dexes=("xyz",),
        ),
        timeout_seconds=3,
        fetcher=fetcher,
    )

    result = client.probe()

    assert result == {
        "name": "hyperliquid-default",
        "venue": "HYPERLIQUID",
        "backend": "FREQTRADE",
        "status": "READY",
        "exchange": "hyperliquid",
        "trading_mode": "futures",
        "dry_run": True,
        "worker_state": "STOPPED",
        "version": "2026.3",
        "hip3_dexes": ["xyz"],
        "active_pair_count": 2,
        "hip3_pair_count": 1,
        "order_send": False,
    }
    serialized = repr((result, client.spec))
    assert "fixture-password" not in serialized
    assert "short-lived-token" not in serialized


def test_worker_probe_fails_closed_on_scope_mismatch() -> None:
    def fetcher(
        url: str,
        method: str,
        payload: dict[str, Any] | None,
        headers: dict[str, str],
        timeout: float,
    ) -> dict[str, Any]:
        del method, payload, headers, timeout
        if url.endswith("/ping"):
            return {"status": "pong"}
        if url.endswith("/token/login"):
            return {"access_token": "token"}
        if url.endswith("/show_config"):
            return {"exchange": "binance", "trading_mode": "futures", "dry_run": True}
        return {"version": "2026.3"}

    client = FreqtradeWorkerClient(
        FreqtradeWorkerSpec(
            name="hyperliquid-default",
            venue="HYPERLIQUID",
            base_url="http://127.0.0.1:8082",
            username="control-plane",
            password="fixture-password",  # noqa: S106
        ),
        fetcher=fetcher,
    )
    with pytest.raises(DomainRejected, match="FREQTRADE_WORKER_SCOPE_MISMATCH"):
        client.probe()


@pytest.mark.parametrize(("venue", "exchange"), [("OKX", "okx"), ("BYBIT", "bybit")])
def test_worker_probe_accepts_exact_okx_bybit_exchange_scope(venue: str, exchange: str) -> None:
    def fetcher(
        url: str,
        method: str,
        payload: dict[str, Any] | None,
        headers: dict[str, str],
        timeout: float,
    ) -> dict[str, Any]:
        del method, payload, headers, timeout
        if url.endswith("/ping"):
            return {"status": "pong"}
        if url.endswith("/token/login"):
            return {"access_token": "token"}
        if url.endswith("/show_config"):
            return {
                "exchange": exchange,
                "trading_mode": "futures",
                "dry_run": False,
                "force_entry_enable": True,
                "state": "running",
            }
        if url.endswith("/version"):
            return {"version": "2026.3"}
        if url.endswith("/whitelist"):
            return {"whitelist": ["BTC/USDT:USDT"]}
        raise AssertionError(url)

    client = FreqtradeWorkerClient(
        FreqtradeWorkerSpec(
            name=f"{exchange}-account-worker",
            venue=venue,  # type: ignore[arg-type]
            base_url="http://127.0.0.1:8082",
            username="control-plane",
            password="fixture-password",  # noqa: S106
        ),
        fetcher=fetcher,
    )

    result = client.probe(expected_mode="LIVE", required_pair="BTC/USDT:USDT")

    assert result["venue"] == venue
    assert result["exchange"] == exchange
    assert result["order_send"] is True


def test_worker_probe_rejects_live_mode_and_missing_hip3_scope() -> None:
    state = {"dry_run": False, "whitelist": ["XYZ-TSLA/USDC:USDC"]}

    def fetcher(
        url: str,
        method: str,
        payload: dict[str, Any] | None,
        headers: dict[str, str],
        timeout: float,
    ) -> dict[str, Any]:
        del method, payload, headers, timeout
        if url.endswith("/ping"):
            return {"status": "pong"}
        if url.endswith("/token/login"):
            return {"access_token": "token"}
        if url.endswith("/show_config"):
            return {
                "exchange": "hyperliquid",
                "trading_mode": "futures",
                "dry_run": state["dry_run"],
            }
        if url.endswith("/whitelist"):
            return {"whitelist": state["whitelist"]}
        return {"version": "2026.3"}

    client = FreqtradeWorkerClient(
        FreqtradeWorkerSpec(
            name="hyperliquid-default",
            venue="HYPERLIQUID",
            base_url="http://127.0.0.1:8082",
            username="control-plane",
            password="fixture-password",  # noqa: S106
            hip3_dexes=("xyz",),
        ),
        fetcher=fetcher,
    )
    with pytest.raises(DomainRejected, match="FREQTRADE_LIVE_MODE_FORBIDDEN"):
        client.probe()

    state["dry_run"] = True
    state["whitelist"] = ["BTC/USDC:USDC"]
    with pytest.raises(DomainRejected, match="FREQTRADE_HIP3_SCOPE_MISMATCH"):
        client.probe()


def test_live_worker_force_entry_is_idempotent_and_force_exit_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state: dict[str, Any] = {"open": [], "closed": {}, "writes": [], "pending_reads": 0}
    clock = [0.0]
    monkeypatch.setattr(freqtrade_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        freqtrade_module.time,
        "sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )

    def trade_value(*, is_open: bool) -> dict[str, Any]:
        value: dict[str, Any] = {
            "trade_id": 41,
            "pair": "SOL/USDT:USDT",
            "is_short": False,
            "amount": 0.08,
            "stake_amount": 5.8,
            "open_rate": 72.5,
            "close_rate": None if is_open else 72.6,
            "is_open": is_open,
            "enter_tag": "tcp-fixture",
            "leverage": 1,
            "stop_loss_abs": 71.8,
            "stoploss_order_id": "stop-41",
            "open_timestamp": 1_785_841_200_000,
            "close_timestamp": None if is_open else 1_785_841_260_000,
            "orders": [
                {
                    "order_id": "entry-41",
                    "status": "closed",
                    "is_open": False,
                    "ft_order_side": "buy",
                },
                {
                    "order_id": "stop-41",
                    "status": "open" if is_open else "canceled",
                    "is_open": is_open,
                    "ft_order_side": "stoploss",
                },
            ],
        }
        if not is_open:
            value["orders"].append(
                {
                    "order_id": "exit-41",
                    "status": "closed",
                    "is_open": False,
                    "ft_order_side": "sell",
                }
            )
        return value

    def fetcher(
        url: str,
        method: str,
        payload: dict[str, Any] | None,
        headers: dict[str, str],
        timeout: float,
    ) -> dict[str, Any] | list[Any]:
        del headers, timeout
        if url.endswith("/ping"):
            return {"status": "pong"}
        if url.endswith("/token/login"):
            return {"access_token": "token"}
        if url.endswith("/show_config"):
            return {
                "exchange": "binance",
                "trading_mode": "futures",
                "dry_run": False,
                "state": "running",
                "force_entry_enable": True,
            }
        if url.endswith("/version"):
            return {"version": "2026.7"}
        if url.endswith("/whitelist"):
            return {"whitelist": ["SOL/USDT:USDT"]}
        if url.endswith("/status"):
            if state["pending_reads"]:
                state["pending_reads"] -= 1
                pending = trade_value(is_open=True)
                pending.update({"amount": 0, "stake_amount": 0, "open_rate": 0, "orders": []})
                return [pending]
            return state["open"]
        if url.endswith("/forceenter"):
            assert method == "POST"
            assert payload == {
                "pair": "SOL/USDT:USDT",
                "side": "long",
                "ordertype": "market",
                "stakeamount": 5.8,
                "leverage": 1.0,
                "entry_tag": "tcp-fixture",
            }
            state["writes"].append("entry")
            # Simulate a Hyperliquid fill that arrives after the old 5-second
            # HTTP timeout but inside the independent confirmation window.
            state["pending_reads"] = 40
            state["open"] = [trade_value(is_open=True)]
            return {"status": "created"}
        if url.endswith("/forceexit"):
            assert method == "POST"
            assert payload == {"tradeid": "41", "ordertype": "market"}
            state["writes"].append("exit")
            state["open"] = []
            state["closed"]["41"] = trade_value(is_open=False)
            return {"status": "closed"}
        if url.endswith("/trade/41"):
            return state["closed"].get("41", trade_value(is_open=True))
        raise AssertionError(url)

    client = FreqtradeWorkerClient(
        FreqtradeWorkerSpec(
            name="binance-live-smoke",
            venue="BINANCE",
            base_url="http://127.0.0.1:8083",
            username="control-plane",
            password="fixture-password",  # noqa: S106
        ),
        timeout_seconds=3,
        confirmation_timeout_seconds=15,
        fetcher=fetcher,
    )
    command = FreqtradeEntryCommand(
        pair="SOL/USDT:USDT",
        side="long",
        stake_amount=Decimal("5.8"),
        max_quantity=Decimal("0.09"),
        leverage=Decimal(1),
        enter_tag="tcp-fixture",
        client_order_id="tcp-fixture-order",
    )

    opened = client.force_enter(command)
    recovered = client.recover_entry(command)
    replayed = client.force_enter(command)
    closed = client.force_exit(opened.trade_id, pair=command.pair)
    recovered_closed = client.recover_exit(opened.trade_id, pair=command.pair)

    assert opened.amount == Decimal("0.08")
    assert recovered.trade_id == opened.trade_id
    assert replayed.trade_id == opened.trade_id
    assert opened.stoploss_order_id == "stop-41"
    assert opened.entry_order_id == "entry-41"
    assert opened.exit_order_id is None
    assert closed.exit_order_id == "exit-41"
    assert recovered_closed.exit_order_id == "exit-41"
    assert closed.is_open is False
    assert state["writes"] == ["entry", "exit"]


def test_trade_parser_reads_active_stoploss_from_real_status_orders() -> None:
    from trading_control_plane.freqtrade import parse_freqtrade_trade

    trade = parse_freqtrade_trade(
        {
            "trade_id": 41,
            "pair": "SOL/USDT:USDT",
            "is_short": False,
            "amount": 0.08,
            "stake_amount": 5.8,
            "open_rate": 72.5,
            "close_rate": None,
            "is_open": True,
            "enter_tag": "tcp-fixture",
            "leverage": 1,
            "stop_loss_abs": 71.8,
            "open_timestamp": 1_785_841_200_000,
            "orders": [
                {
                    "order_id": "entry-41",
                    "status": "closed",
                    "is_open": False,
                    "ft_order_side": "buy",
                },
                {
                    "order_id": "stop-41",
                    "status": "open",
                    "is_open": True,
                    "ft_order_side": "stoploss",
                },
            ],
        }
    )

    assert trade.stoploss_order_id == "stop-41"
    assert trade.entry_order_id == "entry-41"
    assert trade.exit_order_id is None


def test_live_worker_rejects_trade_above_frozen_quantity() -> None:
    def fetcher(
        url: str,
        method: str,
        payload: dict[str, Any] | None,
        headers: dict[str, str],
        timeout: float,
    ) -> dict[str, Any] | list[Any]:
        del method, payload, headers, timeout
        if url.endswith("/ping"):
            return {"status": "pong"}
        if url.endswith("/token/login"):
            return {"access_token": "token"}
        if url.endswith("/show_config"):
            return {
                "exchange": "binance",
                "trading_mode": "futures",
                "dry_run": False,
                "state": "running",
                "force_entry_enable": True,
            }
        if url.endswith("/version"):
            return {"version": "2026.7"}
        if url.endswith("/whitelist"):
            return {"whitelist": ["SOL/USDT:USDT"]}
        if url.endswith("/status"):
            return [
                {
                    "trade_id": 42,
                    "pair": "SOL/USDT:USDT",
                    "is_short": False,
                    "amount": 0.1,
                    "stake_amount": 7.2,
                    "open_rate": 72,
                    "is_open": True,
                    "enter_tag": "tcp-fixture",
                    "leverage": 1,
                    "stop_loss_abs": 71.2,
                    "stoploss_order_id": "stop-42",
                    "open_timestamp": 1_785_841_200_000,
                }
            ]
        raise AssertionError(url)

    client = FreqtradeWorkerClient(
        FreqtradeWorkerSpec(
            name="binance-live-smoke",
            venue="BINANCE",
            base_url="http://127.0.0.1:8083",
            username="control-plane",
            password="fixture-password",  # noqa: S106
        ),
        fetcher=fetcher,
    )
    with pytest.raises(DomainRejected, match="FREQTRADE_ORDER_IDENTITY_CONFLICT"):
        client.force_enter(
            FreqtradeEntryCommand(
                pair="SOL/USDT:USDT",
                side="long",
                stake_amount=Decimal(6),
                max_quantity=Decimal("0.09"),
                leverage=Decimal(1),
                enter_tag="tcp-fixture",
                client_order_id="tcp-fixture-order",
            )
        )
