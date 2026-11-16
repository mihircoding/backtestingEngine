# Results

All 80 tests pass (`python -m pytest -q`). Numbers below are `python run_simulation.py`
(section 6 is `python latency_study.py`):
50,000 events, seed 7, book seeded with 5 levels of 100 shares either side of 100.00.

```
Trades executed : 35,128
Volume traded   : 1,850,207
Mid             : 100.00 -> 100.08  (range 99.94 - 100.15)
```

**Nothing below was programmed in.** The simulator specifies an event mix (60% limit, 25%
market, 15% cancel), a random side, and a random price offset. It never specifies a spread, a
depth profile, an impact function, or a price process. Everything that follows is a consequence
of price-time priority matching.

---

## 1. Spread

| | |
|---|---|
| mean | 0.0198 |
| median | 0.0200 |
| min | 0.0100 (one tick) |
| max | 0.1300 |
| share of time at the 1-tick minimum | **37.9%** |

The spread is an emergent equilibrium between two forces the simulator *does* specify: market
orders eat depth at the touch, and passive limit orders replace it. When consumption outruns
replacement the touch empties and the spread widens; when replacement wins, someone posts inside
and it collapses back to one tick.

This is worth sitting with, because it's the whole argument for building the null model. If you
had only ever seen the output you might reach for an explanation involving market makers
managing inventory risk and adverse selection. There are no market makers here. There is no
inventory. There is no information. A distribution of spreads with a hard floor at the tick and
a long right tail is what a matching engine does when you feed it noise.

## 2. Depth profile (final book)

| Level | Bid px | Bid qty | Ask px | Ask qty |
|---|---|---|---|---|
| 1 | 100.07 | 940 | 100.10 | 739 |
| 2 | 100.06 | 5,867 | 100.11 | 6,772 |
| 3 | 100.05 | 43,400 | 100.12 | 102,217 |
| 4 | 100.04 | 76,773 | 100.13 | 74,866 |
| 5 | 100.03 | 64,406 | 100.14 | 47,915 |
| 6 | 100.02 | 35,342 | 100.15 | 20,416 |
| 7 | 100.01 | 19,458 | 100.16 | 12,214 |
| 8 | 100.00 | 11,387 | 100.17 | 6,875 |

Depth is thin at the touch and hump-shaped a few ticks out. Real books have this shape too.

**But this is the least realistic output here, and the reason is instructive.** Zero-intelligence
agents never reprice. A real trader who posts three ticks away and watches the market leave
pulls the order and reposts; here it sits forever until a random cancel happens to select it.
So liquidity accumulates far from the mid at levels that would never survive in a real book —
100,000 shares three ticks out is not a market, it's an artifact of agents with no memory.

The honest summary: the *shape* is emergent, the *magnitude* is an artifact. Distinguishing
those two is the entire skill in reading a simulation.

## 3. Price impact

| Market order size | Mean \|mid move\| | n |
|---|---|---|
| 1–50 | 0.000407 | 2,640 |
| 51–100 | 0.001099 | 3,321 |
| 101–150 | 0.001840 | 3,346 |
| 151–200 | 0.002573 | 3,284 |

Power-law fit: **impact ∝ size^0.95**.

Bigger orders move the price more — obviously — but **sublinearly**. Doubling the order size
costs less than double the impact. That concavity is one of the most robust empirical facts in
market microstructure, and it emerges here from nothing but walking a book with a hump-shaped
depth profile: the deeper you go, the more size sits at each successive level.

Real markets sit closer to **square-root impact** (exponent ≈ 0.5), which is much more concave
than 0.95. The gap is the depth profile again: real liquidity replenishes strategically after a
large trade, and real large orders are worked in slices rather than fired at once. The mechanism
producing concavity is the same; the strength of it depends on behavior this model doesn't have.

## 4. Is the mid a random walk?

Lag-1 autocorrelation of mid changes: **−0.0440**.

| Horizon k | var(mid₍t+k₎ − mid₍t₎) | Ratio to k=1 | Random walk would be |
|---|---|---|---|
| 1 | 0.000010 | 1.00 | 1.00 |
| 2 | 0.000019 | 1.91 | 2.00 |
| 4 | 0.000034 | 3.45 | 4.00 |
| 8 | 0.000057 | 5.77 | 8.00 |
| 16 | 0.000087 | 8.89 | 16.00 |
| 32 | 0.000128 | 13.00 | 32.00 |
| 64 | 0.000172 | 17.57 | 64.00 |
| 128 | 0.000220 | 22.46 | 128.00 |

Fitted scaling: **var ∝ k^0.64**, i.e. **Hurst exponent H = 0.32** against 0.50 for a true
random walk.

