"""param_grid isn't part of src/ - it's a driver-level analysis, not engine
logic - so it gets one lightweight test on synthetic data rather than a full
suite. The point is just to pin down the contract: every (short, long) pair
with short < long shows up once, nothing duplicated, nothing silently
dropped.
"""

import numpy as np
import pandas as pd

from run_backtest import param_grid


def _trending_prices(n=120, seed=3):
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    path = 100 + 0.1 * t + np.cumsum(rng.normal(0, 0.3, n))
    idx = pd.bdate_range("2022-01-03", periods=n)
    return pd.DataFrame({"SYN": path}, index=idx)


class TestParamGrid:
    def test_only_short_less_than_long_pairs_included(self):
        prices = _trending_prices()
        rows = param_grid(prices, trade_size=10,
                          short_windows=(5, 10, 20), long_windows=(10, 20))
        pairs = {(r["short"], r["long"]) for r in rows}
        assert all(s < l for s, l in pairs)
        # (10, 10) is excluded (not <), everything else with s < l is present
        assert pairs == {(5, 10), (5, 20), (10, 20)}

    def test_each_row_has_sharpe_and_return(self):
        prices = _trending_prices()
        rows = param_grid(prices, trade_size=10,
                          short_windows=(5,), long_windows=(20,))
        assert len(rows) == 1
        row = rows[0]
        assert set(row) == {"short", "long", "sharpe", "total_return", "max_dd"}
        assert isinstance(row["sharpe"], float)
