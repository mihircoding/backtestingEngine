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
from fill_realism import (_weighted, cost_curves, crossover, crossover_by_decay,
                          crossover_by_shape, depth_multipliers,
                          interpretable_to, memory_closes_the_gap)

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


def test_the_crossover_moves_later_as_the_far_book_empties():
    rows = crossover_by_decay(PRICE, VOLUME, depth_frac=0.001, levels=10,
                              shape=0.5, decays=(None, 8.0, 4.0, 2.0))
    found = [r["crossover_frac"] for r in rows if r["crossover_frac"]]

    assert len(found) >= 2, "expected a crossover at more than one decay"
    assert found == sorted(found)
    assert rows[0]["peak_level"] == 10          # the ramp peaks at the far edge
    assert rows[-1]["peak_level"] < rows[0]["peak_level"]
    assert rows[-1]["far_level_multiple"] < rows[0]["far_level_multiple"]


def test_a_decaying_book_is_cheaper_than_the_ramp_at_every_size():
    ramp = cost_curves(PRICE, VOLUME, depth_frac=0.001, levels=10, shape=0.5)
    hump = cost_curves(PRICE, VOLUME, depth_frac=0.001, levels=10, shape=0.5,
                       decay=3.0)
    for a, b in zip(ramp, hump):
        assert b["book_bps"] <= a["book_bps"] + 1e-9


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


# ---------- 5. what the book remembers ----------

def memory_rows(aum=5e9, sqrt=0.44, **sharpes):
    """Rows the way memory_sweep() lays them out, keyed recovery -> list of
    (persistence, sharpe). None is permanent impact."""
    rows = []
    for recovery, points in sharpes.items():
        rec = None if recovery == "never" else float(recovery.lstrip("r"))
        for persistence, sharpe in points:
            rows.append({"aum": aum, "persistence": persistence,
                         "recovery": rec, "sharpe": sharpe, "sqrt_sharpe": sqrt})
    return rows


def test_the_gap_closes_at_the_first_persistence_at_or_below_the_formula():
    rows = memory_rows(r5=[(0.0, 0.52), (0.33, 0.47), (0.67, 0.42), (1.0, 0.38)])
    assert memory_closes_the_gap(rows) == {"5e+09/5": 0.67}


def test_permanent_rows_that_never_reach_the_formula_report_none():
    rows = memory_rows(never=[(0.0, 0.52), (0.33, 0.51), (1.0, 0.47)])
    assert memory_closes_the_gap(rows) == {"5e+09/never": None}


def test_each_recovery_is_judged_on_its_own():
    rows = memory_rows(never=[(0.0, 0.52), (1.0, 0.47)],
                       r5=[(0.33, 0.47), (0.67, 0.42)],
                       r20=[(0.33, 0.49), (1.0, 0.44)])
    out = memory_closes_the_gap(rows)
    assert out == {"5e+09/never": None, "5e+09/5": 0.67, "5e+09/20": 1.0}


def test_equal_to_the_formula_counts_as_closing_it():
    rows = memory_rows(r20=[(0.33, 0.49), (1.0, 0.44)], sqrt=0.44)
    assert memory_closes_the_gap(rows)["5e+09/20"] == 1.0


# ---------- 6. the square root was in the slice, not the order ----------

from fill_realism import (closest_to_square_root, exponents_by_setting,
                          impact_exponent, metaorder_cost, metaorder_curves)


def meta(frac, **kwargs):
    """A flat book, 1,000 shares a level, 10% participation: an order of
    0.1 days is 100,000 shares and walks 100 levels in one bar. Flat so the
    cost of one slice is arithmetic on the levels it took."""
    defaults = dict(depth_frac=0.001, shape=0.0, levels=200, participation=0.10)
    defaults.update(kwargs)
    return metaorder_cost(PRICE, VOLUME, frac, **defaults)


def test_an_order_inside_one_slice_pays_the_levels_it_walked():
    """2,000 shares take 1,000 at 100.01 and 1,000 at 100.02: 1.5 bps."""
    row = meta(0.002)
    assert row["slices"] == 1
    assert row["filled_frac"] == 1.0
    assert row["book_bps"] == pytest.approx(1.5, abs=1e-6)


def test_a_memoryless_book_charges_a_sliced_order_what_it_charges_one_slice():
    """Three slices into a book that refills completely between them cost
    exactly what one slice does. That is the exponent of zero."""
    one = meta(0.10)
    three = meta(0.30)
    assert three["slices"] == 3
    assert three["book_bps"] == pytest.approx(one["book_bps"], abs=1e-9)


def test_the_formula_as_coded_is_also_flat_once_the_order_is_sliced():
    """execution.py charges sqrt(slice / volume) per slice, and every slice
    is the participation cap, so a 3-day order costs what a 1-bar one does."""
    assert meta(0.30)["sqrt_slice_bps"] == pytest.approx(meta(0.10)["sqrt_slice_bps"])
    assert meta(0.30)["sqrt_order_bps"] == pytest.approx(
        meta(0.10)["sqrt_order_bps"] * np.sqrt(3))


