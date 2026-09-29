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


class MovingAverageCrossLongShortStrategy:
    """The same crossing rule, but the down-cross goes short instead of flat.

    MovingAverageCrossStrategy is long or in cash, which is the version every
    tutorial writes and the version cross_section.py measured across 24 assets.
    That test found the rule losing to buy & hold, with the entire loss in
    equities and roughly nothing in bonds and commodities — and it could not
    say whether the reason is that the rule does not work or that half of it
    was missing. A long-only trend rule on something that drifts upward gives
    up the risk premium on every day it sits in cash, so it is penalised by
    the drift rather than by the signal. Trend following as practised is long
    AND short, which removes exactly that asymmetry: a down-cross becomes a
    position instead of an absence of one.

    Nothing else changes. Same windows, same crossing-not-state discipline,
    same signals-only contract with the portfolio - the only difference is
    SHORT where the long-only version emits EXIT. Sizing, and therefore how
    much risk the short leg actually carries, stays the portfolio's business.

    One thing this makes newly relevant: shorts cost carry. Run this with a
    Portfolio built with short_borrow_bps set, or the short leg is borrowing
    stock for free.
    """

    def __init__(self, data: HistoricalDataHandler, short_window: int = 10,
                 long_window: int = 30):
        self.data = data
        self.short_window = short_window
        self.long_window = long_window
        self._state: dict[str, SignalType] = {}

    def on_market(self, event: MarketEvent) -> list[SignalEvent]:
        signals = []

        for symbol in self.data.symbols:
            window = self.data.get_latest(symbol, self.long_window)
            if len(window) < self.long_window:
                continue  # warmup: not enough history to form the long MA yet

            short_ma = window.iloc[-self.short_window:].mean()
            long_ma = window.mean()
            wanted = SignalType.LONG if short_ma > long_ma else SignalType.SHORT

            # Same discipline as the long-only version: signal the change, not
            # the state. Here it also means the flip from long to short is a
            # single signal, and the portfolio turns that into one double-size
            # order rather than an exit followed by an entry.
            if self._state.get(symbol) is not wanted:
                signals.append(SignalEvent(event.time, symbol, wanted))
                self._state[symbol] = wanted

        return signals
