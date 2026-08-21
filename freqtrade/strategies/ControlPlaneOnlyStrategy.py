from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from freqtrade.strategy import IStrategy
from pandas import DataFrame


class ControlPlaneOnlyStrategy(IStrategy):
    """Never creates autonomous signals; approved control-plane intents are the only input."""

    timeframe = "5m"
    can_short = True
    minimal_roi: ClassVar[dict[str, float]] = {"0": 100.0}
    stoploss = -0.99
    trailing_stop = False
    # Allows the authenticated control plane to use Freqtrade's official
    # force-entry position-adjustment path. This strategy still emits no entry,
    # exit, or adjustment signal of its own.
    position_adjustment_enable = True
    process_only_new_candles = True
    startup_candle_count = 1

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        del metadata
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        del metadata
        dataframe["enter_long"] = 0
        dataframe["enter_short"] = 0
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        del metadata
        dataframe["exit_long"] = 0
        dataframe["exit_short"] = 0
        return dataframe

    def leverage(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        proposed_leverage: float,
        max_leverage: float,
        entry_tag: str | None,
        side: str,
        **kwargs: object,
    ) -> float:
        del pair, current_time, current_rate, entry_tag, side, kwargs
        if proposed_leverage <= 0 or proposed_leverage > max_leverage:
            raise ValueError(
                "approved TradeOps leverage exceeds the exchange/account maximum; "
                "the order remains blocked"
            )
        return proposed_leverage
