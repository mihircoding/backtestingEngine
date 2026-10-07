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

The book itself is not flat either, and that is the part this file got wrong
first time. Seeding every price level with the same size says the touch is as
attractive a place to rest as a level ten ticks behind it, which is backwards:
the touch is where you get filled by whoever knows something, so it is the
least attractive place to leave size. Real books lean the other way - thin in
front, thicker behind - and `shape` sets how hard.

The thing I expected `shape` to do, it does not do, and the correction is the
useful part. Holding the book's TOTAL size fixed and only moving it backwards
looked like it ought to tilt the cost curve: small orders dearer because the
touch is thinner, large orders cheaper because the back is fatter. It does not
tilt. It raises the cost at every single order size.

The reason is that a fill price depends on CUMULATIVE depth - how much is
available within n ticks - and not on the total. Moving size from the front to
the back lowers the cumulative at every level except the last, where the two
books are equal by construction. So a shaped book is reached-into further at
any size, and the two curves only meet when the order sweeps the whole book:

    order, shares of a 10,000-share side      flat    shape 0.5    shape 1.0
       100                                    1.00       1.00         1.00
     1,000                                    1.00       1.56         2.28
     5,000                                    3.00       4.07         4.97
     9,000                                    5.00       5.95         6.67
     9,900                                    5.45       6.32         6.97
    (cost in ticks)

Which means the honest description of `shape` is not "a cost-neutral statement
about where liquidity sits". Normalising the total does NOT neutralise it; the
parameter still moves cost, and only ever upward. What normalising buys is
narrower but real: the knob is now a shape a market-data feed can show you,
and it cannot be used to make a backtest cheaper - only dearer. A knob that
can only hurt the result is a safer thing to leave in a repository than one
that can flatter it, which is the argument for keeping it and for leaving the
default at a mild 0.5 rather than at 0.

What this does not fix, which is the honest centre of the file, because
otherwise it is one hidden assumption swapped for another:

  - There are two free parameters, and both are depth rather than cost.
    `depth_frac` is the resting size at a price level as a fraction of the
    bar's volume; `shape` is how that size is distributed across levels.
    Nothing here derives either. The difference from `slippage_bps` is not
    that they are measured rather than assumed - it is that they are DEPTH. A
    venue publishes depth, a market data feed carries it, and anyone can look
    it up for the asset they care about. Cost in basis points is published by
    nobody and depends on the order you were trying to do. An assumption that
    can be checked against a feed is a different kind of object from one that
    cannot. fill_realism.py sweeps both instead of defending a default.

    `shape` is normalised so the book holds the same total size whatever it is
    set to. That is worth stating precisely, because it is weaker than it
    sounds and I first wrote it down too strongly: holding the total fixed
    makes the parameter SIZE-neutral, not COST-neutral. Cost comes off the
    cumulative depth, and every positive shape lowers the cumulative at every
    level, so raising it raises the cost of every order. The knob is one-sided.
    It cannot be turned to make a fill look cheaper than a flat book, which is
    the property that makes it safe to ship; it is not the property of having
    no effect.

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

# How the resting size is distributed across those levels. Size at the i-th
# level away from the touch is proportional to i**SHAPE, renormalised so the
# total resting in the book does not depend on SHAPE at all.
#
# 0.0 is a flat book - every level the same - which is what this handler did
# before and what the tests pin. Positive values thin the touch and thicken
# the levels behind it, which is the direction real books lean: the touch is
# where adverse selection is worst, so it is the least attractive place to
# leave size, and the empirical average book across equity venues rises away
# from the touch before it eventually decays.
#
# 0.5 is the default because it is the middle of the range the literature
# reports and because fill_realism.py sweeps it rather than relying on it.
#
# The normalisation keeps the TOTAL resting size fixed as this changes, so a
# cost difference between two shapes is a difference in where the depth sits
# rather than in how much of it there is. It does not make the parameter
# cost-neutral: cost comes off cumulative depth, and any positive shape lowers
# the cumulative at every level but the last, so raising SHAPE raises the cost
# of every order. That one-sidedness is deliberate - the knob can make a
# backtest look worse and cannot make it look better.
SHAPE = 0.5

# Trailing window, in bars, used to measure how volatile the market is right
# now. 20 trading days is a month and is the window a desk would quote; it is
# long enough that one gap does not dominate it and short enough to still be
# inside a volatility cluster rather than averaging across several.
VOL_WINDOW = 20

