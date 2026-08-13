"""Event types flowing through the engine.

Frozen dataclasses: events are messages, and messages shouldn't mutate after
they're sent. Each carries the minimum information its consumer needs.
"""

from dataclasses import dataclass
from enum import Enum

import pandas as pd


class SignalType(Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    EXIT = "EXIT"


@dataclass(frozen=True)
class MarketEvent:
    """A new bar has arrived."""

    time: pd.Timestamp


@dataclass(frozen=True)
class SignalEvent:
    """A strategy's directional opinion on one symbol. No sizing — that's the
    portfolio's job."""

    time: pd.Timestamp
    symbol: str
    signal: SignalType


@dataclass(frozen=True)
class OrderEvent:
    """A sized instruction to trade. quantity is signed: +buy, -sell."""

    time: pd.Timestamp
    symbol: str
    quantity: int


@dataclass(frozen=True)
class FillEvent:
    """What actually executed. fill_price includes slippage; commission is the
    total cash charge for this fill."""

    time: pd.Timestamp
    symbol: str
    quantity: int
    fill_price: float
    commission: float
