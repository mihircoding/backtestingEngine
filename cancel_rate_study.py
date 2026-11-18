"""Turn the cancel rate up under the focus. Does the ratio invert?

Usage:  python cancel_rate_study.py --part grid --rate 0.35 --focus 0.3 --pad 8000 --chunk 0
        python cancel_rate_study.py --part grid --rate 0.15 --focus 0.3 --clean --pad 8000 --chunk 0
        python cancel_rate_study.py --part flow --rate 0.35 --focus 0.3
        python cancel_rate_study.py --part facts
        python cancel_rate_study.py --part report

Section 10 moved the cancels to the touch and the queue in front of a
resting order melted four times as fast, but a share ahead of you was still
traded three times for every time it was cancelled. The write-up ended on
the arithmetic: if every live cancel in 1,000 events landed on a 30,000-share
queue it would melt 14,800 shares against the 10,000 that trade, a ratio of
0.7, inverted - and said the cancel *rate* had to do what the placement
could not. simulate() has the dial now: p_cancel sets the share of events
that are cancels, with the limit and market shares scaled down in their
original 60:25 proportion so the makers and takers keep their relative
weight. The grid is section 10's short table (five pads, 45 seeds, 1,000
events, held against melting) at 0.25, 0.35 and 0.45 under a focus of 0.3,
and at 0.45 under uniform cancels as the control, with the 0.15 columns read
from sections 9 and 10.

Section 10 also found the warm-up's cancel list two thirds stale: an order
that trades stays in the pool a cancel draws from until a cancel happens to
draw it, so sections 1-5 were measured on a live cancel rate near 5% of
events, not 15%. simulate(clean_stale=True) removes a filled order the
moment it fills. One thing measured while building it is reported rather
than predicted: after a 20,000-event warm-up the clean book holds about 330
live orders against 2,200 - the cancel stream, once it reaches live orders
every time, removes resting orders about as fast as the flow adds them. The
facts part reruns sections 1-4 (50,000 events, seed 7) both ways, and the
grid runs section 10's (0.15, 0.3) cell once more on clean warm books.

Predictions, written before the run (rate_verdicts scores them). The
arithmetic is the one section 10 closed on: trades per event fall with the
market share, 0.25 * (1 - p) / 0.85, and cancels per event rise with p, so
the ratio scales as 3.2 * (0.15 / 0.85) * (1 - p) / p, which is 1 at p = 0.36.

  1. The traded-to-cancelled ratio from 5,000 shares back at a focus of 0.3
     falls as (1 - p) / p: about 1.7 at 0.25, 1.1 at 0.35, 0.7 at 0.45, and
     it inverts between 0.30 and 0.40.
  2. Under uniform cancels the ratio does not invert even at 0.45, about 5
     (24 scaled the same way): the rate alone is not enough, the placement
     is necessary.
  3. The 20,000+ row's melt at 1,000 events rises with the cancel share but
     less than proportionally, because the queue is the touch only half the
     time whatever the rate: about 30% at 0.25, 40% at 0.35, 50% at 0.45,
     each within ten points.
  4. 20,000+ still never fills at 1,000 events at any rate: 6,500 shares
     traded plus half of 30,000 melted is short of the queue.
  5. The held side's fill rate from 5,000-20,000 back falls with the rate by
     roughly the market-order share, 55% to about 48%, 41%, 35%, so under 45%
     at 0.45 and falling at every step.
  6. The spread widens with the rate, from 1.9 ticks to over 2.5 at 0.45:
     fewer limit orders replace what the market orders eat, and the cancels
     thin the touch.
  7. Clean warm books move section 10's (0.15, 0.3) cell by little: the melt
     in the 20,000+ row rises by under five points and the ratio falls by
     under one. The horizon starts from a fresh list either way, and the
     queue is the padding, not the warm book.
  8. The stylized facts at the true 15%: the spread's mean stays within 10%
     of 0.0198 and the share of time at the floor within five points of
     37.9%; the depth hump shrinks more than five-fold (102,000 shares three
     ticks out to under 20,000); the impact exponent rises but stays under
     1.0; H rises from 0.32 but stays under 0.5. The touch is made by
     replenishment and consumption, which the cleaning does not touch; the
     hump is made by orders nobody cancels, which it does.
"""

