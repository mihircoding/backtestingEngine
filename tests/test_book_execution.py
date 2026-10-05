"""Tests for execution through the matching engine.

The claim this handler makes is that the fill price is a consequence of the
book's depth and not of a parameter, so that is what has to be pinned down:
one level for an order the touch can absorb, more levels as the order grows,
nothing invented when the book runs out, and the same shares in as out.

The reference prices are worked by hand rather than taken from a run. The book
is seeded at tick increments either side of the bar's open with a known number
of shares per level, so the volume-weighted price of a market order is
arithmetic, and a test that agrees with the arithmetic is testing the handler
rather than recording it.
"""

import numpy as np
import pytest

from exchange.fees import MAKER_TAKER
from exchange.order import TICK, to_tick
from src.book_execution import BookExecutionHandler
from src.engine import Backtest
from src.events import OrderEvent
from src.execution import ParticipationLimitedExecutionHandler
from src.portfolio import Portfolio
from src.strategy import MovingAverageCrossStrategy
from tests.conftest import make_handler_with_volumes

OPEN = 100.0
VOLUME = 1_000_000.0


def handler(volume=VOLUME, n=12, depth_frac=0.001, levels=10,
            participation=1.0, shape=0.0, **kwargs):
    """A flat book: every bar opens at 100.00 with `volume` shares traded.

    depth_frac 0.001 of a million shares is 1,000 shares a level, so the
    arithmetic in these tests is in round numbers.

    shape is pinned to 0.0 here rather than left at the handler's default,
    which is deliberate. These tests check the matching mechanism - one level
    for a small order, the volume-weighted average of what was taken, shares
    conserved - and a flat book is the one where that arithmetic can be done
    by hand and disagreed with. The shaped book the handler actually ships
    with is tested separately, at the bottom of this file, against the
    properties the shape is supposed to have.
    """
    data = make_handler_with_volumes({"AAA": [OPEN] * n}, {"AAA": [OPEN] * n},
                                      {"AAA": [volume] * n})
    return data, BookExecutionHandler(data, depth_frac=depth_frac, levels=levels,
                                       participation=participation, shape=shape,
                                       **kwargs)


def send(data, ex, quantity, bars=1):
    """Place one order and settle it over `bars` subsequent bars."""
    data.next_bar()
    ex.execute(OrderEvent(time=data.current_time(), symbol="AAA",
                          quantity=quantity))
    fills = []
    for _ in range(bars):
        data.next_bar()
        fills.extend(ex.pop_settled_fills(data.current_time()))
    return fills


# ---------- the price comes from the book ----------

def test_an_order_the_touch_absorbs_pays_exactly_one_tick():
    """1,000 shares rest at 100.01. A 900-share buy never leaves that level,
    so the fill is the level, not a basis-point formula."""
    data, ex = handler()
    fills = send(data, ex, 900)
    assert len(fills) == 1
    assert fills[0].fill_price == pytest.approx(to_tick(OPEN + TICK))
    assert ex.levels_walked == [1]


def test_a_bigger_order_walks_further_and_pays_the_average_of_what_it_took():
    """2,500 shares against 1,000 a level: 1,000 at .01, 1,000 at .02, 500 at
    .03. The volume-weighted price is 100.0180, which is arithmetic."""
    data, ex = handler()
    fills = send(data, ex, 2_500)
    expected = (1_000 * 100.01 + 1_000 * 100.02 + 500 * 100.03) / 2_500
    assert fills[0].fill_price == pytest.approx(expected, abs=1e-9)
    assert ex.levels_walked == [3]


def test_cost_per_share_rises_with_size():
    """The monotonicity is the whole economic claim. It is not imposed by any
    line of code here - it is what walking a sorted book does."""
    prices = []
    for qty in (500, 2_000, 5_000, 9_000):
        data, ex = handler()
        prices.append(send(data, ex, qty)[0].fill_price)
    assert prices == sorted(prices)
    assert prices[0] < prices[-1]


