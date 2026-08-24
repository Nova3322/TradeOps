from __future__ import annotations

import asyncio
import base64
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from trading_control_plane.adapters.facts import (
    CcxtProFactAdapter,
    ExchangeFactSnapshot,
    FactAdapterConnectionProbe,
    FactAdapterRegistry,
    FactAdapterScope,
    FactStreamSupervisor,
)
from trading_control_plane.binance_errors import BinanceApiRejected, BinanceRequestState
from trading_control_plane.config import Settings
from trading_control_plane.domain import DomainRejected
from trading_control_plane.fact_adapter_api import create_fact_adapter_app
from trading_control_plane.fact_adapter_ingestion import (
    normalize_fact_adapter_catalog,
    normalize_fact_adapter_snapshot,
)
from trading_control_plane.fact_adapter_runtime import (
    FactAdapterRuntime,
    _bootstrap_symbol_provider,
    _persist_runtime_snapshot,
)
from trading_control_plane.service import PreparedRuntimeAccountBinding

_TOKEN = "fact-adapter-contract-token-0123456789"  # noqa: S105
_NOW = datetime(2026, 8, 18, 1, 2, 3, tzinfo=UTC)


class FakeCcxtProExchange:
    def __init__(self) -> None:
        self.id = "binanceusdm"
        self.has = {
            "fetchBalance": True,
            "fetchPositions": True,
            "fetchOpenOrders": True,
            "fetchMyTrades": True,
            "fetchFundingHistory": True,
            "fetchFundingRates": True,
            "fetchStatus": True,
            "fetchTickers": True,
            "watchBalance": True,
            "watchPositions": True,
            "watchOrders": True,
            "watchMyTrades": True,
            "watchTickers": True,
        }
        self.markets: dict[str, Mapping[str, Any]] = {}
        self.closed = False

    async def load_markets(self) -> Mapping[str, Any]:
        self.markets = {
            "BTC/USDT:USDT": {
                "id": "BTCUSDT",
                "active": True,
                "contract": True,
                "swap": True,
                "linear": True,
                "contractSize": 0.001,
                "precision": {"amount": 1, "price": 0.1},
                "limits": {"amount": {"min": 1}, "cost": {"min": 5}},
                "quote": "USDT",
                "settle": "USDT",
            }
        }
        return self.markets

    async def fetch_balance(self) -> Mapping[str, Any]:
        return {
            "free": {"USDT": 90, "UNKNOWN": None},
            "used": {"USDT": 10, "UNKNOWN": None},
            "total": {"USDT": 100, "UNKNOWN": None},
        }

    async def fetch_positions(self) -> list[Mapping[str, Any]]:
        return [
            {
                "symbol": "BTC/USDT:USDT",
                "contracts": 2,
                "side": "short",
                "entryPrice": 61_000,
                "markPrice": 60_000,
                "unrealizedPnl": 2,
                "liquidationPrice": 81_000,
                "marginMode": "cross",
                "timestamp": int(_NOW.timestamp() * 1_000),
            }
        ]

    async def fetch_open_orders(self) -> list[Mapping[str, Any]]:
        return [
            {
                "id": "external-order",
                "symbol": "BTC/USDT:USDT",
                "amount": 3,
                "filled": 1,
                "side": "buy",
                "type": "limit",
                "status": "open",
                "price": 59_000,
                "timestamp": int(_NOW.timestamp() * 1_000),
            }
        ]

    async def fetch_my_trades(
        self,
        symbol: str | None,
        since: int,
        limit: int,
    ) -> list[Mapping[str, Any]]:
        assert symbol in {None, "BTC/USDT:USDT"} and since > 0 and limit == 1_000
        return [
            {
                "id": "external-fill",
                "order": "external-order",
                "symbol": "BTC/USDT:USDT",
                "amount": 1,
                "price": 60_000,
                "side": "buy",
                "fee": {"cost": 0.1, "currency": "USDT"},
                "timestamp": int(_NOW.timestamp() * 1_000),
            }
        ]

    async def fetch_funding_history(
        self,
        symbol: str | None,
        since: int,
        limit: int,
    ) -> list[Mapping[str, Any]]:
        assert symbol is None and since > 0 and limit == 1_000
        return [
            {
                "id": "funding-1",
                "symbol": "BTC/USDT:USDT",
                "amount": -0.25,
                "code": "USDT",
                "timestamp": int(_NOW.timestamp() * 1_000),
            }
        ]

    async def fetch_funding_rates(self, symbols: list[str]) -> Mapping[str, Mapping[str, Any]]:
        return {
            symbols[0]: {
                "symbol": symbols[0],
                "fundingRate": 0.0001,
                "nextFundingTimestamp": int((_NOW + timedelta(hours=8)).timestamp() * 1_000),
                "timestamp": int(_NOW.timestamp() * 1_000),
            }
        }

    async def fetch_status(self) -> Mapping[str, Any]:
        return {"status": "ok", "updated": int(_NOW.timestamp() * 1_000)}

    async def fetch_tickers(self, symbols: list[str]) -> Mapping[str, Mapping[str, Any]]:
        return {
            symbols[0]: {
                "symbol": symbols[0],
                "markPrice": 60_000,
                "timestamp": int(_NOW.timestamp() * 1_000),
            }
        }

    async def watch_balance(self) -> Mapping[str, Any]:
        return await self.fetch_balance()

    async def close(self) -> None:
        self.closed = True


class FakeCcxtRestOnlyExchange(FakeCcxtProExchange):
    def __init__(self) -> None:
        super().__init__()
        for capability in (
            "watchBalance",
            "watchPositions",
            "watchOrders",
            "watchMyTrades",
            "watchTickers",
        ):
            self.has[capability] = False


class DDoSProtection(Exception):
    pass


class FakeCcxtBinanceBannedExchange(FakeCcxtRestOnlyExchange):
    def __init__(self) -> None:
        super().__init__()
        self.last_response_headers = {"Retry-After": "3600"}
        self.private_calls = 0

    def _banned(self) -> None:
        self.private_calls += 1
        raise DDoSProtection('binanceusdm 418 {"code":-1003,"msg":"IP banned until 1787104923000"}')

    async def fetch_balance(self) -> Mapping[str, Any]:
        self._banned()
        raise AssertionError("unreachable")

    async def fetch_positions(self) -> list[Mapping[str, Any]]:
        self._banned()
        raise AssertionError("unreachable")

    async def fetch_open_orders(self) -> list[Mapping[str, Any]]:
        self._banned()
        raise AssertionError("unreachable")


class FakeCcxtCooldownProbeExchange(FakeCcxtRestOnlyExchange):
    def __init__(self) -> None:
        super().__init__()
        self.load_calls = 0
        self.balance_calls = 0
        self.position_calls = 0
        self.order_calls = 0

    async def load_markets(self) -> Mapping[str, Any]:
        self.load_calls += 1
        return await super().load_markets()

    async def fetch_balance(self) -> Mapping[str, Any]:
        self.balance_calls += 1
        return await super().fetch_balance()

    async def fetch_positions(self) -> list[Mapping[str, Any]]:
        self.position_calls += 1
        return await super().fetch_positions()

    async def fetch_open_orders(self) -> list[Mapping[str, Any]]:
        self.order_calls += 1
        return await super().fetch_open_orders()


class FakeCcxtReconnectingExchange(FakeCcxtRestOnlyExchange):
    def __init__(self) -> None:
        super().__init__()
        self.has["watchBalance"] = True
        self.watch_calls = 0
        self.steady_state = asyncio.Event()

    async def watch_balance(self) -> Mapping[str, Any]:
        self.watch_calls += 1
        if self.watch_calls == 1:
            raise OSError("fixture disconnect")
        if self.watch_calls == 2:
            return await self.fetch_balance()
        self.steady_state.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class FakeCcxtReconnectCompensationRetryExchange(FakeCcxtRestOnlyExchange):
    def __init__(self) -> None:
        super().__init__()
        self.has["watchBalance"] = True
        self.balance_calls = 0
        self.watch_calls = 0

    async def fetch_balance(self) -> Mapping[str, Any]:
        self.balance_calls += 1
        if self.balance_calls == 2:
            raise OSError("fixture reconnect compensation remains rate limited")
        return await super().fetch_balance()

    async def watch_balance(self) -> Mapping[str, Any]:
        self.watch_calls += 1
        if self.watch_calls == 1:
            raise OSError("fixture disconnect")
        if self.watch_calls in {2, 4}:
            return await FakeCcxtRestOnlyExchange.fetch_balance(self)
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class FakeCcxtPeriodicFailureExchange(FakeCcxtRestOnlyExchange):
    def __init__(self) -> None:
        super().__init__()
        self.balance_calls = 0
        self.fail_periodic = True

    async def fetch_balance(self) -> Mapping[str, Any]:
        self.balance_calls += 1
        if self.fail_periodic and self.balance_calls == 2:
            raise OSError("fixture periodic reconciliation failure")
        return await super().fetch_balance()