def test_the_formula_and_the_law_agree_only_inside_one_slice():
    row = meta(0.05, slippage_bps=0.0)
    assert row["sqrt_slice_bps"] == pytest.approx(row["sqrt_order_bps"])
    row = meta(0.50, slippage_bps=0.0)
    assert row["sqrt_slice_bps"] < row["sqrt_order_bps"]


def test_permanent_memory_makes_each_slice_dearer_than_the_last():
    """Full persistence, no recovery, flat book: slice k starts one slice's
    displacement higher than slice k-1, plus the half it is charged for its
    own. Cost grows with the number of slices and never stops."""
    one, two, three = (meta(f, persistence=1.0, recovery=None)
                       for f in (0.10, 0.20, 0.30))
    assert one["book_bps"] < two["book_bps"] < three["book_bps"]
    # Worked by hand. One slice walks 100 levels, so its vwap is 50.5 ticks
    # out, which at $100 is 50.5 bps; it is charged half of that again for
    # its own permanent part, 75.75 bps. The second slice starts 50.5 bps
    # higher and pays 126.25. The average of the two is 101, which is 25.25
    # above the single slice.
    assert one["book_bps"] == pytest.approx(75.75, abs=0.5)
    assert two["book_bps"] - one["book_bps"] == pytest.approx(25.25, abs=0.5)


def test_a_book_that_heals_charges_less_than_one_that_does_not():
    permanent = meta(1.0, persistence=1.0, recovery=None)
    healing = meta(1.0, persistence=1.0, recovery=2.0)
    assert healing["book_bps"] < permanent["book_bps"]
    assert healing["book_bps"] > meta(1.0)["book_bps"]


def test_the_order_fills_completely_when_the_book_is_deep_enough():
    row = meta(3.0, persistence=0.5, recovery=5.0)
    assert row["slices"] == 30
    assert row["filled_frac"] == 1.0


def test_impact_exponent_reads_a_power_law_back():
    sizes = [0.1, 0.2, 0.5, 1.0, 3.0]
    assert impact_exponent(sizes, [4 * s ** 0.5 for s in sizes]) == pytest.approx(0.5)
    assert impact_exponent(sizes, [7.0] * 5) == pytest.approx(0.0)
    assert impact_exponent(sizes, [2 * s for s in sizes]) == pytest.approx(1.0)


def test_exponents_split_the_one_bar_orders_from_the_sliced_ones():
    rows = metaorder_curves(PRICE, VOLUME, persistences=(0.0, 1.0),
                            recoveries=(None,), depth_frac=0.001, shape=0.0,
                            levels=400,
                            total_fracs=(0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0))
    out = exponents_by_setting(rows)
    assert [(e["persistence"], e["recovery"]) for e in out] == [(0.0, None), (1.0, None)]
    memoryless, permanent = out
    # a flat book is linear inside a slice: cost is half the levels walked
    assert memoryless["book_bps_single"] == pytest.approx(1.0, abs=0.05)
    assert memoryless["book_bps_sliced"] == pytest.approx(0.0, abs=1e-6)
    assert permanent["book_bps_sliced"] > 0.5
    assert memoryless["sqrt_order_bps_sliced"] == pytest.approx(0.5)
    assert memoryless["sqrt_slice_bps_sliced"] == pytest.approx(0.0, abs=1e-6)
    assert permanent["y_sliced"] > memoryless["y_sliced"]


def test_the_nearest_setting_to_the_law_is_the_one_closest_to_a_half():
    out = [{"persistence": 0.0, "recovery": None, "book_bps_sliced": 0.0},
           {"persistence": 0.33, "recovery": None, "book_bps_sliced": 0.48},
           {"persistence": 1.0, "recovery": 5.0, "book_bps_sliced": 0.55}]
    assert closest_to_square_root(out)["persistence"] == 0.33


# ---------- 7. charge the order, not the slice ----------

from fill_realism import (formula_curves, formula_exponents, formula_metaorder_cost,
                          law_ladder)


def test_the_handler_on_the_slice_is_the_replica_section_6_wrote_beside_it():
    """Section 6 compared the book against an arithmetic copy of the formula.
    The handler itself, driven on the same flat tape, charges the same at
    every size - which is what makes that section's formula row a fact about
    execution.py rather than about the copy."""
    for frac in (0.01, 0.05, 0.10, 0.30, 1.0, 3.0):
        handler = formula_metaorder_cost(PRICE, VOLUME, frac, impact_on="slice")
        replica = metaorder_cost(PRICE, VOLUME, frac, persistence=0.0,
                                 recovery=None)
        assert handler["bps"] == pytest.approx(replica["sqrt_slice_bps"], rel=1e-9)
        assert handler["slices"] == replica["slices"]


def test_the_handler_on_the_order_is_the_law_at_every_size():
    for frac in (0.01, 0.10, 0.30, 1.0, 3.0):
        row = formula_metaorder_cost(PRICE, VOLUME, frac, impact_on="order",
                                     impact_coef=0.5, slippage_bps=0.0,
                                     sigma_bps=110.0)
        assert row["filled_frac"] == 1.0
        assert row["bps"] == pytest.approx(0.5 * 110.0 * np.sqrt(row["shares"] / VOLUME))