# How hard depth responds to that volatility. Resting size at every level is
# multiplied by (typical vol / current vol) ** VOL_ELASTICITY, so 0 is the
# constant-depth book this handler had before and 1 makes depth inversely
# proportional to volatility.
#
# 1.0 is the default because it is the standard empirical statement rather
# than a fitted number: quoted spread scales roughly linearly with volatility
# and size at the touch roughly inversely with it, which is one relationship
# seen from two sides. A market maker quoting a fixed amount of risk rather
# than a fixed number of shares produces exactly that exponent, so 1.0 is
# also what the simplest mechanism predicts.
#
# Unlike `shape`, this knob is NOT one-sided, and that is the thing to be
# careful about. A quiet day now gets a DEEPER book than the constant-depth
# model gave it, so an order placed on a quiet day fills cheaper than before.
# Whether the net effect flatters or penalises a given strategy therefore
# depends on when that strategy trades, which is not a question a default can
# answer - fill_realism.py measures it per strategy instead of assuming.
VOL_ELASTICITY = 1.0

# The multiplier is clipped to this band. Twenty days of unusual calm inside a
# ten-year sample can put current vol a factor of four below the median, and
# an unclipped elasticity of 1 would then quote four times the real depth on
# the strength of twenty observations. The clip is an admission that the
# relationship is measured in the middle of the distribution and should not be
# extrapolated off the end of it.
MULTIPLIER_BAND = (0.25, 4.0)


