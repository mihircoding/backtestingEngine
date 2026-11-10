"""What the assumed fill was worth, measured against the book that prints it.

Usage:  python fill_realism.py [--quick] [--section N ...]
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
from src.strategy import (MovingAverageCrossLongShortStrategy,
                          MovingAverageCrossStrategy)
from run_backtest import (TRADING_DAYS, fetch_opens, fetch_prices, fetch_volumes, run,
                          stats)

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
               participation: float, handlers: dict,
               strategy=MovingAverageCrossStrategy, trim: bool = False,
               borrow_bps: float = 0.0, **kwargs) -> dict:
    first_price = float(prices.iloc[0, 0])
    shares = max(int(aum / first_price), 1)
    portfolio_kwargs = ({"max_fill_days": fill_days, "participation": participation,
                         "trim_to_budget": trim}
                        if fill_days is not None else {})
    if borrow_bps:
        portfolio_kwargs["short_borrow_bps"] = borrow_bps
    row = {"aum": aum, "fill_days": fill_days}
    for name, (cls, execution_kwargs) in handlers.items():
        eq = run(prices, strategy, trade_size=shares,
                 initial_cash=aum, opens=opens, volumes=volumes,
                 execution_cls=cls, execution_kwargs=execution_kwargs,
                 portfolio_kwargs=portfolio_kwargs, **kwargs)
        ex = eq.attrs["execution"]
        pf = eq.attrs["portfolio"]
        row[name] = {**stats(eq), "slices": ex.slices,
                     "cost_bps": _weighted(ex.slice_cost_bps, ex.slice_shares),
                     "max_delay_bars": ex.max_delay_bars,
                     "deployed": 1.0 - ex.unfilled_shares * first_price / aum,
                     "invested": invested(eq),
                     "sizings_capped": pf.liquidity_capped,
                     "trims": pf.liquidity_trims,
                     "fills": pf.n_fills,
                     "borrow_paid_bps": pf.borrow_paid / aum * 1e4,
                     "year_ends": year_end_equity(eq)}
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


def time_in_market(prices, opens, strategy=MovingAverageCrossStrategy, **kwargs) -> float:
    """The unconstrained rule's average exposure: what 'fully invested when
    long' comes to over the sample once the flat stretches are counted. The
    invested column of the ladders is read against this, not against 100%."""
    eq = run(prices, strategy, trade_size=int(1e6 / float(prices.iloc[0, 0])),
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


# ---------- 9. the budget on the way out, and the rule that flips ----------

# General collateral on SPY, from long_short.py: the pessimistic end of the
# range on purpose, so the long/short rows are not flattered by free borrow.
LS_BORROW_BPS = 75.0


def trimmed_ladder(prices, opens, volumes, aums=AUMS, fill_days: float = FILL_DAYS,
                   participation: float = 0.10, depth_frac: float = 0.002,
                   law_coef: float = LAW_COEF, persistence: float = LAW_PERSISTENCE,
                   recovery: float | None = LAW_RECOVERY, **kwargs) -> list[dict]:
    """Section 8's ladder with the budget binding on the way out too.

    Section 8 capped the position at entry and then sold it at whatever pace
    the tape allowed, which is how a five-day budget produced a sixteen-bar
    fill: the ADV the position was sized against is not the ADV it is sold
    into. With trim_to_budget the portfolio re-checks the budget every bar
    and sells down anything over it, so what the book holds is always what
    it could get out of in `fill_days`. The trim is itself an order into the
    same volume, so this is a measurement of what that discipline costs and
    whether it shortens the exits, not a promise that it does.
    """
    handlers = _ladder_handlers(participation, depth_frac, law_coef,
                                persistence, recovery)
    return [_sized_row(prices, opens, volumes, aum, fill_days, participation,
                       handlers, trim=True, **kwargs) for aum in aums]


def long_short_ladder(prices, opens, volumes, aums=AUMS, fill_days: float | None = FILL_DAYS,
                      participation: float = 0.10, depth_frac: float = 0.002,
                      law_coef: float = LAW_COEF, persistence: float = LAW_PERSISTENCE,
                      recovery: float | None = LAW_RECOVERY,
                      borrow_bps: float = LS_BORROW_BPS, **kwargs) -> list[dict]:
    """The capacity curve for the rule that flips.

    The long-only rule's worst order is a position's worth. The long/short
    version's down-cross is a flip from long to short, which is two
    positions' worth of shares in one order, so the budget - which caps the
    POSITION - should bite twice as hard on it: the same days of participation
    buy half the position, and the order that reverses it takes twice as
    long. This runs the same ladder on that rule, with the budget and
    without, borrow charged at long_short.py's rate. fill_days=None is the
    unconstrained rule, which is the row section 7's ladder would have had
    if it had been long/short.
    """
    handlers = _ladder_handlers(participation, depth_frac, law_coef,
                                persistence, recovery)
    return [_sized_row(prices, opens, volumes, aum, fill_days, participation,
                       handlers, strategy=MovingAverageCrossLongShortStrategy,
                       borrow_bps=borrow_bps, **kwargs) for aum in aums]


def print_exit_and_flip(trimmed: list[dict], sized: list[dict], ls_free: list[dict],
                        ls_capped: list[dict], baseline: float, ls_baseline: float,
                        fill_days: float) -> None:
    print(f"\n9. THE BUDGET ON THE WAY OUT, AND THE RULE THAT FLIPS")
    print(f"\n   Section 8's ladder, the book handler, with the position sold down "
          f"whenever it is over\n   what {fill_days:g} days of participation could "
          f"get out of. 'entry only' is section 8's row.")
    print(f"\n{'AUM':>8} {'Sharpe entry':>13} {'trimmed':>8} {'return entry':>13} "
          f"{'trimmed':>8} {'invested':>9} {'trimmed':>8} {'longest':>8} {'trimmed':>8} "
          f"{'trims':>6} {'fills':>6} {'trimmed':>8} {'cost':>6} {'trimmed':>8}")
    for a, b in zip(sized, trimmed):
        print(f"{a['aum'] / 1e9:>7.1f}bn {a['book']['sharpe']:>13.2f} {b['book']['sharpe']:>8.2f} "
              f"{a['book']['total_return']:>13.1%} {b['book']['total_return']:>8.1%} "
              f"{a['book']['invested'] / baseline:>9.0%} {b['book']['invested'] / baseline:>8.0%} "
              f"{a['book']['max_delay_bars']:>8d} {b['book']['max_delay_bars']:>8d} "
              f"{b['book']['trims']:>6d} {a['book']['fills']:>6d} {b['book']['fills']:>8d} "
              f"{a['book']['cost_bps']:>6.1f} {b['book']['cost_bps']:>8.1f}")
    print(f"\n   Then the long/short rule, borrow at {LS_BORROW_BPS:.0f}bp, unconstrained "
          f"against the {fill_days:g}-day budget.\n   'invested' is against the long/short "
          f"rule's own {ls_baseline:.0%}; the budget caps the position, and a flip is two of them.")
    print(f"\n{'AUM':>8} {'position':>9} {'Sharpe free':>12} {'slice':>7} {'order':>7} {'book':>7} "
          f"{'return free':>12} {'book':>7} {'invested free':>14} {'book':>7} "
          f"{'longest free':>13} {'book':>7} {'cost book':>10} {'borrow':>7}")
    for f, c in zip(ls_free, ls_capped):
        print(f"{f['aum'] / 1e9:>7.1f}bn {c['position_share']:>9.0%} {f['book']['sharpe']:>12.2f} "
              f"{c['slice']['sharpe']:>7.2f} {c['order']['sharpe']:>7.2f} {c['book']['sharpe']:>7.2f} "
              f"{f['book']['total_return']:>12.1%} {c['book']['total_return']:>7.1%} "
              f"{f['book']['invested'] / ls_baseline:>14.0%} {c['book']['invested'] / ls_baseline:>7.0%} "
              f"{f['book']['max_delay_bars']:>13d} {c['book']['max_delay_bars']:>7d} "
              f"{c['book']['cost_bps']:>10.1f} {c['book']['borrow_paid_bps']:>7.0f}")


# ---------- 10. the one free parameter, and the result that was noise ----------

PARTICIPATION_SWEEP = (0.02, 0.05, 0.10, 0.20, 0.30, 0.50)
BOOTSTRAP_AUMS = (5e9, 2e10)   # the two trim rows section 9 read a sign off


def participation_sweep(prices, opens, volumes, aum: float = 2e10,
                        rates=PARTICIPATION_SWEEP, fill_days: float = FILL_DAYS,
                        depth_frac: float = 0.002, law_coef: float = LAW_COEF,
                        persistence: float = LAW_PERSISTENCE,
                        recovery: float | None = LAW_RECOVERY, **kwargs) -> list[dict]:
    """Section 9's trimmed $20bn row at every participation rate.

    The budget is days x participation x ADV and the fill is participation x
    the bar's actual volume, so participation is in both: a desk that is
    willing to be 20% of the tape is allowed twice the position of one at
    10%, and gets out of it at twice the pace. The ratio of the two - the
    bars the worst exit takes - does not depend on it at all. What does is
    the impact: the slice handler charges the root of the slice's share of
    the bar, the order handler the root of the whole order's, and the book
    walks whatever depth the bar has. All three see the participation; what
    the sweep says is what being a bigger share of the tape costs under each,
    and whether that is the same knob as the patience section 8 swept.
    """
    rows = []
    for rate in rates:
        handlers = _ladder_handlers(rate, depth_frac, law_coef, persistence, recovery)
        row = _sized_row(prices, opens, volumes, aum, fill_days, rate, handlers,
                         trim=True, **kwargs)
        row["participation"] = rate
        rows.append(row)
    return rows


def trim_bootstrap(prices, opens, volumes, aums=BOOTSTRAP_AUMS, fill_days: float = FILL_DAYS,
                   participation: float = 0.10, depth_frac: float = 0.002,
                   law_coef: float = LAW_COEF, persistence: float = LAW_PERSISTENCE,
                   recovery: float | None = LAW_RECOVERY, n_boot: int = 2000,
                   **kwargs) -> list[dict]:
    """Section 9's trim rows, with an interval on the Sharpe difference.

    Section 9 read +0.03 at $5bn and -0.02 at $20bn off the trimmed column
    and called the first noise with a sign. This is the check: the two
    equity curves saw the same tape on the same days, so the stationary
    bootstrap of significance.py resamples them together and the interval
    on the difference is the interval that matters. A difference whose
    interval straddles zero is not a result, and a sign on it is not a
    direction.
    """
    import significance as sig

    cls, execution_kwargs = _ladder_handlers(participation, depth_frac, law_coef,
                                             persistence, recovery)["book"]
    first_price = float(prices.iloc[0, 0])
    rows = []
    for aum in aums:
        shares = max(int(aum / first_price), 1)
        curves = {}
        for trim in (False, True):
            eq = run(prices, MovingAverageCrossStrategy, trade_size=shares,
                     initial_cash=aum, opens=opens, volumes=volumes,
                     execution_cls=cls, execution_kwargs=execution_kwargs,
                     portfolio_kwargs={"max_fill_days": fill_days,
                                       "participation": participation,
                                       "trim_to_budget": trim}, **kwargs)
            curves[trim] = eq.pct_change().dropna().to_numpy()
        gap = sig.paired_difference(curves[True], curves[False], n_boot=n_boot)
        rows.append({"aum": aum, "sharpe_entry_only": sig.sharpe(curves[False]),
                     "sharpe_trimmed": sig.sharpe(curves[True]), **gap,
                     "days_that_differ": int(np.sum(curves[True] != curves[False]))})
    return rows


def print_participation(rows: list[dict], boot: list[dict], aum: float,
                        fill_days: float) -> None:
    print(f"\n10. THE ONE FREE PARAMETER, AND THE RESULT THAT WAS NOISE")
    print(f"\n   Section 9's trimmed ${aum / 1e9:.0f}bn row at every participation rate, "
          f"{fill_days:g}-day budget, all three handlers.")
    print(f"\n{'rate':>5} {'position':>9} {'Sharpe slice':>13} {'order':>7} {'book':>7} "
          f"{'return book':>12} {'longest':>8} {'trims':>6} {'cost slice':>11} {'order':>7} {'book':>7}")
    for r in rows:
        print(f"{r['participation']:>5.0%} {r['position_share']:>9.0%} "
              f"{r['slice']['sharpe']:>13.2f} {r['order']['sharpe']:>7.2f} {r['book']['sharpe']:>7.2f} "
              f"{r['book']['total_return']:>12.1%} {r['book']['max_delay_bars']:>8d} "
              f"{r['book']['trims']:>6d} {r['slice']['cost_bps']:>11.1f} "
              f"{r['order']['cost_bps']:>7.1f} {r['book']['cost_bps']:>7.1f}")
    print(f"\n   The trim rows' Sharpe difference, trimmed minus entry-only, book handler, "
          f"bootstrapped on the same days:")
    print(f"\n{'AUM':>8} {'entry only':>11} {'trimmed':>8} {'diff':>7} {'95% interval':>17} "
          f"{'P(diff<=0)':>11} {'days that differ':>17}")
    for b in boot:
        print(f"{b['aum'] / 1e9:>7.1f}bn {b['sharpe_entry_only']:>11.2f} {b['sharpe_trimmed']:>8.2f} "
              f"{b['diff']:>+7.2f} [{b['lo']:>+6.2f}, {b['hi']:>+6.2f}] "
              f"{b['p_le_zero']:>11.0%} {b['days_that_differ']:>17d}")


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


# ---------- 11. the capacity surface ----------

# Denser than AUMS between $1bn and $20bn, because that is where the budget
# starts to bind at the lower participation rates and the knee has to be
# somewhere in the grid to be read off it.
SURFACE_AUMS = (1e8, 1e9, 2e9, 5e9, 1e10, 2e10, 1e11)


def capacity_surface(prices, opens, volumes, aums=SURFACE_AUMS, rates=PARTICIPATION_SWEEP,
                     fill_days: float = FILL_DAYS, depth_frac: float = 0.002,
                     persistence: float = LAW_PERSISTENCE,
                     recovery: float | None = LAW_RECOVERY, **kwargs) -> list[dict]:
    """Section 9's trimmed book at every AUM and every participation rate.

    Section 10 ended by saying that the capacity ladder's Sharpe is the
    participation it was run at, which makes a one-axis ladder the wrong
    shape for the claim. This is the two-axis version, book handler only,
    so that "capacity" can be read as a position at a rate and a patience
    rather than as a dollar figure with the rate hidden inside it.

    The prediction, written before it ran: at each rate the Sharpe falls
    with AUM only until the budget binds, and is flat beyond. Past that
    point the dollar position is days x rate x ADV whatever the AUM says,
    so the slices, the fills and the exits are the same run, and the extra
    AUM is cash that the Sharpe does not see. If that holds, every column
    is a staircase with one step, and the surface collapses to the rate.
    """
    rows = []
    for rate in rates:
        handlers = {"book": _ladder_handlers(rate, depth_frac, LAW_COEF,
                                             persistence, recovery)["book"]}
        for aum in aums:
            row = _sized_row(prices, opens, volumes, aum, fill_days, rate, handlers,
                             trim=True, **kwargs)
            book = row.pop("book")
            rows.append({**row, "participation": rate, **book})
    return rows


def surface_knees(rows: list[dict], fits: float = 0.999) -> list[dict]:
    """Per rate: where the budget first binds, and what the Sharpe does past it.

    The knee is the smallest AUM whose position_share is below one - the
    first rung where the desk is holding less than the AUM asked for. The
    cells from the knee up are the "flat" part if the prediction holds, so
    the spread of their Sharpes is the number that says whether it did.
    """
    out = []
    for rate in sorted({r["participation"] for r in rows}):
        col = sorted((r for r in rows if r["participation"] == rate), key=lambda r: r["aum"])
        bound = [r for r in col if r["position_share"] < fits]
        whole = [r for r in col if r["position_share"] >= fits]
        knee = bound[0] if bound else None
        past = [r["sharpe"] for r in bound]
        out.append({
            "participation": rate,
            "knee_aum": knee["aum"] if knee else None,
            "position_at_knee": knee["position_share"] if knee else 1.0,
            "sharpe_whole": whole[-1]["sharpe"] if whole else None,
            "sharpe_past_knee_low": min(past) if past else None,
            "sharpe_past_knee_high": max(past) if past else None,
            "sharpe_spread_past_knee": (max(past) - min(past)) if past else 0.0,
            "cost_bps_past_knee": {"low": min(r["cost_bps"] for r in bound),
                                   "high": max(r["cost_bps"] for r in bound)} if bound else None,
        })
    return out


def print_surface(rows: list[dict], knees: list[dict], fill_days: float) -> None:
    aums = sorted({r["aum"] for r in rows})
    rates = sorted({r["participation"] for r in rows})
    cell = {(r["aum"], r["participation"]): r for r in rows}
    print(f"\n11. THE CAPACITY SURFACE   book handler, trimmed, {fill_days:g}-day budget: "
          f"Sharpe at every AUM and every participation rate")
    print(f"\n{'AUM':>9} " + " ".join(f"{p:>8.0%}" for p in rates))
    for aum in aums:
        print(f"{aum / 1e9:>7.1f}bn " + " ".join(f"{cell[(aum, p)]['sharpe']:>8.2f}" for p in rates))
    print(f"\n   and the share of the AUM the budget let the desk hold:")
    print(f"\n{'AUM':>9} " + " ".join(f"{p:>8.0%}" for p in rates))
    for aum in aums:
        print(f"{aum / 1e9:>7.1f}bn " + " ".join(f"{cell[(aum, p)]['position_share']:>8.0%}" for p in rates))
    print(f"\n   Where the budget first binds, and what the Sharpe does from there up:")
    print(f"\n{'rate':>5} {'knee AUM':>9} {'holds':>6} {'Sharpe, last whole':>19} "
          f"{'past the knee':>15} {'spread':>7} {'cost bps past knee':>19}")
    for k in knees:
        if k["knee_aum"] is None:
            print(f"{k['participation']:>5.0%} {'never binds':>9}")
            continue
        print(f"{k['participation']:>5.0%} {k['knee_aum'] / 1e9:>7.1f}bn {k['position_at_knee']:>6.0%} "
              f"{k['sharpe_whole'] if k['sharpe_whole'] is not None else float('nan'):>19.2f} "
              f"{k['sharpe_past_knee_low']:>6.2f}-{k['sharpe_past_knee_high']:.2f} "
              f"{k['sharpe_spread_past_knee']:>7.2f} "
              f"{k['cost_bps_past_knee']['low']:>8.1f}-{k['cost_bps_past_knee']['high']:.1f}")


# ---------- 12. put a fee on the surface ----------

MGMT_FEE = 0.02        # a year, on whatever the fee is charged on
PERF_FEE = 0.20        # of the profit after the management fee
BASES = ("on_aum", "on_deployed")


def fee_economics(rows: list[dict], bars: int, mgmt: float = MGMT_FEE,
                  perf: float = PERF_FEE, trading_days: int = TRADING_DAYS) -> list[dict]:
    """Two-and-twenty, charged two ways, on every cell of the surface.

    Section 11 found that past the knee every cell at a rate makes the same
    dollars whatever the AUM says, because the rest is cash. A management
    fee does not know that: it is charged on the AUM. So each cell gets the
    fee worked out twice - on the AUM the investor committed, and on the
    position the budget actually let the desk hold (`position_share` of it)
    - with the incentive fee taken as a fifth of whatever profit is left
    after the management fee. That is the ten years as one lump: no yearly
    crystallization and no high-water mark, which would charge more, since
    an incentive fee is paid in the up years and not handed back in the
    down ones. The lump is the floor on what the manager takes.

    No runs. Every number is arithmetic on what section 11 wrote.
    """
    years = bars / trading_days
    out = []
    for r in rows:
        gross = r["final"] - r["aum"]
        cell = {"aum": r["aum"], "participation": r["participation"], "years": years,
                "gross_pl": gross, "sharpe": r["sharpe"],
                "position_share": r["position_share"]}
        for basis, capital in (("on_aum", r["aum"]),
                               ("on_deployed", r["aum"] * r["position_share"])):
            fee = mgmt * capital * years
            incentive = perf * max(gross - fee, 0.0)
            take = fee + incentive
            cell[basis] = {"management": fee, "incentive": incentive, "manager_take": take,
                           "investor_net": gross - take,
                           "investor_net_return": (gross - take) / r["aum"],
                           "fee_share_of_gross": take / gross if gross > 0 else None}
        out.append(cell)
    return out


def fee_alignment(cells: list[dict], knees: list[dict], mgmt: float = MGMT_FEE) -> list[dict]:
    """Per rate: the AUM each side of the fee would choose, and where they part.

    On the AUM basis the manager's take only rises with AUM - a fifth of a
    flat profit plus two percent of a growing pile - so the manager's best
    AUM is the top of the surface at every rate. The investor's best is the
    rung where the net is largest, and past the knee the net falls a
    straight 2% of AUM a year, so there is an AUM at which the fee has eaten
    the whole profit: gross / (mgmt x years), which is exact when the knee
    is below it because the gross is flat from the knee up. On the deployed
    basis the fee stops growing where the position does, and both sides'
    best AUM is the same rung.
    """
    knee_at = {k["participation"]: k["knee_aum"] for k in knees}
    out = []
    for rate in sorted({c["participation"] for c in cells}):
        col = sorted((c for c in cells if c["participation"] == rate), key=lambda c: c["aum"])
        years = col[0]["years"]
        top = col[-1]
        best = {basis: max(col, key=lambda c: c[basis]["investor_net"]) for basis in BASES}
        manager = {basis: max(col, key=lambda c: c[basis]["manager_take"]) for basis in BASES}
        knee = knee_at.get(rate)
        flat_gross = top["gross_pl"]
        break_even = flat_gross / (mgmt * years) if flat_gross > 0 else None
        # past the knee, how much either side's number still moves with the
        # AUM, as a share of its level: zero is "the fee stopped growing where
        # the position did", which is the claim for the deployed basis
        past = [c for c in col if knee is not None and c["aum"] > knee] or [top]
        spread = {b: {side: (max(c[b][side] for c in past) - min(c[b][side] for c in past))
                            / max(abs(np.mean([c[b][side] for c in past])), 1.0)
                      for side in ("manager_take", "investor_net")} for b in BASES}
        out.append({
            "participation": rate,
            "knee_aum": knee,
            "flat_gross_pl": flat_gross,
            "break_even_aum": break_even,
            "break_even_over_knee": (break_even / knee) if (break_even and knee) else None,
            "investor_best_aum": {b: best[b]["aum"] for b in best},
            "investor_best_net": {b: best[b][b]["investor_net"] for b in best},
            "manager_best_aum": {b: manager[b]["aum"] for b in manager},
            "manager_take_at_top": {b: top[b]["manager_take"] for b in BASES},
            "investor_net_at_top": {b: top[b]["investor_net"] for b in BASES},
            "spread_past_knee": spread,
        })
    return out


def rate_alignment(cells: list[dict], basis: str = "on_aum") -> list[dict]:
    """Per AUM: the rate each side of the fee would choose, against the Sharpe.

    The other axis. At a fixed AUM the gross rises with the rate until the
    budget stops binding and then stops rising, because a more aggressive
    desk past that point is paying more impact for the same position; the
    fee is a share of the gross, so the manager's best rate and the
    investor's are the same rung. The Sharpe is highest at the most patient
    rate in every row, and nobody's dollars are.
    """
    out = []
    for aum in sorted({c["aum"] for c in cells}):
        row = sorted((c for c in cells if c["aum"] == aum), key=lambda c: c["participation"])
        investor = max(row, key=lambda c: c[basis]["investor_net"])
        manager = max(row, key=lambda c: c[basis]["manager_take"])
        patient = max(row, key=lambda c: c["sharpe"])
        out.append({
            "aum": aum,
            "investor_best_rate": investor["participation"],
            "manager_best_rate": manager["participation"],
            "sharpe_best_rate": patient["participation"],
            "sharpe_at_investor_best": investor["sharpe"],
            "sharpe_best": patient["sharpe"],
            "investor_net_at_best": investor[basis]["investor_net"],
            "investor_net_at_sharpe_best": patient[basis]["investor_net"],
        })
    return out


def print_fee_surface(cells: list[dict], alignment: list[dict], by_aum: list[dict],
                      mgmt: float = MGMT_FEE, perf: float = PERF_FEE) -> None:
    aums = sorted({c["aum"] for c in cells})
    rates = sorted({c["participation"] for c in cells})
    cell = {(c["aum"], c["participation"]): c for c in cells}
    years = cells[0]["years"]
    print(f"\n12. PUT A FEE ON THE SURFACE   {mgmt:.0%} and {perf:.0%}, {years:.1f} years as one lump, "
          f"no high-water mark")
    print(f"\n   the investor's net, $m, fee charged on the AUM:")
    print(f"\n{'AUM':>9} " + " ".join(f"{p:>9.0%}" for p in rates))
    for aum in aums:
        print(f"{aum / 1e9:>7.1f}bn " + " ".join(
            f"{cell[(aum, p)]['on_aum']['investor_net'] / 1e6:>9,.0f}" for p in rates))
    print(f"\n   and the manager's take, $m, same basis:")
    print(f"\n{'AUM':>9} " + " ".join(f"{p:>9.0%}" for p in rates))
    for aum in aums:
        print(f"{aum / 1e9:>7.1f}bn " + " ".join(
            f"{cell[(aum, p)]['on_aum']['manager_take'] / 1e6:>9,.0f}" for p in rates))
    print(f"\n   Where each side would stop, per rate:")
    print(f"\n{'rate':>5} {'knee':>8} {'gross past knee':>16} {'fee eats it at':>15} {'x knee':>7} "
          f"{'investor best':>14} {'manager best':>13} {'take at top':>12} {'on deployed':>12}")
    for a in alignment:
        knee = f"{a['knee_aum'] / 1e9:.0f}bn" if a["knee_aum"] else "never"
        be = f"{a['break_even_aum'] / 1e9:.1f}bn" if a["break_even_aum"] else "-"
        mult = f"{a['break_even_over_knee']:.1f}" if a["break_even_over_knee"] else "-"
        print(f"{a['participation']:>5.0%} {knee:>8} {a['flat_gross_pl'] / 1e9:>15.2f}bn {be:>15} {mult:>7} "
              f"{a['investor_best_aum']['on_aum'] / 1e9:>12.0f}bn {a['manager_best_aum']['on_aum'] / 1e9:>11.0f}bn "
              f"{a['manager_take_at_top']['on_aum'] / 1e9:>10.1f}bn {a['manager_take_at_top']['on_deployed'] / 1e9:>10.2f}bn")
    print(f"\n   And along the other axis, at each AUM, the rate each side would pick "
          f"(fee on the AUM):")
    print(f"\n{'AUM':>9} {'investor':>9} {'manager':>8} {'best Sharpe at':>15} {'Sharpe there':>13} "
          f"{'Sharpe at the pick':>19} {'net at the pick':>16} {'net at best Sharpe':>19}")
    for r in by_aum:
        print(f"{r['aum'] / 1e9:>7.1f}bn {r['investor_best_rate']:>9.0%} {r['manager_best_rate']:>8.0%} "
              f"{r['sharpe_best_rate']:>15.0%} {r['sharpe_best']:>13.2f} {r['sharpe_at_investor_best']:>19.2f} "
              f"{r['investor_net_at_best'] / 1e6:>14,.0f}m {r['investor_net_at_sharpe_best'] / 1e6:>17,.0f}m")


# ---------- 13. crystallize the fee on the curve ----------

def year_end_equity(equity: pd.Series) -> list[list]:
    """The equity curve at the last bar of each calendar year, as
    [date, equity, bars in the year].

    Section 12 charged ten years as one lump because the surface's rows
    kept nothing of the curve but its ends. This is the curve at yearly
    resolution, which is what a fee that crystallizes once a year needs
    and all it needs; kept on every sized row so a fee study is arithmetic
    on the results file rather than a 45-second re-run. The bar count is
    there so a year's management fee can be prorated the way section 12's
    lump was (bars over 252), and the two agree to the dollar on it.
    """
    by_year = equity.groupby(equity.index.year)
    return [[f"{group.index[-1]:%Y-%m-%d}", float(group.iloc[-1]), int(len(group))]
            for _, group in by_year]


def crystallized_fee(year_ends: list[list], aum: float, capital: float,
                     mgmt: float = MGMT_FEE, perf: float = PERF_FEE,
                     bars_per_year: int = TRADING_DAYS) -> dict:
    """Two-and-twenty as a fund actually charges it, year by year on the curve.

    At each year-end: the management fee for the year comes off, then the
    incentive fee is a fifth of whatever the net asset value is above its
    high-water mark, and the mark moves up to the new net. In a down year
    nothing is paid and nothing is refunded, and the mark stays where it
    was until the fund earns back through it. The investor's net is the
    gross P&L less every fee paid; it is also what the final NAV says, which
    the test checks.

    `capital` is what the management fee is charged on - the AUM, or the
    position the budget let the desk hold - and `aum` is the starting NAV.
    The gross P&L each year is taken off the curve, which is the strategy's
    dollars on a fixed position: fees come out of the investor's pocket, not
    the desk's book, so paying them does not change what the desk trades.
    Each year's management fee is prorated by its bars over `bars_per_year`,
    which is how section 12 counted the lump's ten years, so the two
    management totals match to the dollar and whatever is left between the
    two takes is the incentive fee and nothing else.
    """
    nav = aum
    mark = aum
    prev = aum
    paid = {"management": 0.0, "incentive": 0.0}
    years_paid, years_under = 0, 0
    schedule = []
    for date, gross_equity, bars_in_year in year_ends:
        gain = gross_equity - prev
        prev = gross_equity
        fee = mgmt * capital * bars_in_year / bars_per_year
        pre = nav + gain - fee
        incentive = perf * max(pre - mark, 0.0)
        nav = pre - incentive
        paid["management"] += fee
        paid["incentive"] += incentive
        if incentive > 0:
            years_paid += 1
        if nav < mark:
            years_under += 1
        mark = max(mark, nav)
        schedule.append({"year": date[:4], "gross_gain": gain, "management": fee,
                         "incentive": incentive, "nav": nav, "high_water_mark": mark})
    take = paid["management"] + paid["incentive"]
    gross = prev - aum
    # How far under its mark the fund finishes. The yearly incentive is the
    # lump's plus a fifth of this, exactly, whenever the lump paid anything:
    # every dollar the fund ever stood above its mark was charged once and
    # the ones it later gave back were not refunded (tested).
    return {**paid, "manager_take": take, "investor_net": gross - take,
            "investor_net_return": (gross - take) / aum,
            "years": len(year_ends), "years_incentive_paid": years_paid,
            "years_ending_under_the_mark": years_under,
            "final_nav": nav, "high_water_mark": mark,
            "mark_shortfall": mark - nav, "schedule": schedule}


def crystallized_surface(rows: list[dict], lump: list[dict], mgmt: float = MGMT_FEE,
                         perf: float = PERF_FEE) -> list[dict]:
    """Section 12's lump beside the yearly fee, on every cell, both bases.

    The lump was the floor on the incentive fee, and the gap between the
    two is the price of the high-water mark being a floor and not a refund:
    the up years' fifth, paid, minus the down years' fifth, which is never
    handed back. On the management fee the two agree to the dollar, since
    2% a year for ten years is 20% whether it is paid once or ten times.
    """
    floor = {(c["aum"], c["participation"]): c for c in lump}
    out = []
    for r in rows:
        key = (r["aum"], r["participation"])
        cell = {"aum": r["aum"], "participation": r["participation"],
                "gross_pl": r["final"] - r["aum"], "position_share": r["position_share"]}
        for basis, capital in (("on_aum", r["aum"]),
                               ("on_deployed", r["aum"] * r["position_share"])):
            yearly = crystallized_fee(r["year_ends"], r["aum"], capital, mgmt, perf)
            one = floor[key][basis] if key in floor else None
            cell[basis] = {
                **{k: v for k, v in yearly.items() if k != "schedule"},
                "lump_take": one["manager_take"] if one else None,
                "lump_incentive": one["incentive"] if one else None,
                "over_the_lump": (yearly["manager_take"] - one["manager_take"]) if one else None,
                "over_the_lump_share": ((yearly["manager_take"] - one["manager_take"]) / one["manager_take"]
                                        if one and one["manager_take"] > 0 else None),
            }
        cell["schedule_on_aum"] = crystallized_fee(r["year_ends"], r["aum"], r["aum"],
                                                   mgmt, perf)["schedule"]
        out.append(cell)
    return out


def crystallized_alignment(cells: list[dict], knees: list[dict]) -> list[dict]:
    """Per rate: whether the yearly fee moves where either side would stop.

    Section 12's alignment was on the lump. The incentive fee is the part
    that changes, and it is a share of the P&L, which is flat past the
    knee; so the prediction is that the investor's best AUM does not move,
    the manager's does not move, and the whole of the extra take is the
    same dollars in every cell past the knee at a rate.
    """
    knee_at = {k["participation"]: k["knee_aum"] for k in knees}
    out = []
    for rate in sorted({c["participation"] for c in cells}):
        col = sorted((c for c in cells if c["participation"] == rate), key=lambda c: c["aum"])
        knee = knee_at.get(rate)
        past = [c for c in col if knee is not None and c["aum"] >= knee] or [col[-1]]
        extra = [c["on_aum"]["over_the_lump"] for c in past]
        out.append({
            "participation": rate,
            "knee_aum": knee,
            "investor_best_aum": {b: max(col, key=lambda c: c[b]["investor_net"])["aum"] for b in BASES},
            "manager_best_aum": {b: max(col, key=lambda c: c[b]["manager_take"])["aum"] for b in BASES},
            "incentive_at_top": {b: col[-1][b]["incentive"] for b in BASES},
            "lump_incentive_at_top": {b: col[-1][b]["lump_incentive"] for b in BASES},
            "extra_take_past_knee": {"low": min(extra), "high": max(extra)},
            "years_paid_at_top": col[-1]["on_aum"]["years_incentive_paid"],
            "years_under_at_top": col[-1]["on_aum"]["years_ending_under_the_mark"],
        })
    return out


def print_crystallized(cells: list[dict], alignment: list[dict], mgmt: float = MGMT_FEE,
                       perf: float = PERF_FEE) -> None:
    aums = sorted({c["aum"] for c in cells})
    rates = sorted({c["participation"] for c in cells})
    cell = {(c["aum"], c["participation"]): c for c in cells}
    print(f"\n13. CRYSTALLIZE THE FEE ON THE CURVE   {mgmt:.0%} and {perf:.0%} paid yearly, "
          f"high-water mark, fee on the AUM")
    print(f"\n   the manager's take, $m, and in brackets what it is over section 12's lump:")
    print(f"\n{'AUM':>9} " + " ".join(f"{p:>16.0%}" for p in rates))
    for aum in aums:
        print(f"{aum / 1e9:>7.1f}bn " + " ".join(
            f"{cell[(aum, p)]['on_aum']['manager_take'] / 1e6:>8,.0f} "
            f"({cell[(aum, p)]['on_aum']['over_the_lump'] / 1e6:>+5,.0f})" for p in rates))
    print(f"\n   incentive fee alone, $m, yearly against the lump, with the years it was paid in:")
    print(f"\n{'AUM':>9} " + " ".join(f"{p:>18.0%}" for p in rates))
    for aum in aums:
        print(f"{aum / 1e9:>7.1f}bn " + " ".join(
            f"{cell[(aum, p)]['on_aum']['incentive'] / 1e6:>6,.0f} vs "
            f"{cell[(aum, p)]['on_aum']['lump_incentive'] / 1e6:>5,.0f} "
            f"{cell[(aum, p)]['on_aum']['years_incentive_paid']:>2}y" for p in rates))
    print(f"\n   Per rate, whether the yearly fee moves anybody:")
    print(f"\n{'rate':>5} {'knee':>8} {'investor best':>14} {'manager best':>13} "
          f"{'extra past knee':>17} {'incentive at top':>17} {'lump':>8} {'paid in':>8} {'under':>6}")
    for a in alignment:
        knee = f"{a['knee_aum'] / 1e9:.0f}bn" if a["knee_aum"] else "never"
        lo, hi = a["extra_take_past_knee"]["low"] / 1e6, a["extra_take_past_knee"]["high"] / 1e6
        print(f"{a['participation']:>5.0%} {knee:>8} {a['investor_best_aum']['on_aum'] / 1e9:>12.0f}bn "
              f"{a['manager_best_aum']['on_aum'] / 1e9:>11.0f}bn {lo:>7,.0f}-{hi:<8,.0f} "
              f"{a['incentive_at_top']['on_aum'] / 1e6:>15,.0f}m {a['lump_incentive_at_top']['on_aum'] / 1e6:>7,.0f}m "
              f"{a['years_paid_at_top']:>5}/{len(cells[0]['schedule_on_aum'])} {a['years_under_at_top']:>6}")
    top = cell[(aums[-1], rates[0])]
    print(f"\n   The year by year at ${aums[-1] / 1e9:.0f}bn and {rates[0]:.0%}, fee on the AUM, $m:")
    print(f"\n{'year':>6} {'gross gain':>11} {'mgmt':>8} {'incentive':>10} {'NAV':>10} {'mark':>10}")
    for y in top["schedule_on_aum"]:
        print(f"{y['year']:>6} {y['gross_gain'] / 1e6:>+11,.0f} {y['management'] / 1e6:>8,.0f} "
              f"{y['incentive'] / 1e6:>10,.0f} {y['nav'] / 1e6:>10,.0f} {y['high_water_mark'] / 1e6:>10,.0f}")


# ---------- 14. pay the fee out of the fund ----------

def yearly_gains(cell: dict) -> list[float]:
    """The gross dollars a cell's fixed position made in each calendar year,
    read off its `year_ends`."""
    ends = [cell["aum"]] + [y[1] for y in cell["year_ends"]]
    return [b - a for a, b in zip(ends, ends[1:])]


def gain_at(column: list[dict], nav: float, year: int) -> float:
    """What the column says a fund of `nav` made in `year`, between the rungs.

    Linear in the AUM between the two surface cells either side of the NAV,
    through the origin below the smallest rung, flat above the largest.
    Below the knee the position is the AUM, so the dollars scale with it and
    the line between rungs is the cost curve's bend; from the knee up every
    cell at a rate is the same run (section 11), so the line is flat and the
    NAV does not matter. Capacity enters only through which side of the
    knee the NAV sits on, which is the whole point of walking it this way
    rather than re-running the engine at every NAV the fund passes through.
    """
    aums = [0.0] + [c["aum"] for c in column]
    gains = [0.0] + [yearly_gains(c)[year] for c in column]
    return float(np.interp(nav, aums, gains))


def fund_walk(column: list[dict], start: float, knee: float | None,
              mgmt: float = MGMT_FEE, perf: float = PERF_FEE,
              bars_per_year: int = TRADING_DAYS) -> dict:
    """Run the fund on its own NAV: gains stay in, fees come out, and the
    position each year is whatever the NAV can hold.

    Section 13 charged the fee on a fixed position and took it out of the
    investor's pocket, so the desk traded the same shares whether or not
    anyone was paid. This is the other arrangement, and the usual one: the
    fund is the only pot. Each year the desk sizes on the NAV, the year's
    gross comes off the surface at that NAV, the management fee comes out
    of the NAV, a fifth of what stands above the mark follows it, and next
    year's position is sized on what is left. A fund that pays itself out
    of its own book trades a smaller book for it, and a fund that keeps its
    winnings trades a bigger one - so past the knee the gross is still
    section 11's flat dollars, but the fee basis moves every year, and
    under the knee the gross moves with it.
    """
    nav = mark = start
    paid = {"management": 0.0, "incentive": 0.0}
    gross = 0.0
    schedule = []
    crossed_down = crossed_up = None
    for i, (date, _, bars) in enumerate(column[0]["year_ends"]):
        sized_on = nav
        gain = gain_at(column, nav, i)
        fee = mgmt * nav * bars / bars_per_year
        pre = nav + gain - fee
        incentive = perf * max(pre - mark, 0.0)
        nav = pre - incentive
        mark = max(mark, nav)
        gross += gain
        paid["management"] += fee
        paid["incentive"] += incentive
        if knee is not None:
            # Only a fund that STARTED on one side counts as crossing to the
            # other: a cell sitting on the knee rung dips under it in any
            # losing year and climbs back, which is the rung, not a crossing.
            if start > knee > nav and crossed_down is None:
                crossed_down = date[:4]
            if start < knee <= nav and crossed_up is None:
                crossed_up = date[:4]
        schedule.append({"year": date[:4], "sized_on": sized_on, "gross_gain": gain,
                         "management": fee, "incentive": incentive, "nav": nav,
                         "high_water_mark": mark,
                         "under_the_knee": (knee is not None and sized_on < knee)})
    take = paid["management"] + paid["incentive"]
    return {**paid, "manager_take": take, "gross_pl": gross, "investor_net": gross - take,
            "investor_net_return": (gross - take) / start, "start": start, "final_nav": nav,
            "nav_change": nav / start - 1.0, "high_water_mark": mark,
            "crossed_knee_down": crossed_down, "crossed_knee_up": crossed_up,
            "schedule": schedule}


def fee_capacity(column: list[dict], knee: float | None, mgmt: float = MGMT_FEE,
                 perf: float = PERF_FEE, position_at_knee: float = 1.0) -> dict:
    """The AUM a self-paying fund drifts toward, and how fast.

    Past the knee the gross is a fixed G a year. The NAV moves by the gross
    less the fees, which is (1 - perf) x (G - mgmt x NAV) in a year the mark
    is cleared and G - mgmt x NAV in one it is not; either way it is zero at
    NAV = G / mgmt, positive under it and negative over it. So a fund past
    its knee is pulled toward G / mgmt from both sides - that is the AUM at
    which the management fee alone is the whole gross and the investor's
    net is nothing - and the incentive fee does not move the point, only
    the speed, because at the point there is nothing above the mark to take
    a fifth of. The pull is proportional, at a rate of (1 - perf) x mgmt a
    year, so the half-life of the distance is ln 2 over that: 43 years at
    two-and-twenty. G is the mean yearly gross of the cells from the knee
    up, which section 11 showed are one run.

    The knee is a rung of the grid, not the point the budget binds at; the
    dollars the budget lets the desk hold are the knee times the position
    share there (`budget_aum`), and the fee capacity over THAT is the gross
    return on the deployed position over the management fee - a ratio with
    no AUM in it, which is why it is the number to quote.
    """
    past = [c for c in column if knee is not None and c["aum"] >= knee] or [column[-1]]
    flat = float(np.mean([sum(yearly_gains(c)) / len(c["year_ends"]) for c in past]))
    rate = (1.0 - perf) * mgmt
    budget = knee * position_at_knee if knee else None
    return {"flat_gross_per_year": flat, "fee_capacity": flat / mgmt if mgmt else None,
            "knee_aum": knee, "budget_aum": budget,
            "return_on_budget": (flat / budget) if budget else None,
            "capacity_over_budget": (flat / mgmt / budget) if budget and mgmt else None,
            "drift_rate": rate, "half_life_years": float(np.log(2) / rate) if rate else None}


def self_financed_surface(rows: list[dict], knees: list[dict], crystallized: list[dict],
                          mgmt: float = MGMT_FEE, perf: float = PERF_FEE) -> list[dict]:
    """Section 13's fee beside the self-paid one, on every cell of the surface.

    The prediction, written first. Past the knee the desk's position is the
    budget, not the NAV, so the gross is the same flat dollars whichever pot
    pays, and the only thing that moves is the management-fee basis: up
    where the fund starts under its fee capacity, because the winnings it
    keeps outgrow the fee it pays, and down where it starts over it. So the
    manager's take should be above section 13's in the cells between the
    knee and G / mgmt and below it in the cells past G / mgmt - and no cell
    on the surface should be pushed back under its knee inside a decade,
    because the pull is 1.6% a year and the nearest cell above a knee is a
    factor of two away. Under the knee the gross is not flat, and a fund
    that keeps a doubling decade's winnings should grow through the knee
    before the decade is out.
    """
    knee_at = {k["participation"]: k for k in knees}
    fixed = {(c["aum"], c["participation"]): c["on_aum"] for c in crystallized}
    out = []
    for rate in sorted({r["participation"] for r in rows}):
        col = sorted((r for r in rows if r["participation"] == rate), key=lambda r: r["aum"])
        k = knee_at.get(rate, {})
        knee = k.get("knee_aum")
        cap = fee_capacity(col, knee, mgmt, perf, k.get("position_at_knee", 1.0))
        for r in col:
            walk = fund_walk(col, r["aum"], knee, mgmt, perf)
            pocket = fixed.get((r["aum"], rate))
            out.append({"aum": r["aum"], "participation": rate, "knee_aum": knee,
                        **{key: cap[key] for key in ("fee_capacity", "budget_aum", "flat_gross_per_year",
                                                     "return_on_budget", "capacity_over_budget",
                                                     "half_life_years")},
                        "over_fee_capacity": cap["fee_capacity"] is not None and r["aum"] > cap["fee_capacity"],
                        "fixed_gross": r["final"] - r["aum"],
                        "fixed_take": pocket["manager_take"] if pocket else None,
                        "fixed_investor_net": pocket["investor_net"] if pocket else None,
                        **{key: v for key, v in walk.items() if key != "schedule"},
                        "gross_given_up": (r["final"] - r["aum"]) - walk["gross_pl"],
                        "take_over_fixed": (walk["manager_take"] - pocket["manager_take"]) if pocket else None,
                        "schedule": walk["schedule"]})
    return out


def print_self_financed(cells: list[dict], mgmt: float = MGMT_FEE, perf: float = PERF_FEE) -> None:
    aums = sorted({c["aum"] for c in cells})
    rates = sorted({c["participation"] for c in cells})
    cell = {(c["aum"], c["participation"]): c for c in cells}
    print(f"\n14. PAY THE FEE OUT OF THE FUND   {mgmt:.0%} and {perf:.0%} out of the NAV, "
          f"the position sized on what is left")
    print(f"\n   where the NAV finishes the decade, as a share of where it started "
          f"(* = started past its fee capacity):")
    print(f"\n{'AUM':>9} " + " ".join(f"{p:>10.0%}" for p in rates))
    for aum in aums:
        print(f"{aum / 1e9:>7.1f}bn " + " ".join(
            f"{1 + cell[(aum, p)]['nav_change']:>8.2f}x{'*' if cell[(aum, p)]['over_fee_capacity'] else ' '}"
            for p in rates))
    print(f"\n   the manager's take, $m, and in brackets what it is over section 13's "
          f"fee from the investor's pocket:")
    print(f"\n{'AUM':>9} " + " ".join(f"{p:>17.0%}" for p in rates))
    for aum in aums:
        print(f"{aum / 1e9:>7.1f}bn " + " ".join(
            f"{cell[(aum, p)]['manager_take'] / 1e6:>8,.0f} "
            f"({cell[(aum, p)]['take_over_fixed'] / 1e6:>+6,.0f})" for p in rates))
    print(f"\n   gross P&L the self-paid fund gave up against the fixed position, $m "
          f"(a smaller book in the years after a loss):")
    print(f"\n{'AUM':>9} " + " ".join(f"{p:>10.0%}" for p in rates))
    for aum in aums:
        print(f"{aum / 1e9:>7.1f}bn " + " ".join(
            f"{cell[(aum, p)]['gross_given_up'] / 1e6:>10,.0f}" for p in rates))
    print(f"\n   Per rate: the knee rung, the position the budget lets the desk hold, the flat gross "
          f"past the knee, and the AUM at which {mgmt:.0%} of it is that gross:")
    print(f"\n{'rate':>5} {'knee':>6} {'budget':>8} {'gross/yr':>9} {'on budget':>10} "
          f"{'fee capacity':>13} {'x budget':>9} {'crossed up':>14} {'crossed down':>14}")
    for p in rates:
        first = cell[(aums[0], p)]
        col = [cell[(a, p)] for a in aums]
        up = [f"{c['aum'] / 1e9:g}bn in {c['crossed_knee_up']}" for c in col if c["crossed_knee_up"]]
        down = [f"{c['aum'] / 1e9:g}bn in {c['crossed_knee_down']}" for c in col if c["crossed_knee_down"]]
        knee = f"{first['knee_aum'] / 1e9:.0f}bn" if first["knee_aum"] else "never"
        budget = f"{first['budget_aum'] / 1e9:.1f}bn" if first["budget_aum"] else "-"
        print(f"{p:>5.0%} {knee:>6} {budget:>8} {first['flat_gross_per_year'] / 1e6:>8,.0f}m "
              f"{first['return_on_budget'] or float('nan'):>9.1%} {first['fee_capacity'] / 1e9:>11.1f}bn "
              f"{first['capacity_over_budget'] or float('nan'):>9.1f} "
              f"{', '.join(up) or '-':>14} {', '.join(down) or '-':>14}")
    print(f"\n   The pull toward the fee capacity is {(1 - perf) * mgmt:.1%} of the distance a year, "
          f"a half-life of {cells[0]['half_life_years']:.0f} years.")
    one = cell[(aums[-1], rates[0])]
    print(f"\n   The year by year at ${aums[-1] / 1e9:.0f}bn and {rates[0]:.0%}, $m:")
    print(f"\n{'year':>6} {'sized on':>10} {'gross gain':>11} {'mgmt':>8} {'incentive':>10} {'NAV':>10}")
    for y in one["schedule"]:
        print(f"{y['year']:>6} {y['sized_on'] / 1e6:>10,.0f} {y['gross_gain'] / 1e6:>+11,.0f} "
              f"{y['management'] / 1e6:>8,.0f} {y['incentive'] / 1e6:>10,.0f} {y['nav'] / 1e6:>10,.0f}")


# Section 15: the same walk at other fee schedules. The point of attraction
# has only the management fee in it, so this is where that claim gets its
# test - and the investor's side, which section 14 did not look at.
FEE_SCHEDULES = ((0.02, 0.20), (0.01, 0.10), (0.00, 0.30), (0.02, 0.00))


def indifference_return(a: tuple, b: tuple) -> float | None:
    """The gross return on the NAV at which an investor is paid the same under
    schedule `a` as under `b`, each a (mgmt, perf) pair.

    In a year the mark is cleared the investor keeps (1 - perf) of the gross
    less the management fee, so with g the gross as a share of the NAV the
    two schedules pay the same at (1 - p_a)(g - m_a) = (1 - p_b)(g - m_b),
    which is g* = ((1 - p_a) m_a - (1 - p_b) m_b) / (p_b - p_a). Above g*
    the schedule with the higher incentive fee costs the investor more;
    below it the one with the higher management fee does. Two schedules
    with the same incentive fee never cross - the lower management fee is
    cheaper at every return - and that is the None.
    """
    (ma, pa), (mb, pb) = a, b
    if pa == pb:
        return None
    return ((1.0 - pa) * ma - (1.0 - pb) * mb) / (pb - pa)


def fee_schedule_sweep(rows: list[dict], knees: list[dict],
                       schedules=FEE_SCHEDULES) -> list[dict]:
    """Section 14's walk on every cell of the surface at each fee schedule.

    The prediction, written first. The fee capacity is G / mgmt, so halving
    the management fee doubles it and dropping it removes it: at 1-and-10
    the "four to six times the position" becomes eight to twelve, and at
    0-and-30 there is no size at which the manager is the only investor,
    because an incentive fee is only ever a share of a profit. The pull is
    (1 - perf) x mgmt, so 1-and-10 drifts at 0.9% a year and 0-and-30 does
    not drift at all; 2-and-0 drifts fastest, at the full 2%. On the
    investor's side the algebra in `indifference_return` says 0-and-30 is
    the cheaper schedule below a 16% gross return on the NAV and 1-and-10
    below 7%, and no cell on the surface grosses 16% on its NAV, so the
    investor should prefer 0-and-30 in every cell and 1-and-10 in every
    cell past the AUM at which the flat gross is 7% of it. The high-water
    mark is the thing the algebra leaves out: in a cell that loses first,
    the incentive fee is not taken until the loss is made back, which is
    cheaper for the investor than the formula's share, and should bend the
    realized split toward the schedules that lean on the incentive fee.
    """
    knee_at = {k["participation"]: k for k in knees}
    out = []
    for rate in sorted({r["participation"] for r in rows}):
        col = sorted((r for r in rows if r["participation"] == rate), key=lambda r: r["aum"])
        k = knee_at.get(rate, {})
        knee = k.get("knee_aum")
        for mgmt, perf in schedules:
            cap = fee_capacity(col, knee, mgmt, perf, k.get("position_at_knee", 1.0))
            for r in col:
                walk = fund_walk(col, r["aum"], knee, mgmt, perf)
                out.append({"mgmt": mgmt, "perf": perf, "aum": r["aum"], "participation": rate,
                            "knee_aum": knee, "fee_capacity": cap["fee_capacity"],
                            "capacity_over_budget": cap["capacity_over_budget"],
                            "half_life_years": cap["half_life_years"],
                            "flat_gross_per_year": cap["flat_gross_per_year"],
                            "over_fee_capacity": (cap["fee_capacity"] is not None
                                                  and r["aum"] > cap["fee_capacity"]),
                            **{key: walk[key] for key in ("management", "incentive", "manager_take",
                                                          "gross_pl", "investor_net", "final_nav",
                                                          "nav_change", "high_water_mark",
                                                          "crossed_knee_down", "crossed_knee_up")},
                            "ends_at_the_mark": walk["final_nav"] >= walk["high_water_mark"] * (1 - 1e-12),
                            "gross_on_nav_per_year": walk["gross_pl"] / r["aum"] / len(walk["schedule"]),
                            "investor_share": (walk["investor_net"] / walk["gross_pl"]
                                               if walk["gross_pl"] > 0 else None)})
    return out


def schedule_comparison(cells: list[dict], base: tuple = (MGMT_FEE, PERF_FEE)) -> list[dict]:
    """Each schedule against the base, cell by cell: who is paid more, and
    whether the sign is the one the indifference return says.

    The algebra's call for a cell is made on the decade's mean gross as a
    share of the starting NAV; the realized call is the walk's investor net.
    They differ where the high-water mark or a moving NAV got between the
    formula and the fund, and `agrees` counts how often that mattered.
    """
    by = {(c["mgmt"], c["perf"], c["aum"], c["participation"]): c for c in cells}
    out = []
    for c in cells:
        if (c["mgmt"], c["perf"]) == base:
            continue
        ref = by[(base[0], base[1], c["aum"], c["participation"])]
        g_star = indifference_return(base, (c["mgmt"], c["perf"]))
        if g_star is None:
            # same incentive fee: the lower management fee is cheaper, full stop
            predicted = c["mgmt"] < base[0]
        else:
            # above g* the higher incentive fee costs more, below it the
            # higher management fee does; which schedule that favours
            # depends on which side of the base this one sits
            higher_perf = c["perf"] > base[1]
            predicted = (ref["gross_on_nav_per_year"] < g_star) == higher_perf
        gap = c["investor_net"] - ref["investor_net"]
        # to the dollar: a cell no year's gross clears the fee in is charged
        # no incentive under either schedule, and the two are the same fee
        tie = abs(gap) < 1.0
        realized = gap > 0
        out.append({"mgmt": c["mgmt"], "perf": c["perf"], "aum": c["aum"],
                    "participation": c["participation"],
                    "indifference_return": g_star,
                    "indifference_aum": (c["flat_gross_per_year"] / g_star
                                         if g_star and g_star > 0 else None),
                    "gross_on_nav_per_year": ref["gross_on_nav_per_year"],
                    "investor_net_over_base": gap,
                    "take_over_base": c["manager_take"] - ref["manager_take"],
                    "investor_prefers_this": realized, "tie": tie,
                    "algebra_prefers_this": predicted,
                    "agrees": tie or realized == predicted})
    return out


def print_fee_schedules(cells: list[dict], comparison: list[dict],
                        base: tuple = (MGMT_FEE, PERF_FEE)) -> None:
    schedules = sorted({(c["mgmt"], c["perf"]) for c in cells}, key=lambda s: (-s[0], s[1]))
    aums = sorted({c["aum"] for c in cells})
    rates = sorted({c["participation"] for c in cells})
    cell = {(c["mgmt"], c["perf"], c["aum"], c["participation"]): c for c in cells}
    label = {s: f"{s[0] * 100:g}-and-{s[1] * 100:g}" for s in schedules}
    print(f"\n15. THE SAME WALK AT OTHER FEE SCHEDULES   "
          f"{', '.join(label[s] for s in schedules)}, out of the NAV")
    print(f"\n   Per schedule and rate: the fee capacity as a multiple of the position, "
          f"and the half-life of the pull toward it:")
    print(f"\n{'rate':>5} " + " ".join(f"{label[s]:>19}" for s in schedules))
    for p in rates:
        parts = []
        for s in schedules:
            c = cell[(s[0], s[1], aums[0], p)]
            if c["fee_capacity"] is None:
                parts.append(f"{'none':>12} {'-':>6}")
            elif c["capacity_over_budget"] is None:
                # no knee on this column, so no position to quote it against
                parts.append(f"{c['fee_capacity'] / 1e9:>10.1f}bn {c['half_life_years']:>6.0f}y")
            else:
                parts.append(f"{c['capacity_over_budget']:>9.1f}x {c['half_life_years']:>6.0f}y")
        print(f"{p:>5.0%} " + " ".join(f"{x:>19}" for x in parts))
    print(f"\n   The investor's decade, net of fees, $m, at each schedule "
          f"(* = started past the schedule's fee capacity):")
    for s in schedules:
        print(f"\n   {label[s]}")
        print(f"{'AUM':>9} " + " ".join(f"{p:>10.0%}" for p in rates))
        for aum in aums:
            print(f"{aum / 1e9:>7.1f}bn " + " ".join(
                f"{cell[(s[0], s[1], aum, p)]['investor_net'] / 1e6:>9,.0f}"
                f"{'*' if cell[(s[0], s[1], aum, p)]['over_fee_capacity'] else ' '}" for p in rates))
    print(f"\n   The investor's share of the gross, where there was one, at {label[base]} "
          f"against the others (same cells, same gross):")
    print(f"\n{'AUM':>9} " + " ".join(f"{label[s]:>12}" for s in schedules))
    for aum in aums:
        mid = rates[len(rates) // 2]
        row = []
        for s in schedules:
            share = cell[(s[0], s[1], aum, mid)]["investor_share"]
            row.append(f"{share:>11.0%} " if share is not None else f"{'-':>12}")
        print(f"{aum / 1e9:>7.1f}bn " + " ".join(row) + f"   (at {mid:.0%})")
    print(f"\n   Against {label[base]}: the indifference gross return on the NAV, the AUM at "
          f"which the flat gross is that return, and how often the walk agreed with the algebra:")
    print(f"\n{'schedule':>12} {'g*':>6} {'rate':>5} {'indiff. AUM':>12} {'investor better':>16} "
          f"{'ties':>5} {'agrees':>7} {'of':>3}")
    for s in schedules:
        if s == base:
            continue
        for p in rates:
            rows = [c for c in comparison if (c["mgmt"], c["perf"]) == s and c["participation"] == p]
            g = rows[0]["indifference_return"]
            ind = rows[0]["indifference_aum"]
            better = sum(c["investor_prefers_this"] for c in rows)
            ties = sum(c["tie"] for c in rows)
            agree = sum(c["agrees"] for c in rows)
            # a g* at or under zero is a schedule the investor prefers at any profit
            g_text = "never" if g is None else ("any" if g <= 0 else f"{g:.0%}")
            print(f"{label[s]:>12} {g_text:>6} {p:>5.0%} "
                  f"{f'{ind / 1e9:.1f}bn' if ind else '-':>12} {better:>7} of {len(rows):<5} "
                  f"{ties:>5} {agree:>7} {len(rows):>3}")
    worst = max(cells, key=lambda c: c["manager_take"])
    print(f"\n   The largest take on the surface is {label[(worst['mgmt'], worst['perf'])]} at "
          f"${worst['aum'] / 1e9:.0f}bn and {worst['participation']:.0%}: "
          f"${worst['manager_take'] / 1e6:,.0f}m on ${worst['gross_pl'] / 1e6:,.0f}m gross.")


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


def _load_previous(path: str = OUT) -> dict:
    """What the last run wrote, so that one section can be re-run on its own."""
    if os.path.exists(path):
        with open(path) as fh:
            return json.load(fh)
    return {}


def _same_tape(previous: dict, header: dict) -> bool:
    """Whether a --section run is adding to results computed on the same bars.

    Symbol, dates and bar count have to match exactly. The medians only have
    to match to a basis point: yfinance's adjusted closes move at the fifth
    decimal between downloads as it re-rounds the dividend adjustment, and
    that is not a different tape, it is the same one printed again.
    """
    for key in ("symbol", "start", "end", "bars"):
        if previous.get(key) != header[key]:
            return False
    for key in ("median_price", "median_volume"):
        old = previous.get(key)
        if old is None or abs(old - header[key]) > 1e-4 * abs(header[key]):
            return False
    return True


def _sections(prices, opens, volumes, quick: bool) -> dict:
    """The study as a numbered table of sections.

    Each is a function from `have` - everything this run has computed so
    far, falling back to what the last run wrote to results/fill_realism.json
    - to the keys it adds. The numbers match the printout and RESULTS.md.
    Sections 8 and 9 print their rows beside section 7's and 8's, which is
    why they read those from `have` rather than recomputing them: run on its
    own, section 9 is printed against the section 8 that is on disk, and
    says so if there is none.
    """
    aums = AUMS[:3] if quick else AUMS
    depths = DEPTH_FRACS[:3] if quick else DEPTH_FRACS
    price = float(prices.iloc[:, 0].median())
    volume = float(volumes.iloc[:, 0].median())

    def need(have, key, section):
        if key not in have:
            raise SystemExit(f"section {section} must be in {OUT} first: "
                             f"run `python fill_realism.py --section {section}`")
        return have[key]

    def s1(have):
        curves = cost_curves(price, volume)
        cross = crossover(curves)
        print_cost_curves(curves, cross, price, volume, 0.002)
        by_shape = crossover_by_shape(price, volume)
        print_crossover_by_shape(by_shape, price, volume)
        by_decay = crossover_by_decay(price, volume)
        print_crossover_by_decay(by_decay, price, volume)
        vol_study = vol_elasticity_study(prices, opens, volumes)
        print_vol_elasticity(vol_study)
        return {"cost_curves": curves, "crossover": cross, "shape": SHAPE,
                "crossover_by_shape": by_shape, "crossover_by_decay": by_decay,
                "vol_elasticity": VOL_ELASTICITY, "vol_elasticity_study": vol_study}

    def s2(have):
        cap = capacity_under_both(prices, opens, volumes, aums=aums)
        print_capacity(cap)
        readable = interpretable_to(cap, "book")
        print(f"\n   Both Sharpe columns are only readable up to "
              f"${readable / 1e9:.1f}bn, where 90% of the book still reaches the "
              f"market.\n   Above that the rising Sharpe is idle cash, not "
              f"capacity - see interpretable_to().")
        return {"capacity": cap}

    def s3(have):
        sweep = depth_sweep(prices, opens, volumes, aum=5e9, depth_fracs=depths)
        print_depth_sweep(sweep, 5e9)
        return {"depth_sweep": sweep}

    def s4(have):
        bill = fee_bill(prices, opens, volumes)
        print_fees(bill)
        return {"fees": bill}

    def s5(have):
        memory = memory_sweep(prices, opens, volumes,
                              aums=(1e9, 5e9) if not quick else (5e9,))
        closes = memory_closes_the_gap(memory)
        print_memory_sweep(memory, closes)
        return {"persistence": PERSISTENCE, "recovery": RECOVERY,
                "memory_sweep": memory, "memory_closes_the_gap": closes}

    def s6(have):
        metaorders = metaorder_curves(price, volume)
        exponents = exponents_by_setting(metaorders)
        nearest = closest_to_square_root(exponents)
        print_metaorders(metaorders, exponents, nearest)
        return {"metaorders": metaorders, "impact_exponents": exponents,
                "closest_to_square_root": nearest}

    def s7(have):
        formula = formula_curves(price, volume)
        formula_exp = formula_exponents(formula)
        ladder = law_ladder(prices, opens, volumes, aums=aums)
        print_law_ladder(ladder, formula_exp, formula)
        return {"law_coef": LAW_COEF, "formula_curves": formula,
                "formula_exponents": formula_exp, "law_ladder": ladder}

    def s8(have):
        sized = sized_ladder(prices, opens, volumes, aums=aums)
        patience = fill_days_sweep(prices, opens, volumes,
                                   days=FILL_DAYS_SWEEP if not quick else (1, 5, None))
        baseline = time_in_market(prices, opens)
        print_sized_ladder(sized, patience, need(have, "law_ladder", 7), baseline, FILL_DAYS)
        return {"fill_days": FILL_DAYS, "time_in_market": baseline,
                "sized_ladder": sized, "fill_days_sweep": patience}

    def s9(have):
        trimmed = trimmed_ladder(prices, opens, volumes, aums=aums)
        ls_free = long_short_ladder(prices, opens, volumes, aums=aums, fill_days=None)
        ls_capped = long_short_ladder(prices, opens, volumes, aums=aums)
        ls_baseline = time_in_market(prices, opens, strategy=MovingAverageCrossLongShortStrategy)
        print_exit_and_flip(trimmed, need(have, "sized_ladder", 8), ls_free, ls_capped,
                            need(have, "time_in_market", 8), ls_baseline, FILL_DAYS)
        return {"trimmed_ladder": trimmed, "ls_borrow_bps": LS_BORROW_BPS,
                "long_short_free": ls_free, "long_short_capped": ls_capped,
                "long_short_time_in_market": ls_baseline}

    def s10(have):
        rates = participation_sweep(prices, opens, volumes,
                                    rates=PARTICIPATION_SWEEP if not quick else (0.05, 0.10, 0.20))
        boot = trim_bootstrap(prices, opens, volumes)
        print_participation(rates, boot, 2e10, FILL_DAYS)
        return {"participation_sweep": rates, "trim_bootstrap": boot}

    def s11(have):
        surface = capacity_surface(prices, opens, volumes,
                                   aums=SURFACE_AUMS if not quick else SURFACE_AUMS[::2],
                                   rates=PARTICIPATION_SWEEP if not quick else (0.05, 0.10, 0.20))
        knees = surface_knees(surface)
        print_surface(surface, knees, FILL_DAYS)
        return {"surface_aums": list(SURFACE_AUMS), "capacity_surface": surface,
                "surface_knees": knees}

    def s12(have):
        cells = fee_economics(need(have, "capacity_surface", 11), have["bars"])
        alignment = fee_alignment(cells, need(have, "surface_knees", 11))
        by_aum = rate_alignment(cells)
        print_fee_surface(cells, alignment, by_aum)
        return {"mgmt_fee": MGMT_FEE, "perf_fee": PERF_FEE, "fee_surface": cells,
                "fee_alignment": alignment, "rate_alignment": by_aum}

    def s13(have):
        surface = need(have, "capacity_surface", 11)
        if not all("year_ends" in r for r in surface):
            raise SystemExit("section 11 in the results file predates the yearly curve: "
                             "run `python fill_realism.py --section 11 --section 13`")
        cells = crystallized_surface(surface, need(have, "fee_surface", 12))
        alignment = crystallized_alignment(cells, need(have, "surface_knees", 11))
        print_crystallized(cells, alignment)
        return {"crystallized_surface": cells, "crystallized_alignment": alignment}

    def s14(have):
        surface = need(have, "capacity_surface", 11)
        if not all("year_ends" in r for r in surface):
            raise SystemExit("section 11 in the results file predates the yearly curve: "
                             "run `python fill_realism.py --section 11 --section 13 --section 14`")
        cells = self_financed_surface(surface, need(have, "surface_knees", 11),
                                      need(have, "crystallized_surface", 13))
        print_self_financed(cells)
        return {"self_financed_surface": cells}

    def s15(have):
        surface = need(have, "capacity_surface", 11)
        if not all("year_ends" in r for r in surface):
            raise SystemExit("section 11 in the results file predates the yearly curve: "
                             "run `python fill_realism.py --section 11 --section 15`")
        cells = fee_schedule_sweep(surface, need(have, "surface_knees", 11))
        comparison = schedule_comparison(cells)
        print_fee_schedules(cells, comparison)
        return {"fee_schedules": [list(s) for s in FEE_SCHEDULES],
                "fee_schedule_sweep": cells, "fee_schedule_comparison": comparison}

    return {1: s1, 2: s2, 3: s3, 4: s4, 5: s5, 6: s6, 7: s7, 8: s8, 9: s9, 10: s10, 11: s11,
            12: s12, 13: s13, 14: s14, 15: s15}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true",
                        help="three AUM levels and three depths instead of five")
    parser.add_argument("--section", type=int, action="append", metavar="N",
                        help="run only section N (repeatable) and merge it into the "
                             "results file, which keeps every other section as it was")
    args = parser.parse_args()

    prices = fetch_prices(SYMBOL, START, END)
    opens = fetch_opens(SYMBOL, START, END)
    volumes = fetch_volumes(SYMBOL, START, END)
    prices, opens, volumes = _align(prices, opens, volumes)
    print(f"{SYMBOL}  {prices.index[0].date()} to {prices.index[-1].date()}  "
          f"{len(prices)} bars")

    sections = _sections(prices, opens, volumes, args.quick)
    chosen = sorted(set(args.section)) if args.section else list(sections)
    unknown = [n for n in chosen if n not in sections]
    if unknown:
        raise SystemExit(f"no section {unknown}; the study has sections {min(sections)}-{max(sections)}")

    # A full run starts from nothing. A --section run starts from the file,
    # so the sections it does not touch stay exactly as the last run left them.
    results = _load_previous() if args.section else {}
    header = {"symbol": SYMBOL, "start": START, "end": END, "bars": len(prices),
              "median_price": float(prices.iloc[:, 0].median()),
              "median_volume": float(volumes.iloc[:, 0].median())}
    if args.section and results and not _same_tape(results, header):
        raise SystemExit(f"{OUT} was written for a different tape ({results.get('symbol')} "
                         f"{results.get('start')} to {results.get('end')}, "
                         f"{results.get('bars')} bars); run without --section")
    results.update(header)
    for n in chosen:
        results.update(sections[n](results))

    os.makedirs("results", exist_ok=True)
    with open(OUT, "w") as fh:
        json.dump(results, fh, indent=1, default=float)
    print(f"\nwrote {OUT}" + (f" (section{'s' if len(chosen) > 1 else ''} "
                              f"{', '.join(map(str, chosen))} replaced, the rest kept)"
                              if args.section else ""))


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
