"""Execution that asks the matching engine what the fill was, instead of assuming.

Verify with:  pytest tests/test_book_execution.py

Every handler in execution.py prices its own fills. Same-bar close and
next-bar open both apply a flat `slippage_bps`; the participation-limited one
replaces that with a square-root impact law. All three have the same shape: a
formula takes the order's size and returns a cost in basis points.

That formula is the least verifiable thing in the repo. Its constant is the
part every firm calibrates on its own fills and nobody publishes - execution.py
says so about `impact_coef` in as many words - so a result quoted in it is
quoted in a number the reader cannot check.

This handler deletes the formula. The order goes to exchange/, the price-time
priority matching engine in this same repository, and the fill price is
whatever the book prints: the volume-weighted average of the resting orders
the order actually consumed. Cost stops being a parameter and becomes a
consequence of depth. Walking three price levels costs three levels, because
three levels is what it took, and an order larger than the book cannot
complete at any price.

The three models disagree in both directions, which is the interesting part.
A flat 2 bps on SPY is about four ticks; a small order takes one. So the flat
model OVERCHARGES at small size. At a third of the day's volume the book runs
out of levels and the same order pays two hundred basis points or goes
unfilled, where the flat model still charges two. fill_realism.py finds where
they cross.

What this does not fix, which is the honest centre of the file, because
otherwise it is one hidden assumption swapped for another:

  - There is still one free parameter, `depth_frac`: resting size at each
    price level, as a fraction of the bar's volume. Nothing here derives it.
    The difference from `slippage_bps` is not that it is measured rather than
    assumed - it is that it is DEPTH. A venue publishes depth, a market data
    feed carries it, and anyone can look it up for the asset they care about.
    Cost in basis points is published by nobody and depends on the order you
    were trying to do. An assumption that can be checked against a feed is a
    different kind of object from one that cannot. fill_realism.py sweeps it
    over two orders of magnitude instead of defending a default.

  - One bar is one book. A day's liquidity is pooled into a single snapshot
    taken at the open, which overstates what is available at the touch in any
    instant and understates how long working an order really takes. A daily
    bar cannot answer an intraday question; this is the least wrong thing a
    daily bar can do with one.

  - The book is rebuilt every bar and remembers nothing. A strategy whose own
    order from yesterday is still resting is not modelled.

  - Everything here is a taker. Passive execution - rest at the touch, collect
    the spread and the maker rebate - is deliberately not offered, and the
    reason is a measurement rather than laziness. Pooling a day's volume into
    one book puts roughly 160,000 shares at the touch on SPY, and
    queue_position.py already established what happens to an order joining a
    queue that long: with 20,000 or more shares ahead of it, it never fills at
    all. A passive handler built on this book would therefore report that
    quoting is impossible, which is a fact about the pooling, not about
    quoting. Getting that number honestly needs intraday data and a book that
    persists between events, and neither is in scope for a daily-bar engine.
    The fee study in notes/exchange.md is where the maker side lives.

Timing matches NextBarOpenExecutionHandler and
ParticipationLimitedExecutionHandler exactly - an order placed on bar t starts
filling at bar t+1's open - so the handlers differ in how the price is formed
and in nothing else. That is what makes them comparable.
"""

from __future__ import annotations

import numpy as np

from exchange.fees import FLAT, FeeSchedule
from exchange.order import TICK, Side, to_tick
from exchange.orderbook import LimitOrderBook

from .data_handler import HistoricalDataHandler
from .events import FillEvent, OrderEvent

# Shares resting at each price level, as a fraction of the bar's volume.
# 0.002 is roughly 160k shares a penny on a typical SPY day: deep for one
# instant, defensible as the size available at that price across a session.
# It is the handler's one free knob and fill_realism.py sweeps it.
DEPTH_FRAC = 0.002

# How far out the book is populated. 200 tick-wide levels is $2.00 either side
# on a penny-tick name, so the deepest fill this handler will print is 50 bps
# from the open on SPY; past that the order waits for the next bar instead.
LEVELS = 200


