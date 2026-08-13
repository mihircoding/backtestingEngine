"""End-to-end: the whole loop, checked to the cent.

Buy-and-hold with zero frictions is the perfect integration test because the
answer is computable by hand: final equity = cash + shares * (last - first).
If any component mis-dispatches, double-charges, or marks equity at the wrong
moment, this number comes out wrong.
"""

import pandas as pd
import pytest

from src.data_handler import HistoricalDataHandler
from src.engine import Backtest
from src.execution import SimulatedExecutionHandler
from src.portfolio import Portfolio
from src.strategy import BuyAndHoldStrategy


@pytest.fixture
def components():
    idx = pd.bdate_range("2024-01-01", periods=10)
    prices = pd.DataFrame(
        {"ABC": [100, 101, 103, 102, 105, 107, 106, 109, 111, 110]},
        index=idx, dtype=float,
    )
    data = HistoricalDataHandler(prices)
    strategy = BuyAndHoldStrategy(data)
    portfolio = Portfolio(data, initial_cash=100_000.0, trade_size=100)
    execution = SimulatedExecutionHandler(data, slippage_bps=0.0,
                                          commission_per_share=0.0)
    return data, strategy, portfolio, execution


class TestEndToEnd:
    def test_equity_curve_has_one_point_per_bar(self, components):
        data, strategy, portfolio, execution = components
        equity = Backtest(data, strategy, portfolio, execution).run()
        assert len(equity) == 10

    def test_buy_and_hold_final_equity_exact(self, components):
        data, strategy, portfolio, execution = components
        equity = Backtest(data, strategy, portfolio, execution).run()
        # bought 100 sh at 100 on bar 0 (no frictions); last close 110
        # equity = 100,000 + 100 * (110 - 100) = 101,000
        assert equity.iloc[-1] == pytest.approx(101_000.0)

    def test_first_bar_equity_reflects_same_bar_fill(self, components):
        data, strategy, portfolio, execution = components
        equity = Backtest(data, strategy, portfolio, execution).run()
        # fill at 100, marked at 100, zero costs -> no equity change on bar 0.
        # If mark_to_market runs before fills are processed this still passes,
        # but bar 1 would then be wrong -> checked next.
        assert equity.iloc[0] == pytest.approx(100_000.0)
        assert equity.iloc[1] == pytest.approx(100_100.0)  # 100 sh * +1.00
