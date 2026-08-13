import pytest

from src.events import FillEvent, SignalEvent, SignalType
from src.portfolio import Portfolio
from tests.conftest import make_handler


def make_portfolio(prices=None, cash=100_000.0, trade_size=100):
    handler = make_handler(prices or {"AAA": [100.0, 110.0]})
    handler.next_bar()
    return handler, Portfolio(handler, initial_cash=cash, trade_size=trade_size)


class TestSizing:
    def test_long_signal_from_flat(self):
        h, p = make_portfolio()
        order = p.on_signal(SignalEvent(h.current_time(), "AAA", SignalType.LONG))
        assert order.quantity == 100

    def test_exit_signal_closes_position(self):
        h, p = make_portfolio()
        p.positions["AAA"] = 100
        order = p.on_signal(SignalEvent(h.current_time(), "AAA", SignalType.EXIT))
        assert order.quantity == -100

    def test_short_from_long_reverses(self):
        h, p = make_portfolio()
        p.positions["AAA"] = 100
        order = p.on_signal(SignalEvent(h.current_time(), "AAA", SignalType.SHORT))
        assert order.quantity == -200  # +100 -> -100

    def test_redundant_signal_emits_nothing(self):
        h, p = make_portfolio()
        p.positions["AAA"] = 100
        assert p.on_signal(SignalEvent(h.current_time(), "AAA", SignalType.LONG)) is None


class TestAccounting:
    def test_buy_fill_moves_cash_and_position(self):
        h, p = make_portfolio(cash=100_000.0)
        p.on_fill(FillEvent(h.current_time(), "AAA", 100, 100.0, commission=1.0))
        assert p.cash == pytest.approx(100_000.0 - 100 * 100.0 - 1.0)
        assert p.positions["AAA"] == 100

    def test_sell_fill_adds_cash(self):
        h, p = make_portfolio(cash=100_000.0)
        p.positions["AAA"] = 100
        p.on_fill(FillEvent(h.current_time(), "AAA", -100, 110.0, commission=1.0))
        assert p.cash == pytest.approx(100_000.0 + 100 * 110.0 - 1.0)
        assert p.positions["AAA"] == 0

    def test_equity_marks_to_market(self):
        h, p = make_portfolio(cash=100_000.0)
        p.on_fill(FillEvent(h.current_time(), "AAA", 100, 100.0, commission=0.0))
        h.next_bar()  # price moves 100 -> 110
        # cash 90,000 + 100 shares * 110 = 101,000
        assert p.total_equity() == pytest.approx(101_000.0)

    def test_mark_to_market_records_history(self):
        h, p = make_portfolio()
        t = h.current_time()
        p.mark_to_market(t)
        assert p.equity_history == [(t, pytest.approx(100_000.0))]
