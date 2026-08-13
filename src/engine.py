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
        """
        while self.data.has_more():
            self.events.put(self.data.next_bar())

            while not self.events.empty():
                event = self.events.get()

                if isinstance(event, MarketEvent):
                    for signal in self.strategy.on_market(event):
                        self.events.put(signal)

                elif isinstance(event, SignalEvent):
                    order = self.portfolio.on_signal(event)
                    if order is not None:
                        self.events.put(order)

                elif isinstance(event, OrderEvent):
                    self.events.put(self.execution.execute(event))

                elif isinstance(event, FillEvent):
                    self.portfolio.on_fill(event)

            # AFTER the inner loop, so this bar's fills are in this bar's equity.
            self.portfolio.mark_to_market(self.data.current_time())

        return pd.Series(dict(self.portfolio.equity_history))
