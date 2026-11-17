"""Zero-intelligence order flow through the book.

Verify with:  pytest tests/test_simulator.py

"Zero intelligence" (Gode & Sunder, 1993): agents submit random orders with no
strategy whatsoever. The remarkable result is how much realistic market behavior
— spreads, depth profiles, price impact, a random-walking mid — emerges from the
matching mechanics alone. If a book of coin-flipping traders looks like a real
one, the realism was in the exchange rules, not the traders.

That is a genuinely useful null hypothesis. Any "stylized fact" a zero-intelligence
book reproduces is not evidence of anything about trader behavior.
"""

import numpy as np

from .order import Side, to_tick
from .orderbook import LimitOrderBook

TICK = 0.01

# event mix — roughly the shape of real equity flow, where cancels are common
P_LIMIT, P_MARKET, P_CANCEL = 0.60, 0.25, 0.15
P_PASSIVE = 0.80          # share of limit orders placed inside their own side
OFFSET_TICKS = 3.0        # scale of |N(0, .)| price offset from the mid
MARKET_QTY = (10, 200)
LIMIT_QTY = (10, 200)


def seed_book(book: LimitOrderBook, mid: float = 100.0, levels: int = 5,
              qty: int = 100) -> None:
    """Pre-load `levels` price levels either side of `mid`.

    Without this the book starts empty, the mid is undefined, and there is
    nothing for the first market order to trade against.
    """
    for i in range(1, levels + 1):
        book.add_limit_order(Side.BUY, to_tick(mid - TICK * i), qty)
        book.add_limit_order(Side.SELL, to_tick(mid + TICK * i), qty)


def ticks_from_touch(book: LimitOrderBook, order) -> int:
    """How many ticks an order sits behind the best price on its own side."""
    best = book.best_bid() if order.side is Side.BUY else book.best_ask()
    if best is None:
        return 0
    return int(round(abs(order.price - best) / TICK))


