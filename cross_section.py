"""Run the same rule on many assets, because SPY is one sample.

Everything in RESULTS.md so far is a statement about SPY between 2015 and
2024. The grid says 50/200 is unremarkable among its neighbours; the
bootstrap says its Sharpe is 0.75 give or take most of a point; the
walk-forward says fitting the windows makes it worse. All three answer
"is this parameter pair special?" and none of them answers the question
underneath it: is the *rule* bad, or is SPY a bad place to run it?

That distinction matters because trend following did not come from equity
indices. It came from futures books - commodities, rates, currencies -
where the argument for it is that those markets trend for reasons (carry,
hedging pressure, slow-moving macro) that a large-cap equity index does
not share. Testing a trend rule only on SPY and concluding trend following
does not work is like testing a rain jacket indoors.

So: same engine, same costs, same 50/200 windows, same ten years, run
across a cross-section of liquid ETFs that span equities, rates, credit,
commodities and real estate. For each one, the rule against that asset's
own buy & hold, with a bootstrap interval on the gap.

The correlation problem is the reason this file is longer than a loop.
Twenty-odd ETFs are not twenty-odd independent tests - SPY, QQQ and XLK
are close to the same bet - so "the rule beat buy & hold on 9 of 22" is a
much weaker statement than 9-of-22 sounds. effective_trials() in
significance.py already measures that for the parameter grid; the same
calculation applied to the per-asset difference curves says how many
independent opinions this cross-section actually contains.

Usage:  python cross_section.py [--quick]
"""

import argparse

import numpy as np
import pandas as pd

import significance as sig
from run_backtest import TRADING_DAYS, run, stats
from src.strategy import BuyAndHoldStrategy, MovingAverageCrossStrategy

START = "2015-01-01"
END = "2024-12-31"

# Chosen for history and liquidity, not for results: every one of these
# traded through the whole window with a spread a retail order would not
# notice. The groups are here so the table can be read by asset class
# rather than ticker by ticker, which is where the finding turns out to be.
UNIVERSE = {
    "SPY": "US large cap",
    "QQQ": "US tech",
    "IWM": "US small cap",
    "EFA": "Developed ex-US",
    "EEM": "Emerging markets",
    "XLE": "Energy",
    "XLF": "Financials",
    "XLK": "Technology",
    "XLV": "Health care",
    "XLU": "Utilities",
    "XLP": "Staples",
    "XLI": "Industrials",
    "TLT": "20y Treasuries",
    "IEF": "7-10y Treasuries",
    "LQD": "IG credit",
    "HYG": "High yield",
    "TIP": "Inflation linked",
    "GLD": "Gold",
    "SLV": "Silver",
    "DBC": "Commodities",
    "USO": "Crude oil",
    "VNQ": "REITs",
    "UUP": "US dollar",
    "FXE": "Euro",
}

GROUPS = {
    "Equity": ["SPY", "QQQ", "IWM", "EFA", "EEM", "XLE", "XLF", "XLK",
               "XLV", "XLU", "XLP", "XLI", "VNQ"],
    "Rates and credit": ["TLT", "IEF", "LQD", "HYG", "TIP"],
    "Commodities and FX": ["GLD", "SLV", "DBC", "USO", "UUP", "FXE"],
}


def fetch_universe(symbols: list[str], start: str = START,
                   end: str = END) -> pd.DataFrame:
    """One download for the whole cross-section.

    Columns whose history does not cover the window are dropped rather than
    padded. A series that starts in 2018 would quietly be scored over a
    different sample from the rest of the table, and the whole point here is
    that every asset is judged over the same ten years.
    """
    import yfinance as yf

    raw = yf.download(symbols, start=start, end=end, auto_adjust=True,
                      progress=False)["Close"]
    if isinstance(raw, pd.Series):
        raw = raw.to_frame(symbols[0])
    full = raw.dropna(axis=1, thresh=len(raw) - 5).dropna()
    dropped = sorted(set(symbols) - set(full.columns))
    if dropped:
        print(f"  (no full history for {', '.join(dropped)} - dropped)")
    return full


def shares_for(price: float, notional: float = 35_000.0) -> int:
    """Trade size in shares, equalized by dollars rather than by count.

    SPY's 200 shares is $70,000 of exposure; 200 shares of SLV is $5,000.
    Comparing those two runs' Sharpes would be comparing a strategy to a
    strategy-plus-cash. Sizing every asset to the same notional makes the
    columns comparable - and since Sharpe is scale-free, the exact number
    only matters through the fixed commission per share.
    """
    return max(1, int(round(notional / price)))


def one_asset(prices: pd.DataFrame, symbol: str, n_boot: int,
              short: int = 50, long: int = 200) -> dict:
    """The rule against that asset's own buy & hold, on the same dates."""
    px = prices[[symbol]]
    size = shares_for(float(px[symbol].iloc[0]))

    rule = run(px, MovingAverageCrossStrategy, size,
               short_window=short, long_window=long)
    hold = run(px, BuyAndHoldStrategy, size)

    r_ret = rule.pct_change().dropna().values
    h_ret = hold.pct_change().dropna().values
    gap = sig.paired_difference(r_ret, h_ret, n_boot=n_boot)

    return {
        "symbol": symbol,
        "label": UNIVERSE.get(symbol, ""),
        "rule_sharpe": sig.sharpe(r_ret),
        "hold_sharpe": sig.sharpe(h_ret),
        "diff": gap["diff"],
        "lo": gap["lo"],
        "hi": gap["hi"],
        "p_no_better": gap["p_le_zero"],
        "rule_dd": stats(rule)["max_dd"],
        "hold_dd": stats(hold)["max_dd"],
        "trades": rule.attrs["n_fills"],
        "_diff_curve": r_ret - h_ret,
    }


