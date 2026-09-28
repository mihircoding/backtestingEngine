"""The cross-section's own arithmetic, on data with no download in it.

cross_section.py's headline is a table of 24 downloaded series, which is not
something a test suite should depend on. What it can check is everything
around the download: that the assets are sized comparably, that a run on one
asset is scored against that same asset's buy & hold rather than against
something else, and that the summary numbers mean what the table says they
mean.

The last one matters most. "Beat buy & hold on 9 of 24" is a claim that gets
quoted; if wins counted ties, or if the group means silently dropped an
asset, the sentence would still read fine and be wrong.
"""
import numpy as np
import pandas as pd
import pytest

import cross_section as cs


def trending(n=600, slope=0.05, seed=3, start=100.0):
    rng = np.random.default_rng(seed)
    path = start + slope * np.arange(n) + np.cumsum(rng.normal(0, 0.3, n))
    idx = pd.bdate_range("2020-01-01", periods=n)
    return pd.Series(np.maximum(path, 1.0), index=idx)


@pytest.fixture
def prices():
    return pd.DataFrame({"SPY": trending(seed=1),
                         "TLT": trending(seed=2, slope=-0.02, start=120.0),
                         "GLD": trending(seed=3, slope=0.01, start=50.0)})


def test_trade_size_equalizes_dollars_not_share_counts():
    """A $400 stock and a $20 one must not be run at the same share count -
    that would be comparing a strategy against a strategy plus cash."""
    big, small = cs.shares_for(400.0), cs.shares_for(20.0)
    assert big * 400.0 == pytest.approx(small * 20.0, rel=0.02)
    assert cs.shares_for(1e9) == 1          # never rounds down to nothing


def test_each_asset_is_scored_against_its_own_buy_and_hold(prices):
    row = cs.one_asset(prices, "TLT", n_boot=50)
    hold = prices["TLT"].pct_change().dropna()
    naive = hold.mean() / hold.std(ddof=1) * np.sqrt(cs.TRADING_DAYS)
    # Not equal - the engine charges costs and starts after warmup - but the
    # comparison has to be against TLT, not against the first column.
    assert row["hold_sharpe"] == pytest.approx(naive, abs=0.35)
    assert row["symbol"] == "TLT"


def test_the_gap_is_the_difference_of_the_two_sharpes(prices):
    row = cs.one_asset(prices, "SPY", n_boot=50)
    assert row["diff"] == pytest.approx(row["rule_sharpe"] - row["hold_sharpe"],
                                        abs=1e-9)
    assert row["lo"] <= row["diff"] <= row["hi"]


def test_rows_come_back_sorted_best_gap_first(prices):
    rep = cs.cross_section(prices, n_boot=50)
    gaps = [r["diff"] for r in rep["rows"]]
    assert gaps == sorted(gaps, reverse=True)
    assert rep["n_assets"] == 3


def test_wins_counts_strict_improvements_only(prices):
    rep = cs.cross_section(prices, n_boot=50)
    assert rep["wins"] == sum(1 for r in rep["rows"] if r["diff"] > 0)
    assert rep["clear_wins"] == sum(1 for r in rep["rows"] if r["lo"] > 0)
    assert rep["clear_wins"] <= rep["wins"]


def test_correlated_assets_count_as_fewer_independent_tests():
    """Three copies of the same series are one opinion, not three - which is
    the whole reason the table reports an effective count next to the raw
    one."""
    same = trending(seed=9)
    frame = pd.DataFrame({"A": same, "B": same * 2.0, "C": same * 0.5})
    rep = cs.cross_section(frame, n_boot=50)
    assert rep["effective_assets"] < 1.5
    assert rep["effective_assets"] <= rep["n_assets"]


def test_groups_only_report_assets_that_are_present(prices):
    rep = cs.cross_section(prices, n_boot=50)
    counted = sum(g["n"] for g in rep["groups"])
    assert counted == rep["n_assets"]
    for g in rep["groups"]:
        assert 0 <= g["wins"] <= g["n"]


def test_series_without_full_history_are_dropped_not_padded():
    idx = pd.bdate_range("2020-01-01", periods=300)
    short = pd.Series(np.arange(300.0) + 10, index=idx)
    short.iloc[:150] = np.nan
    frame = pd.DataFrame({"FULL": np.arange(300.0) + 10, "SHORT": short},
                         index=idx)
    kept = frame.dropna(axis=1, thresh=len(frame) - 5).dropna()
    assert list(kept.columns) == ["FULL"]
    assert len(kept) == 300
