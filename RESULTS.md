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
  I first wrote here that the rising half is the half that matters short of a
  full sweep. Section 1d measured that and it is wrong: the far half decides
  where the crossover sits, by a factor of twenty.
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

### 1d. The other half of the profile, and what it does to the headline

Reproduce with: `python fill_realism.py` (section 1d)

Section 1b's first caveat was that the profile only has the rising half. It
is worse than a caveat. `i ** 0.5` over 200 levels has its *heaviest* level at
the far edge of the book — level 200 holds 1.5x the flat size, level 1 holds
0.1x — so the ramp was the right shape at the touch and the wrong one
everywhere behind it. Nobody rests size two dollars from a one-cent market.

`decay` is the other half: size at level *i* is `i ** shape * exp(-i / decay)`,
which rises to a peak at `shape * decay` levels and thins out past it. That is
the hump every empirical average book has. `None` is the ramp, exactly, so
every number above is still reachable. The sweep holds `shape` at 0.5 and the
total resting size fixed, as before:

| decay | peak at level | touch | half the size within | level 200 | 0.1% | 1% | 3% | 10% | 30% | crossover |
|---|---|---|---|---|---|---|---|---|---|---|
| none (ships) | 200 | 0.11x | 126 levels | 1.495x | 0.89 | 3.82 | 7.91 | 17.63 | 36.65 | **0.37%** |
| 80 | 40 | 0.38x | 78 | 0.442x | 0.46 | 1.70 | 3.59 | 8.64 | 22.35 | 1.27% |
| 40 | 20 | 0.89x | 46 | 0.087x | 0.37 | 0.99 | 2.05 | 4.95 | 13.48 | 2.90% |
| 20 | 10 | 2.41x | 24 | 0.002x | 0.37 | 0.56 | 1.08 | 2.54 | 6.91 | **7.31%** |

Cost in bps per share by order size as a share of the day's volume, SPY at
$271 and 76.6M shares; touch and level 200 as multiples of the flat per-level
size.

**This is the tilt `shape` was expected to produce and could not.** Section 1b
worked out why shaping the book raises the cost of every order: moving size
backwards lowers cumulative depth at every level but the last. The decay moves
size the other way — out of the tail and into the middle — and that *raises*
cumulative depth at every level a mid-sized order reaches. So large orders get
cheaper. Cheaper than the flat book, past a point: at 30% of the day a decay of
40 pays 13.5 bps against the flat book's 27.9. Small orders are untouched
because the touch is the touch.

And so the crossover — the size above which a flat 2 bps was flattering the
strategy, the number section 1b quoted at 0.39% and $80M — runs from **0.37%
to 7.31%** of a day's volume, $77M to $1.5bn of SPY, as one parameter moves
across a range every value of which is a plausible book. **A twentyfold range
on the headline figure, set by a number nothing in this repository can
observe.** That is the finding, and it is a less comfortable one than 1b's:
the crossover is not a measurement, it is a function of the depth profile, and
the profile is an assumption with two free parameters rather than one.

Which is why `decay` ships off, for the opposite reason `shape` ships on. A
shape can only make the book dearer than flat, so leaving it on is safe. A
decay can make it cheaper, by an amount set by a parameter this repository
cannot pin, and shipping a value from the middle of that range would quote a
crossover that looked measured and was chosen. The ramp is the dearest book of
the family. The one thing it cannot do is flatter the strategy, and the sweep
sits in the results as the error bar on the number.

What does not move, again: the capacity ladder. Deployed capital is a function
of the participation cap and the cap does not know what the book looks like.
Three cost corrections in a row have failed to move it (1b, 1c, and now a
sweep that moves the crossover twenty times over), and that is the sentence
worth taking from the whole of section 1 — **the number that is robust to the
cost model is the one that was never about cost.**

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

*Correction, section 8.* The deployed column is right about the direction
and wrong about what it measures. It is one minus the shares still working
when the data ran out, and the order still working on December 30, 2024 was
a sell that was two bars old. So 41%, 10% and 2% are how much of the *last
order* got done, not how much of the book was ever in the market. Averaged
over every bar, the $100bn book was in the market as much as the $100m one
— it was not idle, it was stuck — and the Sharpe rises for a different
reason than the one given here. Section 8 has the right column and the
mechanism. The conclusion that the rows above $1bn are not Sharpe ratios of
this strategy stands.

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

### 5. What the book remembers, and why the formula was dearer

Reproduce with: `python fill_realism.py` (section 5 of the printout)

Every table above rebuilds the book from scratch each bar. A slice that walked
twenty levels on Monday finds all twenty refilled at Monday's prices on
Tuesday, and nothing the strategy does ever moves the price it trades at next.
That is the "remembers nothing" line in the list below, and it is also the
one thing the square-root formula in `execution.py` has that the book did not:
the permanent half of impact. Almgren's split is that part of what an order
pays is temporary (the levels it ate, which refill) and part is permanent (the
price is higher afterwards because the order happened). A fresh book every bar
is a model with no permanent part at all, which made it the obvious suspect
for why section 2 found the book Sharpe *above* the formula's at every AUM.

`persistence` is the share of each slice's displacement — its fill price
against the mid it was sent at — still in the mid when the next bar's book is
built. The second slice of a large order then pays for the first. `recovery`
is the half-life of that carried displacement in bars; `None` never decays,
which is Almgren's permanent impact. One mechanical point that the tests pin:
a slice is charged half of its own permanent displacement on top of what the
book printed, because the permanent part accrues while the slice trades. That
is the trapezoid rule, the half in Almgren's ½γX², and without it a round trip
nets the permanent impact to a *gain* of one slice — a discretisation artifact
that the first version of this section had and that showed up as a 10-year
backtest getting cheaper the more it remembered.

Sweep at the two AUM levels where slicing happens, book at its shipped
settings otherwise:

| AUM | persistence | recovery | Sharpe | sqrt Sharpe | return | cost/slice | own carry | max carried | slices |
|---|---|---|---|---|---|---|---|---|---|
| $1bn | 0 | — | 0.57 | 0.49 | 118% | 16.4 bp | 0.0 | 0 bp | 89 |
| $1bn | 0.33 | never | 0.57 | 0.49 | 119% | 16.3 | −2.9 | 53 | 89 |
| $1bn | 1.00 | never | 0.56 | 0.49 | 120% | 15.9 | −8.8 | 164 | 89 |
| $1bn | 0.33 | 5 bars | 0.55 | 0.49 | 115% | 18.8 | −0.4 | 11 | 89 |
| $1bn | 1.00 | 5 bars | 0.53 | 0.49 | 109% | 23.6 | −1.1 | 32 | 89 |
| $5bn | 0 | — | 0.52 | 0.44 | 100% | 21.1 | 0.0 | 0 | 301 |
| $5bn | 0.33 | never | 0.51 | 0.44 | 100% | 21.3 | −3.2 | 206 | 301 |
| $5bn | 1.00 | never | 0.47 | 0.44 | 99% | 21.9 | −9.7 | 623 | 301 |
| $5bn | 0.33 | 5 bars | 0.47 | 0.44 | 90% | 29.3 | +4.8 | 28 | 301 |
| $5bn | 0.67 | 5 bars | **0.42** | 0.44 | 80% | 37.8 | +9.7 | 56 | 301 |
| $5bn | 1.00 | 5 bars | 0.38 | 0.44 | 70% | 46.1 | +14.5 | 84 | 301 |
| $5bn | 1.00 | 20 bars | 0.44 | 0.44 | 83% | 35.3 | +3.6 | 92 | 301 |

