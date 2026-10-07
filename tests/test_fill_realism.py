"""Tests for the fill-model comparison.

The study's three claims are each a function, and each is checkable without
touching the network: the cost curves are arithmetic on a known book, the
crossover is an interpolation between two of those rows, and the deployed-
capital cutoff is the thing that stops the capacity ladder being read wrong.

Deliberately not tested here: the numbers in notes/backtester.md section 12.
Those depend on ten years of downloaded prices and belong in the write-up with
a date on them, not in an assertion that breaks when Yahoo revises a split.
"""

import numpy as np
import pandas as pd
import pytest

from exchange.order import TICK
from fill_realism import (_weighted, cost_curves, crossover, crossover_by_shape,
                          depth_multipliers, interpretable_to)

PRICE = 100.0
VOLUME = 1_000_000.0


def curves(shape=0.0, **kwargs):
    """depth_frac 0.001 of a million shares is 1,000 shares a level.

    shape is pinned flat here rather than left at the study's default. These
    tests are arithmetic on a known book, and a flat book is the one whose
    arithmetic can be done by hand. The profile gets its own section below.
    """
    return cost_curves(PRICE, VOLUME, depth_frac=0.001, levels=10,
                       shape=shape, **kwargs)


# ---------- the cost curves ----------

def test_an_order_inside_the_touch_costs_one_tick():
    """0.01% of a million shares is 100, against 1,000 resting at the touch.
    One tick on a $100 stock is 1 basis point."""
    row = curves(size_fracs=(0.0001,))[0]
    assert row["levels_walked"] == 1
    assert row["book_bps"] == pytest.approx(TICK / PRICE * 10_000)


def test_the_book_cost_is_the_average_of_the_levels_consumed():
    """2,500 shares over levels of 1,000: 1,000 ticks at 1, 1,000 at 2, 500 at
    3, so 1.6 ticks a share on average."""
    row = curves(size_fracs=(0.0025,))[0]
    expected_ticks = (1_000 * 1 + 1_000 * 2 + 500 * 3) / 2_500
    assert row["book_bps"] == pytest.approx(expected_ticks * TICK / PRICE * 10_000)
    assert row["levels_walked"] == 3


def test_book_cost_rises_with_size_and_the_flat_charge_does_not():
    rows = curves(size_fracs=(0.0001, 0.001, 0.005, 0.01))
    book = [r["book_bps"] for r in rows]
    assert book == sorted(book)
    assert len({r["flat_bps"] for r in rows}) == 1


def test_an_order_past_the_last_level_is_reported_as_partly_unfilled():
    """10 levels x 1,000 shares is 10,000 fillable. 3% of a million is 30,000,
    so a third of it fills and the row says so rather than quoting a price for
    shares that never traded."""
    row = curves(size_fracs=(0.03,))[0]
    assert row["filled_frac"] == pytest.approx(10_000 / 30_000)
    assert row["levels_walked"] == 10


def test_a_deeper_book_is_cheaper_at_the_same_size():
    thin = cost_curves(PRICE, VOLUME, depth_frac=0.0005, levels=50, shape=0.0,
                       size_fracs=(0.005,))[0]
    thick = cost_curves(PRICE, VOLUME, depth_frac=0.005, levels=50, shape=0.0,
                        size_fracs=(0.005,))[0]
    assert thick["book_bps"] < thin["book_bps"]


def test_the_square_root_law_is_concave_in_size():
    """Not a property of this file - a property of the law it is reproducing -
    but if the comparison curve were linear the whole chart would be wrong."""
    rows = cost_curves(PRICE, VOLUME, size_fracs=(0.01, 0.04, 0.16),
                       shape=0.0)
    first = rows[1]["sqrt_bps"] - rows[0]["sqrt_bps"]
    second = rows[2]["sqrt_bps"] - rows[1]["sqrt_bps"]
    assert second < first * 4


# ---------- the crossover ----------