def cross_section(prices: pd.DataFrame, n_boot: int = 2000,
                  short: int = 50, long: int = 200) -> dict:
    rows = [one_asset(prices, s, n_boot, short, long) for s in prices.columns]

    curves = np.vstack([r["_diff_curve"] for r in rows])
    n_eff = sig.effective_trials(curves)

    # The pooled bet: hold the rule on every asset at once against holding
    # every asset at once. Equal weight, no rebalancing decision to argue
    # about, and it is the only number here that a person could have traded.
    pooled = curves.mean(axis=0)
    pooled_ci = sig.sharpe_interval(pooled, n_boot=n_boot)

    wins = sum(1 for r in rows if r["diff"] > 0)
    clear = sum(1 for r in rows if r["lo"] > 0)
    return {
        "rows": sorted(rows, key=lambda r: -r["diff"]),
        "n_assets": len(rows),
        "effective_assets": n_eff,
        "wins": wins,
        "clear_wins": clear,
        "clear_losses": sum(1 for r in rows if r["hi"] < 0),
        "mean_diff": float(np.mean([r["diff"] for r in rows])),
        "median_diff": float(np.median([r["diff"] for r in rows])),
        "pooled": pooled_ci,
        "groups": group_summary(rows),
    }


def group_summary(rows: list[dict]) -> list[dict]:
    by_symbol = {r["symbol"]: r for r in rows}
    out = []
    for name, symbols in GROUPS.items():
        present = [by_symbol[s] for s in symbols if s in by_symbol]
        if not present:
            continue
        diffs = [r["diff"] for r in present]
        out.append({
            "group": name,
            "n": len(present),
            "mean_diff": float(np.mean(diffs)),
            "wins": sum(1 for d in diffs if d > 0),
            "mean_rule": float(np.mean([r["rule_sharpe"] for r in present])),
            "mean_hold": float(np.mean([r["hold_sharpe"] for r in present])),
        })
    return out


def print_report(rep: dict, short: int = 50, long: int = 200) -> None:
    print(f"\n{short}/{long} against each asset's own buy & hold, "
          f"{START[:4]}-{END[:4]}, same costs throughout\n")
    print(f"{'':6} {'asset':<18} {'rule':>6} {'hold':>6} {'gap':>7} "
          f"{'95% interval':>17} {'trades':>7}")
    for r in rep["rows"]:
        flag = "  " if r["lo"] <= 0 <= r["hi"] else ("+ " if r["lo"] > 0 else "- ")
        print(f"{flag}{r['symbol']:<4} {r['label']:<18} "
              f"{r['rule_sharpe']:>6.2f} {r['hold_sharpe']:>6.2f} "
              f"{r['diff']:>+7.2f} "
              f"[{r['lo']:>+6.2f}, {r['hi']:>+6.2f}] {r['trades']:>7}")

    print(f"\nBeat buy & hold on {rep['wins']} of {rep['n_assets']} assets; "
          f"{rep['clear_wins']} clear of zero, {rep['clear_losses']} clearly worse.")
    print(f"Mean gap {rep['mean_diff']:+.2f}, median {rep['median_diff']:+.2f}.")
    print(f"The {rep['n_assets']} assets behave like "
          f"{rep['effective_assets']:.1f} independent tests.")

    p = rep["pooled"]
    print(f"\nEqual-weight across everything, rule minus hold: Sharpe "
          f"{p['sharpe']:+.2f}, 95% [{p['lo']:+.2f}, {p['hi']:+.2f}], "
          f"P(no better) {p['p_le_zero']:.1%}")

    print(f"\n{'group':<20} {'n':>3} {'mean rule':>10} {'mean hold':>10} "
          f"{'mean gap':>9} {'won':>5}")
    for g in rep["groups"]:
        print(f"{g['group']:<20} {g['n']:>3} {g['mean_rule']:>10.2f} "
              f"{g['mean_hold']:>10.2f} {g['mean_diff']:>+9.2f} "
              f"{g['wins']:>3}/{g['n']}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true",
                        help="500 bootstrap draws instead of 2000")
    parser.add_argument("--short", type=int, default=50)
    parser.add_argument("--long", type=int, default=200)
    args = parser.parse_args()

    print(f"Downloading {len(UNIVERSE)} series...")
    prices = fetch_universe(list(UNIVERSE))
    print(f"  {len(prices)} bars, {len(prices.columns)} assets, "
          f"{prices.index[0].date()} to {prices.index[-1].date()}")

    rep = cross_section(prices, n_boot=500 if args.quick else 2000,
                        short=args.short, long=args.long)
    print_report(rep, args.short, args.long)


if __name__ == "__main__":
    main()
