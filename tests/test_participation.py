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


# ---------- impact on the order, not the slice ----------

SIGMA = 100.0   # 1% daily vol, in bps, fixed so the tape can be flat


def work(ex, data, symbol="AAA"):
    """Drive one working order to completion, returning its fills."""
    fills = []
    while ex.working_quantity(symbol) != 0 and data.has_more():
        data.next_bar()
        fills.extend(ex.pop_settled_fills(data.current_time()))
    return fills


def test_impact_on_takes_only_the_two_forms():
    with pytest.raises(ValueError):
        handler(impact_on="metaorder")


def test_the_order_form_charges_every_slice_the_root_of_the_whole_order():
    """500 shares at a 100-share cap is five slices. Each one costs slippage
    plus Y * sigma * sqrt(500 / 1000), whatever slice it is."""
    data, ex = handler(volume=1000.0, n=12, slippage_bps=2.0, impact_coef=0.5,
                       impact_on="order", sigma_bps=SIGMA)
    data.next_bar()
    ex.execute(OrderEvent(time=data.current_time(), symbol="AAA", quantity=500))
    fills = work(ex, data)
    assert len(fills) == 5
    expected = 2.0 + 0.5 * SIGMA * np.sqrt(500 / 1000)
    for fill in fills:
        assert (fill.fill_price / 100.0 - 1) * 10_000 == pytest.approx(expected)
    assert ex.slice_cost_bps == pytest.approx([expected] * 5)
    assert ex.slice_shares == [100] * 5


def test_the_slice_form_charges_every_slice_the_root_of_the_cap():
    """The original behaviour: the same five slices cost slippage plus
    sigma * sqrt(100 / 1000) each, and would for an order of any size."""
    data, ex = handler(volume=1000.0, n=12, slippage_bps=2.0, impact_coef=1.0,
                       impact_on="slice", sigma_bps=SIGMA)
    data.next_bar()
    ex.execute(OrderEvent(time=data.current_time(), symbol="AAA", quantity=500))
    fills = work(ex, data)
    expected = 2.0 + SIGMA * np.sqrt(100 / 1000)
    assert ex.slice_cost_bps == pytest.approx([expected] * 5)


def test_only_the_order_form_charges_a_bigger_sliced_order_more():
    def cost(impact_on, quantity):
        data, ex = handler(volume=1000.0, n=40, slippage_bps=0.0,
                           impact_on=impact_on, sigma_bps=SIGMA)
        data.next_bar()
        ex.execute(OrderEvent(time=data.current_time(), symbol="AAA",
                              quantity=quantity))
        work(ex, data)
        return np.average(ex.slice_cost_bps, weights=ex.slice_shares)

    assert cost("slice", 900) == pytest.approx(cost("slice", 300))
    assert cost("order", 900) == pytest.approx(cost("order", 300) * np.sqrt(3))


def test_a_fixed_sigma_is_used_even_during_warmup():
    """Without the override the first vol_window bars charge no impact. With
    it, bar two of a twelve-bar tape is charged like any other."""
    data, ex = handler(volume=10_000.0, n=12, slippage_bps=0.0, sigma_bps=SIGMA)
    data.next_bar()
    ex.execute(OrderEvent(time=data.current_time(), symbol="AAA", quantity=100))
    fills = work(ex, data)
    assert ex.slice_cost_bps[0] == pytest.approx(SIGMA * np.sqrt(100 / 10_000))

    data, ex = handler(volume=10_000.0, n=12, slippage_bps=0.0)
    data.next_bar()
    ex.execute(OrderEvent(time=data.current_time(), symbol="AAA", quantity=100))
    work(ex, data)
    assert ex.slice_cost_bps[0] == 0.0


def test_the_order_forms_volume_is_the_trailing_average_of_released_bars():
    """Volume 1,000 for ten bars, then 9,000. An order arriving on the bar
    after the jump, with a 5-bar window, sees (1000*4 + 9000) / 5 = 2,600 -
    not the 9,000 of the bar it arrived on and not the 1,000 before it."""
    vols = [1000.0] * 10 + [9000.0] * 10
    data = make_handler_with_volumes({"AAA": [100.0] * 20}, {"AAA": [100.0] * 20},
                                     {"AAA": vols})
    ex = ParticipationLimitedExecutionHandler(data, participation=1.0,
                                              slippage_bps=0.0, impact_coef=1.0,
                                              impact_on="order", vol_window=5,
                                              sigma_bps=SIGMA)
    for _ in range(11):
        data.next_bar()
    ex.execute(OrderEvent(time=data.current_time(), symbol="AAA", quantity=260))
    work(ex, data)
    assert ex.slice_cost_bps[0] == pytest.approx(SIGMA * np.sqrt(260 / 2600))


