"""Volatility-targeted position sizing.

The claim being tested is narrow and mechanical: a signal on a name realizing
twice the volatility should get half the notional, the estimate must never see
an unreleased bar, and the whole thing must be inert unless it's switched on.
"""

import numpy as np
import pytest

from src.data_handler import HistoricalDataHandler
from src.engine import Backtest
from src.events import SignalEvent, SignalType
from src.execution import SimulatedExecutionHandler
from src.portfolio import TRADING_DAYS, Portfolio
from src.strategy import BuyAndHoldStrategy
from tests.conftest import make_handler

import pandas as pd


def series(daily_vol: float, n: int = 60, start: float = 100.0, seed: int = 3) -> list[float]:
    """A price path with a known daily volatility and no drift."""
    rng = np.random.default_rng(seed)
    shocks = rng.normal(0, daily_vol, n)
    return list(start * np.exp(np.cumsum(shocks)))


def portfolio_at_last_bar(prices: dict, **kwargs) -> tuple:
    """Stream every bar, then hand back (handler, portfolio) ready to size."""
    handler = make_handler(prices)
    while handler.has_more():
        handler.next_bar()
    return handler, Portfolio(handler, initial_cash=100_000.0, trade_size=100, **kwargs)


class TestOffByDefault:
    def test_fixed_size_is_unchanged(self):
        h, p = portfolio_at_last_bar({"AAA": series(0.02)})
        order = p.on_signal(SignalEvent(h.current_time(), "AAA", SignalType.LONG))
        assert order.quantity == 100

    def test_no_vol_estimate_is_computed_when_off(self):
        # trailing_vol() is still callable, but on_signal must not consult it
        h, p = portfolio_at_last_bar({"AAA": series(0.02)})
        assert p.trailing_vol("AAA") is not None
        assert p._size_for("AAA") == 100


class TestVolEstimate:
    def test_recovers_a_known_volatility(self):
        # 1% daily -> ~15.9% annualized; a 20-bar sample is noisy, so this is
        # a loose band on purpose. It is checking the arithmetic, not the
        # sampling error.
        h, p = portfolio_at_last_bar({"AAA": series(0.01, n=400, seed=11)},
                                     vol_target=0.10, vol_window=250)
        expected = 0.01 * np.sqrt(TRADING_DAYS)
        assert p.trailing_vol("AAA") == pytest.approx(expected, rel=0.15)

    def test_returns_none_during_warmup(self):
        h, p = portfolio_at_last_bar({"AAA": [100.0] * 5}, vol_target=0.10, vol_window=20)
        assert p.trailing_vol("AAA") is None

    def test_returns_none_on_a_flat_series(self):
        # zero measured risk would divide by zero and ask for infinite size
        h, p = portfolio_at_last_bar({"AAA": [100.0] * 40}, vol_target=0.10, vol_window=20)
        assert p.trailing_vol("AAA") is None

    def test_cannot_see_unreleased_bars(self):
        # calm for 30 bars, then violent. Sized on bar 30, the estimate must
        # reflect the calm half only.
        prices = [100.0 + 0.01 * i for i in range(30)] + list(series(0.05, n=30, seed=5))
        handler = make_handler({"AAA": prices})
        for _ in range(30):
            handler.next_bar()
        p = Portfolio(handler, vol_target=0.10, vol_window=20)
        calm = p.trailing_vol("AAA")

        while handler.has_more():
            handler.next_bar()
        assert p.trailing_vol("AAA") > calm * 5


