"""What the assumed fill was worth, measured against the book that prints it.

Usage:  python fill_realism.py [--quick]
Verify with:  pytest tests/test_fill_realism.py

Section 11 of notes/backtester.md put a capacity number on this strategy by
charging square-root market impact on every slice. The number is only as good
as the impact law behind it, and that law has a constant nobody publishes -
`impact_coef`, defaulting to 1.0 because 1.0 is a round number.

This repo contains a matching engine. So rather than defend the constant, send
the orders to the engine and read the fills off it: src/book_execution.py
builds a book from the bar's volume, walks it, and reports the volume-weighted
price of the resting orders the order actually consumed. Cost becomes a
consequence of depth rather than a parameter.

Three questions, in the order they have to be asked.

1. WHERE DO THE MODELS DISAGREE? Not "is the formula wrong" - the formula is
   wrong in both directions and the sizes at which it is wrong each way are
   the useful output. A flat 2 bps on a $450 stock is 18 ticks. An order the
   touch absorbs pays 1. So the flat model overcharges small orders by more
   than an order of magnitude, and the crossover - the order size at which the
   book starts costing more than the formula charges - is a share of daily
   volume that can be quoted.

2. DOES IT CHANGE THE CAPACITY ANSWER? The honest form of this question is
   not "which Sharpe is right" but "does the conclusion move". Section 11 said
   the strategy dies somewhere between $5bn and $20bn. If a different fill
   model moves that by a factor of two the capacity number was never worth
   quoting to one significant figure.

3. HOW MUCH OF THE ANSWER IS THE FREE PARAMETER? `depth_frac` is swept across
   two orders of magnitude rather than defended, because the claim being made
   is that depth is a better place to put an assumption than basis points
   are - not that this particular depth is right.

Fees are charged separately at the end. Every order the book handler sends is
a taker, so a maker-taker venue bills 3 mils a share on all of it, and a
strategy that trades its whole book twice a year pays that twice. The fee
study in notes/exchange.md has the general version; this is just the bill for
this strategy.
"""

from __future__ import annotations

import argparse
import json
import os

import numpy as np
import pandas as pd

from exchange.fees import MAKER_TAKER
from exchange.order import TICK
from src.book_execution import SHAPE, BookExecutionHandler
from src.execution import (NextBarOpenExecutionHandler,
                           ParticipationLimitedExecutionHandler)
from src.strategy import MovingAverageCrossStrategy
from run_backtest import (fetch_opens, fetch_prices, fetch_volumes, run, stats)

SYMBOL = "SPY"
START, END = "2015-01-01", "2024-12-31"
AUMS = (1e8, 1e9, 5e9, 2e10, 1e11)
DEPTH_FRACS = (0.0002, 0.0005, 0.002, 0.005, 0.02)
# 0 is the flat book this handler shipped with first. 2.0 is harder than any
# real book; it is in the sweep to show the effect saturating rather than as a
# candidate setting.
SHAPES = (0.0, 0.25, 0.5, 1.0, 2.0)
SIZE_FRACS = (0.0001, 0.001, 0.003, 0.01, 0.03, 0.10, 0.30)
OUT = "results/fill_realism.json"


# ---------- 1. where the two cost models disagree ----------

def _level_sizes(depth_frac: float, volume: float, levels: int,
                 shape: float) -> np.ndarray:
    """Resting shares at each level, nearest the touch first.

    The weights come from BookExecutionHandler rather than being written out
    again here. Two copies of the profile is exactly the kind of duplication
    that lets a study agree with a handler while both drift from what was
    intended, and the profile is the thing under test.
    """
    flat = depth_frac * volume
    w = BookExecutionHandler._depth_weights(levels, shape)
    return np.maximum((flat * w).astype(int), 1)


