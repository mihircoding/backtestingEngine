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

**The crossover moves from 1.97% of a day's volume to 0.39%** — $409M of SPY
down to $80M, 5.1x sooner — while the touch thins from 153,221 shares to
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
  deployed-capital column is identical to the share. That is not an
  anticlimax, it is the answer — what strands the capital at $5bn and above is
  the 10% participation cap, not the cost of the fill, so a cost correction
  cannot move it. The 200-share default trade size still sits four orders of
  magnitude below even the shaped crossover.

So the number that was worth quoting changed by a factor of five, and the
number it was quoted in support of did not change at all. Finding that out
cost one parameter and about thirty lines.

### 2. The capacity number was measuring a cash pile

Same strategy, same AUM ladder, same 10% participation cap as section 11. Only
the fill model changes.

| AUM | days of volume | deployed | square-root Sharpe | book Sharpe | gap |
|---|---|---|---|---|---|
| $0.1bn | 0.00 | 100% | 0.60 | 0.66 | +0.06 |
| $1bn | 0.04 | 100% | 0.49 | 0.62 | +0.13 |
| $5bn | 0.22 | 41% | 0.44 | 0.59 | +0.15 |
| $20bn | 0.89 | 10% | 0.55 | 0.68 | +0.13 |
| $100bn | 4.43 | 2% | 0.73 | 0.78 | +0.05 |

Baseline with no liquidity limit: 0.65.

The fill model moves the Sharpe by +0.05 to +0.15 and never changes a sign, so
the capacity conclusion survives the swap. The interesting result is the one I
was not looking for, and it is in the third column.

**Sharpe falls to 0.44 at $5bn and then rises, to 0.68 and 0.78.** Capacity
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
- The depth is flat across levels and constant through time. Real books are
  thin at the touch and thicken outward, and both thin out in exactly the
  markets where it hurts most.
