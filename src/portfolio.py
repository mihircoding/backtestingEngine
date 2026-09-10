"""Position/cash accounting, order sizing, and the equity curve.

Verify with:  pytest tests/test_portfolio.py tests/test_vol_target.py

State maintained here:
    cash      : float, starts at initial_cash
    positions : dict symbol -> signed share count (missing key == 0)
    equity    : list of (time, total_equity) recorded once per bar

Two sizing schemes live here, and only here. Fixed size (the default) sends
`trade_size` shares regardless of anything. Volatility targeting sizes the
position so the *risk* is constant instead of the share count. Both are
reached through the same on_signal() entry point, because a strategy is not
allowed to know or care which one is in use.
"""

import numpy as np

from .data_handler import HistoricalDataHandler
from .events import OrderEvent, SignalEvent, SignalType

TRADING_DAYS = 252


class Portfolio:
    """Turns signals into orders and keeps the books.

    vol_target: annualized volatility to aim the *portfolio* at, e.g. 0.10 for
        10%. None (the default) keeps the old fixed-share behaviour, so every
        existing test and every existing number in RESULTS.md is unchanged.
    vol_window: how many daily returns the trailing volatility estimate uses.
        20 bars is about a month — short enough to react to a regime change,
        long enough that a single move doesn't dominate the estimate.
    max_leverage: hard ceiling on position notional as a multiple of equity.
        Vol targeting divides by a volatility estimate, and dividing by a
        small number is how a sizing rule quietly ends up 20x levered in a
        calm market. The cap is the difference between a risk model and a
        margin call.
    rebalance_band: how far a live position may drift from its target, as a
        fraction of the target, before it gets resized. Zero would resize
        every bar and pay commission for a one-share correction; 0.2 means
        "only when it's 20% wrong."
    """

    def __init__(self, data: HistoricalDataHandler, initial_cash: float = 100_000.0,
                 trade_size: int = 100, vol_target: float | None = None,
                 vol_window: int = 20, max_leverage: float = 3.0,
                 rebalance_band: float = 0.2):
        self.data = data
        self.initial_cash = initial_cash
        self.trade_size = trade_size
        self.vol_target = vol_target
        self.vol_window = vol_window
        self.max_leverage = max_leverage
        self.rebalance_band = rebalance_band
        self.cash = initial_cash
        self.positions: dict[str, int] = {}
        self.equity_history: list[tuple] = []
        self.n_fills = 0  # how much trading the book actually did

    # direction of the target position for each signal type; the magnitude
    # comes from _size_for(), which is where the two schemes differ
    _TARGETS = {
        SignalType.LONG: 1,
        SignalType.SHORT: -1,
        SignalType.EXIT: 0,
    }

    # ---------- sizing ----------

    def trailing_vol(self, symbol: str) -> float | None:
        """Annualized standard deviation of the last `vol_window` daily returns.

        Returns None during warmup, or if the window is flat (a constant price
        series has zero measured risk, which the sizing rule must not believe).
        Only released bars are visible — this reads through get_latest(), so it
        cannot see the future any more than a strategy can.
        """
        window = self.data.get_latest(symbol, self.vol_window + 1)
        if len(window) < self.vol_window + 1:
            return None

        daily = np.diff(np.log(window.to_numpy(dtype=float)))
        vol = float(daily.std(ddof=1) * np.sqrt(TRADING_DAYS))
        return vol if vol > 0 else None

    def _size_for(self, symbol: str) -> int:
        """How many shares one unit of exposure is worth right now.

        Fixed scheme: `trade_size`, always.

        Vol-targeted scheme: solve for the share count whose annualized
        volatility equals the target,

            shares x price x asset_vol  =  equity x vol_target

        so a name realizing 30% vol gets a third the notional of one realizing
        10%, and the same signal risks the same amount of the book either way.
        Falls back to the fixed size during warmup, when there is not yet
        enough history to estimate a volatility from.
        """
        if self.vol_target is None:
            return self.trade_size

        vol = self.trailing_vol(symbol)
        if vol is None:
            return self.trade_size

        price = self.data.current_price(symbol)
        equity = self.total_equity()
        shares = equity * self.vol_target / (vol * price)

        cap = self.max_leverage * equity / price
        return int(min(shares, cap))

    def on_signal(self, signal: SignalEvent) -> OrderEvent | None:
        """Turn an opinion into a sized order (or None).

        The order is for the DIFFERENCE between target and current position, so
        a SHORT signal while long correctly emits a double-size sell.
        """
        target = self._TARGETS[signal.signal] * self._size_for(signal.symbol)
        current = self.positions.get(signal.symbol, 0)
        delta = target - current

        if delta == 0:
            return None  # already where we want to be; don't spam the book

        return OrderEvent(time=signal.time, symbol=signal.symbol, quantity=delta)

    def on_market(self, time) -> list[OrderEvent]:
        """Resize live positions toward the risk target. Empty list if off.

        Sizing at entry is only half of volatility targeting. A position put
        on at 10% risk in a calm market is running well above 10% two months
        later if the name has since started moving — the target is a property
        of the position over its whole life, not of the moment it was opened.
        So this runs once a bar, on positions that already exist, and never
        opens or reverses one: the direction of a position remains entirely
        the strategy's business.

        The engine calls this after the bar's signals, orders and fills have
        settled, so the position being measured is the real one.
        """
        if self.vol_target is None:
            return []

        orders = []
        for symbol, shares in list(self.positions.items()):
            if shares == 0:
                continue

            target = self._size_for(symbol) * (1 if shares > 0 else -1)
            if target == 0:
                continue

            delta = target - shares
            if abs(delta) <= self.rebalance_band * abs(target):
                continue  # close enough; not worth the commission

            orders.append(OrderEvent(time=time, symbol=symbol, quantity=delta))

        return orders

    # ---------- accounting ----------

    def on_fill(self, fill) -> None:
        """Update cash and positions from a FillEvent.

        The signed quantity handles buys and sells in one expression. No P&L is
        tracked here on purpose — equity is derived from cash + marked positions,
        which cannot drift out of sync with the two things it's derived from.
        """
        self.cash -= fill.quantity * fill.fill_price
        self.cash -= fill.commission
        self.positions[fill.symbol] = self.positions.get(fill.symbol, 0) + fill.quantity
        self.n_fills += 1

    def total_equity(self) -> float:
        """Cash plus every position marked at the latest released close."""
        marked = sum(
            shares * self.data.current_price(symbol)
            for symbol, shares in self.positions.items()
            if shares != 0
        )
        return self.cash + marked

    def mark_to_market(self, time) -> None:
        """Record one equity point for this bar.

        The engine calls this AFTER all of the bar's events are processed. Called
        before fills, the equity curve lags reality by a bar — the end-to-end
        test checks for exactly that bug.
        """
        self.equity_history.append((time, self.total_equity()))