def test_selling_pays_the_same_cost_in_the_other_direction():
    """Slippage has to hurt both ways round, or a round trip is free."""
    data, buy = handler()
    bought = send(data, buy, 2_500)[0].fill_price
    data, sell = handler()
    sold = send(data, sell, -2_500)[0].fill_price
    assert bought - OPEN == pytest.approx(OPEN - sold, abs=1e-9)


# ---------- what it refuses to do ----------

def test_an_order_larger_than_the_book_fills_what_is_there_and_keeps_the_rest():
    """10 levels x 1,000 shares is 10,000 fillable. Asking for 25,000 fills
    10,000 and leaves 15,000 working - it does not invent a price for the
    remainder, and it does not drop it."""
    data, ex = handler(n=4)
    data.next_bar()
    ex.execute(OrderEvent(time=data.current_time(), symbol="AAA",
                          quantity=25_000))
    data.next_bar()
    fills = ex.pop_settled_fills(data.current_time())
    assert fills[0].quantity == 10_000
    assert ex.working_quantity("AAA") == 15_000
    assert ex.exhausted_slices == 1


def test_unfilled_shares_are_counted_rather_than_forgotten():
    data, ex = handler(n=3)
    data.next_bar()
    ex.execute(OrderEvent(time=data.current_time(), symbol="AAA",
                          quantity=25_000))
    data.next_bar()
    ex.pop_settled_fills(data.current_time())
    ex.finalize()
    assert ex.unfilled_shares == 15_000


def test_the_participation_cap_still_binds_before_the_book_does():
    """Participation and depth are separate limits and the tighter one wins.
    At a 1% cap on a million shares the slice is 10,000 - which the 10-level
    book could fill - so the cap is what is being tested."""
    data, ex = handler(participation=0.01, levels=100)
    data.next_bar()
    ex.execute(OrderEvent(time=data.current_time(), symbol="AAA",
                          quantity=50_000))
    data.next_bar()
    fills = ex.pop_settled_fills(data.current_time())
    assert fills[0].quantity == 10_000
    assert ex.working_quantity("AAA") == 40_000


def test_a_bar_with_no_volume_fills_nothing_and_loses_nothing():
    data, ex = handler(volume=0.0)
    data.next_bar()
    ex.execute(OrderEvent(time=data.current_time(), symbol="AAA", quantity=500))
    data.next_bar()
    assert ex.pop_settled_fills(data.current_time()) == []
    assert ex.working_quantity("AAA") == 500


def test_shares_are_conserved_across_however_many_bars_it_takes():
    data, ex = handler(n=20)
    data.next_bar()
    ex.execute(OrderEvent(time=data.current_time(), symbol="AAA",
                          quantity=25_000))
    filled = 0
    while data.has_more():
        data.next_bar()
        filled += sum(f.quantity for f in ex.pop_settled_fills(data.current_time()))
    ex.finalize()
    assert filled + ex.unfilled_shares == 25_000


def test_opposing_orders_net_instead_of_queueing():
    """An order management system cancels against a working order rather than
    stacking a contradiction behind it - same contract as the participation
    handler, and the engine relies on it when a strategy whipsaws."""
    data, ex = handler()
    data.next_bar()
    ex.execute(OrderEvent(time=data.current_time(), symbol="AAA", quantity=5_000))
    ex.execute(OrderEvent(time=data.current_time(), symbol="AAA", quantity=-5_000))
    assert ex.working_quantity("AAA") == 0
    data.next_bar()
    assert ex.pop_settled_fills(data.current_time()) == []


# ---------- fees ----------

def test_venue_fees_are_charged_on_top_and_every_order_is_a_taker():
    """Maker-taker charges 30 mils to cross. 900 shares is $2.70, on top of
    the $4.50 of broker commission."""
    data, ex = handler(fees=MAKER_TAKER)
    fills = send(data, ex, 900)
    assert ex.fee_paid == pytest.approx(-0.0030 * 900)
    assert fills[0].commission == pytest.approx(900 * 0.005 + 0.0030 * 900)


