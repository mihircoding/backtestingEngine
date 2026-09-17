"""Driver: run the MA-cross strategy through the engine, twice.

Two runs on purpose.

1. Synthetic. A trending sine wave makes it obvious whether the strategy is
   doing what you think — long the upswings, flat the downswings. Debug the
   machinery on data where you already know the answer.

2. Real. The same strategy on SPY. The gap between the two numbers is the
   lesson: a backtest that only ever ran on data built to suit it tells you
   nothing.

Usage:  python run_backtest.py [--no-download]
"""

import argparse

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from src.data_handler import HistoricalDataHandler
from src.engine import Backtest
from src.execution import (NextBarOpenExecutionHandler,
                           ParticipationLimitedExecutionHandler,
                           SimulatedExecutionHandler)
from src.portfolio import Portfolio
from src.strategy import BuyAndHoldStrategy, MovingAverageCrossStrategy

TRADING_DAYS = 252


def make_prices(n: int = 500, seed: int = 7) -> pd.DataFrame:
    """A drifting sine wave plus noise — cyclical by construction."""
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    path = 100 + 0.03 * t + 8 * np.sin(t / 25) + np.cumsum(rng.normal(0, 0.4, n))
    idx = pd.bdate_range("2023-01-01", periods=n)
    return pd.DataFrame({"SYN": path}, index=idx)


def make_opens(closes: pd.DataFrame, seed: int = 11, gap_bps: float = 15.0) -> pd.DataFrame:
    """Synthetic open prices for make_prices()'s synthetic series.

    There's no real intraday process to draw on here, so this models the one
    thing every real market actually does overnight: gap. Each open is the
    prior close plus a small random jump (an overnight gap), NOT the same
    bar's own close — using the same bar's close would make same-bar-close
    and next-bar-open fills identical by construction and defeat the point
    of comparing them. The first bar has no prior close, so its open equals
    its own close (nothing traded before this backtest starts anyway).
    """
    rng = np.random.default_rng(seed)
    prev_close = closes.shift(1)
    gap = 1 + rng.normal(0, gap_bps / 10_000, len(closes))
    opens = prev_close.mul(gap, axis=0)
    opens.iloc[0] = closes.iloc[0]
    return opens


def fetch_prices(symbol: str = "SPY", start: str = "2015-01-01",
                 end: str = "2024-12-31") -> pd.DataFrame:
    import yfinance as yf

    raw = yf.download(symbol, start=start, end=end, auto_adjust=True,
                      progress=False)["Close"].dropna()
    return pd.DataFrame({symbol: raw.squeeze()})


def fetch_opens(symbol: str = "SPY", start: str = "2015-01-01",
                end: str = "2024-12-31") -> pd.DataFrame:
    """Real opening prices for the same symbol and range as fetch_prices() —
    what NextBarOpenExecutionHandler needs, and fetch_prices() doesn't give
    you. Fetched from the same yfinance download so the index lines up
    exactly with the close series."""
    import yfinance as yf

    raw = yf.download(symbol, start=start, end=end, auto_adjust=True,
                      progress=False)["Open"].dropna()
    return pd.DataFrame({symbol: raw.squeeze()})


def fetch_volumes(symbol: str = "SPY", start: str = "2015-01-01",
                  end: str = "2024-12-31") -> pd.DataFrame:
    """Shares traded per bar — what ParticipationLimitedExecutionHandler needs
    to know whether an order is small or is the whole day's liquidity.

    Note auto_adjust=True does not touch volume, so these are raw share counts
    while the prices beside them are split- and dividend-adjusted. For SPY over
    this window there were no splits, so the two are consistent; on a name that
    split, the volume series would need the same adjustment and this function
    would be wrong. Stated here because it is exactly the kind of mismatch that
    produces a capacity number nobody can reproduce."""
    import yfinance as yf

    raw = yf.download(symbol, start=start, end=end, auto_adjust=True,
                      progress=False)["Volume"].dropna()
    return pd.DataFrame({symbol: raw.squeeze()})


def realized_vol(equity: pd.Series) -> float:
    """Annualized standard deviation of the equity curve's daily returns."""
    return equity.pct_change().dropna().std(ddof=1) * np.sqrt(TRADING_DAYS)