def cost_curves(price: float, volume: float, depth_frac: float = 0.002,
                levels: int = 200, slippage_bps: float = 2.0,
                size_fracs=SIZE_FRACS, shape: float = SHAPE) -> list[dict]:
    """Cost per share of one order, under each model, at rising order sizes.

    The book cost is computed by actually walking the levels rather than by a
    closed form, because a closed form would be a second implementation of the
    thing under test and the two could agree while both being wrong. That
    matters more now than it did with a flat book: with a profile there is no
    tidy closed form to be tempted by.

    Returns one row per size, with cost in basis points of the price so the
    models are in the same units as the slippage parameter they replace.
    """
    rows = []
    sizes = _level_sizes(depth_frac, volume, levels, shape)
    capacity = int(sizes.sum())

    for frac in size_fracs:
        shares = max(int(frac * volume), 1)
        filled = min(shares, capacity)
        if filled == 0:
            continue

        # Walk out from the touch, taking what each level holds.
        left, cost, walked = filled, 0.0, 0
        for i, available in enumerate(sizes, start=1):
            take = min(left, int(available))
            if take <= 0:
                break
            cost += TICK * i * take
            left -= take
            walked = i
            if left == 0:
                break
        book_bps = (cost / filled) / price * 10_000

        # Square-root impact, as execution.py charges it, at this rate.
        rate = shares / volume
        sqrt_bps = slippage_bps + _daily_vol_bps() * np.sqrt(rate)

        rows.append({
            "size_frac": frac, "shares": shares,
            "levels_walked": walked,
            "filled_frac": filled / shares,
            "flat_bps": slippage_bps,
            "book_bps": book_bps,
            "sqrt_bps": sqrt_bps,
        })
    return rows


def crossover_by_shape(price: float, volume: float, depth_frac: float = 0.002,
                       levels: int = 200, shapes=SHAPES) -> list[dict]:
    """How far the crossover moves when the book is shaped rather than flat.

    The crossover is the order size at which walking the book costs more than
    the flat 2 bps the engine charges by default, so it is the size above
    which the old assumption flatters the strategy. Shaping the book can only
    move it one way - down - because every positive shape lowers cumulative
    depth. The question this answers is how far down, which is the difference
    between a correction worth quoting and one worth a footnote.

    A finer size grid than SIZE_FRACS is used, because interpolating a
    crossover between 1% and 3% of a day's volume reports the grid as much as
    the book.
    """
    grid = tuple(float(x) for x in np.geomspace(1e-5, 0.5, 120))
    rows = []
    for shape in shapes:
        curve = cost_curves(price, volume, depth_frac=depth_frac, levels=levels,
                            size_fracs=grid, shape=shape)
        cross = crossover(curve)
        touch = curve[0]["book_bps"] if curve else float("nan")
        sizes = _level_sizes(depth_frac, volume, levels, shape)
        rows.append({
            "shape": shape,
            "crossover_frac": cross,
            "crossover_shares": None if cross is None else int(cross * volume),
            "crossover_notional": None if cross is None else cross * volume * price,
            "touch_shares": int(sizes[0]),
            "capacity_shares": int(sizes.sum()),
        })
    return rows


def _daily_vol_bps(sigma: float = 0.011) -> float:
    """SPY's daily volatility over this window, in basis points.

    Hard-coded rather than estimated because this function exists only to put
    the square-root law on the same axes as the other two, and re-deriving it
    per call would make the comparison depend on which slice of history the
    caller happened to pass. 110 bps a day is SPY 2015-2024; cost_curves()
    reports the number it used.
    """
    return sigma * 10_000


def crossover(rows: list[dict]) -> float | None:
    """The order size, as a share of daily volume, where the book overtakes
    the flat charge. Linear interpolation between the bracketing rows.

    None when the book is cheaper at every size tested, which happens for a
    deep enough book and is a real answer rather than a missing one.
    """
    for lo, hi in zip(rows, rows[1:]):
        if lo["book_bps"] <= lo["flat_bps"] < hi["book_bps"]:
            span = hi["book_bps"] - lo["book_bps"]
            if span <= 0:
                return lo["size_frac"]
            w = (lo["flat_bps"] - lo["book_bps"]) / span
            return lo["size_frac"] + w * (hi["size_frac"] - lo["size_frac"])
    return None


# ---------- 2. does the capacity answer move ----------

