# Results

All 72 tests pass (`python -m pytest -q`). Numbers below are the output of
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

## What if you did fit the windows, honestly?

The section above is careful to say it fits nothing. That leaves the obvious question unanswered,
and it is the question a trader actually faces: you have a grid, you have history, why not use
it? `run_backtest.py --walk-forward` does exactly that, under the one rule that makes it a fair
test — every choice is made from data that already existed when the choice was made.

Each January, score all 23 pairs on the trailing three years, take the best by Sharpe, and trade
that pair for the next twelve months. No revisions mid-year. Then chain the out-of-sample years
into a single curve.

```
  year     picked  train sharpe  that year OOS
  2018     75/250          1.66         -2.20%
  2019      20/50          1.22          4.84%
  2020      20/50          1.01          9.20%
  2021     10/100          1.04         16.12%
  2022     10/100          1.40        -12.74%
  2023     75/150          1.01         10.74%
  2024      10/50          0.78          8.69%

curve                           return   sharpe    max dd
walk-forward selected           36.56%     0.62   -16.64%
fixed 50/200                    53.80%     0.77   -17.33%
buy & hold                      68.48%     0.76   -18.17%
best pair, whole sample              -     0.86         -  <- hindsight only, 10/100
```

**Fitting the windows made it worse.** 0.62 against 0.77 for the convention nobody fitted, on
the same seven years, with the same costs. It also finished behind buy-and-hold by 32 percentage
points of return. The selection was given a real edge — a 23-cell menu and three years of
evidence — and turned it into a handicap.

**The training Sharpes are the tell.** They average 1.16. The realized number is 0.62. That gap
is not bad luck in one year; it is present in every fold, and it is what an in-sample optimum
is: the pair that best fit three years of noise, reported as if it were a property of the
strategy. Anyone showing a backtest with a tuned parameter and no walk-forward is showing the
1.16 column.

**It changed its mind at 4 of 6 handovers**, twice swinging between the fastest pair on the menu
(10/50) and the slowest (75/250). A parameter that genuinely mattered would be stable, because
the thing it measures — how long SPY trends for — does not reinvent itself every January. The
instability says the grid is mostly ranking noise, which is also why the winner doesn't repeat.

**The hindsight row is the ceiling, and it is a fiction.** 10/100 at 0.86 is the number a grid
search reports when nobody asks which data it used. Nothing in the walk-forward ever picked it
in 2018, when it would have mattered. The distance from 0.86 to 0.62 is the part of a tuned
backtest that does not survive an unseen year, measured rather than argued about.

The sober reading is not "walk-forward selection doesn't work." It is that walk-forward selection
cannot manufacture an edge that the parameter never had, and that this particular parameter never
had one — which the grid section already suspected and this section makes it possible to say with
a number attached. The cost of finding that out honestly was one extra function and 10 tests;
the cost of not finding it out is a resume line that a first interview question dismantles.

## Is same-bar-close as optimistic as the README claims?

Every result above fills at the close of the bar the signal fired on — the README calls that
"optimistic" and says the honest alternative is filling at the *next* bar's open. That was an
assertion until now. `NextBarOpenExecutionHandler` (`src/execution.py`) implements the honest
version: `run_backtest.py --fill-timing` runs the identical SPY MA-cross backtest through both
handlers, same signals, same slippage and commission, changing only when the fill happens.

```
fill             final equity    return   sharpe    max dd
same-bar close        167,347    67.35%     0.75   -17.33%
next-bar open         166,226    66.23%     0.73   -17.26%
```

Waiting one bar costs **$1,121** — 1.1 percentage points of return and 0.02 of Sharpe. That is
smaller than it sounds and larger than it looks. Smaller, because this strategy trades nine
times in ten years; an overnight gap is a coin flip, and nine coin flips average out to roughly
nothing. There is no systematic edge being given up here, just noise that happened to cost money
on this sample. Larger, because the *per-trade* number is what generalizes: $125 a round trip on
a $35,000 position is about 35bps, seventeen times the 2bps slippage assumption, and it lands on
every trade a strategy makes. Run something that trades daily instead of annually and the same
gap compounds into the difference between a live strategy and a backtest.

The number is also specific to a slow trend follower. A strategy whose signal comes *from* the
close — a mean-reversion rule that buys weakness, say — is systematically buying at prices that
gapped down, and next-bar-open fills would take a much bigger bite than a coin flip. The
conclusion to carry away is not "$1,121"; it is that the size of this correction depends on what
the signal is made of, so it has to be measured per strategy rather than assumed small.

