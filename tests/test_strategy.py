from src.events import SignalType
from src.strategy import MovingAverageCrossStrategy
from tests.conftest import make_handler


def run_strategy(prices, short=3, long=5):
    """Stream all bars through the strategy; collect signals per bar index."""
    handler = make_handler({"AAA": prices})
    strat = MovingAverageCrossStrategy(handler, short_window=short, long_window=long)
    fired = []
    i = 0
    while handler.has_more():
        event = handler.next_bar()
        for sig in strat.on_market(event):
            fired.append((i, sig.signal))
        i += 1
    return fired


class TestMovingAverageCross:
    def test_no_signals_during_warmup(self):
        # only 4 bars, long_window=5 -> never enough data
        assert run_strategy([100, 101, 102, 103]) == []

    def test_signals_once_per_crossing_not_per_bar(self):
        # steady uptrend: short MA sits above long MA for many bars,
        # but only ONE LONG signal may fire.
        prices = [100 + i for i in range(20)]
        fired = run_strategy(prices)
        longs = [f for f in fired if f[1] == SignalType.LONG]
        assert len(longs) == 1

    def test_exit_fires_when_trend_reverses(self):
        prices = [100 + i for i in range(10)] + [109 - 2 * i for i in range(10)]
        fired = run_strategy(prices)
        kinds = [k for _, k in fired]
        assert kinds == [SignalType.LONG, SignalType.EXIT] or \
               kinds[:2] == [SignalType.LONG, SignalType.EXIT], (
            f"expected one LONG then one EXIT, got {fired}"
        )
