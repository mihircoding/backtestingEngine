"""The opening and closing cross: one price, everyone at once.

The list of things this project doesn't model has always included the
auctions, with the note that they are where a large share of real volume
trades under entirely different rules. "Different rules" is the part worth
being precise about, because almost every intuition the continuous book
gives you is wrong here.

In the continuous book, orders arrive one at a time, each trade has its own
price, and priority is price then time. In an auction, nothing trades while
orders accumulate; then the book is crossed ONCE, every execution prints at
the SAME price, and that price is chosen by an optimization - the price that
trades the most shares. Time priority does not decide who trades unless two
prices are otherwise tied; it only decides who is unlucky at the margin.

The tie-break ladder below is the one most venues use (Nasdaq's opening and
closing cross, Xetra, Euronext all differ in details and agree on the shape):

  1. Maximum executable volume.
  2. If several prices tie on volume, the one leaving the smallest imbalance.
  3. If still tied, the side of the imbalance decides: unfilled buyers mean
     the price should be higher, unfilled sellers mean lower.
  4. If still tied, the price closest to a reference - the previous close, or
     the continuous market's mid.

Rule 1 is not a convention, it is the whole idea: an auction exists to let
the largest possible number of people trade with each other rather than to
find the "right" price, and the price is whatever falls out of that. Rules
2-4 exist because rule 1 is frequently ambiguous - on a coarse tick grid a
whole range of prices trades the same number of shares - and an exchange
cannot publish a range.

What this module is NOT: a continuous book. There is no resting state, no
cancel, no matching loop. It takes a set of orders and returns one price and
a list of fills. That is the honest shape of the thing.
"""

from dataclasses import dataclass, field

from .order import Side, to_tick


@dataclass
class AuctionOrder:
    """An order entered into the auction book.

    price is None for a market order - "I want to trade at whatever the
    cross prints". Market orders are the reason an auction can clear at all
    when the limit books do not overlap, and the reason a careless one can
    move the print a long way; see auction_study.py.

    sequence is the arrival order, used only for rule 4 of the allocation
    (who is unlucky at the marginal price), never for choosing the price.
    """

    side: Side
    quantity: int
    price: float | None = None
    sequence: int = 0
    participant_id: str | None = None

    def __post_init__(self):
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.price is not None:
            self.price = to_tick(self.price)

    @property
    def is_market(self) -> bool:
        return self.price is None


@dataclass
class AuctionResult:
    price: float | None
    volume: int                       # shares crossed, per side
    imbalance: int                    # unfilled shares at the cross price
    imbalance_side: Side | None       # which side is left holding them
    fills: list = field(default_factory=list)   # (AuctionOrder, filled_qty)
    reason: str = ""                  # which tie-break decided the price


def _willing(orders: list[AuctionOrder], side: Side, price: float) -> int:
    """Shares that side would trade if the cross printed at `price`.

    A buyer is willing at any price at or below their limit; a seller at any
    price at or above theirs. Market orders are willing at every price, which
    is what "market" means and also why they are counted first below.
    """
    total = 0
    for o in orders:
        if o.side is not side:
            continue
        if o.is_market:
            total += o.quantity
        elif side is Side.BUY and o.price >= price:
            total += o.quantity
        elif side is Side.SELL and o.price <= price:
            total += o.quantity
    return total


def candidate_prices(orders: list[AuctionOrder]) -> list[float]:
    """Every price the cross could possibly print at.

    The clearing price is always one of the limit prices in the book. Between
    two adjacent limits nothing changes about who is willing to trade, so the
    executable volume is flat there - checking the limits themselves checks
    every distinct outcome, and there is no need to walk a tick grid.
    """
    return sorted({o.price for o in orders if not o.is_market})


