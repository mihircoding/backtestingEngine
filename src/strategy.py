"""Strategies consume market data and emit signals.

Verify with:  pytest tests/test_strategy.py
"""

from .data_handler import HistoricalDataHandler
from .events import MarketEvent, SignalEvent, SignalType


class BuyAndHoldStrategy:
    """Complete reference strategy: goes long each symbol once, on the first
    bar, then does nothing. Used by the end-to-end engine test. Read it to see
    the contract a strategy must follow."""

    def __init__(self, data: HistoricalDataHandler):
        self.data = data
        self._bought: set[str] = set()

    def on_market(self, event: MarketEvent) -> list[SignalEvent]:
        signals = []
        for symbol in self.data.symbols:
            if symbol not in self._bought:
                signals.append(SignalEvent(event.time, symbol, SignalType.LONG))
                self._bought.add(symbol)
        return signals


class MovingAverageCrossStrategy:
    """Long when short-MA > long-MA, exit when it crosses back.

    The important discipline is signalling on the CROSSING, not on the state.
    `_in_position` remembers what we've already told the portfolio; without it
    we would emit LONG on every bar the averages are apart, and commission on
    the resulting churn would swamp any edge.
    """

    def __init__(self, data: HistoricalDataHandler, short_window: int = 10,
                 long_window: int = 30):
        self.data = data
        self.short_window = short_window
        self.long_window = long_window
        self._in_position: set[str] = set()

    def on_market(self, event: MarketEvent) -> list[SignalEvent]:
        signals = []

        for symbol in self.data.symbols:
            window = self.data.get_latest(symbol, self.long_window)
            if len(window) < self.long_window:
                continue  # warmup: not enough history to form the long MA yet

            short_ma = window.iloc[-self.short_window:].mean()
            long_ma = window.mean()
            holding = symbol in self._in_position

            if short_ma > long_ma and not holding:
                signals.append(SignalEvent(event.time, symbol, SignalType.LONG))
                self._in_position.add(symbol)
            elif short_ma <= long_ma and holding:
                signals.append(SignalEvent(event.time, symbol, SignalType.EXIT))
                self._in_position.discard(symbol)

        return signals