import argparse
import json
import os
from dataclasses import asdict

import numpy as np

from exchange.orderbook import LimitOrderBook
from exchange.queue_position import (MakerOutcome, deep_ratio, melt_buckets,
                                     paired_melt, warm_book)
from exchange.simulator import (hurst_exponent, impact_exponent, seed_book,
                                simulate, TICK)
from exchange.order import Side
from melt_study import print_buckets
from queue_study import HORIZON, PADS

RATES = (0.25, 0.35, 0.45)          # the rates the predictions were written for
EXTRA = (0.30, 0.40)                # two more points, added for the crossing's resolution
FOCUS = 0.3
CELLS = [(r, FOCUS, False) for r in sorted(RATES + EXTRA)] + \
        [(0.45, 1.0, False), (0.15, FOCUS, True)]
UNIFORM = "results/queue_melt.json"      # (0.15, 1.0)
FOCUSED = "results/queue_focus.json"     # (0.15, 0.3)
PARTS = "results/cancel_rate_parts"
OUT = "results/cancel_rate.json"
FACTS_EVENTS, FACTS_SEED = 50_000, 7


def cell_name(rate, focus, clean) -> str:
    return f"{rate}_{focus}" + ("_clean" if clean else "")


N_CHUNKS = 3    # a (cell, pad) is split over seeds so no part runs a minute


def grid_file(rate, focus, clean, pad, chunk) -> str:
    return f"{PARTS}/grid_{cell_name(rate, focus, clean)}_{pad}_{chunk}.json"


def run_grid(rate, focus, clean, pad, chunk, seeds) -> None:
    seeds = list(seeds)[chunk::N_CHUNKS]
    pairs = paired_melt(pads=(pad,), seeds=seeds, horizon=HORIZON,
                        cancel_focus=focus, p_cancel=rate, clean_stale=clean)
    os.makedirs(PARTS, exist_ok=True)
    with open(grid_file(rate, focus, clean, pad, chunk), "w") as fh:
        json.dump([[asdict(h), asdict(m)] for h, m in pairs], fh)
    print(f"{cell_name(rate, focus, clean)} pad {pad} chunk {chunk}: {len(pairs)} pairs")


def load_grid(rate, focus, clean):
    pairs = []
    for pad in PADS:
        for chunk in range(N_CHUNKS):
            with open(grid_file(rate, focus, clean, pad, chunk)) as fh:
                pairs += [(MakerOutcome(**h), MakerOutcome(**m)) for h, m in json.load(fh)]
    return pairs


def flow_stats(rate, focus, clean, seeds=range(12)) -> dict:
    """What the warm-up flow looks like at this rate: spread, trades, live
    cancels, and how many orders are left in the book."""
    rows = []
    for seed in seeds:
        book = LimitOrderBook()
        seed_book(book, mid=100.0, levels=20, qty=200)
        res = simulate(book, n_events=20_000, seed=seed, cancel_focus=focus,
                       p_cancel=rate, clean_stale=clean)
        rows.append((np.mean(res["spreads"]) / TICK, res["n_trades"] / 20_000,
                     res["volume"] / 20_000, res["live_cancels"] / 20_000,
                     res["live_cancels"] / max(res["cancel_events"], 1),
                     len(book._by_id)))
    m = np.mean(rows, axis=0)
    return {"spread_ticks": float(m[0]), "trades_per_event": float(m[1]),
            "shares_per_event": float(m[2]), "live_cancels_per_event": float(m[3]),
            "live_share_of_cancels": float(m[4]), "live_orders": float(m[5])}