def stats(equity: pd.Series) -> dict:
    """Total return, annualized Sharpe, max drawdown. Risk-free assumed 0."""
    daily = equity.pct_change().dropna()
    vol = daily.std(ddof=1)
    running_max = equity.cummax()
    return {
        "final": equity.iloc[-1],
        "total_return": equity.iloc[-1] / equity.iloc[0] - 1,
        "sharpe": daily.mean() / vol * np.sqrt(TRADING_DAYS) if vol > 0 else 0.0,
        "max_dd": (equity / running_max - 1).min(),
    }


def run(prices: pd.DataFrame, strategy_cls, trade_size: int, slippage_bps: float = 2.0,
       commission_per_share: float = 0.005, opens: pd.DataFrame | None = None,
       volumes: pd.DataFrame | None = None, initial_cash: float = 100_000.0,
       execution_cls=SimulatedExecutionHandler, portfolio_kwargs: dict | None = None,
       execution_kwargs: dict | None = None, **kwargs) -> pd.Series:
    """One full backtest, fresh components each time (they carry state).

    opens / execution_cls: pass opens=<DataFrame> and
    execution_cls=NextBarOpenExecutionHandler to fill at the following bar's
    open instead of the same bar's close. See fill_timing_comparison() below
    for what that costs.

    portfolio_kwargs: extra arguments for the Portfolio, e.g.
    {"vol_target": 0.10}. Kept separate from **kwargs, which go to the
    strategy — the two take different knobs and mixing them up is the kind
    of bug that silently runs the wrong backtest.
    """
    data = HistoricalDataHandler(prices, opens=opens, volumes=volumes)
    strategy = strategy_cls(data, **kwargs)
    execution = execution_cls(data, slippage_bps=slippage_bps,
                              commission_per_share=commission_per_share,
                              **(execution_kwargs or {}))
    # The portfolio needs to see what is still working at the broker, or it
    # re-sends shares that are already in flight. Only the participation
    # handler ever has any, and it is the only one that offers the hook.
    pending = getattr(execution, "working_quantity", None)
    portfolio = Portfolio(data, initial_cash=initial_cash, trade_size=trade_size,
                          pending=pending, **(portfolio_kwargs or {}))
    equity = Backtest(data, strategy, portfolio, execution).run()
    if hasattr(execution, "finalize"):
        execution.finalize()
    equity.attrs["n_fills"] = portfolio.n_fills
    equity.attrs["execution"] = execution
    return equity


def fill_timing_comparison(prices: pd.DataFrame, opens: pd.DataFrame, strategy_cls,
                           trade_size: int, **kwargs) -> dict:
    """Same signals, same costs — only WHEN the fill happens changes.

    README calls same-bar-close fills "optimistic": you saw the close and
    then traded at it, which no real order can do. This runs the identical
    backtest twice, swapping only the execution handler, to put a number on
    that optimism instead of just asserting it.
    """
    same_bar = run(prices, strategy_cls, trade_size,
                   execution_cls=SimulatedExecutionHandler, **kwargs)
    next_open = run(prices, strategy_cls, trade_size, opens=opens,
                    execution_cls=NextBarOpenExecutionHandler, **kwargs)
    return {
        "same_bar_close": stats(same_bar),
        "next_bar_open": stats(next_open),
    }


def vol_target_comparison(prices: pd.DataFrame, strategy_cls, trade_size: int,
                          targets=(0.05, 0.10, 0.15, 0.20), **kwargs) -> list[dict]:
    """Fixed share count vs volatility-targeted sizing, same signals throughout.

    The strategy is untouched between runs — it emits the same LONG and EXIT
    events on the same bars either way, because it reads prices and nothing
    else. What changes is how many shares those events turn into, and whether
    the size is revisited while the position is open.

    Realized vol is reported next to the target because the target is a
    forecast: it's set from a trailing 20-day estimate, and the future is not
    obliged to look like the last 20 days. The gap between the two columns is
    the honest measure of how well a rear-view estimate steers.
    """
    rows = []
    fixed = run(prices, strategy_cls, trade_size, **kwargs)
    rows.append({"label": f"fixed {trade_size} sh", "target": None,
                 **stats(fixed), "realized_vol": realized_vol(fixed),
                 "fills": fixed.attrs["n_fills"]})

    for target in targets:
        equity = run(prices, strategy_cls, trade_size,
                     portfolio_kwargs={"vol_target": target}, **kwargs)
        rows.append({"label": f"vol target {target:.0%}", "target": target,
                     **stats(equity), "realized_vol": realized_vol(equity),
                     "fills": equity.attrs["n_fills"]})
    return rows


