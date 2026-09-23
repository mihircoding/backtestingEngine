"""Builds docs/data.js for the GitHub Pages site.

The site plots real numbers, not numbers typed into HTML by hand - so it
gets them the only honest way, by running the same backtests RESULTS.md
runs and dumping the output. Re-run this after changing the engine and the
site updates with it:

    python docs/build_data.py

or rebuild one section and leave the rest of data.js alone:

    python docs/build_data.py --only capacity

The second form exists because yfinance re-adjusts the whole price history
every time SPY pays a dividend. A full rebuild months later scales every
2015-2024 price by the same small factor (0.25% as of September 2026), and since the
backtests trade a fixed share count with a per-share commission, every dollar
figure on the page moves with it while the copy quoting them doesn't.
Returns are unaffected; the dollars are not.
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from run_backtest import (capacity_sweep, cost_sensitivity, fetch_opens, fetch_prices,
                          fetch_volumes, make_opens, make_prices, param_grid,
                          realized_vol, run, significance_report, stats,
                          vol_target_comparison, walk_forward_selection)
from src.execution import NextBarOpenExecutionHandler
from src.strategy import BuyAndHoldStrategy, MovingAverageCrossStrategy

OUT = Path(__file__).resolve().parent / "data.js"


def series(equity, every=1):
    """(date, value) pairs, thinned, rounded - the chart can't resolve more."""
    eq = equity.iloc[::every]
    return [[d.strftime("%Y-%m-%d"), round(float(v), 2)] for d, v in eq.items()]


def rounded(row):
    return {k: (round(float(v), 4) if isinstance(v, float) else v)
            for k, v in row.items()}


def capacity(spy=None):
    """Both crossovers at rising AUM, fills capped at 10% of volume - the
    same sweep as `run_backtest.py --capacity`."""
    print("capacity (12 backtests)...")
    spy = fetch_prices() if spy is None else spy
    opens, volumes = fetch_opens(), fetch_volumes()
    out = {"participation": 0.10,
           "median_dollar_volume": round(float(
               (volumes["SPY"] * spy["SPY"]).median()), 0)}
    for key, short, long in (("slow", 50, 200), ("fast", 10, 50)):
        rows = capacity_sweep(spy, opens, volumes, MovingAverageCrossStrategy,
                              short_window=short, long_window=long)
        out[key] = {"short": short, "long": long,
                    "rows": [rounded(r) for r in rows]}
    return out


def histogram(values, bins=40):
    """Bin a bootstrap distribution for the chart, so the page ships 40
    numbers instead of 5,000."""
    counts, edges = np.histogram(np.asarray(values, dtype=float), bins=bins)
    centres = (edges[:-1] + edges[1:]) / 2
    return {"x": [round(float(c), 4) for c in centres],
            "y": [int(n) for n in counts]}