"Own carry" is the share-weighted offset each slice started from, signed
against the order, so a negative number means the handler's own past trading
had on average pushed the price *toward* the slice. Deployed capital is
unchanged on every row.

**Permanent impact is not a cost to a round trip, so it cannot be what the
formula has that the book does not.** With `recovery=None` the cost per slice
barely moves — 16.4 to 15.9 bps at $1bn, 21.1 to 21.9 at $5bn — and own carry
is *negative*: the strategy buys a position over weeks, pushing the mid up
through every slice, then sells it over weeks starting from the mid it pushed
up to. What the entry paid the exit gets back. That is the textbook result
(Huberman and Stanzl 2004: with linear permanent impact, round trips are
free of it) and it is the thing a formula that charges impact per slice,
both ways, gets wrong. The square-root model charges the full
displacement on the way in and the full displacement on the way out. The
book with permanent memory charges it once. **So the gap in section 2 — book
above formula by 0.03 to 0.08 — is not the book being generous. It is the
formula charging the permanent half of impact twice.**

**What the formula is actually modelling is a book that heals in about a
week and keeps half of what you moved.** The transient rows are the ones that
raise cost, because a mid that has recovered by the time the exit arrives does
not give the entry's impact back. At $5bn with a 5-bar half-life the book
crosses the formula between persistence 0.33 and 0.67 (0.47, 0.42 against
0.44), and at a 20-bar half-life it lands on it exactly at persistence 1.0.
The formula's one constant, `impact_coef=1.0`, is therefore equivalent to a
specific pair of book parameters, and that pair is in the plausible range —
the propagator literature puts the surviving fraction at a day's horizon
between a third and two thirds (Bouchaud et al. 2004), with a decay that is
slow but not zero. A constant nobody publishes turns out to be a statement
about resilience that someone has.

**The permanent rows have a number in them that should not be believed, and
it is the reason the knob ships off.** Max carried is 164 bps at $1bn and 623
bps at $5bn — the handler's private mid drifting six percent away from the
tape over a decade, because buys and sells happen into different depth and
the displacements do not cancel. That is not a market; it is what a
never-decaying offset on top of *observed* prices does, and the observed
prices already contain the permanent impact of everything that actually
traded. What a backtest can honestly add is the strategy's own displacement
over the horizon the tape could not have seen it, which is days, not years.
So the transient model is the right one and the permanent one is a limiting
case — but the transient model's two parameters are exactly as unobservable
as `decay` was in 1d, and for the same reason `persistence` ships at 0: the
memoryless book is the one setting that cannot be accused of a chosen
number, and the sweep is the error bar. The direction is at least
one-sided, like `shape` — a positive persistence can only make a one-way
order dearer, and `tests/test_book_execution.py` pins that.

**And, for the fourth time, deployed capital did not move.** Shape, vol
elasticity, decay, now memory: four corrections to the cost of a fill, and
the capacity cap is the same 41% at $5bn on every row of every table. The
participation cap does not know what the book looks like. The number that is
robust to the cost model is the one that was never about cost, and it is the
number to lead with.

### 6. The square root was in the slice, not the order

Reproduce with: `python fill_realism.py` (section 6 of the printout)

Section 5 ended with a sentence I wanted to be able to say: that the formula's
constant is equivalent to a pair of book parameters, so the two execution
handlers are one model in two parametrisations. It was true at one AUM. This
section is what happened when I tried to make it true across sizes, and the
reason it cannot be is a flaw in the formula handler that has been there since
the first commit.

**What `execution.py` actually charges.** The participation handler's docstring
says cost follows a square-root law and cites Almgren. The law in the
literature is a statement about a whole order: an order of Q shares worked
over a day or a week costs about σ√(Q/V) per share, where V is a day's volume
(Almgren et al. 2005, Tóth et al. 2011). What the handler charges is
σ√(q/V) per *slice*, where q is one bar's slice. Those are the same number
only while the order fits in one bar. Past that, every slice is the
participation cap, every slice costs the same, and the cost per share of the
whole order stops depending on how big the whole order is. A position that
takes a day to build and one that takes a month cost the same per share.
That is not the square-root law. It is a flat charge with a square root in
its name.

**The test.** One order of Q shares, from a hundredth of a day's volume up to
three days of it, worked to completion at 10% participation into a book on a
flat tape — same open and same volume every bar, no volatility — so the only
thing that can move the price between slices is what the book remembers of
its own earlier slices. The number reported is implementation shortfall: the
share-weighted fill against the mid the order arrived at. Next to it, what the
formula handler charges the same order, and what the law it is named after
says. Cost in basis points per share, SPY at $271 and 77M shares a day:

| days of volume | 0.01 | 0.02 | 0.05 | 0.10 | 0.20 | 0.50 | 1.0 | 2.0 | 3.0 |
|---|---|---|---|---|---|---|---|---|---|
| book, memoryless (ships) | 3.6 | 5.9 | 10.9 | 17.5 | 17.5 | 17.5 | 17.5 | 17.5 | 17.5 |
| book, persistence 0.33, permanent | 4.3 | 6.8 | 12.7 | 20.3 | 23.3 | 32.2 | 46.7 | 75.9 | 105.0 |
| book, persistence 1.00, permanent | 5.5 | 8.8 | 16.4 | 26.2 | 35.1 | 61.7 | 106.0 | 194.5 | 283.1 |
| book, persistence 0.33, 5-bar half-life | 4.3 | 6.8 | 12.7 | 20.3 | 22.9 | 29.3 | 36.8 | 45.4 | 49.7 |
| book, persistence 1.00, 5-bar half-life | 5.5 | 8.8 | 16.4 | 26.2 | 34.0 | 53.3 | 76.3 | 102.1 | 114.9 |
| book, persistence 1.00, 20-bar half-life | 5.5 | 8.8 | 16.4 | 26.2 | 34.7 | 59.3 | 96.4 | 159.4 | 210.0 |
| **formula, as coded** | 13.0 | 17.6 | 26.6 | 36.8 | **36.8** | **36.8** | **36.8** | **36.8** | **36.8** |
| square-root law, Y = 1 | 11.0 | 15.6 | 24.6 | 34.8 | 49.2 | 77.8 | 110.0 | 155.6 | 190.5 |

Orders up to 0.10 of a day fit in one slice; everything to the right is
sliced. Read across the formula row: 36.8 bps from a tenth of a day to three
days of volume, while the law it cites goes from 35 to 190. The memoryless
book, which is what ships, does the same thing at 17.5. Neither of the two
handlers this repository compares has a cost that grows with the size of a
sliced order, and section 2's capacity ladder was built with both of them.