def cost_sensitivity(prices: pd.DataFrame, strategy_cls, trade_size: int,
                     base_slippage_bps: float = 2.0, base_commission: float = 0.005,
                     multipliers=(1, 5, 10, 25, 50, 100), **kwargs) -> list[dict]:
    """How much of the result is costs, and at what multiple of realistic
    costs would that stop being true?

    The strategy decides WHEN to trade from price alone (moving averages),
    never from cash or fill price, so the sequence of buy/sell bars is
    identical at every cost level. That means the gap between a run's final
    equity and a same-signals zero-cost run's final equity is exactly the
    dollar cost of trading — nothing else about the run is moving.
    """
    zero_cost_final = run(prices, strategy_cls, trade_size, slippage_bps=0.0,
                          commission_per_share=0.0, **kwargs).iloc[-1]

    rows = []
    for m in multipliers:
        equity = run(prices, strategy_cls, trade_size,
                    slippage_bps=base_slippage_bps * m,
                    commission_per_share=base_commission * m, **kwargs)
        s = stats(equity)
        rows.append({"multiplier": m, "sharpe": s["sharpe"],
                    "total_return": s["total_return"],
                    "cost": zero_cost_final - equity.iloc[-1]})
    return rows


def capacity_sweep(prices: pd.DataFrame, opens: pd.DataFrame, volumes: pd.DataFrame,
                   strategy_cls, aums=(1e8, 1e9, 5e9, 2e10, 1e11),
                   participation: float = 0.10, **kwargs) -> list[dict]:
    """The same strategy at rising assets under management.

    Everything else in this repo quotes results at an implicit zero AUM: fills
    are instant, complete, and priced identically whether you trade a hundred
    shares or ten million. That makes every Sharpe in RESULTS.md an upper
    bound, and how tight a bound is a question with an answer.

    So: size the position to a given amount of capital, cap fills at
    `participation` of each bar's volume, charge square-root impact on every
    slice, and find where the strategy stops working. Every row is fully
    invested when long, which is a more aggressive configuration than the 200
    shares used elsewhere in this file - capacity is a question about a real
    book, and a book that leaves 60% of itself in cash has no capacity problem
    to study.

    The first row is the same fully-invested backtest with the unconstrained
    next-bar-open handler. It is AUM-independent by construction, so it is the
    zero-impact reference every other row is measured against.

    Returns one record per AUM level, with the execution diagnostics attached.
    A capacity claim without the fill delay beside it is not checkable.
    """
    first_price = float(prices.iloc[0, 0])
    median_notional = float((volumes.iloc[:, 0] * prices.iloc[:, 0]).median())

    def fully_invested(aum: float) -> int:
        return max(int(aum / first_price), 1)

    baseline = run(prices, strategy_cls, trade_size=fully_invested(1e6),
                   initial_cash=1e6, opens=opens,
                   execution_cls=NextBarOpenExecutionHandler, **kwargs)
    rows = [{"aum": None, "label": "no liquidity limit", **stats(baseline),
             "slices": None, "max_delay": None, "stranded": 0.0,
             "position_in_days": 0.0}]

    for aum in aums:
        equity = run(prices, strategy_cls, trade_size=fully_invested(aum),
                     initial_cash=aum, opens=opens, volumes=volumes,
                     execution_cls=ParticipationLimitedExecutionHandler,
                     execution_kwargs={"participation": participation}, **kwargs)
        ex = equity.attrs["execution"]
        rows.append({
            "aum": aum,
            "label": f"${aum / 1e9:,.1f}B",
            **stats(equity),
            "slices": ex.slices,
            "max_delay": ex.max_delay_bars,
            "stranded": ex.unfilled_shares * float(prices.iloc[-1, 0]),
            # The position as a multiple of a median day's dollar volume. This
            # is the number that decides every other number in the row: "$20
            # billion" means nothing on its own, "0.9 days of volume" means
            # the whole position takes nine days to trade at a 10% cap.
            "position_in_days": aum / median_notional,
        })
    return rows


