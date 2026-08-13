import numpy as np
import pandas as pd
import pytest

from src.data_handler import HistoricalDataHandler


def make_handler(prices_dict, periods=None):
    """Build a data handler from {symbol: [prices]} with a business-day index."""
    lengths = {len(v) for v in prices_dict.values()}
    assert len(lengths) == 1, "all price lists must be equal length"
    n = lengths.pop()
    idx = pd.bdate_range("2023-01-02", periods=n)
    return HistoricalDataHandler(pd.DataFrame(prices_dict, index=idx))


@pytest.fixture
def trending_handler():
    """One symbol, price rising 100 -> 129 by $1/day."""
    return make_handler({"AAA": [100.0 + i for i in range(30)]})


def release_all(handler):
    """Stream every bar (so current_price is the last bar)."""
    events = []
    while handler.has_more():
        events.append(handler.next_bar())
    return events