**The exponent.** Slope of log cost on log size, fitted separately on the
orders that fit in one bar and on the ones that are sliced, because the two
regimes have different causes:

| memory | book, one bar | book, sliced | formula, sliced | law, sliced | implied Y |
|---|---|---|---|---|---|
| none (ships) | 0.68 | **0.00** | **0.00** | 0.50 | 0.20 |
| 0.33, permanent | 0.68 | **0.48** | 0.00 | 0.50 | **0.49** |
| 0.67, permanent | 0.68 | 0.63 | 0.00 | 0.50 | 0.73 |
| 1.00, permanent | 0.68 | 0.71 | 0.00 | 0.50 | 0.96 |
| 0.33, 5-bar half-life | 0.68 | 0.27 | 0.00 | 0.50 | 0.37 |
| 0.67, 5-bar | 0.68 | 0.39 | 0.00 | 0.50 | 0.53 |
| 1.00, 5-bar | 0.68 | 0.45 | 0.00 | 0.50 | 0.68 |
| 0.33, 20-bar | 0.68 | 0.41 | 0.00 | 0.50 | 0.44 |
| 0.67, 20-bar | 0.68 | 0.55 | 0.00 | 0.50 | 0.66 |
| 1.00, 20-bar | 0.68 | 0.63 | 0.00 | 0.50 | 0.86 |

Inside one bar the exponent is 0.68 whatever the memory setting, and it is set
by the depth profile: with `shape=0.5` cumulative depth grows like the
levels walked to the 1.5, so cost grows like size to the 1/1.5. The flat book
would give 1.0 (the test pins that) and the law gives 0.5; the shipped
profile sits between them, a little steeper than the law. Across bars the
profile drops out entirely and memory is the only thing left. A book that
refills completely has an exponent of exactly zero, and so does the formula.

**Persistence 0.33 with no recovery is the square-root law.** Exponent 0.48
against the law's 0.50, and the implied coefficient — book cost over
σ√(Q/V), geometric mean across the sliced orders — is 0.49. The literature
puts Y between a half and one, with Tóth et al.'s estimate near the bottom
of that range. So a book that keeps a third of each slice's displacement and
never gives it back reproduces the empirical impact law in both exponent and
level, on a parameter grid that was chosen in section 5 for a different
purpose. The other permanent rows are too steep (0.63, 0.71: a book that
keeps everything is on its way to linear, which is Almgren's permanent term
on its own) and the 5-bar rows are too shallow (0.27 to 0.45: a book that has
forgotten the first slice by the sixth cannot charge the thirtieth for it).
The 20-bar rows bracket the law, which is what a half-life comparable to the
order's own duration should do.

**So the section 5 sentence has to be weakened, and here is the right
version.** Section 5 found that the formula's Sharpe at $5bn is matched by a
book with a 5-bar half-life and persistence between 0.33 and 0.67. That
equivalence held at one AUM because it had to: the formula is flat in order
size and the book is not, so two curves with different exponents cross once
and are not the same curve. The pair of parameters that reproduces the
*shape* of the law is a different pair — permanent, and a third — and it
charges a three-day order 105 bps where the formula charges 37. The two
handlers are not one model in two parametrisations. One of them is a model
of impact and the other is a constant with a square root in it, and the
5-bar book "matched" it by being wrong in the same amount at one size.

This also reopens section 5's verdict on the permanent rows. They were set
aside there because of the 623-bps drift of the handler's private mid over a
decade, and that argument stands for a *backtest*, where the tape already
contains the permanent impact of everything that actually traded. It does
not stand for a single order over its own execution horizon, which is what
the square-root law describes and what this section measures. The honest
position is that the law is a statement about days and the drift is a
statement about years, and a book whose memory decays on a scale between
the two — the 20-bar rows — is the one that is defensible for both. Its
exponent is 0.55 to 0.63 at the shipped and full persistence, which is a
little above the law, and its Y of 0.66 to 0.86 is in the published range.

**What this changes in the earlier sections.** The capacity ladder in
section 2 compared the square-root handler against a memoryless book. Both
charge a $5bn position the same per share as a $100M one, once sliced, which
means the "book Sharpe" column and the "sqrt Sharpe" column agree about the
one thing that the square-root law says matters most — that a big order is
dearer per share than a small one — by both leaving it out. The deployed-
capital cap is unaffected, for the fifth time: it is set by participation,
not cost. But the Sharpe at $5bn under a book that obeys the law is the
section 5 row with persistence 0.33 and permanent memory, 0.51, not the
memoryless 0.52 — a difference of 0.01, because the strategy's orders at
$5bn are 301 slices spread across a decade and the law bites per order, not
per decade. Which is the right place to end: the fill model that was wrong
about the law was wrong by a basis point of Sharpe for this strategy, and
would be wrong by much more for a strategy that built a position fast.

`metaorder_cost` drives the handler's own book on the flat tape rather than
re-implementing the arithmetic, for the reason every other section gives:
two copies of the mechanism would agree while both drifting. Ten tests, one
of which works the two-slice permanent case by hand to the quarter of a
basis point.

### 7. Charge the order, and the formula lands on the book — at the one AUM where either describes a trade

Reproduce with: `python fill_realism.py` (section 7 of the printout)

Section 6 found the flaw and ended on a prediction. Fix the formula so it
charges the root of the whole order rather than the slice, and the book
whose exponent and coefficient matched the law — persistence 0.33, permanent
memory — should land on it at every AUM, not at the one where two curves with
different exponents happened to cross. This section does the fix and runs
the prediction. The prediction was half right, and the half that was wrong
is the more useful half.

**The fix.** `ParticipationLimitedExecutionHandler` takes `impact_on`.
`"slice"` is what shipped: σ√(q/V) on each bar's slice against that bar's
volume. `"order"` is the law as published: σ√(Q/ADV), where Q is the whole
working order and ADV the trailing 20-day volume, both read once when the
order arrives, charged on every slice until it is done. The coefficient is
left to the caller and the studies use Y = 0.5, which is where Tóth et al.
put it; the shipped default of 1.0 on the slice is unchanged, because every
earlier table was run on it and this section is about what changes. A
handler that estimates σ from a tape with no volatility would charge nothing,
so `sigma_bps` can now be fixed, and the flat-tape test of section 6 is
rerun from the handler itself rather than from the arithmetic replica that
section wrote beside the book. The replica and the handler agree to nine
figures at every size, which is the "two copies of the mechanism" objection
that section ended on, closed.