class FakeCcxtBlockingSnapshotExchange(FakeCcxtRestOnlyExchange):
    def __init__(self, *, fail: bool = False) -> None:
        super().__init__()
        self.fail = fail
        self.balance_calls = 0
        self.snapshot_started = asyncio.Event()
        self.snapshot_release = asyncio.Event()

    async def fetch_balance(self) -> Mapping[str, Any]:
        self.balance_calls += 1
        self.snapshot_started.set()
        await self.snapshot_release.wait()
        if self.fail:
            raise OSError("fixture snapshot failure")
        return await super().fetch_balance()


class FakeCcxtFailedReconnectExchange(FakeCcxtRestOnlyExchange):
    def __init__(self) -> None:
        super().__init__()
        self.has["watchBalance"] = True
        self.balance_calls = 0

    async def fetch_balance(self) -> Mapping[str, Any]:
        self.balance_calls += 1
        return await super().fetch_balance()

    async def watch_balance(self) -> Mapping[str, Any]:
        raise OSError("fixture reconnect remains unavailable")


class FakeCcxtAccountWideExchange(FakeCcxtProExchange):
    async def load_markets(self) -> Mapping[str, Any]:
        markets = dict(await super().load_markets())
        markets["ETH/USDT:USDT"] = {
            **markets["BTC/USDT:USDT"],
            "id": "ETHUSDT",
        }
        self.markets = markets
        return markets

    async def fetch_positions(self) -> list[Mapping[str, Any]]:
        positions = list(await super().fetch_positions())
        positions.append(
            {
                "symbol": "ETH/USDT:USDT",
                "contracts": 1,
                "side": "long",
                "entryPrice": 3_000,
                "markPrice": 3_100,
                "unrealizedPnl": 100,
                "liquidationPrice": 2_000,
                "marginMode": "cross",
                "timestamp": int(_NOW.timestamp() * 1_000),
            }
        )
        return positions

    async def fetch_tickers(self, symbols: list[str]) -> Mapping[str, Mapping[str, Any]]:
        return {
            symbol: {
                "symbol": symbol,
                "markPrice": 60_000 if symbol.startswith("BTC") else 3_100,
                "timestamp": int(_NOW.timestamp() * 1_000),
            }
            for symbol in symbols
        }


class FakeCcxtFullCatalogExchange(FakeCcxtProExchange):
    async def load_markets(self) -> Mapping[str, Any]:
        markets = dict(await super().load_markets())
        markets["ETH/USDT:USDT"] = {
            **markets["BTC/USDT:USDT"],
            "id": "ETHUSDT",
        }
        self.markets = markets
        return markets


class FakeCcxtBinanceSymbolTradesExchange(FakeCcxtAccountWideExchange):
    def __init__(self) -> None:
        super().__init__()
        self.trade_symbols: list[str | None] = []
        self.trade_since: list[int] = []

    async def fetch_my_trades(
        self,
        symbol: str | None,
        since: int,
        limit: int,
    ) -> list[Mapping[str, Any]]:
        self.trade_symbols.append(symbol)
        self.trade_since.append(since)
        assert symbol is not None and since > 0 and limit == 1_000
        return [
            {
                "id": f"fill-{symbol}",
                "order": f"order-{symbol}",
                "symbol": symbol,
                "amount": 1,
                "price": 60_000,
                "side": "buy",
                "fee": {"cost": 0.1, "currency": "USDT"},
                "timestamp": int(_NOW.timestamp() * 1_000),
            }
        ]


def _scope(venue: str = "BINANCE", *, account_id: str = "account-a") -> FactAdapterScope:
    return FactAdapterScope(
        workspace_id="workspace-a",
        team_id="team-a",
        account_id=account_id,
        venue=venue,  # type: ignore[arg-type]
        environment="TESTNET",
        symbols=("BTC/USDT:USDT",),
        account_mode="PORTFOLIO_MARGIN" if venue == "BINANCE" else "STANDARD",
    )


def _credentials(venue: str) -> Mapping[str, str]:
    if venue == "HYPERLIQUID":
        return {"account_address": "0x0000000000000000000000000000000000000001"}
    if venue == "OKX":
        return {"api_key": "key", "api_secret": "secret", "passphrase": "pass"}
    return {"api_key": "key", "api_secret": "secret"}


@pytest.mark.parametrize("venue", ["BINANCE", "HYPERLIQUID", "OKX", "BYBIT"])
def test_ccxt_pro_fact_contract_normalizes_all_supported_venues(venue: str) -> None:
    exchange = FakeCcxtProExchange()
    observed: dict[str, object] = {}

    def factory(
        scope: FactAdapterScope,
        credentials: Mapping[str, str],
        options: Mapping[str, Any],
    ) -> FakeCcxtProExchange:
        observed.update(scope=scope, credentials=dict(credentials), options=dict(options))
        return exchange

    adapter = CcxtProFactAdapter(
        _scope(venue),
        credentials=_credentials(venue),
        exchange_factory=factory,
        clock=lambda: _NOW,
    )
    snapshot = asyncio.run(adapter.snapshot(reason="INITIAL"))

    assert snapshot.data_status == "CURRENT"
    assert snapshot.positions[0]["quantity"] == "-2"
    assert snapshot.positions[0]["unrealized_pnl"] == "2"
    assert snapshot.orders[0]["order_id"] == "external-order"
    assert snapshot.fills[0]["fill_id"] == "external-fill"
    assert snapshot.marks[0]["mark_price"] == "60000"
    assert {row["kind"] for row in snapshot.funding} == {"PAYMENT", "RATE"}
    assert snapshot.account_status == {
        "status": "ok",
        "updated": int(_NOW.timestamp() * 1_000),
    }
    assert snapshot.unknown_fields == ()
    assert set(snapshot.metrics.rest_requests) == {
        "fetchBalance",
        "fetchFundingHistory",
        "fetchFundingRates",
        "fetchMyTrades",
        "fetchOpenOrders",
        "fetchPositions",
        "fetchStatus",
        "fetchTickers",
        "loadMarkets",
    }
    assert set(adapter.watch_channels) == {"BALANCE", "POSITION", "ORDER", "FILL", "MARK"}
    assert "api_wallet_private_key" not in observed["credentials"]  # type: ignore[operator]
    if venue == "BINANCE":
        assert observed["options"] == {
            "defaultType": "swap",
            "adjustForTimeDifference": True,
            "fetchCurrencies": False,
            "fetchOpenOrders": {"warnWithoutSymbol": False},
            "papi": True,
            "portfolioMargin": True,
        }
    elif venue == "HYPERLIQUID":
        assert observed["options"] == {
            "defaultType": "swap",
            "fetchMarkets": {"types": ["swap"], "hip3": {"dexes": []}},
        }

    normalized = normalize_fact_adapter_snapshot(snapshot)
    assert normalized[0].symbol == "BTCUSDT"
    assert normalized[0].position.quantity == Decimal("-2")
    assert normalized[0].orders[0].status == "SENT"
    assert normalized[0].fills[0].fill_id == "external-fill"
    assert normalized[0].equity.equity == Decimal(100)

    triggered_protection = normalize_fact_adapter_snapshot(
        replace(
            snapshot,
            orders=(
                {
                    **snapshot.orders[0],
                    "type": "limit",
                    "reduce_only": True,
                    "trigger_price": "59000",
                },
            ),
        )
    )
    assert triggered_protection[0].orders[0].order_type == "STOPLOSS"
    assert triggered_protection[0].orders[0].stop_price == Decimal("59000")

    incomplete_history = normalize_fact_adapter_snapshot(
        replace(snapshot, unknown_fields=("fetchMyTrades",))
    )
    assert incomplete_history[0].fills == ()
    assert incomplete_history[0].funding == ()
    assert incomplete_history[0].history_error_code == "FACT_ADAPTER_HISTORY_INCOMPLETE"


def test_fact_adapter_rejects_trading_signing_material() -> None:
    with pytest.raises(DomainRejected, match="FACT_ADAPTER_CREDENTIAL_SCOPE_INVALID"):
        CcxtProFactAdapter(
            _scope("HYPERLIQUID"),
            credentials={
                "account_address": "0x0000000000000000000000000000000000000001",
                "api_wallet_private_key": "must-not-enter-fact-adapter",
            },
            exchange_factory=lambda *_args: FakeCcxtProExchange(),
        )


