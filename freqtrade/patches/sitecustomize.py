"""Version-locked Freqtrade Binance Portfolio Margin startup compatibility.

Freqtrade 2026.7 routes its Binance startup one-way-mode check directly to
``fapi`` even when CCXT's documented ``papi`` option is enabled.  Portfolio
Margin keys therefore fail before the standard Freqtrade/CCXT execution path
can start.  CCXT 4.5.68 also projects only the isolated UM sub-wallet as the
linear balance, even though Binance exposes usable collateral at the unified
Portfolio Margin account level.  This patch routes both compatibility gaps
through official CCXT PAPI methods.  Order, leverage and stop-loss operations
remain owned by Freqtrade and CCXT.
"""

from __future__ import annotations

import inspect

import ccxt
import freqtrade
from freqtrade.enums import TradingMode
from freqtrade.exceptions import DDosProtection, OperationalException, TemporaryError
from freqtrade.exchange.binance import Binance
from freqtrade.exchange.common import retrier
from portfolio_margin_compat import (
    normalize_portfolio_margin_account,
    upgrade_portfolio_margin_algo_request,
)

if not freqtrade.__version__.startswith("2026.7"):
    raise RuntimeError("Portfolio Margin compatibility is locked to Freqtrade 2026.7")

_original_additional_exchange_init = Binance.additional_exchange_init
_original_get_balances = Binance.get_balances
_original_ccxt_create_order = ccxt.binance.create_order
_original_ccxt_fetch_order = ccxt.binance.fetch_order
_original_ccxt_fetch_open_orders = ccxt.binance.fetch_open_orders
_original_ccxt_fetch_orders = ccxt.binance.fetch_orders
_original_ccxt_cancel_order = ccxt.binance.cancel_order
_original_ccxt_cancel_all_orders = ccxt.binance.cancel_all_orders
if "fapiPrivateGetPositionSideDual" not in inspect.getsource(
    _original_additional_exchange_init
):
    raise RuntimeError("Freqtrade Binance startup contract changed; refusing compatibility patch")
for method, marker in (
    (_original_ccxt_create_order, "papiPostUmConditionalOrder"),
    (_original_ccxt_fetch_open_orders, "papiGetUmConditionalOpenOrders"),
    (_original_ccxt_fetch_orders, "papiGetUmConditionalAllOrders"),
    (_original_ccxt_cancel_order, "papiDeleteUmConditionalOrder"),
):
    if marker not in inspect.getsource(method):
        raise RuntimeError(
            "CCXT PAPI conditional-order contract changed; refusing compatibility patch"
        )


@retrier
def _portfolio_margin_additional_exchange_init(self: Binance) -> None:
    if not self._api.options.get("papi"):
        return _original_additional_exchange_init(self)
    if self.trading_mode != TradingMode.FUTURES or self._config["dry_run"]:
        return None
    if self.margin_mode.value != "cross":
        raise OperationalException("Binance Portfolio Margin requires cross margin mode")
    try:
        position_side = self._api.papiGetUmPositionSideDual()
        if position_side.get("dualSidePosition") is True:
            raise OperationalException(
                "Hedge Mode is not supported by the controlled Freqtrade worker"
            )
    except ccxt.DDoSProtection as exc:
        raise DDosProtection(exc) from exc
    except (ccxt.OperationFailed, ccxt.ExchangeError) as exc:
        raise TemporaryError(
            "Portfolio Margin startup probe failed through the official CCXT PAPI route"
        ) from exc
    except ccxt.BaseError as exc:
        raise OperationalException(exc) from exc
    return None