| days of volume | 0.01 | 0.02 | 0.05 | 0.10 | 0.20 | 0.50 | 1.0 | 2.0 | 3.0 |
|---|---|---|---|---|---|---|---|---|---|
| formula on the slice, Y = 1 (ships) | 13.0 | 17.6 | 26.6 | 36.8 | 36.8 | 36.8 | 36.8 | 36.8 | 36.8 |
| formula on the order, Y = 0.5 | 7.5 | 9.8 | 14.3 | 19.4 | 26.6 | 40.9 | 57.0 | 79.8 | 97.3 |
| book, persistence 0.33, permanent (section 6) | 4.3 | 6.8 | 12.7 | 20.3 | 23.3 | 32.2 | 46.7 | 75.9 | 105.0 |

Exponent across sliced orders: 0.00 on the slice, 0.47 on the order. Not
0.50, because the 2 bps of slippage does not scale with size and pulls the
fitted slope down; with slippage off it is 0.50 to two decimals and a test
pins it. The order form and the book now run within 15% of each other from a
fifth of a day to three days, which they should, since the book's exponent
and implied Y were fitted to be this line. Inside one bar the book is
cheaper — 4.3 bps against 7.5 at a hundredth of a day — because the touch
is deep for a small order and the formula has no touch.

**The ladder.** Section 2 again, under three handlers that are now distinct:
the formula as coded, the formula as the law, and the book that matched it.
Cost is the share-weighted charge per slice in basis points.

| AUM | deployed | Sharpe: slice | order | book | cost: slice | order | book |
|---|---|---|---|---|---|---|---|
| $0.1bn | 100% | 0.60 | 0.63 | 0.64 | 10.6 | 6.2 | 3.7 |
| $1bn | 100% | 0.49 | **0.57** | **0.57** | 28.2 | **15.1** | **16.3** |
| $5bn | 41% | 0.44 | 0.45 | 0.51 | 33.4 | 31.3 | 21.3 |
| $20bn | 10% | 0.55 | 0.41 | 0.59 | 34.0 | 59.5 | 24.6 |
| $100bn | 2% | 0.73 | 0.57 | 0.70 | 37.4 | 119.4 | 47.6 |

At $1bn the law and the book land on each other: 0.57 and 0.57, 15 bps and
16. That is the prediction holding — and it holds for a reason section 6
did not expect. At $1bn the longest order in the run took two bars and most
took one, so the slice and the order are the same quantity and the two
forms of the formula differ only in Y. **The gap section 2 found between
the formula and the book at the readable AUMs was never the slicing. It was
the coefficient.** Y = 1 is twice the empirical value, and at Y = 0.5 the
formula charges a $1bn book 15 bps a slice where the shipped version charged
28, which is 0.08 of Sharpe — the whole of section 2's gap at that AUM.
Section 5 looked for that gap in the book's memory and found a 5-bar
half-life that reproduced it; section 6 showed that match was two different
curves crossing; this is what they were crossing *at*. The book with a third
of persistence and permanent memory, which section 6 chose for its exponent,
also has the law's coefficient, so it agrees with the law at the one AUM
where the law and the book are both describing an order that completes.

Above $1bn they come apart, and the way they come apart is the finding.
At $5bn the law charges 31 bps and the book 21; at $20bn, 60 against 25; at
$100bn, 119 against 48, with the law's Sharpe falling while the book's rises.
The law form charges every slice for the root of the *arrival* size, and at
$20bn the arrival size is a position that takes 41 bars to build and is 90%
never built — the deployed column. A square-root law is an empirical fact
about metaorders that were executed, over a day or a few; applied to an
order of which a tenth ever reaches the market, it charges the tenth that
filled for the nine tenths that did not. The book charges the slices that
happened, and remembers them. Neither is right about an order that does not
complete, because nothing measured on completed orders can be, and section 2
already said those rows are not Sharpe ratios. Which leaves a choice the
handler had to make and the docstring states: the law is applied to the size
the desk committed to when it sent the order, re-based if the order changes
while working. A desk that knew in advance it would get a tenth done would
have sent a tenth, and that is a strategy-sizing question, not a fill-model
one.

*Correction, section 8.* "90% never built" is the deployed column, and the
deployed column is the last order's fill — see the note under section 2.
Over the run the $20bn position was built, every time, in up to 41 bars,
and the book was in the market as much as an unconstrained one. The orders
did complete; they completed late. What the law form charges above $1bn is
still the root of a size that took a month to trade, and the sizing fix is
still the honest one, so the rest of this paragraph stands with "never
built" read as "built over a month".

**What this changes in the earlier sections.** Section 2's "square-root
Sharpe" column at the readable AUMs was a coefficient of 1 on a slice, and
the right number for $1bn is 0.57, not 0.49: the shipped formula overcharged
a billion-dollar book by 13 bps a trade. The deployed-capital cap is
unaffected, for the sixth time. Section 5's "why the formula was dearer" has
its answer in one word, and the word is Y. And the sentence section 5 wanted
to say — that the two handlers are one model in two parametrisations — is
true at exactly one AUM and for one reason, and the reason is not the one
that section proposed. Nothing ships differently: `impact_on` defaults to
`"slice"` and `impact_coef` to 1.0, because changing a default changes every
table above it, and the gap is now measured rather than hidden.

### 8. Send what you can get done — and find the book was never idle, it was stuck

Reproduce with: `python fill_realism.py` (section 8 of the printout)

Section 7 ended on a sizing question. Above $1bn the order sent is a
position that takes a month or more to build, and a desk that knows it will
get a tenth done sends a tenth. So `Portfolio` now takes a liquidity budget:
`max_fill_days` and `participation`, and the position from either sizing
scheme is clipped to days × participation × trailing ADV, the most that the
participation handler can fill inside that many bars. Every order completes
by construction and the question becomes what the strategy earns on the AUM
once it is only allowed to hold the part of it that fits.

Before running that, the column it was going to be read against had to be
checked, and it was wrong. **`deployed` is one minus the shares still
working when the data ran out, over the AUM.** On December 30, 2024 the
order still working was a sell, two bars old, at every AUM above $1bn. So 41% at $5bn,
10% at $20bn and 2% at $100bn are the fraction of *one order* — the last
one — that got done in two days. They are not how much of the book was in
the market. `Portfolio` now records its gross exposure every bar and
`invested()` averages it, against the unconstrained rule's own time in the
market (89%: a crossover rule on a tape that mostly went up is mostly long).

| AUM | deployed, as reported | in the market, time-averaged | longest fill, bars |
|---|---|---|---|
| $0.1bn | 100% | 101% | 1 |
| $1bn | 100% | 107% | 2 |
| $5bn | 41% | 107% | 10 |
| $20bn | 10% | 100% | 41 |
| $100bn | 2% | 101% | 161 |

The $100bn book was in the market exactly as much as the $100m one. It was
never idle. It was *stuck*: a position that takes 161 bars to build takes
161 bars to sell, the next crossover arrives before either is done, and the
book spends the run ramping toward a target it never reaches and then
ramping away from it. Sections 2 and 7 read the Sharpe rising with AUM as
cash in the denominator, and it is not that — at a zero rate a constant
cash fraction does not move a Sharpe at all. What it is: a seven-month
moving average of a crossover signal on a tape that went up is a lagged
buy-and-hold, and buy-and-hold's Sharpe over these ten years is 0.79. The
$100bn row's 0.73–0.75 is that, with the costs taken out. Every ladder
above this one has the right conclusion — those rows are not Sharpe ratios
of this strategy — attached to the wrong mechanism, and the correction is
noted under each.

