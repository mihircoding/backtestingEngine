"""The event loop that wires everything together.

Verify with:  pytest tests/test_engine.py
"""

import queue

import pandas as pd

from .data_handler import HistoricalDataHandler
from .events import FillEvent, MarketEvent, OrderEvent, SignalEvent


class Backtest:
    def __init__(self, data: HistoricalDataHandler, strategy, portfolio, execution):
        self.data = data
        self.strategy = strategy
        self.portfolio = portfolio
        self.execution = execution
        self.events: queue.Queue = queue.Queue()

    def run(self) -> pd.Series:
        """Drain the event queue once per bar; return the equity curve.

        The outer loop is time. The inner loop is causality: one MarketEvent can
        cascade into signals, orders and fills, and everything it spawns must be
        settled before the bar's equity is stamped.

        Each bar has two settling phases. The first is the strategy's: its
        signals become orders become fills. The second is the portfolio's risk
        pass, which can only run once the first has finished, because resizing
        a position to a risk target requires knowing what the position actually
        is. Both are drained before the bar's equity is recorded.
        """
        while self.data.has_more():
            self.events.put(self.data.next_bar())
            self._drain()

            # The strategy has had its say and every fill it caused is booked.
            # Only now can the portfolio see its true position and resize it to
            # a risk target (a no-op unless volatility targeting is switched on).
            for order in self.portfolio.on_market(self.data.current_time()):
                self.events.put(order)
            self._drain()

            # AFTER both drains, so this bar's fills are in this bar's equity.
            self.portfolio.mark_to_market(self.data.current_time())

        return pd.Series(dict(self.portfolio.equity_history))

    def _drain(self) -> None:
        """Process events until the cascade settles.

        Not a fixed number of iterations: handlers push new events as they go,
        so this runs until whatever was on the queue has finished spawning.
        """
        while not self.events.empty():
            event = self.events.get()

            if isinstance(event, MarketEvent):
                # Orders queued on the PREVIOUS bar settle here, first —
                # before this bar's own signals are generated. No-op for
                # SimulatedExecutionHandler, which never has anything
                # pending; this is what lets NextBarOpenExecutionHandler
                # defer a fill by one bar without the engine caring which
                # execution handler it's holding.
                for fill in self.execution.pop_settled_fills(event.time):
                    self.events.put(fill)

                for signal in self.strategy.on_market(event):
                    self.events.put(signal)

            elif isinstance(event, SignalEvent):
                order = self.portfolio.on_signal(event)
                if order is not None:
                    self.events.put(order)

            elif isinstance(event, OrderEvent):
                fill = self.execution.execute(event)
                if fill is not None:
                    self.events.put(fill)

            elif isinstance(event, FillEvent):
                self.portfolio.on_fill(event)