def simulate(book: LimitOrderBook, n_events: int = 5000, seed: int = 0,
             hook=None, protected_ids=(), cancel_focus: float = 1.0,
             max_tries: int = 200) -> dict:
    """Push random events through the book and record what happens.

    Returns {'mids', 'spreads', 'n_trades', 'volume', 'impacts'} where `impacts`
    holds (market order size, absolute mid move) pairs — enough to measure price
    impact against order size afterwards.

    hook, if given, is called as hook(event_index, book, trades) after every
    event, trades being whatever that event printed. It exists so an experiment
    can watch one specific order's fate without a second copy of the event
    generator living somewhere else in the repo — the flow these numbers
    describe has to be the same flow, or the comparison is to a different
    market. queue_position.py is the caller.

    protected_ids are order ids the random cancel stream will not touch. A
    participant studying its own resting order does not want that order pulled
    out from under it by an agent that picked an id at random; real cancels come
    from the order's own owner. Note what this does NOT model: a real queue also
    shrinks because people ahead of you cancel, and holding those in place makes
    every fill probability measured this way a lower bound.

    cancel_focus decides where a cancel lands. At 1.0 (the flow every section
    before 10 was measured on) the cancelled order is drawn uniformly from
    everything live, so a hundred orders at the touch get a hundred orders'
    share of the cancels. Below 1.0 the odds of being the one cancelled fall
    by that factor with every tick an order sits behind its own side's best:
    at 0.5 an order one tick back is half as likely to be pulled as one at the
    touch, two ticks back a quarter. That is the shape of real cancel flow,
    where most cancels are quotes being repositioned rather than changed
    minds, and section 9 found its absence was the whole reason the queue in
    front of an order never melted.

    The focused draw is by rejection: the uniform candidate is drawn from the
    main stream exactly as before, then kept with probability focus**ticks,
    and every retry comes from a second generator. So the main stream is
    consumed identically whatever the focus, and two runs with the same seed
    see the same arrivals, the same market orders and the same cancels-as-
    events, differing only in which resting order each cancel lands on. That
    is what lets section 10 pair a held queue against a melting one under the
    focused flow the way section 9 did under the uniform one. max_tries caps
    the retries, so an order twenty ticks back is still cancelled now and then
    rather than never.
    """
    rng = np.random.default_rng(seed)
    picker = np.random.default_rng(seed + 104_729) if cancel_focus < 1.0 else None
    protected = set(protected_ids)

    mids: list[float] = []
    spreads: list[float] = []
    impacts: list[tuple[int, float]] = []
    n_trades = 0
    volume = 0

    live_ids: list[int] = [i for i in book._by_id if i not in protected]
    last_mid = book.mid_price() or 100.0

    for _i in range(n_events):
        roll = rng.random()
        mid = book.mid_price() or last_mid

        if roll < P_LIMIT:
            side = Side.BUY if rng.random() < 0.5 else Side.SELL
            offset = abs(rng.normal(0, OFFSET_TICKS)) * TICK
            passive = rng.random() < P_PASSIVE

            # Passive orders sit behind the mid; aggressive ones reach across it.
            # The aggressive minority is what makes limit orders trade at all,
            # and it is why the spread does not simply widen forever.
            if side is Side.BUY:
                price = mid - offset if passive else mid + offset
            else:
                price = mid + offset if passive else mid - offset

            qty = int(rng.integers(*LIMIT_QTY))
            oid, trades = book.add_limit_order(side, to_tick(price), qty)
            if oid in book._by_id:
                live_ids.append(oid)

        elif roll < P_LIMIT + P_MARKET:
            side = Side.BUY if rng.random() < 0.5 else Side.SELL
            qty = int(rng.integers(*MARKET_QTY))
            before = book.mid_price()
            trades = book.market_order(side, qty)
            after = book.mid_price()
            if before is not None and after is not None:
                impacts.append((qty, abs(after - before)))

        else:
            trades = []
            if live_ids:
                idx = int(rng.integers(0, len(live_ids)))
                if picker is not None and live_ids[idx] in book._by_id:
                    # The first draw decides whether this cancel reaches a
                    # live order or races a fill that already happened, as it
                    # does in the uniform stream (over a long run most do:
                    # the list is only cleaned when a stale id is drawn).
                    # Only a live cancel is moved, and only to another live
                    # order. The retries leave the list alone, so stale ids
                    # keep leaving it at the uniform rate and the focus
                    # reshapes where the live cancels land, not how many
                    # there are.
                    for _try in range(max_tries):
                        order = book._by_id.get(live_ids[idx])
                        if (order is not None and picker.random()
                                < cancel_focus ** ticks_from_touch(book, order)):
                            break
                        idx = int(picker.integers(0, len(live_ids)))
                # Stale ids are left in the list on purpose: cancel() returning
                # False for an already-filled order is exactly what a real
                # gateway sees when a cancel races a fill.
                book.cancel(live_ids[idx])
                live_ids[idx] = live_ids[-1]
                live_ids.pop()

        n_trades += len(trades)
        volume += sum(t.quantity for t in trades)

        current_mid, current_spread = book.mid_price(), book.spread()
        if current_mid is not None:
            mids.append(current_mid)
            spreads.append(current_spread)
            last_mid = current_mid

        if hook is not None:
            hook(_i, book, trades)

    return {"mids": mids, "spreads": spreads, "n_trades": n_trades,
            "volume": volume, "impacts": impacts}


def impact_exponent(impacts: list[tuple[int, float]],
                    buckets=(0, 50, 100, 150, 200)) -> float:
    """Power-law fit impact ~ size^b from (order size, |mid move|) pairs.

    b < 1 is concave (sublinear) impact - the stylized fact in RESULTS.md
    section 3. Bucketing before the log-log fit matches what run_simulation.py
    reports, so this is the same number, just made reusable for a test.
    """
    import numpy as np
    import pandas as pd

    df = pd.DataFrame(impacts, columns=["qty", "impact"])
    df["bucket"] = pd.cut(df["qty"], buckets)
    table = df.groupby("bucket", observed=True)["impact"].mean()
    x = np.log(np.array([b.mid for b in table.index], dtype=float))
    y = np.log(table.values)
    slope, _ = np.polyfit(x, y, 1)
    return float(slope)


def hurst_exponent(mids: list[float],
                   horizons=(1, 2, 4, 8, 16, 32, 64, 128)) -> float:
    """Fit var(mid[t+k] - mid[t]) ~ k^(2H) across horizons k.

    H = 0.5 is a pure random walk, H < 0.5 is mean-reverting (sub-diffusive)
    - the stylized fact in RESULTS.md section 4.
    """
    import numpy as np

    m = np.asarray(mids, dtype=float)
    var1 = np.var(m[1:] - m[:-1])
    ratios = [np.var(m[k:] - m[:-k]) / var1 for k in horizons]
    slope, _ = np.polyfit(np.log(horizons), np.log(ratios), 1)
    return float(slope / 2)
