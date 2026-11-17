"""Cancels that know where the touch is. Does the queue melt now?

Usage:  python focus_study.py [--quick] [--part short|long|decision|report]
                              [--focus 0.5]

Section 9 let the queue in front of an order melt and found it hardly did:
from 5,000 shares back, 77% of the queue ahead left by trading and 3% by
cancel, because a cancel stream that picks uniformly from everything live
gives a hundred orders at the touch a hundred orders' share of the cancels.
The write-up said the missing mechanism was cancels concentrated at the
touch, and that it needed a cancel model rather than a flag. simulate() now
has one: cancel_focus. Below 1.0, the odds of an order being the one pulled
fall by that factor with every tick it sits behind its own side's best.

Two things were measured while the model was being built, so they are
reported rather than predicted. First, in the flow every section was run on,
two thirds of the cancel events land on an id that has already traded: the
nominal 15% cancel share is about 5% of events reaching a live order, and
the rest are cancels racing a fill. The focused draw keeps that split and
moves only the live cancels. Second, under a normal touch the share of live
cancels landing at the best price goes from 3-5% uniform to about 21% at a
focus of 0.5 and 36% at 0.3, with the spread, the trade count and the live
cancel count unchanged, so the book being measured is the same book with
its cancels moved.

The run is section 9's paired design at two strengths, 0.5 and 0.3, warm-up
included: the same (pad, seed) grid, held against melting, with the uniform
column read from results/queue_melt.json rather than re-run.

Predictions, written before the run (focus_verdicts scores them). The
arithmetic: a padded queue of N 100-share orders at the touch gets N / (N +
W) of the live cancels, where W is the rest of the book's weight, about 370
order-equivalents at 0.5 and 215 at 0.3 against 2,300 uniform. Scaled from
section 9's measured 4.9%:

  1. The 20,000+ row at 1,000 events, which never fills and so sees the
     whole horizon, melts by about 20% at 0.5 and about 30% at 0.3, against
     4.9% uniform. Melt is the touch's share of a fixed number of cancels.
  2. The traded-to-cancelled ratio from 5,000 shares back falls from 24:1
     to single digits and does not invert at either strength. Only a third
     of the cancel events reach a live order, about 47 in 1,000 events, and
     the touch trades ten shares an event; a real busy touch inverts the
     ratio with far more cancels than this flow sends.
  3. The 5,000-20,000 fill rate at 1,000 events gains under twenty points,
     and 20,000+ still never fills: 10,000 shares traded plus a third of
     30,000 melted is short of the queue.
  4. The break-even queue for paying a tick rises with the focus, past
     20,000 shares at 0.3, because joining a queue that melts is worth more.
  5. The markout from the back of the queue stays positive. Where the
     cancels land changes when the order fills, not what the mean-reverting
     mid does afterwards.
  6. The held side under the focused flow stays within five points of
     section 8's fill rates bucket by bucket: same spread, same trades, same
     number of live cancels, so the queue is cleared at the same pace.
"""

import argparse
import json

from exchange.fees import MAKER_TAKER
from exchange.queue_position import (crossing, deep_ratio, focus_verdicts,
                                     melt_buckets, melt_decision, paired_melt,
                                     rest_one_order, warm_book)
from melt_study import LONG_HORIZON, print_buckets
from queue_study import HORIZON, PADS

STRENGTHS = (0.5, 0.3)
UNIFORM = "results/queue_melt.json"
OUT = "results/queue_focus.json"


def decision_table(seeds, focus, pads=PADS, horizon=HORIZON) -> dict:
    """Section 8's join-or-improve table under the focused cancel stream."""
    front = []
    pairs = []
    for seed in seeds:
        book = warm_book(seed, cancel_focus=focus)
        better = rest_one_order(book, improve_ticks=1, pad=0, seed=seed,
                                horizon=horizon, cancel_focus=focus)
        if better is None:
            continue
        front.append(better)
        for pad in pads:
            held = rest_one_order(warm_book(seed, cancel_focus=focus), pad=pad,
                                  seed=seed, horizon=horizon, melt=False,
                                  cancel_focus=focus)
            melted = rest_one_order(warm_book(seed, cancel_focus=focus),
                                    pad=pad, seed=seed, horizon=horizon,
                                    melt=True, cancel_focus=focus)
            if held is not None and melted is not None:
                pairs.append((pad, held, melted))
    return melt_decision(pairs, front, MAKER_TAKER)


def part_file(focus: float, name: str) -> str:
    return OUT.replace(".json", f"_{focus}_{name}.json")


