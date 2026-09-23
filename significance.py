"""How much of a backtest's Sharpe is luck?

Two separate questions, and the grid in RESULTS.md needs both.

1. One strategy, one sample. A Sharpe of 0.55 over ten years is an estimate
   with an error bar on it, and the usual textbook error bar assumes daily
   returns are independent, which returns from a trend-following rule are
   not - they come in runs, because the position is the same for weeks at a
   time. The stationary bootstrap (Politis & Romano, 1994) resamples blocks
   of random length instead of single days, so runs survive the resampling
   and the error bar stops being too narrow.

2. Twenty-three strategies, one sample. Score 23 parameter pairs and the
   best one is biased upward even if none of them has any edge, because you
   picked the maximum of 23 noisy numbers. The deflated Sharpe ratio (Bailey
   & Lopez de Prado, 2014) asks the honest version: given that many trials,
   how high would the best Sharpe have got by chance alone, and is the one
   we actually found above that?

Nothing here is specific to this engine - it takes return series and numbers
of trials. run_backtest.py --significance wires it to the SPY grid.
"""

import numpy as np
from scipy import stats as sps

TRADING_DAYS = 252
EULER_MASCHERONI = 0.5772156649015329


def sharpe(returns: np.ndarray, periods: int = TRADING_DAYS) -> float:
    """Annualized Sharpe of a return series, risk-free 0."""
    r = np.asarray(returns, dtype=float)
    sd = r.std(ddof=1)
    if sd == 0:
        return 0.0
    return float(r.mean() / sd * np.sqrt(periods))


def stationary_bootstrap(returns: np.ndarray, n_boot: int = 2000,
                         block: float = 20.0, seed: int = 0) -> np.ndarray:
    """`n_boot` resamples of `returns`, each the same length as the original.

    Walk forward through the series copying consecutive observations, and at
    each step restart at a fresh random point with probability 1/block. Block
    lengths are therefore geometric with mean `block`, and the series wraps
    at the end so every observation has the same chance of being picked -
    that stationarity is the whole point of this variant.

    block=20 is about a month of trading days, comfortably longer than a
    typical position in a 50/200 crossover is autocorrelated, and the result
    is not sensitive to it: 10 and 40 move the interval below by a couple of
    hundredths.
    """
    r = np.asarray(returns, dtype=float)
    n = len(r)
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, n, size=(n_boot, n))
    restart = rng.random((n_boot, n)) < 1.0 / block
    restart[:, 0] = True

    idx = np.empty((n_boot, n), dtype=np.int64)
    idx[:, 0] = starts[:, 0]
    for t in range(1, n):
        # continue the block (wrapping), or jump to a new random start
        idx[:, t] = np.where(restart[:, t], starts[:, t], (idx[:, t - 1] + 1) % n)
    return r[idx]


def sharpe_interval(returns: np.ndarray, n_boot: int = 2000, block: float = 20.0,
                    level: float = 0.95, seed: int = 0) -> dict:
    """Bootstrap confidence interval for a Sharpe, plus P(Sharpe <= 0).

    The p-value is the share of resamples with a Sharpe at or below zero: a
    one-sided answer to "could this have come from a strategy with no edge",
    read straight off the resampled distribution rather than from a t-table.
    """
    draws = stationary_bootstrap(returns, n_boot, block, seed)
    sd = draws.std(axis=1, ddof=1)
    sharpes = np.where(sd == 0, 0.0, draws.mean(axis=1) / np.where(sd == 0, 1, sd)
                       * np.sqrt(TRADING_DAYS))
    tail = (1 - level) / 2
    return {
        "sharpe": sharpe(returns),
        "lo": float(np.quantile(sharpes, tail)),
        "hi": float(np.quantile(sharpes, 1 - tail)),
        "p_le_zero": float(np.mean(sharpes <= 0)),
        "draws": sharpes,
    }


def paired_difference(a: np.ndarray, b: np.ndarray, n_boot: int = 2000,
                      block: float = 20.0, level: float = 0.95,
                      seed: int = 0) -> dict:
    """Bootstrap the Sharpe difference between two series, resampled together.

    Both series get the SAME resampled dates. Resampling them independently
    would throw away the fact that they saw the same market on the same day,
    which is most of why their Sharpes move together, and would widen the
    interval for no reason.
    """
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    if len(a) != len(b):
        raise ValueError(f"series must be the same length, got {len(a)} and {len(b)}")

    n = len(a)
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, n, size=(n_boot, n))
    restart = rng.random((n_boot, n)) < 1.0 / block
    restart[:, 0] = True
    idx = np.empty((n_boot, n), dtype=np.int64)
    idx[:, 0] = starts[:, 0]
    for t in range(1, n):
        idx[:, t] = np.where(restart[:, t], starts[:, t], (idx[:, t - 1] + 1) % n)

    def annualized(x):
        sd = x.std(axis=1, ddof=1)
        return np.where(sd == 0, 0.0, x.mean(axis=1) / np.where(sd == 0, 1, sd)
                        * np.sqrt(TRADING_DAYS))

    diff = annualized(a[idx]) - annualized(b[idx])
    tail = (1 - level) / 2
    return {
        "diff": sharpe(a) - sharpe(b),
        "lo": float(np.quantile(diff, tail)),
        "hi": float(np.quantile(diff, 1 - tail)),
        "p_le_zero": float(np.mean(diff <= 0)),
    }