def test_the_two_forms_agree_inside_one_bar_once_the_coefficient_matches():
    """With the same Y the forms differ only by what they divide by: the
    slice over the bar, or the order over the day. Inside one bar those are
    the same ratio."""
    slice_ = formula_metaorder_cost(PRICE, VOLUME, 0.05, impact_on="slice",
                                    impact_coef=0.5, slippage_bps=0.0)
    order = formula_metaorder_cost(PRICE, VOLUME, 0.05, impact_on="order",
                                   impact_coef=0.5, slippage_bps=0.0)
    assert slice_["bps"] == pytest.approx(order["bps"], rel=1e-9)


def test_exponents_are_zero_on_the_slice_and_a_half_on_the_order():
    curves = formula_curves(PRICE, VOLUME, slippage_bps=0.0,
                            total_fracs=(0.05, 0.1, 0.2, 0.5, 1.0, 2.0))
    exps = {e["impact_on"]: e for e in formula_exponents(curves)}
    assert exps["slice"]["sliced"] == pytest.approx(0.0, abs=1e-9)
    assert exps["order"]["sliced"] == pytest.approx(0.5, abs=0.01)
    assert exps["order"]["impact_coef"] == 0.5


def test_slippage_flattens_the_orders_exponent_a_little():
    """2 bps that does not scale with size on top of a square root pulls the
    fitted slope below a half. That is why the printout says 0.47."""
    with_slip = formula_curves(PRICE, VOLUME, slippage_bps=2.0)
    without = formula_curves(PRICE, VOLUME, slippage_bps=0.0)
    a = {e["impact_on"]: e for e in formula_exponents(with_slip)}["order"]["sliced"]
    b = {e["impact_on"]: e for e in formula_exponents(without)}["order"]["sliced"]
    assert b == pytest.approx(0.5, abs=0.01)
    assert 0.4 < a < b


def test_the_ladder_slices_identically_under_all_three_handlers():
    """Participation sets the slicing and all three share it, so a row's
    three handlers report the same slice count and the same deployment -
    the only thing that differs between them is what a slice costs."""
    n = 320
    idx = pd.bdate_range("2020-01-01", periods=n)
    rng = np.random.default_rng(4)
    closes = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.01, n))), index=idx)
    prices = closes.to_frame("SPY")
    opens = (closes * (1 + rng.normal(0, 0.001, n))).to_frame("SPY")
    volumes = pd.DataFrame({"SPY": [1e6] * n}, index=idx)
    rows = law_ladder(prices, opens, volumes, aums=(1e7, 1e8),
                      short_window=10, long_window=30)
    assert [r["aum"] for r in rows] == [1e7, 1e8]
    for r in rows:
        assert r["slice"]["slices"] == r["order"]["slices"] == r["book"]["slices"]
        assert r["slice"]["deployed"] == pytest.approx(r["order"]["deployed"])
        assert set(r["order"]) >= {"sharpe", "cost_bps", "slices", "deployed"}
    big = rows[1]
    # a $100M position on a $100M-a-day tape is a day of volume, which the
    # law charges for and the slice form does not
    assert big["order"]["cost_bps"] > big["slice"]["cost_bps"]


# ---------- 8. send what you can get done ----------

from fill_realism import fill_days_sweep, invested, sized_ladder, time_in_market


def _tape(n=320, seed=4, volume=1e6):
    idx = pd.bdate_range("2020-01-01", periods=n)
    rng = np.random.default_rng(seed)
    closes = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.01, n))), index=idx)
    prices = closes.to_frame("SPY")
    opens = (closes * (1 + rng.normal(0, 0.001, n))).to_frame("SPY")
    volumes = pd.DataFrame({"SPY": [volume] * n}, index=idx)
    return prices, opens, volumes


def test_a_capped_position_completes_and_the_uncapped_one_is_the_old_row():
    prices, opens, volumes = _tape()
    # $1e8 at ~$100 is a million shares on a million-share tape: ten days at
    # 10%, and the budget of two days holds a fifth of it
    rows = sized_ladder(prices, opens, volumes, aums=(1e8,), fill_days=2,
                        short_window=10, long_window=30)
    row = rows[0]
    assert row["position_share"] == pytest.approx(0.2, abs=0.01)
    for name in ("slice", "order", "book"):
        assert row[name]["max_delay_bars"] <= 3
        assert row[name]["sizings_capped"] > 0
    free = sized_ladder(prices, opens, volumes, aums=(1e8,), fill_days=None,
                        short_window=10, long_window=30)[0]
    assert free["position_share"] == 1.0
    assert free["book"]["sizings_capped"] == 0
    assert free["book"]["max_delay_bars"] > 3


def test_more_patience_holds_more_of_the_book():
    prices, opens, volumes = _tape()
    sweep = fill_days_sweep(prices, opens, volumes, aum=1e8, days=(1, 5, None),
                            short_window=10, long_window=30)
    shares = [r["position_share"] for r in sweep]
    held = [r["book"]["invested"] for r in sweep]
    assert shares == sorted(shares) and held == sorted(held)
    assert shares[-1] == 1.0


