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
from src.book_execution import (PERSISTENCE, RECOVERY, SHAPE, VOL_ELASTICITY,
                                VOL_WINDOW, BookExecutionHandler)
from src.data_handler import HistoricalDataHandler
from src.events import OrderEvent
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
# Where the depth profile turns over, in levels. None is the ramp the handler
# ships with; 20 puts the peak ten levels out and empties the far book. The
# sweep exists to show how much the crossover depends on a parameter nothing
# in this repository can observe, not to pick one.
DECAYS = (None, 80.0, 40.0, 20.0)
# How much of a slice's displacement the next bar's book still carries. 0 is
# the memoryless book every section before 5 used; 1 carries all of it, which
# is past any published estimate and is in the sweep as the ceiling.
PERSISTENCES = (0.0, 0.33, 0.67, 1.0)
# Half-life of the carried displacement, in bars. None is Almgren's permanent
# impact, which the round trip gets back; 5 bars is a book that heals in a
# week, which it does not; 20 is a month, in between.
RECOVERIES = (None, 5.0, 20.0)
# 0 is the constant-depth book. 1.0 is the shipped default and the textbook
# relationship; 2.0 is past anything measured and is there to show the effect
# growing smoothly rather than as a candidate setting.
ELASTICITIES = (0.0, 0.5, 1.0, 2.0)
# Whole orders as multiples of a day's volume, for section 6. Up to 0.1 fits
# in one bar at 10% participation; 3.0 is thirty slices, which is the $5bn
# position from section 2 being built.
METAORDER_FRACS = (0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 3.0)
OUT = "results/fill_realism.json"


# ---------- 1. where the two cost models disagree ----------

def _level_sizes(depth_frac: float, volume: float, levels: int,
                 shape: float, decay: float | None = None) -> np.ndarray:
    """Resting shares at each level, nearest the touch first.

    The weights come from BookExecutionHandler rather than being written out
    again here. Two copies of the profile is exactly the kind of duplication
    that lets a study agree with a handler while both drift from what was
    intended, and the profile is the thing under test.
    """
    flat = depth_frac * volume
    w = BookExecutionHandler._depth_weights(levels, shape, decay)
    return np.maximum((flat * w).astype(int), 1)


def cost_curves(price: float, volume: float, depth_frac: float = 0.002,
                levels: int = 200, slippage_bps: float = 2.0,
                size_fracs=SIZE_FRACS, shape: float = SHAPE,
                decay: float | None = None) -> list[dict]:
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
    sizes = _level_sizes(depth_frac, volume, levels, shape, decay)
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


