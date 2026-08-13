"""Position/cash accounting, order sizing, and the equity curve.

Verify with:  pytest tests/test_portfolio.py

State maintained here:
    cash      : float, starts at initial_cash
    positions : dict symbol -> signed share count (missing key == 0)
    equity    : list of (time, total_equity) recorded once per bar
"""

from .data_handler import HistoricalDataHandler
from .events import OrderEvent, SignalEvent, SignalType


class Portfolio:
    def __init__(self, data: HistoricalDataHandler, initial_cash: float = 100_000.0,
                 trade_size: int = 100):
        self.data = data
        self.initial_cash = initial_cash
        self.trade_size = trade_size
        self.cash = initial_cash
        self.positions: dict[str, int] = {}
        self.equity_history: list[tuple] = []

    # target position for each signal type — sizing is deliberately dumb here
    _TARGETS = {
        SignalType.LONG: 1,
        SignalType.SHORT: -1,
        SignalType.EXIT: 0,
    }

    def on_signal(self, signal: SignalEvent) -> OrderEvent | None:
        """Turn an opinion into a sized order (or None).

        Fixed-size scheme (deliberately simple — sizing is a project of its own).
        The order is for the DIFFERENCE between target and current position, so
        a SHORT signal while long correctly emits a double-size sell.
        """
        target = self._TARGETS[signal.signal] * self.trade_size
        current = self.positions.get(signal.symbol, 0)
        delta = target - current

        if delta == 0:
            return None  # already where we want to be; don't spam the book

        return OrderEvent(time=signal.time, symbol=signal.symbol, quantity=delta)

    def on_fill(self, fill) -> None:
        """Update cash and positions from a FillEvent.

        The signed quantity handles buys and sells in one expression. No P&L is
        tracked here on purpose — equity is derived from cash + marked positions,
        which cannot drift out of sync with the two things it's derived from.
        """
        self.cash -= fill.quantity * fill.fill_price
        self.cash -= fill.commission
        self.positions[fill.symbol] = self.positions.get(fill.symbol, 0) + fill.quantity

    def total_equity(self) -> float:
        """Cash plus every position marked at the latest released close."""
        marked = sum(
            shares * self.data.current_price(symbol)
            for symbol, shares in self.positions.items()
            if shares != 0
        )
        return self.cash + marked

    def mark_to_market(self, time) -> None:
        """Record one equity point for this bar.

        The engine calls this AFTER all of the bar's events are processed. Called
        before fills, the equity curve lags reality by a bar — the end-to-end
        test checks for exactly that bug.
        """
        self.equity_history.append((time, self.total_equity()))
