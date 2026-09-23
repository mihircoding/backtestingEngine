"""Checks on the luck-vs-edge maths in significance.py.

Several of these are the properties the formulas are supposed to have rather
than hand-computed numbers: the expected maximum of n normals is checked
against a simulation of the maximum of n normals, and the bootstrap is
checked against the thing it exists to fix (autocorrelation making the naive
error bar too narrow).
"""
import numpy as np
import pytest
from scipy import stats as sps

import significance as sig


def ar1(n, rho=0.9, mean=0.0, sd=0.01, seed=0):
    """A return series with runs in it, like a trend follower's."""
    rng = np.random.default_rng(seed)
    shocks = rng.normal(0, sd, n)
    out = np.empty(n)
    out[0] = shocks[0]
    for t in range(1, n):
        out[t] = rho * out[t - 1] + shocks[t]
    return out + mean


def test_sharpe_matches_the_definition():
    r = np.array([0.01, -0.005, 0.02, 0.0, 0.007])
    expected = r.mean() / r.std(ddof=1) * np.sqrt(252)
    assert sig.sharpe(r) == pytest.approx(expected)
    assert sig.sharpe(np.zeros(10)) == 0.0          # flat equity, not a divide by zero


def test_bootstrap_only_reuses_observations_it_was_given():
    r = np.arange(50) / 1000.0
    draws = sig.stationary_bootstrap(r, n_boot=25, block=10, seed=3)
    assert draws.shape == (25, 50)
    assert set(np.unique(draws)).issubset(set(r))
    assert np.array_equal(draws, sig.stationary_bootstrap(r, n_boot=25, block=10, seed=3))


def test_a_long_block_keeps_runs_together():
    """block=1 restarts every step (iid resampling); a long block does not."""
    r = ar1(400, rho=0.95)
    iid = sig.stationary_bootstrap(r, n_boot=200, block=1.0, seed=1)
    blocked = sig.stationary_bootstrap(r, n_boot=200, block=40.0, seed=1)

    def lag1(x):
        return np.mean([np.corrcoef(row[:-1], row[1:])[0, 1] for row in x])

    assert abs(lag1(iid)) < 0.1                      # shuffled: no memory left
    assert lag1(blocked) > 0.7                       # runs survived


def test_ignoring_runs_makes_the_error_bar_too_narrow():
    """The reason the block bootstrap is here at all."""
    r = ar1(1000, rho=0.9, mean=0.0004)
    narrow = sig.sharpe_interval(r, n_boot=800, block=1.0, seed=2)
    honest = sig.sharpe_interval(r, n_boot=800, block=30.0, seed=2)
    assert honest["hi"] - honest["lo"] > 1.5 * (narrow["hi"] - narrow["lo"])


def test_interval_brackets_the_sample_and_reads_the_sign():
    rng = np.random.default_rng(5)
    good = rng.normal(0.0008, 0.01, 1500)            # Sharpe ~1.3
    res = sig.sharpe_interval(good, n_boot=800, seed=4)
    assert res["lo"] < res["sharpe"] < res["hi"]
    assert res["p_le_zero"] < 0.05

    noise = rng.normal(0.0, 0.01, 1500)
    noise -= noise.mean()                            # exactly no edge, by construction
    assert 0.4 < sig.sharpe_interval(noise, n_boot=800, seed=4)["p_le_zero"] < 0.6


def test_paired_difference_uses_the_same_dates_for_both():
    rng = np.random.default_rng(7)
    market = rng.normal(0.0004, 0.01, 800)
    same = sig.paired_difference(market, market, n_boot=400, seed=1)
    assert same["diff"] == pytest.approx(0.0)
    assert same["lo"] == pytest.approx(0.0) and same["hi"] == pytest.approx(0.0)

    with pytest.raises(ValueError):
        sig.paired_difference(market, market[:-1])


def test_paired_difference_finds_a_real_gap():
    rng = np.random.default_rng(11)
    market = rng.normal(0.0002, 0.01, 1500)
    better = market + rng.normal(0.0006, 0.002, 1500)
    res = sig.paired_difference(better, market, n_boot=800, seed=2)
    assert res["diff"] > 0 and res["p_le_zero"] < 0.05


def test_expected_max_sharpe_against_a_simulation():
    """Best of n worthless strategies, formula vs brute force."""
    rng = np.random.default_rng(13)
    for n in (5, 23, 100):
        sd = 0.4
        simulated = rng.normal(0, sd, size=(20000, n)).max(axis=1).mean()
        assert sig.expected_max_sharpe(n, sd) == pytest.approx(simulated, abs=0.03)