A random walk's variance grows linearly in the horizon — double the time, double the variance.
This doesn't. At k = 128 the variance is 22× the one-step variance, not 128×. Combined with the
negative lag-1 autocorrelation, the mid is **sub-diffusive: it mean-reverts at short horizons.**

Two mechanisms, both mechanical:

1. **Bid-ask bounce.** The mid ticks up when the best ask is consumed and down when the best bid
   is, and those events alternate roughly at random. That alternation is negative
   autocorrelation by construction.
2. **The restoring force of resting depth.** A market buy that lifts the touch leaves a *thinner*
   ask and a *fatter* bid behind it. The book is now asymmetric in a way that makes the next move
   more likely to go back down. Depth pushes back.

**This is the most useful result in the project, and it is a warning.** Real tick data shows the
same signature, and a naive reading says "the mid mean-reverts, therefore buy dips at the tick
level." But you cannot trade the mid — you buy at the ask and sell at the bid. The mean reversion
here is 0.001-ish per event against a mean spread of 0.0198. **The signal is roughly twenty
times smaller than the cost of acting on it.** Predictability and profitability are different
things, and short-horizon microstructure is where people most reliably confuse them.

It also means: if your alpha model finds mean reversion at the tick level, you have probably
rediscovered bid-ask bounce. Test against a zero-intelligence null before believing otherwise.

---

## Correctness

The matching rules are tested as scenarios rather than as functions — `tests/test_orderbook.py`
reads as the specification:

- Marketable limit orders fill at the **maker's** price, not the taker's limit.
- FIFO within a price level: two 100-share sells at 101.00, a 150-share buy arrives, and the
  fills are 100 from the first and 50 from the second, in that order.
- Price priority beats time priority across levels: a better ask that arrives *later* still
  fills first.
- Partial fill remainder rests as the new best on the opposite side.
- Matching stops when the price no longer crosses — a 300-share buy at 100.00 against asks at
  100.00 and 102.00 fills 100 and rests 200.
- Cancels preserve the queue position of everything else at the level.
- A filled order cannot be cancelled.

## 5. What it costs to run

Everything above is about what the book *produces*. This is about what it costs, which is the
other half of the reason exchanges are interesting: a matching engine is a latency-sensitive
piece of infrastructure before it is a source of stylized facts.

`benchmark.py` sweeps book depth and times two implementations in the same process — the
current one, and `ScanBook`, which overrides exactly one method to restore the previous
`max(bids)` / `min(asks)` lookup. Everything else is shared, so any difference is attributable
to best-price lookup alone.

| levels | scan (ev/s) | heap (ev/s) |
|---|---|---|
| 10 | 73,727 | 80,720 |
| 50 | 68,411 | 81,856 |
| 100 | 62,227 | 79,588 |
| 500 | 30,576 | 82,596 |
| 1,000 | 17,492 | 79,948 |

Scanning the price dict costs 4.2x throughput going from 10 to 1,000 levels. The current book
costs nothing measurable over the same range — 80,720 against 79,948, which is inside the noise.
In isolation the lookup itself is 29x faster at 1,000 levels (8,669ns → 302ns).

That right-hand column used to fall off too, by 2.2x across the same sweep. What was left after
the heaps fixed best-price lookup was cancellation, and this is the section where it got found.

### The cancel tail, and what fixed it

Per-operation latency at 500 levels, microseconds, as it stood before:

| operation | n | p50 | p99 | max |
|---|---|---|---|---|
| add | 12,063 | 4.22 | 15.49 | 157.15 |
| cancel | 4,946 | 1.94 | **170.38** | 469.48 |
| market | 2,991 | 13.44 | 43.92 | 704.00 |

Cancel was the fastest median operation in the book and had by far the worst tail — an 88x
p50-to-p99 gap. The cause is one line: `cancel()` found the order in O(1) through `_by_id` and
then called `deque.remove()`, which is O(orders resting at that price level). Cancelling at the
front of a queue was instant; cancelling at the back of a deep one walked the whole queue. The
caller neither chose their queue position nor can see it, so the same API call cost 2μs or 170μs
depending on something invisible — and in real equity flow, well over 90% of orders are cancelled
rather than filled, so this was the hot path wearing the worst distribution.

The fix is the same idea the heaps already used: **don't delete, tombstone**. `cancel()` flags
the order inactive, drops its id, decrements the level's live-order count, and returns. The
order stays physically in its deque until the matching loop reaches it and throws it away, which
is the one moment the work was unavoidable anyway. Per-level live counts (`_bid_live` /
`_ask_live`) are what keep this honest: when the last live order at a price goes, the price key
is deleted in O(1) — so `best_bid()`, `depth()` and the heaps never see a level that nobody is
quoting, and the dropped deque takes its tombstones with it.

