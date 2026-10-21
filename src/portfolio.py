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
    short_borrow_bps: annualized cost, in basis points of notional, of holding
        a short position. Charged daily against cash for every negative
        position, pro rata at bps/10,000/252 per bar. Zero (the default) is
        free shorting, which is what every existing number in RESULTS.md was
        run with and is fine for a long-only rule that never has a negative
        position to charge. It is NOT fine for a long/short rule: borrowing
        stock costs money, the cost is charged for every day the short is on
        rather than per trade, and a long/short backtest that leaves it out
        has quietly awarded itself a free income stream. General collateral
        on a liquid ETF is roughly 25-75bp a year; a hard-to-borrow name is
        a different order of magnitude.
    pending: optional callable symbol -> signed shares still working at the
        execution handler. Both execution handlers that fill instantly leave
        nothing working, so the default of None (treated as zero) is right for
        them. ParticipationLimitedExecutionHandler does not: an order can take
        days to complete, and a portfolio that sizes against its *filled*
        position while a partial fill is still working will order the same
        shares twice, then twice again the next bar. Position plus open orders
        is what a real order management system tracks, and this is that.
    max_fill_days / participation: cap the position at what the desk can
        actually get done. A participation-limited handler fills at most
        `participation` of each bar's volume, so a position of more than
        max_fill_days x participation x ADV cannot be built inside
        max_fill_days bars, and RESULTS.md section 7 found that above $1bn
        most of such an order is never built at all - the next signal
        cancels it first. A desk that knows it will get a tenth done sends a
        tenth, and this is that rule: the share count from either sizing
        scheme is clipped to that liquidity budget, with ADV the trailing
        `adv_window` released volumes, so a position never takes longer than
        max_fill_days to put on. Both default to None, which is no cap, and
        every number in RESULTS.md before section 8 was run without one.
        The two go together: a cap in days means nothing without the rate it
        is filled at, so setting one without the other is an error rather
        than a silent default. It caps the POSITION, not the order; a
        reversal from long to short is two positions' worth and takes twice
        as long, which is what it would take a desk too.
    """

    def __init__(self, data: HistoricalDataHandler, initial_cash: float = 100_000.0,
                 trade_size: int = 100, vol_target: float | None = None,
                 vol_window: int = 20, max_leverage: float = 3.0,
                 rebalance_band: float = 0.2, short_borrow_bps: float = 0.0,
                 pending=None, max_fill_days: float | None = None,
                 participation: float | None = None, adv_window: int = 20):
        if (max_fill_days is None) != (participation is None):
            raise ValueError("max_fill_days and participation go together: "
                             "a liquidity cap needs both the days and the rate")
        if max_fill_days is not None and max_fill_days <= 0:
            raise ValueError("max_fill_days must be positive")
        if participation is not None and not 0 < participation <= 1:
            raise ValueError("participation must be in (0, 1]")
        self.data = data
        self.initial_cash = initial_cash
        self.trade_size = trade_size
        self.vol_target = vol_target
        self.vol_window = vol_window
        self.max_leverage = max_leverage
        self.rebalance_band = rebalance_band
        self.short_borrow_bps = short_borrow_bps
        self.pending = pending
        self.max_fill_days = max_fill_days
        self.participation = participation
        self.adv_window = adv_window
        self.liquidity_capped = 0   # how many sizings the liquidity budget clipped
        self.borrow_paid = 0.0  # cumulative dollars of borrow cost, for reporting
        self.cash = initial_cash
        self.positions: dict[str, int] = {}
        self.equity_history: list[tuple] = []
        # Gross position notional over equity, one point per bar. The
        # capacity studies need to know how much of the book was actually in
        # the market, and the equity curve alone cannot say: a book that is
        # 10% invested and one that is 100% invested in something ten times
        # calmer draw the same line.
        self.exposure_history: list[tuple] = []
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
        return self._liquidity_clip(symbol, self._risk_size_for(symbol))

    def _risk_size_for(self, symbol: str) -> int:
        """The size before the liquidity budget: fixed, or vol-targeted."""
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

    def liquidity_budget(self, symbol: str) -> int | None:
        """The most shares a position may be, given how fast they can be
        bought: max_fill_days x participation x trailing ADV. None when no
        cap is set. Reads released volumes only, like everything else here,
        and during warmup the average is over whatever has been released."""
        if self.max_fill_days is None:
            return None
        window = self.data.get_latest_volumes(symbol, self.adv_window)
        if not len(window):
            return None
        adv = float(window.mean())
        return int(self.max_fill_days * self.participation * adv)

    def _liquidity_clip(self, symbol: str, shares: int) -> int:
        budget = self.liquidity_budget(symbol)
        if budget is None or shares <= budget:
            return shares
        self.liquidity_capped += 1
        return budget

    def _effective_position(self, symbol: str) -> int:
        """Filled position plus anything still working at the broker.

        Sizing against the filled position alone is correct only when fills are
        instant. When they are not, the shares in flight have already been
        committed and must count toward the target, or the same trade gets sent
        repeatedly while it is being worked.
        """
        current = self.positions.get(symbol, 0)
        if self.pending is None:
            return current
        return current + self.pending(symbol)

    def on_signal(self, signal: SignalEvent) -> OrderEvent | None:
        """Turn an opinion into a sized order (or None).

        The order is for the DIFFERENCE between target and current position, so
        a SHORT signal while long correctly emits a double-size sell.
        """
        target = self._TARGETS[signal.signal] * self._size_for(signal.symbol)
        current = self._effective_position(signal.symbol)
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

            delta = target - self._effective_position(symbol)
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

    def accrue_borrow(self) -> float:
        """Charge one bar of stock-borrow cost on every short position.

        Returns the dollars charged, which is zero unless short_borrow_bps was
        set and something is actually short. The charge is a function of how
        long a short is held, not of how often it is traded, which is why it
        belongs here on the per-bar path rather than next to commission in the
        execution handler.
        """
        if not self.short_borrow_bps:
            return 0.0

        daily = self.short_borrow_bps / 10_000.0 / TRADING_DAYS
        charge = sum(
            abs(shares) * self.data.current_price(symbol) * daily
            for symbol, shares in self.positions.items()
            if shares < 0
        )
        self.cash -= charge
        self.borrow_paid += charge
        return charge

    def mark_to_market(self, time) -> None:
        """Record one equity point for this bar.

        The engine calls this AFTER all of the bar's events are processed. Called
        before fills, the equity curve lags reality by a bar — the end-to-end
        test checks for exactly that bug.

        Borrow accrues here, immediately before the mark, so a short position is
        charged for the bar it was actually held and the equity point already
        has the charge in it.
        """
        self.accrue_borrow()
        equity = self.total_equity()
        self.equity_history.append((time, equity))
        gross = sum(abs(shares) * self.data.current_price(symbol)
                    for symbol, shares in self.positions.items() if shares != 0)
        self.exposure_history.append((time, gross / equity if equity > 0 else 0.0))
