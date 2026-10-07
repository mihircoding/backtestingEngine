# Results

Three write-ups live in this repo, and they are in two different files because
they answer two different kinds of question.

- **[notes/backtester.md](notes/backtester.md)** — does this strategy make
  money, and how much of the answer is the testing procedure rather than the
  strategy. Twelve sections: costs, significance, walk-forward, position
  sizing, capacity, a 24-asset cross-section and a long/short leg.
- **[notes/exchange.md](notes/exchange.md)** — what a matching engine does to
  order flow. Eight sections: emergent spread and impact, mean reversion,
  venue fees, latency, the opening auction, and what a place in the queue is
  worth.
- **This file** — the one question that needs both halves, and could not be
  asked while they were separate repositories.

Interview notes: [notes/interview-backtester.md](notes/interview-backtester.md),
[notes/interview-exchange.md](notes/interview-exchange.md).

---

## The fill was always a guess. Now it isn't.

Reproduce with: `python fill_realism.py`

Every execution handler in `src/execution.py` prices its own fills. The first
two apply a flat `slippage_bps`. The third replaces that with the square-root
impact law, which is the right functional form and comes with a constant —
`impact_coef`, defaulting to 1.0 — that every firm calibrates on its own fills
and nobody publishes. Section 11 of notes/backtester.md quoted a capacity
number out of that handler, so it quoted a number in units the reader cannot
check.

This repository contains a price-time priority matching engine. So instead of
defending the constant, `src/book_execution.py` sends the orders to the engine:
build a book from the bar's volume, walk it, and take the volume-weighted price
of the resting orders the order actually consumed. Cost stops being a parameter
and becomes a consequence of depth.

SPY, 2015-2024, 2,515 bars, median price $271 and median volume 77M shares. The
book rests 0.2% of a day's volume — about 153,000 shares — at every penny.

### 1. The flat charge is wrong in both directions

| order, % of a day | shares | flat 2bp | square-root law | the book | levels walked |
|---|---|---|---|---|---|
| 0.01% | 7,661 | 2.00 | 3.10 | **0.37** | 1 |
| 0.10% | 76,610 | 2.00 | 5.48 | **0.37** | 1 |
| 0.30% | 229,832 | 2.00 | 8.02 | **0.49** | 2 |
| 1.00% | 766,108 | 2.00 | 13.00 | **1.11** | 6 |
| 3.00% | 2,298,324 | 2.00 | 21.05 | **2.95** | 16 |
| 10.0% | 7,661,080 | 2.00 | 36.79 | **9.41** | 51 |
| 30.0% | 22,983,240 | 2.00 | 62.25 | **27.86** | 151 |

Cost per share, in basis points.

A flat 2 bps on a $271 stock is 5.4 cents, or roughly five ticks. An order the
touch absorbs pays one. So the default slippage assumption **overcharges small
orders by a factor of five**, and keeps charging the same 2 bps for an order
that walks 151 price levels and should cost fourteen times that.

The crossover is the number worth quoting: **a flat book starts costing more
than a flat 2 bps at 1.97% of a day's volume** — 1.5M shares, about $409M of
SPY. Section 1b shapes the book and moves that to 0.39%, so read this one as
the flat-book figure rather than the final answer.
Below that, every cost figure in notes/backtester.md is pessimistic. Above it,
optimistic. The repo's own trade size of 200 shares sits six orders of
magnitude below the crossover, which means the costs charged throughout that
file are conservative, and that is worth knowing in the direction it points.

The square-root law sits above the book at every size tested, by a factor of
eight at the small end and two at the large end. That is not evidence the law
is wrong — its shape is clearly right, the three columns converge in the way a
concave cost curve should — it is evidence that `impact_coef = 1.0` is
aggressive for a name as liquid as SPY. Which is exactly what a constant
nobody publishes is expected to be: a guess, and in this case a conservative
one.

### 1b. The book was not flat either, and that moved the crossover 5x

Reproduce with: `python fill_realism.py` (section 1b)

Section 1 replaced a cost assumption with a depth assumption and called that
progress. It is, but the depth assumption had a second half hiding in it that
I did not notice writing the first version: the book was seeded with the
**same size at every price level**. That says resting at the touch is as
attractive as resting ten ticks behind it, which is backwards. The touch is
where you get filled by whoever knows something, so it is the least attractive
place to leave size, and real books lean the other way — thin in front,
thicker behind.

`shape` sets how hard. Size at the i-th level is proportional to `i ** shape`,
renormalised so the **total** resting size does not change. `shape = 0` is the
old flat book; the handler now ships at 0.5.

I expected that to tilt the cost curve — small orders dearer because the touch
is thinner, large orders cheaper because the back is fatter. **It does not
tilt. It raises cost at every size.**

