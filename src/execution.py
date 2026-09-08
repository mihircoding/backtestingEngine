"""Simulated execution: orders become fills, with frictions.

Verify with:  pytest tests/test_execution.py
"""

from .data_handler import HistoricalDataHandler
from .events import FillEvent, OrderEvent


class SimulatedExecutionHandler:
    """Fills every order in full at the current bar's close, adjusted for
    slippage, plus commission.

    slippage_bps : price impact in basis points (1 bp = 0.01%).
                   Buys fill ABOVE the close, sells fill BELOW — slippage
                   always hurts you. That asymmetry is the whole model.
    commission_per_share : flat cash cost per share traded.
    """

    def __init__(self, data: HistoricalDataHandler, slippage_bps: float = 2.0,
                 commission_per_share: float = 0.005):
        self.data = data
        self.slippage_bps = slippage_bps
        self.commission_per_share = commission_per_share

    def execute(self, order: OrderEvent) -> FillEvent:
        """Turn an order into a fill at the current bar's close.

        Modeling note (say this in interviews): filling at the close of the bar
        you signaled on is optimistic. A stricter model fills at the NEXT bar's
        open — see NextBarOpenExecutionHandler below, and RESULTS.md for what
        that costs on real data. Slippage proportional to price is also crude —
        real impact scales with order size relative to liquidity. Start simple,
        know the caveats.
        """
        price = self.data.current_price(order.symbol)
        return _price_fill(order, price, self.slippage_bps, self.commission_per_share)

    def pop_settled_fills(self, time) -> list[FillEvent]:
        """No-op: this handler fills synchronously inside execute(), so there
        is never anything pending to settle on a later bar.

        Exists only so Backtest.run() can call it unconditionally on both
        execution handlers without an isinstance check — see engine.py.
        """
        return []


class NextBarOpenExecutionHandler:
    """Fills every order at the OPENING price of the bar *after* the one the
    order was placed on, instead of the same bar's close.

    README's own "known simplifications" section calls same-bar-close fills
    optimistic: you saw the close and then traded at it, which no real order
    can do (the close print and the decision to trade on it happen at the
    same instant). This handler is the stricter alternative it points to.

    Mechanically this needs the engine's help, because a fill can no longer
    be produced the instant an order arrives — there's no "next bar" yet.
    execute() only records the order; Backtest.run() calls pop_settled_fills()
    once per MarketEvent, *after* that bar's data has been released, to turn
    yesterday's orders into today's fills at today's open.

    Requires a HistoricalDataHandler built with real `opens` data — see
    data_handler.py. Slippage and commission use the exact same formula as
    SimulatedExecutionHandler so the two are comparable on nothing but fill
    timing.
    """

    def __init__(self, data: HistoricalDataHandler, slippage_bps: float = 2.0,
                 commission_per_share: float = 0.005):
        self.data = data
        self.slippage_bps = slippage_bps
        self.commission_per_share = commission_per_share
        self._pending: list[OrderEvent] = []

    def execute(self, order: OrderEvent) -> None:
        """Queue the order. It does not become a fill here."""
        self._pending.append(order)
        return None

    def pop_settled_fills(self, time) -> list[FillEvent]:
        """Fill every order queued on the previous bar at THIS bar's open.

        Called once per MarketEvent, after next_bar() has already advanced
        the data handler's cursor — so current_open() below correctly reads
        the new bar's open, not the one the order was placed on.

        An order queued on the final bar of a run has no "next bar" to fill
        on and is silently dropped when the run ends — a real edge case of
        this fill model, not a bug, and RESULTS.md says so.
        """
        fills = []
        for order in self._pending:
            price = self.data.current_open(order.symbol)
            fills.append(_price_fill(order, price, self.slippage_bps,
                                      self.commission_per_share, time_override=time))
        self._pending = []
        return fills


def _price_fill(order: OrderEvent, price: float, slippage_bps: float,
                 commission_per_share: float, time_override=None) -> FillEvent:
    """Shared fill-pricing math for both handlers: apply slippage in the
    direction that always hurts, charge flat per-share commission, stamp the
    fill with the bar it actually executed on."""
    slip = price * slippage_bps / 10_000
    fill_price = price + slip if order.quantity > 0 else price - slip
    commission = abs(order.quantity) * commission_per_share

    return FillEvent(
        time=time_override if time_override is not None else order.time,
        symbol=order.symbol,
        quantity=order.quantity,
        fill_price=fill_price,
        commission=commission,
    )
