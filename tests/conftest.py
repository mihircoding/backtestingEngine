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


def make_handler_with_opens(closes_dict, opens_dict):
    """Same as make_handler, but with a parallel {symbol: [opens]} frame so
    current_open() and NextBarOpenExecutionHandler have something to read."""
    lengths = {len(v) for v in closes_dict.values()} | {len(v) for v in opens_dict.values()}
    assert len(lengths) == 1, "all price lists must be equal length"
    n = lengths.pop()
    idx = pd.bdate_range("2023-01-02", periods=n)
    return HistoricalDataHandler(pd.DataFrame(closes_dict, index=idx),
                                  opens=pd.DataFrame(opens_dict, index=idx))


def make_handler_with_volumes(closes_dict, opens_dict, volumes_dict):
    """Closes, opens and per-bar share volume, for the participation-limited
    execution handler. It needs all three: opens to price a fill, closes to
    estimate the volatility its impact model scales with, and volumes to know
    how much of a bar an order is allowed to take."""
    lengths = ({len(v) for v in closes_dict.values()}
               | {len(v) for v in opens_dict.values()}
               | {len(v) for v in volumes_dict.values()})
    assert len(lengths) == 1, "all price lists must be equal length"
    n = lengths.pop()
    idx = pd.bdate_range("2023-01-02", periods=n)
    return HistoricalDataHandler(pd.DataFrame(closes_dict, index=idx),
                                  opens=pd.DataFrame(opens_dict, index=idx),
                                  volumes=pd.DataFrame(volumes_dict, index=idx))


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