def test_binance_418_enters_shared_cooldown_and_suppresses_all_followup_reads() -> None:
    async def scenario() -> None:
        started = datetime.now(UTC)
        probe_state = BinanceRequestState()
        probe_exchange = FakeCcxtBinanceBannedExchange()
        probe = FactAdapterConnectionProbe(
            bootstrap_symbols={"BINANCE": "BTCUSDT"},
            exchange_factory=lambda *_args: probe_exchange,
            binance_request_state=probe_state,
        )
        probe_result = await probe._verify(
            workspace_id="workspace-a",
            team_id="team-a",
            account_id="account-probe",
            venue="BINANCE",
            environment="LIVE",
            account_mode="STANDARD",
            credentials=_credentials("BINANCE"),
        )
        assert probe_result.success is False
        assert probe_result.error_code == "BINANCE_RATE_LIMITED"
        assert probe_result.diagnostics is not None
        assert probe_result.diagnostics["http_status"] == 418

        request_state = BinanceRequestState()
        banned_exchange = FakeCcxtBinanceBannedExchange()
        adapter = CcxtProFactAdapter(
            _scope(),
            credentials=_credentials("BINANCE"),
            exchange_factory=lambda *_args: banned_exchange,
            binance_request_state=request_state,
            clock=lambda: started,
        )
        with pytest.raises(DomainRejected) as first:
            await adapter.snapshot(reason="INITIAL")
        assert first.value.code == "FACT_ADAPTER_SNAPSHOT_UNAVAILABLE"
        cause: BaseException | None = first.value
        causes: list[BaseException] = []
        while cause is not None and cause not in causes:
            causes.append(cause)
            cause = cause.__cause__ or cause.__context__
        assert any(
            isinstance(item, BinanceApiRejected) and item.code == "BINANCE_RATE_LIMITED"
            for item in causes
        )
        diagnostic = request_state.current_diagnostic()
        assert diagnostic is not None
        assert diagnostic.category == "IP_TEMPORARILY_BANNED"
        assert diagnostic.http_status == 418
        assert diagnostic.next_retry_at == started + timedelta(hours=1)
        assert adapter.metrics.rate_limit_418 >= 1
        await adapter.close()

        cooldown_exchange = FakeCcxtCooldownProbeExchange()
        blocked_adapter = CcxtProFactAdapter(
            _scope(account_id="account-b"),
            credentials=_credentials("BINANCE"),
            exchange_factory=lambda *_args: cooldown_exchange,
            binance_request_state=request_state,
            clock=lambda: started + timedelta(minutes=1),
        )
        with pytest.raises(BinanceApiRejected) as blocked:
            await blocked_adapter.snapshot(reason="INITIAL")
        assert blocked.value.code == "BINANCE_RATE_LIMITED_COOLDOWN"
        assert cooldown_exchange.load_calls == 0
        assert blocked_adapter.metrics.cooldown_suppressions == 1
        await blocked_adapter.close()

        supervised_exchange = FakeCcxtCooldownProbeExchange()
        supervised_adapter = CcxtProFactAdapter(
            _scope(account_id="account-c"),
            credentials=_credentials("BINANCE"),
            exchange_factory=lambda *_args: supervised_exchange,
            binance_request_state=request_state,
            clock=lambda: started + timedelta(minutes=1),
        )
        registry = FactAdapterRegistry()
        await registry.register(supervised_adapter)
        delays: list[float] = []
        supervisor: FactStreamSupervisor

        async def sleeper(delay: float) -> None:
            delays.append(delay)
            supervisor.stop()

        supervisor = FactStreamSupervisor(
            registry,
            supervised_adapter,
            sleeper=sleeper,
        )
        await supervisor.run()
        assert len(delays) == 1
        assert 3_500 <= delays[0] <= 3_600
        assert supervised_adapter.metrics.websocket_reconnects == 0
        assert supervised_exchange.load_calls == 0
        await registry.close()

        periodic_exchange = FakeCcxtCooldownProbeExchange()
        periodic_adapter = CcxtProFactAdapter(
            _scope(account_id="account-periodic"),
            credentials=_credentials("BINANCE"),
            exchange_factory=lambda *_args: periodic_exchange,
            clock=lambda: started + timedelta(minutes=1),
        )
        periodic_registry = FactAdapterRegistry()
        await periodic_registry.register(periodic_adapter)
        await periodic_registry.publish_snapshot(await periodic_adapter.snapshot(reason="INITIAL"))
        periodic_adapter._binance_request_state = request_state
        periodic_supervisor = FactStreamSupervisor(
            periodic_registry,
            periodic_adapter,
        )
        await periodic_supervisor._run_periodic_reconciliation()
        periodic = await periodic_registry.latest(
            periodic_adapter.scope.key,
            stale_after=timedelta(days=1),
        )
        assert periodic.data_status == "UNKNOWN"
        assert periodic.unknown_fields == ("FACT_ADAPTER_BINANCE_RATE_LIMITED_COOLDOWN",)
        assert periodic.metrics.next_retry_at == diagnostic.next_retry_at.isoformat()
        assert periodic_exchange.load_calls == 1
        await periodic_registry.close()

        probe = FactAdapterConnectionProbe(
            bootstrap_symbols={"BINANCE": "BTCUSDT"},
            exchange_factory=lambda *_args: cooldown_exchange,
            binance_request_state=request_state,
        )
        result = await probe._verify(
            workspace_id="workspace-a",
            team_id="team-a",
            account_id="account-d",
            venue="BINANCE",
            environment="LIVE",
            account_mode="STANDARD",
            credentials=_credentials("BINANCE"),
        )
        assert result.success is False
        assert result.error_code == "BINANCE_RATE_LIMITED_COOLDOWN"
        assert cooldown_exchange.load_calls == 0

        recovery_exchange = FakeCcxtCooldownProbeExchange()
        recovery_adapter = CcxtProFactAdapter(
            _scope(account_id="account-recovery"),
            credentials=_credentials("BINANCE"),
            exchange_factory=lambda *_args: recovery_exchange,
            binance_request_state=request_state,
            clock=lambda: started + timedelta(hours=1, seconds=1),
        )
        recovered = await recovery_adapter.snapshot(reason="INITIAL")
        assert recovered.data_status == "CURRENT"
        assert recovery_exchange.load_calls == 1
        assert recovery_exchange.balance_calls == 1
        assert recovery_exchange.position_calls == 1
        assert recovery_exchange.order_calls == 1
        assert request_state.current_diagnostic() is None
        await recovery_adapter.close()

    asyncio.run(scenario())


def test_fact_adapter_derives_stable_ids_for_zero_hash_funding_payments() -> None:
    class ZeroHashFundingExchange(FakeCcxtProExchange):
        def __init__(self) -> None:
            super().__init__()
            self.id = "hyperliquid"

        async def fetch_funding_history(
            self,
            symbol: str | None,
            since: int,
            limit: int,
        ) -> list[Mapping[str, Any]]:
            assert symbol is None and since > 0 and limit == 1_000
            return [
                {
                    "id": "0x" + ("0" * 64),
                    "symbol": "BTC/USDT:USDT",
                    "amount": amount,
                    "code": "USDT",
                    "timestamp": int((_NOW + offset).timestamp() * 1_000),
                }
                for amount, offset in (
                    (-0.25, timedelta(hours=1)),
                    (-0.5, timedelta(hours=2)),
                )
            ]

    exchange = ZeroHashFundingExchange()
    adapter = CcxtProFactAdapter(
        _scope("HYPERLIQUID"),
        credentials=_credentials("HYPERLIQUID"),
        exchange_factory=lambda *_args: exchange,
        clock=lambda: _NOW,
    )

    first = asyncio.run(adapter.snapshot(reason="INITIAL"))
    second = asyncio.run(adapter.snapshot(reason="PERIODIC_RECONCILIATION"))
    first_ids = [row["payment_id"] for row in first.funding if row["kind"] == "PAYMENT"]
    second_ids = [row["payment_id"] for row in second.funding if row["kind"] == "PAYMENT"]

    assert len(set(first_ids)) == 2
    assert all(payment_id.startswith("derived:") for payment_id in first_ids)
    assert second_ids == first_ids