Same flow, both implementations timed in the same process (`EagerCancelBook` restores the old
`deque.remove()` and overrides nothing else):

| cancel() | p50 | p99 | max | p99/p50 |
|---|---|---|---|---|
| eager (splice the deque) | 2.09 | 79.62 | 131.81 | 38x |
| lazy (tombstone) | **0.89** | **3.21** | **43.62** | **4x** |

The median halves, which was not the point. The 99th percentile falls by 25x and the ratio
between them goes from 38x to 4x, which was. A matching engine is judged on its tail: a venue
whose cancels are usually fast and occasionally 80μs is a venue that occasionally fails to pull
a quote in time, and the times it fails are exactly the busy ones.

The cost is memory, and rather than assert it stays bounded the benchmark counts it: after
20,000 events at 500 levels the book is holding **1,787 tombstones against 4,065 live orders**,
worst single level 368. They are freed two ways — the matching loop discards them as it passes,
and an emptied level drops its whole deque at once.

`tests/test_lazy_cancel.py` runs the lazy book and the eager one through identical random flow
and compares every trade and both quotes after every event, because an optimization that changes
an answer is not an optimization.

These are single-run numbers from CPython on a shared VM and they wobble 10-20% between runs.
The shape of the curves is the result; the absolute figures are not.

## 6. What being slow costs, in ticks

The list of things this project doesn't model has always started with latency, with the note
that its absence means nothing here touches the actual subject of low-latency trading. That is
now `src/latency.py` and `python latency_study.py`.

The model rests on one fact about a matching engine: **it does not know when you decided.** It
knows when your packet arrived, and it serves arrivals in order. `MessageBus` is a priority queue
keyed by (arrival, sequence), where arrival is submission time plus a per-participant latency
draw — a hard floor plus an exponential tail, because a symmetric distribution would imply
messages arriving faster than physics allows. The sequence number breaks ties, which is not a
detail: ordering a tie by submission time would hand it to whoever decided first, which is
exactly the advantage latency is supposed to take away.

The experiment is the smallest one containing the real effect. Two market makers quote 100
shares one tick either side of fair value. Identical logic, identical information, identical
size — the only difference between them is how long their messages take to reach the book. Fair
value jumps three ticks 25% of the time; informed flow reacts to the jump in 50µs; uninformed
flow arrives at random. Every fill is marked against fair value immediately afterwards, so one
tick per share means the full quoted edge was captured and nothing was lost to information.

The fast maker is fixed at 10µs. Only the slow one moves.

```
fast maker (10us)
 slow maker | quoted vol  toxic%  passive  took vol  taking     net      total
       10us |    362,766   34.0%    0.779    24,906   1.966   0.855    331,455
       25us |    456,710   21.1%    1.000    72,835   1.991   1.136    601,827
       50us |    451,833   21.3%    1.000    77,671   1.990   1.145    606,482
      100us |    445,532   21.6%    0.999    78,221   1.990   1.147    600,977
      250us |    423,473   22.7%    0.997    79,828   1.993   1.155    581,085
     1000us |    311,305   31.1%    0.983    89,023   1.993   1.207    483,365

slow maker
 slow maker | quoted vol  toxic%  passive  took vol  taking     net      total
       10us |    359,007   33.8%    0.793    26,799   2.005   0.878    338,578
       25us |    283,433   59.7%    0.234         0   0.000   0.234     66,427
       50us |    251,190   30.9%    0.082         0   0.000   0.082     20,579
      100us |    256,869   30.5%    0.102         0   0.000   0.102     26,171
      250us |    276,415   28.9%    0.186         0   0.000   0.186     51,281
     1000us |    373,766   23.8%    0.475       491  -0.754   0.473    176,982
```

`passive` is P&L per share on the maker's own resting quotes; `taking` is P&L per share where it
was the aggressor; `total` is the run's whole P&L in ticks.

**Fifteen microseconds is worth 80% of the business.** The first row is the control: identical
latency, and the two makers split the run almost exactly, 331,455 ticks against 338,578. Move one
of them from 10µs to 25µs and the split becomes 601,827 against 66,427. Nothing else changed —
same flow, same seed, same quoting logic, same size. At 50µs the slow maker keeps 20,579 ticks,
6% of what it earned at parity.

**And the total barely moves.** 670,033 ticks at parity, 668,254 at a 15µs gap. Latency does not
create value here; it decides who collects value that the flow was going to pay either way. That
is the cleanest statement this project can make about why firms spend money on microwave towers,
and it comes out of the accounting rather than from an argument.

