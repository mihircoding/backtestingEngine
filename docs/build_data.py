"""Builds docs/data.js for the GitHub Pages site.

The site plots real numbers, not numbers typed into HTML by hand - so it
gets them the only honest way, by running the same backtests RESULTS.md
runs and dumping the output. Re-run this after changing the engine and the
site updates with it:

    python docs/build_data.py
"""

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from run_backtest import (cost_sensitivity, fetch_opens, fetch_prices, make_opens,
                          make_prices, param_grid, run, stats)
from src.execution import NextBarOpenExecutionHandler
from src.strategy import BuyAndHoldStrategy, MovingAverageCrossStrategy

OUT = Path(__file__).resolve().parent / "data.js"


def series(equity, every=1):
    """(date, value) pairs, thinned, rounded - the chart can't resolve more."""
    eq = equity.iloc[::every]
    return [[d.strftime("%Y-%m-%d"), round(float(v), 2)] for d, v in eq.items()]


def main():
    print("synthetic...")
    syn = make_prices()
    syn_ma = run(syn, MovingAverageCrossStrategy, trade_size=500,
                 short_window=10, long_window=30)
    syn_bh = run(syn, BuyAndHoldStrategy, trade_size=500)

    print("SPY (downloading)...")
    spy = fetch_prices()
    spy_opens = fetch_opens()
    spy_ma = run(spy, MovingAverageCrossStrategy, trade_size=200,
                 short_window=50, long_window=200)
    spy_bh = run(spy, BuyAndHoldStrategy, trade_size=200)
    spy_nbo = run(spy, MovingAverageCrossStrategy, trade_size=200, opens=spy_opens,
                  execution_cls=NextBarOpenExecutionHandler,
                  short_window=50, long_window=200)

    print("cost sensitivity...")
    costs = cost_sensitivity(spy, MovingAverageCrossStrategy, trade_size=200,
                             short_window=50, long_window=200)

    print("parameter grid (23 backtests)...")
    grid = param_grid(spy, trade_size=200)

    # Trade log: the MA-cross strategy's fills, recovered by replaying the
    # signal rather than instrumenting the engine (the engine deliberately
    # doesn't keep a trade blotter - the portfolio derives equity from cash
    # and positions instead).
    px = spy["SPY"]
    short_ma, long_ma = px.rolling(50).mean(), px.rolling(200).mean()
    above = (short_ma > long_ma)
    flips = above.ne(above.shift(1)) & short_ma.notna() & long_ma.notna()
    trades = []
    holding = False
    for date in px.index[flips]:
        going_long = bool(above.loc[date])
        if going_long == holding:
            continue
        trades.append({"date": date.strftime("%Y-%m-%d"),
                       "side": "buy" if going_long else "sell",
                       "price": round(float(px.loc[date]), 2)})
        holding = going_long

    data = {
        "generated": None,
        "synthetic": {
            "price": series(syn["SYN"], 2),
            "ma": series(syn_ma, 2),
            "bh": series(syn_bh, 2),
            "ma_stats": {k: round(float(v), 4) for k, v in stats(syn_ma).items()},
            "bh_stats": {k: round(float(v), 4) for k, v in stats(syn_bh).items()},
        },
        "spy": {
            "price": series(spy["SPY"], 3),
            "ma": series(spy_ma, 3),
            "bh": series(spy_bh, 3),
            "next_open": series(spy_nbo, 3),
            "ma_stats": {k: round(float(v), 4) for k, v in stats(spy_ma).items()},
            "bh_stats": {k: round(float(v), 4) for k, v in stats(spy_bh).items()},
            "nbo_stats": {k: round(float(v), 4) for k, v in stats(spy_nbo).items()},
            "trades": trades,
        },
        "costs": [{k: (round(float(v), 4) if k != "multiplier" else v)
                   for k, v in row.items()} for row in costs],
        "grid": [{k: (round(float(v), 4) if isinstance(v, float) else v)
                  for k, v in row.items()} for row in grid],
    }

    OUT.write_text("window.DATA = " + json.dumps(data, separators=(",", ":")) + ";\n",
                   encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size / 1024:.0f} KB)")
    print(f"  SPY MA sharpe {data['spy']['ma_stats']['sharpe']:.2f} | "
          f"B&H {data['spy']['bh_stats']['sharpe']:.2f} | "
          f"next-open {data['spy']['nbo_stats']['sharpe']:.2f}")
    print(f"  {len(trades)} fills, {len(grid)} grid cells")


if __name__ == "__main__":
    main()
