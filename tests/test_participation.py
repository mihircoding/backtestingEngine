"""Tests for the volume-participation execution handler.

Three things have to be true for a capacity study built on this to mean
anything: the cap is never exceeded, nothing is silently lost when an order
can't complete, and the cost of a slice rises with its size in the shape the
square-root law says it should.
"""

import numpy as np
import pandas as pd
import pytest

from src.engine import Backtest
from src.events import OrderEvent
from src.execution import (NextBarOpenExecutionHandler,
                           ParticipationLimitedExecutionHandler)
from src.portfolio import Portfolio
from src.strategy import MovingAverageCrossStrategy
from tests.conftest import make_handler_with_volumes

FLAT = [100.0] * 12


def handler(volume=1000.0, n=12, participation=0.10, **kwargs):
    data = make_handler_with_volumes({"AAA": [100.0] * n}, {"AAA": [100.0] * n},
                                     {"AAA": [volume] * n})
    return data, ParticipationLimitedExecutionHandler(
        data, participation=participation, **kwargs)


def test_a_small_order_fills_in_one_slice():
    data, ex = handler(volume=10_000.0)
    data.next_bar()
    ex.execute(OrderEvent(time=data.current_time(), symbol="AAA", quantity=50))
    data.next_bar()
    fills = ex.pop_settled_fills(data.current_time())
    assert len(fills) == 1
    assert fills[0].quantity == 50
    assert ex.working_quantity("AAA") == 0


def test_the_cap_is_never_exceeded():
    """1,000 shares a bar at a 10% cap means 100 shares a bar, no matter what
    was asked for."""
    data, ex = handler(volume=1000.0)
    data.next_bar()
    ex.execute(OrderEvent(time=data.current_time(), symbol="AAA", quantity=1000))
    for _ in range(5):
        data.next_bar()
        for fill in ex.pop_settled_fills(data.current_time()):
            assert abs(fill.quantity) == 100


def test_an_order_takes_as_many_bars_as_the_cap_implies():
    data, ex = handler(volume=1000.0)
    data.next_bar()
    ex.execute(OrderEvent(time=data.current_time(), symbol="AAA", quantity=500))
    filled = 0
    bars = 0
    while ex.working_quantity("AAA") != 0 and data.has_more():
        data.next_bar()
        bars += 1
        filled += sum(f.quantity for f in ex.pop_settled_fills(data.current_time()))
    assert filled == 500
    assert bars == 5
    assert ex.max_delay_bars == 5


def test_selling_is_capped_the_same_way():
    """The cap is on size, not on direction. A sign error here would make one
    side of every trade free."""
    data, ex = handler(volume=1000.0)
    data.next_bar()
    ex.execute(OrderEvent(time=data.current_time(), symbol="AAA", quantity=-500))
    data.next_bar()
    fills = ex.pop_settled_fills(data.current_time())
    assert fills[0].quantity == -100
    assert ex.working_quantity("AAA") == -400


def test_a_reversing_order_cancels_the_working_one():
    """What an order management system does, and what stops a whipsawing
    strategy from stacking contradictory instructions."""
    data, ex = handler(volume=1000.0)
    data.next_bar()
    ex.execute(OrderEvent(time=data.current_time(), symbol="AAA", quantity=400))
    ex.execute(OrderEvent(time=data.current_time(), symbol="AAA", quantity=-400))
    assert ex.working_quantity("AAA") == 0
    data.next_bar()
    assert ex.pop_settled_fills(data.current_time()) == []


def test_a_zero_volume_bar_fills_nothing_and_loses_nothing():
    data = make_handler_with_volumes({"AAA": FLAT}, {"AAA": FLAT},
                                     {"AAA": [0.0] * 6 + [10_000.0] * 6})
    ex = ParticipationLimitedExecutionHandler(data, participation=0.10)
    data.next_bar()
    ex.execute(OrderEvent(time=data.current_time(), symbol="AAA", quantity=200))
    for _ in range(4):
        data.next_bar()
        assert ex.pop_settled_fills(data.current_time()) == []
    assert ex.working_quantity("AAA") == 200


def test_unfilled_shares_are_counted_not_forgotten():
    """An order still working when the data runs out is a position the
    strategy believes it has and does not. If this number were dropped, the
    capacity study would quietly report the unconstrained result."""
    data, ex = handler(volume=1000.0, n=4)
    data.next_bar()
    ex.execute(OrderEvent(time=data.current_time(), symbol="AAA", quantity=1000))
    while data.has_more():
        data.next_bar()
        ex.pop_settled_fills(data.current_time())
    ex.finalize()
    assert ex.unfilled_shares == 1000 - 3 * 100


