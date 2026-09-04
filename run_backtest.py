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
from src.execution import SimulatedExecutionHandler
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


def fetch_prices(symbol: str = "SPY", start: str = "2015-01-01",
                 end: str = "2024-12-31") -> pd.DataFrame:
    import yfinance as yf

    raw = yf.download(symbol, start=start, end=end, auto_adjust=True,
                      progress=False)["Close"].dropna()
    return pd.DataFrame({symbol: raw.squeeze()})


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
       commission_per_share: float = 0.005, **kwargs) -> pd.Series:
    """One full backtest, fresh components each time (they carry state)."""
    data = HistoricalDataHandler(prices)
    strategy = strategy_cls(data, **kwargs)
    portfolio = Portfolio(data, initial_cash=100_000.0, trade_size=trade_size)
    execution = SimulatedExecutionHandler(data, slippage_bps=slippage_bps,
                                          commission_per_share=commission_per_share)
    return Backtest(data, strategy, portfolio, execution).run()


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