def expected_max_sharpe(n_trials: int, sharpe_sd: float,
                        periods: int = TRADING_DAYS) -> float:
    """Annualized Sharpe the best of `n_trials` worthless strategies reaches.

    If every trial has a true Sharpe of zero and the estimates are spread by
    `sharpe_sd` (also annualized), the largest of them still lands here. The
    approximation is the standard one for the expected maximum of n normals,
    accurate to about 1% from n=5 up:

        E[max] ~ sd * [ (1-g) z(1 - 1/n) + g z(1 - 1/(n e)) ],  g = Euler's

    One trial gives zero, which is the sanity check: with nothing to choose
    between, there is no selection bias to subtract.
    """
    if n_trials < 2 or sharpe_sd <= 0:
        return 0.0
    g = EULER_MASCHERONI
    z1 = sps.norm.ppf(1 - 1.0 / n_trials)
    z2 = sps.norm.ppf(1 - 1.0 / (n_trials * np.e))
    return float(sharpe_sd * ((1 - g) * z1 + g * z2))


def effective_trials(curves: np.ndarray) -> float:
    """How many *independent* strategies a set of correlated ones amounts to.

    Deflation is driven by the number of tries, and 23 moving-average pairs on
    one index are nowhere near 23 independent tries - they are long the same
    market most of the same days. Counting them as 23 would overstate the
    selection bias, which sounds conservative but is not: it makes the luck
    bar too high and the surviving edge look harder-won than it was.

    The estimate is the participation ratio of the correlation matrix's
    eigenvalues, (sum lambda)^2 / sum lambda^2: identical strategies give 1,
    uncorrelated ones give back the count they started with.
    """
    x = np.atleast_2d(np.asarray(curves, dtype=float))
    if len(x) < 2:
        return float(len(x))
    corr = np.corrcoef(x)
    corr = np.nan_to_num(corr, nan=0.0)
    eig = np.clip(np.linalg.eigvalsh(corr), 0.0, None)
    if eig.sum() == 0:
        return float(len(x))
    return float(eig.sum() ** 2 / (eig ** 2).sum())


def probabilistic_sharpe(returns: np.ndarray, benchmark: float = 0.0,
                         periods: int = TRADING_DAYS) -> float:
    """P(true annualized Sharpe > `benchmark`), given the sample's shape.

    Bailey & Lopez de Prado's PSR. Skew and kurtosis are in the denominator
    because they change how reliable a Sharpe estimate is: negative skew and
    fat tails - exactly what daily equity returns have - mean the same Sharpe
    over the same number of days is weaker evidence than the normal-returns
    formula assumes.
    """
    r = np.asarray(returns, dtype=float)
    n = len(r)
    if n < 3 or r.std(ddof=1) == 0:
        return float("nan")

    sr = sharpe(r, periods) / np.sqrt(periods)              # per period
    sr0 = benchmark / np.sqrt(periods)
    skew = float(sps.skew(r, bias=False))
    kurt = float(sps.kurtosis(r, fisher=False, bias=False))  # 3 for a normal
    var = 1 - skew * sr + (kurt - 1) / 4 * sr ** 2
    if var <= 0:
        return float("nan")
    return float(sps.norm.cdf((sr - sr0) * np.sqrt(n - 1) / np.sqrt(var)))


def deflated_sharpe(returns: np.ndarray, n_trials: float, sharpe_sd: float,
                    benchmark: float = 0.0, periods: int = TRADING_DAYS) -> dict:
    """PSR measured against the Sharpe selection bias alone would produce.

    The bar is not zero: it is `benchmark` plus expected_max_sharpe(), the
    best a search over that many worthless variants turns up anyway. A
    deflated Sharpe near 1 means the winner cleared the bar; near 0.5 means
    it is about what luck delivers; below, worse.

    `benchmark` matters for anything long-only. A trend rule on an index is
    long the market most of the time, so it inherits the market's Sharpe and
    beating zero says nothing about the rule. Passing buy & hold's Sharpe
    asks the question worth asking.
    """
    threshold = expected_max_sharpe(int(round(n_trials)), sharpe_sd, periods)
    return {
        "sharpe": sharpe(returns, periods),
        "n_trials": n_trials,
        "benchmark": benchmark,
        "threshold": threshold,
        "bar": benchmark + threshold,
        "psr_vs_zero": probabilistic_sharpe(returns, 0.0, periods),
        "dsr": probabilistic_sharpe(returns, benchmark + threshold, periods),
    }
