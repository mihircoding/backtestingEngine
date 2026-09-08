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
from src.execution import NextBarOpenExecutionHandler, SimulatedExecutionHandler
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


class TestEndToEndNextBarOpen:
    """Same buy-and-hold check as above, but through NextBarOpenExecutionHandler
    — the fill has to land one bar later, at that bar's open, not its close.
    """

    @pytest.fixture
    def components(self):
        idx = pd.bdate_range("2024-01-01", periods=10)
        closes = [100, 101, 103, 102, 105, 107, 106, 109, 111, 110]
        opens = [100, 102, 104, 101, 106, 108, 105, 110, 112, 109]
        prices = pd.DataFrame({"ABC": closes}, index=idx, dtype=float)
        opens_df = pd.DataFrame({"ABC": opens}, index=idx, dtype=float)

        data = HistoricalDataHandler(prices, opens=opens_df)
        strategy = BuyAndHoldStrategy(data)
        portfolio = Portfolio(data, initial_cash=100_000.0, trade_size=100)
        execution = NextBarOpenExecutionHandler(data, slippage_bps=0.0,
                                                commission_per_share=0.0)
        return data, strategy, portfolio, execution

    def test_bar_zero_equity_is_flat_because_the_fill_hasnt_happened_yet(self, components):
        data, strategy, portfolio, execution = components
        equity = Backtest(data, strategy, portfolio, execution).run()
        # The signal fires on bar 0, but the order only settles on bar 1's
        # open — bar 0 must show no trade at all yet.
        assert equity.iloc[0] == pytest.approx(100_000.0)

    def test_bar_one_equity_reflects_a_fill_at_bar_ones_open_not_bar_zeros_close(self, components):
        data, strategy, portfolio, execution = components
        equity = Backtest(data, strategy, portfolio, execution).run()
        # Bought 100 sh at bar 1's open (102), not bar 0's close (100).
        # cash = 100,000 - 100*102 = 89,800; marked at bar 1's close (101).
        assert equity.iloc[1] == pytest.approx(89_800.0 + 100 * 101.0)

    def test_final_equity_differs_from_same_bar_close_by_exactly_the_entry_gap(self, components):
        data, strategy, portfolio, execution = components
        equity = Backtest(data, strategy, portfolio, execution).run()
        # Entered one bar later, at 102 instead of 100 -> $200 worse, nothing
        # else about the run differs (zero costs, same exit price never sold).
        # Same-bar-close final equity is 101,000 (see the test above).
        assert equity.iloc[-1] == pytest.approx(101_000.0 - 100 * (102 - 100))