**Now the sizing rule.** Same ladder, three handlers, the position capped at
five days of 10% participation. `position` is what the budget allowed as a
share of the AUM, on the median bar; `in the market` is relative to the
unconstrained rule.

| AUM | position | in the market | Sharpe: slice | order | book | return: slice | order | book | cost: slice | order | book | longest fill |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| $0.1bn | 100% | 101% | 0.60 | 0.63 | 0.64 | 124% | 130% | 134% | 10.6 | 6.2 | 3.7 | 1 |
| $1bn | 100% | 107% | 0.49 | 0.57 | 0.57 | 99% | 117% | 119% | 28.2 | 15.1 | 16.3 | 2 |
| $5bn | 100% | 107% | 0.44 | 0.45 | 0.51 | 81% | 83% | 100% | 33.4 | 31.3 | 21.3 | 10 |
| $20bn | **34%** | 48% | 0.47 | 0.43 | 0.54 | 44% | 40% | 52% | 35.5 | 43.1 | 23.0 | 16 |
| $100bn | **7%** | 11% | 0.44 | 0.40 | 0.51 | 9% | 8% | 10% | 35.5 | 43.1 | 23.0 | 16 |

Three things in that table.

*The Sharpe stops rising.* Once the book holds only what it can trade, the
$20bn and $100bn rows read 0.54 and 0.51 on the book handler, which is the
$5bn row's 0.51 — the Sharpe of the strategy at a size where it is a
strategy. The costs in the last three columns are identical at $20bn and
$100bn because the position is identical: 5 days × 10% × ADV is the same
share count whatever the AUM, so above the budget every row is the same
trade with more cash beside it. The return column is where the AUM shows
up: 52% on $20bn, 10% on $100bn, against 112% for the stuck book at $20bn
that was "fully invested" because it could not get out.

*Capacity has a number now, and it is a patience.* The budget binds where
the position the AUM asks for exceeds 5 days of participation, which on
this tape is **$12bn** at the median ($10bn in 2015, $14bn in 2024, as SPY's
share volume fell and its price tripled). A day's patience is $2.4bn; a
month's is $48bn. Below the line the strategy earns its full return at its
full-size Sharpe; above it the Sharpe holds and the return on AUM falls as
1/AUM. That is what a capacity curve looks like, and the deployed column was
never going to draw it.

*The law form is the one that pays for patience.* At $20bn the formula on
the order charges 43 bps a slice where the book charges 23, and its Sharpe
is the lowest of the three, because the order is now a five-day metaorder
that completes and the law charges every slice for the root of all five
days. Section 7 said the two agree where an order completes in a bar or
two; a five-day order is where they are supposed to differ, and they do,
with the book cheaper. Which one is right is the question section 6 could
not settle without intraday data, and still cannot.

**How much patience is worth, at $20bn.** The same book, the desk willing
to take longer.

| days to fill | position | in the market | Sharpe: slice | order | book | return, book | cost, book | longest fill |
|---|---|---|---|---|---|---|---|---|
| 1 | 7% | 11% | 0.48 | 0.54 | 0.55 | 12% | 20.3 | 3 |
| 2 | 14% | 21% | 0.46 | 0.49 | 0.53 | 22% | 21.6 | 6 |
| 5 | 34% | 48% | 0.47 | 0.43 | 0.54 | 52% | 23.0 | 16 |
| 10 | 68% | 81% | 0.45 | 0.34 | 0.52 | 76% | 23.0 | 23 |
| 20 | 100% | 100% | 0.55 | 0.41 | 0.59 | 112% | 24.6 | 41 |
| no cap | 100% | 100% | 0.55 | 0.41 | 0.59 | 112% | 24.6 | 41 |

From a day to ten the book's Sharpe sits between 0.52 and 0.55 and the
return climbs with the position, which is the budget doing exactly what it
should: the Sharpe is a property of the strategy and the return is a
property of how much of it you can hold. The order form's Sharpe falls from
0.54 to 0.34 along the same rows, because the longer the metaorder the more
the root of it costs; whether a ten-day metaorder in SPY really costs 43 bps
a slice is the thing the literature disagrees about by a factor of two. The
20-day row is the uncapped row, because at 20 days the budget is $48bn and
nothing at $20bn ever hits it — and its 0.59 is the stuck book's number, a
lagged buy-and-hold with the strategy's costs, which is higher than any
capped row and is not a number to want.

One thing the budget does not do, stated: the longest fill is 16 bars on a
five-day budget, not five. The budget is set against the trailing ADV at the
signal and the fill is capped at 10% of each bar's actual volume, so an
order sent into a quiet fortnight — or a holiday week — takes longer than
the average said it would. A desk would know that too. Nothing ships
differently: both parameters default to None, and every number above this
section was run without a budget. Section 9 makes the budget bind on the
exit as well, and finds out what that is worth.

### 9. Make the budget bind on the way out, and run the rule that flips — the exit shortens by three bars, and the reversal has no capacity to measure

Reproduce with: `python fill_realism.py` (section 9 of the printout)

Section 8 left two things open. The budget bound on the way in only: the
position was capped at what five days of participation could *build*, then
sold at whatever pace the tape allowed, which is how a five-day budget
produced a sixteen-bar fill. And the ladder was long-only, where the worst
order is a position's worth; the long/short version of the rule reverses,
and a reversal is two positions in one order.

**The budget on the way out.** `Portfolio` takes `trim_to_budget`: every
bar, every open position is checked against the budget as it stands — the
same days × participation × trailing ADV, with today's ADV — and anything
over it is sold down, inside the same rebalance band vol targeting uses.
A desk that cannot get out in five days does not own the position. Off by
default; section 8's rows were run with it off. Same ladder, the book
handler, entry-only against trimmed:

| AUM | Sharpe, entry only | trimmed | return, entry only | trimmed | in the market, entry only | trimmed | longest fill, entry only | trimmed | trims | cost, entry only | trimmed |
|---|---|---|---|---|---|---|---|---|---|---|---|
| $0.1bn | 0.64 | 0.64 | 134% | 134% | 101% | 101% | 1 | 1 | 0 | 3.7 | 3.7 |
| $1bn | 0.57 | 0.57 | 119% | 119% | 107% | 107% | 2 | 2 | 0 | 16.3 | 16.3 |
| $5bn | 0.51 | 0.54 | 100% | 104% | 107% | 103% | 10 | 10 | 6 | 21.3 | 20.6 |
| $20bn | 0.54 | 0.52 | 52% | **36%** | 48% | 38% | 16 | **13** | 48 | 23.0 | 21.6 |
| $100bn | 0.51 | 0.50 | 10% | 7% | 11% | 9% | 16 | 13 | 48 | 23.0 | 21.6 |

