"""Section 11: the cancel rate as a dial, and a cancel list that stays clean.

Two things were added to simulate() and both default to the flow every
earlier section ran on, so the first tests are about that: the default has
to be the old flow to the event. Then the dial, then the cleaning.
"""

import pytest

from exchange.orderbook import LimitOrderBook
from exchange.simulator import (P_CANCEL, P_LIMIT, P_MARKET, event_mix,
                                seed_book, simulate)
from cancel_rate_study import crossing_rate


def fresh():
    book = LimitOrderBook()
    seed_book(book, mid=100.0, levels=20, qty=200)
    return book


def test_the_default_mix_is_the_flow_every_section_ran_on():
    assert event_mix() == (P_LIMIT, P_MARKET, P_CANCEL)
    assert event_mix(P_CANCEL) == (P_LIMIT, P_MARKET, P_CANCEL)


def test_a_higher_cancel_share_keeps_makers_and_takers_in_proportion():
    limit, market, cancel = event_mix(0.45)
    assert cancel == 0.45
    assert limit + market + cancel == pytest.approx(1.0)
    assert limit / market == pytest.approx(P_LIMIT / P_MARKET)


def test_the_mix_rejects_a_share_that_is_not_one():
    with pytest.raises(ValueError):
        event_mix(1.0)


def test_passing_the_flows_own_rate_is_the_old_flow_to_the_event():
    a = simulate(fresh(), n_events=3_000, seed=5)
    b = simulate(fresh(), n_events=3_000, seed=5, p_cancel=P_CANCEL)
    assert a["mids"] == b["mids"]
    assert a["n_trades"] == b["n_trades"]


def test_more_cancels_means_fewer_trades_and_a_wider_spread():
    low = simulate(fresh(), n_events=5_000, seed=3, p_cancel=0.15)
    high = simulate(fresh(), n_events=5_000, seed=3, p_cancel=0.45)
    assert high["cancel_events"] > 2 * low["cancel_events"]
    assert high["n_trades"] < low["n_trades"]
    assert sum(high["spreads"]) / len(high["spreads"]) \
        > sum(low["spreads"]) / len(low["spreads"])


def test_a_clean_list_reaches_a_live_order_with_every_cancel():
    book = fresh()
    res = simulate(book, n_events=5_000, seed=1, clean_stale=True)
    assert res["live_cancels"] == res["cancel_events"]
    # and the book is thinner for it: the stale list was holding the hump up
    dirty = simulate(fresh(), n_events=5_000, seed=1)
    assert dirty["live_cancels"] < dirty["cancel_events"]


def test_cleaning_the_list_does_not_change_which_events_happen():
    """Same seed, same draws: the cleaning changes what a cancel reaches,
    not whether the next event is a limit, a market or a cancel."""
    a = simulate(fresh(), n_events=2_000, seed=9)
    b = simulate(fresh(), n_events=2_000, seed=9, clean_stale=True)
    assert a["cancel_events"] == b["cancel_events"]
    assert len(a["impacts"]) == len(b["impacts"])     # the same market orders


def test_the_crossing_rate_interpolates_between_the_rates_that_straddle_one():
    points = [(0.15, 3.0), (0.25, 2.0), (0.35, 0.5), (0.45, 0.2)]
    assert crossing_rate(points) == pytest.approx(0.25 + (1.0 - 2.0) / (0.5 - 2.0) * 0.10)
    assert crossing_rate([(0.15, 3.0), (0.45, 2.0)]) is None