def test_one_shot_fact_probe_uses_exact_scope_and_always_closes() -> None:
    exchange = FakeCcxtCooldownProbeExchange()
    observed: dict[str, object] = {}

    def factory(
        scope: FactAdapterScope,
        credentials: Mapping[str, str],
        options: Mapping[str, Any],
    ) -> FakeCcxtProExchange:
        observed.update(scope=scope, credentials=dict(credentials), options=dict(options))
        return exchange

    probe = FactAdapterConnectionProbe(
        bootstrap_symbols={"BINANCE": "BTCUSDT"},
        exchange_factory=factory,
    )
    result = probe.verify(
        workspace_id="workspace-a",
        team_id="team-a",
        account_id="account-a",
        venue="BINANCE",
        environment="TESTNET",
        account_mode="PORTFOLIO_MARGIN",
        credentials={"api_key": "key", "api_secret": "secret"},
        now=_NOW,
    )

    assert result.success is True
    assert exchange.closed is True
    assert exchange.load_calls == 0
    assert exchange.balance_calls == 1
    assert exchange.position_calls == 0
    assert exchange.order_calls == 0
    assert observed["scope"] == _scope("BINANCE")
    assert observed["options"] == {
        "defaultType": "swap",
        "adjustForTimeDifference": True,
        "fetchCurrencies": False,
        "fetchOpenOrders": {"warnWithoutSymbol": False},
        "papi": True,
        "portfolioMargin": True,
    }
    assert "secret" not in repr(result)


def test_fact_runtime_starts_from_one_bootstrap_symbol_per_venue() -> None:
    provider = _bootstrap_symbol_provider(
        Settings(database_url="postgresql+psycopg://test:test@localhost/test", _env_file=None)
    )

    assert provider("BINANCE") == ("BTCUSDT",)
    assert provider("HYPERLIQUID") == ("BTC",)


def test_websocket_increment_rejects_unknown_quantity_instead_of_zeroing_it() -> None:
    class IncompletePositionExchange(FakeCcxtProExchange):
        async def watch_positions(self) -> list[Mapping[str, Any]]:
            return [
                {
                    "symbol": "BTC/USDT:USDT",
                    "side": "long",
                    "timestamp": int(_NOW.timestamp() * 1_000),
                }
            ]

    async def scenario() -> None:
        exchange = IncompletePositionExchange()
        adapter = CcxtProFactAdapter(
            _scope(),
            credentials=_credentials("BINANCE"),
            exchange_factory=lambda *_args: exchange,
            clock=lambda: _NOW,
        )
        await adapter.snapshot(reason="INITIAL")
        with pytest.raises(DomainRejected, match="FACT_ADAPTER_RESPONSE_INCOMPLETE"):
            await adapter.watch("POSITION")
        await adapter.close()

    asyncio.run(scenario())


def test_hyperliquid_fact_adapter_loads_only_configured_hip3_dexes() -> None:
    observed: dict[str, Any] = {}
    scope = replace(
        _scope("HYPERLIQUID"),
        symbols=("BTC/USDC:USDC", "XYZ-TSLA/USDC:USDC"),
        hip3_dexes=("xyz",),
    )

    CcxtProFactAdapter(
        scope,
        credentials=_credentials("HYPERLIQUID"),
        exchange_factory=lambda _scope, _credentials, options: (
            observed.update(options=dict(options)) or FakeCcxtProExchange()
        ),
    )

    assert observed["options"] == {
        "defaultType": "swap",
        "fetchMarkets": {"types": ["swap", "hip3"], "hip3": {"dexes": ["xyz"]}},
    }


def test_runtime_rotates_hyperliquid_connection_when_hip3_scope_changes() -> None:
    async def scenario() -> None:
        binding = PreparedRuntimeAccountBinding(
            exchange_account_id=UUID("00000000-0000-0000-0000-000000000011"),
            workspace_id=UUID("00000000-0000-0000-0000-000000000012"),
            team_id=UUID("00000000-0000-0000-0000-000000000013"),
            service_principal_id=UUID("00000000-0000-0000-0000-000000000014"),
            service_principal_username="runtime-sync",
            account_id="hyperliquid-account",
            venue="HYPERLIQUID",
            environment="LIVE",
            account_version=1,
            credential_version=1,
            credentials={"account_address": "0x0000000000000000000000000000000000000001"},
            hip3_dexes=("xyz",),
        )
        bindings = [binding]
        exchanges: list[FakeCcxtRestOnlyExchange] = []
        options: list[dict[str, Any]] = []

        def factory(
            _scope: FactAdapterScope,
            _credentials: Mapping[str, str],
            configured: Mapping[str, Any],
        ) -> FakeCcxtRestOnlyExchange:
            exchange = FakeCcxtRestOnlyExchange()
            exchanges.append(exchange)
            options.append(dict(configured))
            return exchange

        settings = Settings(
            environment="test",
            database_url="postgresql+psycopg://user:pass@localhost/trading",
            runtime_sync_enabled=True,
            credential_encryption_key=base64.urlsafe_b64encode(b"a" * 32).decode(),
            fact_adapter_enabled=True,
            fact_adapter_bearer_token=_TOKEN,
            _env_file=None,  # type: ignore[call-arg]
        )
        registry = FactAdapterRegistry()
        runtime = FactAdapterRuntime(
            settings=settings,
            registry=registry,
            binding_provider=lambda: tuple(bindings),
            symbol_provider=lambda _venue: ("BTC",),
            exchange_factory=factory,
        )

        await runtime.reconcile_once()
        assert options[0]["fetchMarkets"] == {
            "types": ["swap", "hip3"],
            "hip3": {"dexes": ["xyz"]},
        }

        bindings[0] = replace(binding, account_version=2, hip3_dexes=())
        await runtime.reconcile_once()
        assert exchanges[0].closed is True
        assert len(exchanges) == 2
        assert options[1]["fetchMarkets"] == {
            "types": ["swap"],
            "hip3": {"dexes": []},
        }
        await runtime.close()

    asyncio.run(scenario())


def test_hyperliquid_mark_and_native_identity_use_exchange_contract() -> None:
    adapter = CcxtProFactAdapter(
        _scope("HYPERLIQUID"),
        credentials=_credentials("HYPERLIQUID"),
        exchange_factory=lambda *_args: FakeCcxtProExchange(),
        clock=lambda: _NOW,
    )
    pair = "BTC/USDC:USDC"
    markets = {
        pair: {
            "id": "0",
            "info": {"name": "BTC"},
        }
    }

    marks = adapter._marks(
        {
            pair: {
                "symbol": pair,
                "markPrice": None,
                "info": {"markPx": "60001.5"},
                "timestamp": int(_NOW.timestamp() * 1_000),
            }
        },
        markets,
        _NOW,
        (pair,),
    )

    assert marks == (
        {
            "symbol": pair,
            "native_symbol": "BTC",
            "mark_price": "60001.5",
            "observed_at": _NOW.isoformat(),
        },
    )


def test_mark_uses_funding_rate_fallback_and_ignores_unconfirmed_ticker() -> None:
    adapter = CcxtProFactAdapter(
        _scope(),
        credentials=_credentials("BINANCE"),
        exchange_factory=lambda *_args: FakeCcxtProExchange(),
        clock=lambda: _NOW,
    )
    pair = "BTC/USDT:USDT"
    markets = {pair: {"id": "BTCUSDT"}}

    marks = adapter._marks(
        {pair: {"symbol": pair, "markPrice": None}},
        markets,
        _NOW,
        (pair,),
        fallback={pair: {"markPrice": "60002", "timestamp": int(_NOW.timestamp() * 1_000)}},
    )
    missing = adapter._marks(
        {pair: {"symbol": pair, "markPrice": None}},
        markets,
        _NOW,
        (pair,),
    )

    assert marks[0]["mark_price"] == "60002"
    assert marks[0]["native_symbol"] == "BTCUSDT"
    assert missing == ()


def test_initial_fact_snapshot_failure_retries_before_starting_stream() -> None:
    class InitialFailureExchange(FakeCcxtRestOnlyExchange):
        def __init__(self) -> None:
            super().__init__()
            self.balance_calls = 0

        async def fetch_balance(self) -> Mapping[str, Any]:
            self.balance_calls += 1
            if self.balance_calls == 1:
                raise OSError("fixture initial snapshot failure")
            return await super().fetch_balance()

    async def scenario() -> None:
        exchange = InitialFailureExchange()
        adapter = CcxtProFactAdapter(
            _scope(),
            credentials=_credentials("BINANCE"),
            exchange_factory=lambda *_args: exchange,
            clock=lambda: _NOW,
        )
        registry = FactAdapterRegistry()
        await registry.register(adapter)
        delays: list[float] = []
        supervisor: FactStreamSupervisor

        async def sleeper(delay: float) -> None:
            delays.append(delay)

        async def callback(_snapshot: ExchangeFactSnapshot) -> None:
            supervisor.stop()

        supervisor = FactStreamSupervisor(
            registry,
            adapter,
            reconnect_initial_seconds=0.1,
            reconnect_max_seconds=1,
            max_reconnect_attempts=2,
            sleeper=sleeper,
            snapshot_callback=callback,
            persistence_coalesce_seconds=0,
        )
        await supervisor.run()

        current = await registry.latest(adapter.scope.key, stale_after=timedelta(days=365))
        assert current.data_status == "CURRENT"
        assert exchange.balance_calls == 2
        assert delays == [0.1]
        assert adapter.metrics.snapshot_failed == 1
        assert adapter.metrics.snapshot_completed == 1
        await registry.close()

    asyncio.run(scenario())


