"""Replays historical bars one at a time.

The whole point of this class is enforcing "no lookahead" structurally: the
strategy asks for data through get_latest(), and get_latest() can only return
bars that have already been streamed. There is no method that exposes the
future. Keep it that way.
"""

import pandas as pd

from .events import MarketEvent


class HistoricalDataHandler:
    """Wraps a DataFrame of close prices (index: timestamps, columns: symbols).

    opens is optional: a same-shaped DataFrame of each bar's open price. It
    only exists to support NextBarOpenExecutionHandler (see execution.py) —
    everything else in the engine runs on closes alone. Pass None (the
    default) and current_open() raises rather than silently returning a
    close, so a caller can't accidentally price a "next bar open" fill off
    the wrong number without noticing.
    """

    def __init__(self, prices: pd.DataFrame, opens: pd.DataFrame | None = None):
        self.prices = prices
        self.opens = opens
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

    def current_open(self, symbol: str) -> float:
        """Open of the most recently released bar.

        Only meaningful when this handler was built with an opens frame —
        that's a deliberate hard failure, not a fallback to the close, so a
        next-bar-open fill can never silently become a same-bar-close fill.
        """
        if self.opens is None:
            raise ValueError(
                "this HistoricalDataHandler has no open-price data — pass "
                "opens=... to the constructor to use next-bar-open fills"
            )
        return float(self.opens[symbol].iloc[self._cursor - 1])

    def current_time(self) -> pd.Timestamp:
        return self.prices.index[self._cursor - 1]
