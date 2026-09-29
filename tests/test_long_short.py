"""The short leg, and the carry it has to pay for existing.

Three separate claims get checked here.

The strategy emits SHORT where the long-only version emitted EXIT, still on
crossings rather than on states, so a rule that stays below its long average
for a year sends one signal and not two hundred and fifty.

The portfolio charges borrow. That is a per-BAR cost rather than a per-trade
one, which is the whole reason it could not live next to commission in the
execution handler, and it has to be exactly zero by default or every number
already in RESULTS.md silently changes.

And signal_position() in long_short.py, which reproduces the rule outside the
engine so the short leg can be examined on its own, actually agrees with the
engine. A second implementation that disagrees with the first is worse than no
second implementation, because the decomposition it feeds would be describing
a strategy nobody ran.
"""
import numpy as np
import pandas as pd
import pytest

from long_short import signal_position
from run_backtest import run
from src.events import SignalType
from src.portfolio import TRADING_DAYS, Portfolio
from src.strategy import (BuyAndHoldStrategy,
                          MovingAverageCrossLongShortStrategy,
                          MovingAverageCrossStrategy)
from tests.conftest import make_handler, release_all


def signals_from(prices, cls, short=3, long=5):
    handler = make_handler({"AAA": prices})
    strat = cls(handler, short_window=short, long_window=long)
    fired = []
    i = 0
    while handler.has_more():
        event = handler.next_bar()
        for sig in strat.on_market(event):
            fired.append((i, sig.signal))
        i += 1
    return fired


class TestLongShortSignals:
    def test_nothing_fires_during_warmup(self):
        assert signals_from([100, 101, 102, 103],
                            MovingAverageCrossLongShortStrategy) == []

    def test_a_falling_series_goes_short_once(self):
        fired = signals_from([100 - i for i in range(30)],
                             MovingAverageCrossLongShortStrategy)
        assert [s for _, s in fired] == [SignalType.SHORT]

    def test_a_rising_series_goes_long_once(self):
        fired = signals_from([100 + i for i in range(30)],
                             MovingAverageCrossLongShortStrategy)
        assert [s for _, s in fired] == [SignalType.LONG]

    def test_it_never_emits_exit(self):
        up_then_down = [100 + i for i in range(20)] + [120 - 2 * i for i in range(20)]
        fired = signals_from(up_then_down, MovingAverageCrossLongShortStrategy)
        assert SignalType.EXIT not in [s for _, s in fired]
        assert [s for _, s in fired] == [SignalType.LONG, SignalType.SHORT]

    def test_it_fires_on_the_same_bars_the_long_only_version_does(self):
        prices = ([100 + i for i in range(15)] + [115 - 2 * i for i in range(15)]
                  + [85 + 2 * i for i in range(15)])
        long_only = signals_from(prices, MovingAverageCrossStrategy)
        both = signals_from(prices, MovingAverageCrossLongShortStrategy)
        assert [i for i, _ in long_only] == [i for i, _ in both]


class TestBorrowCost:
    def _portfolio(self, bps, prices=(100.0,) * 10):
        handler = make_handler({"AAA": list(prices)})
        release_all(handler)
        return Portfolio(handler, initial_cash=100_000.0, trade_size=100,
                         short_borrow_bps=bps)

    def test_zero_by_default_so_nothing_already_measured_moves(self):
        handler = make_handler({"AAA": [100.0] * 10})
        release_all(handler)
        p = Portfolio(handler)
        p.positions["AAA"] = -1_000
        assert p.accrue_borrow() == 0.0
        assert p.cash == p.initial_cash

    def test_a_long_position_is_never_charged(self):
        p = self._portfolio(500.0)
        p.positions["AAA"] = +1_000
        assert p.accrue_borrow() == 0.0

    def test_a_short_is_charged_on_notional_per_bar(self):
        p = self._portfolio(365.0)          # 3.65% a year
        p.positions["AAA"] = -1_000         # $100,000 short at $100
        charge = p.accrue_borrow()
        expected = 100_000 * 0.0365 / TRADING_DAYS
        assert charge == pytest.approx(expected)
        assert p.cash == pytest.approx(p.initial_cash - expected)

    def test_it_accumulates_over_bars_rather_than_over_trades(self):
        p = self._portfolio(365.0)
        p.positions["AAA"] = -1_000
        for _ in range(5):
            p.accrue_borrow()
        assert p.borrow_paid == pytest.approx(
            5 * 100_000 * 0.0365 / TRADING_DAYS)

    def test_a_bigger_short_costs_proportionally_more(self):
        one = self._portfolio(200.0)
        one.positions["AAA"] = -100
        two = self._portfolio(200.0)
        two.positions["AAA"] = -300
        assert two.accrue_borrow() == pytest.approx(3 * one.accrue_borrow())

    def test_the_mark_already_has_the_charge_in_it(self):
        p = self._portfolio(365.0)
        p.positions["AAA"] = -1_000
        p.mark_to_market(pd.Timestamp("2023-01-02"))
        _, equity = p.equity_history[-1]
        assert equity == pytest.approx(p.cash + (-1_000) * 100.0)
        assert p.borrow_paid > 0


