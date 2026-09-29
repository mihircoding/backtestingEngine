"""Does the short leg rescue the trend rule, or was the rule the problem?

Usage:  python long_short.py [--quick]

cross_section.py ran the 50/200 crossing rule on 24 liquid ETFs and found it
losing to buy & hold, with the whole loss in equities and roughly nothing in
bonds, credit, commodities or FX. It ended with an explicit admission that it
could not tell two very different explanations apart:

    (a) the rule does not work, or
    (b) the rule was only half built, because it was long-only.

That distinction is not a detail. A long-only trend rule on something with an
upward drift is penalised for being in cash, not for being wrong: every day
it sits out it forgoes the risk premium, whether or not the signal was any
good. Trend following as actually practised is long AND short, and the short
leg removes precisely that asymmetry. If (b) is the answer, the gap should
close where it was widest - equities - and the rule should stop looking like
a worse version of buy & hold.

So this runs the same engine, the same 24 assets, the same ten years and the
same costs a third time, with SHORT where the long-only rule went flat, and
puts the three side by side.

Two things this file insists on that a long/short backtest usually skips:

  - Borrow is charged. Shorting means borrowing stock, which costs money for
    every day the position is held, and a long/short equity curve that does
    not pay it has awarded itself a free income stream. Portfolio takes
    short_borrow_bps for this; the last table is the sensitivity, because the
    right rate is not one number - it is 25bp for SPY and considerably more
    for USO.
  - The short leg is not judged on the whole strategy's P&L. Adding a second
    leg to a losing strategy can improve the total while the new leg itself
    loses money, so there is a separate table asking one question directly:
    on the days this rule was short, what did the asset do?
"""

import argparse

import numpy as np
import pandas as pd

import significance as sig
from cross_section import (END, GROUPS, START, UNIVERSE, fetch_universe,
                           shares_for)
from run_backtest import run, stats
from src.strategy import (BuyAndHoldStrategy, MovingAverageCrossLongShortStrategy,
                          MovingAverageCrossStrategy)

# General collateral on a liquid ETF. Deliberately at the top of that range
# rather than the bottom: the sensitivity table below shows what the choice is
# worth, and it is better for the headline number to be the pessimistic one.
BORROW_BPS = 75.0
BORROW_GRID = (0.0, 25.0, 75.0, 150.0, 400.0)


def signal_position(prices: pd.Series, short: int = 50, long: int = 200,
                    allow_short: bool = True) -> pd.Series:
    """The rule's target position as +1 / 0 / -1, one value per bar.

    This reproduces the strategy's decision outside the engine so the short
    leg can be examined on its own. It is not a second implementation of the
    backtest - there is no cash, no commission and no sizing here, and nothing
    in the reported Sharpes comes from this function. It exists to answer "on
    which days was the rule short", which the engine's equity curve cannot be
    asked.

    The value at bar t uses closes up to and including t, matching
    HistoricalDataHandler.get_latest(), so the return it earns is bar t+1's.
    Bars inside the warmup are 0 either way: no long MA, no position.
    """
    short_ma = prices.rolling(short).mean()
    long_ma = prices.rolling(long).mean()
    up = short_ma > long_ma
    pos = pd.Series(np.where(up, 1.0, -1.0 if allow_short else 0.0),
                    index=prices.index)
    pos[long_ma.isna()] = 0.0
    return pos