Below the budget nothing happens, which is the first check. At $20bn the
trim fires 48 times in ten years — about once every seven weeks — and the
reason is not a quiet fortnight here and there. SPY's share volume fell
through the decade as its price tripled, so a position sized to the ADV at
the signal is over the line a few weeks later more often than not, and the
book sells down a little, again and again, into the same falling volume.
That costs a third of the return — 52% to 36% on the AUM — for a Sharpe
that does not move (0.54 to 0.52), and it holds a fifth less of the book.
The longest fill goes from 16 bars to 13, not to 5. The budget is a
statement about the trailing average and the fill is 10% of each bar's
*actual* volume; a trim is an order into the same thin tape as the exit it
is meant to shorten, so it shortens it by what it sold before the signal
and no more. "Hold only what you can get out of" is a discipline that costs
return and buys three bars, on this tape. A desk would still run it,
because the sixteen-bar exit is the one that happens in the week it
matters, and the Sharpe says the return it gives up was the return of
being stuck.

The $5bn row is the one to be suspicious of: six trims, Sharpe up 0.03.
That is noise with a sign, not a result.

**The rule that flips.** `long_short_ladder` runs
`MovingAverageCrossLongShortStrategy` — short where the long-only rule went
flat — on the same ladder, borrow charged at 75bp (the rate `long_short.py`
uses, chosen pessimistically), unconstrained and under the five-day budget.
`position` is what the budget allows, which is the same share count as
section 8's, because the budget caps the position and does not know the
rule reverses.

| AUM | position | Sharpe, free | budgeted: slice | order | book | return, free | book, budgeted | longest fill, free | budgeted | long-only, budgeted | cost, book | borrow paid |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| $0.1bn | 100% | 0.25 | 0.24 | 0.24 | 0.25 | 20% | 20% | 1 | 1 | 1 | 5.9 | 40bp/yr |
| $1bn | 100% | 0.27 | **−0.57** | 0.25 | 0.27 | −9% | −9% | 4 | 4 | 2 | 19.0 | 39 |
| $5bn | 100% | 0.11 | −0.12 | **−0.32** | 0.12 | −40% | −37% | 18 | 18 | 10 | 22.6 | 34 |
| $20bn | 34% | 0.22 | 0.02 | −0.12 | 0.11 | 27% | 7% | 74 | **24** | 16 | 27.0 | 11 |
| $100bn | 7% | 0.35 | −0.03 | −0.18 | 0.06 | 63% | 1% | 531 | 24 | 16 | 40.4 | 2 |

Three things, and the first is the one the section was run for.

*The flip takes twice as long, until the budget binds, and then half again.*
Longest fill, long/short against long-only under the same budget: 4 against
2 at $1bn, 18 against 10 at $5bn, 24 against 16 at $20bn and above. Below
the budget a reversal is two positions' worth of shares into the same
participation, so it takes twice the bars, as it should. At the budget both
rules hold the same capped position and the reversal is two of those: ten
days nominal, 24 actual against the long-only's 16, because both are
stretched by the same quiet weeks and the reversal spends longer in them.
Unconstrained, the reversal at $100bn takes **531 bars** — two years — and
never completes before the next signal, which is section 8's stuck book
twice over: a rule that is always meant to be long or short and is, in
practice, slowly becoming one of them.

*The rule has no capacity, because it has no edge to scale.* Long/short
on SPY over 2015–2024 is Sharpe 0.25 at $100m with a one-bar fill, which is
`long_short.py`'s finding on 24 ETFs read on one: the short leg of a trend
rule on an equity index loses money, and borrow at 75bp is 40bp a year on
top. Capacity is the AUM at which the Sharpe of a strategy stops being the
Sharpe of the strategy, and this one's never was anything to protect. Under
the budget the $20bn book earns 7% on the AUM in ten years and the $100bn
book 1%, at a Sharpe of a tenth: the budget does its job — the return on
AUM falls as 1/AUM above the line, exactly as in section 8 — and the thing
it is scaling is worth nothing. The free rows' rising Sharpe (0.22, 0.35)
is the stuck book again, and this time with a 531-bar fill it is not even
a lagged buy-and-hold, it is a lagged nothing.

*The formula on the order is the law's verdict on reversals, and it is
severe.* The `order` column charges a reversal as one metaorder of two
positions, root of the whole size on every slice, and at $5bn that is
43 bps a slice over 345 slices: a −96% return, the strategy's capital gone.
The slice form at $1bn (−0.57, −52%) is the same thing at a smaller size.
The book handler, which charges each bar's slice against that bar's depth
and lets the book heal, reads 0.12 at $5bn. Section 7 said the two forms
disagree by 2× on a five-day order and the literature does not settle it;
on a ten-day reversal they disagree by the whole strategy, and the same
intraday data would settle both.

What this leaves. The trim sells down and never tops up, so a fixed-size
book that drifted *under* its budget on a loud week is not rebuilt — the
signal did not ask for more, and a budget is a ceiling. The long/short rows
share section 8's one free parameter, the 10% participation, and the flip's
24 bars is at that rate. And the borrow is the only carry charged: the
short's rebate on the proceeds, which at 2023 rates would have paid for the
borrow several times over, is not credited, which flatters nothing here
because the short leg loses before carry.

### 10. Sweep the one free parameter, and bootstrap the sign — aggression is paid in impact, patience in time, and the +0.03 was noise

Reproduce with: `python fill_realism.py` (section 10 of the printout)

Section 9 left two loose ends and named them. The trim ran at 10%
participation, which is section 8's one free parameter and has been 10%
since section 2 because 10% is a round number; and the $5bn row read
+0.03 of Sharpe off six trims, which the text called noise with a sign
without checking. Both are a sweep and a bootstrap away, so here they are.

**The participation sweep.** `participation_sweep` reruns the trimmed
$20bn row — the one where the trim fires 48 times — at every rate from 2%
to 50%, under all three handlers. Participation sits on both sides of the
budget: the position allowed is days × participation × ADV, and the fill
is participation × the bar's actual volume, so a desk willing to be 20%
of the tape holds twice the position of one at 10% and gets out of it at
twice the pace. The prediction going in, from the Oct 22 note, was that
the 20% desk "gets out in half the time and pays for it in impact".

| participation | position allowed | Sharpe: slice | order | book | return, book | longest fill | trims | cost, slice | order | book |
|---|---|---|---|---|---|---|---|---|---|---|
| 2% | 7% | 0.53 | 0.52 | **0.60** | 8% | 13 | 48 | 16.4 | 17.6 | **7.4** |
| 5% | 17% | 0.48 | 0.46 | 0.57 | 20% | 13 | 48 | 24.7 | 26.7 | 13.6 |
| 10% | 34% | 0.42 | 0.40 | 0.52 | 36% | 13 | 48 | 34.1 | 36.9 | 21.6 |
| 20% | 68% | 0.32 | 0.30 | 0.43 | 60% | 13 | 40 | 46.6 | 50.1 | 32.1 |
| 30% | 100% | 0.33 | 0.32 | 0.48 | 90% | 13 | 18 | 56.3 | 57.0 | 38.4 |
| 50% | 100% | 0.25 | 0.30 | 0.43 | 87% | 13 | 1 | 71.7 | 60.5 | 44.1 |

