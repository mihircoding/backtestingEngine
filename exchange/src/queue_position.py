"""What a maker's place in the queue is worth.

Verify with:  pytest tests/test_queue_position.py

The book has given time priority away for free since the first commit - a
deque per price level, append at the back, fill from the front - and nothing
in the project has ever asked what that priority is worth to the person
holding it. It is the first thing a market maker cares about. Two orders at
the same price are not the same order: the one in front trades first, and on
a level with ten thousand shares resting the one at the back may never trade
at all.

The measurement is a controlled experiment rather than an observation. Warm
the book with the same zero-intelligence flow as sections 1-5, then rest one
100-share buy order at a chosen price with a chosen number of shares ahead of
it, let the flow run, and record what happened to that one order. Three
numbers come out and they pull against each other:

    fill ratio      how much of the order traded at all
    edge at fill    mid minus your price at the moment you filled, in ticks.
                    This is the half-spread a maker is nominally paid.
    markout         mid some events LATER minus your price. This is what the
                    position was actually worth, and it is the only one of
                    the three that a P&L statement recognises.

The gap between the second and the third is adverse selection, and the point
of measuring it in a zero-intelligence book is that none of these agents knows
anything, so whatever appears cannot be blamed on the other side being
informed. What appears is a gap with the WRONG SIGN: markouts here get better
the further back in the queue you were, not worse. The reason is that this
book's mid mean-reverts (section 4 of RESULTS.md, H below 0.5), so the large
trade that had to come through to reach you deep in the queue moves the price
and then gives it back.

That is the useful version of the result. The textbook claim that the back of
the queue gets the worse fills is not a statement about queueing mechanics at
all - the mechanics alone say the opposite. It is a statement about
information, and it needs the informed half of real order flow to be large
enough to reverse the sign measured here. queue_study.py has the numbers.

Two caveats that belong next to the numbers rather than under them:

  - The shares ahead are held in place (see protected_ids in simulate()).
    Real queues also melt because the people in front cancel, which is a
    large part of why front-of-queue is worth having. Fill probabilities
    here are a lower bound.
  - Padding the level to build a queue adds depth that was not there, which
    feeds back into the flow slightly. The padding is reported alongside the
    natural queue length so the two are never confused.
"""

from __future__ import annotations

from dataclasses import dataclass

from .fees import FeeSchedule
from .order import Side, to_tick
from .orderbook import LimitOrderBook
from .simulator import TICK, seed_book, simulate

WARMUP_EVENTS = 20_000
HORIZON_EVENTS = 4_000
MARKOUT_EVENTS = 250
ORDER_SIZE = 100


@dataclass(frozen=True)
class MakerOutcome:
    """One resting order's whole life, in the units a desk would ask for."""

    price: float
    ahead: int                 # live shares in front of us when we joined
    natural_ahead: int         # how many of those were the book's, not padding
    size: int
    filled: int                # shares that traded
    fill_event: int | None     # events after joining, None if never filled
    mid_at_entry: float
    mid_at_fill: float | None
    mid_after: float | None    # MARKOUT_EVENTS later, or at the end of the run

    @property
    def fill_ratio(self) -> float:
        return self.filled / self.size

    @property
    def edge_ticks(self) -> float | None:
        """Mid minus our price at the instant we filled, in ticks.

        Positive is the direction a maker wants: we bought below the mid.
        """
        if self.mid_at_fill is None:
            return None
        return (self.mid_at_fill - self.price) / TICK

    @property
    def markout_ticks(self) -> float | None:
        """Mid minus our price MARKOUT_EVENTS after the fill, in ticks.

        What the shares were worth once the dust settled, which is the number
        that survives to a P&L statement.
        """
        if self.mid_after is None:
            return None
        return (self.mid_after - self.price) / TICK

    @property
    def adverse_ticks(self) -> float | None:
        """How far the mid kept going against us after we filled, in ticks."""
        if self.mid_at_fill is None or self.mid_after is None:
            return None
        return (self.mid_after - self.mid_at_fill) / TICK

    def expected_ticks(self, fees: FeeSchedule | None = None) -> float:
        """Markout plus rebate, weighted by how much actually filled.

        This is the quote's value, not the fill's: an order that never trades
        earns nothing, and a quote that fills half the time at twice the edge
        is worth the same as one that always fills at half of it. Comparing
        two quoting decisions requires this form, because they do not fill
        equally often - which is the entire subject of this file.
        """
        if self.filled == 0:
            return 0.0
        per_share = self.markout_ticks or 0.0
        if fees is not None:
            per_share += fees.maker / TICK
        return self.fill_ratio * per_share


def warm_book(seed: int, n_events: int = WARMUP_EVENTS) -> LimitOrderBook:
    """A book shaped by the flow the rest of the project characterized."""
    book = LimitOrderBook()
    seed_book(book, mid=100.0, levels=20, qty=200)
    simulate(book, n_events=n_events, seed=seed)
    return book


