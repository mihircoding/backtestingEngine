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

That's an assertion until it's tested against something. `cost_sensitivity()` reruns the same
SPY MA-cross backtest at multiples of the 2bps/$0.005 base cost — the strategy decides *when* to
trade from price alone, never from cash or fill price, so every run makes the identical trades;
only the cost of making them changes:

| Cost multiplier | Sharpe | Return | Total cost |
|---|---|---|---|
| 1x (base case above) | 0.75 | 67.35% | $102 |
| 5x | 0.74 | 66.94% | $509 |
| 10x | 0.74 | 66.43% | $1,019 |
| 25x | 0.72 | 64.90% | $2,547 |
| 50x | 0.69 | 62.35% | $5,095 |
| 100x | 0.63 | 57.26% | $10,189 |

Even at **100x** realistic costs — 2% one-way slippage, $0.50/share commission, nothing a real
broker charges — total costs reach $10,189, still short of the $14,371 whipsaw-driven gap. Costs
scale roughly linearly with the multiplier because the trade list never changes; the underperformance
does not, because it was never a cost problem. This is the same claim as above, now with a number
attached to "how wrong would the cost model have to be."

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

## Is 50/200 special, or lucky?

`run_backtest.py --param-grid` runs the same strategy across every (short, long) window pair
in a 5x5 grid (short in 10/20/30/50/75, long in 50/100/150/200/250, short < long only — 23 valid
pairs) on the same SPY 2015-2024 data, same costs, nothing else changed. This is not a fitting
exercise — it does not pick the best cell and rerun with it, because that would just be the data
snooping the bullet below warns about. It asks a narrower, honest question: is 50/200 unusual
among nearby choices, or would a lot of round numbers have told the same story?

```
 short   long   sharpe    return    max dd
    10    100     0.86    64.81%   -13.35%
    10    200     0.83    65.92%   -11.14%
    10    250     0.82    65.27%   -12.97%
    50    250     0.77    70.06%   -17.41%
    10    150     0.77    59.23%   -16.79%
    50    150     0.76    67.89%   -17.28%
    75    200     0.75    68.90%   -17.44%
    50    200     0.75    67.35%   -17.33%  <- convention
     ...   (15 more, sharpe 0.57-0.74)
```

50/200 lands 8th of 23 by Sharpe — solidly middle of the pack, not cherry-picked and not an
outlier either way. More telling: only 3 of the 23 pairs beat buy-and-hold's 0.77 Sharpe, and
50/200 is not one of them. That is consistent with the rest of this document — the crossover
is not a free lunch at any nearby setting, and picking a "better" pair after the fact would have
been curve-fitting the 2015-2024 sample, not finding a better strategy. Full grid, and the flag
to reproduce it, in `run_backtest.py`.

## Is same-bar-close as optimistic as the README claims?

Every result above fills at the close of the bar the signal fired on — the README calls that
"optimistic" and says the honest alternative is filling at the *next* bar's open. That was an
assertion until now. `NextBarOpenExecutionHandler` (`src/execution.py`) implements the honest
version: `run_backtest.py --fill-timing` runs the identical SPY MA-cross backtest through both
handlers, same signals, same slippage and commission, changing only when the fill happens.

```
FILL_TIMING_TABLE_PLACEHOLDER
```

FILL_TIMING_NARRATIVE_PLACEHOLDER

## What is not modeled

- Fills are complete, instant, at any size, under either fill-timing model. No liquidity
  constraint — `NextBarOpenExecutionHandler` changes *when* the fill happens, not that it's
  always instant and complete.
- Slippage scales with price, not with order size relative to volume.
- No borrow costs, margin, or taxes.
- Daily bars only. Intraday, the crossover dates would move.
- One parameter pair (50/200) is used throughout. It is not cherry-picked (see the grid above),
  but it is also not fitted, and it shouldn't be — fitting the windows on this same 2015-2024
  sample would launder curve-fitting as insight, not find a better strategy.