def facts(clean: bool, n_events=FACTS_EVENTS, seed=FACTS_SEED) -> dict:
    """Sections 1-4 of the notes, one way or the other."""
    book = LimitOrderBook()
    seed_book(book, mid=100.0, levels=5, qty=100)
    res = simulate(book, n_events=n_events, seed=seed, clean_stale=clean)
    spreads = np.array(res["spreads"])
    depth = {side.name.lower(): book.depth(side, 8) for side in (Side.BUY, Side.SELL)}
    hs = [hurst_exponent(simulate(seeded(), 8_000, seed=s, clean_stale=clean)["mids"])
          for s in range(1, 6)]
    return {"n_trades": res["n_trades"], "volume": res["volume"],
            "live_share_of_cancels": res["live_cancels"] / res["cancel_events"],
            "spread_mean": float(spreads.mean()),
            "spread_at_floor": float((spreads <= 0.0100001).mean()),
            "depth": depth, "touch_depth": (depth["buy"][0][1] + depth["sell"][0][1]) / 2,
            "hump": max(q for side in depth.values() for _, q in side),
            "impact_exponent": impact_exponent(res["impacts"]),
            "hurst": hurst_exponent(res["mids"]),
            "hurst_seeds_1_5": hs, "live_orders": len(book._by_id)}


def seeded():
    book = LimitOrderBook()
    seed_book(book, mid=100.0, levels=5, qty=100)
    return book


def row(buckets, lo):
    return next((r for r in buckets if r["lo"] == lo), None)


def crossing_rate(points: list[tuple[float, float]], target: float = 1.0):
    """Rate at which the ratio crosses `target`, interpolated between rates."""
    for (r0, v0), (r1, v1) in zip(points, points[1:]):
        if (v0 - target) * (v1 - target) <= 0 and v0 != v1:
            return r0 + (target - v0) / (v1 - v0) * (r1 - r0)
    return None