def test_invested_is_the_time_average_of_exposure_not_the_last_order():
    prices, opens, volumes = _tape()
    base = time_in_market(prices, opens, short_window=10, long_window=30)
    assert 0 < base <= 1.2
    # a tiny book fills instantly, so it is in the market exactly as much as
    # the unconstrained rule
    row = sized_ladder(prices, opens, volumes, aums=(1e4,), fill_days=None,
                       short_window=10, long_window=30)[0]
    assert row["book"]["invested"] == pytest.approx(base, rel=0.05)
    assert row["book"]["deployed"] == 1.0


# ---------- 9. the budget on the way out, and the rule that flips ----------

from fill_realism import long_short_ladder, trimmed_ladder


def _drying_tape(n=320, seed=4, loud=1e6, quiet=2e5, dries_at=160):
    prices, opens, _ = _tape(n=n, seed=seed)
    volumes = pd.DataFrame({"SPY": [loud] * dries_at + [quiet] * (n - dries_at)},
                           index=prices.index)
    return prices, opens, volumes


def test_the_trimmed_ladder_sells_down_when_the_tape_dries_up_and_the_entry_only_one_does_not():
    prices, opens, volumes = _drying_tape()
    kw = dict(aums=(1e8,), fill_days=2, short_window=10, long_window=30)
    entry_only = sized_ladder(prices, opens, volumes, **kw)[0]
    trimmed = trimmed_ladder(prices, opens, volumes, **kw)[0]
    assert entry_only["book"]["trims"] == 0
    assert trimmed["book"]["trims"] > 0
    # the trimmed book holds less once the volume dries up
    assert trimmed["book"]["invested"] < entry_only["book"]["invested"]


def test_on_a_steady_tape_the_trim_never_fires_and_the_rows_agree():
    prices, opens, volumes = _tape()
    kw = dict(aums=(1e8,), fill_days=2, short_window=10, long_window=30)
    entry_only = sized_ladder(prices, opens, volumes, **kw)[0]
    trimmed = trimmed_ladder(prices, opens, volumes, **kw)[0]
    assert trimmed["book"]["trims"] == 0
    assert trimmed["book"]["sharpe"] == pytest.approx(entry_only["book"]["sharpe"])
    assert trimmed["book"]["fills"] == entry_only["book"]["fills"]


def test_the_flip_takes_twice_as_long_as_the_position_under_the_same_budget():
    prices, opens, volumes = _tape()
    kw = dict(aums=(1e8,), fill_days=2, short_window=10, long_window=30)
    long_only = sized_ladder(prices, opens, volumes, **kw)[0]
    both = long_short_ladder(prices, opens, volumes, **kw)[0]
    # the budget caps the position at two days of participation; a reversal
    # is two positions' worth and takes about four
    assert long_only["book"]["max_delay_bars"] <= 3
    assert 4 <= both["book"]["max_delay_bars"] <= 6
    assert both["position_share"] == pytest.approx(long_only["position_share"])


def test_the_long_short_rows_charge_borrow_and_the_free_row_is_unconstrained():
    prices, opens, volumes = _tape()
    kw = dict(aums=(1e8,), short_window=10, long_window=30)
    free = long_short_ladder(prices, opens, volumes, fill_days=None, **kw)[0]
    capped = long_short_ladder(prices, opens, volumes, fill_days=2, **kw)[0]
    assert free["position_share"] == 1.0 and capped["position_share"] < 1.0
    assert free["book"]["borrow_paid_bps"] > 0 and capped["book"]["borrow_paid_bps"] > 0
    none = long_short_ladder(prices, opens, volumes, fill_days=2, borrow_bps=0.0, **kw)[0]
    assert none["book"]["borrow_paid_bps"] == 0.0


# ---------- 10. the one free parameter, and the result that was noise ----------

from fill_realism import participation_sweep, trim_bootstrap


def test_participation_buys_position_and_pays_for_it_but_does_not_buy_time():
    prices, opens, volumes = _drying_tape()
    rows = participation_sweep(prices, opens, volumes, aum=1e8, rates=(0.05, 0.10, 0.20),
                               fill_days=2, short_window=10, long_window=30)
    shares = [r["position_share"] for r in rows]
    assert shares == sorted(shares) and shares[0] < shares[-1]
    # the budget and the fill scale together, so the worst exit is the same
    # number of bars whatever the rate
    assert len({r["book"]["max_delay_bars"] for r in rows}) == 1
    # and every handler charges more for a bigger share of the bar
    for handler in ("slice", "order", "book"):
        costs = [r[handler]["cost_bps"] for r in rows]
        assert costs == sorted(costs) and costs[0] < costs[-1]