class TestBorrowChangesTheAnswer:
    def _prices(self):
        # down for a year then up, so the rule is short for a long stretch
        n = 400
        path = ([200.0 - 0.3 * i for i in range(n // 2)]
                + [140.0 + 0.3 * i for i in range(n // 2)])
        return pd.DataFrame({"AAA": path},
                            index=pd.bdate_range("2020-01-01", periods=n))

    def test_charging_borrow_makes_the_long_short_rule_worse(self):
        prices = self._prices()
        free = run(prices, MovingAverageCrossLongShortStrategy, 100,
                   short_window=20, long_window=60)
        charged = run(prices, MovingAverageCrossLongShortStrategy, 100,
                      short_window=20, long_window=60,
                      portfolio_kwargs={"short_borrow_bps": 400.0})
        assert charged.iloc[-1] < free.iloc[-1]

    def test_borrow_does_nothing_to_a_rule_that_never_shorts(self):
        prices = self._prices()
        free = run(prices, MovingAverageCrossStrategy, 100,
                   short_window=20, long_window=60)
        charged = run(prices, MovingAverageCrossStrategy, 100,
                      short_window=20, long_window=60,
                      portfolio_kwargs={"short_borrow_bps": 400.0})
        assert charged.iloc[-1] == pytest.approx(free.iloc[-1])

    def test_buy_and_hold_is_untouched_too(self):
        prices = self._prices()
        a = run(prices, BuyAndHoldStrategy, 100)
        b = run(prices, BuyAndHoldStrategy, 100,
                portfolio_kwargs={"short_borrow_bps": 1_000.0})
        assert a.iloc[-1] == pytest.approx(b.iloc[-1])


class TestSignalPositionMatchesTheEngine:
    """The outside-the-engine reconstruction has to be the same rule."""

    def _series(self):
        rng = np.random.default_rng(11)
        steps = rng.normal(0.0004, 0.012, 600)
        path = 100.0 * np.exp(np.cumsum(steps))
        return pd.Series(path, index=pd.bdate_range("2019-01-01", periods=600))

    def test_it_flips_on_the_bars_the_strategy_signals_on(self):
        prices = self._series()
        handler = make_handler({"AAA": list(prices.values)})
        strat = MovingAverageCrossLongShortStrategy(handler, short_window=20,
                                                    long_window=60)
        engine_flips = []
        i = 0
        while handler.has_more():
            event = handler.next_bar()
            for sig in strat.on_market(event):
                engine_flips.append((i, 1 if sig.signal is SignalType.LONG else -1))
            i += 1

        pos = signal_position(prices, short=20, long=60).values
        recon_flips = [(j, int(pos[j])) for j in range(1, len(pos))
                       if pos[j] != pos[j - 1] and pos[j - 1] != 0.0]
        first = [(j, int(pos[j])) for j in range(len(pos))
                 if pos[j] != 0.0][:1]
        assert first + recon_flips == engine_flips

    def test_the_warmup_holds_no_position_either_way(self):
        pos = signal_position(self._series(), short=20, long=60)
        assert (pos.iloc[:59] == 0.0).all()
        assert pos.iloc[59:].abs().eq(1.0).all()

    def test_long_only_mode_never_goes_negative(self):
        pos = signal_position(self._series(), short=20, long=60,
                              allow_short=False)
        assert pos.min() == 0.0 and pos.max() == 1.0