@retrier
def _portfolio_margin_get_balances(self: Binance, params: dict | None = None) -> dict:
    if (
        not self._api.options.get("papi")
        or self.trading_mode != TradingMode.FUTURES
        or self._config["dry_run"]
    ):
        return _original_get_balances(self, params)
    if self.margin_mode.value != "cross":
        raise OperationalException("Binance Portfolio Margin requires cross margin mode")
    if params:
        raise OperationalException(
            "Portfolio Margin balance compatibility does not accept caller overrides"
        )
    try:
        account = self._api.papiGetAccount()
        balances = normalize_portfolio_margin_account(
            account,
            stake_currency=self._config["stake_currency"],
        )
        self._log_exchange_response("portfolio_margin_account_balance", balances)
        return balances
    except ValueError as exc:
        raise OperationalException(str(exc)) from exc
    except ccxt.DDoSProtection as exc:
        raise DDosProtection(exc) from exc
    except (ccxt.OperationFailed, ccxt.ExchangeError) as exc:
        raise TemporaryError(
            "Portfolio Margin balance probe failed through the official CCXT PAPI route"
        ) from exc
    except ccxt.BaseError as exc:
        raise OperationalException(exc) from exc


def _papi_enabled(exchange: ccxt.binance, params: dict) -> bool:
    return bool(
        params.get("papi")
        or params.get("portfolioMargin")
        or exchange.options.get("papi")
        or exchange.options.get("portfolioMargin")
    )


def _conditional_requested(params: dict) -> bool:
    return any(
        params.get(field) is not None
        for field in (
            "triggerPrice",
            "stopPrice",
            "stopLossPrice",
            "takeProfitPrice",
            "trailingPercent",
            "callbackRate",
        )
    )


def _conditional_lookup_requested(params: dict) -> bool:
    return any(params.get(field) is True for field in ("stop", "trigger", "conditional"))


def _clean_algo_lookup_params(params: dict) -> dict:
    return {
        key: value
        for key, value in params.items()
        if key
        not in {
            "type",
            "papi",
            "portfolioMargin",
            "stop",
            "trigger",
            "conditional",
            "clientOrderId",
            "origClientOrderId",
            "newClientStrategyId",
        }
    }


def _portfolio_margin_create_order(
    self: ccxt.binance,
    symbol: str,
    type: str,
    side: str,
    amount: float,
    price: float | None = None,
    params: dict | None = None,
) -> dict:
    resolved = dict(params or {})
    if self.markets is None:
        self.load_markets()
    market = self.market(symbol)
    if not (
        market["linear"]
        and _papi_enabled(self, resolved)
        and _conditional_requested(resolved)
    ):
        return _original_ccxt_create_order(self, symbol, type, side, amount, price, resolved)
    request = self.create_order_request(symbol, type, side, amount, price, resolved)
    upgraded = upgrade_portfolio_margin_algo_request(request)
    response = self.request("um/algo/order", "papi", "POST", upgraded)
    return self.parse_order(response, market)


def _portfolio_margin_fetch_order(
    self: ccxt.binance, id: str, symbol: str | None = None, params: dict | None = None
) -> dict:
    resolved = dict(params or {})
    if symbol is None or self.markets is None:
        self.load_markets()
    market = None if symbol is None else self.market(symbol)
    if not (
        market is not None
        and market["linear"]
        and _papi_enabled(self, resolved)
        and _conditional_lookup_requested(resolved)
    ):
        return _original_ccxt_fetch_order(self, id, symbol, resolved)
    client_id = next(
        (
            resolved.get(field)
            for field in ("clientAlgoId", "clientOrderId", "origClientOrderId")
            if resolved.get(field)
        ),
        None,
    )
    request = {"clientAlgoId": client_id} if client_id else {"algoId": id}
    response = self.request(
        "um/algo/algoOrder", "papi", "GET", {**request, **_clean_algo_lookup_params(resolved)}
    )
    return self.parse_order(response, market)


