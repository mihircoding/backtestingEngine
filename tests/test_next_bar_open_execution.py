"""NextBarOpenExecutionHandler: fills at the NEXT bar's open, not the same
bar's close. See src/execution.py for why this exists.
"""

import pytest

from src.events import OrderEvent
from src.execution import NextBarOpenExecutionHandler
from tests.conftest import make_handler_with_opens


def setup(slippage_bps=10.0, commission=0.01):
    # Bar 1 close 100 / open 99.5. Bar 2 close 103 / open 101.
    handler = make_handler_with_opens(
        closes_dict={"AAA": [100.0, 103.0]},
        opens_dict={"AAA": [99.5, 101.0]},
    )
    handler.next_bar()  # release bar 1: current close 100.0, current open 99.5
    return handler, NextBarOpenExecutionHandler(handler, slippage_bps, commission)


class TestNextBarOpenExecution:
    def test_execute_does_not_fill_immediately(self):
        """execute() only queues the order — unlike SimulatedExecutionHandler,
        it never itself returns a FillEvent. The engine is what defers the
        pop_settled_fills() call to the following bar (see test below and
        engine.py); this test just pins execute()'s own contract."""
        handler, ex = setup()
        order = OrderEvent(handler.current_time(), "AAA", 100)
        result = ex.execute(order)
        assert result is None
        assert ex._pending == [order]

    def test_fill_uses_next_bars_open_not_close(self):
        handler, ex = setup(slippage_bps=0.0)
        ex.execute(OrderEvent(handler.current_time(), "AAA", 100))

        handler.next_bar()  # release bar 2: open 101.0, close 103.0
        fills = ex.pop_settled_fills(handler.current_time())

        assert len(fills) == 1
        # Must price off the OPEN (101.0), never the close (103.0).
        assert fills[0].fill_price == pytest.approx(101.0)

    def test_slippage_direction_matches_same_bar_handler(self):
        handler, ex = setup(slippage_bps=10.0)  # 10bp of the 101.0 open = 0.101
        ex.execute(OrderEvent(handler.current_time(), "AAA", 100))
        handler.next_bar()
        buy_fill = ex.pop_settled_fills(handler.current_time())[0]
        assert buy_fill.fill_price == pytest.approx(101.101)

    def test_sell_fills_below_open(self):
        handler, ex = setup(slippage_bps=10.0)
        ex.execute(OrderEvent(handler.current_time(), "AAA", -50))
        handler.next_bar()
        sell_fill = ex.pop_settled_fills(handler.current_time())[0]
        assert sell_fill.fill_price == pytest.approx(101.0 - 0.101)
        assert sell_fill.quantity == -50

    def test_commission_uses_same_formula_as_same_bar_handler(self):
        handler, ex = setup(commission=0.02)
        ex.execute(OrderEvent(handler.current_time(), "AAA", -250))
        handler.next_bar()
        fill = ex.pop_settled_fills(handler.current_time())[0]
        assert fill.commission == pytest.approx(5.00)  # abs(-250) * 0.02

    def test_fill_is_stamped_with_the_settlement_bar_time_not_order_time(self):
        handler, ex = setup()
        order_time = handler.current_time()
        ex.execute(OrderEvent(order_time, "AAA", 10))
        handler.next_bar()
        settle_time = handler.current_time()
        fill = ex.pop_settled_fills(settle_time)[0]

        assert settle_time != order_time
        assert fill.time == settle_time

    def test_multiple_pending_orders_all_settle_together(self):
        handler = make_handler_with_opens(
            closes_dict={"AAA": [100.0, 103.0], "BBB": [50.0, 52.0]},
            opens_dict={"AAA": [99.5, 101.0], "BBB": [49.0, 51.0]},
        )
        handler.next_bar()
        ex = NextBarOpenExecutionHandler(handler, slippage_bps=0.0, commission_per_share=0.0)
        ex.execute(OrderEvent(handler.current_time(), "AAA", 10))
        ex.execute(OrderEvent(handler.current_time(), "BBB", -5))

        handler.next_bar()
        fills = ex.pop_settled_fills(handler.current_time())

        assert {f.symbol for f in fills} == {"AAA", "BBB"}
        assert next(f for f in fills if f.symbol == "AAA").fill_price == pytest.approx(101.0)
        assert next(f for f in fills if f.symbol == "BBB").fill_price == pytest.approx(51.0)

    def test_order_placed_on_the_final_bar_never_fills(self):
        """Documented edge case: there is no 'next bar' after the last one, so
        an order queued there is dropped rather than filled at a fabricated
        price. A caller that cares should check for pending orders after
        run() — this test exists so that silent behavior stays intentional."""
        handler, ex = setup()
        handler.next_bar()  # now on the final bar
        ex.execute(OrderEvent(handler.current_time(), "AAA", 10))

        assert len(ex._pending) == 1
        assert not handler.has_more()  # confirms this really is the last bar

    def test_requires_a_handler_with_open_data(self):
        from tests.conftest import make_handler

        handler = make_handler({"AAA": [100.0, 103.0]})
        handler.next_bar()
        ex = NextBarOpenExecutionHandler(handler)
        ex.execute(OrderEvent(handler.current_time(), "AAA", 10))
        handler.next_bar()

        with pytest.raises(ValueError, match="no open-price data"):
            ex.pop_settled_fills(handler.current_time())