## Does position sizing matter more than the signal?

Every number above uses a fixed 200 shares. That is a sizing rule, even though it looks like
the absence of one, and it has a property worth noticing: 200 shares of SPY was $34,000 of
exposure in 2015 and $92,000 in 2024. The book's risk quietly tripled over the sample without
anyone deciding it should.

Volatility targeting decides it on purpose. Size the position so that

```
shares x price x trailing_vol  =  equity x vol_target
```

and the position carries the same risk whether the market is calm or violent, in 2015 or 2024.
`Portfolio(vol_target=...)` implements it, and `run_backtest.py --vol-target` runs the identical
MA-cross signals at four targets:

```
sizing              target  realized    return   sharpe    max dd   fills
fixed 200 sh             -     7.26%    67.35%     0.75   -17.33%       9
vol target 5%           5%     5.11%    51.81%     0.85    -7.00%     165
vol target 10%         10%    10.17%   124.18%     0.85   -13.95%     161
vol target 15%         15%    15.06%   221.98%     0.85   -21.24%     155
vol target 20%         20%    19.27%   340.81%     0.87   -24.73%     136
```

Three things in that table.

**The targeting works.** Realized volatility lands within a fifth of a point of the target at
every setting, off a trailing 20-day estimate. That is the whole mechanism validated on real
data: a rear-view estimate of SPY's volatility is a good enough forecast to steer by.

**Sharpe is flat across targets, and that is the correct answer.** 0.85, 0.85, 0.85, 0.87.
Doubling the target doubles the return and doubles the volatility, so the ratio doesn't move —
which is exactly what theory says leverage does. A sizing change that appeared to improve Sharpe
as you turned it up would be evidence of a bug, not of alpha. What the knob actually chooses is
where on that line you want to sit: 5% target gives up two thirds of the return to cut the
drawdown from 17% to 7%.

**Sharpe went from 0.75 to 0.85 anyway, and it is not free.** The improvement comes from holding
constant risk instead of accidentally-increasing risk, not from better timing — the trade dates
are identical. It costs 150 extra fills. At the base 2bps/$0.005 cost assumption those fills are
already paid for in the table, but they are also 150 more chances for the liquidity assumptions
in "What is not modeled" below to be wrong, and a rule that rebalances into a crash is exactly
the rule that finds out the fills aren't free. The `rebalance_band` parameter exists for that
reason: at 0.2 a position is only resized once it is 20% away from target, which is what keeps
the fill count at 160 instead of 2,500.

## How much money does this hold?

Every result above this line is quoted at an implicit zero assets under management. Fills are
instant, complete, and priced identically whether the order is a hundred shares or ten million —
the README's own list of simplifications says so in its first line. That makes every Sharpe in
this file an upper bound, and the first question anyone asks about a strategy is how much money
it runs.

`ParticipationLimitedExecutionHandler` (`src/execution.py`) answers it by refusing to do two
things. It will not fill more than a set share of a bar's volume — 10% throughout below, which is
roughly what a trader working a large order will admit to — and it charges square-root market
impact on every slice:

```
impact (bps) = impact_coef x trailing daily volatility (bps) x sqrt(slice / bar volume)
```

That functional form is standard (Almgren et al. 2005); the constant in front is the part every
firm calibrates on its own fills and nobody publishes, so it is a named parameter with a
documented default of 1.0 rather than a number buried in the code. Volatility is the trailing
estimate the handler reads from the data, which means impact rises automatically in the markets
where liquidity is worst.

The remainder of an order stays working and continues on the next bar, at the next bar's price.
That delay, not the commission, is the real cost: you do not get the price you saw when you
decided. Anything still working when the data runs out is counted, not dropped — `stranded` below
— because a partially filled position is one the strategy believes it has and does not.

`run_backtest.py --capacity` sizes the book to a given amount of capital and runs the sweep. Note
that every row here is *fully invested when long*, which is more aggressive than the 200 shares
used everywhere else in this file; a book that leaves 60% of itself in cash has no capacity
problem to study, and that is why the return and drawdown columns differ from the tables above.
The first row is the same fully-invested backtest with the unconstrained next-bar-open handler,
which is AUM-independent by construction.