def test_with_no_fee_schedule_only_commission_is_charged():
    data, ex = handler()
    fills = send(data, ex, 900)
    assert ex.fee_paid == 0.0
    assert fills[0].commission == pytest.approx(900 * 0.005)


# ---------- against the handler it replaces ----------

def test_a_small_order_costs_less_here_than_the_flat_model_charges():
    """The headline of fill_realism.py, as an assertion. A flat 2 bps on a
    $100 stock is 2 cents; the touch costs 1. The formula handlers overcharge
    at small size, and that direction is as much a finding as the other one."""
    data, book = handler()
    book_price = send(data, book, 900)[0].fill_price

    data2 = make_handler_with_volumes({"AAA": [OPEN] * 12}, {"AAA": [OPEN] * 12},
                                       {"AAA": [VOLUME] * 12})
    flat = ParticipationLimitedExecutionHandler(data2, participation=1.0,
                                                slippage_bps=2.0)
    data2.next_bar()
    flat.execute(OrderEvent(time=data2.current_time(), symbol="AAA",
                            quantity=900))
    data2.next_bar()
    flat_price = flat.pop_settled_fills(data2.current_time())[0].fill_price

    assert book_price < flat_price


def test_it_drops_into_the_engine_like_any_other_handler():
    """The interface claim: Backtest.run() takes this handler without knowing
    anything about order books."""
    n = 60
    closes = [100.0 + 5 * np.sin(i / 6) + i * 0.1 for i in range(n)]
    data = make_handler_with_volumes({"AAA": closes}, {"AAA": closes},
                                      {"AAA": [1_000_000.0] * n})
    ex = BookExecutionHandler(data, participation=0.10)
    strategy = MovingAverageCrossStrategy(data, short_window=5, long_window=20)
    portfolio = Portfolio(data, initial_cash=100_000.0)
    equity = Backtest(data, strategy, portfolio, ex).run()
    ex.finalize()
    assert len(equity) == n
    assert equity.notna().all()
    assert ex.slices > 0


# ---------- the free parameter ----------

def test_deeper_books_cost_less_which_is_the_parameter_being_swept():
    data, thin = handler(depth_frac=0.0005)
    thin_price = send(data, thin, 2_500)[0].fill_price
    data, thick = handler(depth_frac=0.005)
    thick_price = send(data, thick, 2_500)[0].fill_price
    assert thick_price < thin_price


@pytest.mark.parametrize("bad", [
    {"participation": 0.0}, {"participation": 1.5},
    {"depth_frac": 0.0}, {"levels": 0}, {"shape": -0.5},
])
def test_nonsense_settings_are_refused_at_construction(bad):
    data = make_handler_with_volumes({"AAA": [OPEN]}, {"AAA": [OPEN]},
                                      {"AAA": [VOLUME]})
    with pytest.raises(ValueError):
        BookExecutionHandler(data, **bad)


# ---------- the shape of the book ----------
#
# `shape` moves resting size from the touch to the levels behind it, holding
# the total fixed. I expected that to tilt the cost curve - small orders
# dearer, large orders cheaper - and it does not: it raises cost at every
# size, because a fill price comes off CUMULATIVE depth and moving size
# backwards lowers the cumulative everywhere except the last level.
#
# So that one-sidedness is what these tests pin, since it is the property the
# parameter is safe because of. A depth knob that could be turned to make
# fills look cheaper would be slippage_bps again under a better name.


def test_a_flat_shape_leaves_every_level_the_same_size():
    """shape=0 reproduces the old flat book exactly, not approximately.

    Every test above this line is written against the flat book, so if this
    drifts those tests stop measuring what their names say.
    """
    w = BookExecutionHandler._depth_weights(levels=25, shape=0.0)
    assert np.allclose(w, 1.0)