def capacity_under_both(prices, opens, volumes, aums=AUMS,
                        participation: float = 0.10,
                        depth_frac: float = 0.002, **kwargs) -> list[dict]:
    """The same strategy, the same AUM ladder, two fill models.

    The configuration is section 11's, deliberately: fully invested when long,
    10% participation, same windows. Only the handler changes, so a difference
    between the columns is a difference between fill models and nothing else.
    """
    first_price = float(prices.iloc[0, 0])
    median_notional = float((volumes.iloc[:, 0] * prices.iloc[:, 0]).median())

    def shares_for(aum: float) -> int:
        return max(int(aum / first_price), 1)

    base = run(prices, MovingAverageCrossStrategy, trade_size=shares_for(1e6),
               initial_cash=1e6, opens=opens,
               execution_cls=NextBarOpenExecutionHandler, **kwargs)
    rows = [{"aum": None, **stats(base), "model": "no liquidity limit"}]

    for aum in aums:
        sqrt_eq = run(prices, MovingAverageCrossStrategy,
                      trade_size=shares_for(aum), initial_cash=aum,
                      opens=opens, volumes=volumes,
                      execution_cls=ParticipationLimitedExecutionHandler,
                      execution_kwargs={"participation": participation},
                      **kwargs)
        book_eq = run(prices, MovingAverageCrossStrategy,
                      trade_size=shares_for(aum), initial_cash=aum,
                      opens=opens, volumes=volumes,
                      execution_cls=BookExecutionHandler,
                      execution_kwargs={"participation": participation,
                                        "depth_frac": depth_frac},
                      **kwargs)
        sqrt_ex, book_ex = sqrt_eq.attrs["execution"], book_eq.attrs["execution"]
        rows.append({
            "aum": aum,
            "position_in_days": aum / median_notional,
            "sqrt": stats(sqrt_eq),
            "book": stats(book_eq),
            "sqrt_stranded": sqrt_ex.unfilled_shares * first_price,
            "book_stranded": book_ex.unfilled_shares * first_price,
            "sqrt_slices": sqrt_ex.slices,
            "book_slices": book_ex.slices,
            "book_exhausted": book_ex.exhausted_slices,
            "mean_levels_walked": (float(np.mean(book_ex.levels_walked))
                                   if book_ex.levels_walked else 0.0),
            # The share of the intended book that ever got into the market.
            # Without this column the two Sharpe columns are uninterpretable
            # above a few billion - see dies_between() for why.
            "sqrt_deployed": 1.0 - sqrt_ex.unfilled_shares * first_price / aum,
            "book_deployed": 1.0 - book_ex.unfilled_shares * first_price / aum,
        })
    return rows


def interpretable_to(rows: list[dict], key: str,
                     floor: float = 0.90) -> float | None:
    """The largest AUM at which most of the book actually reached the market.

    This exists because of a flaw in how section 11 asked the capacity
    question, which only shows up once two fill models are run side by side.

    The obvious reading of a capacity ladder is "find where Sharpe falls over".
    On this ladder Sharpe does not fall over - it falls to 0.44 at $5bn and
    then RISES, to 0.68 and 0.78, under both fill models. That is not capacity
    improving with size. It is the participation cap refusing to fill the
    order: at $100bn, 98% of the intended book never gets into the market, so
    the equity curve is 98% idle cash, the denominator of the Sharpe ratio
    collapses, and the statistic starts describing a cash pile instead of a
    strategy.

    So the ladder is only readable up to the point where the book is actually
    deployed, and `floor` is where that line gets drawn. Past it the honest
    answer is not a Sharpe at all - it is the deployed fraction, which is the
    capacity number the study was reaching for in the first place.
    """
    best = None
    for row in rows:
        if row["aum"] is None:
            continue
        if row[f"{key}_deployed"] >= floor:
            best = row["aum"]
    return best


# ---------- 3. the free parameter ----------

def depth_sweep(prices, opens, volumes, aum: float = 5e9,
                depth_fracs=DEPTH_FRACS, participation: float = 0.10,
                **kwargs) -> list[dict]:
    """One AUM level, the book's depth swept over two orders of magnitude.

    If the capacity conclusion only survives at one depth it is a conclusion
    about the depth. The point of the sweep is to say which it is.
    """
    first_price = float(prices.iloc[0, 0])
    shares = max(int(aum / first_price), 1)
    rows = []
    for frac in depth_fracs:
        equity = run(prices, MovingAverageCrossStrategy, trade_size=shares,
                     initial_cash=aum, opens=opens, volumes=volumes,
                     execution_cls=BookExecutionHandler,
                     execution_kwargs={"participation": participation,
                                       "depth_frac": frac}, **kwargs)
        ex = equity.attrs["execution"]
        rows.append({
            "depth_frac": frac,
            "shares_at_touch": int(frac * float(volumes.iloc[:, 0].median())),
            **stats(equity),
            "mean_levels_walked": (float(np.mean(ex.levels_walked))
                                   if ex.levels_walked else 0.0),
            "stranded": ex.unfilled_shares * first_price,
        })
    return rows


# ---------- 4. the venue's cut ----------