def significance(spy=None):
    """Bootstrap intervals and the deflated Sharpe - `--significance`."""
    print("significance (23 backtests + 5,000 resamples)...")
    spy = fetch_prices() if spy is None else spy
    rep = significance_report(spy, trade_size=200)
    conv, best = rep["convention"], rep["deflated_best"]
    return {
        "pairs": rep["pairs"],
        "effective_pairs": round(float(rep["effective_pairs"]), 2),
        "sharpe_spread": round(float(rep["sharpe_spread"]), 4),
        "best_pair": f"{rep['best']['pair'][0]}/{rep['best']['pair'][1]}",
        "buy_and_hold_sharpe": round(float(rep["buy_and_hold_sharpe"]), 4),
        "convention": {k: round(float(conv[k]), 4)
                       for k in ("sharpe", "lo", "hi", "p_le_zero")},
        "distribution": histogram(conv["draws"]),
        "vs_buy_and_hold": {k: round(float(rep["vs_buy_and_hold"][k]), 4)
                            for k in ("diff", "lo", "hi", "p_le_zero")},
        "best_vs_buy_and_hold": {k: round(float(rep["best_vs_buy_and_hold"][k]), 4)
                                 for k in ("diff", "lo", "hi", "p_le_zero")},
        "deflated": {
            "sharpe": round(float(best["sharpe"]), 4),
            "threshold": round(float(best["threshold"]), 4),
            "psr_vs_zero": round(float(best["psr_vs_zero"]), 4),
            "dsr": round(float(best["dsr"]), 4),
            "dsr_vs_bh": round(float(rep["deflated_vs_bh"]["dsr"]), 4),
            "bar_vs_bh": round(float(rep["deflated_vs_bh"]["bar"]), 4),
        },
    }


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

    print("walk-forward selection (~170 backtests, this is the slow one)...")
    wf = walk_forward_selection(spy, trade_size=200)

    print("position sizing (5 backtests)...")
    sizing = vol_target_comparison(spy, MovingAverageCrossStrategy, trade_size=200,
                                   short_window=50, long_window=200)
    # one curve per sizing rule, for the chart
    sizing_curves = {
        "fixed": series(spy_ma, 3),
        "vt10": series(run(spy, MovingAverageCrossStrategy, trade_size=200,
                           portfolio_kwargs={"vol_target": 0.10},
                           short_window=50, long_window=200), 3),
        "vt5": series(run(spy, MovingAverageCrossStrategy, trade_size=200,
                          portfolio_kwargs={"vol_target": 0.05},
                          short_window=50, long_window=200), 3),
    }

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
        "sizing": {
            "rows": [{k: (round(float(v), 4) if isinstance(v, float) else v)
                      for k, v in row.items()} for row in sizing],
            "curves": sizing_curves,
        },
        "walk_forward": {
            "train_years": 3,
            "n_pairs": wf["n_pairs"],
            "picks": [{k: (round(float(v), 4) if isinstance(v, float) else v)
                       for k, v in p.items()} for p in wf["picks"]],
            "curves": {
                "selected": series(wf["walk_forward"], 3),
                "fixed": series(wf["fixed_50_200"], 3),
                "bh": series(wf["buy_and_hold"], 3),
            },
            "stats": {name: {k: round(float(v), 4) for k, v in stats(wf[key]).items()}
                      for name, key in [("selected", "walk_forward"),
                                        ("fixed", "fixed_50_200"),
                                        ("bh", "buy_and_hold")]},
            "hindsight": {k: (round(float(v), 4) if isinstance(v, float) else v)
                          for k, v in wf["hindsight_best"].items()},
        },
        "capacity": capacity(spy),
        "significance": significance(spy),
    }

    write(data)
    print(f"wrote {OUT} ({OUT.stat().st_size / 1024:.0f} KB)")
    print(f"  SPY MA sharpe {data['spy']['ma_stats']['sharpe']:.2f} | "
          f"B&H {data['spy']['bh_stats']['sharpe']:.2f} | "
          f"next-open {data['spy']['nbo_stats']['sharpe']:.2f}")
    print(f"  {len(trades)} fills, {len(grid)} grid cells")
    print("  sizing: " + " | ".join(
        f"{r['label']} vol {r['realized_vol']:.1%} sharpe {r['sharpe']:.2f}"
        for r in sizing))
    wfs = data["walk_forward"]["stats"]
    print(f"  walk-forward: selected {wfs['selected']['sharpe']:.2f} | "
          f"fixed 50/200 {wfs['fixed']['sharpe']:.2f} | "
          f"buy & hold {wfs['bh']['sharpe']:.2f} | "
          f"hindsight {data['walk_forward']['hindsight']['sharpe']:.2f}")
    report_capacity(data["capacity"])
    report_significance(data["significance"])


def write(data):
    OUT.write_text("window.DATA = " + json.dumps(data, separators=(",", ":")) + ";\n",
                   encoding="utf-8")


def report_capacity(cap):
    for key in ("slow", "fast"):
        c = cap[key]
        print(f"  capacity {c['short']}/{c['long']}: " + " | ".join(
            f"{r['label']} sharpe {r['sharpe']:.2f} dd {r['max_dd']:.1%}"
            for r in c["rows"]))


def report_significance(sig):
    c = sig["convention"]
    print(f"  significance: 50/200 sharpe {c['sharpe']:.2f} "
          f"[{c['lo']:.2f}, {c['hi']:.2f}] | best {sig['best_pair']} "
          f"dsr {sig['deflated']['dsr']:.1%} vs zero, "
          f"{sig['deflated']['dsr_vs_bh']:.1%} vs buy & hold")


SECTIONS = {"capacity": capacity, "significance": significance}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", choices=sorted(SECTIONS),
                        help="rebuild one section and keep the rest of data.js as is")
    args = parser.parse_args()
    if args.only:
        text = OUT.read_text(encoding="utf-8")
        data = json.loads(text[text.index("=") + 1:].strip().rstrip(";"))
        data[args.only] = SECTIONS[args.only]()
        write(data)
        print(f"wrote {OUT} ({OUT.stat().st_size / 1024:.0f} KB), "
              f"replaced '{args.only}' only")
        if args.only == "capacity":
            report_capacity(data["capacity"])
        elif args.only == "significance":
            report_significance(data["significance"])
    else:
        main()