**The slow maker does not trade less. It trades worse.** At 25µs it still fills 283,433 shares on
its own quotes — 79% of what it filled on them at parity — and earns 0.234 ticks on each instead of 0.793.
Look at the `toxic%` column on that row: 59.7% of what it traded came from informed flow, against
21.1% for the fast maker on the same run. It is not being excluded from the market. It is being
selected into the half of the market that costs money. A maker looking only at fill rates would
see a healthy business.

**The fast maker's second income stream is the interesting one.** Its `passive` number is 1.000 —
a perfect score, never adversely selected at all, because its cancel always beats the informed
order. But look at `took vol`: 72,835 shares where it was the aggressor, at 1.991 ticks each. Its
own requote is what takes them. After a three-tick jump down, the new ask it posts sits *below*
where the competition's stale bid still is, so posting it crosses. That is latency arbitrage, and
it was not written into the maker's logic — the maker only knows how to cancel and re-post around
fair value. It falls out of being first.

**Why the slow maker's curve flattens and then turns up, and why that isn't good news.** From
25µs onward it is already slower than the pick-off, and being slower still does not make the
pick-off worse: the loss per jump is capped at the size it quoted. Past that point the variation
in the column is not about toxicity at all, it is about queue position for ordinary flow — and at
1000µs the maker is quoting around a fair value a full round stale, which is a different problem
(uncompetitive rather than picked off) that this model's once-per-round value updates do not
represent well. The monotone part of the story is 10µs to 50µs. The tail of the sweep is in the
table because leaving it out would be a nicer chart and a worse result.

### 6b. The venue's fees, which are a fifth of the edge

Everything above trades for free. No exchange does. On US equity venues the maker-taker schedule
pays for a resting fill and charges for taking, and at a top tier that is roughly +$0.0020 and
−$0.0030 a share. The makers here quote one tick — one cent — either side of fair value, so the
rebate is **a fifth of the entire edge they are working with**, and the study is not describing
the business until it is charged. `src/fees.py` charges it, at three schedules: no fees, maker-taker,
and an inverted venue (−$0.0010 maker, +$0.0002 taker) of the kind that pays for aggressive flow.

Whole-run P&L in ticks, trading plus fees:

```
 slow maker |                no fees            maker-taker               inverted
            |       fast        slow       fast        slow       fast        slow
       10us |    331,455     338,578    396,536     402,340    295,676     303,213
       25us |    601,827      66,427    671,318     123,113    557,613      38,083
       50us |    606,482      20,579    673,547      70,817    562,852      -4,540
      100us |    600,977      26,171    666,617      77,545    557,988         484
      250us |    581,085      51,281    641,831     106,564    540,334      23,639
     1000us |    483,365     176,982    518,919     251,588    454,015     139,615
```

**The rebate does not change who wins, and it changes whether the loser has a business.** The fast
maker's advantage at 50µs is the same 30x with fees as without — fees are per share, and both
makers do similar volume, so the subsidy lands on both of them. But the slow maker at 50µs earns
20,579 ticks of trading P&L on 251,190 shares quoted, which is **$0.0008 a share**. A maker-taker
venue paying $0.0020 nearly quadruples that. An inverted venue charging $0.0010 takes more than the
strategy makes, and the run goes negative — the single negative cell in the table.

That number has a name here: `breakeven_maker_rate()` solves for the maker rate at which a run nets
to exactly zero.

```
 slow maker |                         fast |                         slow
            |   trading P&L     break-even |   trading P&L     break-even
       10us |       331,455       -0.0089$ |       338,578       -0.0092$
       25us |       601,827       -0.0127$ |        66,427       -0.0023$
       50us |       606,482       -0.0129$ |        20,579       -0.0008$
      100us |       600,977       -0.0130$ |        26,171       -0.0010$
      250us |       581,085       -0.0132$ |        51,281       -0.0019$
     1000us |       483,365       -0.0147$ |       176,982       -0.0047$
```

Negative means it can afford to *pay* the venue that much per share. The fast maker can pay well
over a cent; the 50µs maker can pay eight hundredths of a cent, which is less than an inverted
venue charges. **Latency and venue choice are the same decision.** A slow maker belongs where
quoting is subsidised; the fast one is indifferent, which is why it can afford to quote where the
aggressive flow is.

Two caveats worth stating. Rate cards are tiered by monthly volume and run to several pages —
these are top-line numbers, not a quote. And a real maker facing an inverted venue's fee would
widen its quote rather than keep paying, which this study's fixed one-tick edge does not let it do.

### What this does not model