def test_required_snapshot_failure_cancels_sibling_reads() -> None:
    class RequiredReadFailureExchange(FakeCcxtRestOnlyExchange):
        def __init__(self) -> None:
            super().__init__()
            self.positions_cancelled = False
            self.orders_cancelled = False

        async def fetch_balance(self) -> Mapping[str, Any]:
            raise OSError("fixture required read failure")

        async def fetch_positions(self) -> list[Mapping[str, Any]]:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.positions_cancelled = True
                raise
            raise AssertionError("unreachable")

        async def fetch_open_orders(self) -> list[Mapping[str, Any]]:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.orders_cancelled = True
                raise
            raise AssertionError("unreachable")

    async def scenario() -> None:
        exchange = RequiredReadFailureExchange()
        adapter = CcxtProFactAdapter(
            _scope(),
            credentials=_credentials("BINANCE"),
            exchange_factory=lambda *_args: exchange,
            clock=lambda: _NOW,
        )

        with pytest.raises(DomainRejected, match="FACT_ADAPTER_SNAPSHOT_UNAVAILABLE"):
            await adapter.snapshot(reason="INITIAL")

        assert exchange.positions_cancelled is True
        assert exchange.orders_cancelled is True
        await adapter.close()

    asyncio.run(scenario())


def test_fact_snapshot_includes_non_freqtrade_account_positions() -> None:
    exchange = FakeCcxtAccountWideExchange()
    adapter = CcxtProFactAdapter(
        _scope(),
        credentials=_credentials("BINANCE"),
        exchange_factory=lambda *_args: exchange,
        clock=lambda: _NOW,
    )

    snapshot = asyncio.run(adapter.snapshot(reason="INITIAL"))

    assert snapshot.scope.symbols == ("BTC/USDT:USDT",)
    assert {row["native_symbol"] for row in snapshot.instruments} == {
        "BTCUSDT",
        "ETHUSDT",
    }
    assert {row["native_symbol"] for row in snapshot.positions} == {
        "BTCUSDT",
        "ETHUSDT",
    }
    normalized = normalize_fact_adapter_snapshot(snapshot)
    assert {item.symbol for item in normalized} == {"BTCUSDT", "ETHUSDT"}


def test_binance_trade_history_queries_every_reconciled_symbol() -> None:
    exchange = FakeCcxtBinanceSymbolTradesExchange()
    adapter = CcxtProFactAdapter(
        _scope(),
        credentials=_credentials("BINANCE"),
        exchange_factory=lambda *_args: exchange,
        clock=lambda: _NOW,
    )

    snapshot = asyncio.run(adapter.snapshot(reason="INITIAL"))

    assert exchange.trade_symbols == ["BTC/USDT:USDT", "ETH/USDT:USDT"]
    assert exchange.trade_since == [int((_NOW - timedelta(days=7)).timestamp() * 1_000)] * 2
    assert {row["native_symbol"] for row in snapshot.fills} == {"BTCUSDT", "ETHUSDT"}
    assert "fetchMyTrades" not in snapshot.unknown_fields


def test_fact_snapshot_drops_delisted_catalog_subscription() -> None:
    scope = replace(
        _scope(),
        symbols=("BTC/USDT:USDT", "DELISTED/USDT:USDT"),
    )
    adapter = CcxtProFactAdapter(
        scope,
        credentials=_credentials("BINANCE"),
        exchange_factory=lambda *_args: FakeCcxtRestOnlyExchange(),
        clock=lambda: _NOW,
    )

    snapshot = asyncio.run(adapter.snapshot(reason="INITIAL"))

    assert {row["native_symbol"] for row in snapshot.instruments} == {"BTCUSDT"}
    assert adapter._tracked_symbols == {"BTC/USDT:USDT"}


def test_official_catalog_covers_unsubscribed_active_contracts() -> None:
    exchange = FakeCcxtFullCatalogExchange()
    adapter = CcxtProFactAdapter(
        _scope(),
        credentials=_credentials("BINANCE"),
        exchange_factory=lambda *_args: exchange,
        clock=lambda: _NOW,
    )

    snapshot = asyncio.run(adapter.snapshot(reason="INITIAL"))

    assert {row["native_symbol"] for row in snapshot.instruments} == {"BTCUSDT"}
    assert {row["native_symbol"] for row in snapshot.catalog_instruments} == {
        "BTCUSDT",
        "ETHUSDT",
    }
    assert {item.symbol for item in normalize_fact_adapter_catalog(snapshot)} == {
        "BTCUSDT",
        "ETHUSDT",
    }
    assert adapter._tracked_symbols == {"BTC/USDT:USDT"}


def test_registry_deduplicates_adapter_owned_events() -> None:
    async def scenario() -> None:
        adapter = CcxtProFactAdapter(
            _scope(),
            credentials=_credentials("BINANCE"),
            exchange_factory=lambda *_args: FakeCcxtProExchange(),
            clock=lambda: _NOW,
        )
        registry = FactAdapterRegistry()
        await registry.register(adapter)
        snapshot = await adapter.snapshot(reason="INITIAL")
        await registry.publish_snapshot(snapshot)
        payload = {
            "positions": [
                {
                    "symbol": "BTC/USDT:USDT",
                    "native_symbol": "BTCUSDT",
                    "side": "long",
                    "quantity": "1",
                }
            ]
        }
        first = await registry.publish(adapter.scope.key, "POSITION", payload)
        duplicate = await registry.publish(adapter.scope.key, "POSITION", payload)
        assert first is not None
        assert duplicate is None
        await registry.close()

    asyncio.run(scenario())


def test_snapshot_api_and_websocket_are_authenticated_and_scope_isolated() -> None:
    async def prepare() -> FactAdapterRegistry:
        adapter = CcxtProFactAdapter(
            _scope(),
            credentials=_credentials("BINANCE"),
            exchange_factory=lambda *_args: FakeCcxtProExchange(),
            clock=lambda: _NOW,
        )
        registry = FactAdapterRegistry()
        await registry.register(adapter)
        await registry.publish_snapshot(await adapter.snapshot(reason="INITIAL"))
        return registry

    registry = asyncio.run(prepare())
    app = create_fact_adapter_app(registry=registry, bearer_token=_TOKEN)
    client = TestClient(app)
    query = {
        "workspace_id": "workspace-a",
        "team_id": "team-a",
        "account_id": "account-a",
        "venue": "BINANCE",
        "environment": "TESTNET",
    }

    assert client.get("/facts/snapshot", params=query).status_code == 401
    response = client.get(
        "/facts/snapshot",
        params=query,
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )
    assert response.status_code == 200
    assert response.json()["scope"]["account_id"] == "account-a"
    other = {**query, "account_id": "account-b"}
    assert (
        client.get(
            "/facts/snapshot",
            params=other,
            headers={"Authorization": f"Bearer {_TOKEN}"},
        ).json()["error"]["code"]
        == "FACT_SNAPSHOT_UNKNOWN"
    )
    with client:
        with client.websocket_connect(
            "/facts/ws",
            params={**query, "after_sequence": 99, "after_stream_id": "old-stream"},
            headers={"Authorization": f"Bearer {_TOKEN}"},
        ) as websocket:
            event = websocket.receive_json()
            assert event["kind"] == "SNAPSHOT"
            assert event["scope"]["account_id"] == "account-a"
            assert event["resume"] == {
                "status": "STREAM_RESET_COMPENSATED",
                "requested_after_sequence": 99,
                "requested_stream_id": "old-stream",
            }


def test_snapshot_freshness_marks_old_data_stale_without_zeroing_unknowns() -> None:
    async def scenario() -> None:
        adapter = CcxtProFactAdapter(
            _scope(),
            credentials=_credentials("BINANCE"),
            exchange_factory=lambda *_args: FakeCcxtProExchange(),
            clock=lambda: _NOW - timedelta(hours=1),
        )
        registry = FactAdapterRegistry()
        await registry.register(adapter)
        snapshot = await adapter.snapshot(reason="INITIAL")
        await registry.publish_snapshot(snapshot)
        stale = await registry.latest(adapter.scope.key, stale_after=timedelta(seconds=30))
        assert stale.data_status == "STALE"
        assert stale.positions[0]["quantity"] == "-2"
        assert all(row["currency"] != "UNKNOWN" for row in stale.balances)
        await registry.close()

    asyncio.run(scenario())