def test_the_sweep_rows_are_the_trimmed_ladder_at_that_rate():
    prices, opens, volumes = _drying_tape()
    kw = dict(fill_days=2, short_window=10, long_window=30)
    row = participation_sweep(prices, opens, volumes, aum=1e8, rates=(0.10,), **kw)[0]
    ladder = trimmed_ladder(prices, opens, volumes, aums=(1e8,), participation=0.10, **kw)[0]
    assert row["book"]["sharpe"] == pytest.approx(ladder["book"]["sharpe"])
    assert row["book"]["trims"] == ladder["book"]["trims"]
    assert row["participation"] == 0.10


def test_the_bootstrap_reads_zero_when_the_trim_never_fires():
    prices, opens, volumes = _tape()
    row = trim_bootstrap(prices, opens, volumes, aums=(1e8,), fill_days=2, n_boot=200,
                         short_window=10, long_window=30)[0]
    assert row["days_that_differ"] == 0
    assert row["diff"] == 0.0 and row["lo"] == 0.0 and row["hi"] == 0.0
    assert row["sharpe_trimmed"] == pytest.approx(row["sharpe_entry_only"])


def test_the_bootstrap_interval_brackets_the_difference_when_the_trim_does_fire():
    prices, opens, volumes = _drying_tape()
    row = trim_bootstrap(prices, opens, volumes, aums=(1e8,), fill_days=2, n_boot=300,
                         short_window=10, long_window=30)[0]
    assert row["days_that_differ"] > 0
    assert row["diff"] == pytest.approx(row["sharpe_trimmed"] - row["sharpe_entry_only"])
    assert row["lo"] <= row["diff"] <= row["hi"]
    assert 0.0 <= row["p_le_zero"] <= 1.0


# ---------- 11. the capacity surface ----------

from fill_realism import _same_tape, _sections, capacity_surface, surface_knees


def _surface(**kw):
    prices, opens, volumes = _tape()
    # $1e6 at ~$100 is ten thousand shares, which a two-day budget at any of
    # these rates holds whole; $1e8 and $1e9 are a million and ten million,
    # which it does not
    return capacity_surface(prices, opens, volumes, aums=(1e6, 1e8, 1e9),
                            rates=(0.05, 0.10, 0.20), fill_days=2,
                            short_window=10, long_window=30, **kw)


def test_the_surface_has_a_cell_for_every_aum_at_every_rate():
    rows = _surface()
    assert len(rows) == 9
    assert {(r["aum"], r["participation"]) for r in rows} == {
        (a, p) for a in (1e6, 1e8, 1e9) for p in (0.05, 0.10, 0.20)}
    for r in rows:
        assert {"sharpe", "cost_bps", "max_delay_bars", "position_share", "trims"} <= set(r)
    # more AUM at a rate holds less of it; more rate at an AUM holds more
    for p in (0.05, 0.10, 0.20):
        col = sorted((r for r in rows if r["participation"] == p), key=lambda r: r["aum"])
        assert [r["position_share"] for r in col] == sorted((r["position_share"] for r in col), reverse=True)
    for a in (1e6, 1e8, 1e9):
        row = sorted((r for r in rows if r["aum"] == a), key=lambda r: r["participation"])
        assert [r["position_share"] for r in row] == sorted(r["position_share"] for r in row)


def test_past_the_knee_every_cell_at_a_rate_is_the_same_run():
    # once the budget binds, the position is the budget whatever the AUM
    # says, so the slices and the exits are identical and only the cash differs
    rows = _surface()
    for p in (0.05, 0.10, 0.20):
        bound = [r for r in rows if r["participation"] == p and r["position_share"] < 1]
        assert len(bound) == 2
        assert bound[0]["max_delay_bars"] == bound[1]["max_delay_bars"]
        assert bound[0]["cost_bps"] == pytest.approx(bound[1]["cost_bps"])
        # the same dollars, made on the same shares; the Sharpe only differs
        # because the cash pile the daily return is divided by does
        assert (bound[0]["final"] - bound[0]["aum"]
                == pytest.approx(bound[1]["final"] - bound[1]["aum"], rel=1e-6))
        assert bound[0]["sharpe"] == pytest.approx(bound[1]["sharpe"], abs=0.05)


def test_the_knee_is_the_first_rung_the_budget_binds_on():
    rows = _surface()
    knees = {k["participation"]: k for k in surface_knees(rows)}
    assert set(knees) == {0.05, 0.10, 0.20}
    for p, k in knees.items():
        col = sorted((r for r in rows if r["participation"] == p), key=lambda r: r["aum"])
        assert k["knee_aum"] == 1e8
        assert k["position_at_knee"] == col[1]["position_share"] < 1
        assert k["sharpe_whole"] == col[0]["sharpe"]
        past = [r["sharpe"] for r in col[1:]]
        assert k["sharpe_past_knee_low"] == min(past) and k["sharpe_past_knee_high"] == max(past)
        assert k["sharpe_spread_past_knee"] == pytest.approx(max(past) - min(past))
    # a tape nothing binds on has no knee and no spread
    prices, opens, volumes = _tape()
    small = capacity_surface(prices, opens, volumes, aums=(1e4, 1e5), rates=(0.10,),
                             fill_days=2, short_window=10, long_window=30)
    flat = surface_knees(small)[0]
    assert flat["knee_aum"] is None and flat["sharpe_spread_past_knee"] == 0.0