@pytest.mark.parametrize("shape", [0.0, 0.3, 0.5, 1.0, 2.0])
def test_total_resting_size_does_not_depend_on_the_shape(shape):
    """The normalisation is the whole argument, so it gets its own test.

    If total depth moved with shape, a cost difference between two shapes
    could just be the book being bigger, and the parameter would be a second
    cost dial rather than a statement about where the liquidity sits.
    """
    levels = 40
    w = BookExecutionHandler._depth_weights(levels=levels, shape=shape)
    assert w.sum() == pytest.approx(levels)


def test_a_positive_shape_thins_the_touch_and_thickens_the_back():
    w = BookExecutionHandler._depth_weights(levels=20, shape=0.5)

    assert w[0] < 1.0               # thinner at the touch than a flat book
    assert w[-1] > 1.0              # thicker at the back
    assert np.all(np.diff(w) > 0)   # and monotone in between


@pytest.mark.parametrize("size", [300, 1_000, 2_500, 5_000, 7_500, 9_000])
def test_a_shaped_book_is_never_cheaper_than_a_flat_one(size):
    """The one-sidedness, across the whole range of sizes the book can fill.

    Both books hold the same 10,000 shares a side over the same ten levels.
    The shaped one reaches further for any of these orders, so it pays at
    least as much. This is the test that would catch someone "improving" the
    profile into something that quietly discounts fills.
    """
    d_flat, h_flat = handler(shape=0.0)
    d_tilt, h_tilt = handler(shape=1.0)
    p_flat = send(d_flat, h_flat, size)[0].fill_price
    p_tilt = send(d_tilt, h_tilt, size)[0].fill_price

    assert p_tilt >= p_flat


def test_the_two_books_agree_on_an_order_that_sweeps_the_whole_side():
    """Where the curves meet, which is the reason the total is normalised.

    An order big enough to take every resting share pays the average of all
    of them, and the two books hold the same shares at the same prices - only
    in different proportions. So the one order that touches all of it cannot
    tell them apart, give or take the per-level rounding.
    """
    d_flat, h_flat = handler(shape=0.0)
    d_tilt, h_tilt = handler(shape=1.0)
    f_flat = send(d_flat, h_flat, 10_000)[0]
    f_tilt = send(d_tilt, h_tilt, 10_000)[0]

    assert f_tilt.fill_price == pytest.approx(f_flat.fill_price, abs=2 * TICK)


def test_shaping_does_not_change_how_much_the_book_can_absorb():
    """Capacity is set by total depth, and total depth is held fixed.

    An order twice the size of the book is short by the same amount under
    either profile, so the shape cannot be used to claim the venue swallowed
    more than it did.
    """
    d_flat, h_flat = handler(shape=0.0)
    d_tilt, h_tilt = handler(shape=1.5)
    flat_filled = sum(f.quantity for f in send(d_flat, h_flat, 20_000))
    tilt_filled = sum(f.quantity for f in send(d_tilt, h_tilt, 20_000))

    # Within a share a level: the per-level rounding is the only difference.
    assert abs(flat_filled - tilt_filled) <= h_flat.levels


def test_a_book_too_thin_to_shape_still_prints_every_level():
    """The rounding floor, which is the one place the normalisation leaks.

    A heavy tilt on a thin book asks for less than a share at the touch.
    Rounding up to one share puts slightly more in the book than asked for;
    the alternative is an empty level, which would print a fill price that
    skipped a tick the order should have paid for. Being wrong by a share is
    the better side of that trade, and it is better stated than discovered.
    """
    data, ex = handler(volume=5_000.0, depth_frac=0.001, levels=10, shape=3.0)
    fills = send(data, ex, 3)

    assert fills, "a three-share order should fill against any live book"
    # The profile asks for a fraction of a share at the front levels, so each
    # holds exactly one. Three shares therefore walk three levels and pay the
    # average of the first three ticks - not one tick, which is what a book
    # with a real 5 shares at the touch would have printed.
    assert fills[0].quantity == 3
    assert fills[0].fill_price == pytest.approx(OPEN + 2 * TICK)