- **One-way latency, applied once.** No gateway queueing, no serialization delay that grows with
  message size, no matching-engine processing time, and no separate market-data latency — the
  makers here see the jump instantly and only their *outbound* messages are delayed. Real
  co-location is bought mostly to see faster, and that half is missing.
- **The makers are stationary.** No inventory skew, no spread widening when toxicity rises, no
  pulling out of the market entirely. A real slow maker's response to this table would be to
  quote wider, and quoting wider is how it survives — which means the 50µs row, where it keeps 6%
  of its parity P&L, is what happens to a maker that refuses to adapt, not a law.
- **Fair value is exogenous and public.** Both makers see the same jump at the same instant, which
  is deliberate: it isolates latency from information. In a real market the fast participant
  usually has both.

## 7. The auction, where the rules are different

Sections 1-6 all happen in the continuous book: orders arrive one at a time, every trade has its
own price, priority is price then time. The opening and closing crosses run on none of that, and
they are where a large share of real volume trades. `src/auction.py` is the mechanism;
`auction_study.py` is what it costs.

In an auction nothing trades while orders accumulate, then the book is crossed once. Every
execution prints at the **same** price, and that price is chosen by maximising executable volume
rather than by anyone's quote. Volume ties — frequent, because a tick grid makes a whole range of
prices trade the same number of shares — are broken by the smaller imbalance, then by the side of
the imbalance, then by a reference price. Time priority never decides the price; it only decides
who is unlucky at the margin.

### Uniform pricing costs the taker money

Give both mechanisms the same resting offers and send the same buy order into each. Order sizes
are expressed as a share of the liquidity on the offer so the table is about the mechanism rather
than about how many shares this simulator happened to leave lying around.

```
   order  % depth  sweep vwap    bps    cross    bps  buyer pays  breakeven
   6,318      5%    100.0295    2.4   100.03    2.5          $3      27.0x
  12,637     10%    100.0347    3.0   100.04    3.5         $68       2.0x
  31,594     25%    100.0391    3.4   100.05    4.5        $344       4.9x
  63,188     50%    100.0467    4.2   100.06    5.5        $842       2.3x
  94,782     75%    100.0534    4.8   100.07    6.5      $1,576       1.9x
 120,058     95%    100.0596    5.5   100.10    9.5      $4,855       2.4x
```

**The cross is more expensive for the aggressor every single time, and it has to be.** Sweeping
the book pays the *average* of every price walked through; the cross pays the *last* one. The
average of a rising ladder is below its last rung, so the gap is arithmetic, not a market
condition. What the buyer loses is not burned — it goes to the sellers who were resting at the
better prices and now print at the clearing price instead of at their own. A uniform-price
clearing pays inframarginal suppliers the marginal price, which is the textbook reason passive
size shows up for it.

**So why does anyone send size to the close?** Because the comparison above holds liquidity
fixed, and that is exactly what an auction does not do. The `breakeven` column prices the real
trade-off: how many times deeper the auction book has to be before one clearing price beats the
walk. Past about 10% of visible depth the answer is **roughly two times**, and a real closing
cross is far past two — the whole day's participants arrive in the same instant instead of
leaving a few hundred shares on the screen at a time. The 27x in the first row is a tick-grid
artefact and worth reading correctly: at that size the cross is only 0.1bps worse, and it takes
an enormous amount of extra depth to win a whole tick.

The one-line version for an interview: *the auction is not cheaper per share of displayed
liquidity, it is cheaper because it concentrates the liquidity, and it needs roughly a 2x
concentration to break even.*

### What the pre-open feed publishes

```
 market buy  indicative  move bps  imbalance  side  reason
          0           -         -          -   BUY  no overlap
     18,956      100.04      +3.5      8,733  SELL  smallest imbalance
     37,913      100.05      +4.5     11,892  SELL  smallest imbalance
     56,869      100.06      +5.5     16,473  SELL  smallest imbalance
     75,826      100.07      +6.5     22,354  SELL  smallest imbalance
     94,782      100.07      +6.5      3,398  SELL  smallest imbalance
    113,739      100.09      +8.5      4,910  SELL  smallest imbalance
```

The first row is the useful one. With no market order the two limit books do not overlap — there
is a spread, which is what a spread means — and the auction has nothing to cross. **An auction
still needs someone willing to pay the other side's price.** What it changes is what that person
pays, not whether they are required.

Note also that "smallest imbalance" decides almost every row. The maximum-volume rule, which is
the one everybody quotes, is ambiguous most of the time on a real tick grid; the tie-breaks are
not footnotes, they are what actually sets the print.

### What this does not model

- **No auction order types.** Real crosses have market-on-open, limit-on-close, imbalance-only
  and on-close-only orders, each with its own eligibility window and its own priority tier. The
  allocation here is one tier, filled in arrival order.