def _portfolio_margin_fetch_open_orders(
    self: ccxt.binance,
    symbol: str | None = None,
    since: int | None = None,
    limit: int | None = None,
    params: dict | None = None,
) -> list[dict]:
    resolved = dict(params or {})
    if self.markets is None:
        self.load_markets()
    market = None if symbol is None else self.market(symbol)
    if not (
        (market is None or market["linear"])
        and _papi_enabled(self, resolved)
        and _conditional_lookup_requested(resolved)
    ):
        return _original_ccxt_fetch_open_orders(self, symbol, since, limit, resolved)
    request = {} if market is None else {"symbol": market["id"]}
    response = self.request(
        "um/algo/openAlgoOrders",
        "papi",
        "GET",
        {**request, **_clean_algo_lookup_params(resolved)},
    )
    return self.parse_orders(response, market, since, limit)


def _portfolio_margin_fetch_orders(
    self: ccxt.binance,
    symbol: str | None = None,
    since: int | None = None,
    limit: int | None = None,
    params: dict | None = None,
) -> list[dict]:
    resolved = dict(params or {})
    if symbol is None:
        return _original_ccxt_fetch_orders(self, symbol, since, limit, resolved)
    if self.markets is None:
        self.load_markets()
    market = self.market(symbol)
    if not (
        market["linear"]
        and _papi_enabled(self, resolved)
        and _conditional_lookup_requested(resolved)
    ):
        return _original_ccxt_fetch_orders(self, symbol, since, limit, resolved)
    request = {"symbol": market["id"]}
    if since is not None:
        request["startTime"] = since
    if limit is not None:
        request["limit"] = limit
    response = self.request(
        "um/algo/allAlgoOrders",
        "papi",
        "GET",
        {**request, **_clean_algo_lookup_params(resolved)},
    )
    return self.parse_orders(response, market, since, limit)


def _portfolio_margin_cancel_order(
    self: ccxt.binance, id: str, symbol: str | None = None, params: dict | None = None
) -> dict:
    resolved = dict(params or {})
    if symbol is None or self.markets is None:
        self.load_markets()
    market = None if symbol is None else self.market(symbol)
    if not (
        market is not None
        and market["linear"]
        and _papi_enabled(self, resolved)
        and _conditional_lookup_requested(resolved)
    ):
        return _original_ccxt_cancel_order(self, id, symbol, resolved)
    client_id = next(
        (
            resolved.get(field)
            for field in ("clientAlgoId", "clientOrderId", "origClientOrderId")
            if resolved.get(field)
        ),
        None,
    )
    request = {"clientAlgoId": client_id} if client_id else {"algoId": id}
    response = self.request(
        "um/algo/order", "papi", "DELETE", {**request, **_clean_algo_lookup_params(resolved)}
    )
    return self.parse_order(response, market)


def _portfolio_margin_cancel_all_orders(
    self: ccxt.binance, symbol: str | None = None, params: dict | None = None
) -> list[dict]:
    resolved = dict(params or {})
    if symbol is None or self.markets is None:
        self.load_markets()
    market = None if symbol is None else self.market(symbol)
    if not (
        market is not None
        and market["linear"]
        and _papi_enabled(self, resolved)
        and _conditional_lookup_requested(resolved)
    ):
        return _original_ccxt_cancel_all_orders(self, symbol, resolved)
    response = self.request(
        "um/algo/allOpenOrders", "papi", "DELETE", {"symbol": market["id"]}
    )
    return [self.safe_order({"info": response, "symbol": symbol, "status": "canceled"})]


Binance.additional_exchange_init = _portfolio_margin_additional_exchange_init
Binance.get_balances = _portfolio_margin_get_balances
Binance._tcp_portfolio_margin_compat = True
ccxt.binance.create_order = _portfolio_margin_create_order
ccxt.binance.fetch_order = _portfolio_margin_fetch_order
ccxt.binance.fetch_open_orders = _portfolio_margin_fetch_open_orders
ccxt.binance.fetch_orders = _portfolio_margin_fetch_orders
ccxt.binance.cancel_order = _portfolio_margin_cancel_order
ccxt.binance.cancel_all_orders = _portfolio_margin_cancel_all_orders