Half of the prediction held. *The longest fill is 13 bars at every rate.*
Not approximately — exactly, because the budget and the fill scale
together and the ratio is what the exit takes: five days of ADV at the
signal, sold at the same share of the actual tape, is thirteen bars when
the tape is thin, whether the share is 2% or 50%. Aggression does not buy
time. It buys position — 7% of the AUM at 2%, all of it at 30% — and the
whole bill for that position is impact. The book handler charges 7 bps a
slice at 2% and 44 at 50%, and the Sharpe goes from 0.60 to 0.43 under it,
0.53 to 0.25 under the formula on the slice. At 30% the budget stops
binding, the trim all but stops firing, and the row is section 7's
unconstrained $20bn book with the costs of being 30% of the tape.

That makes the comparison with section 8 the point of the section. Here
are the two ways of doubling the $20bn book from 34% to 68% of the AUM,
both on the book handler:

| the knob | position | Sharpe | return | cost, bps a slice | longest fill |
|---|---|---|---|---|---|
| patience: 5 days → 10, at 10% (section 8) | 34% → 68% | 0.54 → 0.52 | 52% → 76% | 23.0 → 23.0 | 16 → 23 |
| aggression: 10% → 20%, at 5 days | 34% → 68% | 0.52 → **0.43** | 36% → 60% | 21.6 → **32.1** | 13 → 13 |

**Patience is paid in time and aggression in impact, and they are not the
same bill.** Doubling the book by waiting costs nothing a slice and seven
bars on the worst exit; doubling it by taking more of each bar costs ten
basis points a slice, a tenth of Sharpe, and no bars at all. Section 8
found that capacity is a patience; this finds that the Sharpe of that
capacity is a participation, and that the one free parameter the whole
capacity ladder has run on since section 2 is the one that sets the
Sharpe, not the one that sets the size. A desk that reads 0.52 at $20bn
and 10% is reading a number that would be 0.60 at 2% and 0.43 at 20% on
the same tape with the same rule, and none of the three is more right
than the others. It is a choice, and the ladder should say so.

All three handlers agree on the direction and disagree on the level, by
about a tenth: the book is dearest to a small order and cheapest to a
large one relative to the formula (7 against 16 bps at 2%, 44 against 72
at 50%), which is section 1's finding — the flat charge is wrong in both
directions — showing up as a Sharpe gap instead of a cost gap. The
formula on the order is within two bps of the formula on the slice at
every rate here, because at $20bn under a five-day budget every order is
the same five-day order and the two forms differ only in how they slice
it.

**The bootstrap.** `trim_bootstrap` runs section 9's entry-only and
trimmed books at $5bn and $20bn, takes the two daily return series, and
hands them to `significance.paired_difference` — the stationary bootstrap
from the parameter-grid study, both series resampled on the same days, so
the interval is on the difference and not on two Sharpes that happen to
share a market.

| AUM, book handler | Sharpe, entry only | trimmed | difference | 95% interval | P(diff ≤ 0) | days the two books differ |
|---|---|---|---|---|---|---|
| $5bn | 0.51 | 0.54 | +0.03 | [−0.02, +0.09] | 13% | 1,037 |
| $20bn | 0.54 | 0.52 | −0.02 | [−0.22, +0.18] | 55% | 1,893 |

*The +0.03 was noise, and so was the −0.02.* Section 9's instinct was
right at $5bn: six trims, a difference whose interval straddles zero,
and a sign that 13% of resamples flip. It was also right at $20bn without
saying so — the trimmed book holds a fifth less and the two curves differ
on three quarters of the days, so the interval is four times wider and
the sign is a coin flip at 55%. The thing section 9 could say about the
trim, and still can, is what it does to the return (a third of it) and the
exit (three bars); what it does to the Sharpe is not distinguishable from
nothing at either AUM, on ten years of one tape.

What this leaves. The participation sweep is one AUM, and the rate at
which the budget stops binding (30% here) is a function of the AUM and
the tape, not a constant. Impact at 30% or 50% of the tape is outside
anything the depth profile of section 1c was fitted on; those rows are
the handler extrapolating, reported because the sweep should run until
the budget stops binding and not because a desk would trade there. And
the bootstrap's block length is the parameter grid's 20 days; the trim
fires every seven weeks, so a longer block would widen both intervals,
which moves neither conclusion.

### 11. Run the whole surface, and find capacity was never an AUM — past the knee every cell at a rate is the same dollars

Reproduce with: `python fill_realism.py --section 11` (section 11 of the
printout; the flag re-runs one section and merges it into the results file,
leaving the other ten as they were)

Section 10 ended on the sentence that the capacity ladder's Sharpe is the
participation it was run at, which makes a ladder — one axis, AUM, with
the rate hidden inside the handler — the wrong shape for the claim. This
runs the two-axis version: section 9's trimmed book, book handler, five-day
budget, at seven AUMs from $100m to $100bn and the six rates of section
10, 42 cells. The prediction written down before it ran: at each rate, the
Sharpe falls with AUM only until the budget binds, and is flat beyond,
because past that point the position is days × rate × ADV whatever the
AUM says, so the slices, the fills and the exits are the same run and the
extra AUM is cash, which a Sharpe does not see. If that holds, every
column is a staircase with one step and the surface collapses to the rate.

`capacity_surface` runs the grid. `surface_knees` reads off, per rate,
the first rung the budget binds on — the smallest AUM holding less than
the whole — and the spread of the Sharpes from there up, which is the
number that says whether the prediction held.

**The surface.** Sharpe, then the share of the AUM the budget let the
desk hold:

| AUM | 2% | 5% | 10% | 20% | 30% | 50% |
|---|---|---|---|---|---|---|
| $100m | 0.64 | 0.64 | 0.64 | 0.64 | 0.64 | 0.64 |
| $1bn | 0.63 | 0.60 | 0.57 | 0.56 | 0.56 | 0.56 |
| $2bn | **0.60** | 0.58 | 0.56 | 0.51 | 0.51 | 0.51 |
| $5bn | 0.62 | **0.56** | 0.54 | 0.48 | 0.46 | 0.44 |
| $10bn | 0.61 | 0.58 | **0.50** | 0.47 | 0.43 | 0.41 |
| $20bn | 0.60 | 0.57 | 0.52 | **0.43** | 0.48 | 0.43 |
| $100bn | 0.60 | 0.56 | 0.50 | 0.42 | **0.42** | **0.40** |

| AUM | 2% | 5% | 10% | 20% | 30% | 50% |
|---|---|---|---|---|---|---|
| $100m | 100% | 100% | 100% | 100% | 100% | 100% |
| $1bn | 100% | 100% | 100% | 100% | 100% | 100% |
| $2bn | **68%** | 100% | 100% | 100% | 100% | 100% |
| $5bn | 27% | **68%** | 100% | 100% | 100% | 100% |
| $10bn | 14% | 34% | **68%** | 100% | 100% | 100% |
| $20bn | 7% | 17% | 34% | **68%** | 100% | 100% |
| $100bn | 1% | 3% | 7% | 14% | **20%** | **34%** |