- **Nobody responds to the imbalance.** Publishing it is the whole point of publishing it — a
  large buy imbalance is a standing invitation, and on a real venue the imbalance usually shrinks
  before the cross. Nothing here reacts, so the indicative path above is the number a responder
  would be reading, not what would happen after they read it.
- **The depth is the zero-intelligence simulator's.** The relative story — the sign of the gap,
  the shape of the breakeven — is the mechanism. The magnitudes in basis points inherit the same
  caveat as everywhere else in this file: ZI agents build a book that is far too deep near the
  mid, so all of these impacts are too small.

## 8. Time priority, priced

The book has enforced price-then-time priority since the first commit and nothing measured what
that priority is worth to the person holding it. `src/queue_position.py` is the experiment,
`queue_study.py` runs it: warm the book with the same zero-intelligence flow as sections 1-5, rest
one 100-share bid with a chosen number of shares in front of it, give it 1,000 events to fill, and
record what happened to that one order. 225 orders across 45 books.

`LimitOrderBook.queue_ahead(order_id)` is the new primitive — live shares resting in front of one
order at its own level, skipping the tombstones the lazy cancel leaves behind. It is O(orders at
the level) and the matching loop does not call it; measurement does.

| shares ahead   |   n | fills | events |  edge | markout | adverse |
|----------------|-----|-------|--------|-------|---------|---------|
| 0-200          |  18 |  100% |     10 | +0.56 |   +0.44 |   -0.11 |
| 200-1,000      |  52 |   94% |     52 | +1.01 |   +0.76 |   -0.26 |
| 1,000-5,000    |  51 |   92% |    204 | +1.13 |   +1.03 |   -0.10 |
| 5,000-20,000   |  57 |   60% |    610 | +1.13 |   +1.71 |   +0.57 |
| 20,000+        |  47 |    0% |  never |     - |       - |       - |

`edge` is the mid minus our price at the instant we filled, in ticks — the half-spread a maker is
nominally paid. `markout` is the same quantity 250 events later, which is what the position was
actually worth. `adverse` is the difference.

Two of the three columns say what anyone would expect. Time to fill rises roughly in proportion to
the queue: 10 events from the front, 610 from 5,000 shares back, and from 20,000 back the order
never traded at all inside the window. Fill rate follows it down.

### The markout came out backwards, and that is the finding

The standard account of why queue position is valuable has three parts, and the third is that
back-of-queue fills are *worse* fills: you only trade when something large comes through, and
something large moves the price against you. In this book that is false. The markout from 5,000 to
20,000 shares back is the best in the table, and the adverse-selection column is **positive** there
— the mid kept moving in the buyer's favour after the fill.

The mechanism is in section 4. This book's mid is sub-diffusive, H below 0.5, because there is no
information in it to make a price move permanent. So the large sell that had to arrive to reach
5,000 shares down the queue pushes the mid down, fills us, and then the mid comes back. Deep-queue
fills are selected for large trades, and in a book of coin-flippers a large trade is a large
*uninformed* trade.

That settles something rather than measuring it. Adverse selection is not a mechanical consequence
of queueing — the mechanics alone produce the opposite sign. It requires the flow to know
something, and the real market's version of this table is two effects pointing opposite ways with
the informed one large enough to flip the sign. That is a considerably stronger claim than "the
back of the queue is worse", and it is the one the numbers support.

### The decision this turns into

A maker facing a long queue at the touch has a choice: join the back of it, or pay a tick and stand
alone at the front of a new level. Improving costs a full tick with certainty and buys first place.
So there is one number to find — how long does the queue have to be before the tick is worth
paying?

| shares ahead | fills |  value |
|--------------|-------|--------|
|          303 |  86% |  +1.14 |
|          803 |  86% |  +1.22 |
|        2,303 |  83% |  +1.23 |
|        8,303 |  59% |  +1.15 |
|       30,303 |   0% |  +0.00 |
| 0 (improved) | 100% |  +0.67 |

`value` is expected ticks per quote: markout plus the maker rebate from `src/fees.py`, weighted by
how much of the order filled. Per *quote* rather than per fill, because an order that never trades
is worth zero and the two choices do not fill equally often — which is the entire subject. Paired
by seed: the improved quote only exists where the spread was wider than one tick, so only the
29 books that left room to improve are counted, or the comparison would be measuring
the spread instead of the decision.

**Break-even queue: about 17,600 shares.** Behind more than that, paying the tick to be
first is the better quote; in front of it, the tick costs more than the priority is worth. For
scale, the touch in this book holds anywhere from tens of shares to eight thousand, so the honest
reading is that queue position is worth *less than a tick* almost all of the time here — and the
reason is the one above. Without informed flow, being late in the queue is not being adversely
selected, it is just being late.

