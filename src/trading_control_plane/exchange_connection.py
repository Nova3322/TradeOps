from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from trading_control_plane.binance import (
    BinancePortfolioMarginReadOnlyClient,
    BinanceReadOnlyClient,
)
from trading_control_plane.binance_errors import BinanceRequestState
from trading_control_plane.bybit import BybitReadOnlyClient
from trading_control_plane.domain import DomainRejected
from trading_control_plane.hyperliquid import HyperliquidReadOnlyClient
from trading_control_plane.okx import OkxReadOnlyClient


@dataclass(frozen=True, slots=True)
class ConnectionProbeResult:
    success: bool
    error_code: str | None
    diagnostics: dict[str, object] | None = None


class ExchangeConnectionVerifier(Protocol):
    def verify(
        self,
        *,
        venue: str,
        environment: str,
        credentials: Mapping[str, str],
        now: datetime,
    ) -> ConnectionProbeResult: ...


class ReadOnlyExchangeConnectionVerifier:
    """One-shot official-host probes; no method can sign or submit a trading action."""

    def __init__(self, *, request_state: BinanceRequestState | None = None) -> None:
        self._request_state = request_state or BinanceRequestState()

    def _binance(self, credentials: Mapping[str, str], now: datetime, *, environment: str) -> None:
        retry_at = self._request_state.low_priority_retry_at(now=now)
        if retry_at is not None and retry_at > now:
            raise DomainRejected(
                "BINANCE_CONNECTION_WEIGHT_HEADROOM_DEFERRED",
                "manual Binance connection verification is deferred to preserve weight headroom",
                metadata={"next_retry_at": retry_at.isoformat()},
            )
        standard = BinanceReadOnlyClient(
            base_url=(
                "https://testnet.binancefuture.com"
                if environment == "TESTNET"
                else "https://fapi.binance.com"
            ),
            api_key=credentials["api_key"],
            api_secret=credentials["api_secret"],
            request_state=self._request_state,
        )
        try:
            standard.verify_connection(now=now)
            return
        except DomainRejected as standard_error:
            if environment == "TESTNET":
                raise
            if standard_error.code != "BINANCE_AUTHENTICATION_FAILED":
                raise
            portfolio = BinancePortfolioMarginReadOnlyClient(
                base_url="https://papi.binance.com",
                api_key=credentials["api_key"],
                api_secret=credentials["api_secret"],
                request_state=self._request_state,
            )
            try:
                portfolio.verify_connection(now=now)
                return
            except DomainRejected as portfolio_error:
                if portfolio_error.code in {
                    "BINANCE_RATE_LIMITED",
                    "BINANCE_CONNECTION_WEIGHT_HEADROOM_DEFERRED",
                }:
                    raise portfolio_error from None
                raise standard_error from None

    def verify(
        self,
        *,
        venue: str,
        environment: str,
        credentials: Mapping[str, str],
        now: datetime,
    ) -> ConnectionProbeResult:
        try:
            if environment not in {"TESTNET", "LIVE"}:
                raise DomainRejected(
                    "EXCHANGE_ENVIRONMENT_UNSUPPORTED", "execution environment is unsupported"
                )
            if environment == "TESTNET" and venue not in {"BINANCE", "HYPERLIQUID"}:
                raise DomainRejected(
                    "TESTNET_EXECUTION_UNSUPPORTED",
                    "this exchange does not support test-environment execution",
                )
            if venue == "BINANCE":
                self._binance(credentials, now, environment=environment)
            elif venue == "HYPERLIQUID":
                HyperliquidReadOnlyClient(
                    base_url=(
                        "https://api.hyperliquid-testnet.xyz"
                        if environment == "TESTNET"
                        else "https://api.hyperliquid.xyz"
                    ),
                    account_address=credentials.get("account_address"),
                    api_wallet_address=credentials.get("api_wallet_address"),
                ).verify_connection(now=now)
            elif venue == "OKX":
                OkxReadOnlyClient(
                    api_key=credentials["api_key"],
                    api_secret=credentials["api_secret"],
                    passphrase=credentials["passphrase"],
                ).verify_connection(now=now)
            elif venue == "BYBIT":
                BybitReadOnlyClient(
                    api_key=credentials["api_key"],
                    api_secret=credentials["api_secret"],
                ).verify_connection(now=now)
            else:
                raise DomainRejected("EXCHANGE_VENUE_UNSUPPORTED", "exchange venue is unsupported")
        except DomainRejected as exc:
            return ConnectionProbeResult(
                success=False,
                error_code=exc.code,
                diagnostics=None if exc.metadata is None else dict(exc.metadata),
            )
        except (KeyError, ValueError):
            return ConnectionProbeResult(
                success=False,
                error_code=f"{venue}_CONNECTION_CONFIGURATION_INVALID",
            )
        return ConnectionProbeResult(success=True, error_code=None)