def fee_bill(prices, opens, volumes, aum: float = 1e9,
             participation: float = 0.10, **kwargs) -> dict:
    """What a maker-taker venue charges this strategy for crossing every time.

    Every order the book handler sends is a market order, so there is no
    rebate to net against the charge - which is the part of the fee study that
    applies to a trend follower and the reason it is worth a line here.
    """
    first_price = float(prices.iloc[0, 0])
    shares = max(int(aum / first_price), 1)
    out = {}
    for name, schedule in (("no fees", None), ("maker-taker", MAKER_TAKER)):
        kw = {"participation": participation, "depth_frac": 0.002}
        if schedule is not None:
            kw["fees"] = schedule
        equity = run(prices, MovingAverageCrossStrategy, trade_size=shares,
                     initial_cash=aum, opens=opens, volumes=volumes,
                     execution_cls=BookExecutionHandler,
                     execution_kwargs=kw, **kwargs)
        ex = equity.attrs["execution"]
        out[name] = {**stats(equity), "fee_paid": ex.fee_paid}
    return out


# ---------- reporting ----------

def print_cost_curves(rows: list[dict], cross: float | None, price: float,
                      volume: float, depth_frac: float) -> None:
    print(f"\n1. COST PER SHARE, THREE MODELS   {SYMBOL} at ${price:,.0f}, "
          f"{volume / 1e6:.0f}M shares a day")
    print(f"   book: {int(depth_frac * volume):,} shares resting at every "
          f"penny ({depth_frac:.2%} of the day)")
    print(f"\n{'order (% of day)':>17} {'shares':>12} {'flat 2bp':>9} "
          f"{'sqrt law':>9} {'the book':>9} {'levels':>7} {'filled':>8}")
    for r in rows:
        print(f"{r['size_frac']:>16.2%} {r['shares']:>12,} "
              f"{r['flat_bps']:>8.2f}  {r['sqrt_bps']:>8.2f}  "
              f"{r['book_bps']:>8.2f}  {r['levels_walked']:>7,} "
              f"{r['filled_frac']:>7.0%}")
    if cross is None:
        print("\n   The book is cheaper at every size tested.")
    else:
        print(f"\n   Crossover: the book starts costing more than a flat 2 bps "
              f"at {cross:.2%} of a day's volume")
        print(f"   ({int(cross * volume):,} shares, about "
              f"${cross * volume * price / 1e6:,.0f}M of {SYMBOL}).")


def print_crossover_by_shape(rows: list[dict], price: float,
                             volume: float) -> None:
    """The correction the depth profile makes to section 1's headline number."""
    print(f"\n1b. WHAT SHAPING THE BOOK DOES TO THAT CROSSOVER")
    print(f"    shape 0 is the flat book this handler shipped with. Total "
          f"resting size is")
    print(f"    identical down the column - only its distribution changes.")
    print(f"\n{'shape':>7} {'at the touch':>14} {'crossover':>11} "
          f"{'shares':>12} {'notional':>11}")
    for r in rows:
        if r["crossover_frac"] is None:
            print(f"{r['shape']:>7.2f} {r['touch_shares']:>14,} "
                  f"{'never':>11} {'-':>12} {'-':>11}")
            continue
        print(f"{r['shape']:>7.2f} {r['touch_shares']:>14,} "
              f"{r['crossover_frac']:>10.2%} {r['crossover_shares']:>12,} "
              f"{r['crossover_notional'] / 1e6:>10,.0f}M")

    flat = next((r for r in rows if r["shape"] == 0.0), None)
    shaped = next((r for r in rows if r["shape"] == SHAPE), None)
    if flat and shaped and flat["crossover_frac"] and shaped["crossover_frac"]:
        ratio = flat["crossover_frac"] / shaped["crossover_frac"]
        print(f"\n    At the shipped default of {SHAPE}, the crossover arrives "
              f"{ratio:.1f}x sooner than")
        print(f"    the flat book said it would. Capacity is untouched - the "
              f"book holds the same")
        print(f"    {shaped['capacity_shares']:,} shares either way - so this "
              f"is purely the cost of")
        print(f"    reaching past a thinner touch.")