def test_the_crossover_lands_between_the_rows_that_bracket_it():
    rows = curves(size_fracs=(0.0001, 0.001, 0.003, 0.01))
    cross = crossover(rows)
    assert cross is not None
    below = [r for r in rows if r["book_bps"] <= r["flat_bps"]]
    above = [r for r in rows if r["book_bps"] > r["flat_bps"]]
    assert below[-1]["size_frac"] <= cross <= above[0]["size_frac"]


def test_a_book_deep_enough_to_never_cross_returns_none():
    """None is an answer, not a missing value: on a book this deep, a flat
    2 bps is conservative at every size tested."""
    rows = cost_curves(PRICE, VOLUME, depth_frac=0.5, levels=200, shape=0.0,
                       size_fracs=(0.0001, 0.001, 0.01))
    assert crossover(rows) is None


def test_a_thinner_book_crosses_sooner():
    sizes = (0.0001, 0.0005, 0.001, 0.005, 0.01, 0.05)
    thin = crossover(cost_curves(PRICE, VOLUME, depth_frac=0.0002,
                                 levels=500, size_fracs=sizes, shape=0.0))
    thick = crossover(cost_curves(PRICE, VOLUME, depth_frac=0.002,
                                  levels=500, size_fracs=sizes, shape=0.0))
    assert thin < thick


# ---------- the cutoff that keeps the ladder honest ----------

def _ladder(deployed):
    rows = [{"aum": None, "sharpe": 0.65}]
    for aum, dep in deployed:
        rows.append({"aum": aum, "book_deployed": dep, "sqrt_deployed": dep,
                     "book": {"sharpe": 0.6}, "sqrt": {"sharpe": 0.5}})
    return rows


def test_the_ladder_is_readable_up_to_the_last_fully_deployed_row():
    rows = _ladder([(1e8, 1.0), (1e9, 0.98), (5e9, 0.41), (2e10, 0.10)])
    assert interpretable_to(rows, "book") == 1e9


def test_a_ladder_that_never_deploys_is_readable_nowhere():
    """The case worth catching: every row mostly cash means there is no AUM at
    which the Sharpe column means anything, and the function says None rather
    than handing back the smallest row."""
    rows = _ladder([(1e9, 0.4), (5e9, 0.2)])
    assert interpretable_to(rows, "book") is None


def test_the_floor_is_the_caller_s_choice():
    rows = _ladder([(1e8, 1.0), (1e9, 0.5)])
    assert interpretable_to(rows, "book", floor=0.90) == 1e8
    assert interpretable_to(rows, "book", floor=0.40) == 1e9


# ---------- the depth profile ----------
#
# The claim section 1b makes is one-directional: shaping the book moves the
# crossover earlier and never later, and it does so without changing how much
# the book can absorb. Both halves need pinning, because the second is what
# separates a statement about the shape of liquidity from a statement about
# how much of it there is.


def test_shaping_the_book_cannot_make_an_order_cheaper():
    """Checked size by size rather than on the crossover alone.

    A crossover that moved the right way while some individual size got
    cheaper would mean the profile was doing something other than what it is
    documented to do.
    """
    flat = curves(shape=0.0)
    tilt = curves(shape=1.0)

    assert len(flat) == len(tilt)
    for f, t in zip(flat, tilt):
        assert f["size_frac"] == t["size_frac"]
        assert t["book_bps"] >= f["book_bps"] - 1e-9


def test_a_shaped_book_holds_the_same_shares_as_a_flat_one():
    """Capacity is total depth, and total depth is what the profile preserves.

    Tolerance is one share a level: rounding each level up to a whole share is
    the only thing that stops this being exact, and it is stated in
    book_execution.py rather than smoothed over here.
    """
    rows = crossover_by_shape(PRICE, VOLUME, depth_frac=0.001, levels=10)
    capacities = [r["capacity_shares"] for r in rows]

    assert max(capacities) - min(capacities) <= 10