def rest_one_order(book: LimitOrderBook, improve_ticks: int = 0,
                   pad: int = 0, size: int = ORDER_SIZE,
                   horizon: int = HORIZON_EVENTS,
                   markout: int = MARKOUT_EVENTS,
                   seed: int = 0) -> MakerOutcome | None:
    """Rest one buy order and follow it until it fills or the horizon ends.

    improve_ticks=0 joins the best bid, behind whatever is already there.
    improve_ticks=1 steps a tick in front of it, which is only possible when
    the spread is wider than one tick - the caller gets None when it isn't,
    rather than a quietly different experiment.
    improve_ticks=-1 steps a tick behind the touch, onto an empty level.

    pad rests extra shares at our price first, so a long queue can be studied
    without waiting for the flow to happen to build one.
    """
    best_bid, best_ask = book.best_bid(), book.best_ask()
    if best_bid is None or best_ask is None:
        return None

    price = to_tick(best_bid + improve_ticks * TICK)
    if price >= best_ask:
        return None          # would cross, so it is not a resting order at all

    natural = sum(q for p, q in book.depth(Side.BUY, levels=50) if p == price)

    pad_id = None
    if pad > 0:
        pad_id, _ = book.add_limit_order(Side.BUY, price, pad,
                                         participant_id="ahead")

    order_id, immediate = book.add_limit_order(Side.BUY, price, size,
                                               participant_id="maker")
    if immediate:
        return None          # traded on arrival; that is a taker, not a maker

    ahead = book.queue_ahead(order_id)
    if ahead is None:
        return None

    mid_at_entry = book.mid_price()
    mids: list[float] = []
    filled = 0
    fill_event: int | None = None

    def watch(i: int, live_book: LimitOrderBook, trades) -> None:
        nonlocal filled, fill_event
        for trade in trades:
            if trade.maker_id == order_id:
                filled += trade.quantity
                if fill_event is None:
                    fill_event = i
        mid = live_book.mid_price()
        mids.append(mid if mid is not None else (mids[-1] if mids else mid_at_entry))

    protect = [order_id] if pad_id is None else [order_id, pad_id]
    simulate(book, n_events=horizon, seed=seed + 7919, hook=watch,
             protected_ids=protect)

    mid_at_fill = mids[fill_event] if fill_event is not None else None
    mid_after = None
    if fill_event is not None:
        mid_after = mids[min(fill_event + markout, len(mids) - 1)]

    return MakerOutcome(price=price, ahead=ahead, natural_ahead=natural,
                        size=size, filled=min(filled, size),
                        fill_event=fill_event, mid_at_entry=mid_at_entry,
                        mid_at_fill=mid_at_fill, mid_after=mid_after)


def sweep_queue(pads=(0, 250, 1_000, 4_000, 16_000), seeds=range(40),
                improve_ticks: int = 0, **kwargs) -> list[MakerOutcome]:
    """One order per (pad, seed), so outcomes can be bucketed by shares ahead.

    The grid is over padding rather than over `ahead` directly because the
    natural queue at the touch is whatever the flow left there - anywhere from
    tens of shares to thousands, as the depth profile in RESULTS.md section 2
    implies. Padding shifts that distribution up; the reported `ahead` is
    always the measured number, never the target.
    """
    out = []
    for pad in pads:
        for seed in seeds:
            outcome = rest_one_order(warm_book(seed), improve_ticks=improve_ticks,
                                     pad=pad, seed=seed, **kwargs)
            if outcome is not None:
                out.append(outcome)
    return out


def bucket(outcomes: list[MakerOutcome], edges=(0, 200, 1_000, 5_000, 20_000,
                                                10**9)) -> list[dict]:
    """Average the outcomes inside each range of shares-ahead."""
    rows = []
    for lo, hi in zip(edges, edges[1:]):
        group = [o for o in outcomes if lo <= o.ahead < hi]
        if not group:
            continue
        hit = [o for o in group if o.filled]
        rows.append({
            "lo": lo, "hi": hi, "n": len(group),
            "median_ahead": sorted(o.ahead for o in group)[len(group) // 2],
            "fill_rate": len(hit) / len(group),
            "fill_ratio": sum(o.fill_ratio for o in group) / len(group),
            "events_to_fill": (sum(o.fill_event for o in hit) / len(hit)
                               if hit else None),
            "edge": (sum(o.edge_ticks for o in hit) / len(hit)) if hit else None,
            "markout": (sum(o.markout_ticks for o in hit) / len(hit))
                       if hit else None,
            "adverse": (sum(o.adverse_ticks for o in hit) / len(hit))
                       if hit else None,
        })
    return rows
