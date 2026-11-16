"""Let the queue melt. What was the held-still caveat in section 8 worth?

Usage:  python melt_study.py [--quick]

Section 8 priced time priority with the shares in front of the order held in
place: the padding was one protected order, so the queue ahead only ever
shrank by trading. The write-up called every fill probability a lower bound
for that reason, and said the people in front of you cancelling is "a large
part of why front-of-queue is worth having". This runs the same experiment
with the queue allowed to melt and puts a number on that sentence.

The design is a paired one. Every (pad, seed) is run twice on the same warm
book: once with the padding as one protected order, once as 100-share orders
the cancel stream can hit like anyone else's. The event generator draws the
same random numbers either way, so the two runs see the same arrivals and the
same cancels-as-events; only which resting order a cancel lands on differs.
The difference between the two outcomes is the melting and nothing else.

Predictions, written before the run (melt_verdicts scores them):

  1. The queue melts at the rate the cancel stream implies: 15% of events
     are cancels, each lands on one of ~2,300 live orders, so a queue loses
     about 0.15 * H / 2,300 of itself in H events - 6-7% at 1,000 events,
     ~25% at 4,000. Melting is a rate, so the horizon is what sets how much
     the caveat was worth.
  2. Fill rates rise in the deep buckets, but by under ten points at 1,000
     events, and the 20,000+ bucket still never fills. A 6% melt does not
     turn an order that needed 20,000 shares to trade into one that fills.
  3. The break-even queue moves up by roughly the melt share - joining looks
     a little better when the queue in front of you shrinks on its own - from
     about 17,600 shares to about 19,000.
  4. The sign in section 8 survives. The markout from the back of the queue
     stays positive, because melting changes when you fill and not what the
     mid does afterwards; the mean-reverting mid is the mechanism and it is
     untouched.
"""

import argparse
import json

from exchange.fees import MAKER_TAKER
from exchange.queue_position import (crossing, melt_buckets, melt_decision,
                                     melt_verdicts, paired_melt,
                                     rest_one_order, warm_book)
from queue_study import HORIZON, PADS

LONG_HORIZON = 4_000
OUT = "results/queue_melt.json"


def decision_table(seeds, pads=PADS, horizon=HORIZON) -> dict:
    """Section 8's table 2 again, joining the back of a queue that melts."""
    front = []
    pairs = []
    for seed in seeds:
        better = rest_one_order(warm_book(seed), improve_ticks=1, pad=0,
                                seed=seed, horizon=horizon)
        if better is None:
            continue
        front.append(better)
        for pad in pads:
            held = rest_one_order(warm_book(seed), pad=pad, seed=seed,
                                  horizon=horizon, melt=False)
            melted = rest_one_order(warm_book(seed), pad=pad, seed=seed,
                                    horizon=horizon, melt=True)
            if held is not None and melted is not None:
                pairs.append((pad, held, melted))
    return melt_decision(pairs, front, MAKER_TAKER)


def print_buckets(rows, horizon):
    print(f"\n{horizon:,} events to fill. Left of the bar the queue is held, "
          "right of it the queue melts.\n")
    print(f"{'shares ahead':>13} {'n':>4} | {'fills':>6} {'events':>7} "
          f"{'markout':>8} | {'fills':>6} {'events':>7} {'markout':>8} "
          f"| {'traded':>7} {'cancel':>7}")
    for r in rows:
        hi = "+" if r["hi"] > 10**8 else f"-{r['hi']:,}"
        ev = lambda v: f"{v:.0f}" if v else "-"
        mk = lambda v: f"{v:+.2f}" if v is not None else "-"
        label = f"{r['lo']:,}{hi}"
        print(f"{label:>13} {r['n']:>4} | "
              f"{r['held_fill_rate']:>6.0%} {ev(r['held_events']):>7} "
              f"{mk(r['held_markout']):>8} | {r['melt_fill_rate']:>6.0%} "
              f"{ev(r['melt_events']):>7} {mk(r['melt_markout']):>8} | "
              f"{r['traded_share']:>7.1%} {r['cancelled_share']:>7.1%}")
    print("\ntraded / cancel = how the shares that were in front of us left "
          "the queue, as a share\nof what was there when we joined, in the "
          "melting run.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true",
                        help="12 seeds instead of 45; a smoke test, not a result")
    parser.add_argument("--part", choices=["short", "long", "decision", "report"],
                        help="run one table and save it; 'report' reads the "
                             "three saved tables. Without it, everything runs "
                             "in one go, which takes a few minutes.")
    args = parser.parse_args()
    seeds = range(12) if args.quick else range(45)
    part = lambda name: OUT.replace(".json", f"_{name}.json")

    if args.part in ("short", "long"):
        horizon = HORIZON if args.part == "short" else LONG_HORIZON
        rows = melt_buckets(paired_melt(pads=PADS, seeds=seeds, horizon=horizon))
        print_buckets(rows, horizon)
        with open(part(args.part), "w") as fh:
            json.dump(rows, fh, indent=2)
        return
    if args.part == "decision":
        with open(part("decision"), "w") as fh:
            json.dump(decision_table(seeds), fh, indent=2)
        return

    n_live = len(warm_book(0)._by_id)
    if args.part == "report":
        short = json.load(open(part("short")))
        long = json.load(open(part("long")))
        two = json.load(open(part("decision")))
    else:
        short = melt_buckets(paired_melt(pads=PADS, seeds=seeds, horizon=HORIZON))
        long = melt_buckets(paired_melt(pads=PADS, seeds=seeds, horizon=LONG_HORIZON))
        two = decision_table(seeds)
    print_buckets(short, HORIZON)
    print_buckets(long, LONG_HORIZON)

    be_held = crossing(two["rows"], "held_value", two["front_value"])
    be_melt = crossing(two["rows"], "melt_value", two["front_value"])
    print(f"\n\nJoin the touch or pay a tick to be first, {two['front_n']} books "
          "where the spread left room\n")
    print(f"{'shares ahead':>13} | {'held':>6} {'value':>7} | {'melt':>6} {'value':>7}")
    for r in two["rows"]:
        print(f"{r['median_ahead']:>13,} | {r['held_fill_rate']:>6.0%} "
              f"{r['held_value']:>+7.2f} | {r['melt_fill_rate']:>6.0%} "
              f"{r['melt_value']:>+7.2f}")
    print(f"{'0 (improved)':>13} | {'':>6} {two['front_value']:>+7.2f} |")
    fmt = lambda v: f"about {round(v / 100) * 100:,} shares" if v else "no crossing"
    print(f"\nBreak-even queue, held: {fmt(be_held)}. Melting: {fmt(be_melt)}.")

    verdicts = melt_verdicts(short, long, be_held, be_melt, n_live,
                             HORIZON, LONG_HORIZON)
    print("\nPredictions, scored:")
    for v in verdicts:
        print(f"  [{'held' if v['held'] else 'FAILED'}] {v['claim']}\n"
              f"         measured: {v['measured']}")

    with open(OUT, "w") as fh:
        json.dump({"n_seeds": len(seeds), "n_live_orders": n_live,
                   "short": {"horizon": HORIZON, "buckets": short},
                   "long": {"horizon": LONG_HORIZON, "buckets": long},
                   "decision": two, "breakeven_held": be_held,
                   "breakeven_melt": be_melt, "verdicts": verdicts}, fh,
                  indent=2)
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