def print_capacity(rows: list[dict], header: str) -> None:
    base = rows[0]
    print(f"\n{header}")
    print(f"{'AUM':<20} {'days of volume':>15} {'return':>9} {'sharpe':>8} "
          f"{'vs base':>8} {'max dd':>9} {'slices':>7} {'slowest':>9} {'stranded':>11}")
    for row in rows:
        if row["aum"] is None:
            print(f"{row['label']:<20} {'-':>15} {row['total_return']:>9.2%} "
                  f"{row['sharpe']:>8.2f} {'-':>8} {row['max_dd']:>9.2%} "
                  f"{'-':>7} {'-':>9} {'-':>11}")
            continue
        drop = row["sharpe"] - base["sharpe"]
        print(f"{row['label']:<20} {row['position_in_days']:>15.2f} "
              f"{row['total_return']:>9.2%} {row['sharpe']:>8.2f} {drop:>+8.2f} "
              f"{row['max_dd']:>9.2%} {row['slices']:>7} "
              f"{row['max_delay']:>8}d {row['stranded'] / 1e6:>10.1f}M")


def param_grid(prices: pd.DataFrame, trade_size: int,
               short_windows=(10, 20, 30, 50, 75),
               long_windows=(50, 100, 150, 200, 250),
               **kwargs) -> list[dict]:
    """Sharpe of MovingAverageCrossStrategy across a grid of (short, long)
    windows, short < long only.

    RESULTS.md picks 50/200 "by convention rather than fitted" and is
    explicit that fitting the windows on this same sample would be data
    snooping. This does not fit anything - it does not pick the best cell
    and rerun with it. It just asks a narrower question: is 50/200
    unremarkable among nearby honest choices, or did the 50/200 convention
    get lucky on this particular sample? A convention that only looks good
    at exactly one point in a smooth grid is a convention I would not
    trust in a different sample either.
    """
    rows = []
    for short in short_windows:
        for long in long_windows:
            if short >= long:
                continue
            equity = run(prices, MovingAverageCrossStrategy, trade_size,
                        short_window=short, long_window=long, **kwargs)
            s = stats(equity)
            rows.append({"short": short, "long": long,
                        "sharpe": s["sharpe"], "total_return": s["total_return"],
                        "max_dd": s["max_dd"]})
    return rows


WF_TRAIN_YEARS = 3


def _walk_forward_windows(prices: pd.DataFrame, train_years: int):
    """Yield (test_year, train_slice, train_plus_test_slice) for each fold.

    The third slice exists because the engine replays from the first bar it
    is handed, and a 200-day moving average needs 200 bars before it says
    anything. Handing the test year on its own would spend most of it in
    warmup. Handing it the training years as a prefix and then keeping only
    the test year's segment of the equity curve gives the strategy its
    warmup without letting any of the training period into the score.
    """
    years = sorted({ts.year for ts in prices.index})
    for test_year in years[train_years:]:
        train = prices.loc[str(test_year - train_years):str(test_year - 1)]
        through_test = prices.loc[str(test_year - train_years):str(test_year)]
        yield test_year, train, through_test


def _chain(segments: list[pd.Series]) -> pd.Series:
    """Glue per-year equity segments into one curve starting at 1.0.

    Each segment is rebased to its own first bar before chaining, so a year's
    contribution is its return and nothing else. Without rebasing, a year that
    started with more capital than the last one ended with would quietly
    inject money into the curve.
    """
    out, level = [], 1.0
    for seg in segments:
        rebased = seg / seg.iloc[0] * level
        out.append(rebased)
        level = rebased.iloc[-1]
    return pd.concat(out)