class TestSizing:
    def test_size_hits_the_target_risk(self):
        h, p = portfolio_at_last_bar({"AAA": series(0.01, n=400, seed=11)},
                                     vol_target=0.10, vol_window=250)
        shares = p._size_for("AAA")
        price = h.current_price("AAA")
        realized = shares * price * p.trailing_vol("AAA") / p.total_equity()
        assert realized == pytest.approx(0.10, rel=0.01)

    def test_twice_the_vol_gets_half_the_notional(self):
        calm = {"AAA": series(0.01, n=300, seed=21)}
        wild = {"AAA": series(0.02, n=300, seed=21)}  # same shocks, doubled

        hc, pc = portfolio_at_last_bar(calm, vol_target=0.10, vol_window=250)
        hw, pw = portfolio_at_last_bar(wild, vol_target=0.10, vol_window=250)

        calm_notional = pc._size_for("AAA") * hc.current_price("AAA")
        wild_notional = pw._size_for("AAA") * hw.current_price("AAA")
        assert wild_notional == pytest.approx(calm_notional / 2, rel=0.05)

    def test_leverage_cap_binds_in_a_calm_market(self):
        # 0.1% daily vol -> ~1.6% annualized -> a 10% target asks for ~6x
        h, p = portfolio_at_last_bar({"AAA": series(0.001, n=300, seed=7)},
                                     vol_target=0.10, vol_window=250, max_leverage=2.0)
        notional = p._size_for("AAA") * h.current_price("AAA")
        assert notional == pytest.approx(2.0 * p.total_equity(), rel=0.01)

    def test_falls_back_to_fixed_size_during_warmup(self):
        h, p = portfolio_at_last_bar({"AAA": [100.0, 101.0, 102.0]},
                                     vol_target=0.10, vol_window=20)
        assert p._size_for("AAA") == 100

    def test_exit_is_still_a_full_flatten(self):
        h, p = portfolio_at_last_bar({"AAA": series(0.02, n=300)},
                                     vol_target=0.10, vol_window=250)
        p.positions["AAA"] = 137  # whatever the sizer happened to ask for
        order = p.on_signal(SignalEvent(h.current_time(), "AAA", SignalType.EXIT))
        assert order.quantity == -137


class TestEndToEnd:
    def test_targeting_pulls_realized_vol_toward_the_target(self):
        """The point of the whole exercise, measured on the equity curve.

        Buy and hold a 32%-vol name. Fixed sizing puts a fixed share count on
        it and inherits whatever volatility that name happens to have.
        Targeting 10% should land materially closer to 10%.
        """
        prices = pd.DataFrame({"AAA": series(0.02, n=500, seed=13)},
                              index=pd.bdate_range("2023-01-02", periods=500))

        def equity(**kwargs):
            data = HistoricalDataHandler(prices)
            pf = Portfolio(data, initial_cash=100_000.0, trade_size=100, **kwargs)
            ex = SimulatedExecutionHandler(data, slippage_bps=0.0, commission_per_share=0.0)
            return Backtest(data, BuyAndHoldStrategy(data), pf, ex).run()

        def ann_vol(curve):
            return curve.pct_change().dropna().std(ddof=1) * np.sqrt(TRADING_DAYS)

        fixed = ann_vol(equity())
        targeted = ann_vol(equity(vol_target=0.10, vol_window=20))

        assert abs(targeted - 0.10) < abs(fixed - 0.10)


class TestRebalance:
    """on_market() is the half of vol targeting that keeps a live position at
    its target instead of only setting it at entry."""

    def test_off_by_default(self):
        h, p = portfolio_at_last_bar({"AAA": series(0.02, n=300)})
        p.positions["AAA"] = 100
        assert p.on_market(h.current_time()) == []

    def test_ignores_flat_symbols(self):
        h, p = portfolio_at_last_bar({"AAA": series(0.02, n=300)},
                                     vol_target=0.10, vol_window=250)
        p.positions["AAA"] = 0
        assert p.on_market(h.current_time()) == []

    def test_trims_an_oversized_position(self):
        h, p = portfolio_at_last_bar({"AAA": series(0.02, n=300)},
                                     vol_target=0.10, vol_window=250)
        target = p._size_for("AAA")
        p.positions["AAA"] = target * 3
        # equity moved when we handed ourselves free shares, so re-read the target
        order = p.on_market(h.current_time())[0]
        assert order.quantity < 0
        assert p.positions["AAA"] + order.quantity == p._size_for("AAA")

    def test_band_suppresses_small_corrections(self):
        h, p = portfolio_at_last_bar({"AAA": series(0.02, n=300)},
                                     vol_target=0.10, vol_window=250,
                                     rebalance_band=0.2)
        target = p._size_for("AAA")
        p.positions["AAA"] = int(target * 1.1)   # 10% off, inside the band
        assert p.on_market(h.current_time()) == []

    def test_a_short_stays_short(self):
        # resizing must never flip the sign; direction belongs to the strategy
        h, p = portfolio_at_last_bar({"AAA": series(0.02, n=300)},
                                     vol_target=0.10, vol_window=250)
        p.positions["AAA"] = -5
        orders = p.on_market(h.current_time())
        assert p.positions["AAA"] + orders[0].quantity < 0