def test_get_latest_volumes_never_reaches_past_the_cursor():
    data = make_handler_with_volumes({"AAA": [100.0] * 6}, {"AAA": [100.0] * 6},
                                     {"AAA": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]})
    assert len(data.get_latest_volumes("AAA", 3)) == 0
    data.next_bar()
    data.next_bar()
    assert list(data.get_latest_volumes("AAA", 3)) == [1.0, 2.0]
    data.next_bar()
    data.next_bar()
    assert list(data.get_latest_volumes("AAA", 3)) == [2.0, 3.0, 4.0]


def test_a_topped_up_order_is_rebased_to_what_is_left_to_work():
    """200 shares arrive, one slice of 100 fills, then 300 more arrive. The
    400 still working is the order the law is applied to from then on."""
    data, ex = handler(volume=1000.0, n=20, slippage_bps=0.0, impact_coef=1.0,
                       impact_on="order", sigma_bps=SIGMA)
    data.next_bar()
    ex.execute(OrderEvent(time=data.current_time(), symbol="AAA", quantity=200))
    data.next_bar()
    ex.pop_settled_fills(data.current_time())
    assert ex.slice_cost_bps[-1] == pytest.approx(SIGMA * np.sqrt(200 / 1000))
    ex.execute(OrderEvent(time=data.current_time(), symbol="AAA", quantity=300))
    assert ex.working_quantity("AAA") == 400
    data.next_bar()
    ex.pop_settled_fills(data.current_time())
    assert ex.slice_cost_bps[-1] == pytest.approx(SIGMA * np.sqrt(400 / 1000))


def test_a_completed_order_leaves_nothing_behind_for_the_next_one():
    data, ex = handler(volume=1000.0, n=30, slippage_bps=0.0, impact_coef=1.0,
                       impact_on="order", sigma_bps=SIGMA)
    data.next_bar()
    ex.execute(OrderEvent(time=data.current_time(), symbol="AAA", quantity=900))
    work(ex, data)
    assert ex._arrival == {}
    ex.execute(OrderEvent(time=data.current_time(), symbol="AAA", quantity=-100))
    data.next_bar()
    ex.pop_settled_fills(data.current_time())
    assert ex.slice_cost_bps[-1] == pytest.approx(SIGMA * np.sqrt(100 / 1000))


def test_a_reversal_that_nets_to_zero_clears_the_arrival_too():
    data, ex = handler(volume=1000.0, n=12, impact_on="order", sigma_bps=SIGMA)
    data.next_bar()
    ex.execute(OrderEvent(time=data.current_time(), symbol="AAA", quantity=300))
    ex.execute(OrderEvent(time=data.current_time(), symbol="AAA", quantity=-300))
    assert ex.working_quantity("AAA") == 0
    assert ex._arrival == {}


# ---------- the liquidity budget: send what you can get done ----------

def _signal(data):
    from src.events import SignalEvent, SignalType
    return SignalEvent(time=data.current_time(), symbol="AAA", signal=SignalType.LONG)


def test_the_budget_is_days_times_participation_times_adv():
    data, ex = handler(volume=1000.0)
    pf = Portfolio(data, initial_cash=1e9, trade_size=100_000,
                   max_fill_days=5, participation=0.10)
    data.next_bar()
    assert pf.liquidity_budget("AAA") == 500          # 5 x 10% x 1000
    assert pf.on_signal(_signal(data)).quantity == 500
    assert pf.liquidity_capped == 1


def test_a_position_that_fits_is_not_touched():
    data, ex = handler(volume=1000.0)
    pf = Portfolio(data, initial_cash=1e9, trade_size=300,
                   max_fill_days=5, participation=0.10)
    data.next_bar()
    assert pf.on_signal(_signal(data)).quantity == 300
    assert pf.liquidity_capped == 0


def test_a_capped_position_completes_inside_the_budgeted_days():
    """The whole point: at the budget, the participation handler fills the
    order in max_fill_days bars, where the uncapped order would still be
    working when the data ran out."""
    from src.engine import Backtest
    from src.strategy import BuyAndHoldStrategy

    def run_with(cap):
        data, ex = handler(volume=1000.0, n=12)
        kwargs = {"max_fill_days": 3, "participation": 0.10} if cap else {}
        pf = Portfolio(data, initial_cash=1e9, trade_size=100_000,
                       pending=ex.working_quantity, **kwargs)
        Backtest(data, BuyAndHoldStrategy(data), pf, ex).run()
        ex.finalize()
        return ex

    capped, free = run_with(True), run_with(False)
    assert capped.unfilled_shares == 0
    assert capped.max_delay_bars == 3
    assert free.unfilled_shares > 0


