"""Time priority, priced. What is a place in the queue actually worth?

Usage:  python queue_study.py [--quick]

The book has enforced price-then-time priority since the first commit, and
"no queue position study" has been near the top of the not-modeled list ever
since. This measures it, in the only currency that matters to the person
holding the priority: ticks of P&L per quote.

Two questions, one per table.

Table 1 - what does the queue cost you? Rest the same 100-share bid with
different numbers of shares in front of it and watch what happens. Two of the
three effects are the expected ones:

    it takes longer to fill      and roughly in proportion to shares ahead:
                                 10 events from the front, 610 from 5,000
                                 shares back, never from 20,000
    you fill less often          only inside a finite window, but every real
                                 quoting decision lives inside one

The third came out backwards from what this file was written to show, and it
is the reason the file is worth reading. The expected result was that
back-of-queue fills are WORSE fills - you only trade when something large
comes through, and something large moves the price against you. That is the
standard account of why queue position is valuable, and in this book it is
false. The markout from the back of the queue is the best in the table, by
about a tick.

The mechanism is sitting in section 4 of RESULTS.md. This book's mid is
sub-diffusive - it mean-reverts, H below 0.5 - because there is no
information in it to make a price move permanent. So the large sell that
reaches 5,000 shares down the queue pushes the mid down, fills you, and then
the mid comes BACK, which is a gift to the buyer it just filled. Deep-queue
fills are selected for large trades, and in a book of coin-flippers a large
trade is a large uninformed trade.

Which settles something about adverse selection rather than measuring it.
Adverse selection is not a mechanical consequence of queueing: the mechanics
alone produce the opposite sign. It needs the flow to know something, and a
zero-intelligence book is precisely a book where it does not. So the real
market's version of this table is the sum of two effects that point opposite
ways, and the informed one has to be big enough to flip the sign - which is
a much stronger statement than "the back of the queue is worse", and it is
the one the data supports.

Table 2 - the decision this turns into. A maker looking at a bid with a long
queue has a choice: join the back of it, or pay a tick and stand alone at the
front of a new level. Improving the price costs a full tick with certainty
and buys first place in the queue. So it comes down to one number: how long
does the queue have to be before the tick is worth paying?
"""

import argparse
import json

from exchange.fees import MAKER_TAKER
from exchange.queue_position import (HORIZON_EVENTS, bucket, rest_one_order,
                                warm_book)

PADS = (0, 500, 2_000, 8_000, 30_000)
HORIZON = 1_000
OUT = "results/queue_position.json"


def table_one(seeds, pads=PADS, horizon=HORIZON) -> tuple[list, list]:
    """One resting order per (pad, seed); bucket the outcomes by shares ahead."""
    outcomes = []
    for pad in pads:
        for seed in seeds:
            o = rest_one_order(warm_book(seed), improve_ticks=0, pad=pad,
                               seed=seed, horizon=horizon)
            if o is not None:
                outcomes.append(o)
    return outcomes, bucket(outcomes)