def test_impact_grows_like_the_square_root_of_participation():
    """Double the participation rate and impact should rise by sqrt(2), not by
    2. This is the whole content of the square-root law, so it gets a test
    rather than a comment."""
    rng = np.random.default_rng(0)
    n = 60
    closes = list(100 * np.cumprod(1 + rng.normal(0, 0.01, n)))
    data = make_handler_with_volumes({"AAA": closes}, {"AAA": closes},
                                     {"AAA": [1_000_000.0] * n})
    ex = ParticipationLimitedExecutionHandler(data, participation=1.0)
    for _ in range(40):
        data.next_bar()
    low = ex._impact_bps("AAA", 0.01)
    high = ex._impact_bps("AAA", 0.04)
    assert high == pytest.approx(2 * low, rel=1e-9)   # sqrt(4x) = 2x


def test_impact_is_zero_during_warmup():
    """No volatility estimate yet means no impact estimate. Understating cost
    on the first few bars is a stated limitation; inventing a number is not."""
    data, ex = handler(volume=1_000_000.0)
    data.next_bar()
    assert ex._impact_bps("AAA", 0.5) == 0.0


def test_it_costs_more_than_the_unconstrained_handler_on_the_same_order():
    """Same bar, same price, same commission - the only difference is that one
    of them charges for size."""
    rng = np.random.default_rng(5)
    closes = list(100 * np.cumprod(1 + rng.normal(0, 0.01, 40)))
    common = dict(slippage_bps=2.0, commission_per_share=0.0)
    data_a = make_handler_with_volumes({"AAA": closes}, {"AAA": closes},
                                       {"AAA": [10_000.0] * 40})
    data_b = make_handler_with_volumes({"AAA": closes}, {"AAA": closes},
                                       {"AAA": [10_000.0] * 40})
    # Same prices, same bar, same commission. The only difference between the
    # two handlers is that one of them charges for taking half a day's volume.
    free = NextBarOpenExecutionHandler(data_a, **common)
    limited = ParticipationLimitedExecutionHandler(data_b, participation=1.0, **common)
    for _ in range(30):
        data_a.next_bar()
        data_b.next_bar()
    for ex, data in ((free, data_a), (limited, data_b)):
        ex.execute(OrderEvent(time=data.current_time(), symbol="AAA", quantity=5000))
    data_a.next_bar()
    data_b.next_bar()
    free_price = free.pop_settled_fills(data_a.current_time())[0].fill_price
    limited_price = limited.pop_settled_fills(data_b.current_time())[0].fill_price
    assert limited_price > free_price


def test_the_portfolio_does_not_re_order_shares_already_in_flight():
    """The bug this handler makes possible, and the reason Portfolio grew a
    `pending` hook.

    Portfolio sizes every order as target-minus-current. When fills were
    instant, current was the whole truth. With a multi-day fill working, the
    shares in flight are already committed, and a portfolio that ignores them
    re-sends them - every bar, until the position is several times the size it
    was aiming for. Position plus open orders is what a real order management
    system tracks.
    """
    from src.events import SignalEvent, SignalType

    def second_order(use_hook: bool):
        data, ex = handler(volume=1000.0)
        pf = Portfolio(data, initial_cash=1_000_000.0, trade_size=900,
                       pending=ex.working_quantity if use_hook else None)
        data.next_bar()
        signal = SignalEvent(time=data.current_time(), symbol="AAA",
                             signal=SignalType.LONG)
        first = pf.on_signal(signal)
        ex.execute(first)
        return pf.on_signal(signal)

    assert second_order(use_hook=True) is None
    assert second_order(use_hook=False).quantity == 900


def test_participation_must_be_a_fraction():
    data = make_handler_with_volumes({"AAA": FLAT}, {"AAA": FLAT},
                                     {"AAA": [1.0] * 12})
    for bad in (0.0, -0.1, 1.5):
        with pytest.raises(ValueError):
            ParticipationLimitedExecutionHandler(data, participation=bad)


def test_volume_data_is_required():
    from src.data_handler import HistoricalDataHandler
    idx = pd.bdate_range("2023-01-02", periods=12)
    data = HistoricalDataHandler(pd.DataFrame({"AAA": FLAT}, index=idx))
    data.next_bar()
    with pytest.raises(ValueError):
        data.current_volume("AAA")