def test_the_budget_reads_released_volume_only():
    volumes = [100.0] * 6 + [10_000.0] * 6
    data = make_handler_with_volumes({"AAA": FLAT}, {"AAA": FLAT}, {"AAA": volumes})
    pf = Portfolio(data, initial_cash=1e9, trade_size=100_000,
                   max_fill_days=2, participation=0.5, adv_window=20)
    for _ in range(6):
        data.next_bar()
    # six bars of 100 released; the 10,000s are still in the future
    assert pf.liquidity_budget("AAA") == 100


def test_the_budget_clips_the_vol_targeted_size_too():
    prices = [100.0 * (1 + 0.02 * ((-1) ** i)) for i in range(40)]
    data = make_handler_with_volumes({"AAA": prices}, {"AAA": prices},
                                     {"AAA": [1000.0] * 40})
    pf = Portfolio(data, initial_cash=1e9, vol_target=0.10, vol_window=20,
                   max_fill_days=1, participation=0.10)
    for _ in range(25):
        data.next_bar()
    assert pf._risk_size_for("AAA") > 100
    assert pf.on_signal(_signal(data)).quantity == 100


def test_days_and_rate_go_together():
    data, ex = handler()
    with pytest.raises(ValueError):
        Portfolio(data, max_fill_days=5)
    with pytest.raises(ValueError):
        Portfolio(data, participation=0.1)
    with pytest.raises(ValueError):
        Portfolio(data, max_fill_days=0, participation=0.1)


def test_no_budget_is_the_old_behaviour():
    data, ex = handler(volume=1000.0)
    pf = Portfolio(data, initial_cash=1e9, trade_size=100_000)
    data.next_bar()
    assert pf.liquidity_budget("AAA") is None
    assert pf.on_signal(_signal(data)).quantity == 100_000


def test_exposure_is_gross_notional_over_equity_each_bar():
    from src.engine import Backtest
    from src.strategy import BuyAndHoldStrategy
    data = make_handler_with_volumes({"AAA": [100.0] * 5}, {"AAA": [100.0] * 5},
                                     {"AAA": [1e6] * 5})
    ex = NextBarOpenExecutionHandler(data, slippage_bps=0.0, commission_per_share=0.0)
    pf = Portfolio(data, initial_cash=100_000.0, trade_size=500)
    Backtest(data, BuyAndHoldStrategy(data), pf, ex).run()
    exposure = [x for _, x in pf.exposure_history]
    # bar 1 signals, bar 2 fills at the open: flat, then half the book, forever
    assert exposure[0] == 0.0
    assert exposure[1:] == pytest.approx([0.5] * 4)


# ---------- the budget on the way out ----------

def _quiet_after(n_loud=10, n_quiet=20, loud=1000.0, quiet=200.0):
    volumes = [loud] * n_loud + [quiet] * n_quiet
    n = n_loud + n_quiet
    return make_handler_with_volumes({"AAA": [100.0] * n}, {"AAA": [100.0] * n},
                                     {"AAA": volumes})


def test_a_held_position_is_sold_down_when_the_tape_goes_quiet():
    data = _quiet_after()
    pf = Portfolio(data, initial_cash=1e9, trade_size=100_000,
                   max_fill_days=5, participation=0.10, adv_window=5,
                   trim_to_budget=True)
    for _ in range(5):
        data.next_bar()
    assert pf.on_signal(_signal(data)).quantity == 500      # 5 x 10% x 1000
    pf.positions["AAA"] = 500                                # filled in full
    assert pf.on_market(data.current_time()) == []           # fits today
    for _ in range(10):
        data.next_bar()                                      # five quiet bars in the window
    orders = pf.on_market(data.current_time())
    assert len(orders) == 1
    assert orders[0].quantity == 100 - 500                   # down to 5 x 10% x 200
    assert pf.liquidity_trims == 1


def test_the_trim_sells_down_and_never_tops_up():
    data = _quiet_after(n_loud=10, n_quiet=20, loud=200.0, quiet=1000.0)
    pf = Portfolio(data, initial_cash=1e9, trade_size=100_000,
                   max_fill_days=5, participation=0.10, adv_window=5,
                   trim_to_budget=True)
    for _ in range(5):
        data.next_bar()
    pf.positions["AAA"] = 100
    for _ in range(10):
        data.next_bar()                                      # budget is now 500
    assert pf.on_market(data.current_time()) == []
    assert pf.liquidity_trims == 0