def test_a_section_run_refuses_a_different_tape_but_not_a_rerounded_one():
    header = {"symbol": "SPY", "start": "2015-01-01", "end": "2024-12-31", "bars": 2515,
              "median_price": 271.0247, "median_volume": 76610800.0}
    assert _same_tape(dict(header), header)
    # yfinance re-rounds the adjustment between downloads; that is the same tape
    assert _same_tape({**header, "median_price": 271.0247 * (1 + 5e-5)}, header)
    assert not _same_tape({**header, "median_price": 271.0247 * 1.01}, header)
    assert not _same_tape({**header, "bars": 2514}, header)
    assert not _same_tape({**header, "end": "2023-12-31"}, header)
    assert not _same_tape({}, header)


def test_a_section_that_prints_beside_an_earlier_one_says_so_when_it_is_missing(capsys):
    prices, opens, volumes = _tape()
    sections = _sections(prices, opens, volumes, quick=True)
    assert list(sections) == list(range(1, 14))
    # section 9 prints against section 8's rows; with nothing on disk it has to stop
    with pytest.raises(SystemExit, match="section 8"):
        sections[9]({})
    # and section 11 stands on its own
    added = sections[11]({})
    capsys.readouterr()
    assert set(added) == {"surface_aums", "capacity_surface", "surface_knees"}
    # section 12 is arithmetic on section 11, so it needs it and runs nothing
    with pytest.raises(SystemExit, match="section 11"):
        sections[12]({"bars": len(prices)})
    fees = sections[12]({**added, "bars": len(prices)})
    capsys.readouterr()
    assert set(fees) == {"mgmt_fee", "perf_fee", "fee_surface", "fee_alignment", "rate_alignment"}
    assert len(fees["fee_surface"]) == len(added["capacity_surface"])
    # section 13 is arithmetic on both, and every sized row now carries the
    # yearly curve it needs
    assert all("year_ends" in r for r in added["capacity_surface"])
    yearly = sections[13]({**added, **fees, "bars": len(prices)})
    capsys.readouterr()
    assert set(yearly) == {"crystallized_surface", "crystallized_alignment"}
    assert len(yearly["crystallized_surface"]) == len(added["capacity_surface"])


# ---------- 12. put a fee on the surface ----------

from fill_realism import fee_alignment, fee_economics, rate_alignment


def _flat_surface(gross=1.0e9, knee=2e9, rates=(0.02, 0.10), aums=(1e9, 2e9, 5e9, 1e10)):
    """Section 11's shape by hand: gross rises to the knee and is flat past it,
    and the position the budget allows is a fixed number of dollars from the
    knee up, so position_share falls as 1/AUM."""
    rows, knees = [], []
    for rate in rates:
        k = knee * rate / 0.02       # the knee scales with the rate, as on the real tape
        g = gross * rate / 0.02
        for aum in aums:
            share = min(1.0, 0.68 * k / aum)
            pl = g if aum >= k else g * aum / k
            rows.append({"aum": aum, "participation": rate, "final": aum + pl,
                         "sharpe": 0.6 - 2 * rate, "position_share": share})
        bound = [a for a in aums if 0.68 * k / a < 0.999]
        knees.append({"participation": rate, "knee_aum": bound[0] if bound else None})
    return rows, knees


def test_the_fee_is_two_and_twenty_on_each_basis_and_the_lump_has_no_incentive_in_a_loss():
    rows, _ = _flat_surface()
    cells = fee_economics(rows, bars=2520, mgmt=0.02, perf=0.20)
    c = next(c for c in cells if c["aum"] == 5e9 and c["participation"] == 0.02)
    assert c["years"] == pytest.approx(10.0)
    assert c["gross_pl"] == pytest.approx(1.0e9)
    # on the AUM: 2% of $5bn for ten years is $1bn, which is the whole gross
    assert c["on_aum"]["management"] == pytest.approx(1.0e9)
    assert c["on_aum"]["incentive"] == 0.0
    assert c["on_aum"]["investor_net"] == pytest.approx(0.0)
    # on the position the budget allowed: 2% of $1.36bn for ten years
    held = 5e9 * c["position_share"]
    assert c["on_deployed"]["management"] == pytest.approx(0.02 * held * 10)
    assert c["on_deployed"]["incentive"] == pytest.approx(0.2 * (1.0e9 - 0.02 * held * 10))
    assert c["on_deployed"]["investor_net"] + c["on_deployed"]["manager_take"] == pytest.approx(1.0e9)
    # a cell that lost money pays the management fee and no incentive, and
    # reports no share of a gross it does not have
    lost = fee_economics([{**rows[0], "final": rows[0]["aum"] * 0.9}], bars=2520)[0]
    assert lost["on_aum"]["incentive"] == 0.0
    assert lost["on_aum"]["fee_share_of_gross"] is None
    assert lost["on_aum"]["investor_net"] == pytest.approx(-0.1 * rows[0]["aum"] - lost["on_aum"]["management"])