```
50/200 crossover                 (SPY median daily dollar volume: $22.6bn)
AUM                   days of volume    return   sharpe  vs base    max dd  slices   slowest
no liquidity limit                 -   195.14%     0.72        -   -38.19%       -         -
$0.1B                           0.00   193.49%     0.72    -0.01   -38.30%       9        1d
$1.0B                           0.04   189.90%     0.70    -0.02   -38.53%       9        1d
$5.0B                           0.22   188.96%     0.69    -0.03   -39.13%      30        5d
$20.0B                          0.88   190.34%     0.68    -0.04   -40.80%     111       21d
$100.0B                         4.42   192.01%     0.72    -0.00   -39.21%     520       90d

10/50 crossover
AUM                   days of volume    return   sharpe  vs base    max dd  slices   slowest
no liquidity limit                 -   142.55%     0.73        -   -18.62%       -         -
$0.1B                           0.00   133.53%     0.69    -0.04   -19.44%      61        1d
$1.0B                           0.04   114.73%     0.60    -0.13   -21.50%      69        2d
$5.0B                           0.22   106.22%     0.57    -0.16   -25.93%     227        7d
$20.0B                          0.88   120.04%     0.63    -0.10   -35.49%     652       36d
$100.0B                         4.42   145.88%     0.66    -0.07   -31.64%    1418      144d
```

**Capacity is a property of turnover, not of size.** Both tables hold the same instrument at the
same AUM under the same cap. The 50/200 strategy gives up 0.04 of Sharpe at $20 billion; the
10/50 strategy gives up 0.16 at a quarter of that. The difference is not the position — it is
that one of them trades nine times in ten years and the other trades sixty-one. Impact is a toll
paid per trade, so the capacity of a strategy is roughly its edge divided by its turnover, and
the AUM number on its own is close to meaningless.

**The second table is the more interesting one, and not because of Sharpe.** Look at the
drawdown column: −18.62% unconstrained, −35.49% at $20 billion. The 10/50 crossover's whole
appeal over the slower version is that it gets out of trouble faster, and a participation limit
takes that away first. It is not that the strategy gets worse at making money; it stops being the
thing it was. A liquidity constraint does not scale a strategy down uniformly, it removes
whichever property depended on trading quickly, and for a trend follower that property is the
exit.

**Both tables recover at $100 billion, which is a warning, not a result.** Sharpe is
non-monotone in AUM here because nine trades — or sixty-one — is not a sample. A fill delay is a
coin flip on each trade: sometimes the price you get while working the order for 90 days is
better than the one you saw. With this few trades those coin flips do not average out, and the
drop from 0.73 to 0.57 in the second table is a real effect measured with error bars wide enough
to include the bounce at the bottom. Reading the $100B row as "capacity improves above $20
billion" would be exactly the sort of noise-mining the walk-forward section above exists to warn
about.

The `slowest` column is what actually rules those bottom rows out. At $100 billion the 10/50
strategy's worst single order took **144 trading days** to complete — seven months to establish
a position held on a signal that flips every few weeks. The Sharpe is beside the point; the
strategy is not implementable, and no cost model is needed to say so. The honest ceiling for this
strategy on this instrument sits between $5 and $20 billion, set by fill delay rather than by
impact cost.

One more thing the sweep makes concrete: SPY is about the most liquid instrument in the world, at
a $22.6 billion median day. A $5 billion book is a fifth of a day's volume there. The same
strategy on a mid-cap name trading $50 million a day would hit the same wall at about $11
million — four hundred and fifty times sooner. Capacity results do not transfer between
instruments, and a capacity number quoted without the instrument's volume beside it is not a
number.

## What is not modeled

- ~~Fills are complete, instant, at any size~~ — `ParticipationLimitedExecutionHandler` caps
  fills at a share of each bar's volume and charges square-root impact on every slice; see the
  capacity section above for what that costs. The two older handlers still fill everything
  instantly, and every number in this file outside that section uses one of them.
- ~~Slippage scales with price, not with order size relative to volume~~ — true of the default
  handler, and the reason the participation-limited one exists.
- No borrow costs, margin, or taxes.
- Daily bars only. Intraday, the crossover dates would move.
- One parameter pair (50/200) is used throughout. It is not cherry-picked (see the grid above),
  and it is not fitted — fitting the windows on this same 2015-2024 sample would launder
  curve-fitting as insight. Fitting them honestly, on trailing data only, is the walk-forward
  section, and it does worse than leaving them alone.