def test_the_trim_leaves_a_position_a_little_over_the_line_alone():
    data = _quiet_after(n_loud=10, n_quiet=20, loud=1000.0, quiet=900.0)
    pf = Portfolio(data, initial_cash=1e9, trade_size=100_000,
                   max_fill_days=5, participation=0.10, adv_window=5,
                   trim_to_budget=True, rebalance_band=0.2)
    for _ in range(5):
        data.next_bar()
    pf.positions["AAA"] = 500
    for _ in range(10):
        data.next_bar()                                      # budget 450: 11% over, inside the band
    assert pf.on_market(data.current_time()) == []


def test_the_trim_counts_what_is_already_working():
    data = _quiet_after()
    pf = Portfolio(data, initial_cash=1e9, trade_size=100_000,
                   max_fill_days=5, participation=0.10, adv_window=5,
                   trim_to_budget=True, pending=lambda s: -400)
    for _ in range(15):
        data.next_bar()
    pf.positions["AAA"] = 500                                # 400 of it already being sold
    assert pf.on_market(data.current_time()) == []


def test_the_trim_applies_to_a_short_too():
    data = _quiet_after()
    pf = Portfolio(data, initial_cash=1e9, trade_size=100_000,
                   max_fill_days=5, participation=0.10, adv_window=5,
                   trim_to_budget=True)
    for _ in range(15):
        data.next_bar()
    pf.positions["AAA"] = -500
    orders = pf.on_market(data.current_time())
    assert orders[0].quantity == 400                         # buy back down to -100


def test_the_trim_needs_a_budget_and_is_off_by_default():
    data = _quiet_after()
    with pytest.raises(ValueError):
        Portfolio(data, trim_to_budget=True)
    pf = Portfolio(data, initial_cash=1e9, trade_size=100_000,
                   max_fill_days=5, participation=0.10, adv_window=5)
    for _ in range(15):
        data.next_bar()
    pf.positions["AAA"] = 500
    assert pf.on_market(data.current_time()) == []
    assert pf.liquidity_trims == 0


def test_a_trimmed_book_is_further_out_when_the_exit_comes():
    """The claim behind the flag, at its most modest: once the tape goes
    quiet the trimmed book starts selling before the signal does, so when
    the exit arrives there is less left to get out of. It does not get out
    inside the budget - nothing can, on a tape that drops tenfold, because
    the trim is an order into the same thin volume - and the test says so
    rather than pretending."""
    from src.engine import Backtest
    from src.events import SignalEvent, SignalType

    class InThenOut:
        def __init__(self, data):
            self.data = data
            self.bar = 0

        def on_market(self, event):
            self.bar += 1
            if self.bar == 6:
                return [SignalEvent(event.time, "AAA", SignalType.LONG)]
            if self.bar == 26:
                return [SignalEvent(event.time, "AAA", SignalType.EXIT)]
            return []

    def run_with(trim):
        volumes = [1000.0] * 12 + [100.0] * 28
        data = make_handler_with_volumes({"AAA": [100.0] * 40}, {"AAA": [100.0] * 40},
                                         {"AAA": volumes})
        ex = ParticipationLimitedExecutionHandler(data, participation=0.10)
        pf = Portfolio(data, initial_cash=1e9, trade_size=100_000,
                       pending=ex.working_quantity, max_fill_days=5, participation=0.10,
                       adv_window=5, trim_to_budget=trim)
        Backtest(data, InThenOut(data), pf, ex).run()
        ex.finalize()
        return pf, ex

    pf_trim, ex_trim = run_with(True)
    pf_free, ex_free = run_with(False)
    assert pf_trim.liquidity_trims >= 1
    assert pf_free.liquidity_trims == 0
    # both books bought 500 into 1000-share bars and sell into 100-share bars
    # at 10 a bar, and neither exit finished before the data ran out...
    assert ex_free.unfilled_shares > 0 and ex_trim.unfilled_shares > 0
    # ...but the trimmed one had been selling since the volume dried up, so
    # it was smaller at the signal and is smaller at the end
    free_at_exit = [x for _, x in pf_free.exposure_history][25]
    trim_at_exit = [x for _, x in pf_trim.exposure_history][25]
    assert trim_at_exit < free_at_exit
    assert pf_trim.positions["AAA"] < pf_free.positions["AAA"]