def one_asset(prices: pd.DataFrame, symbol: str, n_boot: int, short: int = 50,
              long: int = 200, borrow_bps: float = BORROW_BPS) -> dict:
    """Hold, long-only rule and long/short rule on one asset, same dates."""
    px = prices[[symbol]]
    size = shares_for(float(px[symbol].iloc[0]))
    short_kw = {"portfolio_kwargs": {"short_borrow_bps": borrow_bps}}

    hold = run(px, BuyAndHoldStrategy, size)
    lo = run(px, MovingAverageCrossStrategy, size,
             short_window=short, long_window=long)
    ls = run(px, MovingAverageCrossLongShortStrategy, size,
             short_window=short, long_window=long, **short_kw)

    h_ret = hold.pct_change().dropna().values
    lo_ret = lo.pct_change().dropna().values
    ls_ret = ls.pct_change().dropna().values

    gap_lo = sig.paired_difference(lo_ret, h_ret, n_boot=n_boot)
    gap_ls = sig.paired_difference(ls_ret, h_ret, n_boot=n_boot)
    leg = short_leg(px[symbol], short, long)

    return {
        "symbol": symbol,
        "label": UNIVERSE.get(symbol, ""),
        "hold": sig.sharpe(h_ret),
        "long_only": sig.sharpe(lo_ret),
        "long_short": sig.sharpe(ls_ret),
        "gap_lo": gap_lo["diff"],
        "gap_ls": gap_ls["diff"],
        "ls_lo": gap_ls["lo"],
        "ls_hi": gap_ls["hi"],
        "trades_lo": lo.attrs["n_fills"],
        "trades_ls": ls.attrs["n_fills"],
        "dd_ls": stats(ls)["max_dd"],
        "_ls_curve": ls_ret - h_ret,
        "_lo_curve": lo_ret - h_ret,
        **leg,
    }


def short_leg(prices: pd.Series, short: int = 50, long: int = 200) -> dict:
    """What the asset did on the days the rule wanted to be short.

    The short leg's own P&L, before costs, is minus the asset's return on
    those days. So a POSITIVE mean here is the short leg losing money, and
    the sign is the whole point: a second leg can raise a strategy's Sharpe
    while itself being a loser, because it changes the strategy's correlation
    to the first leg. Judging the leg requires looking at the leg.
    """
    pos = signal_position(prices, short, long, allow_short=True)
    ret = prices.pct_change()
    earned = pos.shift(1) * ret
    is_short = pos.shift(1) < 0

    n = int(is_short.sum())
    asset_when_short = float(ret[is_short].mean()) if n else float("nan")
    return {
        "days_short": n,
        "share_short": n / int(pos.shift(1).ne(0).sum()),
        "asset_when_short_ann": asset_when_short * 252,
        "short_leg_ann": float(earned[is_short].sum() / len(ret) * 252) if n else 0.0,
        "long_leg_ann": float(earned[pos.shift(1) > 0].sum() / len(ret) * 252),
    }


def report(prices: pd.DataFrame, n_boot: int, short: int = 50, long: int = 200,
           borrow_bps: float = BORROW_BPS) -> dict:
    rows = [one_asset(prices, s, n_boot, short, long, borrow_bps)
            for s in prices.columns]

    ls_pooled = sig.sharpe_interval(
        np.vstack([r["_ls_curve"] for r in rows]).mean(axis=0), n_boot=n_boot)
    lo_pooled = sig.sharpe_interval(
        np.vstack([r["_lo_curve"] for r in rows]).mean(axis=0), n_boot=n_boot)

    by_symbol = {r["symbol"]: r for r in rows}
    groups = []
    for name, symbols in GROUPS.items():
        present = [by_symbol[s] for s in symbols if s in by_symbol]
        if not present:
            continue
        groups.append({
            "group": name, "n": len(present),
            "gap_lo": float(np.mean([r["gap_lo"] for r in present])),
            "gap_ls": float(np.mean([r["gap_ls"] for r in present])),
            "wins_ls": sum(1 for r in present if r["gap_ls"] > 0),
            "asset_when_short": float(np.mean(
                [r["asset_when_short_ann"] for r in present])),
        })

    return {
        "rows": sorted(rows, key=lambda r: -r["gap_ls"]),
        "n_assets": len(rows),
        "lo_pooled": lo_pooled,
        "ls_pooled": ls_pooled,
        "wins_ls": sum(1 for r in rows if r["gap_ls"] > 0),
        "wins_lo": sum(1 for r in rows if r["gap_lo"] > 0),
        "better_than_long_only": sum(1 for r in rows
                                     if r["long_short"] > r["long_only"]),
        "groups": groups,
        "borrow_bps": borrow_bps,
    }