def uncross(orders: list[AuctionOrder],
            reference_price: float | None = None) -> AuctionResult:
    """Find the cross price and allocate the fills.

    Returns volume 0 and price None when the books do not overlap and there
    is nothing a single price can match - a real opening auction in that
    state just opens the continuous book instead.
    """
    prices = candidate_prices(orders)
    if not prices:
        # Only market orders. They can cross each other, but nothing in the
        # book says at what price, so a venue falls back on the reference.
        buy = sum(o.quantity for o in orders if o.side is Side.BUY)
        sell = sum(o.quantity for o in orders if o.side is Side.SELL)
        vol = min(buy, sell)
        if vol == 0 or reference_price is None:
            return AuctionResult(None, 0, abs(buy - sell),
                                 _side_of(buy - sell), [], "no limit prices")
        return _allocate(orders, to_tick(reference_price), vol,
                         "market orders only, reference price used")

    rows = []
    for p in prices:
        demand = _willing(orders, Side.BUY, p)
        supply = _willing(orders, Side.SELL, p)
        rows.append((p, min(demand, supply), demand - supply))

    best_volume = max(r[1] for r in rows)
    if best_volume == 0:
        surplus = rows[0][2]
        return AuctionResult(None, 0, abs(surplus), _side_of(surplus), [],
                             "no overlap")

    reason = "maximum volume"
    tied = [r for r in rows if r[1] == best_volume]
    if len(tied) > 1:
        smallest = min(abs(r[2]) for r in tied)
        tied = [r for r in tied if abs(r[2]) == smallest]
        reason = "smallest imbalance"

    if len(tied) > 1:
        signs = {_sign(r[2]) for r in tied}
        if signs == {1}:            # buyers left over everywhere: go higher
            tied = [max(tied, key=lambda r: r[0])]
            reason = "buy-side imbalance"
        elif signs == {-1}:         # sellers left over: go lower
            tied = [min(tied, key=lambda r: r[0])]
            reason = "sell-side imbalance"

    if len(tied) > 1:
        if reference_price is not None:
            ref = to_tick(reference_price)
            tied = [min(tied, key=lambda r: (abs(r[0] - ref), r[0]))]
            reason = "closest to reference"
        else:
            # No reference to appeal to. The midpoint of the tied range is
            # the neutral choice and is what a venue does when it has no
            # previous close either (a first day of trading, say).
            lo, hi = min(r[0] for r in tied), max(r[0] for r in tied)
            tied = [(to_tick((lo + hi) / 2), best_volume, 0)]
            reason = "midpoint of tied range"

    price, volume, surplus = tied[0]
    result = _allocate(orders, price, volume, reason)
    return result


def _sign(x: int) -> int:
    return (x > 0) - (x < 0)


def _side_of(surplus: int) -> Side | None:
    if surplus > 0:
        return Side.BUY
    if surplus < 0:
        return Side.SELL
    return None


def _allocate(orders: list[AuctionOrder], price: float, volume: int,
              reason: str) -> AuctionResult:
    """Hand out `volume` shares on each side at the single cross price.

    Everyone strictly better than the cross price is filled in full - that is
    what "maximum volume" bought them. The rationing happens only at the
    cross price itself, where the willing quantity is what pushed one side
    over the other, and it is done in arrival order. Real venues vary here
    (some pro-rate, some give market orders and better-priced limits their
    own priority tiers); FIFO is the simplest rule that is actually used and
    keeps the tie-break honest rather than fractional.
    """
    fills = []
    for side in (Side.BUY, Side.SELL):
        aggressive, marginal = [], []
        for o in orders:
            if o.side is not side:
                continue
            if o.is_market:
                aggressive.append(o)
            elif side is Side.BUY and o.price > price:
                aggressive.append(o)
            elif side is Side.SELL and o.price < price:
                aggressive.append(o)
            elif o.price == price:
                marginal.append(o)

        remaining = volume
        for o in sorted(aggressive, key=lambda x: x.sequence):
            take = min(o.quantity, remaining)
            if take:
                fills.append((o, take))
                remaining -= take
        for o in sorted(marginal, key=lambda x: x.sequence):
            take = min(o.quantity, remaining)
            if take:
                fills.append((o, take))
                remaining -= take

    demand = _willing(orders, Side.BUY, price)
    supply = _willing(orders, Side.SELL, price)
    return AuctionResult(price, volume, abs(demand - supply),
                         _side_of(demand - supply), fills, reason)


def indicative(orders: list[AuctionOrder],
               reference_price: float | None = None) -> dict:
    """What the exchange publishes during the pre-open.

    Same calculation as uncross(), reported rather than executed. The point
    of publishing it is that it invites the other side in: a large buy
    imbalance at 09:28 is a standing offer to anyone willing to sell into it,
    and the imbalance usually shrinks before the cross because of exactly
    that. Nothing here models that response - it just shows the number the
    responders would be looking at.
    """
    r = uncross(orders, reference_price)
    return {
        "price": r.price,
        "volume": r.volume,
        "imbalance": r.imbalance,
        "side": r.imbalance_side.value if r.imbalance_side else None,
        "reason": r.reason,
    }


def book_from_levels(bid_levels: list[tuple[float, int]],
                     ask_levels: list[tuple[float, int]],
                     start_sequence: int = 0) -> list[AuctionOrder]:
    """Turn a depth snapshot into auction orders, for comparing the two
    mechanisms on the same liquidity."""
    orders, seq = [], start_sequence
    for price, qty in bid_levels:
        orders.append(AuctionOrder(Side.BUY, qty, price, seq))
        seq += 1
    for price, qty in ask_levels:
        orders.append(AuctionOrder(Side.SELL, qty, price, seq))
        seq += 1
    return orders
