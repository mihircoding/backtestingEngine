"""Walk-forward selection lives in run_backtest.py, not src/ - it's an
analysis built on top of the engine rather than part of it, so it gets a
focused suite rather than full coverage.

What's worth pinning down is the honesty of the procedure, not its numbers.
Three things have to hold or the result is meaningless: a fold's training
data has to end before its test year starts, chaining years together must not
invent capital, and the pick for a given year must not move when data from
after that year changes. The last one is the whole point - it is the test
that would fail if the selection ever peeked.
"""

import numpy as np
import pandas as pd
import pytest

from run_backtest import (WF_TRAIN_YEARS, _chain, _walk_forward_windows,
                          walk_forward_selection)

SHORTS = (5, 10)
LONGS = (20, 40)


def _multi_year_prices(years=5, seed=17, start="2016-01-01"):
    """A few years of business-day prices with enough movement to make the
    grid cells actually differ from each other."""
    idx = pd.bdate_range(start, periods=260 * years)
    rng = np.random.default_rng(seed)
    t = np.arange(len(idx))
    path = 100 + 0.02 * t + 6 * np.sin(t / 60) + np.cumsum(rng.normal(0, 0.35, len(idx)))
    return pd.DataFrame({"SYN": path}, index=idx)


class TestFolds:
    def test_training_data_ends_before_the_test_year_begins(self):
        prices = _multi_year_prices()
        for test_year, train, through_test in _walk_forward_windows(prices, 2):
            assert train.index[-1].year == test_year - 1
            assert train.index[-1] < pd.Timestamp(f"{test_year}-01-01")
            assert through_test.index[-1].year == test_year

    def test_one_fold_per_year_after_the_training_window(self):
        prices = _multi_year_prices(years=5)
        n_years = len({ts.year for ts in prices.index})
        folds = list(_walk_forward_windows(prices, 2))
        assert len(folds) == n_years - 2

    def test_warmup_prefix_is_the_training_window_itself(self):
        """through_test exists to give the moving average its warmup. It must
        be the train slice plus the test year and nothing else - if it started
        earlier, folds would silently use different amounts of history."""
        prices = _multi_year_prices()
        for test_year, train, through_test in _walk_forward_windows(prices, 3):
            assert through_test.index[0] == train.index[0]
            pd.testing.assert_frame_equal(through_test.loc[train.index], train)


class TestChaining:
    def test_starts_at_one_and_compounds_segment_returns(self):
        idx = pd.bdate_range("2020-01-01", periods=9)
        a = pd.Series([100.0, 110.0, 120.0], index=idx[:3])   # +20%
        b = pd.Series([50.0, 45.0, 40.0], index=idx[3:6])     # -20%
        c = pd.Series([7.0, 7.0, 14.0], index=idx[6:])        # +100%
        chained = _chain([a, b, c])
        assert chained.iloc[0] == pytest.approx(1.0)
        assert chained.iloc[-1] == pytest.approx(1.2 * 0.8 * 2.0)

    def test_segment_capital_level_does_not_leak_into_the_curve(self):
        """Each year's run starts from the engine's own initial cash, so the
        raw segments sit at unrelated levels. Only their returns may survive
        the chaining."""
        idx = pd.bdate_range("2020-01-01", periods=4)
        a = pd.Series([100.0, 110.0], index=idx[:2])
        b = pd.Series([1_000_000.0, 1_100_000.0], index=idx[2:])
        assert _chain([a, b]).iloc[-1] == pytest.approx(1.21)


class TestNoLookahead:
    def test_a_years_pick_ignores_everything_after_that_year(self):
        """The strongest claim this analysis makes. Replace the final year's
        prices with something completely different and every earlier year's
        choice has to come out identical - those choices were made from data
        that ends before the replaced year starts."""
        prices = _multi_year_prices(years=5)
        last_year = max(ts.year for ts in prices.index)

        altered = prices.copy()
        mask = altered.index.year == last_year
        rng = np.random.default_rng(99)
        altered.loc[mask, "SYN"] *= 1 + rng.normal(0, 0.05, mask.sum())

        base = walk_forward_selection(prices, trade_size=10, train_years=2,
                                      short_windows=SHORTS, long_windows=LONGS)
        moved = walk_forward_selection(altered, trade_size=10, train_years=2,
                                       short_windows=SHORTS, long_windows=LONGS)

        def picks_before(result):
            return [(p["year"], p["short"], p["long"]) for p in result["picks"]
                    if p["year"] < last_year]

        assert picks_before(base) == picks_before(moved)
        assert len(picks_before(base)) > 0, "test would be vacuous otherwise"

    def test_out_of_sample_curve_covers_only_test_years(self):
        prices = _multi_year_prices(years=5)
        result = walk_forward_selection(prices, trade_size=10, train_years=2,
                                        short_windows=SHORTS, long_windows=LONGS)
        traded_years = {ts.year for ts in result["walk_forward"].index}
        assert traded_years == {p["year"] for p in result["picks"]}


class TestSelection:
    def test_picks_are_valid_grid_cells_one_per_test_year(self):
        prices = _multi_year_prices(years=5)
        result = walk_forward_selection(prices, trade_size=10, train_years=2,
                                        short_windows=SHORTS, long_windows=LONGS)
        years = [p["year"] for p in result["picks"]]
        assert years == sorted(set(years))
        for p in result["picks"]:
            assert p["short"] in SHORTS and p["long"] in LONGS
            assert p["short"] < p["long"]
        assert result["n_pairs"] == sum(1 for s in SHORTS for l in LONGS if s < l)

    def test_hindsight_best_is_at_least_as_good_as_any_single_pair(self):
        """It is the maximum over the whole sample by construction, so no
        individual pair may beat it. Cheap, but it is the number the write-up
        calls a ceiling, and a ceiling that isn't one would be misleading."""
        from run_backtest import param_grid, stats
        prices = _multi_year_prices(years=4)
        result = walk_forward_selection(prices, trade_size=10, train_years=2,
                                        short_windows=SHORTS, long_windows=LONGS)
        grid = param_grid(prices, trade_size=10,
                          short_windows=SHORTS, long_windows=LONGS)
        assert result["hindsight_best"]["sharpe"] >= max(r["sharpe"] for r in grid) - 1e-9

    def test_default_training_window_is_three_years(self):
        assert WF_TRAIN_YEARS == 3