def run_part(name: str, focus: float, seeds) -> None:
    if name in ("short", "long"):
        horizon = HORIZON if name == "short" else LONG_HORIZON
        rows = melt_buckets(paired_melt(pads=PADS, seeds=seeds, horizon=horizon,
                                        cancel_focus=focus))
        print(f"\ncancel_focus = {focus}")
        print_buckets(rows, horizon)
        table = rows
    else:
        table = decision_table(seeds, focus)
    with open(part_file(focus, name), "w") as fh:
        json.dump(table, fh, indent=2)


def assemble(focus: float, seeds) -> dict:
    """One focus's three tables, from the part files, in section 9's shape."""
    short = json.load(open(part_file(focus, "short")))
    long = json.load(open(part_file(focus, "long")))
    two = json.load(open(part_file(focus, "decision")))
    return {"focus": focus, "n_seeds": len(seeds),
            "short": {"horizon": HORIZON, "buckets": short},
            "long": {"horizon": LONG_HORIZON, "buckets": long},
            "decision": two,
            "breakeven_held": crossing(two["rows"], "held_value", two["front_value"]),
            "breakeven_melt": crossing(two["rows"], "melt_value", two["front_value"])}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true",
                        help="12 seeds instead of 45; a smoke test, not a result")
    parser.add_argument("--part", choices=["short", "long", "decision", "report"],
                        help="run one table at one focus and save it; 'report' "
                             "reads every saved table. Without it everything "
                             "runs in one go, about six minutes.")
    parser.add_argument("--focus", type=float, default=None,
                        help="one strength for --part; the report reads all of "
                             f"{STRENGTHS}")
    args = parser.parse_args()
    seeds = range(12) if args.quick else range(45)

    if args.part and args.part != "report":
        run_part(args.part, args.focus, seeds)
        return
    if not args.part:
        for focus in STRENGTHS:
            for name in ("short", "long", "decision"):
                run_part(name, focus, seeds)

    uniform = json.load(open(UNIFORM))
    focused = {f: assemble(f, seeds) for f in STRENGTHS}

    fmt = lambda v: f"about {round(v / 100) * 100:,} shares" if v else "no crossing"
    print(f"\n{'':>14}{'uniform':>12}" + "".join(f"{f:>12}" for f in STRENGTHS))
    for horizon in ("short", "long"):
        for lo in (5_000, 20_000):
            label = f"{lo:,}+ melt @{uniform[horizon]['horizon']:,}"
            cells = [next(r for r in res[horizon]["buckets"] if r["lo"] == lo)
                     for res in (uniform, *focused.values())]
            print(f"{label:>24}" + "".join(f"{c['cancelled_share']:>12.1%}" for c in cells))
            print(f"{'   fills held / melt':>24}"
                  + "".join(f"{c['held_fill_rate']:>5.0%}/{c['melt_fill_rate']:<6.0%}"
                            for c in cells))
    print(f"{'traded : cancelled':>24}"
          + "".join(f"{deep_ratio(res['short']['buckets']):>12.1f}"
                    for res in (uniform, *focused.values())))
    print(f"{'break-even, melting':>24}"
          + "".join(f"{(res['breakeven_melt'] or 0):>12,.0f}"
                    for res in (uniform, *focused.values())))
    for focus, res in focused.items():
        two = res["decision"]
        print(f"\nJoin or improve at cancel_focus = {focus}, {two['front_n']} books")
        print(f"{'shares ahead':>13} | {'held':>6} {'value':>7} | {'melt':>6} {'value':>7}")
        for r in two["rows"]:
            print(f"{r['median_ahead']:>13,} | {r['held_fill_rate']:>6.0%} "
                  f"{r['held_value']:>+7.2f} | {r['melt_fill_rate']:>6.0%} "
                  f"{r['melt_value']:>+7.2f}")
        print(f"{'0 (improved)':>13} | {'':>6} {two['front_value']:>+7.2f} |")
        print(f"Break-even, held: {fmt(res['breakeven_held'])}. "
              f"Melting: {fmt(res['breakeven_melt'])}.")

    verdicts = focus_verdicts(uniform, focused)
    print("\nPredictions, scored:")
    for v in verdicts:
        print(f"  [{'held' if v['held'] else 'FAILED'}] {v['claim']}\n"
              f"         measured: {v['measured']}")

    with open(OUT, "w") as fh:
        json.dump({"strengths": list(STRENGTHS), "uniform_from": UNIFORM,
                   "focused": {str(f): r for f, r in focused.items()},
                   "verdicts": verdicts}, fh, indent=2)
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
