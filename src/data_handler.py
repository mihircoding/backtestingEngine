"""Replays historical bars one at a time.

The whole point of this class is enforcing "no lookahead" structurally: the
strategy asks for data through get_latest(), and get_latest() can only return
bars that have already been streamed. There is no method that exposes the
future. Keep it that way.
"""

import pandas as pd

from .events import MarketEvent


class HistoricalDataHandler:
    """Wraps a DataFrame of close prices (index: timestamps, columns: symbols)."""

    def __init__(self, prices: pd.DataFrame):
        self.prices = prices
        self.symbols = list(prices.columns)
        self._cursor = 0  # number of bars released so far

    def has_more(self) -> bool:
        return self._cursor < len(self.prices)

    def next_bar(self) -> MarketEvent:
        """Release the next bar and return the corresponding MarketEvent."""
        if not self.has_more():
            raise StopIteration("no more bars")
        self._cursor += 1
        return MarketEvent(time=self.prices.index[self._cursor - 1])

    def get_latest(self, symbol: str, n: int = 1) -> pd.Series:
        """Up to the last n *released* close prices for symbol.

        During warmup this returns fewer than n values — callers must handle
        short series (e.g., an MA strategy can't signal before `long_window`
        bars exist).
        """
        if self._cursor == 0:
            return pd.Series(dtype=float)
        window = self.prices[symbol].iloc[max(0, self._cursor - n) : self._cursor]
        return window

    def current_price(self, symbol: str) -> float:
        """Close of the most recently released bar (used for fills/marking)."""
        return float(self.prices[symbol].iloc[self._cursor - 1])

    def current_time(self) -> pd.Timestamp:
        return self.prices.index[self._cursor - 1]