def test_on_the_aum_the_two_sides_part_at_the_knee_and_on_the_position_they_do_not():
    rows, knees = _flat_surface()
    cells = fee_economics(rows, bars=2520)
    al = {a["participation"]: a for a in fee_alignment(cells, knees)}
    a = al[0.02]
    # the manager's take on the AUM only grows: best is the top of the surface
    assert a["manager_best_aum"]["on_aum"] == 1e10
    # the investor's best on the AUM is the knee, where the gross stops growing
    assert a["investor_best_aum"]["on_aum"] == a["knee_aum"] == 2e9
    # past the knee the fee eats the flat gross at gross / (2% x years): $5bn
    assert a["break_even_aum"] == pytest.approx(5e9)
    assert a["break_even_over_knee"] == pytest.approx(2.5)
    # on the position the budget allowed, neither side's number moves past the knee
    assert a["spread_past_knee"]["on_deployed"]["manager_take"] == pytest.approx(0.0, abs=1e-9)
    assert a["spread_past_knee"]["on_deployed"]["investor_net"] == pytest.approx(0.0, abs=1e-9)
    # while on the AUM both still do
    assert a["spread_past_knee"]["on_aum"]["manager_take"] > 0.5
    assert a["spread_past_knee"]["on_aum"]["investor_net"] > 0.5
    # a rate whose knee is off the top of the surface reports no multiple
    assert al[0.10]["knee_aum"] == 1e10 and al[0.10]["break_even_over_knee"] == pytest.approx(2.5)


def test_along_the_rate_axis_the_fee_points_both_sides_at_the_same_rung():
    rows, _ = _flat_surface(rates=(0.02, 0.05, 0.10, 0.20))
    cells = fee_economics(rows, bars=2520)
    for r in rate_alignment(cells):
        assert r["investor_best_rate"] == r["manager_best_rate"]
        # and the Sharpe's own pick is the most patient rate in every row
        assert r["sharpe_best_rate"] == 0.02
        assert r["sharpe_best"] >= r["sharpe_at_investor_best"]
        assert r["investor_net_at_best"] >= r["investor_net_at_sharpe_best"]


# ---------- 13. crystallize the fee on the curve ----------

from fill_realism import (crystallized_alignment, crystallized_fee,
                          crystallized_surface, year_end_equity)


def _curve(gains, aum=1e9, bars=252):
    """A ten-year gross curve from a list of yearly gains, in [date, equity,
    bars] form, each year a full 252 bars."""
    out, eq = [], aum
    for i, g in enumerate(gains):
        eq += g
        out.append([f"{2015 + i}-12-31", eq, bars])
    return out


def test_year_end_equity_keeps_the_last_bar_of_each_year_and_counts_its_bars():
    idx = pd.bdate_range("2020-01-01", "2022-12-31")
    eq = pd.Series(np.arange(len(idx), dtype=float), index=idx)
    ends = year_end_equity(eq)
    assert [e[0] for e in ends] == ["2020-12-31", "2021-12-31", "2022-12-30"]
    assert [e[1] for e in ends] == [float(eq["2020"].iloc[-1]), float(eq["2021"].iloc[-1]),
                                    float(eq["2022"].iloc[-1])]
    assert [e[2] for e in ends] == [len(eq["2020"]), len(eq["2021"]), len(eq["2022"])]
    assert sum(e[2] for e in ends) == len(idx)


def test_the_yearly_fee_is_a_fifth_above_the_mark_and_nothing_under_it():
    # +100m, +100m, -150m, +100m on $1bn, no management fee, so the mark is
    # the only thing in play: paid, paid, nothing, and then nothing again
    # because +100m does not get back to the 1,200m mark
    ends = _curve([100e6, 100e6, -150e6, 100e6])
    f = crystallized_fee(ends, 1e9, 1e9, mgmt=0.0, perf=0.2)
    inc = [y["incentive"] for y in f["schedule"]]
    assert inc[0] == pytest.approx(20e6) and inc[1] == pytest.approx(20e6)
    assert inc[2] == 0.0 and inc[3] == 0.0
    assert f["years_incentive_paid"] == 2 and f["years_ending_under_the_mark"] == 2
    # the mark is the net asset value after the fee, not the gross
    assert f["schedule"][1]["high_water_mark"] == pytest.approx(1e9 + 160e6)
    # and the investor's net is what the final NAV says
    assert f["final_nav"] == pytest.approx(1e9 + f["investor_net"])
    assert f["management"] == 0.0
    assert f["mark_shortfall"] == pytest.approx(1160e6 - f["final_nav"])


def test_the_management_fee_is_prorated_by_bars_so_it_matches_the_lump_to_the_dollar():
    ends = _curve([10e6] * 10, bars=252)
    ends[3][2] = 250
    ends[7][2] = 254
    f = crystallized_fee(ends, 1e9, 1e9, mgmt=0.02, perf=0.0)
    assert f["management"] == pytest.approx(0.02 * 1e9 * sum(e[2] for e in ends) / 252)
    # charged on the position rather than the AUM, it is that much smaller
    g = crystallized_fee(ends, 1e9, 0.4e9, mgmt=0.02, perf=0.0)
    assert g["management"] == pytest.approx(0.4 * f["management"])


