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
        open. Slippage proportional to price is also crude — real impact scales
        with order size relative to liquidity. Start simple, know the caveats.
        """
        price = self.data.current_price(order.symbol)
        slip = price * self.slippage_bps / 10_000

        # Sign of the trade decides which way slippage cuts. It never helps.
        fill_price = price + slip if order.quantity > 0 else price - slip
        commission = abs(order.quantity) * self.commission_per_share

        return FillEvent(
            time=order.time,
            symbol=order.symbol,
            quantity=order.quantity,
            fill_price=fill_price,
            commission=commission,
        )