### What this does not model

- ~~**The queue is held still.**~~ Section 9 lets it melt. The shares in front were protected
  from the random cancel stream (`protected_ids` in `simulate()`), and the claim was that this
  made every fill probability a lower bound. It did, and the bound was tight: letting the queue
  melt moves the break-even by about 1,100 shares and the deep fill rate by two points at 1,000
  events, because in this flow a share in front of you is twenty-four times more likely to be
  traded than cancelled.
- **The padding is liquidity that was not there.** Building a long queue means adding depth at the
  touch, which feeds back into the flow. The natural queue length is reported alongside the padding
  so the two are never confused.
- **One order, no inventory, no requoting.** A real maker facing a 610-event wait would cancel and
  re-post. This measures what happens to an order left alone, which is the cleaner experiment and
  not the strategy.

## 9. Let the queue melt

Section 8 held the queue still. The padding in front of the order under study was one protected
order the cancel stream could not touch, and the write-up called every fill probability a lower
bound for that reason, adding that the people in front of you cancelling is "a large part of why
front-of-queue is worth having". That was a claim about real markets, made from a book that had not
been asked. `melt_study.py` asks it: the same 225 orders in the same 45 books, with the padding
rested as 100-share orders the cancel stream can pick off like anyone else's.

The design is paired. Every (pad, seed) runs twice on the same warm book, and the event generator
draws the same random numbers either way — same arrivals, same market orders, same cancels-as-
events — so the only thing that differs is which resting order a cancel lands on. Whatever
separates the two outcomes is the melting and nothing else. The held side of every table below
reproduces section 8 to the digit, which is the check that the pairing works.

Four predictions were written before the run and `melt_verdicts()` scores them. One failed, and
it is the one that says what the queue is actually made of.

| shares ahead  |  n | held: fills | events | markout | melting: fills | events | markout | left by trade | by cancel |
|---------------|----|-------------|--------|---------|----------------|--------|---------|---------------|-----------|
| 0-200         | 18 |        100% |     10 |   +0.44 |           100% |     10 |   +0.44 |          100% |      0.0% |
| 200-1,000     | 52 |         94% |     52 |   +0.76 |            94% |     52 |   +0.76 |         96.7% |      0.5% |
| 1,000-5,000   | 51 |         92% |    204 |   +1.03 |            92% |    205 |   +1.05 |         93.8% |      1.3% |
| 5,000-20,000  | 57 |         60% |    610 |   +1.71 |            61% |    612 |   +1.87 |         77.2% |      3.2% |
| 20,000+       | 47 |          0% |  never |       - |             0% |  never |       - |         33.4% |      4.9% |

1,000 events to fill, as in section 8. The last two columns are new: of the shares that were in
front of us when we joined, the share that left the queue by trading and the share that left by
being cancelled, counted up to our own fill. They do not sum to 100% because the rest was still
there when the clock ran out.

### The queue does not melt. It gets eaten.

The prediction was arithmetic on the cancel stream: 15% of events are cancels, each lands on one
of ~2,300 live orders, so a queue of any length should lose about 0.15 × H / 2,300 of itself in H
events — 6-7% at 1,000 events and a quarter at 4,000. Measured: 3.9% at 1,000 and 6.4% at 4,000,
in the buckets deep enough for melting to matter. Two things the arithmetic left out, both
visible in the table.

The first is the race. From 5,000 shares back, 77% of the queue in front of us was *traded*
inside the 1,000 events and 3% was cancelled: twenty-four shares eaten for every one that melted.
The clock stops at our fill, and the fill comes at 610 events on average, so the cancel stream
only had 610 events to work on a queue that trading was clearing at ten shares an event.

The second is that the cancel stream here does not know where the touch is. It picks an order
uniformly from everything live — twenty levels a side, plus the tail of resting orders the flow
has accumulated, and the pool grows by about one order every two events as new limit orders
rest. A hundred orders at the best bid are 4% of that pool and get 4% of the cancels. In a real
book the opposite holds: most cancels *are* at the touch, because, as the project page puts it,
most cancels in real markets are not changes of mind but repositioning. That is
the mechanism section 8's sentence was about, and this flow does not have it. The zero-
intelligence cancel is the one kind of cancel that is not a decision about the queue.

### What the caveat was worth

| shares ahead | held: fills | value | melting: fills | value |
|--------------|-------------|-------|----------------|-------|
|          303 |        86% | +1.14 |           86% | +1.14 |
|          803 |        86% | +1.22 |           86% | +1.29 |
|        2,303 |        83% | +1.23 |           83% | +1.27 |
|        8,303 |        59% | +1.15 |           62% | +1.26 |
|       30,303 |         0% | +0.00 |            0% | +0.00 |
| 0 (improved) |       100% | +0.67 |          100% | +0.67 |

