"""Tests for the fill-model comparison.

The study's three claims are each a function, and each is checkable without
touching the network: the cost curves are arithmetic on a known book, the
crossover is an interpolation between two of those rows, and the deployed-
capital cutoff is the thing that stops the capacity ladder being read wrong.

Deliberately not tested here: the numbers in notes/backtester.md section 12.
Those depend on ten years of downloaded prices and belong in the write-up with
a date on them, not in an assertion that breaks when Yahoo revises a split.
"""

import pytest

from exchange.order import TICK
from fill_realism import cost_curves, crossover, interpretable_to

PRICE = 100.0
VOLUME = 1_000_000.0


def curves(**kwargs):
    """depth_frac 0.001 of a million shares is 1,000 shares a level."""
    return cost_curves(PRICE, VOLUME, depth_frac=0.001, levels=10, **kwargs)


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
    thin = cost_curves(PRICE, VOLUME, depth_frac=0.0005, levels=50,
                       size_fracs=(0.005,))[0]
    thick = cost_curves(PRICE, VOLUME, depth_frac=0.005, levels=50,
                        size_fracs=(0.005,))[0]
    assert thick["book_bps"] < thin["book_bps"]


def test_the_square_root_law_is_concave_in_size():
    """Not a property of this file - a property of the law it is reproducing -
    but if the comparison curve were linear the whole chart would be wrong."""
    rows = cost_curves(PRICE, VOLUME, size_fracs=(0.01, 0.04, 0.16))
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
    rows = cost_curves(PRICE, VOLUME, depth_frac=0.5, levels=200,
                       size_fracs=(0.0001, 0.001, 0.01))
    assert crossover(rows) is None


def test_a_thinner_book_crosses_sooner():
    sizes = (0.0001, 0.0005, 0.001, 0.005, 0.01, 0.05)
    thin = crossover(cost_curves(PRICE, VOLUME, depth_frac=0.0002,
                                 levels=500, size_fracs=sizes))
    thick = crossover(cost_curves(PRICE, VOLUME, depth_frac=0.002,
                                  levels=500, size_fracs=sizes))
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