def walk_forward_selection(prices: pd.DataFrame, trade_size: int,
                           train_years: int = WF_TRAIN_YEARS,
                           short_windows=(10, 20, 30, 50, 75),
                           long_windows=(50, 100, 150, 200, 250),
                           **kwargs) -> dict:
    """Refit the MA windows every year on trailing data, trade them the next.

    param_grid() answers "is 50/200 unremarkable among nearby choices" and is
    careful to say it fits nothing. This is the other half of that question,
    and the one a trader actually faces: if you *did* fit the windows, using
    only data you had at the time, would you be better off?

    Every year, score all 23 (short, long) pairs on the trailing `train_years`
    of data, take the best by Sharpe, and trade exactly that pair for the next
    twelve months. No peeking: the choice for 2021 is made from 2018-2020 and
    is never revised once 2021 starts. Then chain the out-of-sample years into
    one curve and compare it to three things:

      - fixed 50/200, the convention, over the same out-of-sample span
      - buy and hold over the same span
      - the single best pair over the WHOLE sample, chosen with hindsight

    That last one is not a strategy anyone could trade. It is the ceiling: the
    number a grid search reports when nobody asks which data it used. The
    distance between it and the walk-forward curve is the part of a tuned
    backtest that does not survive contact with an unseen year.
    """
    pairs = [(s, l) for s in short_windows for l in long_windows if s < l]
    picks, segments = [], []

    for test_year, train, through_test in _walk_forward_windows(prices, train_years):
        scored = []
        for short, long in pairs:
            equity = run(train, MovingAverageCrossStrategy, trade_size,
                         short_window=short, long_window=long, **kwargs)
            scored.append((stats(equity)["sharpe"], short, long))
        in_sample_sharpe, short, long = max(scored)

        full = run(through_test, MovingAverageCrossStrategy, trade_size,
                   short_window=short, long_window=long, **kwargs)
        segment = full.loc[str(test_year)]
        segments.append(segment)
        picks.append({
            "year": test_year, "short": short, "long": long,
            "train_sharpe": in_sample_sharpe,
            "test_return": segment.iloc[-1] / segment.iloc[0] - 1,
        })

    first_test = picks[0]["year"]
    oos = prices.loc[str(first_test):]
    fixed = run(prices.loc[str(first_test - train_years):],
                MovingAverageCrossStrategy, trade_size,
                short_window=50, long_window=200, **kwargs).loc[str(first_test):]
    bh = run(oos, BuyAndHoldStrategy, trade_size, **kwargs)

    hindsight = max(
        ((stats(run(prices, MovingAverageCrossStrategy, trade_size,
                    short_window=s, long_window=l, **kwargs))["sharpe"], s, l)
         for s, l in pairs))

    return {
        "picks": picks,
        "walk_forward": _chain(segments),
        "fixed_50_200": fixed / fixed.iloc[0],
        "buy_and_hold": bh / bh.iloc[0],
        "hindsight_best": {"sharpe": hindsight[0], "short": hindsight[1],
                           "long": hindsight[2]},
        "n_pairs": len(pairs),
    }