Section 8's decision table, same 29 books where the spread left room to improve, with the joined
queue allowed to melt. The improved quote has nobody in front of it and so nothing to melt.

**Break-even queue: 17,600 shares held, 18,700 melting.** Joining the back of a queue that shrinks
on its own is worth a little more, so the tick is worth paying a little later — by about 1,100
shares, which is 6% of the number and inside the grid's resolution. At 4,000 events, where an
order from 20,000 shares back does eventually trade, melting takes that bucket's fill rate from
57% to 68% and its wait from 2,970 events to 2,880: eleven points, where the order had to wait
longest and the cancel stream had the longest run at the queue. Everywhere else the two columns
are within two points of each other.

The markout column moved in the direction section 8 would predict and for section 8's reason. The
melting fills from the deep queue mark out at +1.87 against +1.71 held, with the adverse-selection
column at +0.80 against +0.57 — still positive, still the wrong sign for the textbook, and a shade
more so. Melting changes *when* the order fills, not what the mid does afterwards; the fills that
came a few events earlier caught a little more of the mid coming back. The mean-reverting mid is
the mechanism, and the cancel stream never touched it.

So the sentence in section 8 was right about real markets and wrong about this one. Fill
probabilities here were a lower bound, and the bound was tight to within two points, because the
thing that makes real queues melt — cancels concentrated at the touch — is precisely what a
uniform cancel stream cannot produce. The number to carry is the ratio, not the melt: in this
flow a share ahead of you is twenty-four times more likely to be traded than pulled. A real book
at a busy touch would show that ratio inverted, and that inversion is where "a large part of why
front-of-queue is worth having" actually lives.

### What this does not model

- **Cancels are uniform over the book.** The finding above is that this is the whole story. A
  cancel rate that rises toward the touch — or a flow whose cancels are re-postings a tick away —
  is the next experiment, and it needs a cancel model, not a protected-ids flag.
- **The melting padding is still padding.** It is liquidity that was not there, now in 100-share
  lots instead of one, and `natural_ahead` is reported beside it for the same reason as before.
- **Fills are still counted to the fill.** The traded/cancelled split stops at our own fill, which
  is the right window for the question asked (what happened in front of us while we waited) and
  the wrong one for "how fast does a queue melt in general". The 20,000+ row at 1,000 events, which
  never fills, is the clean reading of the raw rate: 4.9% by cancel against 33% by trade.

## What isn't modeled

- One symbol, one venue. No routing, no NBBO, no Reg NMS.
- Limit, market, IOC and FOK orders — no stops, icebergs, pegged, or auction orders.
- ~~No opening/closing auction~~ — `src/auction.py` crosses a book at a single price under the
  maximum-volume rule and its tie-break ladder, and section 7 measures what that costs an
  aggressive order against sweeping the same liquidity. It is a separate mechanism, not wired
  into the continuous simulator: sections 1-6 still run a book that never opens or closes.
- ~~No latency~~ — `src/latency.py` puts messages on the wire and the book serves them in
  arrival order; section 6 measures what a 15µs disadvantage does to a market maker. The
  simulator in sections 1-5 still runs with no latency at all, so every number in those sections
  describes a market where everyone is infinitely fast.
- ~~No fees or rebates~~ — `src/fees.py` charges maker-taker and inverted schedules, and
  section 6b shows the rebate is a fifth of the quoted edge and decides whether the slow maker has
  a business at all. Sections 1-5 still trade for free. Self-trade prevention exists now (`participant_id` + `StpPolicy` on every
  order type - see README's Design notes and `tests/test_stp.py`), but no other risk checks
  (position limits, fat-finger checks). The zero-intelligence simulator in sections 1-5 still doesn't
  assign participant identities to its agents, so none of the numbers above exercise it.
- ~~No queue position study~~ — `src/queue_position.py` rests one order at a chosen place in the
  queue and follows it, and section 8 prices time priority at about a tick per 17,600 shares
  ahead. It also finds the adverse-selection sign REVERSED against the textbook account, for a
  reason that is a property of this null model rather than of queueing. Section 9 lets the queue
  in front melt and finds it barely does — 18,700 shares — because a uniform cancel stream eats
  the touch twenty-four times slower than trading does.
- Agents have no memory, no inventory, and no information — which is exactly what makes it a
  valid null model, exactly what makes the depth magnitudes wrong, and, per section 8, exactly
  why adverse selection comes out with the wrong sign here.
