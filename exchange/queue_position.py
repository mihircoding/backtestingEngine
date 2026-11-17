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
PAD_LOT = 100              # about the flow's mean order (10-200), so padding melts like flow


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
    ahead_at_exit: int = -1    # live shares still in front when we filled or gave up
    traded_ahead: int = 0      # shares ahead of us that left the queue by trading
    cancelled_ahead: int = 0   # shares ahead of us that left by being cancelled

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


def warm_book(seed: int, n_events: int = WARMUP_EVENTS,
              cancel_focus: float = 1.0) -> LimitOrderBook:
    """A book shaped by the flow the rest of the project characterized.

    cancel_focus below 1.0 warms it under the touch-focused cancel stream of
    section 10 instead, so the queue under study is measured in a book that
    was built by the same flow that will be melting it.
    """
    book = LimitOrderBook()
    seed_book(book, mid=100.0, levels=20, qty=200)
    simulate(book, n_events=n_events, seed=seed, cancel_focus=cancel_focus)
    return book


def rest_one_order(book: LimitOrderBook, improve_ticks: int = 0,
                   pad: int = 0, size: int = ORDER_SIZE,
                   horizon: int = HORIZON_EVENTS,
                   markout: int = MARKOUT_EVENTS,
                   seed: int = 0, melt: bool = False,
                   pad_lot: int = PAD_LOT,
                   cancel_focus: float = 1.0) -> MakerOutcome | None:
    """Rest one buy order and follow it until it fills or the horizon ends.

    improve_ticks=0 joins the best bid, behind whatever is already there.
    improve_ticks=1 steps a tick in front of it, which is only possible when
    the spread is wider than one tick - the caller gets None when it isn't,
    rather than a quietly different experiment.
    improve_ticks=-1 steps a tick behind the touch, onto an empty level.

    pad rests extra shares at our price first, so a long queue can be studied
    without waiting for the flow to happen to build one.

    melt=False (the original experiment) rests the padding as one order and
    protects it from the random cancel stream, so the queue ahead only ever
    shrinks by trading. melt=True rests it as pad_lot-share orders, about the size
    the flow itself sends, and leaves them exposed: the cancel stream can pick
    them off like anyone else's, which is how real queues shrink. The order
    under study is protected either way - its owner is us, and we are not
    cancelling it.

    cancel_focus is passed straight to simulate(): below 1.0 the cancels that
    do the melting cluster at the touch instead of landing anywhere.
    """
    best_bid, best_ask = book.best_bid(), book.best_ask()
    if best_bid is None or best_ask is None:
        return None

    price = to_tick(best_bid + improve_ticks * TICK)
    if price >= best_ask:
        return None          # would cross, so it is not a resting order at all

    natural = sum(q for p, q in book.depth(Side.BUY, levels=50) if p == price)

    pad_ids: list[int] = []
    if pad > 0:
        lots = [pad_lot] * (pad // pad_lot) + ([pad % pad_lot] if pad % pad_lot else [])
        if not melt:
            lots = [pad]
        for lot in lots:
            pad_id, _ = book.add_limit_order(Side.BUY, price, lot,
                                             participant_id="ahead")
            pad_ids.append(pad_id)

    order_id, immediate = book.add_limit_order(Side.BUY, price, size,
                                               participant_id="maker")
    if immediate:
        return None          # traded on arrival; that is a taker, not a maker

    ahead = book.queue_ahead(order_id)
    if ahead is None:
        return None

    # Everyone in front of us, by id, so that at the end each share that left
    # the queue can be attributed to a trade or to a cancel.
    ahead_ids: set[int] = set()
    for resting in book.bids[price]:
        if resting.order_id == order_id:
            break
        if resting.active:
            ahead_ids.add(resting.order_id)

    mid_at_entry = book.mid_price()
    mids: list[float] = []
    filled = 0
    traded_ahead = 0
    fill_event: int | None = None
    ahead_at_exit: int | None = None

    def watch(i: int, live_book: LimitOrderBook, trades) -> None:
        nonlocal filled, fill_event, traded_ahead, ahead_at_exit
        for trade in trades:
            if trade.maker_id == order_id:
                filled += trade.quantity
                if fill_event is None:
                    fill_event = i
                    ahead_at_exit = 0
            elif trade.maker_id in ahead_ids and fill_event is None:
                traded_ahead += trade.quantity
        mid = live_book.mid_price()
        mids.append(mid if mid is not None else (mids[-1] if mids else mid_at_entry))

    protect = [order_id] if melt else [order_id, *pad_ids]
    simulate(book, n_events=horizon, seed=seed + 7919, hook=watch,
             protected_ids=protect, cancel_focus=cancel_focus)

    if ahead_at_exit is None:
        still = book.queue_ahead(order_id)
        ahead_at_exit = still if still is not None else 0
    cancelled_ahead = max(ahead - ahead_at_exit - traded_ahead, 0)

    mid_at_fill = mids[fill_event] if fill_event is not None else None
    mid_after = None
    if fill_event is not None:
        mid_after = mids[min(fill_event + markout, len(mids) - 1)]

    return MakerOutcome(price=price, ahead=ahead, natural_ahead=natural,
                        size=size, filled=min(filled, size),
                        fill_event=fill_event, mid_at_entry=mid_at_entry,
                        mid_at_fill=mid_at_fill, mid_after=mid_after,
                        ahead_at_exit=ahead_at_exit, traded_ahead=traded_ahead,
                        cancelled_ahead=cancelled_ahead)


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


# ---------- section 9: let the queue melt ----------

def paired_melt(pads=(0, 500, 2_000, 8_000, 30_000), seeds=range(45),
                horizon: int = 1_000, improve_ticks: int = 0,
                cancel_focus: float = 1.0,
                **kwargs) -> list[tuple[MakerOutcome, MakerOutcome]]:
    """The same order in the same book twice: queue held still, queue melting.

    Paired by (pad, seed), so the two runs see the same warm book, the same
    place in the queue and the same stream of event types - the generator
    draws the same numbers either way, and only which order a cancel lands on
    differs. Whatever separates the two outcomes is the melting and nothing
    else. Pairs where either run was impossible (crossed, or filled on
    arrival) are dropped from both sides.

    cancel_focus below 1.0 runs both sides under section 10's touch-focused
    cancels, warm-up included. The pairing survives it: the focused draw
    consumes the main random stream exactly as the uniform one does.
    """
    pairs = []
    for pad in pads:
        for seed in seeds:
            held = rest_one_order(warm_book(seed, cancel_focus=cancel_focus),
                                  improve_ticks=improve_ticks, pad=pad,
                                  seed=seed, horizon=horizon, melt=False,
                                  cancel_focus=cancel_focus, **kwargs)
            melted = rest_one_order(warm_book(seed, cancel_focus=cancel_focus),
                                    improve_ticks=improve_ticks, pad=pad,
                                    seed=seed, horizon=horizon, melt=True,
                                    cancel_focus=cancel_focus, **kwargs)
            if held is not None and melted is not None:
                pairs.append((held, melted))
    return pairs


def melt_buckets(pairs, edges=(0, 200, 1_000, 5_000, 20_000, 10**9)) -> list[dict]:
    """Side by side, per range of shares ahead: how each version of the queue
    treated the same order, and how the melting queue in front of us left."""
    rows = []
    for lo, hi in zip(edges, edges[1:]):
        group = [(h, m) for h, m in pairs if lo <= h.ahead < hi]
        if not group:
            continue
        n = len(group)
        held = [h for h, _ in group]
        melt = [m for _, m in group]

        def mean(values):
            values = [v for v in values if v is not None]
            return sum(values) / len(values) if values else None

        rows.append({
            "lo": lo, "hi": hi, "n": n,
            "median_ahead": sorted(h.ahead for h in held)[n // 2],
            "held_fill_rate": sum(1 for h in held if h.filled) / n,
            "melt_fill_rate": sum(1 for m in melt if m.filled) / n,
            "held_events": mean([h.fill_event for h in held if h.filled]),
            "melt_events": mean([m.fill_event for m in melt if m.filled]),
            "held_markout": mean([h.markout_ticks for h in held if h.filled]),
            "melt_markout": mean([m.markout_ticks for m in melt if m.filled]),
            "held_adverse": mean([h.adverse_ticks for h in held if h.filled]),
            "melt_adverse": mean([m.adverse_ticks for m in melt if m.filled]),
            # of the shares that were in front of us at entry, the share that
            # left by trading and the share that left by cancelling
            "traded_share": (sum(m.traded_ahead for m in melt)
                             / max(sum(m.ahead for m in melt), 1)),
            "cancelled_share": (sum(m.cancelled_ahead for m in melt)
                                / max(sum(m.ahead for m in melt), 1)),
            "held_cancelled_share": (sum(h.cancelled_ahead for h in held)
                                     / max(sum(h.ahead for h in held), 1)),
        })
    return rows


def melt_decision(pairs_join, front, fees) -> dict:
    """Section 8's join-or-improve table, with the joined queue allowed to melt.

    pairs_join are (held, melted) outcomes of joining the touch at each pad;
    front is the improved quote per seed, which has nobody in front of it and
    so has nothing to melt. Values are expected ticks per quote, as before.
    """
    front_value = sum(o.expected_ticks(fees) for o in front) / len(front)
    by_pad: dict[int, list] = {}
    for pad, held, melted in pairs_join:
        by_pad.setdefault(pad, []).append((held, melted))
    rows = []
    for pad, group in sorted(by_pad.items()):
        n = len(group)
        rows.append({
            "pad": pad, "n": n,
            "median_ahead": sorted(h.ahead for h, _ in group)[n // 2],
            "held_value": sum(h.expected_ticks(fees) for h, _ in group) / n,
            "melt_value": sum(m.expected_ticks(fees) for _, m in group) / n,
            "held_fill_rate": sum(1 for h, _ in group if h.filled) / n,
            "melt_fill_rate": sum(1 for _, m in group if m.filled) / n,
        })
    return {"front_value": front_value, "front_n": len(front), "rows": rows}


def crossing(rows: list[dict], key: str, target: float) -> float | None:
    """Shares ahead at which `key` crosses `target`, by linear interpolation."""
    for a, b in zip(rows, rows[1:]):
        if (a[key] - target) * (b[key] - target) <= 0:
            span = b[key] - a[key]
            if span == 0:
                return float(a["median_ahead"])
            t = (target - a[key]) / span
            return a["median_ahead"] + t * (b["median_ahead"] - a["median_ahead"])
    return None


def melt_verdicts(short: list[dict], long: list[dict], be_held: float | None,
                  be_melt: float | None, n_live: int, horizon_short: int,
                  horizon_long: int) -> list[dict]:
    """Score what section 9 was written to expect, before it was run.

    The expectation comes from the cancel stream itself: a cancel is 15% of
    events and lands on a uniformly chosen live order, so over H events a
    queue of any length loses about 0.15 * H / n_live of itself to cancels.
    With ~2,300 live orders that is 6-7% at 1,000 events and ~25% at 4,000.
    """
    from .simulator import P_CANCEL
    expect_short = P_CANCEL * horizon_short / n_live
    expect_long = P_CANCEL * horizon_long / n_live

    def deep(rows):
        return [r for r in rows if r["lo"] >= 5_000]

    cancelled_short = (sum(r["cancelled_share"] * r["n"] for r in deep(short))
                       / max(sum(r["n"] for r in deep(short)), 1))
    cancelled_long = (sum(r["cancelled_share"] * r["n"] for r in deep(long))
                      / max(sum(r["n"] for r in deep(long)), 1))
    deepest_short = [r for r in short if r["lo"] >= 20_000]
    gain_deep = [r["melt_fill_rate"] - r["held_fill_rate"] for r in deep(short)]
    markout_deep = [r["melt_markout"] for r in short
                    if 5_000 <= r["lo"] < 20_000 and r["melt_markout"] is not None]

    verdicts = [
        {"claim": "the queue ahead melts by about 0.15 * H / live orders: "
                  f"~{expect_short:.0%} at {horizon_short:,} events, "
                  f"~{expect_long:.0%} at {horizon_long:,}",
         "measured": f"{cancelled_short:.1%} and {cancelled_long:.1%} of the "
                     "deep queues left by cancel",
         "held": (abs(cancelled_short - expect_short) < 0.5 * expect_short
                  and abs(cancelled_long - expect_long) < 0.5 * expect_long)},
        {"claim": "fill rates rise in the deep buckets, but by less than ten "
                  "points at 1,000 events, and 20,000+ still never fills",
         "measured": f"deep-bucket gains {[round(g, 3) for g in gain_deep]}; "
                     f"20,000+ fills {deepest_short[0]['melt_fill_rate']:.0%}"
                     if deepest_short else "no 20,000+ bucket",
         "held": (all(0 <= g < 0.10 for g in gain_deep)
                  and (not deepest_short
                       or deepest_short[0]["melt_fill_rate"] == 0.0))},
        {"claim": "the break-even queue moves up, by roughly the melt share "
                  "(17,600 -> about 19,000 at 1,000 events)",
         "measured": f"held {be_held}, melting {be_melt}",
         "held": (be_held is not None and be_melt is not None
                  and be_melt > be_held
                  and be_melt < be_held * 1.25)},
        {"claim": "the markout from the back of the queue stays positive: "
                  "melting changes when you fill, not what the mid does after",
         "measured": f"5,000-20,000 melt markout {markout_deep}",
         "held": bool(markout_deep) and all(m > 0 for m in markout_deep)},
    ]
    return verdicts


# ---------- section 10: cancels that know where the touch is ----------

def deep_ratio(rows: list[dict], lo: int = 5_000) -> float | None:
    """Shares traded for every share cancelled, in the bucket starting at `lo`.

    Section 9's twenty-four to one is the 5,000-20,000 bucket: deep enough
    for the queue to be a queue, shallow enough that most orders in it fill
    inside the window, so the split is measured over a wait that ended.
    """
    for r in rows:
        if r["lo"] == lo:
            if r["cancelled_share"] == 0:
                return None
            return r["traded_share"] / r["cancelled_share"]
    return None


def focus_verdicts(uniform: dict, focused: dict[float, dict]) -> list[dict]:
    """Score what section 10 was written to expect, before it was run.

    uniform is section 9's saved result (results/queue_melt.json); focused
    maps each cancel_focus to the same shape, measured under the focused
    stream. The arithmetic behind the numbers is in focus_study.py.
    """
    strengths = sorted(focused, reverse=True)       # 0.5 before 0.3
    weakest, strongest = strengths[0], strengths[-1]

    def row(result, horizon, lo):
        for r in result[horizon]["buckets"]:
            if r["lo"] == lo:
                return r
        return None

    def bucket_gain(result, horizon, lo):
        r = row(result, horizon, lo)
        return r["melt_fill_rate"] - r["held_fill_rate"] if r else None

    melt_never = {f: row(focused[f], "short", 20_000)["cancelled_share"]
                  for f in strengths}
    melt_uniform = row(uniform, "short", 20_000)["cancelled_share"]
    ratios = {f: deep_ratio(focused[f]["short"]["buckets"]) for f in strengths}
    gains = {f: bucket_gain(focused[f], "short", 5_000) for f in strengths}
    never_fills = {f: row(focused[f], "short", 20_000)["melt_fill_rate"]
                   for f in strengths}
    be = {f: focused[f]["breakeven_melt"] for f in strengths}
    markouts = {f: row(focused[f], "short", 5_000)["melt_markout"]
                for f in strengths}
    held_gap = {}
    for f in strengths:
        gaps = []
        for r in uniform["short"]["buckets"]:
            mine = row(focused[f], "short", r["lo"])
            if mine is not None:
                gaps.append(abs(mine["held_fill_rate"] - r["held_fill_rate"]))
        held_gap[f] = max(gaps) if gaps else None

    pct = lambda d: {f: f"{v:.1%}" for f, v in d.items()}
    verdicts = [
        {"claim": "the queue ahead melts by the touch's share of the cancel "
                  "weight: the 20,000+ row at 1,000 events goes from 4.9% to "
                  f"about 20% at {weakest} and about 30% at {strongest}",
         "measured": f"uniform {melt_uniform:.1%}, focused {pct(melt_never)}",
         "held": (0.12 <= melt_never[weakest] <= 0.28
                  and 0.20 <= melt_never[strongest] <= 0.40
                  and melt_never[strongest] > melt_never[weakest])},
        {"claim": "the traded-to-cancelled ratio from 5,000 shares back falls "
                  "from 24 to single digits, and does not invert at either "
                  "strength, because only a third of the cancel events reach "
                  "a live order and the touch trades ten shares an event",
         "measured": f"{ {f: round(v, 1) for f, v in ratios.items()} }",
         "held": all(1.0 < v < 10.0 for v in ratios.values())},
        {"claim": "the 5,000-20,000 fill rate gains under twenty points at "
                  "1,000 events, and 20,000+ still never fills",
         "measured": f"gains {pct(gains)}, 20,000+ fills {pct(never_fills)}",
         "held": (all(0 <= g < 0.20 for g in gains.values())
                  and all(v == 0.0 for v in never_fills.values()))},
        {"claim": "the break-even queue rises with the focus, past 20,000 "
                  f"shares at {strongest}",
         "measured": f"uniform {uniform['breakeven_melt']}, focused {be}",
         "held": (all(v is not None for v in be.values())
                  and be[weakest] > uniform["breakeven_melt"]
                  and be[strongest] > be[weakest]
                  and be[strongest] > 20_000)},
        {"claim": "the markout from 5,000-20,000 back stays positive: where "
                  "the cancels land changes when you fill, not what the mid "
                  "does afterwards",
         "measured": f"{ {f: round(v, 2) for f, v in markouts.items() if v is not None} }",
         "held": all(v is not None and v > 0 for v in markouts.values())},
        {"claim": "the held side under the focused flow stays within five "
                  "points of section 8's fill rates in every bucket, because "
                  "the spread, the trade count and the live cancel count are "
                  "the same flow",
         "measured": f"largest bucket gap {pct(held_gap)}",
         "held": all(v is not None and v <= 0.05 for v in held_gap.values())},
    ]
    return verdicts