class BookExecutionHandler:
    """Routes orders into the matching engine and reports the fills it printed.

    data        : the usual HistoricalDataHandler. Needs opens and volumes,
                  for the same reasons the participation handler does.
    depth_frac  : resting shares per price level, as a fraction of bar volume.
    levels      : tick-wide levels the book is seeded with. An order larger
                  than depth_frac * levels * volume cannot complete in one bar
                  however aggressive it is, which is the point.
    participation : cap on the share of a bar's volume one order may take.
                  Identical in meaning to the participation handler's, so the
                  two differ only in how the price is formed.
    fees        : a FeeSchedule from exchange/fees.py. Defaults to FLAT, so a
                  comparison against the formula handlers is not confounded by
                  a cost they do not charge at all. MAKER_TAKER is the
                  realistic setting; every order here is a taker, so switching
                  it on is a straight 3 mils a share.
    commission_per_share : broker commission, charged on top of venue fees and
                  with the same default as the other handlers so this one is
                  swappable with them.
    """

    def __init__(self, data: HistoricalDataHandler, depth_frac: float = DEPTH_FRAC,
                 levels: int = LEVELS, participation: float = 0.10,
                 fees: FeeSchedule = FLAT, commission_per_share: float = 0.005):
        if not 0 < participation <= 1:
            raise ValueError("participation must be in (0, 1]")
        if depth_frac <= 0:
            raise ValueError("depth_frac must be positive")
        if levels < 1:
            raise ValueError("levels must be at least 1")

        self.data = data
        self.depth_frac = depth_frac
        self.levels = levels
        self.participation = participation
        self.fees = fees
        self.commission_per_share = commission_per_share

        self._working: dict[str, int] = {}
        self._age: dict[str, int] = {}

        # Diagnostics. Same contract as the participation handler: this handler
        # exists for the numbers it refuses to hide.
        self.slices = 0
        self.unfilled_shares = 0
        self.max_delay_bars = 0
        self.levels_walked: list[int] = []  # distinct price levels per slice
        self.fee_paid = 0.0                 # signed, positive meaning a credit
        self.exhausted_slices = 0           # slices the book could not complete

    # ---------- handler interface ----------

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
        """Send one bar's worth of every working order to the book."""
        fills = []

        for symbol in list(self._working):
            remaining = self._working[symbol]
            volume = self.data.current_volume(symbol)
            cap = int(self.participation * volume)
            slice_size = int(np.sign(remaining) * min(abs(remaining), max(cap, 0)))
            self._age[symbol] += 1

            if slice_size == 0:
                continue      # no volume, or a cap below one share: it waits

            filled, vwap, walked = self._route(symbol, slice_size, volume)

            if filled == 0:
                # The book printed nothing. The order does not disappear and it
                # does not cost anything; it waits, exactly as an order that
                # found no liquidity does.
                self.exhausted_slices += 1
                continue

            fee = self.fees.taker_charge(abs(filled))
            self.fee_paid += fee
            # FillEvent carries one cash field. A venue fee is a per-share cost
            # like commission, so it rides in the same place - sign flipped,
            # because fees.py reports a credit as positive and
            # FillEvent.commission is a charge.
            commission = abs(filled) * self.commission_per_share - fee

            fills.append(FillEvent(time=time, symbol=symbol, quantity=filled,
                                    fill_price=vwap, commission=commission))
            self.slices += 1
            self.levels_walked.append(walked)
            if abs(filled) < abs(slice_size):
                self.exhausted_slices += 1

            left = remaining - filled
            if left == 0:
                self.max_delay_bars = max(self.max_delay_bars, self._age[symbol])
                self._working.pop(symbol)
                self._age.pop(symbol)
            else:
                self._working[symbol] = left

        return fills

    def finalize(self) -> None:
        """Book whatever never got filled. Call once after a run.

        Same contract, and the same reason, as the participation handler's: an
        order still working when the data runs out is a position the strategy
        thinks it has and does not.
        """
        self.unfilled_shares = sum(abs(q) for q in self._working.values())

    # ---------- the book ----------

    def _build_book(self, symbol: str, volume: float) -> LimitOrderBook:
        """A book centred on this bar's open, `levels` deep either side.

        Centred on the open rather than the close because the open is the price
        the order is being sent at - the same choice, for the same reason, as
        the next-bar-open handler.
        """
        book = LimitOrderBook()
        mid = self.data.current_open(symbol)
        per_level = max(int(self.depth_frac * volume), 1)
        for i in range(1, self.levels + 1):
            book.add_limit_order(Side.BUY, to_tick(mid - TICK * i), per_level)
            book.add_limit_order(Side.SELL, to_tick(mid + TICK * i), per_level)
        return book

    def _route(self, symbol: str, slice_size: int,
                volume: float) -> tuple[int, float, int]:
        """Send one slice to a freshly built book as a market order.

        Returns (filled, vwap, levels_walked), with `filled` signed the way
        slice_size is. A zero return means the book printed nothing at all.
        """
        book = self._build_book(symbol, volume)
        side = Side.BUY if slice_size > 0 else Side.SELL
        trades = book.market_order(side, abs(slice_size))

        if not trades:
            return 0, 0.0, 0

        shares = sum(t.quantity for t in trades)
        vwap = sum(t.price * t.quantity for t in trades) / shares
        walked = len({t.price for t in trades})
        return int(np.sign(slice_size)) * shares, vwap, walked