def rate_verdicts(cells: dict, base: dict, flows: dict, fact: dict) -> list[dict]:
    """Score the docstring's predictions. cells maps cell names to the short
    table; base holds sections 9 and 10's (0.15) tables; flows the warm-up
    flow stats; fact the stylized facts, default against clean."""
    foc = [(0.15, deep_ratio(base["0.15_0.3"]))] + \
          [(r, deep_ratio(cells[cell_name(r, FOCUS, False)])) for r in sorted(RATES + EXTRA)]
    cross = crossing_rate(foc)
    uni45 = deep_ratio(cells["0.45_1.0"])
    melts = {r: row(cells[cell_name(r, FOCUS, False)], 20_000)["cancelled_share"] for r in RATES}
    fills = {r: row(cells[cell_name(r, FOCUS, False)], 20_000)["melt_fill_rate"] for r in RATES}
    held = [row(base["0.15_0.3"], 5_000)["held_fill_rate"]] + \
           [row(cells[cell_name(r, FOCUS, False)], 5_000)["held_fill_rate"] for r in RATES]
    spreads = [flows["0.15_0.3"]["spread_ticks"]] + \
              [flows[cell_name(r, FOCUS, False)]["spread_ticks"] for r in RATES]
    clean, dirty = cells["0.15_0.3_clean"], base["0.15_0.3"]
    d_melt = row(clean, 20_000)["cancelled_share"] - row(dirty, 20_000)["cancelled_share"]
    d_ratio = deep_ratio(clean) - deep_ratio(dirty)
    f0, f1 = fact["default"], fact["clean"]
    expect_melt = {0.25: 0.30, 0.35: 0.40, 0.45: 0.50}
    pct = lambda d: {k: f"{v:.1%}" for k, v in d.items()}
    return [
        {"claim": "the ratio from 5,000 back at focus 0.3 falls as (1-p)/p - "
                  "about 1.7, 1.1, 0.7 at 0.25, 0.35, 0.45 - and inverts "
                  "between 0.30 and 0.40",
         "measured": f"{[(r, round(v, 2)) for r, v in foc]}, crosses 1 at "
                     f"{cross if cross is None else round(cross, 3)}",
         "held": cross is not None and 0.30 <= cross <= 0.40
                 and dict(foc)[RATES[-1]] < 1.0},
        {"claim": "under uniform cancels the ratio does not invert at 0.45, "
                  "about 5: the rate alone is not enough",
         "measured": f"uniform 0.45: {uni45:.2f}",
         "held": uni45 is not None and uni45 > 2.0},
        {"claim": "the 20,000+ melt at 1,000 events rises less than "
                  "proportionally: about 30%, 40%, 50%, each within ten points",
         "measured": f"{pct(melts)}",
         "held": all(abs(melts[r] - expect_melt[r]) <= 0.10 for r in RATES)},
        {"claim": "20,000+ still never fills at 1,000 events at any rate",
         "measured": f"{pct(fills)}",
         "held": all(v == 0.0 for v in fills.values())},
        {"claim": "the held fill rate from 5,000-20,000 back falls with the "
                  "rate at every step, to under 45% at 0.45",
         "measured": f"{[f'{h:.0%}' for h in held]} at 0.15, 0.25, 0.35, 0.45",
         "held": all(a > b for a, b in zip(held, held[1:])) and held[-1] < 0.45},
        {"claim": "the spread widens with the rate, past 2.5 ticks at 0.45",
         "measured": f"{[round(s, 2) for s in spreads]} ticks",
         "held": all(a < b for a, b in zip(spreads, spreads[1:])) and spreads[-1] > 2.5},
        {"claim": "clean warm books move section 10's (0.15, 0.3) cell by "
                  "little: 20,000+ melt up under five points, ratio down under one",
         "measured": f"melt {d_melt:+.1%}, ratio {d_ratio:+.2f}",
         "held": 0 <= d_melt < 0.05 and -1.0 < d_ratio <= 0},
        {"claim": "at the true 15%: spread mean within 10% and floor share "
                  "within five points; hump shrinks more than five-fold; impact "
                  "exponent rises but stays under 1; H rises but stays under 0.5",
         "measured": f"spread {f0['spread_mean']:.4f} -> {f1['spread_mean']:.4f}, "
                     f"floor {f0['spread_at_floor']:.1%} -> {f1['spread_at_floor']:.1%}, "
                     f"hump {f0['hump']:,} -> {f1['hump']:,}, impact "
                     f"{f0['impact_exponent']:.2f} -> {f1['impact_exponent']:.2f}, "
                     f"H {f0['hurst']:.2f} -> {f1['hurst']:.2f}",
         "held": (abs(f1["spread_mean"] / f0["spread_mean"] - 1) < 0.10
                  and abs(f1["spread_at_floor"] - f0["spread_at_floor"]) < 0.05
                  and f1["hump"] * 5 < f0["hump"]
                  and f0["impact_exponent"] < f1["impact_exponent"] < 1.0
                  and f0["hurst"] < f1["hurst"] < 0.5)},
    ]