def test_one_trial_has_no_selection_bias():
    assert sig.expected_max_sharpe(1, 0.5) == 0.0
    assert sig.expected_max_sharpe(50, 0.0) == 0.0
    assert sig.expected_max_sharpe(50, 0.4) > sig.expected_max_sharpe(10, 0.4)
    assert sig.expected_max_sharpe(23, 0.8) == pytest.approx(
        2 * sig.expected_max_sharpe(23, 0.4))


def test_probabilistic_sharpe_matches_the_normal_case():
    rng = np.random.default_rng(17)
    r = rng.normal(0.0005, 0.01, 2000)
    r = (r - r.mean()) / r.std(ddof=1) * 0.01 + 0.0005     # exactly normal-ish moments
    sr = sig.sharpe(r) / np.sqrt(252)
    assert sig.probabilistic_sharpe(r) == pytest.approx(
        sps.norm.cdf(sr * np.sqrt(len(r) - 1)), abs=0.02)


def test_a_higher_bar_is_harder_to_clear():
    rng = np.random.default_rng(19)
    r = rng.normal(0.0006, 0.01, 1500)
    assert sig.probabilistic_sharpe(r, 0.0) > sig.probabilistic_sharpe(r, 0.5)
    assert sig.probabilistic_sharpe(r, sig.sharpe(r)) == pytest.approx(0.5, abs=0.02)


def test_deflating_never_flatters_and_needs_a_choice_to_bite():
    rng = np.random.default_rng(23)
    r = rng.normal(0.0006, 0.01, 1500)
    d = sig.deflated_sharpe(r, n_trials=23, sharpe_sd=0.35)
    assert d["dsr"] < d["psr_vs_zero"]
    assert d["threshold"] > 0

    alone = sig.deflated_sharpe(r, n_trials=1, sharpe_sd=0.35)
    assert alone["dsr"] == pytest.approx(alone["psr_vs_zero"])


def test_the_best_of_23_coin_flips_does_not_look_significant():
    """End to end: 23 strategies with no edge, keep the winner, deflate it."""
    rng = np.random.default_rng(29)
    trials = rng.normal(0.0, 0.01, size=(23, 2500))
    sharpes = np.array([sig.sharpe(t) for t in trials])
    best = trials[int(np.argmax(sharpes))]

    naive = sig.probabilistic_sharpe(best, 0.0)
    d = sig.deflated_sharpe(best, n_trials=23, sharpe_sd=float(sharpes.std(ddof=1)))
    assert naive > 0.8                    # on its own the winner looks decent
    assert d["dsr"] < 0.5                 # against 23 tries it is nothing


def test_effective_trials_counts_independent_strategies():
    rng = np.random.default_rng(31)
    independent = rng.normal(0, 0.01, size=(10, 3000))
    assert sig.effective_trials(independent) == pytest.approx(10, rel=0.05)

    one_idea = np.repeat(independent[:1], 10, axis=0)
    assert sig.effective_trials(one_idea) == pytest.approx(1.0, abs=0.01)

    # two ideas, five near-copies of each - should read as about two
    market = rng.normal(0, 0.01, size=(2, 3000))
    copies = np.vstack([m + rng.normal(0, 0.0005, size=(5, 3000)) for m in market])
    assert 1.8 < sig.effective_trials(copies) < 2.6

    assert sig.effective_trials(independent[:1]) == 1.0


def test_a_long_only_rule_is_measured_against_the_index_not_zero():
    rng = np.random.default_rng(37)
    index = rng.normal(0.0004, 0.01, 2500)
    index += 0.0004 - index.mean()                  # a market with a real Sharpe
    rule = index * 0.9 + rng.normal(0.00005, 0.002, 2500)   # mostly just long it

    vs_zero = sig.deflated_sharpe(rule, n_trials=23, sharpe_sd=0.1)
    vs_index = sig.deflated_sharpe(rule, n_trials=23, sharpe_sd=0.1,
                                   benchmark=sig.sharpe(index))
    assert vs_index["bar"] > vs_zero["bar"]
    assert vs_index["bar"] == pytest.approx(sig.sharpe(index) + vs_index["threshold"])
    assert vs_index["dsr"] < vs_zero["dsr"]
    assert vs_zero["psr_vs_zero"] == vs_index["psr_vs_zero"]