def test_the_crossover_moves_earlier_as_the_book_is_shaped_harder():
    rows = crossover_by_shape(PRICE, VOLUME, depth_frac=0.001, levels=10,
                              shapes=(0.0, 0.5, 1.0, 2.0))
    found = [r["crossover_frac"] for r in rows if r["crossover_frac"]]

    assert len(found) >= 2, "expected a crossover at more than one shape"
    assert found == sorted(found, reverse=True)


def test_a_harder_shape_thins_the_touch():
    rows = crossover_by_shape(PRICE, VOLUME, depth_frac=0.001, levels=10,
                              shapes=(0.0, 1.0))
    flat, tilt = rows

    assert tilt["touch_shares"] < flat["touch_shares"]


# ---------- depth through time ----------
#
# Section 1c's finding rests on comparing two averages: the multiplier over
# every bar in the sample against the multiplier on the bars the strategy
# traded. That comparison only means anything if the first one is close to 1 -
# otherwise the model has quietly made the whole sample thinner and the
# "timing" result is just a lower depth_frac in disguise. So that is what gets
# pinned here, on a synthetic series where the answer is known in advance.

def multiplier_frame(closes):
    return pd.DataFrame({"AAA": closes},
                        index=pd.bdate_range("2015-01-02", periods=len(closes)))


def wobble(bars: int, sigma: float, start: int = 0) -> list[float]:
    return [100.0 * (1 + sigma * (1 if (i + start) % 2 else -1))
            for i in range(bars)]


def test_a_stationary_series_averages_a_multiplier_of_one():
    """The claim the conditional-cost result depends on. Constant volatility
    means current vol equals its own median on every bar, so the book is
    neither deeper nor thinner than the constant-depth one anywhere."""
    series = depth_multipliers(multiplier_frame(wobble(300, 0.01)),
                               symbol="AAA", vol_elasticity=1.0, vol_window=20)
    assert series.mean() == pytest.approx(1.0, abs=0.02)


def test_zero_elasticity_leaves_every_bar_alone():
    series = depth_multipliers(multiplier_frame(wobble(120, 0.01)),
                               symbol="AAA", vol_elasticity=0.0, vol_window=20)
    assert (series == 1.0).all()


def test_the_warmup_bars_report_no_adjustment():
    """Two windows of returns before there is a median to compare against."""
    series = depth_multipliers(multiplier_frame(wobble(120, 0.01)),
                               symbol="AAA", vol_elasticity=1.0, vol_window=20)
    assert (series[:40] == 1.0).all()


def test_a_volatile_stretch_is_scored_thinner_than_a_calm_one():
    """The loud stretch is deliberately the minority of the sample. The
    reference is an EXPANDING MEDIAN, so a regime that takes up more than half
    the history becomes the typical one and gets scored at 1 - correct
    behaviour, and the reason a fixture has to say which regime is the
    exception.
    """
    closes = wobble(200, 0.002) + wobble(40, 0.02, start=200)
    series = depth_multipliers(multiplier_frame(closes), symbol="AAA",
                               vol_elasticity=1.0, vol_window=20)
    assert series[-1] < 1.0
    assert series[180] == pytest.approx(1.0, abs=0.05)


# ---------- the share weighting ----------

def test_the_weighted_mean_is_by_shares_not_by_slice():
    """A hundred-thousand-share slice should not count the same as a hundred-
    share one, because the quantity being averaged is a cost per share."""
    assert _weighted([1.0, 3.0], [100_000, 100]) == pytest.approx(
        (1.0 * 100_000 + 3.0 * 100) / 100_100)


def test_slices_the_estimator_could_not_score_are_left_out():
    """Warmup bars report NaN rather than 1.0 for the percentile, and averaging
    them in as zeroes would drag every conditional number toward the middle."""
    assert _weighted([float("nan"), 2.0], [999_999, 1]) == 2.0


def test_nothing_to_average_is_not_an_error():
    assert np.isnan(_weighted([], []))
    assert np.isnan(_weighted([float("nan")], [10]))