| order, % of a day | shares | flat 2bp | sqrt law | flat book | shaped book |
|---|---|---|---|---|---|
| 0.01% | 7,661 | 2.00 | 3.10 | 0.37 | **0.37** |
| 0.10% | 76,610 | 2.00 | 5.48 | 0.37 | **0.89** |
| 0.30% | 229,832 | 2.00 | 8.02 | 0.49 | **1.75** |
| 1.00% | 766,108 | 2.00 | 13.00 | 1.11 | **3.82** |
| 3.00% | 2,298,324 | 2.00 | 21.05 | 2.95 | **7.91** |
| 10.0% | 7,661,080 | 2.00 | 36.79 | 9.41 | **17.63** |
| 30.0% | 22,983,240 | 2.00 | 62.25 | 27.86 | **36.65** |

Cost per share in basis points. Both books hold 30,644,200 shares a side — the
same liquidity, differently arranged.

The reason the tilt does not happen is worth being able to say in one line: a
fill price comes off **cumulative** depth, how much is available within n
ticks, and not off the total. Moving size backwards lowers the cumulative at
every level except the last, where the two books are equal by construction. So
the shaped book is reached into further at any size, and the two curves meet
only on an order that sweeps the entire side. There is a test on each half of
that.

**The crossover moves from 1.96% of a day's volume to 0.37%** — $408M of SPY
down to $77M, 5.3x sooner — while the touch thins from 153,221 shares to
16,192 and total capacity does not move at all.

Which means the honest description of `shape` is not the one I first wrote
down. Normalising the total makes the parameter **size**-neutral, not
**cost**-neutral; it still moves cost, and only ever upward. What that buys is
narrower than cost-neutrality but real: the knob is a shape a market-data feed
can show you, and it cannot be turned to make a backtest look cheaper — only
dearer. A parameter that can only hurt the result is a safer thing to leave in
a repository than one that can flatter it.

Two things I would not claim from this:

- **The profile is monotone, and a real book is not.** Depth rises away from
  the touch and then decays further out; `i ** shape` only has the rising half.
  It is the half that matters for anything short of a full sweep, and the
  sweep in `fill_realism.py` shows the effect saturating well before `shape`
  gets silly — at 2.0 the touch holds 11 shares, which is not a book, and it
  is in the sweep to show the limit rather than as a candidate setting.
- **5x on the crossover did not change the conclusion the crossover was for.**
  The capacity ladder barely moves: Sharpe 0.62 → 0.58 at $1bn, and the
  deployed-capital column is identical to the share. Section 1c then takes it
  to 0.57 for the same reason and with the same non-effect on deployment. That is not an
  anticlimax, it is the answer — what strands the capital at $5bn and above is
  the 10% participation cap, not the cost of the fill, so a cost correction
  cannot move it. The 200-share default trade size still sits four orders of
  magnitude below even the shaped crossover.

So the number that was worth quoting changed by a factor of five, and the
number it was quoted in support of did not change at all. Finding that out
cost one parameter and about thirty lines.

### 1c. And it was not the same book every day, which is where the money was

Reproduce with: `python fill_realism.py` (section 1c)

Sections 1 and 1b both fixed **where** the liquidity sits. Neither touched
**when** it is there. The book was rebuilt every bar out of that bar's volume
and otherwise identical: an average day's depth quoted on every day in the
sample, including the ones nobody wanted to quote into.

That is the wrong way round, and the mechanism is a sentence. A market maker
quotes a budget of risk, not a number of shares. When the price is moving
around more, the same number of shares is more risk, so it shows fewer of
them. Depth and volatility move against each other, which is the same fact as
the better known one that spreads widen when vol rises — one relationship seen
from two sides.

`vol_elasticity` puts that in. Resting size at every level is multiplied by
`(typical vol / current vol) ** vol_elasticity`, where current vol is the
trailing 20-bar realized volatility and typical vol is the **expanding median**
of that same series — what a desk sitting at that date would call a normal day,
given only the history it had. The handler reads both out of `get_latest()`, so
a bar that has not been released cannot change a fill that has already printed.
There is a test that truncates the future and checks the price is identical.

Elasticity 1.0 ships, because it is the textbook statement rather than a fitted
number and because the risk-budget argument above predicts exactly that
exponent.

| elasticity | depth it met | cost per share | Sharpe | total return | levels walked |
|---|---|---|---|---|---|
| 0.00 (constant) | 1.00x | 13.76 bps | 0.58 | 121.8% | 57.4 |
| 0.50 | 0.88x | 14.95 bps | 0.58 | 120.1% | 63.0 |
| **1.00 (ships)** | **0.80x** | **16.44 bps** | **0.57** | **118.0%** | **70.0** |
| 2.00 | 0.74x | 19.66 bps | 0.55 | 113.5% | 85.1 |