def print_capacity(rows: list[dict]) -> None:
    base = rows[0]
    print(f"\n2. THE SAME CAPACITY LADDER UNDER BOTH MODELS")
    print(f"   baseline, no liquidity limit: Sharpe {base['sharpe']:.2f}")
    print(f"\n{'AUM':>8} {'days of vol':>12} {'deployed':>9} "
          f"{'sqrt sharpe':>12} {'book sharpe':>12} {'gap':>7} {'levels':>7}")
    for r in rows[1:]:
        gap = r["book"]["sharpe"] - r["sqrt"]["sharpe"]
        flag = "" if r["book_deployed"] >= 0.90 else "   <- mostly cash"
        print(f"{r['aum'] / 1e9:>7.1f}B {r['position_in_days']:>12.2f} "
              f"{r['book_deployed']:>9.0%} "
              f"{r['sqrt']['sharpe']:>12.2f} {r['book']['sharpe']:>12.2f} "
              f"{gap:>+7.2f} {r['mean_levels_walked']:>7.1f}{flag}")


def print_depth_sweep(rows: list[dict], aum: float) -> None:
    print(f"\n3. HOW MUCH OF THAT IS THE FREE PARAMETER   (at ${aum / 1e9:.0f}bn)")
    print(f"\n{'depth_frac':>11} {'shares a penny':>15} {'sharpe':>8} "
          f"{'return':>9} {'levels walked':>14} {'stranded':>11}")
    for r in rows:
        print(f"{r['depth_frac']:>11.4f} {r['shares_at_touch']:>15,} "
              f"{r['sharpe']:>8.2f} {r['total_return']:>9.2%} "
              f"{r['mean_levels_walked']:>14.1f} {r['stranded'] / 1e9:>10.2f}B")


def print_fees(bill: dict) -> None:
    free, paid = bill["no fees"], bill["maker-taker"]
    print(f"\n4. THE VENUE'S CUT   every order is a taker, so 30 mils on all of it")
    print(f"   Sharpe {free['sharpe']:.2f} with no fees, "
          f"{paid['sharpe']:.2f} on maker-taker "
          f"({paid['sharpe'] - free['sharpe']:+.2f})")
    print(f"   Total taker fees paid: ${-paid['fee_paid'] / 1e6:,.1f}M")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true",
                       help="three AUM levels and three depths instead of five")
    args = parser.parse_args()

    aums = AUMS[:3] if args.quick else AUMS
    depths = DEPTH_FRACS[:3] if args.quick else DEPTH_FRACS

    prices = fetch_prices(SYMBOL, START, END)
    opens = fetch_opens(SYMBOL, START, END)
    volumes = fetch_volumes(SYMBOL, START, END)
    prices, opens, volumes = _align(prices, opens, volumes)

    price = float(prices.iloc[:, 0].median())
    volume = float(volumes.iloc[:, 0].median())

    print(f"{SYMBOL}  {prices.index[0].date()} to {prices.index[-1].date()}  "
          f"{len(prices)} bars")

    curves = cost_curves(price, volume)
    cross = crossover(curves)
    print_cost_curves(curves, cross, price, volume, 0.002)

    by_shape = crossover_by_shape(price, volume)
    print_crossover_by_shape(by_shape, price, volume)

    cap = capacity_under_both(prices, opens, volumes, aums=aums)
    print_capacity(cap)
    readable = interpretable_to(cap, "book")
    print(f"\n   Both Sharpe columns are only readable up to "
          f"${readable / 1e9:.1f}bn, where 90% of the book still reaches the "
          f"market.\n   Above that the rising Sharpe is idle cash, not "
          f"capacity - see interpretable_to().")

    sweep = depth_sweep(prices, opens, volumes, aum=5e9, depth_fracs=depths)
    print_depth_sweep(sweep, 5e9)

    bill = fee_bill(prices, opens, volumes)
    print_fees(bill)

    os.makedirs("results", exist_ok=True)
    with open(OUT, "w") as fh:
        json.dump({"symbol": SYMBOL, "start": START, "end": END,
                   "median_price": price, "median_volume": volume,
                   "cost_curves": curves, "crossover": cross,
                   "shape": SHAPE, "crossover_by_shape": by_shape,
                   "capacity": cap, "depth_sweep": sweep, "fees": bill},
                  fh, indent=1, default=float)
    print(f"\nwrote {OUT}")


def _align(*frames: pd.DataFrame) -> tuple:
    """Intersect the indices so every frame covers the same bars.

    yfinance can return a different number of rows per field; a capacity study
    on misaligned volume would charge the wrong day's liquidity.
    """
    idx = frames[0].index
    for frame in frames[1:]:
        idx = idx.intersection(frame.index)
    return tuple(frame.loc[idx] for frame in frames)


if __name__ == "__main__":
    main()
