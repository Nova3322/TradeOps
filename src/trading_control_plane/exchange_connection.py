from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from trading_control_plane.binance import (
    BinancePortfolioMarginReadOnlyClient,
    BinanceReadOnlyClient,
)
from trading_control_plane.bybit import BybitReadOnlyClient
from trading_control_plane.domain import DomainRejected
from trading_control_plane.hyperliquid import HyperliquidReadOnlyClient
from trading_control_plane.okx import OkxReadOnlyClient


@dataclass(frozen=True, slots=True)
class ConnectionProbeResult:
    success: bool
    error_code: str | None


class ExchangeConnectionVerifier(Protocol):
    def verify(
        self,
        *,
        venue: str,
        credentials: Mapping[str, str],
        now: datetime,
    ) -> ConnectionProbeResult: ...


class ReadOnlyExchangeConnectionVerifier:
    """One-shot official-host probes; no method can sign or submit a trading action."""

    @staticmethod
    def _binance(credentials: Mapping[str, str], now: datetime) -> None:
        standard = BinanceReadOnlyClient(
            base_url="https://fapi.binance.com",
            api_key=credentials["api_key"],
            api_secret=credentials["api_secret"],
        )
        try:
            standard.verify_connection(now=now)
            return
        except DomainRejected as standard_error:
            if standard_error.code != "BINANCE_AUTHENTICATION_FAILED":
                raise
            portfolio = BinancePortfolioMarginReadOnlyClient(
                base_url="https://papi.binance.com",
                api_key=credentials["api_key"],
                api_secret=credentials["api_secret"],
            )
            try:
                portfolio.verify_connection(now=now)
                return
            except DomainRejected:
                raise standard_error from None

    def verify(
        self,
        *,
        venue: str,
        credentials: Mapping[str, str],
        now: datetime,
    ) -> ConnectionProbeResult:
        try:
            if venue == "BINANCE":
                self._binance(credentials, now)
            elif venue == "HYPERLIQUID":
                HyperliquidReadOnlyClient(
                    base_url="https://api.hyperliquid.xyz",
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
                raise DomainRejected(
                    "EXCHANGE_VENUE_UNSUPPORTED", "exchange venue is unsupported"
                )
        except DomainRejected as exc:
            return ConnectionProbeResult(success=False, error_code=exc.code)
        except (KeyError, ValueError):
            return ConnectionProbeResult(
                success=False,
                error_code=f"{venue}_CONNECTION_CONFIGURATION_INVALID",
            )
        return ConnectionProbeResult(success=True, error_code=None)