$1bn, 10% participation, share-weighted. "Depth it met" is the multiplier
averaged over the bars the strategy actually traded, weighted by shares.

**This knob is not like `shape`.** `shape` can only make a fill dearer, so
leaving it switched on cannot flatter a backtest. This one cuts both ways: a
quiet bar now gets a **deeper** book than the constant model gave it, and an
order placed on one fills cheaper than before. Whether it helps or hurts is
therefore not a property of the model at all. It is a property of **when the
strategy trades**, and that has to be measured rather than argued.

Measured, it is one-sided for this strategy, and this is the result:

- Across every bar in the sample the multiplier averages **0.99**. The book has
  not been quietly made thinner; a run that never traded would see no change.
- On the bars this strategy chose to trade it averages **0.80**. The book it
  actually met is a fifth thinner than the one it was being charged for.
- Its fills land at the **64th percentile** of realized volatility, not the
  50th — stable at 0.62 to 0.64 at every AUM level on the ladder, so it is a
  property of the signal and not of the size.

The reason is not subtle once stated, which is what makes it worth stating: a
moving-average crossover fires when a trend breaks, and a trend breaking **is**
a volatility event. The signal is correlated with illiquidity by construction.
Every strategy that trades on price movement has some version of this, and the
constant-depth book cannot see any of it.

**Splitting the 2.68 bps.** It would be easy to over-claim here, because cost
is convex in depth and two different things are being conflated. Running a
**constant** book scaled to that same 0.80x — thinner every single day, but
never thinner on the days that matter — costs 15.93 bps, which is +2.16 of the
+2.68. So **81% of the extra cost is nothing but when this strategy trades**,
and the remaining 19% is the convexity around it. The co-movement is the
effect; the dispersion is a rounding error on it.

Both columns below run at the shipped `shape` of 0.5, so they differ from the
flat-book ladder in section 2 and from each other only in `vol_elasticity`.

| AUM | deployed | Sharpe, constant | Sharpe, elastic | cost, constant | cost, elastic |
|---|---|---|---|---|---|
| $0.1bn | 100% | 0.65 | 0.64 | 3.17 bps | 3.77 bps |
| $1bn | 100% | 0.58 | 0.57 | 13.76 bps | 16.44 bps |
| $5bn | 41% | 0.54 | 0.52 | 17.23 bps | 21.07 bps |
| $20bn | 10% | 0.64 | 0.62 | 18.15 bps | 22.00 bps |
| $100bn | 2% | 0.76 | 0.75 | 19.18 bps | 23.84 bps |

Two things to notice in that table, and the second one matters more.

The cost column rises by 19% to 24% at every rung, which is the point of the
section. The **deployed** column does not move by a single share. That is the
third time in this file that a cost correction has failed to move the capacity
answer, and by now it is not a coincidence but a structural fact worth saying
plainly: what strands capital above $5bn here is the 10% participation cap, and
a participation cap is not a cost. Correcting a cost model cannot move a limit
that was never about cost. Knowing which of your numbers are load-bearing is
most of what a cost study is for.

What I would not claim from this:

- **The elasticity is not estimated from depth data.** It is the exponent the
  risk-budget argument predicts and the one the spread literature reports,
  checked for sensitivity by sweeping it, not fitted to an order book. Fitting
  it needs a depth history this project does not have.
- **The multiplier is clipped to [0.25x, 4.0x].** Twenty days of unusual calm
  inside ten years can put current vol a factor of four below the median, and
  an unclipped elasticity of 1 would then quote four times the real depth on
  the strength of twenty observations. The clip is an admission that the
  relationship was only ever measured in the middle of the distribution.
- **One bar is still one book.** Depth now varies across days and remains a
  single pooled snapshot within one. The intraday version of this effect — the
  book thinning for minutes around a print — needs intraday data.
- **Volume is not adjusted alongside depth.** Real volume *rises* with
  volatility while depth at the touch falls, and only the second half is
  modeled here, so the participation cap is slightly tighter on loud days than
  it should be. It moves nothing in the table above, because the cap only
  binds above $5bn where the deployed column is the answer anyway.

### 2. The capacity number was measuring a cash pile

Same strategy, same AUM ladder, same 10% participation cap as section 11. Only
the fill model changes.

| AUM | days of volume | deployed | square-root Sharpe | book Sharpe | gap |
|---|---|---|---|---|---|
| $0.1bn | 0.00 | 100% | 0.60 | 0.64 | +0.04 |
| $1bn | 0.04 | 100% | 0.49 | 0.57 | +0.08 |
| $5bn | 0.22 | 41% | 0.44 | 0.52 | +0.08 |
| $20bn | 0.89 | 10% | 0.55 | 0.62 | +0.07 |
| $100bn | 4.43 | 2% | 0.73 | 0.75 | +0.03 |

