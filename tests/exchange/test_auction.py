"""The cross, tested as scenarios rather than as functions.

Two things are easy to get wrong in an auction and expensive to get wrong
quietly. The first is the tie-break ladder: on a tick grid a whole range of
prices frequently trades the same number of shares, and which one a venue
publishes is a rule, not an opinion. The second is the allocation - everyone
better than the clearing price is filled, and the rationing happens only at
the clearing price itself, which is exactly backwards from how the
continuous book works.
"""
import pytest

from exchange.auction import (AuctionOrder, book_from_levels, candidate_prices,
                         indicative, uncross)
from exchange.order import Side


def order(side, qty, price=None, seq=0):
    return AuctionOrder(side, qty, price, seq)


def test_clearing_price_maximizes_volume():
    # 100 shares can cross at 10.00 and 300 at 10.02 - volume wins, even
    # though 10.00 is closer to the reference.
    orders = [order(Side.BUY, 300, 10.02, 0),
              order(Side.SELL, 100, 9.98, 1),
              order(Side.SELL, 300, 10.02, 2)]
    r = uncross(orders, reference_price=10.00)
    assert r.price == 10.02
    assert r.volume == 300
    assert r.reason == "maximum volume"


def test_no_overlap_means_no_trade():
    orders = [order(Side.BUY, 100, 9.99, 0), order(Side.SELL, 100, 10.01, 1)]
    r = uncross(orders, reference_price=10.00)
    assert r.price is None and r.volume == 0


def test_everyone_prints_at_one_price():
    """The defining property. A buyer bidding 10.10 and one bidding 10.05
    both pay the clearing price - the better bid buys priority, not a
    better price."""
    orders = [order(Side.BUY, 100, 10.10, 0),
              order(Side.BUY, 100, 10.05, 1),
              order(Side.SELL, 200, 10.05, 2)]
    r = uncross(orders, reference_price=10.00)
    assert r.price == 10.05
    assert r.volume == 200
    assert all(q > 0 for _, q in r.fills)


def test_better_prices_fill_first_and_the_margin_is_rationed():
    """300 shares of demand at 10.00, 200 of it from someone bidding above.
    The aggressive bid is whole; the marginal one takes what is left."""
    orders = [order(Side.BUY, 200, 10.05, 0),
              order(Side.BUY, 200, 10.00, 1),
              order(Side.SELL, 300, 10.00, 2)]
    r = uncross(orders, reference_price=10.00)
    assert r.price == 10.00 and r.volume == 300
    filled = {(o.side, o.price): q for o, q in r.fills}
    assert filled[(Side.BUY, 10.05)] == 200
    assert filled[(Side.BUY, 10.00)] == 100


def test_at_the_margin_it_is_first_come_first_served():
    orders = [order(Side.BUY, 100, 10.00, seq=0),
              order(Side.BUY, 100, 10.00, seq=1),
              order(Side.SELL, 100, 10.00, seq=2)]
    r = uncross(orders, reference_price=10.00)
    by_seq = {o.sequence: q for o, q in r.fills}
    assert by_seq[0] == 100
    assert 1 not in by_seq


def test_market_orders_trade_at_any_price():
    orders = [order(Side.BUY, 500), order(Side.SELL, 500, 10.20, 1)]
    r = uncross(orders, reference_price=10.00)
    assert r.price == 10.20 and r.volume == 500


def test_market_orders_on_both_sides_fall_back_to_the_reference():
    """No limit price anywhere means nothing in the book says what the
    shares are worth. A venue leans on the previous close."""
    orders = [order(Side.BUY, 100), order(Side.SELL, 100, seq=1)]
    r = uncross(orders, reference_price=42.00)
    assert r.price == 42.00 and r.volume == 100
    assert uncross(orders, reference_price=None).price is None


def test_a_volume_tie_is_broken_by_the_smaller_imbalance():
    """Three prices cross 200 shares. Two of them leave 200 unfilled and one
    leaves 500, so the big one is discarded before anything else is asked."""
    orders = [order(Side.BUY, 200, 10.02, 0),
              order(Side.BUY, 200, 10.00, 1),
              order(Side.SELL, 200, 9.98, 2),
              order(Side.SELL, 500, 10.02, 3)]
    r = uncross(orders, reference_price=9.98)
    assert r.volume == 200
    # Of the two survivors, both leave buyers unfilled, so the price goes up.
    assert r.price == 10.00
    assert r.reason == "buy-side imbalance"


def test_a_symmetric_tie_falls_through_to_the_reference_price():
    """Equal volume, equal imbalance, opposite sides: the book itself has
    run out of opinions and the previous close decides."""
    orders = [order(Side.BUY, 100, 10.02, 0),
              order(Side.BUY, 400, 10.00, 1),
              order(Side.SELL, 100, 10.00, 2),
              order(Side.SELL, 400, 10.02, 3)]
    assert uncross(orders, reference_price=10.02).price == 10.02
    assert uncross(orders, reference_price=10.00).price == 10.00
    assert uncross(orders, reference_price=10.02).reason == "closest to reference"


def test_imbalance_side_points_where_the_price_should_go():
    """Unfilled buyers at the cross price mean the price is too low for the
    demand that showed up - which is the information the pre-open feed is
    publishing."""
    orders = [order(Side.BUY, 1_000, 10.00, 0), order(Side.SELL, 100, 10.00, 1)]
    r = uncross(orders, reference_price=10.00)
    assert r.volume == 100
    assert r.imbalance == 900 and r.imbalance_side is Side.BUY


def test_candidate_prices_are_the_limits_and_nothing_else():
    orders = [order(Side.BUY, 100, 10.00, 0), order(Side.SELL, 100, 10.05, 1),
              order(Side.BUY, 100)]
    assert candidate_prices(orders) == [10.00, 10.05]


def test_indicative_reports_the_same_cross_without_executing():
    orders = [order(Side.BUY, 300, 10.02, 0), order(Side.SELL, 300, 10.00, 1)]
    ind = indicative(orders, reference_price=10.00)
    full = uncross(orders, reference_price=10.00)
    assert ind["price"] == full.price and ind["volume"] == full.volume


def test_both_sides_cross_the_same_number_of_shares():
    orders = book_from_levels([(9.99, 500), (9.98, 700)],
                              [(9.97, 400), (9.99, 900)])
    r = uncross(orders, reference_price=9.98)
    bought = sum(q for o, q in r.fills if o.side is Side.BUY)
    sold = sum(q for o, q in r.fills if o.side is Side.SELL)
    assert bought == sold == r.volume


def test_no_order_is_overfilled():
    orders = book_from_levels([(10.02, 300), (10.00, 800)],
                              [(9.99, 250), (10.01, 650)])
    r = uncross(orders, reference_price=10.00)
    for o, q in r.fills:
        assert 0 < q <= o.quantity


def test_quantity_must_be_positive():
    with pytest.raises(ValueError):
        AuctionOrder(Side.BUY, 0, 10.00)
