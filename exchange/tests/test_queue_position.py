"""Time priority, and what the experiment that prices it is allowed to assume.

Two things are being tested and they are different kinds of thing. queue_ahead()
is arithmetic and has right answers, including in the awkward cases the lazy
cancel creates - a tombstone sitting in the middle of a deque must not be
counted as somebody in front of you. The experiment harness has no right
answers, so what is checked there is that its controls do what they claim:
that the shares we asked to be ahead really are ahead, that the order we said
not to cancel is not cancelled, and that a fill is attributed to the order that
supplied it and not to whatever else printed on the same event.
"""
import pytest

from src.order import Side
from src.orderbook import LimitOrderBook
from src.queue_position import (MakerOutcome, rest_one_order, warm_book,
                                bucket)
from src.simulator import TICK, seed_book, simulate


def test_queue_ahead_counts_only_what_is_in_front():
    book = LimitOrderBook()
    first, _ = book.add_limit_order(Side.BUY, 100.00, 300)
    second, _ = book.add_limit_order(Side.BUY, 100.00, 150)
    third, _ = book.add_limit_order(Side.BUY, 100.00, 75)

    assert book.queue_ahead(first) == 0
    assert book.queue_ahead(second) == 300
    assert book.queue_ahead(third) == 450


def test_orders_at_other_prices_are_not_in_your_queue():
    book = LimitOrderBook()
    book.add_limit_order(Side.BUY, 100.01, 5_000)     # better price, own queue
    book.add_limit_order(Side.BUY, 99.99, 5_000)      # worse price, own queue
    mine, _ = book.add_limit_order(Side.BUY, 100.00, 100)
    assert book.queue_ahead(mine) == 0


def test_a_cancelled_order_stops_being_ahead_of_you():
    book = LimitOrderBook()
    ahead, _ = book.add_limit_order(Side.BUY, 100.00, 400)
    mine, _ = book.add_limit_order(Side.BUY, 100.00, 100)
    assert book.queue_ahead(mine) == 400

    # cancel() is lazy: the object stays in the deque as a tombstone. The count
    # has to skip it anyway, or every experiment overstates the queue.
    book.cancel(ahead)
    assert book.queue_ahead(mine) == 0


def test_a_partial_fill_shrinks_the_queue_by_what_traded():
    book = LimitOrderBook()
    book.add_limit_order(Side.SELL, 100.00, 400)
    mine, _ = book.add_limit_order(Side.SELL, 100.00, 100)
    assert book.queue_ahead(mine) == 400

    book.market_order(Side.BUY, 250)
    assert book.queue_ahead(mine) == 150


def test_queue_ahead_is_none_once_the_order_is_gone():
    book = LimitOrderBook()
    mine, _ = book.add_limit_order(Side.BUY, 100.00, 100)
    assert book.queue_ahead(999_999) is None
    book.cancel(mine)
    assert book.queue_ahead(mine) is None


def test_protected_ids_are_not_cancelled_by_the_random_flow():
    book = LimitOrderBook()
    seed_book(book, mid=100.0, levels=5, qty=200)
    mine, _ = book.add_limit_order(Side.BUY, 99.90, 100, participant_id="maker")

    simulate(book, n_events=3_000, seed=3, protected_ids=[mine])
    order = book._by_id.get(mine)
    # It may have filled, which removes it. What must not happen is a cancel:
    # a cancelled order is flagged inactive and left behind.
    assert order is None or order.active


def test_the_hook_sees_every_event_in_order():
    book = LimitOrderBook()
    seed_book(book, mid=100.0, levels=5, qty=200)
    seen = []
    simulate(book, n_events=500, seed=1, hook=lambda i, b, t: seen.append(i))
    assert seen == list(range(500))


def test_the_hook_does_not_change_the_simulation():
    def run(hook):
        book = LimitOrderBook()
        seed_book(book, mid=100.0, levels=5, qty=200)
        return simulate(book, n_events=2_000, seed=7, hook=hook)

    plain = run(None)
    watched = run(lambda i, b, t: None)
    assert plain["n_trades"] == watched["n_trades"]
    assert plain["volume"] == watched["volume"]
    assert plain["mids"] == watched["mids"]


def test_padding_puts_exactly_that_many_shares_in_front():
    outcome = rest_one_order(warm_book(4), improve_ticks=1, pad=1_000,
                             horizon=50, seed=4)
    if outcome is None:
        pytest.skip("that book's spread was one tick wide; nothing to improve")
    # improve_ticks=1 creates a brand-new price level, so the only thing in
    # front of us is the padding we put there.
    assert outcome.natural_ahead == 0
    assert outcome.ahead == 1_000


def test_joining_the_touch_sits_behind_what_is_already_resting():
    outcome = rest_one_order(warm_book(2), improve_ticks=0, pad=0,
                             horizon=50, seed=2)
    assert outcome is not None
    assert outcome.ahead == outcome.natural_ahead > 0


def test_an_order_that_never_fills_reports_no_edge_and_no_markout():
    o = MakerOutcome(price=100.0, ahead=10_000, natural_ahead=10_000, size=100,
                     filled=0, fill_event=None, mid_at_entry=100.01,
                     mid_at_fill=None, mid_after=None)
    assert o.fill_ratio == 0.0
    assert o.edge_ticks is None and o.markout_ticks is None
    assert o.expected_ticks() == 0.0


def test_expected_ticks_weights_the_markout_by_the_fill():
    half = MakerOutcome(price=100.0, ahead=0, natural_ahead=0, size=100,
                        filled=50, fill_event=10, mid_at_entry=100.0,
                        mid_at_fill=100.01, mid_after=100.02)
    full = MakerOutcome(price=100.0, ahead=0, natural_ahead=0, size=100,
                        filled=100, fill_event=10, mid_at_entry=100.0,
                        mid_at_fill=100.01, mid_after=100.02)
    assert full.markout_ticks == pytest.approx(2.0)
    assert full.expected_ticks() == pytest.approx(2.0)
    assert half.expected_ticks() == pytest.approx(1.0)


def test_adverse_selection_is_markout_minus_edge():
    o = MakerOutcome(price=100.0, ahead=0, natural_ahead=0, size=100,
                     filled=100, fill_event=5, mid_at_entry=100.0,
                     mid_at_fill=100.02, mid_after=99.99)
    assert o.edge_ticks == pytest.approx(2.0)
    assert o.markout_ticks == pytest.approx(-1.0)
    assert o.adverse_ticks == pytest.approx(o.markout_ticks - o.edge_ticks)


def test_a_rebate_only_counts_on_the_shares_that_filled():
    from src.fees import MAKER_TAKER
    o = MakerOutcome(price=100.0, ahead=0, natural_ahead=0, size=100,
                     filled=50, fill_event=1, mid_at_entry=100.0,
                     mid_at_fill=100.0, mid_after=100.0)
    # markout is zero, so everything left is half of the rebate in ticks
    assert o.expected_ticks(MAKER_TAKER) == pytest.approx(
        0.5 * MAKER_TAKER.maker / TICK)


def test_buckets_keep_every_outcome_exactly_once():
    outcomes = [
        MakerOutcome(100.0, ahead, ahead, 100, 100, 5, 100.0, 100.01, 100.01)
        for ahead in (0, 150, 900, 3_000, 12_000, 50_000)
    ]
    rows = bucket(outcomes)
    assert sum(r["n"] for r in rows) == len(outcomes)
    assert all(r["fill_rate"] == 1.0 for r in rows)