def test_runtime_reuses_one_connection_and_rotates_on_credential_version() -> None:
    async def scenario() -> None:
        binding = PreparedRuntimeAccountBinding(
            exchange_account_id=UUID("00000000-0000-0000-0000-000000000001"),
            workspace_id=UUID("00000000-0000-0000-0000-000000000002"),
            team_id=UUID("00000000-0000-0000-0000-000000000003"),
            service_principal_id=UUID("00000000-0000-0000-0000-000000000004"),
            service_principal_username="runtime-sync",
            account_id="account-a",
            venue="BINANCE",
            environment="TESTNET",
            account_version=1,
            credential_version=1,
            credentials={"api_key": "key-a", "api_secret": "secret-a"},
        )
        bindings = [binding]
        exchanges: list[FakeCcxtRestOnlyExchange] = []

        def factory(
            _scope: FactAdapterScope,
            _credentials: Mapping[str, str],
            _options: Mapping[str, Any],
        ) -> FakeCcxtRestOnlyExchange:
            exchange = FakeCcxtRestOnlyExchange()
            exchanges.append(exchange)
            return exchange

        settings = Settings(
            environment="test",
            database_url="postgresql+psycopg://user:pass@localhost/trading",
            runtime_sync_enabled=True,
            credential_encryption_key=base64.urlsafe_b64encode(b"a" * 32).decode(),
            fact_adapter_enabled=True,
            fact_adapter_bearer_token=_TOKEN,
            _env_file=None,  # type: ignore[call-arg]
        )
        registry = FactAdapterRegistry()
        runtime = FactAdapterRuntime(
            settings=settings,
            registry=registry,
            binding_provider=lambda: tuple(bindings),
            symbol_provider=lambda venue: ("BTCUSDT",) if venue == "BINANCE" else (),
            exchange_factory=factory,
        )
        await runtime.reconcile_once()
        assert len(await registry.scope_keys()) == 1
        assert len(exchanges) == 1

        await runtime.reconcile_once()
        assert len(exchanges) == 1

        bindings[0] = replace(binding, account_version=2)
        await runtime.reconcile_once()
        assert exchanges[0].closed is False
        assert len(exchanges) == 1

        bindings[0] = replace(
            binding,
            account_version=3,
            credential_version=2,
            credentials={"api_key": "key-b", "api_secret": "secret-b"},
        )
        await runtime.reconcile_once()
        assert exchanges[0].closed is True
        assert len(exchanges) == 2
        assert len(await registry.scope_keys()) == 1
        await runtime.close()
        assert exchanges[1].closed is True

    asyncio.run(scenario())


def test_periodic_runtime_snapshot_persists_then_computes_scope_reconciliation() -> None:
    binding = PreparedRuntimeAccountBinding(
        exchange_account_id=UUID("00000000-0000-0000-0000-000000000001"),
        workspace_id=UUID("00000000-0000-0000-0000-000000000002"),
        team_id=UUID("00000000-0000-0000-0000-000000000003"),
        service_principal_id=UUID("00000000-0000-0000-0000-000000000004"),
        service_principal_username="runtime-sync",
        account_id="account-a",
        venue="BINANCE",
        environment="LIVE",
        account_version=1,
        credential_version=1,
        credentials={"api_key": "key-a", "api_secret": "secret-a"},
    )
    adapter = CcxtProFactAdapter(
        _scope(),
        credentials=_credentials("BINANCE"),
        exchange_factory=lambda *_args: FakeCcxtProExchange(),
        clock=lambda: _NOW,
    )
    initial = asyncio.run(adapter.snapshot(reason="INITIAL"))
    events: list[tuple[str, object]] = []

    class ServiceStub:
        def synchronize_active_venue_instruments(self, *args: object, **kwargs: object):
            events.append(("catalog", (args, kwargs)))

        def ingest_normalized_read_only_account_snapshot(self, *args: object, **kwargs: object):
            events.append(("ingest", (args, kwargs)))

        def reconcile_scope(self, execution_scope: str, actor_id: UUID, *, now: datetime):
            events.append(("reconcile", (execution_scope, actor_id, now)))

        def record_runtime_source_health(self, *args: object, **kwargs: object):
            events.append(("health", (args, kwargs)))

    service = ServiceStub()
    _persist_runtime_snapshot(  # type: ignore[arg-type]
        service,
        binding,
        replace(initial, reason="PERIODIC_RECONCILIATION"),
        now=_NOW,
    )
    assert [event[0] for event in events] == ["catalog", "ingest", "reconcile"]
    assert events[2][1] == (
        "LIVE:account-a:BINANCE",
        binding.service_principal_id,
        _NOW,
    )

    events.clear()
    _persist_runtime_snapshot(  # type: ignore[arg-type]
        service,
        binding,
        replace(initial, reason="WEBSOCKET_INCREMENT"),
        now=_NOW,
    )
    assert [event[0] for event in events] == ["ingest"]


def test_websocket_disconnect_reconnects_then_rest_compensates_before_increment() -> None:
    async def scenario() -> None:
        exchange = FakeCcxtReconnectingExchange()
        adapter = CcxtProFactAdapter(
            _scope(),
            credentials=_credentials("BINANCE"),
            exchange_factory=lambda *_args: exchange,
            clock=lambda: _NOW,
        )
        registry = FactAdapterRegistry()
        await registry.register(adapter)
        callbacks: list[str] = []
        callback_event = asyncio.Event()
        delays: list[float] = []

        async def callback(snapshot: ExchangeFactSnapshot) -> None:
            callbacks.append(snapshot.reason)
            if snapshot.reason == "WEBSOCKET_INCREMENT":
                callback_event.set()

        async def sleeper(delay: float) -> None:
            delays.append(delay)

        supervisor = FactStreamSupervisor(
            registry,
            adapter,
            reconnect_initial_seconds=0.1,
            reconnect_max_seconds=1,
            sleeper=sleeper,
            snapshot_callback=callback,
            persistence_coalesce_seconds=0,
        )
        task = asyncio.create_task(supervisor.run())
        await asyncio.wait_for(exchange.steady_state.wait(), timeout=1)
        await asyncio.wait_for(callback_event.wait(), timeout=1)
        current = await registry.latest(adapter.scope.key, stale_after=timedelta(days=1))
        assert callbacks == [
            "INITIAL",
            "RECONNECT_COMPENSATION",
            "WEBSOCKET_INCREMENT",
        ]
        assert delays == [0.1]
        assert current.reason == "WEBSOCKET_INCREMENT"
        assert current.snapshot_version == 3
        assert current.metrics.rest_compensations == 1
        supervisor.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await registry.close()

    asyncio.run(scenario())


def test_failed_reconnect_compensation_preserves_exponential_backoff() -> None:
    async def scenario() -> None:
        exchange = FakeCcxtReconnectCompensationRetryExchange()
        adapter = CcxtProFactAdapter(
            _scope(),
            credentials=_credentials("BINANCE"),
            exchange_factory=lambda *_args: exchange,
            clock=lambda: _NOW,
        )
        registry = FactAdapterRegistry()
        await registry.register(adapter)
        callbacks: list[str] = []
        recovered = asyncio.Event()
        delays: list[float] = []

        async def callback(snapshot: ExchangeFactSnapshot) -> None:
            callbacks.append(snapshot.reason)
            if snapshot.reason == "RECONNECT_COMPENSATION":
                recovered.set()

        async def sleeper(delay: float) -> None:
            delays.append(delay)

        supervisor = FactStreamSupervisor(
            registry,
            adapter,
            reconnect_initial_seconds=0.1,
            reconnect_max_seconds=1,
            sleeper=sleeper,
            snapshot_callback=callback,
            persistence_coalesce_seconds=0,
        )
        task = asyncio.create_task(supervisor.run())
        await asyncio.wait_for(recovered.wait(), timeout=1)
        current = await registry.latest(adapter.scope.key, stale_after=timedelta(days=365))
        assert callbacks == [
            "INITIAL",
            "RECONNECT_COMPENSATION",
        ]
        assert delays == [0.1, 0.2]
        assert task.done() is False
        assert current.data_status == "CURRENT"
        assert current.metrics.snapshot_failed == 1
        assert current.metrics.websocket_reconnects == 2
        assert current.metrics.rest_compensations == 1
        supervisor.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await registry.close()

    asyncio.run(scenario())