def crossover_by_decay(price: float, volume: float, depth_frac: float = 0.002,
                       levels: int = 200, shape: float = SHAPE,
                       decays=DECAYS, size_fracs=SIZE_FRACS) -> list[dict]:
    """How far the crossover moves when the far book is allowed to thin out.

    The ramp profile has its heaviest level at the far edge of the book, which
    is the right shape at the touch and the wrong one everywhere behind it -
    nobody rests size two dollars from a one-cent market. Letting the profile
    decay past a peak fixes that, and this measures what fixing it does to
    the number the first section quotes.

    It does a lot, and in the direction `shape` was expected to go and could
    not. Holding the total fixed, moving size out of the tail and into the
    middle raises cumulative depth at every level a mid-sized order reaches,
    so large orders get cheaper - cheaper than the flat book, past a point.
    The crossover climbs with it. Which means the crossover is a function of
    a parameter this repository has no data to pin, and the sweep is here as
    the error bar on it rather than as a search for the right value.
    """
    rows = []
    for decay in decays:
        grid = tuple(float(x) for x in np.geomspace(1e-5, 0.5, 120))
        curve = cost_curves(price, volume, depth_frac=depth_frac, levels=levels,
                            size_fracs=grid, shape=shape, decay=decay)
        coarse = cost_curves(price, volume, depth_frac=depth_frac,
                             levels=levels, size_fracs=size_fracs, shape=shape,
                             decay=decay)
        cross = crossover(curve)
        w = BookExecutionHandler._depth_weights(levels, shape, decay)
        cumulative = np.cumsum(w) / w.sum()
        rows.append({
            "decay": decay,
            "peak_level": int(np.argmax(w)) + 1,
            "touch_multiple": float(w[0]),
            "half_depth_level": int(np.searchsorted(cumulative, 0.5)) + 1,
            "far_level_multiple": float(w[-1]),
            "crossover_frac": cross,
            "crossover_notional": None if cross is None else cross * volume * price,
            "cost_bps": {f"{r['size_frac']:g}": r["book_bps"] for r in coarse},
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


# ---------- 1c. the book is not the same depth every day ----------

def depth_multipliers(prices, symbol: str = SYMBOL,
                      vol_elasticity: float = VOL_ELASTICITY,
                      vol_window: int = VOL_WINDOW) -> np.ndarray:
    """The depth multiplier the handler would apply on every bar in the sample.

    Built by stepping a data handler through the series and asking the
    execution handler the same question it asks itself, rather than
    recomputing the formula here. A second implementation of the thing under
    test is how a study ends up agreeing with a handler while both drift from
    what was meant - the same reason _level_sizes() borrows the depth profile.

    Bars before the estimator has enough history return 1.0, which is what the
    handler does with them too.
    """
    data = HistoricalDataHandler(prices)
    handler = BookExecutionHandler(data, vol_elasticity=vol_elasticity,
                                   vol_window=vol_window)
    out = []
    while data.has_more():
        data.next_bar()
        out.append(handler._trailing_vol(symbol)[0])
    return np.array(out, dtype=float)


def _weighted(values: list[float], weights: list[int]) -> float:
    """Share-weighted mean, ignoring entries the estimator could not score.

    Share-weighted rather than slice-weighted throughout, because a cost is a
    cost per share and a hundred-share slice should not count as much as a
    hundred-thousand-share one.
    """
    v = np.asarray(values, dtype=float)
    w = np.asarray(weights, dtype=float)
    ok = ~np.isnan(v)
    if not ok.any() or w[ok].sum() == 0:
        return float("nan")
    return float((v[ok] * w[ok]).sum() / w[ok].sum())


def _execution_summary(equity) -> dict:
    """Cost and conditions of the fills one run printed."""
    ex = equity.attrs["execution"]
    return {
        **stats(equity),
        "slices": ex.slices,
        "cost_bps": _weighted(ex.slice_cost_bps, ex.slice_shares),
        "vol_percentile": _weighted(ex.slice_vol_pct, ex.slice_shares),
        "multiplier": _weighted(ex.slice_multiplier, ex.slice_shares),
        "mean_levels_walked": (float(np.mean(ex.levels_walked))
                               if ex.levels_walked else 0.0),
        "deployed": ex.unfilled_shares,
    }


def vol_elasticity_study(prices, opens, volumes, aum: float = 1e9,
                         elasticities=ELASTICITIES, participation: float = 0.10,
                         depth_frac: float = 0.002, **kwargs) -> dict:
    """What it costs this strategy that depth is not the same every day.

    Sections 1 and 1b fixed WHERE the liquidity sits. This one fixes WHEN it is
    there. Depth and volatility move against each other - a maker quoting a
    fixed amount of risk rather than a fixed number of shares shows less size
    when the price is moving around more - so the constant-depth book quotes an
    average day's liquidity on every day, including the ones nobody wanted to
    quote into.

    Unlike `shape`, this knob is not one-sided: a quiet bar now gets a DEEPER
    book than before and an order placed on one fills cheaper. So whether it
    helps or hurts depends entirely on when the strategy trades, and that is a
    measurement rather than a property of the model. Hence the three numbers
    this returns together:

      - `unconditional_multiplier`: the average multiplier across every bar in
        the sample. Near 1 means the sample as a whole has not been quietly
        made thinner, so any cost difference is about timing.
      - `multiplier` in each row: the SHARE-WEIGHTED average on the bars this
        strategy actually traded. The gap between the two is the finding.
      - `level_matched`: a constant-depth book scaled down to that same
        share-weighted average. It separates trading on thinner days (which
        this run reproduces) from the dispersion around it (which it does not),
        because cost is convex in depth and the two are easy to conflate.
    """
    first_price = float(prices.iloc[0, 0])
    shares = max(int(aum / first_price), 1)

    def go(**execution_kwargs) -> dict:
        equity = run(prices, MovingAverageCrossStrategy, trade_size=shares,
                     initial_cash=aum, opens=opens, volumes=volumes,
                     execution_cls=BookExecutionHandler,
                     execution_kwargs={"participation": participation,
                                       "depth_frac": depth_frac,
                                       **execution_kwargs}, **kwargs)
        return _execution_summary(equity)

    rows = [{"elasticity": e, **go(vol_elasticity=e)} for e in elasticities]

    shipped = next((r for r in rows if r["elasticity"] == VOL_ELASTICITY), None)
    level_matched = None
    if shipped is not None and not np.isnan(shipped["multiplier"]):
        level_matched = {
            **go(vol_elasticity=0.0,
                 depth_frac=depth_frac * shipped["multiplier"]),
            # After the spread, not before: the run's own summary carries a
            # `multiplier` of 1.0 (it is a constant-depth run) and would
            # otherwise overwrite the level it was matched to.
            "matched_to": shipped["multiplier"],
        }

    all_bars = depth_multipliers(prices)
    return {
        "aum": aum,
        "vol_window": VOL_WINDOW,
        "rows": rows,
        "level_matched": level_matched,
        "unconditional_multiplier": float(all_bars.mean()),
        "unconditional_median": float(np.median(all_bars)),
    }


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


# ---------- 5. what the book remembers ----------

def memory_sweep(prices, opens, volumes, aums=(1e9, 5e9),
                 persistences=PERSISTENCES, recoveries=RECOVERIES,
                 participation: float = 0.10, depth_frac: float = 0.002,
                 **kwargs) -> list[dict]:
    """The sliced order, charged for its own earlier slices.

    Every section above rebuilds the book from scratch each bar, so a slice
    that walked twenty levels on Monday finds them all refilled at Monday's
    prices on Tuesday. That is a model with no permanent impact, and it is
    the one thing the square-root formula in execution.py has that the book
    did not - which makes it the obvious candidate for why section 2 found
    the book Sharpe ABOVE the formula's at every AUM.

    `persistence` is the share of each slice's displacement still in the mid
    at the next bar. Swept from 0 (the memoryless book every earlier table
    used) to 1 (the whole displacement carried, which is more than any
    empirical estimate), at the two AUM levels where slicing happens: $1bn
    is a few slices an order, $5bn is a position that takes weeks to build.
    `recovery` is the half-life of the carried displacement in bars; None is
    Almgren's permanent impact, which never decays.

    Each row reports the Sharpe, the share-weighted cost per slice, how much
    of that cost was the handler's own doing (the carried offset the slice
    started from), and the square-root model's Sharpe at the same AUM for
    reference. The question is whether some setting of this knob lands the
    book on the formula, and the answer is read off the table rather than
    fitted.
    """
    first_price = float(prices.iloc[0, 0])
    rows = []
    for aum in aums:
        shares = max(int(aum / first_price), 1)
        sqrt_eq = run(prices, MovingAverageCrossStrategy, trade_size=shares,
                      initial_cash=aum, opens=opens, volumes=volumes,
                      execution_cls=ParticipationLimitedExecutionHandler,
                      execution_kwargs={"participation": participation},
                      **kwargs)
        sqrt_sharpe = stats(sqrt_eq)["sharpe"]
        for recovery in recoveries:
            for persistence in persistences:
                if persistence == 0.0 and recovery is not None:
                    continue    # identical to the memoryless row
                equity = run(prices, MovingAverageCrossStrategy,
                             trade_size=shares, initial_cash=aum, opens=opens,
                             volumes=volumes, execution_cls=BookExecutionHandler,
                             execution_kwargs={"participation": participation,
                                               "depth_frac": depth_frac,
                                               "persistence": persistence,
                                               "recovery": recovery},
                             **kwargs)
                ex = equity.attrs["execution"]
                carried = _weighted(ex.slice_carried_bps, ex.slice_shares)
                rows.append({
                    "aum": aum,
                    "persistence": persistence,
                    "recovery": recovery,
                    **stats(equity),
                    "sqrt_sharpe": sqrt_sharpe,
                    "slices": ex.slices,
                    "cost_bps": _weighted(ex.slice_cost_bps, ex.slice_shares),
                    "carried_bps": carried,
                    "max_carried_bps": (float(np.max(np.abs(ex.slice_carried_bps)))
                                        if ex.slice_carried_bps else 0.0),
                    "max_delay_bars": ex.max_delay_bars,
                    "deployed": 1.0 - ex.unfilled_shares * first_price / aum,
                })
    return rows


def memory_closes_the_gap(rows: list[dict]) -> dict:
    """At each AUM and recovery, the smallest persistence whose Sharpe is at
    or below the square-root model's. None where even full persistence leaves
    the book above the formula. Keyed "<aum>/<recovery>" with the recovery
    spelled "never" for permanent impact."""
    out = {}
    for aum in sorted({r["aum"] for r in rows}):
        for recovery in sorted({r["recovery"] for r in rows},
                               key=lambda x: (x is not None, x or 0.0)):
            group = [r for r in rows if r["aum"] == aum and r["recovery"] == recovery]
            hit = [r for r in group if r["sharpe"] <= r["sqrt_sharpe"]]
            label = "never" if recovery is None else f"{recovery:g}"
            out[f"{aum:.0e}/{label}"] = (
                min(hit, key=lambda r: r["persistence"])["persistence"]
                if hit else None)
    return out


# ---------- 6. the square root was in the slice, not the order ----------

def metaorder_cost(price: float, volume: float, total_frac: float,
                   persistence: float = PERSISTENCE,
                   recovery: float | None = RECOVERY,
                   participation: float = 0.10, depth_frac: float = 0.002,
                   shape: float = SHAPE, levels: int = 200,
                   slippage_bps: float = 2.0) -> dict:
    """One order, worked to completion into a book that does not otherwise move.

    `total_frac` is the whole order as a multiple of a day's volume; at 10%
    participation an order of 0.3 days takes three bars and an order of three
    days takes thirty. The book is the handler's own, driven on a flat tape -
    the same open and the same volume every bar, no volatility - so that the
    only thing moving the price between slices is what the handler remembers
    of its own earlier slices. The number that comes out is implementation
    shortfall: the share-weighted fill against the mid the order arrived at,
    in basis points, which is the quantity the square-root law is a law
    about.

    Beside it, two formulas. `sqrt_slice_bps` is what execution.py charges for
    the same order: slippage plus sigma times the root of each SLICE's share
    of the bar, share-weighted across the slices. `sqrt_order_bps` is the law
    the handler is named after, sigma times the root of the whole ORDER's
    share of a day's volume (Almgren et al. 2005, Toth et al. 2011), with the
    same unit coefficient. The two agree only while the order fits in one
    slice; the point of the section is what happens after that.
    """
    shares = max(int(total_frac * volume), 1)
    cap = max(int(participation * volume), 1)
    bars = -(-shares // cap) + 4
    idx = pd.bdate_range("2023-01-02", periods=bars)
    flat = pd.DataFrame({SYMBOL: [price] * bars}, index=idx)
    data = HistoricalDataHandler(flat, opens=flat.copy(),
                                 volumes=pd.DataFrame({SYMBOL: [volume] * bars},
                                                      index=idx))
    ex = BookExecutionHandler(data, depth_frac=depth_frac, levels=levels,
                              participation=participation, shape=shape,
                              vol_elasticity=0.0, persistence=persistence,
                              recovery=recovery)
    data.next_bar()
    ex.execute(OrderEvent(time=data.current_time(), symbol=SYMBOL,
                          quantity=shares))
    fills = []
    while ex.working_quantity(SYMBOL) and data.has_more():
        data.next_bar()
        fills.extend(ex.pop_settled_fills(data.current_time()))
    ex.finalize()

    filled = sum(f.quantity for f in fills)
    avg = sum(f.fill_price * f.quantity for f in fills) / filled if filled else price
    sigma = _daily_vol_bps()
    slices = [cap] * (shares // cap) + ([shares % cap] if shares % cap else [])
    sqrt_slice = sum(q * (slippage_bps + sigma * np.sqrt(q / volume))
                     for q in slices) / shares
    return {
        "total_frac": total_frac, "shares": shares,
        "slices": len(fills), "filled_frac": filled / shares,
        "book_bps": (avg - price) / price * 10_000,
        "sqrt_slice_bps": sqrt_slice,
        "sqrt_order_bps": sigma * np.sqrt(total_frac),
    }


def metaorder_curves(price: float, volume: float,
                     persistences=PERSISTENCES, recoveries=RECOVERIES,
                     total_fracs=METAORDER_FRACS, **kwargs) -> list[dict]:
    """metaorder_cost at every size, for every memory setting in the sweep."""
    rows = []
    for recovery in recoveries:
        for persistence in persistences:
            if persistence == 0.0 and recovery is not None:
                continue
            for frac in total_fracs:
                rows.append({"persistence": persistence, "recovery": recovery,
                             **metaorder_cost(price, volume, frac,
                                              persistence=persistence,
                                              recovery=recovery, **kwargs)})
    return rows


def impact_exponent(sizes, costs) -> float:
    """Slope of log cost against log size: 0 is flat, 0.5 is the square-root
    law, 1 is linear. Least squares on the logs, so every point counts once
    whatever the spacing of the grid."""
    x = np.log(np.asarray(sizes, dtype=float))
    y = np.log(np.asarray(costs, dtype=float))
    x = x - x.mean()
    return float((x @ (y - y.mean())) / (x @ x))


def exponents_by_setting(rows: list[dict], participation: float = 0.10) -> list[dict]:
    """The impact exponent of each cost model, inside one slice and across many.

    Fitted separately on the orders that fit in a single bar and on the ones
    that have to be sliced, because the two regimes have different causes.
    Inside a bar the exponent is set by the depth profile - how fast
    cumulative depth grows as the order walks out. Across bars it is set by
    memory alone: a book that refills completely between slices charges
    every slice the same, so the exponent is zero whatever the profile.
    """
    out = []
    settings = []
    for r in rows:
        key = (r["persistence"], r["recovery"])
        if key not in settings:
            settings.append(key)
    for persistence, recovery in settings:
        group = [r for r in rows if (r["persistence"], r["recovery"])
                 == (persistence, recovery)]
        single = [r for r in group if r["total_frac"] <= participation]
        sliced = [r for r in group if r["total_frac"] >= participation]
        row = {"persistence": persistence, "recovery": recovery}
        for name in ("book_bps", "sqrt_slice_bps", "sqrt_order_bps"):
            row[f"{name}_single"] = impact_exponent(
                [r["total_frac"] for r in single], [r[name] for r in single])
            row[f"{name}_sliced"] = impact_exponent(
                [r["total_frac"] for r in sliced], [r[name] for r in sliced])
        # The law's coefficient this book implies: the geometric mean of
        # book cost over sigma * sqrt(size) across the sliced orders. The
        # literature puts it between a half and one.
        row["y_sliced"] = float(np.exp(np.mean(
            [np.log(r["book_bps"] / r["sqrt_order_bps"]) for r in sliced])))
        out.append(row)
    return out


def closest_to_square_root(exponents: list[dict]) -> dict:
    """The memory setting whose sliced-order exponent is nearest one half."""
    return min(exponents, key=lambda e: abs(e["book_bps_sliced"] - 0.5))


def print_metaorders(rows: list[dict], exponents: list[dict],
                     best: dict, participation: float = 0.10) -> None:
    print(f"\n6. THE SQUARE ROOT WAS IN THE SLICE, NOT THE ORDER   one order, "
          f"worked to completion on a flat tape")
    print(f"   Cost is implementation shortfall in bps against the arrival "
          f"mid. '{'formula':>7}' is what execution.py charges")
    print(f"   for the same order; 'sqrt law' is sigma * sqrt(order / day's "
          f"volume), which is what the formula is named after.")
    settings = [(e["persistence"], e["recovery"]) for e in exponents]
    fracs = sorted({r["total_frac"] for r in rows})
    head = " ".join(f"{f:>7.2f}" for f in fracs)
    print(f"\n{'persist':>8} {'recovery':>9} | {'days of volume:':>15} {head}")
    for persistence, recovery in settings:
        group = {r["total_frac"]: r for r in rows
                 if (r["persistence"], r["recovery"]) == (persistence, recovery)}
        rec = "never" if recovery is None else f"{recovery:.0f} bars"
        cells = " ".join(f"{group[f]['book_bps']:>7.1f}" for f in fracs)
        print(f"{persistence:>8.2f} {rec:>9} | {'book':>15} {cells}")
    first = {r["total_frac"]: r for r in rows
             if (r["persistence"], r["recovery"]) == settings[0]}
    print(f"{'':>8} {'':>9} | {'formula':>15} "
          + " ".join(f"{first[f]['sqrt_slice_bps']:>7.1f}" for f in fracs))
    print(f"{'':>8} {'':>9} | {'sqrt law':>15} "
          + " ".join(f"{first[f]['sqrt_order_bps']:>7.1f}" for f in fracs))

    print(f"\n   Impact exponent (slope of log cost on log size), orders that "
          f"fit in one bar | orders that are sliced:")
    print(f"{'persist':>8} {'recovery':>9} {'book':>14} {'formula':>14} "
          f"{'sqrt law':>14} {'implied Y':>10}")
    for e in exponents:
        rec = "never" if e["recovery"] is None else f"{e['recovery']:.0f} bars"
        print(f"{e['persistence']:>8.2f} {rec:>9} "
              f"{e['book_bps_single']:>6.2f} | {e['book_bps_sliced']:>5.2f} "
              f"{e['sqrt_slice_bps_single']:>6.2f} | {e['sqrt_slice_bps_sliced']:>5.2f} "
              f"{e['sqrt_order_bps_single']:>6.2f} | {e['sqrt_order_bps_sliced']:>5.2f} "
              f"{e['y_sliced']:>10.2f}")
    rec = "never" if best["recovery"] is None else f"{best['recovery']:.0f} bars"
    print(f"\n   Nearest to the square-root law across sliced orders: "
          f"persistence {best['persistence']:.2f}, recovery {rec}, "
          f"exponent {best['book_bps_sliced']:.2f}, implied Y "
          f"{best['y_sliced']:.2f}.")


# ---------- 7. charge the order, not the slice ----------

LAW_COEF = 0.5          # Toth et al. (2011) put Y between a half and one
LAW_PERSISTENCE = 0.33  # the book setting section 6 found nearest the law
LAW_RECOVERY = None     # ... with permanent memory


def formula_metaorder_cost(price: float, volume: float, total_frac: float,
                           impact_on: str = "slice", impact_coef: float = 1.0,
                           participation: float = 0.10,
                           slippage_bps: float = 2.0,
                           sigma_bps: float | None = None) -> dict:
    """One order worked to completion by the formula handler on a flat tape.

    The same loop as metaorder_cost, with ParticipationLimitedExecutionHandler
    in place of the book, so the formula row of section 6 comes from the
    handler itself rather than from the arithmetic replica that section wrote
    beside it - which is the "two copies of the mechanism" objection the
    section ends on, now closed. The tape has no volatility, so the handler
    is given its sigma rather than estimating zero from the tape.
    """
    sigma = _daily_vol_bps() if sigma_bps is None else sigma_bps
    shares = max(int(total_frac * volume), 1)
    cap = max(int(participation * volume), 1)
    bars = -(-shares // cap) + 4
    idx = pd.bdate_range("2023-01-02", periods=bars)
    flat = pd.DataFrame({SYMBOL: [price] * bars}, index=idx)
    data = HistoricalDataHandler(flat, opens=flat.copy(),
                                 volumes=pd.DataFrame({SYMBOL: [volume] * bars},
                                                      index=idx))
    ex = ParticipationLimitedExecutionHandler(
        data, participation=participation, slippage_bps=slippage_bps,
        commission_per_share=0.0, impact_coef=impact_coef,
        impact_on=impact_on, sigma_bps=sigma)
    data.next_bar()
    ex.execute(OrderEvent(time=data.current_time(), symbol=SYMBOL,
                          quantity=shares))
    fills = []
    while ex.working_quantity(SYMBOL) and data.has_more():
        data.next_bar()
        fills.extend(ex.pop_settled_fills(data.current_time()))
    ex.finalize()
    filled = sum(f.quantity for f in fills)
    avg = sum(f.fill_price * f.quantity for f in fills) / filled if filled else price
    return {"impact_on": impact_on, "impact_coef": impact_coef,
            "total_frac": total_frac, "shares": shares, "slices": len(fills),
            "filled_frac": filled / shares,
            "bps": (avg - price) / price * 10_000}


def formula_curves(price: float, volume: float, total_fracs=METAORDER_FRACS,
                   law_coef: float = LAW_COEF, **kwargs) -> list[dict]:
    """The formula handler at every size, as coded and as the law."""
    rows = []
    for impact_on, coef in (("slice", 1.0), ("order", law_coef)):
        for frac in total_fracs:
            rows.append(formula_metaorder_cost(price, volume, frac,
                                               impact_on=impact_on,
                                               impact_coef=coef, **kwargs))
    return rows


def formula_exponents(rows: list[dict], participation: float = 0.10) -> list[dict]:
    """The impact exponent of each form of the formula, one bar | sliced."""
    out = []
    for impact_on in ("slice", "order"):
        group = [r for r in rows if r["impact_on"] == impact_on]
        single = [r for r in group if r["total_frac"] <= participation]
        sliced = [r for r in group if r["total_frac"] >= participation]
        out.append({
            "impact_on": impact_on,
            "impact_coef": group[0]["impact_coef"],
            "single": impact_exponent([r["total_frac"] for r in single],
                                      [r["bps"] for r in single]),
            "sliced": impact_exponent([r["total_frac"] for r in sliced],
                                      [r["bps"] for r in sliced]),
        })
    return out


def law_ladder(prices, opens, volumes, aums=AUMS, participation: float = 0.10,
               depth_frac: float = 0.002, law_coef: float = LAW_COEF,
               persistence: float = LAW_PERSISTENCE,
               recovery: float | None = LAW_RECOVERY, **kwargs) -> list[dict]:
    """Section 2's capacity ladder with three handlers that are now distinct.

    Section 6 ended on a prediction: once the formula charges the order
    rather than the slice, the book with a third of its displacement kept
    permanently - the setting whose exponent and coefficient matched the
    law - should land on it at every AUM, not at the one AUM where two
    curves with different exponents happened to cross. This runs the three
    side by side: the formula as coded (Y = 1 on the slice), the formula as
    the law (Y = law_coef on the order), and that book. Cost is the
    share-weighted charge per slice in bps against the bar's open.
    """
    first_price = float(prices.iloc[0, 0])
    rows = []
    for aum in aums:
        shares = max(int(aum / first_price), 1)
        common = dict(trade_size=shares, initial_cash=aum, opens=opens,
                      volumes=volumes, **kwargs)
        slice_eq = run(prices, MovingAverageCrossStrategy,
                       execution_cls=ParticipationLimitedExecutionHandler,
                       execution_kwargs={"participation": participation},
                       **common)
        order_eq = run(prices, MovingAverageCrossStrategy,
                       execution_cls=ParticipationLimitedExecutionHandler,
                       execution_kwargs={"participation": participation,
                                         "impact_on": "order",
                                         "impact_coef": law_coef},
                       **common)
        book_eq = run(prices, MovingAverageCrossStrategy,
                      execution_cls=BookExecutionHandler,
                      execution_kwargs={"participation": participation,
                                        "depth_frac": depth_frac,
                                        "persistence": persistence,
                                        "recovery": recovery},
                      **common)
        row = {"aum": aum}
        for name, eq in (("slice", slice_eq), ("order", order_eq), ("book", book_eq)):
            ex = eq.attrs["execution"]
            row[name] = {**stats(eq), "slices": ex.slices,
                         "cost_bps": _weighted(ex.slice_cost_bps, ex.slice_shares),
                         "max_delay_bars": ex.max_delay_bars,
                         "deployed": 1.0 - ex.unfilled_shares * first_price / aum,
                         "invested": invested(eq)}
        rows.append(row)
    return rows


def invested(equity) -> float:
    """How much of the book was in the market, averaged over every bar.

    Gross position notional over equity, from the portfolio's own record.
    This is the number the `deployed` column was reaching for and is not:
    `deployed` is one minus the shares still working when the data ran out,
    over the AUM - which is a statement about the LAST order, and how many
    bars it had been working when the run stopped. Section 8 has the
    correction.
    """
    history = equity.attrs["portfolio"].exposure_history
    return float(np.mean([x for _, x in history])) if history else 0.0


# ---------- 8. send what you can get done ----------

FILL_DAYS = 5                       # a position must be buildable inside a week
FILL_DAYS_SWEEP = (1, 2, 5, 10, 20, None)   # None is no cap: section 7's row


def _ladder_handlers(participation: float, depth_frac: float, law_coef: float,
                     persistence: float, recovery: float | None) -> dict:
    """The three handlers of section 7, so the two ladders cannot drift."""
    return {
        "slice": (ParticipationLimitedExecutionHandler,
                  {"participation": participation}),
        "order": (ParticipationLimitedExecutionHandler,
                  {"participation": participation, "impact_on": "order",
                   "impact_coef": law_coef}),
        "book": (BookExecutionHandler,
                 {"participation": participation, "depth_frac": depth_frac,
                  "persistence": persistence, "recovery": recovery}),
    }


def _sized_row(prices, opens, volumes, aum: float, fill_days: float | None,
               participation: float, handlers: dict, **kwargs) -> dict:
    first_price = float(prices.iloc[0, 0])
    shares = max(int(aum / first_price), 1)
    portfolio_kwargs = ({"max_fill_days": fill_days, "participation": participation}
                        if fill_days is not None else {})
    row = {"aum": aum, "fill_days": fill_days}
    for name, (cls, execution_kwargs) in handlers.items():
        eq = run(prices, MovingAverageCrossStrategy, trade_size=shares,
                 initial_cash=aum, opens=opens, volumes=volumes,
                 execution_cls=cls, execution_kwargs=execution_kwargs,
                 portfolio_kwargs=portfolio_kwargs, **kwargs)
        ex = eq.attrs["execution"]
        row[name] = {**stats(eq), "slices": ex.slices,
                     "cost_bps": _weighted(ex.slice_cost_bps, ex.slice_shares),
                     "max_delay_bars": ex.max_delay_bars,
                     "deployed": 1.0 - ex.unfilled_shares * first_price / aum,
                     "invested": invested(eq),
                     "sizings_capped": eq.attrs["portfolio"].liquidity_capped}
    # What the budget let the desk hold, as a share of the AUM: the median
    # liquidity budget over the run against the shares the AUM asked for.
    # Reported from the data rather than from inside a run so that the same
    # number stands beside all three handlers.
    if fill_days is None:
        row["position_share"] = 1.0
    else:
        adv = volumes.iloc[:, 0].rolling(20, min_periods=1).mean()
        budget = (fill_days * participation * adv).median()
        row["position_share"] = min(1.0, float(budget) / shares)
    return row


def time_in_market(prices, opens, **kwargs) -> float:
    """The unconstrained rule's average exposure: what 'fully invested when
    long' comes to over the sample once the flat stretches are counted. The
    invested column of the ladders is read against this, not against 100%."""
    eq = run(prices, MovingAverageCrossStrategy, trade_size=int(1e6 / float(prices.iloc[0, 0])),
             initial_cash=1e6, opens=opens, execution_cls=NextBarOpenExecutionHandler,
             **kwargs)
    return invested(eq)


def sized_ladder(prices, opens, volumes, aums=AUMS, fill_days: float = FILL_DAYS,
                 participation: float = 0.10, depth_frac: float = 0.002,
                 law_coef: float = LAW_COEF, persistence: float = LAW_PERSISTENCE,
                 recovery: float | None = LAW_RECOVERY, **kwargs) -> list[dict]:
    """Section 7's ladder again, with the strategy sending what it can fill.

    Section 7 ended on the observation that above $1bn no handler describes a
    trade, because the order sent is a position that takes forty bars to
    build and the next signal cancels most of it. That is a sizing question,
    and this answers it the way a desk does: the position is capped at what
    `participation` of ADV fills inside `fill_days` bars, so every order
    completes and the deployed column reads 100% by construction. What is
    left to measure is what the strategy earns on the AUM once it is only
    allowed to hold the part of it that fits - which is the capacity number
    section 2 was reaching for, with the cash pile removed from the Sharpe.
    """
    handlers = _ladder_handlers(participation, depth_frac, law_coef,
                                persistence, recovery)
    return [_sized_row(prices, opens, volumes, aum, fill_days, participation,
                       handlers, **kwargs) for aum in aums]


def fill_days_sweep(prices, opens, volumes, aum: float = 2e10,
                    days=FILL_DAYS_SWEEP, participation: float = 0.10,
                    depth_frac: float = 0.002, law_coef: float = LAW_COEF,
                    persistence: float = LAW_PERSISTENCE,
                    recovery: float | None = LAW_RECOVERY, **kwargs) -> list[dict]:
    """One AUM, every patience. A desk that will wait a month holds more of
    the book than one that wants it done by Friday; this says what the extra
    position earns and what it costs, at the AUM where section 7 said 90% of
    the order was never sent."""
    handlers = _ladder_handlers(participation, depth_frac, law_coef,
                                persistence, recovery)
    return [_sized_row(prices, opens, volumes, aum, d, participation,
                       handlers, **kwargs) for d in days]


def print_sized_ladder(rows: list[dict], sweep: list[dict], ladder: list[dict],
                       baseline: float, fill_days: float) -> None:
    print(f"\n8. SEND WHAT YOU CAN GET DONE   the position capped at what "
          f"participation fills inside {fill_days:g} bars")
    print(f"\n   First, section 7's ladder read by how much of the book was in the "
          f"market, averaged over the run,\n   against the unconstrained rule's "
          f"{baseline:.0%}. The 'deployed' column was the last order's fill.")
    print(f"\n{'AUM':>8} {'deployed':>9} {'invested':>9} {'of baseline':>12} {'longest':>8}")
    for r in ladder:
        print(f"{r['aum'] / 1e9:>7.1f}bn {r['book']['deployed']:>9.0%} "
              f"{r['book']['invested']:>9.0%} {r['book']['invested'] / baseline:>12.0%} "
              f"{r['book']['max_delay_bars']:>8d}")
    print(f"\n   Then the same ladder with the position capped at {fill_days:g} days "
          f"of participation:")
    print(f"\n{'AUM':>8} {'position':>9} {'invested':>9} {'Sharpe slice':>13} {'order':>7} "
          f"{'book':>7} {'return slice':>13} {'order':>7} {'book':>7} "
          f"{'cost slice':>11} {'order':>7} {'book':>7} {'longest':>8}")
    for r in rows:
        print(f"{r['aum'] / 1e9:>7.1f}bn {r['position_share']:>9.0%} "
              f"{r['book']['invested'] / baseline:>9.0%} "
              f"{r['slice']['sharpe']:>13.2f} {r['order']['sharpe']:>7.2f} "
              f"{r['book']['sharpe']:>7.2f} "
              f"{r['slice']['total_return']:>13.1%} {r['order']['total_return']:>7.1%} "
              f"{r['book']['total_return']:>7.1%} "
              f"{r['slice']['cost_bps']:>10.1f} {r['order']['cost_bps']:>7.1f} "
              f"{r['book']['cost_bps']:>7.1f} {r['book']['max_delay_bars']:>8d}")
    aum = sweep[0]["aum"]
    print(f"\n   At ${aum / 1e9:.0f}bn, how long the desk is willing to take:")
    print(f"\n{'days':>6} {'position':>9} {'invested':>9} {'Sharpe slice':>13} {'order':>7} "
          f"{'book':>7} {'return book':>12} {'cost book':>10} {'longest':>8}")
    for r in sweep:
        label = "none" if r["fill_days"] is None else f"{r['fill_days']:g}"
        print(f"{label:>6} {r['position_share']:>9.0%} "
              f"{r['book']['invested'] / baseline:>9.0%} "
              f"{r['slice']['sharpe']:>13.2f} {r['order']['sharpe']:>7.2f} "
              f"{r['book']['sharpe']:>7.2f} {r['book']['total_return']:>12.1%} "
              f"{r['book']['cost_bps']:>10.1f} {r['book']['max_delay_bars']:>8d}")


def print_law_ladder(rows: list[dict], exponents: list[dict],
                     curves: list[dict]) -> None:
    print(f"\n7. CHARGE THE ORDER, NOT THE SLICE   the formula handler on the "
          f"flat tape, from the handler itself")
    fracs = sorted({r["total_frac"] for r in curves})
    print(f"\n{'':>24} | {'days of volume:':>15} "
          + " ".join(f"{f:>7.2f}" for f in fracs))
    for impact_on, label in (("slice", "as coded, Y=1"), ("order", "on the order")):
        group = {r["total_frac"]: r for r in curves if r["impact_on"] == impact_on}
        coef = group[fracs[0]]["impact_coef"]
        tag = f"{label}, Y={coef:g}" if impact_on == "order" else label
        print(f"{tag:>24} | {'':>15} "
              + " ".join(f"{group[f]['bps']:>7.1f}" for f in fracs))
    for e in exponents:
        print(f"   exponent, impact on the {e['impact_on']}: "
              f"{e['single']:.2f} inside one bar | {e['sliced']:.2f} sliced")

    print(f"\n   The capacity ladder under three handlers: formula on the slice "
          f"(ships), formula on the order (the law), book with memory.")
    print(f"\n{'AUM':>8} {'Sharpe slice':>13} {'order':>7} {'book':>7} "
          f"{'cost slice':>11} {'order':>7} {'book':>7} {'deployed':>9}")
    for r in rows:
        print(f"{r['aum'] / 1e9:>7.1f}bn {r['slice']['sharpe']:>13.2f} "
              f"{r['order']['sharpe']:>7.2f} {r['book']['sharpe']:>7.2f} "
              f"{r['slice']['cost_bps']:>10.1f} {r['order']['cost_bps']:>7.1f} "
              f"{r['book']['cost_bps']:>7.1f} {r['book']['deployed']:>9.0%}")


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


def print_crossover_by_decay(rows: list[dict], price: float,
                             volume: float) -> None:
    print(f"\n1d. AND WHERE THE FAR BOOK EMPTIES OUT   (shape {SHAPE}, "
          f"total size held fixed)")
    print(f"    The ramp's heaviest level is its furthest. Letting the profile "
          f"decay past a peak:")
    fracs = list(rows[0]["cost_bps"])
    print(f"\n{'decay':>7} {'peak lvl':>9} {'touch':>7} {'half depth':>11} "
          f"{'lvl 200':>8}" + "".join(f"{f'{float(x):.2%}':>9}" for x in fracs)
          + f" {'crossover':>11}")
    for r in rows:
        label = "none" if r["decay"] is None else f"{r['decay']:.0f}"
        cross = ("never" if r["crossover_frac"] is None
                 else f"{r['crossover_frac']:.2%}")
        print(f"{label:>7} {r['peak_level']:>9} {r['touch_multiple']:>6.2f}x "
              f"{r['half_depth_level']:>11} {r['far_level_multiple']:>7.3f}x"
              + "".join(f"{r['cost_bps'][x]:>9.2f}" for x in fracs)
              + f" {cross:>11}")
    print(f"    (cost in bps per share by order size as a share of the day; "
          f"touch and lvl 200 as multiples of the flat per-level size)")
    lo = min(r["crossover_frac"] for r in rows if r["crossover_frac"])
    hi = max(r["crossover_frac"] for r in rows if r["crossover_frac"])
    print(f"\n    The crossover runs from {lo:.2%} to {hi:.2%} of a day's "
          f"volume - ${lo * volume * price / 1e6:,.0f}M to "
          f"${hi * volume * price / 1e6:,.0f}M of SPY -")
    print(f"    on a parameter nothing here observes. That {hi / lo:.0f}x "
          f"range is the error bar on the headline,")
    print(f"    and the reason the handler ships the ramp: it is the dearest "
          f"book of the family, so")
    print(f"    the one number it cannot do is flatter the strategy.")


def print_vol_elasticity(study: dict) -> None:
    """Section 1b was about where the depth sits. This is about when."""
    rows = study["rows"]
    flat = next((r for r in rows if r["elasticity"] == 0.0), None)
    shipped = next((r for r in rows if r["elasticity"] == VOL_ELASTICITY), None)

    print(f"\n1c. AND IT IS NOT THE SAME DEPTH EVERY DAY   "
          f"(at ${study['aum'] / 1e9:.0f}bn)")
    print(f"    Resting size scales as (typical vol / current vol) ** "
          f"elasticity, measured on a")
    print(f"    {study['vol_window']}-bar window against an expanding median "
          f"of itself. 0 is the constant book.")
    print(f"\n{'elasticity':>11} {'depth met':>10} {'cost/share':>11} "
          f"{'sharpe':>8} {'return':>9} {'levels':>8}")
    for r in rows:
        met = "-" if np.isnan(r["multiplier"]) else f"{r['multiplier']:.2f}x"
        print(f"{r['elasticity']:>11.2f} {met:>10} {r['cost_bps']:>10.2f}  "
              f"{r['sharpe']:>8.2f} {r['total_return']:>9.2%} "
              f"{r['mean_levels_walked']:>8.1f}")

    if shipped is None or np.isnan(shipped["multiplier"]):
        return

    print(f"\n    Across every bar in the sample the multiplier averages "
          f"{study['unconditional_multiplier']:.2f}, so")
    print(f"    the book has not been made thinner overall. On the bars this "
          f"strategy chose to")
    print(f"    trade it averages {shipped['multiplier']:.2f}. Its fills land "
          f"at the {shipped['vol_percentile'] * 100:.0f}th percentile of")
    print(f"    realized volatility, not the 50th, because a moving-average "
          f"crossover fires after")
    print(f"    a trend breaks and a trend breaking IS a volatility event. "
          f"The signal is")
    print(f"    correlated with illiquidity by construction.")

    matched = study.get("level_matched")
    if matched and flat:
        total = shipped["cost_bps"] - flat["cost_bps"]
        level = matched["cost_bps"] - flat["cost_bps"]
        if total > 0:
            print(f"\n    Splitting that {total:+.2f} bps a share. A constant "
                  f"book held at the same")
            print(f"    {matched['matched_to']:.2f}x - thinner every day, but "
                  f"never thinner on the days that")
            print(f"    matter - costs {level:+.2f} bps. So {level / total:.0%} "
                  f"of the extra cost is nothing")
            print(f"    but WHEN this strategy trades, and the remaining "
                  f"{1 - level / total:.0%} is the convexity")
            print(f"    of cost in depth: the level of the co-movement matters "
                  f"{level / (total - level):.0f}x more than")
            print(f"    the spread around it.")


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


def print_memory_sweep(rows: list[dict], closes: dict) -> None:
    print(f"\n5. WHAT THE BOOK REMEMBERS   the sliced order, charged for its "
          f"own earlier slices")
    print(f"\n{'aum':>6} {'persist':>8} {'recovery':>9} {'sharpe':>7} "
          f"{'sqrt':>6} {'return':>8} {'cost':>7} {'carried':>8} "
          f"{'max carried':>12} {'slices':>7} {'deployed':>9}")
    for r in rows:
        rec = "never" if r["recovery"] is None else f"{r['recovery']:.0f} bars"
        print(f"{r['aum'] / 1e9:>5.0f}B {r['persistence']:>8.2f} {rec:>9} "
              f"{r['sharpe']:>7.2f} {r['sqrt_sharpe']:>6.2f} "
              f"{r['total_return']:>8.1%} {r['cost_bps']:>6.1f}bp "
              f"{r['carried_bps']:>7.1f}bp {r['max_carried_bps']:>11.1f}bp "
              f"{r['slices']:>7} {r['deployed']:>9.0%}")
    for key, p in closes.items():
        aum, recovery = key.split("/")
        where = ("with permanent impact" if recovery == "never"
                 else f"with a {recovery}-bar half-life")
        if p is None:
            print(f"   ${float(aum) / 1e9:.0f}bn {where}: no persistence "
                  f"brings the book down to the square-root model.")
        else:
            print(f"   ${float(aum) / 1e9:.0f}bn {where}: at or below the "
                  f"square-root model from persistence {p:.2f}.")


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
    by_decay = crossover_by_decay(price, volume)
    print_crossover_by_decay(by_decay, price, volume)

    vol_study = vol_elasticity_study(prices, opens, volumes)
    print_vol_elasticity(vol_study)

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

    memory = memory_sweep(prices, opens, volumes,
                          aums=(1e9, 5e9) if not args.quick else (5e9,))
    closes = memory_closes_the_gap(memory)
    print_memory_sweep(memory, closes)

    metaorders = metaorder_curves(price, volume)
    exponents = exponents_by_setting(metaorders)
    nearest = closest_to_square_root(exponents)
    print_metaorders(metaorders, exponents, nearest)

    formula = formula_curves(price, volume)
    formula_exp = formula_exponents(formula)
    ladder = law_ladder(prices, opens, volumes, aums=aums)
    print_law_ladder(ladder, formula_exp, formula)

    sized = sized_ladder(prices, opens, volumes, aums=aums)
    patience = fill_days_sweep(prices, opens, volumes,
                               days=FILL_DAYS_SWEEP if not args.quick else (1, 5, None))
    baseline = time_in_market(prices, opens)
    print_sized_ladder(sized, patience, ladder, baseline, FILL_DAYS)

    os.makedirs("results", exist_ok=True)
    with open(OUT, "w") as fh:
        json.dump({"symbol": SYMBOL, "start": START, "end": END,
                   "median_price": price, "median_volume": volume,
                   "cost_curves": curves, "crossover": cross,
                   "shape": SHAPE, "crossover_by_shape": by_shape,
                   "crossover_by_decay": by_decay,
                   "vol_elasticity": VOL_ELASTICITY,
                   "vol_elasticity_study": vol_study,
                   "capacity": cap, "depth_sweep": sweep, "fees": bill,
                   "persistence": PERSISTENCE, "recovery": RECOVERY,
                   "memory_sweep": memory, "memory_closes_the_gap": closes,
                   "metaorders": metaorders, "impact_exponents": exponents,
                   "closest_to_square_root": nearest,
                   "law_coef": LAW_COEF, "formula_curves": formula,
                   "formula_exponents": formula_exp, "law_ladder": ladder,
                   "fill_days": FILL_DAYS, "time_in_market": baseline,
                   "sized_ladder": sized, "fill_days_sweep": patience},
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
