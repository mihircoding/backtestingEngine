"""Simulated execution: orders become fills, with frictions.

Verify with:  pytest tests/test_execution.py tests/test_participation.py

Three handlers, in increasing order of how much they are willing to admit:
same-bar close (optimistic), next-bar open (honest about timing), and
participation-limited (honest about size). They share one fill-pricing
function so the only differences between them are the ones being studied.
"""

import numpy as np

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


class ParticipationLimitedExecutionHandler:
    """Fills are capped at a share of each bar's volume, and cost more the
    larger that share is.

    Both of the handlers above will fill any order, at any size, instantly. A
    million shares and a hundred shares get the same price. That is the single
    most flattering assumption in this repo, because it means every result is
    implicitly quoted at zero assets under management — and the first question
    anyone asks about a strategy is how much money it holds.

    This handler answers that question by refusing to do two things:

    1. **Fill more than `participation` of the bar's volume.** A trader working
       a large order does not take the whole book; they work a percentage of
       volume and accept that the order takes days. The remainder stays working
       and continues on the next bar, at the next bar's price, which is the real
       cost: you don't get the price you saw when you decided.
    2. **Charge a flat slippage.** Market impact follows a square-root law -
       cost per share grows like the square root of the order's share of volume,
       not linearly and not not-at-all:

           impact (bps) = impact_coef x daily volatility (bps) x sqrt(rate)

       where `rate` is the slice's shares divided by the bar's volume. The form
       is Almgren et al. (2005) and the textbooks; the constant in front is the
       part every firm calibrates on its own fills and nobody publishes, so
       `impact_coef` is exposed as a parameter with a documented default of 1.0
       rather than buried. Volatility is the trailing estimate from the data
       handler, so impact automatically rises in exactly the markets where
       liquidity is worst - which turns out to be where all of the damage is.

    Timing matches NextBarOpenExecutionHandler: an order placed on bar t begins
    filling at bar t+1's open. The two are therefore directly comparable, and
    the only difference between them is liquidity.

    Working orders net against each other. An order that reverses a working one
    cancels it rather than queueing behind it, which is what an order management
    system does and what stops a whipsawing strategy from accumulating a pile of
    contradictory instructions.

    A caller that sizes orders from its *filled* position - which is what
    Portfolio does when volatility targeting is on - will over-order against a
    partially filled working order unless it also subtracts what is still
    working. `working_quantity()` is here for exactly that, and Portfolio takes
    it as its `pending` argument.
    """

    def __init__(self, data: HistoricalDataHandler, participation: float = 0.10,
                 slippage_bps: float = 2.0, commission_per_share: float = 0.005,
                 impact_coef: float = 1.0, vol_window: int = 20):
        if not 0 < participation <= 1:
            raise ValueError("participation must be in (0, 1]")
        self.data = data
        self.participation = participation
        self.slippage_bps = slippage_bps
        self.commission_per_share = commission_per_share
        self.impact_coef = impact_coef
        self.vol_window = vol_window
        self._working: dict[str, int] = {}
        # Diagnostics. The point of this handler is the numbers it refuses to
        # hide, so it keeps them rather than making the caller reconstruct them.
        self.slices = 0            # how many partial fills it took
        self.unfilled_shares = 0   # still working when the run ended
        self.max_delay_bars = 0    # worst time-to-complete, in bars
        self._age: dict[str, int] = {}

    def execute(self, order: OrderEvent) -> None:
        """Add to the working order for this symbol. Never fills here."""
        net = self._working.get(order.symbol, 0) + order.quantity
        if net == 0:
            self._working.pop(order.symbol, None)
            self._age.pop(order.symbol, None)
        else:
            self._working[order.symbol] = net
            self._age.setdefault(order.symbol, 0)
        return None

    def working_quantity(self, symbol: str) -> int:
        """Shares still to be executed on this symbol. Signed."""
        return self._working.get(symbol, 0)

    def pop_settled_fills(self, time) -> list[FillEvent]:
        """Execute one bar's worth of every working order."""
        fills = []
        for symbol in list(self._working):
            remaining = self._working[symbol]
            cap = int(self.participation * self.data.current_volume(symbol))
            slice_size = int(np.sign(remaining) * min(abs(remaining), max(cap, 0)))
            self._age[symbol] += 1

            if slice_size == 0:
                # A bar with no volume, or a participation cap below one share.
                # The order does not disappear; it waits, which is the honest
                # outcome and is why unfilled_shares exists.
                continue

            price = self.data.current_open(symbol)
            rate = abs(slice_size) / max(self.data.current_volume(symbol), 1.0)
            bps = self.slippage_bps + self._impact_bps(symbol, rate)
            fills.append(_price_fill(OrderEvent(time=time, symbol=symbol,
                                                quantity=slice_size),
                                     price, bps, self.commission_per_share,
                                     time_override=time))
            self.slices += 1

            left = remaining - slice_size
            if left == 0:
                self.max_delay_bars = max(self.max_delay_bars, self._age[symbol])
                self._working.pop(symbol)
                self._age.pop(symbol)
            else:
                self._working[symbol] = left

        return fills

    def _impact_bps(self, symbol: str, rate: float) -> float:
        """Square-root market impact, in basis points, for one slice.

        Falls back to no impact during warmup, when there is not yet enough
        history for a volatility estimate. That understates cost on the first
        few bars of a run and is stated rather than papered over with a guess.
        """
        window = self.data.get_latest(symbol, self.vol_window + 1)
        if len(window) < self.vol_window + 1:
            return 0.0
        daily = np.diff(np.log(window.to_numpy(dtype=float)))
        sigma_bps = float(daily.std(ddof=1)) * 10_000
        return self.impact_coef * sigma_bps * np.sqrt(rate)

    def finalize(self) -> None:
        """Book whatever never got filled. Call once after a run.

        An order that is still working when the data runs out is not free and
        is not filled - it is a position the strategy thinks it has and does
        not. Counting those shares is the difference between a capacity study
        and a capacity advertisement.
        """
        self.unfilled_shares = sum(abs(q) for q in self._working.values())
