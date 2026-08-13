# Results

All 18 tests pass (`python -m pytest -q`). Numbers below are the output of
`python run_backtest.py`, reproducible from a clean checkout.

Setup: $100,000 starting cash, 2 bps slippage, $0.005/share commission, risk-free rate 0.

| Run | Strategy | Final equity | Return | Sharpe | Max DD |
|---|---|---|---|---|---|
| Synthetic sine wave, 500 bars | MA cross 10/30 | 117,408 | **+17.41%** | **4.10** | −2.09% |
| Synthetic sine wave, 500 bars | Buy & hold | 98,223 | −1.76% | −0.24 | −10.83% |
| SPY, 2015-01-02 → 2024-12-30 | MA cross 50/200 | 167,347 | +67.35% | 0.75 | −17.33% |
| SPY, 2015-01-02 → 2024-12-30 | Buy & hold | 181,718 | **+81.73%** | 0.77 | −16.28% |

Same engine. Same strategy class. Same parameters modulo window length. A Sharpe of 4.10 on
one dataset and 0.75 on the other.

## The synthetic number is meaningless, and that is the point

`make_prices()` builds a drifting sine wave. It is *cyclical by construction* — a trend-following
strategy on data that trends in clean, regular, mean-reverting arcs is being graded on an exam
it wrote itself. Sharpe 4.10 is not evidence the strategy works; it is evidence the engine works,
which is the only thing synthetic data can honestly tell you.

That is still worth having. When the machinery is wrong you cannot tell whether a bad number is
a bad strategy or a bad backtester. Debugging on data where you already know the answer separates
the two questions. Then you go to real prices and expect the number to collapse.

## What actually happened on SPY

Nine fills in ten years — four and a half round trips. The full log:

| Date | Action | Price |
|---|---|---|
| 2015-12-09 | buy 200 | 172.12 |
| 2016-01-15 | sell 200 | 158.30 |
| 2016-04-20 | buy 200 | 178.08 |
| 2018-12-12 | sell 200 | 236.05 |
| 2019-03-26 | buy 200 | 252.64 |
| 2020-03-31 | sell 200 | 236.28 |
| 2020-07-06 | buy 200 | 292.03 |
| 2022-03-16 | sell 200 | 409.80 |
| 2023-01-26 | buy 200 | 387.09 |

Time in market: **74%**. Total transaction costs: **$102** ($9 commission + $93 slippage) on
$14,371 of underperformance. **Costs are not the story.** Anyone who blames the shortfall on
frictions hasn't read the trade log.

The story is the whipsaws — every sell-then-buy-higher pair, priced out:

| Round trip | Out at | Back in at | Cost (200 sh) |
|---|---|---|---|
| 2016 | 158.30 | 178.08 | −$3,956 |
| 2018–19 | 236.05 | 252.64 | −$3,318 |
| **2020 COVID** | **236.28** | **292.03** | **−$11,150** |
| 2022 bear | 409.80 | 387.09 | **+$4,542** |
| | | **net** | **−$13,882** |

Add $102 of costs and $486 of entry-timing drag (buy-and-hold got in at 169.69 on day one; the
crossover waited until 172.12 after its 200-day warmup) and you have $14,470 — essentially the
entire $14,371 gap. The underperformance is fully explained by four decisions.

The 2020 round trip alone is 78% of it. The 50-day crossed below the 200-day on March 31, after
the crash had already happened, and crossed back on July 6, after the recovery had already
happened. The strategy sold near the bottom and bought back 24% higher. That is not bad luck;
it is what a lagging indicator does to a V-shaped move, and it is structural.

The 2022 trade is the counterexample that keeps this honest: a slow grinding bear market is
exactly the regime a 50/200 crossover is built for, and there it made $4,542. The strategy is
not useless. It is regime-dependent, and 2015–2024 contained one regime it likes and one it
does not.

## What I would say the strategy is worth

Nothing, as traded here. Sharpe 0.75 versus 0.77 for doing nothing, with a *deeper* drawdown
(−17.3% vs −16.3%) — it took more pain for less money. Being out of the market 26% of the time
should reduce drawdown; it didn't, because it was out during the wrong 26%.

The defensible version of the claim is narrower: a 50/200 crossover reduces exposure to slow
bear markets at the cost of missing sharp recoveries, and over 2015–2024 the sharp recovery cost
more than the slow bear market saved.

## Engine correctness

The end-to-end test is the one I trust. Buy-and-hold with zero slippage and zero commission has
a closed-form answer:

```
equity = 100,000 + 100 shares × (110 − 100) = 101,000
```

`tests/test_engine.py` checks that to the cent, and separately checks that bar 1's equity is
100,100 — which only holds if `mark_to_market` runs *after* the bar's fills are processed. An
engine that marks first still passes the final-value check but fails on bar 1. That was the bug
worth writing a test for.

## What is not modeled

- Fills are complete, instant, at any size, at the same bar's close. No liquidity constraint.
- Slippage scales with price, not with order size relative to volume.
- No borrow costs, margin, or taxes.
- Daily bars only. Intraday, the crossover dates would move.
- One parameter pair (50/200), chosen by convention rather than fitted. That is deliberate —
  fitting the windows on this same 2015–2024 sample would produce a better number and a worse
  answer.