def test_failed_periodic_reconciliation_keeps_stream_state_unknown_without_reconnect() -> None:
    async def scenario() -> None:
        exchange = FakeCcxtPeriodicFailureExchange()
        adapter = CcxtProFactAdapter(
            _scope(),
            credentials=_credentials("BINANCE"),
            exchange_factory=lambda *_args: exchange,
            clock=lambda: datetime.now(UTC),
        )
        registry = FactAdapterRegistry()
        await registry.register(adapter)
        persisted: list[ExchangeFactSnapshot] = []

        async def callback(snapshot: ExchangeFactSnapshot) -> None:
            persisted.append(snapshot)

        supervisor = FactStreamSupervisor(
            registry,
            adapter,
            snapshot_callback=callback,
            persistence_coalesce_seconds=0,
        )
        await supervisor.refresh("INITIAL")
        await supervisor._run_periodic_reconciliation()

        failed = await registry.latest(adapter.scope.key, stale_after=timedelta(days=1))
        assert failed.data_status == "UNKNOWN"
        assert failed.unknown_fields == ("FACT_ADAPTER_PERIODIC_RECONCILIATION_UNAVAILABLE",)
        assert failed.metrics.snapshot_failed == 1
        assert failed.metrics.websocket_reconnects == 0
        assert failed.metrics.next_retry_at is not None
        assert persisted[-1].data_status == "UNKNOWN"

        await registry.publish(
            adapter.scope.key,
            "BALANCE",
            {"balances": list(failed.balances)},
        )
        increment = await registry.latest(adapter.scope.key, stale_after=timedelta(days=1))
        assert increment.reason == "WEBSOCKET_INCREMENT"
        assert increment.data_status == "UNKNOWN"
        assert increment.unknown_fields == failed.unknown_fields

        exchange.fail_periodic = False
        await supervisor._run_periodic_reconciliation()
        recovered = await registry.latest(adapter.scope.key, stale_after=timedelta(days=1))
        assert recovered.data_status == "CURRENT"
        assert recovered.unknown_fields == ()
        assert recovered.metrics.periodic_reconciliations == 1
        assert recovered.metrics.snapshot_failed == 1
        assert recovered.metrics.websocket_reconnects == 0
        assert recovered.metrics.next_retry_at is None
        await registry.close()

    asyncio.run(scenario())


def test_reconciliation_jitter_is_scope_stable_bounded_and_distributed() -> None:
    def supervisor(account_id: str) -> FactStreamSupervisor:
        adapter = CcxtProFactAdapter(
            _scope(account_id=account_id),
            credentials=_credentials("BINANCE"),
            exchange_factory=lambda *_args: FakeCcxtRestOnlyExchange(),
            clock=lambda: _NOW,
        )
        return FactStreamSupervisor(FactAdapterRegistry(), adapter, reconciliation_seconds=300)

    first = supervisor("account-a")
    restarted = supervisor("account-a")
    other = supervisor("account-b")

    assert first.reconciliation_delay_seconds == restarted.reconciliation_delay_seconds
    assert 240 <= first.reconciliation_delay_seconds <= 300
    assert 240 <= other.reconciliation_delay_seconds <= 300
    assert first.reconciliation_delay_seconds != other.reconciliation_delay_seconds


@pytest.mark.parametrize("fail", [False, True])
def test_snapshot_refresh_is_per_scope_single_flight_and_propagates_failure(fail: bool) -> None:
    async def scenario() -> None:
        exchange = FakeCcxtBlockingSnapshotExchange(fail=fail)
        adapter = CcxtProFactAdapter(
            _scope(),
            credentials=_credentials("BINANCE"),
            exchange_factory=lambda *_args: exchange,
            clock=lambda: _NOW,
        )
        registry = FactAdapterRegistry()
        await registry.register(adapter)
        supervisor = FactStreamSupervisor(registry, adapter)

        periodic = asyncio.create_task(supervisor.refresh("PERIODIC_RECONCILIATION"))
        await exchange.snapshot_started.wait()
        gap = asyncio.create_task(supervisor.refresh("SEQUENCE_GAP_COMPENSATION"))
        await asyncio.sleep(0)
        exchange.snapshot_release.set()
        outcomes = await asyncio.gather(periodic, gap, return_exceptions=True)

        assert exchange.balance_calls == 1
        assert adapter.metrics.snapshot_started == 1
        assert adapter.metrics.snapshot_joined == 1
        if fail:
            assert all(isinstance(item, DomainRejected) for item in outcomes)
            assert adapter.metrics.snapshot_failed == 1
            with pytest.raises(DomainRejected, match="FACT_SNAPSHOT_UNKNOWN"):
                await registry.latest(adapter.scope.key, stale_after=timedelta(days=1))
        else:
            assert outcomes == [None, None]
            current = await registry.latest(adapter.scope.key, stale_after=timedelta(days=1))
            assert current.reason == "SEQUENCE_GAP_COMPENSATION"
            assert current.metrics.snapshot_completed == 1
            assert current.metrics.sequence_compensations == 1
            assert current.metrics.periodic_reconciliations == 0
        await registry.close()

    asyncio.run(scenario())


def test_continuous_websocket_failure_does_not_create_rest_snapshot_storm() -> None:
    async def scenario() -> None:
        exchange = FakeCcxtFailedReconnectExchange()
        adapter = CcxtProFactAdapter(
            _scope(),
            credentials=_credentials("BINANCE"),
            exchange_factory=lambda *_args: exchange,
            clock=lambda: _NOW,
        )
        registry = FactAdapterRegistry()
        await registry.register(adapter)
        delays: list[float] = []

        async def sleeper(delay: float) -> None:
            delays.append(delay)

        supervisor = FactStreamSupervisor(
            registry,
            adapter,
            reconnect_initial_seconds=0.1,
            reconnect_max_seconds=1,
            max_reconnect_attempts=2,
            sleeper=sleeper,
        )
        with pytest.raises(DomainRejected, match="FACT_ADAPTER_WEBSOCKET_UNAVAILABLE"):
            await supervisor.run()

        current = await registry.latest(adapter.scope.key, stale_after=timedelta(days=1))
        assert exchange.balance_calls == 1
        assert delays == [0.1, 0.2]
        assert current.data_status == "UNKNOWN"
        assert "FACT_ADAPTER_WEBSOCKET_UNAVAILABLE" in current.unknown_fields
        assert (await registry.health(stale_after=timedelta(days=1)))["status"] == "not_ready"
        await registry.close()

    asyncio.run(scenario())


def test_repeated_identical_scope_gap_is_cooled_down_and_remains_unknown() -> None:
    async def scenario() -> None:
        seed_adapter = CcxtProFactAdapter(
            _scope(),
            credentials=_credentials("BINANCE"),
            exchange_factory=lambda *_args: FakeCcxtRestOnlyExchange(),
            clock=lambda: _NOW,
        )
        seed = await seed_adapter.snapshot(reason="INITIAL")

        class RepeatedGapAdapter:
            scope = seed.scope
            watch_channels = ("POSITION",)

            def __init__(self) -> None:
                self.snapshot_calls = 0
                self.watch_calls = 0
                self.steady_state = asyncio.Event()

            async def snapshot(self, *, reason, observed_at=None):
                del observed_at
                self.snapshot_calls += 1
                return replace(
                    seed,
                    snapshot_version=self.snapshot_calls,
                    observed_at=datetime.now(UTC),
                    reason=reason,
                    data_status="CURRENT",
                    unknown_fields=(),
                )

            async def watch(self, kind):
                assert kind == "POSITION"
                self.watch_calls += 1
                if self.watch_calls <= 2:
                    return {
                        "positions": [
                            {
                                "native_symbol": "ETHUSDT",
                                "side": "long",
                                "quantity": "1",
                            }
                        ]
                    }
                self.steady_state.set()
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

            async def close(self) -> None:
                return None

        adapter = RepeatedGapAdapter()
        registry = FactAdapterRegistry()
        await registry.register(adapter)
        supervisor = FactStreamSupervisor(registry, adapter, gap_cooldown_seconds=60)
        task = asyncio.create_task(supervisor.run())
        await asyncio.wait_for(adapter.steady_state.wait(), timeout=1)
        current = None
        for _ in range(40):
            current = await registry.latest(adapter.scope.key, stale_after=timedelta(days=1))
            if adapter.snapshot_calls == 2 and current.data_status == "UNKNOWN":
                break
            await asyncio.sleep(0)

        assert adapter.snapshot_calls == 2
        assert current is not None
        assert current.data_status == "UNKNOWN"
        assert any(field.startswith("sequence_gap:") for field in current.unknown_fields)
        supervisor.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await registry.close()
        await seed_adapter.close()

    asyncio.run(scenario())


