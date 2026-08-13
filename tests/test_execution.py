import pytest

from src.events import OrderEvent
from src.execution import SimulatedExecutionHandler
from tests.conftest import make_handler


def setup_exec(slippage_bps=10.0, commission=0.01):
    handler = make_handler({"AAA": [100.0, 200.0]})
    handler.next_bar()  # current price = 100.0
    return handler, SimulatedExecutionHandler(handler, slippage_bps, commission)


class TestExecution:
    def test_buy_fills_above_close(self):
        handler, ex = setup_exec(slippage_bps=10.0)
        order = OrderEvent(handler.current_time(), "AAA", 100)
        fill = ex.execute(order)
        # 100 * (1 + 10bp) = 100.10
        assert fill.fill_price == pytest.approx(100.10)
        assert fill.quantity == 100

    def test_sell_fills_below_close(self):
        handler, ex = setup_exec(slippage_bps=10.0)
        fill = ex.execute(OrderEvent(handler.current_time(), "AAA", -100))
        assert fill.fill_price == pytest.approx(99.90)

    def test_commission_scales_with_size(self):
        handler, ex = setup_exec(commission=0.01)
        fill = ex.execute(OrderEvent(handler.current_time(), "AAA", -250))
        assert fill.commission == pytest.approx(2.50)  # abs(-250) * 0.01

    def test_uses_current_bar_not_future(self):
        """Only bar 1 (price 100) has been released; the 200 bar must be invisible."""
        handler, ex = setup_exec(slippage_bps=0.0)
        fill = ex.execute(OrderEvent(handler.current_time(), "AAA", 10))
        assert fill.fill_price == pytest.approx(100.0)