def borrow_sensitivity(prices: pd.DataFrame, grid=BORROW_GRID, short: int = 50,
                       long: int = 200) -> list[dict]:
    """The pooled long/short result at several borrow rates.

    Same signals every time - only the carry charge changes - so the spread
    across rows is exactly the part of the answer that is a financing
    assumption rather than a statement about the signal.
    """
    out = []
    for bps in grid:
        curves = []
        for symbol in prices.columns:
            px = prices[[symbol]]
            size = shares_for(float(px[symbol].iloc[0]))
            ls = run(px, MovingAverageCrossLongShortStrategy, size,
                     short_window=short, long_window=long,
                     portfolio_kwargs={"short_borrow_bps": bps})
            hold = run(px, BuyAndHoldStrategy, size)
            curves.append(ls.pct_change().dropna().values
                          - hold.pct_change().dropna().values)
        pooled = np.vstack(curves).mean(axis=0)
        out.append({"bps": bps, "sharpe": sig.sharpe(pooled)})
    return out


def print_report(rep: dict, sens: list[dict], short: int, long: int) -> None:
    print(f"\n{short}/{long}, three ways, {START[:4]}-{END[:4]}, "
          f"borrow charged at {rep['borrow_bps']:.0f}bp\n")
    print(f"{'':6} {'asset':<18} {'hold':>6} {'long':>6} {'l/s':>6} "
          f"{'l/s - hold':>11} {'95% interval':>17} {'% short':>8}")
    for r in rep["rows"]:
        flag = "  " if r["ls_lo"] <= 0 <= r["ls_hi"] else ("+ " if r["ls_lo"] > 0 else "- ")
        print(f"{flag}{r['symbol']:<4} {r['label']:<18} {r['hold']:>6.2f} "
              f"{r['long_only']:>6.2f} {r['long_short']:>6.2f} "
              f"{r['gap_ls']:>+11.2f} [{r['ls_lo']:>+6.2f}, {r['ls_hi']:>+6.2f}] "
              f"{r['share_short']:>7.0%}")

    print(f"\nLong/short beat buy & hold on {rep['wins_ls']} of "
          f"{rep['n_assets']}; long-only managed {rep['wins_lo']}.")
    print(f"Adding the short leg improved {rep['better_than_long_only']} of "
          f"{rep['n_assets']} assets against the long-only version.")

    lo, ls = rep["lo_pooled"], rep["ls_pooled"]
    print(f"\nEqual-weight across everything, rule minus hold:")
    print(f"  long only    Sharpe {lo['sharpe']:>+6.2f}  "
          f"95% [{lo['lo']:+.2f}, {lo['hi']:+.2f}]")
    print(f"  long/short   Sharpe {ls['sharpe']:>+6.2f}  "
          f"95% [{ls['lo']:+.2f}, {ls['hi']:+.2f}]")

    print(f"\n{'group':<20} {'n':>3} {'long gap':>9} {'l/s gap':>9} "
          f"{'won':>5} {'asset ann. when short':>22}")
    for g in rep["groups"]:
        print(f"{g['group']:<20} {g['n']:>3} {g['gap_lo']:>+9.2f} "
              f"{g['gap_ls']:>+9.2f} {g['wins_ls']:>3}/{g['n']} "
              f"{g['asset_when_short']:>21.1%}")
    print("\nThe last column is what the asset returned, annualized, on the days the\n"
          "rule was short it. Positive means the short leg was betting against\n"
          "something that went up, which is the leg losing money on its own terms.")

    print(f"\n{'borrow (bp/yr)':>15} {'pooled l/s Sharpe':>19}")
    for row in sens:
        print(f"{row['bps']:>15.0f} {row['sharpe']:>+19.2f}")
    print("Same signals throughout; only the financing assumption moves.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true",
                        help="500 bootstrap draws and 6 assets; a smoke test")
    parser.add_argument("--short", type=int, default=50)
    parser.add_argument("--long", type=int, default=200)
    args = parser.parse_args()

    symbols = list(UNIVERSE)
    if args.quick:
        symbols = ["SPY", "QQQ", "TLT", "GLD", "USO", "UUP"]
    print(f"Downloading {len(symbols)} series...")
    prices = fetch_universe(symbols)
    print(f"  {len(prices)} bars, {len(prices.columns)} assets, "
          f"{prices.index[0].date()} to {prices.index[-1].date()}")

    rep = report(prices, n_boot=500 if args.quick else 2000,
                 short=args.short, long=args.long)
    sens = borrow_sensitivity(prices, short=args.short, long=args.long)
    print_report(rep, sens, args.short, args.long)


if __name__ == "__main__":
    main()