def test_rest_only_fallback_has_an_explicit_hourly_cap() -> None:
    async def scenario() -> None:
        seed_adapter = CcxtProFactAdapter(
            _scope(),
            credentials=_credentials("BINANCE"),
            exchange_factory=lambda *_args: FakeCcxtRestOnlyExchange(),
            clock=lambda: _NOW,
        )
        seed = await seed_adapter.snapshot(reason="INITIAL")

        class RestOnlyAdapter:
            scope = seed.scope
            watch_channels: tuple = ()

            def __init__(self) -> None:
                self.snapshot_calls = 0

            async def snapshot(self, *, reason, observed_at=None):
                del observed_at
                self.snapshot_calls += 1
                return replace(
                    seed,
                    snapshot_version=self.snapshot_calls,
                    observed_at=datetime.now(UTC),
                    reason=reason,
                )

            async def watch(self, kind):
                raise AssertionError(kind)

            async def close(self) -> None:
                return None

        adapter = RestOnlyAdapter()
        registry = FactAdapterRegistry()
        await registry.register(adapter)
        sleep_calls = 0
        supervisor: FactStreamSupervisor

        async def sleeper(_delay: float) -> None:
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls == 5:
                supervisor.stop()

        supervisor = FactStreamSupervisor(
            registry,
            adapter,
            fallback_seconds=30,
            fallback_max_per_hour=2,
            sleeper=sleeper,
        )
        await supervisor.run()

        assert sleep_calls == 5
        assert adapter.snapshot_calls == 3  # INITIAL plus two bounded fallbacks.
        await registry.close()
        await seed_adapter.close()

    asyncio.run(scenario())


def test_increment_persistence_is_coalesced_without_losing_latest_state() -> None:
    async def scenario() -> None:
        adapter = CcxtProFactAdapter(
            _scope(),
            credentials=_credentials("BINANCE"),
            exchange_factory=lambda *_args: FakeCcxtRestOnlyExchange(),
            clock=lambda: _NOW,
        )
        registry = FactAdapterRegistry()
        await registry.register(adapter)
        await registry.publish_snapshot(await adapter.snapshot(reason="INITIAL"))
        persisted: list[ExchangeFactSnapshot] = []

        async def callback(snapshot: ExchangeFactSnapshot) -> None:
            persisted.append(snapshot)

        supervisor = FactStreamSupervisor(
            registry,
            adapter,
            snapshot_callback=callback,
            persistence_coalesce_seconds=0.05,
        )
        await supervisor._persist_current()
        for total in ("101", "102"):
            await registry.publish(
                adapter.scope.key,
                "BALANCE",
                {
                    "balances": [
                        {
                            "currency": "USDT",
                            "free": total,
                            "used": "0",
                            "total": total,
                            "observed_at": _NOW.isoformat(),
                        }
                    ]
                },
            )
            await supervisor._schedule_callback()
        await asyncio.sleep(0.06)

        assert len(persisted) == 2
        assert persisted[-1].reason == "WEBSOCKET_INCREMENT"
        assert persisted[-1].balances[0]["total"] == "102"
        await registry.close()

    asyncio.run(scenario())


def test_non_authoritative_position_increment_preserves_absent_positions_until_snapshot() -> None:
    async def scenario() -> None:
        adapter = CcxtProFactAdapter(
            _scope(),
            credentials=_credentials("BINANCE"),
            exchange_factory=lambda *_args: FakeCcxtAccountWideExchange(),
            clock=lambda: _NOW,
        )
        registry = FactAdapterRegistry()
        await registry.register(adapter)
        initial = await adapter.snapshot(reason="INITIAL")
        await registry.publish_snapshot(initial)

        await registry.publish(
            adapter.scope.key,
            "POSITION",
            {
                "positions": [
                    {
                        **next(
                            row for row in initial.positions if row["native_symbol"] == "BTCUSDT"
                        ),
                        "quantity": "-3",
                        "observed_at": (_NOW + timedelta(seconds=2)).isoformat(),
                    }
                ]
            },
        )
        increment = await registry.latest(adapter.scope.key, stale_after=timedelta(days=1))
        assert {row["native_symbol"] for row in increment.positions} == {
            "BTCUSDT",
            "ETHUSDT",
        }
        assert (
            next(row for row in increment.positions if row["native_symbol"] == "ETHUSDT")[
                "quantity"
            ]
            == "1"
        )

        authoritative = replace(
            initial,
            observed_at=datetime.now(UTC) + timedelta(seconds=1),
            reason="PERIODIC_RECONCILIATION",
            positions=tuple(row for row in initial.positions if row["native_symbol"] == "BTCUSDT"),
        )
        await registry.publish_snapshot(authoritative)
        normalized = normalize_fact_adapter_snapshot(
            await registry.latest(adapter.scope.key, stale_after=timedelta(days=1))
        )
        assert next(item for item in normalized if item.symbol == "ETHUSDT").position.quantity == 0
        await registry.close()

    asyncio.run(scenario())


def test_out_of_order_increment_is_ignored_and_reported_by_adapter_metrics() -> None:
    async def scenario() -> None:
        adapter = CcxtProFactAdapter(
            _scope(),
            credentials=_credentials("BINANCE"),
            exchange_factory=lambda *_args: FakeCcxtProExchange(),
            clock=lambda: _NOW,
        )
        registry = FactAdapterRegistry()
        await registry.register(adapter)
        initial = await adapter.snapshot(reason="INITIAL")
        await registry.publish_snapshot(initial)
        base = initial.positions[0]
        await registry.publish(
            adapter.scope.key,
            "POSITION",
            {
                "positions": [
                    {
                        **base,
                        "quantity": "-0.003",
                        "observed_at": (_NOW + timedelta(seconds=2)).isoformat(),
                    }
                ]
            },
        )
        await registry.publish(
            adapter.scope.key,
            "POSITION",
            {
                "positions": [
                    {
                        **base,
                        "quantity": "-9",
                        "observed_at": (_NOW + timedelta(seconds=1)).isoformat(),
                    }
                ]
            },
        )

        current = await registry.latest(adapter.scope.key, stale_after=timedelta(days=1))
        assert current.positions[0]["quantity"] == "-0.003"
        assert adapter.metrics.out_of_order_events == 1
        await registry.close()

    asyncio.run(scenario())


def test_periodic_reconciliation_and_multi_team_scope_identity_are_explicit() -> None:
    async def scenario() -> None:
        first = CcxtProFactAdapter(
            _scope(account_id="shared-account"),
            credentials=_credentials("BINANCE"),
            exchange_factory=lambda *_args: FakeCcxtProExchange(),
            clock=lambda: _NOW,
        )
        second_scope = replace(
            _scope(account_id="shared-account"),
            workspace_id="workspace-b",
            team_id="team-b",
        )
        second = CcxtProFactAdapter(
            second_scope,
            credentials=_credentials("BINANCE"),
            exchange_factory=lambda *_args: FakeCcxtProExchange(),
            clock=lambda: _NOW,
        )
        registry = FactAdapterRegistry()
        await registry.register(first)
        await registry.register(second)
        await registry.publish_snapshot(await first.snapshot(reason="INITIAL"))
        periodic = await first.snapshot(reason="PERIODIC_RECONCILIATION")
        await registry.publish_snapshot(periodic)
        await registry.publish_snapshot(await second.snapshot(reason="INITIAL"))

        assert len(await registry.scope_keys()) == 2
        assert first.scope.key != second.scope.key
        assert periodic.metrics.periodic_reconciliations == 1
        assert (await registry.latest(first.scope.key, stale_after=timedelta(days=1))).scope == (
            first.scope
        )
        assert (await registry.latest(second.scope.key, stale_after=timedelta(days=1))).scope == (
            second.scope
        )
        await registry.close()

    asyncio.run(scenario())


def test_mark_burst_coalesces_to_one_persistence_callback_with_latest_value() -> None:
    async def scenario() -> None:
        adapter = CcxtProFactAdapter(
            _scope(),
            credentials=_credentials("BINANCE"),
            exchange_factory=lambda *_args: FakeCcxtProExchange(),
            clock=lambda: _NOW,
        )
        registry = FactAdapterRegistry()
        await registry.register(adapter)
        await registry.publish_snapshot(await adapter.snapshot(reason="INITIAL"))
        persisted: list[ExchangeFactSnapshot] = []

        async def callback(snapshot: ExchangeFactSnapshot) -> None:
            persisted.append(snapshot)

        supervisor = FactStreamSupervisor(
            registry,
            adapter,
            snapshot_callback=callback,
            persistence_coalesce_seconds=0.05,
        )
        await supervisor._persist_current()
        for mark in ("60001", "60002"):
            await registry.publish(
                adapter.scope.key,
                "MARK",
                {
                    "marks": [
                        {
                            "symbol": "BTC/USDT:USDT",
                            "native_symbol": "BTCUSDT",
                            "mark_price": mark,
                            "observed_at": _NOW.isoformat(),
                        }
                    ]
                },
            )
            await supervisor._schedule_callback()
        await asyncio.sleep(0.06)

        assert len(persisted) == 2
        assert persisted[-1].reason == "WEBSOCKET_INCREMENT"
        assert persisted[-1].marks[0]["mark_price"] == "60002"
        await registry.close()

    asyncio.run(scenario())