def report(seeds) -> None:
    base = {"0.15_1.0": json.load(open(UNIFORM))["short"]["buckets"],
            "0.15_0.3": json.load(open(FOCUSED))["focused"]["0.3"]["short"]["buckets"]}
    cells = {cell_name(*c): melt_buckets(load_grid(*c)) for c in CELLS}
    flows = json.load(open(f"{PARTS}/flow.json"))
    fact = json.load(open(f"{PARTS}/facts.json"))

    for name, rows in cells.items():
        print(f"\ncell (rate, focus{', clean' if 'clean' in name else ''}) = {name}")
        print_buckets(rows, HORIZON)

    print(f"\n{'':>22}" + "".join(f"{n:>16}" for n in [*base, *cells]))
    for lo in (5_000, 20_000):
        print(f"{f'{lo:,}+ melt':>22}"
              + "".join(f"{row(t, lo)['cancelled_share']:>16.1%}"
                        for t in [*base.values(), *cells.values()]))
        print(f"{'   fills held / melt':>22}"
              + "".join(f"{row(t, lo)['held_fill_rate']:>7.0%}/{row(t, lo)['melt_fill_rate']:<8.0%}"
                        for t in [*base.values(), *cells.values()]))
    print(f"{'traded : cancelled':>22}"
          + "".join(f"{(deep_ratio(t) or float('nan')):>16.2f}"
                    for t in [*base.values(), *cells.values()]))
    print(f"{'warm spread, ticks':>22}"
          + "".join(f"{flows[n]['spread_ticks']:>16.2f}" for n in [*base, *cells]))
    print(f"{'live orders, warm':>22}"
          + "".join(f"{flows[n]['live_orders']:>16,.0f}" for n in [*base, *cells]))

    print("\nStylized facts, 50,000 events, seed 7: default list / clean list")
    for key in ("n_trades", "live_share_of_cancels", "spread_mean", "spread_at_floor",
                "touch_depth", "hump", "impact_exponent", "hurst", "live_orders"):
        print(f"{key:>22} {fact['default'][key]:>14,.4f} {fact['clean'][key]:>14,.4f}")
    print(f"{'H, seeds 1-5 (8k)':>22} "
          f"{[round(h, 2) for h in fact['default']['hurst_seeds_1_5']]} / "
          f"{[round(h, 2) for h in fact['clean']['hurst_seeds_1_5']]}")

    verdicts = rate_verdicts(cells, base, flows, fact)
    print("\nPredictions, scored:")
    for v in verdicts:
        print(f"  [{'held' if v['held'] else 'FAILED'}] {v['claim']}\n"
              f"         measured: {v['measured']}")

    foc = [(0.15, deep_ratio(base["0.15_0.3"]))] + \
          [(r, deep_ratio(cells[cell_name(r, FOCUS, False)])) for r in sorted(RATES + EXTRA)]
    with open(OUT, "w") as fh:
        json.dump({"rates": sorted(RATES + EXTRA), "predicted_rates": list(RATES), "focus": FOCUS, "n_seeds": len(seeds),
                   "horizon": HORIZON, "base_from": [UNIFORM, FOCUSED],
                   "cells": {n: {"horizon": HORIZON, "buckets": t} for n, t in cells.items()},
                   "flow": flows, "facts": fact,
                   "ratio_by_rate_focus_0.3": foc,
                   "inversion_rate": crossing_rate(foc),
                   "verdicts": verdicts}, fh, indent=2)
    print(f"\nwrote {OUT}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--part", required=True,
                        choices=["grid", "flow", "facts", "report"])
    parser.add_argument("--rate", type=float)
    parser.add_argument("--focus", type=float, default=FOCUS)
    parser.add_argument("--clean", action="store_true")
    parser.add_argument("--pad", type=int)
    parser.add_argument("--chunk", type=int, default=0,
                        help=f"which third of the seeds, 0-{N_CHUNKS - 1}")
    parser.add_argument("--quick", action="store_true",
                        help="12 seeds instead of 45; a smoke test, not a result")
    args = parser.parse_args()
    seeds = range(12) if args.quick else range(45)
    os.makedirs(PARTS, exist_ok=True)

    if args.part == "grid":
        run_grid(args.rate, args.focus, args.clean, args.pad, args.chunk, seeds)
    elif args.part == "flow":
        path = f"{PARTS}/flow.json"
        flows = json.load(open(path)) if os.path.exists(path) else {}
        flows[cell_name(args.rate, args.focus, args.clean)] = \
            flow_stats(args.rate, args.focus, args.clean)
        json.dump(flows, open(path, "w"), indent=2)
        print(flows[cell_name(args.rate, args.focus, args.clean)])
    elif args.part == "facts":
        out = {"default": facts(False), "clean": facts(True)}
        json.dump(out, open(f"{PARTS}/facts.json", "w"), indent=2)
        for k in ("spread_mean", "spread_at_floor", "hump", "impact_exponent", "hurst"):
            print(k, out["default"][k], out["clean"][k])
    else:
        report(seeds)


if __name__ == "__main__":
    main()
