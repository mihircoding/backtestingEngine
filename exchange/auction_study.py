"""The cross charges the taker more than the book does. So why use it?

Usage:  python auction_study.py

Everything in RESULTS.md sections 1-6 happens in the continuous book, and
the list of what isn't modeled has always started the same way: no opening
or closing auction, which is where a large share of real volume actually
trades under entirely different rules. src/auction.py is that mechanism.
This is what it costs.

Give both mechanisms the SAME resting liquidity and send the same buy order
into each. The continuous book fills it by walking up the offers, and the
buyer pays the average of every price it consumes. The auction fills it at
one price - the marginal one - and so does everyone who traded with it.

The average of a rising ladder is below its last rung, so the buyer pays
MORE in the cross, every time, by construction. The gap is the surplus that
the early sellers used to give away by resting at good prices, handed back
to them. That is not a defect of the auction; it is the definition of a
uniform-price clearing, and it is why passive sellers like it.

Which leaves the question the comparison cannot answer on its own: real
traders send size to the close anyway. The second table is why. It holds
the order constant and asks how much DEEPER the auction book has to be
before the single clearing price beats the sweep - the break-even depth
multiple. It is not large, and a real closing cross is far past it, because
every participant in the day shows up at the same instant instead of
leaving a few hundred shares on the screen at a time.
"""

import numpy as np

from src.auction import AuctionOrder, book_from_levels, indicative, uncross
from src.order import Side, to_tick
from src.orderbook import LimitOrderBook
from src.simulator import seed_book, simulate

# Order sizes as a share of the liquidity resting on the offer, so the table
# says something about the mechanism rather than about how many shares this
# particular simulator happened to leave lying around.
SHARES_OF_DEPTH = (0.05, 0.10, 0.25, 0.50, 0.75, 0.95)
N_LEVELS = 500
SEED = 11


def warm_book(n_events: int = 20_000, seed: int = SEED) -> LimitOrderBook:
    """A book with a realistic shape rather than a hand-drawn ladder.

    The zero-intelligence simulator from sections 1-5 builds depth that
    thins out away from the mid the way a real one does. Reusing it means
    this comparison runs on the book the project already characterized,
    instead of on a flat ladder that would make both mechanisms look linear.
    """
    book = LimitOrderBook()
    seed_book(book, mid=100.0, levels=20, qty=200)
    simulate(book, n_events=n_events, seed=seed)
    return book


def sweep_cost(levels: list[tuple[float, int]], quantity: int) -> dict:
    """Walk a market order up the ladder, paying every price it eats."""
    remaining, spent, filled, last = quantity, 0.0, 0, None
    for price, qty in levels:
        take = min(qty, remaining)
        if take <= 0:
            break
        spent += take * price
        filled += take
        remaining -= take
        last = price
    if filled == 0:
        return {"filled": 0, "vwap": None, "last": None, "spent": 0.0}
    return {"filled": filled, "vwap": spent / filled, "last": last,
            "spent": spent, "unfilled": remaining}


def scale_levels(levels: list[tuple[float, int]], k: float
                 ) -> list[tuple[float, int]]:
    """The same price ladder with k times the size at every level.

    Deliberately crude: it scales what is there rather than inventing new
    price levels, so the only thing changing between runs is depth. A real
    auction book is also *wider* than the continuous one, and widening it
    would only help the auction further, so this understates the case.
    """
    return [(p, max(1, int(round(q * k)))) for p, q in levels]


def cross_price(levels: list[tuple[float, int]], quantity: int, side: Side,
                reference: float) -> float | None:
    """Clearing price when `quantity` market shares meet that resting side."""
    orders = (book_from_levels([], levels) if side is Side.BUY
              else book_from_levels(levels, []))
    orders.append(AuctionOrder(side, quantity, None, sequence=10_000))
    return uncross(orders, reference_price=reference).price


def breakeven_depth(levels: list[tuple[float, int]], quantity: int,
                    target_vwap: float, reference: float,
                    hi: float = 64.0) -> float | None:
    """Smallest depth multiple at which the cross beats sweeping the book.

    Bisection on k. The clearing price falls with depth in steps, not
    smoothly - it is a price on a tick grid - so the answer is reported to
    one decimal and read as "about this much", not as a root.
    """
    if cross_price(levels, quantity, Side.BUY, reference) <= target_vwap:
        return 1.0
    if (cross_price(scale_levels(levels, hi), quantity, Side.BUY, reference)
            or np.inf) > target_vwap:
        return None
    lo = 1.0
    for _ in range(40):
        mid = (lo + hi) / 2
        price = cross_price(scale_levels(levels, mid), quantity, Side.BUY,
                            reference)
        if price is not None and price <= target_vwap:
            hi = mid
        else:
            lo = mid
    return hi