def test_the_yearly_take_over_the_lump_is_a_fifth_of_how_far_under_its_mark_the_fund_finishes():
    # a good decade with a bad year in it: the lump pays a fifth of the net
    # gain once; the yearly fee paid for the 2020 high and never gave it back
    gains = [50e6, 80e6, 120e6, -60e6, 90e6, 300e6, 400e6, -500e6, 100e6, 150e6]
    rows = [{"aum": 1e9, "participation": 0.1, "final": 1e9 + sum(gains),
             "position_share": 1.0, "year_ends": _curve(gains)}]
    lump = [{"aum": 1e9, "participation": 0.1,
             "on_aum": {"manager_take": 0.02 * 1e9 * 10 + 0.2 * (sum(gains) - 0.02 * 1e9 * 10),
                        "incentive": 0.2 * (sum(gains) - 0.02 * 1e9 * 10)},
             "on_deployed": {"manager_take": 0.02 * 1e9 * 10 + 0.2 * (sum(gains) - 0.02 * 1e9 * 10),
                             "incentive": 0.2 * (sum(gains) - 0.02 * 1e9 * 10)}}]
    cell = crystallized_surface(rows, lump, mgmt=0.02, perf=0.2)[0]["on_aum"]
    assert cell["mark_shortfall"] > 0
    assert cell["over_the_lump"] == pytest.approx(0.2 * cell["mark_shortfall"])
    assert cell["over_the_lump"] == pytest.approx(cell["incentive"] - cell["lump_incentive"])
    # a decade that finishes at its mark has paid for every dollar exactly once
    steady = [100e6] * 10
    rows2 = [{"aum": 1e9, "participation": 0.1, "final": 2e9, "position_share": 1.0,
              "year_ends": _curve(steady)}]
    lump2 = [{"aum": 1e9, "participation": 0.1,
              "on_aum": {"manager_take": 0.2e9 + 0.2 * 0.8e9, "incentive": 0.2 * 0.8e9},
              "on_deployed": {"manager_take": 0.2e9 + 0.2 * 0.8e9, "incentive": 0.2 * 0.8e9}}]
    cell2 = crystallized_surface(rows2, lump2, mgmt=0.02, perf=0.2)[0]["on_aum"]
    assert cell2["mark_shortfall"] == pytest.approx(0.0, abs=1.0)
    assert cell2["over_the_lump"] == pytest.approx(0.0, abs=1.0)
    assert cell2["years_incentive_paid"] == 10


def test_crystallized_alignment_keeps_the_knee_and_reports_the_extra_past_it():
    gains = [50e6, 80e6, 120e6, -60e6, 90e6, 300e6, 400e6, -500e6, 100e6, 150e6]
    rows, knees = [], [{"participation": 0.1, "knee_aum": 2e9}]
    for aum in (1e9, 2e9, 5e9):
        # below the knee the position is the AUM and the gross scales with it;
        # from the knee up the position is the budget and the gross is flat
        scaled = [g * min(1.0, aum / 2e9) for g in gains]
        rows.append({"aum": aum, "participation": 0.1, "final": aum + sum(scaled),
                     "position_share": min(1.0, 2e9 / aum), "year_ends": _curve(scaled, aum=aum)})
    lump = []
    for r in rows:
        g = r["final"] - r["aum"]
        lump.append({"aum": r["aum"], "participation": 0.1,
                     **{b: {"manager_take": 0.2 * cap + 0.2 * max(g - 0.2 * cap, 0.0),
                            "incentive": 0.2 * max(g - 0.2 * cap, 0.0)}
                        for b, cap in (("on_aum", r["aum"]), ("on_deployed", r["aum"] * r["position_share"]))}})
    cells = crystallized_surface(rows, lump)
    a = crystallized_alignment(cells, knees)[0]
    assert a["knee_aum"] == 2e9
    # the gross is flat, the management fee grows: the investor's best is the
    # knee and the manager's is the top, as on the lump
    assert a["investor_best_aum"]["on_aum"] == 2e9 and a["manager_best_aum"]["on_aum"] == 5e9
    extra = {c["aum"]: c["on_aum"]["over_the_lump"] for c in cells}
    assert a["extra_take_past_knee"]["low"] == pytest.approx(min(extra[2e9], extra[5e9]))
    assert a["extra_take_past_knee"]["high"] == pytest.approx(max(extra[2e9], extra[5e9]))
    assert a["years_paid_at_top"] == cells[-1]["on_aum"]["years_incentive_paid"]


def test_section_13_needs_the_yearly_curve_on_section_11(monkeypatch):
    prices, opens, volumes = _tape()
    sections = _sections(prices, opens, volumes, quick=True)
    assert 13 in sections
    stale = {"capacity_surface": [{"aum": 1e9, "participation": 0.1, "final": 1.1e9,
                                   "position_share": 1.0, "sharpe": 0.5}],
             "fee_surface": [], "surface_knees": [], "bars": 2520}
    with pytest.raises(SystemExit, match="section 11"):
        sections[13](stale)