def table_two(seeds, pads=PADS, horizon=HORIZON) -> dict:
    """Join the back of the touch, or pay a tick and stand at the front.

    Paired by seed: the improved quote only exists when the spread is wider
    than one tick, and comparing an average over the wide-spread seeds against
    an average over all of them would be measuring the spread, not the
    decision. Only seeds where both quotes were possible are counted.
    """
    joined: dict[int, list] = {}
    improved: dict[int, object] = {}

    for seed in seeds:
        better = rest_one_order(warm_book(seed), improve_ticks=1, pad=0,
                                seed=seed, horizon=horizon)
        if better is None:
            continue                      # spread was one tick; no room to improve
        improved[seed] = better
        for pad in pads:
            o = rest_one_order(warm_book(seed), improve_ticks=0, pad=pad,
                               seed=seed, horizon=horizon)
            if o is not None:
                joined.setdefault(pad, []).append((seed, o))

    fees = MAKER_TAKER
    front = [o for o in improved.values()]
    front_value = sum(o.expected_ticks(fees) for o in front) / len(front)

    rows = []
    for pad in pads:
        pairs = [(s, o) for s, o in joined.get(pad, []) if s in improved]
        if not pairs:
            continue
        rows.append({
            "pad": pad,
            "n": len(pairs),
            "median_ahead": sorted(o.ahead for _, o in pairs)[len(pairs) // 2],
            "join_value": sum(o.expected_ticks(fees) for _, o in pairs) / len(pairs),
            "join_fill_rate": sum(1 for _, o in pairs if o.filled) / len(pairs),
        })

    return {"front_value": front_value, "front_n": len(front),
            "front_fill_rate": sum(1 for o in front if o.filled) / len(front),
            "rows": rows}


def breakeven_queue(two: dict) -> float | None:
    """Linear interpolation, in shares ahead, of where joining stops winning.

    Reported to the nearest hundred shares in the printout, because the grid
    is coarse and pretending otherwise would be false precision.
    """
    rows = two["rows"]
    target = two["front_value"]
    for a, b in zip(rows, rows[1:]):
        if (a["join_value"] - target) * (b["join_value"] - target) <= 0:
            span = b["join_value"] - a["join_value"]
            if span == 0:
                return float(a["median_ahead"])
            t = (target - a["join_value"]) / span
            return a["median_ahead"] + t * (b["median_ahead"] - a["median_ahead"])
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true",
                        help="12 seeds instead of 60; for a smoke test, not a result")
    args = parser.parse_args()
    seeds = range(12) if args.quick else range(45)

    outcomes, rows = table_one(seeds)
    print(f"One 100-share bid, {HORIZON:,} events to fill, "
          f"{len(outcomes)} orders across {len(seeds)} books\n")
    print(f"{'shares ahead':>13} {'n':>4} {'filled':>7} {'events':>7} "
          f"{'edge':>6} {'markout':>8} {'adverse':>8}")
    for r in rows:
        hi = "+" if r["hi"] > 10**8 else f"-{r['hi']:,}"
        label = f"{r['lo']:,}{hi}"
        ev = f"{r['events_to_fill']:.0f}" if r["events_to_fill"] else "-"
        fmt = lambda v: f"{v:+.2f}" if v is not None else "    -"
        print(f"{label:>13} {r['n']:>4} {r['fill_rate']:>6.0%} {ev:>7} "
              f"{fmt(r['edge']):>6} {fmt(r['markout']):>8} {fmt(r['adverse']):>8}")
    print("\nedge = mid minus our price at the fill, in ticks. markout = same, "
          f"{250} events later.\nadverse = the difference, i.e. how far the mid "
          "kept running after we traded.")

    two = table_two(seeds)
    be = breakeven_queue(two)
    print(f"\n\nJoin the touch, or pay a tick to stand at the front of a new "
          f"level\n({two['front_n']} books where the spread left room to "
          f"improve)\n")
    print(f"{'shares ahead':>13} {'fills':>7} {'value':>8}")
    for r in two["rows"]:
        print(f"{r['median_ahead']:>13,} {r['join_fill_rate']:>6.0%} "
              f"{r['join_value']:>+8.2f}")
    print(f"{'0 (improved)':>13} {two['front_fill_rate']:>6.0%} "
          f"{two['front_value']:>+8.2f}")
    print("\nvalue = expected ticks per quote: markout plus the maker rebate, "
          "weighted by\nhow much of the order filled. An order that never "
          "trades is worth zero, which is\nwhy this is per quote and not per "
          "fill.")
    if be is not None:
        print(f"\nBreak-even queue: about {round(be / 100) * 100:,} shares. "
              "Behind more than that, paying\nthe tick to be first is the "
              "better quote.")
    else:
        print("\nNo crossing on this grid - one of the two quotes wins at every "
              "queue length tested.")

    with open(OUT, "w") as fh:
        json.dump({"horizon": HORIZON, "n_seeds": len(seeds), "buckets": rows,
                   "queue_vs_price": two, "breakeven_shares": be}, fh, indent=2)
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