class BookExecutionHandler:
    """Routes orders into the matching engine and reports the fills it printed.

    data        : the usual HistoricalDataHandler. Needs opens and volumes,
                  for the same reasons the participation handler does.
    depth_frac  : resting shares per price level, as a fraction of bar volume.
    levels      : tick-wide levels the book is seeded with. An order larger
                  than depth_frac * levels * volume cannot complete in one bar
                  however aggressive it is, which is the point.
    shape       : exponent on the depth profile. 0 is flat; positive thins the
                  touch and thickens the levels behind it. Total resting size
                  is held constant, so the capacity limit above is unchanged -
                  but cost is not, because cost comes off cumulative depth.
                  Every positive shape makes every order dearer than the flat
                  book, and the two agree only on an order that sweeps the
                  entire side.
    vol_elasticity : how hard resting size responds to realized volatility.
                  0 reproduces the constant-depth book exactly. 1 makes depth
                  inversely proportional to trailing volatility, measured on a
                  `vol_window` window against an expanding median of itself.
                  Unlike `shape` this can cut either way - quiet bars get a
                  deeper book than before - so whether it helps or hurts a
                  given strategy is a measurement, not a property of the knob.
    vol_window  : bars in that trailing volatility estimate. 20 is a month.
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
                 fees: FeeSchedule = FLAT, commission_per_share: float = 0.005,
                 shape: float = SHAPE, vol_elasticity: float = VOL_ELASTICITY,
                 vol_window: int = VOL_WINDOW):
        if not 0 < participation <= 1:
            raise ValueError("participation must be in (0, 1]")
        if depth_frac <= 0:
            raise ValueError("depth_frac must be positive")
        if levels < 1:
            raise ValueError("levels must be at least 1")
        if shape < 0:
            raise ValueError("shape must be non-negative")
        if vol_elasticity < 0:
            raise ValueError("vol_elasticity must be non-negative")
        if vol_window < 2:
            raise ValueError("vol_window must be at least 2 bars")

        self.data = data
        self.depth_frac = depth_frac
        self.levels = levels
        self.shape = shape
        self._weights = self._depth_weights(levels, shape)
        self.vol_elasticity = vol_elasticity
        self.vol_window = vol_window
        self.participation = participation
        self.fees = fees
        self.commission_per_share = commission_per_share

        self._working: dict[str, int] = {}
        self._age: dict[str, int] = {}
        self._pending_vol: tuple[float, float] = (1.0, float("nan"))

        # Diagnostics. Same contract as the participation handler: this handler
        # exists for the numbers it refuses to hide.
        self.slices = 0
        self.unfilled_shares = 0
        self.max_delay_bars = 0
        self.levels_walked: list[int] = []  # distinct price levels per slice
        self.fee_paid = 0.0                 # signed, positive meaning a credit
        self.exhausted_slices = 0           # slices the book could not complete

        # One entry per slice that printed, for the conditional-cost question:
        # not "what did fills cost on average" but "were the expensive bars the
        # ones this strategy chose to trade on". Answering that needs the cost
        # and the state of the market lined up slice by slice, which is why
        # they are recorded together rather than summed as they go.
        self.slice_shares: list[int] = []     # shares filled, unsigned
        self.slice_cost_bps: list[float] = []  # vwap vs the bar's open, signed
        self.slice_vol_pct: list[float] = []   # where that bar's vol sat, 0-1
        self.slice_multiplier: list[float] = []  # depth multiplier applied

        # Cache for the trailing-vol calculation, keyed on how many bars have
        # been released. Every working symbol on a bar asks the same question
        # of the same price history, and the answer only changes when the
        # cursor does.
        self._vol_cache: dict[tuple[str, int], tuple[float, float]] = {}

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

            # Cost of this slice against the price it was sent at, signed so
            # positive always means the fill went against the order whichever
            # way it was pointing.
            open_px = self.data.current_open(symbol)
            adverse = (vwap - open_px) if filled > 0 else (open_px - vwap)
            multiplier, percentile = self._pending_vol
            self.slice_shares.append(abs(filled))
            self.slice_cost_bps.append(adverse / open_px * 10_000)
            self.slice_vol_pct.append(percentile)
            self.slice_multiplier.append(multiplier)
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

    @staticmethod
    def _depth_weights(levels: int, shape: float) -> np.ndarray:
        """Share of the book's total size resting at each level, nearest first.

        Proportional to i**shape and normalised to sum to `levels`, so the
        weight is a MULTIPLE of the flat book's per-level size. shape=0 gives
        every level a weight of exactly 1, which is the flat book, so the
        profile is a generalisation of the old behaviour rather than a
        replacement for it.
        """
        i = np.arange(1, levels + 1, dtype=float)
        w = i ** shape
        return w * (levels / w.sum())

    def _trailing_vol(self, symbol: str) -> tuple[float, float]:
        """Realized vol over the last `vol_window` bars, and where it sits.

        Returns (multiplier, percentile). The multiplier is what this bar's
        depth gets scaled by; the percentile is where this bar's volatility
        falls among every bar the handler has seen so far, which is what makes
        the conditional-cost question answerable afterwards.

        Both come out of `data.get_latest()`, which by construction can only
        return bars that have already been released. So the reference level is
        an expanding median - what a desk sitting at that date would call a
        typical day given its own history - and not the full-sample median,
        which would be a lookahead. The cost of that honesty is that the first
        few hundred bars are measured against a thin history and the multiplier
        there is closer to 1 than it should be.
        """
        if self.vol_elasticity == 0.0:
            return 1.0, float("nan")

        closes = self.data.get_latest(symbol, 1 << 30)
        key = (symbol, len(closes))
        if key in self._vol_cache:
            return self._vol_cache[key]

        # One rolling value per bar, so there have to be enough returns for at
        # least two windows before a median of them means anything.
        if len(closes) < 2 * self.vol_window:
            return 1.0, float("nan")

        returns = np.diff(np.log(closes.to_numpy(dtype=float)))
        windows = np.lib.stride_tricks.sliding_window_view(returns, self.vol_window)
        sigma = windows.std(axis=1, ddof=1)
        current = float(sigma[-1])
        typical = float(np.median(sigma))

        if current <= 0 or typical <= 0:
            return 1.0, float("nan")

        lo, hi = MULTIPLIER_BAND
        multiplier = float(np.clip((typical / current) ** self.vol_elasticity, lo, hi))
        percentile = float((sigma <= current).mean())

        self._vol_cache[key] = (multiplier, percentile)
        return multiplier, percentile

    def _build_book(self, symbol: str, volume: float) -> LimitOrderBook:
        """A book centred on this bar's open, `levels` deep either side.

        Centred on the open rather than the close because the open is the price
        the order is being sent at - the same choice, for the same reason, as
        the next-bar-open handler.

        Size at each level is the flat per-level size times that level's weight
        from the profile, times this bar's volatility multiplier. Rounding every
        level up to at least one share leaks a little size into a book that
        asked for less than a share a level, which is the right way to be wrong:
        an empty level would print a fill price that skipped a tick it should
        have paid for.
        """
        book = LimitOrderBook()
        mid = self.data.current_open(symbol)
        multiplier, percentile = self._trailing_vol(symbol)
        self._pending_vol = (multiplier, percentile)
        flat = self.depth_frac * volume * multiplier
        for i in range(1, self.levels + 1):
            per_level = max(int(flat * self._weights[i - 1]), 1)
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