Baseline with no liquidity limit: 0.65. The book column runs at the handler's
shipped settings, which now include both the depth profile of section 1b and
the volatility response of section 1c; it was 0.66 / 0.62 / 0.59 / 0.68 / 0.78
on the flat, constant-depth book this section was first written against.

The fill model moves the Sharpe by +0.03 to +0.08 and never changes a sign, so
the capacity conclusion survives the swap. The interesting result is the one I
was not looking for, and it is in the third column.

**Sharpe falls to 0.44 at $5bn and then rises, to 0.62 and 0.75.** Capacity
does not improve with size. What happens is that the participation cap stops
the orders from filling: at $100bn, 98% of the intended book never reaches the
market. The equity curve is 98% idle cash, the denominator of the Sharpe ratio
collapses, and the statistic starts describing the cash rather than the
strategy. A fill model is being *rewarded* for failing to trade.

So section 11's ladder was only readable up to $1bn, where the book still
deploys. Past that the honest output is not a Sharpe at all — it is the
deployed fraction, which is the capacity number the study was reaching for in
the first place. `interpretable_to()` draws that line and `print_capacity()`
flags every row below it. Both columns of the old table were quietly affected;
running two fill models side by side is what made it visible, because a flaw
that moves both columns the same way looks like a result until something else
is standing next to it.

### 3. How much of this is the one free parameter

`depth_frac` is an assumption, same as `slippage_bps` was. The difference is
what kind: depth is published by venues and carried on every market data
feed, and anyone can look up what rests at the touch in the name they care
about. Cost in basis points is published by nobody. That is the whole claim —
not that this depth is correct, so it gets swept across two orders of
magnitude at $5bn.

| depth_frac | shares a penny | Sharpe | return | levels walked | deployed |
|---|---|---|---|---|---|
| 0.0002 | 15,322 | 0.49 | 90.9% | 189.9 | 16% |
| 0.0005 | 38,305 | 0.44 | 84.0% | 174.2 | 41% |
| 0.0020 | 153,221 | 0.59 | 114.8% | 44.3 | 41% |
| 0.0050 | 383,054 | 0.62 | 121.0% | 18.0 | 41% |
| 0.0200 | 1,532,216 | 0.63 | 124.0% | 4.5 | 41% |

A hundredfold change in depth moves the Sharpe by 0.19, from 0.44 to 0.63. So
the answer is parameter-dependent and the sweep is not decoration — but the
dependence is one-directional and bounded, where `slippage_bps` could be set
to anything.

The row that does not fit is the first one, which posts a *higher* Sharpe than
the thinner-book row below it. That is the same artifact as section 2, showing
up a second time: at the thinnest depth the order walks 190 of the available
200 levels, exhausts the book, and strands 84% of the capital instead of 59%.
More cash, less volatility, better Sharpe. One mechanism explains both
non-monotonicities in this study, which is the main reason I believe the
explanation.

### 4. The venue's cut, which turns out not to matter here

Every order the book handler sends is a market order, so a maker-taker venue
bills 3 mils a share on all of it with no rebate to net against. At $1bn that
is **$1.3M of taker fees over ten years and no change in the Sharpe to two
decimal places.**

A trend follower holding positions for months does not trade enough for fee
tier to matter. Section 5 of notes/exchange.md found fees were a fifth of the
quoted edge for a market maker turning over constantly. Same fee schedule,
same code, two conclusions three orders of magnitude apart — because the fee
is per share and the edge is per holding period, and nothing about the venue
tells you which one you are. That contrast is only available because both
strategies now live in the same repository.

### What this still does not model

- One bar is one book. A day's liquidity is pooled into a single snapshot at
  the open, which overstates what is at the touch in any instant and
  understates how long working an order really takes.
- The book is rebuilt every bar and remembers nothing between them.
- Every order is a taker. Passive execution is deliberately absent, and the
  reason is a measurement: pooling a day's volume puts ~153,000 shares at the
  touch, and section 8 of notes/exchange.md already showed that an order
  joining a queue of 20,000 or more never fills. A passive handler on this
  book would report that quoting is impossible, which is a fact about the
  pooling and not about quoting. That needs intraday data and a persistent
  book.
- Depth varies across days (section 1c) but not within one, so the book
  thinning for minutes around a print is still missing. Volume is also held at
  its actual value while depth responds to volatility; really both move, in
  opposite directions.
- The depth profile rises away from the touch and never decays. A real average
  book rises and then falls off further out; only the half that matters short
  of a full sweep is modeled.