def report(label: str, equity: pd.Series) -> dict:
    s = stats(equity)
    print(f"{label:<28} {s['final']:>12,.0f} {s['total_return']:>9.2%} "
          f"{s['sharpe']:>8.2f} {s['max_dd']:>9.2%}")
    return s


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-download", action="store_true",
                        help="skip the SPY run (no network)")
    parser.add_argument("--param-grid", action="store_true",
                        help="also run the (short, long) window grid on SPY and exit")
    parser.add_argument("--fill-timing", action="store_true",
                        help="compare same-bar-close vs next-bar-open fills on SPY and exit")
    parser.add_argument("--vol-target", action="store_true",
                        help="compare fixed sizing against volatility targeting on SPY and exit")
    parser.add_argument("--capacity", action="store_true",
                        help="run the same strategy at rising AUM with a volume "
                             "participation limit on SPY, then exit")
    parser.add_argument("--walk-forward", action="store_true",
                        help="refit the MA windows yearly on trailing data and trade them "
                             "out of sample on SPY, then exit")
    args = parser.parse_args()

    print(f"{'run':<28} {'final equity':>12} {'return':>9} {'sharpe':>8} {'max dd':>9}")
    print("-" * 70)

    syn = make_prices()
    syn_ma = run(syn, MovingAverageCrossStrategy, trade_size=500,
                 short_window=10, long_window=30)
    report("Synthetic - MA cross", syn_ma)
    syn_bh = run(syn, BuyAndHoldStrategy, trade_size=500)
    report("Synthetic - buy & hold", syn_bh)

    spy = spy_ma = spy_bh = None
    if not args.no_download:
        spy = fetch_prices()
        spy_ma = run(spy, MovingAverageCrossStrategy, trade_size=200,
                     short_window=50, long_window=200)
        report("SPY 2015-2024 - MA cross", spy_ma)
        spy_bh = run(spy, BuyAndHoldStrategy, trade_size=200)
        report("SPY 2015-2024 - buy & hold", spy_bh)

        print("\nCost sensitivity - SPY MA cross at multiples of the base "
              "2bps slippage / $0.005 commission:")
        print(f"{'multiplier':>10} {'sharpe':>8} {'return':>9} {'total cost':>11}")
        for row in cost_sensitivity(spy, MovingAverageCrossStrategy, trade_size=200,
                                    short_window=50, long_window=200):
            print(f"{row['multiplier']:>9}x {row['sharpe']:>8.2f} "
                  f"{row['total_return']:>9.2%} {row['cost']:>10,.0f}$")
        print("RESULTS.md says costs aren't the story ($102 against a $14,371 gap) -")
        print("this is what it would take for that to stop being true.")

        if args.param_grid:
            print("\nParameter grid - Sharpe by (short, long) window, SPY 2015-2024:")
            grid = param_grid(spy, trade_size=200)
            grid.sort(key=lambda r: r["sharpe"], reverse=True)
            print(f"{'short':>6} {'long':>6} {'sharpe':>8} {'return':>9} {'max dd':>9}")
            for row in grid:
                marker = "  <- convention" if (row["short"], row["long"]) == (50, 200) else ""
                print(f"{row['short']:>6} {row['long']:>6} {row['sharpe']:>8.2f} "
                      f"{row['total_return']:>9.2%} {row['max_dd']:>9.2%}{marker}")
            bh_sharpe = stats(spy_bh)["sharpe"]
            beat_bh = sum(1 for r in grid if r["sharpe"] > bh_sharpe)
            print(f"\n{beat_bh}/{len(grid)} grid cells beat buy & hold (Sharpe "
                  f"{bh_sharpe:.2f}). 50/200 rank by Sharpe: "
                  f"{[i for i, r in enumerate(grid, 1) if (r['short'], r['long']) == (50, 200)][0]}"
                  f" of {len(grid)}.")
            return

        if args.capacity:
            opens, volumes = fetch_opens(), fetch_volumes()
            print("\nCapacity - SPY, fills capped at 10% of each bar's volume,")
            print("square-root impact on every slice. Fully invested when long.")
            for short, long in ((50, 200), (10, 50)):
                rows = capacity_sweep(spy, opens, volumes,
                                      MovingAverageCrossStrategy,
                                      short_window=short, long_window=long)
                print_capacity(rows, f"{short}/{long} crossover "
                                     f"({rows[-1]['slices']} slices at the top size):")
            print("\n'days of volume' is the whole position divided by SPY's median daily")
            print("dollar volume, so at a 10% cap it is a tenth of the trading days one")
            print("full entry or exit takes. 'slowest' is the longest a single order")
            print("actually took; 'stranded' is notional still working when data ran out.")
            print("\nCapacity is a property of turnover, not of size. Compare the two")
            print("tables at the same AUM: the strategy that trades five times as often")
            print("pays the impact bill five times as often for the same position.")
            return

        if args.vol_target:
            print("\nPosition sizing - SPY MA cross, fixed share count vs "
                  "volatility targeting:")
            print(f"{'sizing':<18} {'target':>7} {'realized':>9} {'return':>9} "
                  f"{'sharpe':>8} {'max dd':>9} {'fills':>7}")
            for row in vol_target_comparison(spy, MovingAverageCrossStrategy,
                                             trade_size=200, short_window=50,
                                             long_window=200):
                tgt = f"{row['target']:.0%}" if row["target"] is not None else "-"
                print(f"{row['label']:<18} {tgt:>7} {row['realized_vol']:>9.2%} "
                      f"{row['total_return']:>9.2%} {row['sharpe']:>8.2f} "
                      f"{row['max_dd']:>9.2%} {row['fills']:>7}")
            return

        if args.walk_forward:
            wf = walk_forward_selection(spy, trade_size=200)
            print(f"\nWalk-forward parameter selection - SPY, best of {wf['n_pairs']} "
                  f"(short, long) pairs by Sharpe on the trailing {WF_TRAIN_YEARS} "
                  f"years, traded the next one:")
            print(f"{'year':>6} {'picked':>10} {'train sharpe':>13} {'that year OOS':>14}")
            for p in wf["picks"]:
                pair = f"{p['short']}/{p['long']}"
                print(f"{p['year']:>6} {pair:>10} {p['train_sharpe']:>13.2f} "
                      f"{p['test_return']:>14.2%}")

            print(f"\n{'curve':<28} {'return':>9} {'sharpe':>8} {'max dd':>9}")
            for label, key in [("walk-forward selected", "walk_forward"),
                               ("fixed 50/200", "fixed_50_200"),
                               ("buy & hold", "buy_and_hold")]:
                s = stats(wf[key])
                print(f"{label:<28} {s['total_return']:>9.2%} {s['sharpe']:>8.2f} "
                      f"{s['max_dd']:>9.2%}")
            hb = wf["hindsight_best"]
            print(f"{'best pair, whole sample':<28} {'-':>9} {hb['sharpe']:>8.2f} "
                  f"{'-':>9}  <- hindsight only, {hb['short']}/{hb['long']}")

            picks = wf["picks"]
            mean_train = sum(p["train_sharpe"] for p in picks) / len(picks)
            changes = sum(1 for a, b in zip(picks, picks[1:])
                          if (a["short"], a["long"]) != (b["short"], b["long"]))
            print(f"\nThe selection averaged {mean_train:.2f} Sharpe on the data it was "
                  f"chosen on and delivered")
            print(f"{stats(wf['walk_forward'])['sharpe']:.2f} on the data it was not. It "
                  f"changed its pick at {changes} of {len(picks) - 1} handovers.")
            return

        if args.fill_timing:
            spy_opens = fetch_opens()
            print("\nFill timing - SPY MA cross, same-bar-close vs next-bar-open, "
                  "same signals and costs:")
            cmp = fill_timing_comparison(spy, spy_opens, MovingAverageCrossStrategy,
                                         trade_size=200, short_window=50, long_window=200)
            print(f"{'fill':<16} {'final equity':>12} {'return':>9} {'sharpe':>8} {'max dd':>9}")
            for label, key in [("same-bar close", "same_bar_close"),
                               ("next-bar open", "next_bar_open")]:
                s = cmp[key]
                print(f"{label:<16} {s['final']:>12,.0f} {s['total_return']:>9.2%} "
                      f"{s['sharpe']:>8.2f} {s['max_dd']:>9.2%}")
            gap = cmp["same_bar_close"]["final"] - cmp["next_bar_open"]["final"]
            print(f"\nWaiting one extra bar to fill costs ${gap:,.0f} here - see "
                  "RESULTS.md for what that number does and doesn't mean.")
            return

    n_panels = 2 if spy is None else 4
    fig, axes = plt.subplots(n_panels, 1, figsize=(11, 3 * n_panels))
    syn["SYN"].plot(ax=axes[0], title="Synthetic price (cyclical by construction)")
    syn_ma.plot(ax=axes[1], label="MA cross")
    syn_bh.plot(ax=axes[1], label="buy & hold", ls="--")
    axes[1].set_title("Synthetic - equity"); axes[1].legend()
    if spy is not None:
        spy["SPY"].plot(ax=axes[2], title="SPY close")
        spy_ma.plot(ax=axes[3], label="MA cross")
        spy_bh.plot(ax=axes[3], label="buy & hold", ls="--")
        axes[3].set_title("SPY - equity"); axes[3].legend()
    plt.tight_layout()
    plt.savefig("engine_backtest.png", dpi=120)
    print("\nSaved plot to engine_backtest.png")


if __name__ == "__main__":
    main()