def compare(book: LimitOrderBook) -> list[dict]:
    mid = book.mid_price()
    asks = book.depth(Side.SELL, levels=N_LEVELS)
    resting = sum(q for _, q in asks)

    rows = []
    for share in SHARES_OF_DEPTH:
        size = int(resting * share)
        cont = sweep_cost(asks, size)
        auc = cross_price(asks, size, Side.BUY, reference=mid)
        if cont["filled"] == 0 or auc is None:
            continue
        rows.append({
            "share": share,
            "size": size,
            "cont_vwap": cont["vwap"],
            "cont_last": cont["last"],
            "cont_bps": (cont["vwap"] / mid - 1) * 10_000,
            "auc_price": auc,
            "auc_bps": (auc / mid - 1) * 10_000,
            "extra": (auc - cont["vwap"]) * size,
            "breakeven": breakeven_depth(asks, size, cont["vwap"], mid),
        })
    return rows


def imbalance_path(book: LimitOrderBook, steps: int = 6) -> list[dict]:
    """The pre-open indicative feed as a buy order grows.

    The indicative price and imbalance exist to be published: a large buy
    imbalance at 09:28 is a standing invitation to anyone willing to sell
    into it, and on a real venue the imbalance usually shrinks before the
    cross because of exactly that. Nothing here responds - this is only the
    number a responder would be reading.

    Note the first row. With no market order the two limit books do not
    overlap (there is a spread, which is what a spread means) and the
    auction has nothing to cross. An auction needs someone willing to pay
    the other side's price, same as the continuous book does; what it
    changes is what that person pays, not whether they are needed.
    """
    mid = book.mid_price()
    asks = book.depth(Side.SELL, levels=N_LEVELS)
    bids = book.depth(Side.BUY, levels=N_LEVELS)
    resting = sum(q for _, q in asks)

    out = []
    for i in range(steps + 1):
        qty = int(resting * i * 0.15)
        orders = book_from_levels(bids, asks)
        if qty:
            orders.append(AuctionOrder(Side.BUY, qty, None, sequence=10_000))
        ind = indicative(orders, reference_price=mid)
        out.append({"market_buy": qty, **ind,
                    "move_bps": (ind["price"] / mid - 1) * 10_000
                    if ind["price"] else None})
    return out


def main() -> None:
    book = warm_book()
    mid = book.mid_price()
    asks = book.depth(Side.SELL, levels=N_LEVELS)
    resting = sum(q for _, q in asks)
    print(f"Book after 20,000 events: mid {mid:.4f}, spread {book.spread():.4f}, "
          f"{resting:,} shares resting on the offer")

    rows = compare(book)
    print("\nOne buyer, the same offers, two mechanisms\n")
    print(f"{'order':>8} {'% depth':>8} {'sweep vwap':>11} {'bps':>6} "
          f"{'cross':>8} {'bps':>6} {'buyer pays':>11} {'breakeven':>10}")
    for r in rows:
        be = f"{r['breakeven']:.1f}x" if r["breakeven"] else "  -"
        print(f"{r['size']:>8,} {r['share']:>7.0%} {r['cont_vwap']:>11.4f} "
              f"{r['cont_bps']:>6.1f} {r['auc_price']:>8.2f} "
              f"{r['auc_bps']:>6.1f} {r['extra']:>10,.0f} {be:>10}")

    print("\n'buyer pays' is the extra dollars the cross costs versus sweeping "
          "the same ladder.\nIt is not lost - it goes to the resting sellers, "
          "who all print at the clearing price\ninstead of at their own. "
          "'breakeven' is how many times deeper the auction book has\nto be "
          "before the single price is cheaper than the walk.")

    print("\nPre-open indicative feed, buy imbalance growing\n")
    print(f"{'market buy':>11} {'indicative':>11} {'move bps':>9} "
          f"{'imbalance':>10} {'side':>5}  reason")
    for r in imbalance_path(book):
        price = f"{r['price']:.2f}" if r["price"] else "-"
        move = f"{r['move_bps']:+.1f}" if r["move_bps"] is not None else "-"
        imb = f"{r['imbalance']:,}" if r["price"] else "-"
        print(f"{r['market_buy']:>11,} {price:>11} {move:>9} "
              f"{imb:>10} {str(r['side'] or '-'):>5}  {r['reason']}")


if __name__ == "__main__":
    main()