Bold is the knee: the first rung at which the desk holds less than it
asked for. And from the knee up, per rate:

| rate | knee AUM | holds | Sharpe, last rung that fits | Sharpe past the knee | spread | ten-year P&L past the knee, every rung |
|---|---|---|---|---|---|---|
| 2% | $2bn | 68% | 0.63 | 0.60–0.62 | 0.02 | **$1.68bn** |
| 5% | $5bn | 68% | 0.58 | 0.56–0.58 | 0.02 | **$3.94bn** |
| 10% | $10bn | 68% | 0.54 | 0.50–0.52 | 0.02 | **$7.18bn** |
| 20% | $20bn | 68% | 0.47 | 0.42–0.43 | 0.01 | $11.95bn → $12.25bn |
| 30% | $100bn | 20% | 0.48 | 0.42 | — | $18.21bn |
| 50% | $100bn | 34% | 0.43 | 0.40 | — | $27.24bn |

**It held, and more exactly than a Sharpe can show.** The spread past the
knee is two hundredths at most. The dollars are tighter than that: at 2%
the book makes $1,684.6m over the ten years at $5bn, at $10bn, at $20bn
and at $100bn — the same number to the dollar — and at 5% it is
$3,939.6m at every rung from $10bn up, at 10% $7,178.8m. The knee rung
itself is not quite there yet (40 trims against 48, the budget binding on
most orders rather than all), and the rung after it is, and from there up
the AUM is a number on the account statement. The two hundredths the
Sharpe moves is the cash: the daily return divides the same P&L by an
equity that drifts with it, and a $100bn book drifts less. So the surface
is not a surface. Past the knee it is six numbers, one per rate.

**Where the knee is.** It lands at the same rung in every column: the AUM,
in billions, equal to the rate in points — $2bn at 2%, $5bn at 5%, $10bn
at 10%, $20bn at 20% — and at the same 68% each time, because the budget
is five days of the rate's share of ADV and nothing else. On this tape a
point of participation and a week buys five days of 1% of 80 million
shares at the $169 they cost in January 2015: $0.68bn of position per
point, which is the 68% of a billion the knee sits at. That is the
capacity statement, and it has no Sharpe in it.

**And on the near side of the knee, the other axis stops mattering.** At
$1bn the Sharpe reads 0.56 at 20%, 30% and 50% alike: a $1bn order fills
inside one bar at a fifth of SPY's volume, and a desk that is willing to be
half the tape gets the same fill as one that is willing to be a fifth,
because the order is gone before the difference arises. Below the knee the
participation is slack; above it the AUM is. Each axis bites on one side,
and the knee is where they hand over.

**So the honest capacity claim.** Not "$12bn at a week" (section 8) and
not "$12bn at 10% and a week" (what section 10 asked for) but: at 10% of
the tape and a five-day budget, this rule takes $7.2bn off ten years of
SPY at a Sharpe of 0.50, from any AUM above $10bn; at 2% it takes $1.7bn
at 0.60; at 50% it takes $27bn at 0.40. The rate you choose is the trade
between the two columns. The patient desk has the best Sharpe and the
smallest P&L, which is the thing a Sharpe never says about capacity: it
is a ratio, and the desk is paid in the numerator.

The one cell that wobbles is $20bn at 30%, 0.48 against 0.43 either side
of it — a book that fits whole but only just, 34 of its orders capped and
18 trimmed, the knee's own neighbour. The sweep ran to 50% for the reason
section 10 gave, that the budget should stop binding somewhere in the
grid, and the depth profile was not fitted at those rates, so the 30% and
50% columns are the handler extrapolating.

### What this still does not model

- One bar is one book. A day's liquidity is pooled into a single snapshot at
  the open, which overstates what is at the touch in any instant and
  understates how long working an order really takes.
- ~~The book is rebuilt every bar and remembers nothing between them.~~
  Section 5. `persistence` carries a share of each slice's displacement into
  the next bar's mid and `recovery` lets it heal. Permanent impact nets out on
  the round trip and the transient version lands on the square-root formula at
  a half-life of a week, which says what the formula's constant was. Ships at
  0 because both parameters are unobserved here and the memoryless book is
  the one setting nobody chose.
- **The memoryless book that ships does not obey the square-root law, and
  neither does the formula it is compared against.** Section 6. Both charge
  a sliced order the same per share whatever its total size; the law says
  cost grows with the root of it. A book with persistence 0.33 and no
  recovery reproduces the law's exponent and coefficient. It still ships at
  0, for the drift reason in section 5, and the gap this leaves is stated
  rather than tuned away: a strategy that builds a position fast is
  undercharged by every handler here. Section 7 gives the formula handler
  the law's form (`impact_on="order"`) and finds it lands on that book at
  $1bn, for the coefficient rather than the slicing, and that above $1bn no
  handler here describes an order that completes.
- ~~**The law is applied to the order that was sent, not the one that got
  done.**~~ Section 8. The order that was sent now *gets* done: the
  portfolio clips the position to what participation fills inside
  `max_fill_days`, every order completes, and the capacity number is a
  patience — $12bn at a week's, $2.4bn at a day's. And the "90% never
  built" this bullet rested on was the last order's fill, not the book's;
  the book was always in the market, built late and sold late. What a
  five-day metaorder in SPY actually costs — 23 bps a slice on the book or
  43 on the law — is still the open question, and still needs intraday data.
  Section 9 makes the budget bind on the way out too (the longest fill goes
  from 16 bars to 13, for a third of the return at $20bn) and runs the
  long/short rule, whose reversal takes twice as long below the budget and
  half again at it — and which has no capacity to measure, because on this
  tape it has no edge to scale. Section 10 sweeps the participation the
  whole ladder has run on: the worst exit is 13 bars at every rate, because
  the budget and the fill scale together, so aggression buys position and
  not time, and pays for it in impact — 0.60 at 2% to 0.43 at 20% on the
  same tape. Patience is paid in time, aggression in impact, and the Sharpe
  of the capacity number is the participation it was run at. The +0.03 the
  trim read at $5bn bootstraps to [−0.02, +0.09]: noise, as section 9 said.
  Section 11 runs the whole AUM × participation surface and finds capacity
  was never an AUM: past the rung the budget binds on, every cell at a
  rate makes the same dollars to the dollar — $1.7bn at 2%, $7.2bn at 10%,
  $27bn at 50% — and the AUM is a number on the account statement. The
  knee is $0.68bn of position per point of participation and week, and the
  rate is the trade between the Sharpe column and the P&L column.
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
- The depth profile that ships rises away from the touch and never decays.
  Section 1d has the decaying version and the reason it is off: it moves the
  crossover by 20x on a parameter nothing here observes, so the dearest book
  of the family ships and the sweep is the error bar.
