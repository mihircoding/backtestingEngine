# Interview notes — event-driven backtesting

What to be able to say about this project, and what interviewers actually push on. Quant *developer*
interviews lean on this material more than any other project in the set, because building and
maintaining exactly this infrastructure is usually the job.

---

## The 60-second version

> I built an event-driven backtester — the same architecture as a production trading system, in
> miniature. Five components communicate only through a queue of immutable events: market data,
> signal, order, fill. The data handler physically cannot hand out unreleased bars, so lookahead
> bias is prevented structurally rather than by convention. I ran a 50/200 moving-average
> crossover on SPY over 2015–2024: it returned 67% against buy-and-hold's 82%, with a deeper
> drawdown. Transaction costs were $102 — the entire shortfall came from four whipsaw round trips,
> and 78% of it from one, selling on March 31 2020 and buying back in July.

That last sentence is the one that lands. Anyone can report a backtest number; attributing the
shortfall to specific decisions shows you actually looked.

---

## Core concepts

### Vectorized vs event-driven

**Know the tradeoff, not just the definitions.**

| | Vectorized | Event-driven |
|---|---|---|
| Speed | Fast (whole-array ops) | Slow (Python loop per bar) |
| Lookahead risk | High — the future is in memory | Structurally prevented |
| Order realism | Poor — no partial fills, limits, latency | Natural |
| Path to live | Rewrite | Swap two components |
| Best for | Research sweeps, parameter scans | Validation, pre-production |

Real workflows use both: vectorized to explore the space cheaply, event-driven to validate the
handful of candidates that survive. Saying "event-driven is better" is the wrong answer; saying
"they answer different questions" is the right one.

### Why a queue and not direct method calls?

Three reasons, in order of how much interviewers care:

1. **It matches live trading.** In production, fills arrive asynchronously from the exchange,
   milliseconds or minutes after the order. A queue models that; a direct call
   `fill = execution.execute(order)` bakes in the assumption that execution is instantaneous
   and synchronous. The same strategy class can run live by swapping the data handler and
   execution handler.
2. **Decoupling.** Components know event *types*, not each other. You can add a risk-check
   handler that intercepts OrderEvents without touching the strategy.
3. **Fan-out.** One signal can produce several orders (a pairs trade produces two legs); one
   order can produce several fills. Return values don't model that cleanly; a queue does.

### Why frozen dataclasses for events?

Messages shouldn't mutate after they're sent. If a downstream handler could rewrite a FillEvent,
your audit log and your accounting would disagree, and you would never find out which one is
lying. Freezing also makes them hashable and cheap to log.

### The ordering bug

**Q: Where do you mark equity, and why does it matter?**

After the inner event loop drains, not before. If you mark first, the bar's fills aren't in that
bar's equity, and your entire curve is shifted by one bar. It looks completely plausible — the
shape is right, the final value is right, every intermediate value is wrong. This is the bug the
end-to-end test exists to catch.

Generalize the point: **in an event-driven system, ordering bugs don't crash, they bias.**

### How lookahead is prevented

The data handler holds a cursor and exposes `get_latest(symbol, n)`, which slices only up to the
cursor. There is no method that returns unreleased data.

The framing that scores: *"the strategy isn't trusted not to look at the future — it isn't
offered the future."* A convention that everyone must remember is not a control. A type that
makes the mistake unrepresentable is.

Corollary worth mentioning unprompted: during warmup `get_latest` returns fewer rows than
requested, so strategies must handle short series. That's the correct behavior — padding with
zeros or dropping the check would fabricate history.

### Signal on the crossing, not the state

If the strategy emits LONG on every bar where short MA > long MA, the portfolio sees hundreds of
redundant signals. The portfolio's `on_signal` returns `None` when the target equals the current
position, so nothing catastrophic happens — but the *strategy* is the right place to hold that
state, because it's the strategy's notion of "I've already acted on this."

Follow-up they may ask: *"You have two places tracking position — the strategy's `_in_position`
set and the portfolio's `positions` dict. Isn't that duplicated state?"* Yes, and it's
deliberate: they mean different things. The strategy tracks *intent*; the portfolio tracks
*reality*. They diverge the moment an order is rejected, partially filled, or risk-limited —
and modeling that divergence is the whole reason the two layers are separate.

---

## Execution and cost modeling

### Defend the fill model

Filling at the close of the bar you signalled on is **optimistic**: you observed the close, then
traded at it. Nobody gets that. The honest alternatives:

- **Next bar's open** — the standard conservative choice for daily strategies.
- **VWAP over the next bar** — realistic for larger orders worked over time.
- **Next tick / next quote** — the only real answer, and it needs tick data.

Know that the choice matters more the higher the frequency. For a strategy trading 4 times a
decade it's noise; for one trading intraday it's the entire P&L.

### Slippage

Modeled as a fixed number of basis points, always against you. The asymmetry is the point — a
model where slippage is symmetric noise averages to zero and is therefore no model at all.

The real relationship is roughly **square-root impact**: cost scales like
`σ · sqrt(order size / average daily volume)`. Saying that out loud demonstrates you know the
bps model is a placeholder.

### Commission on fills, not orders

Trivial here because fills are always complete. It matters the moment you add partial fills: an
order for 1,000 shares that fills 400 then 600 should be charged on 400 and 600, not on 1,000
at submission. Worth flagging as a "this is what I'd change first" answer.

---

## Questions I'd expect

**"Walk me through what happens on one bar."**
DataHandler advances the cursor and pushes a MarketEvent. The engine pops it, hands it to the
strategy, which pulls up to `long_window` closes and may return SignalEvents. Each signal is
popped and handed to the portfolio, which compares target position to current position and emits
an OrderEvent for the difference — or `None` if it's already there. Each order is popped and
handed to the execution handler, which applies slippage and commission and returns a FillEvent.
Each fill is popped and applied to cash and positions. When the queue is empty, the portfolio
marks to market and appends one point to the equity curve.

**"Your Sharpe went from 4.10 to 0.75. What happened?"**
Different data, and the first dataset was built to flatter the strategy. The synthetic series is
a sine wave — cyclical by construction, which is exactly what a trend follower wants. It's a
correctness harness for the engine, not evidence about the strategy. The real number is 0.75,
against 0.77 for doing nothing.

**"Was it transaction costs?"**
No — $102 total against $14,371 of underperformance. It was four decisions, and one of them
(sell 2020-03-31 at 236.28, buy back 2020-07-06 at 292.03) cost $11,150 on its own. A 50/200
crossover is a lagging indicator; against a V-shaped recovery it sells the bottom and buys the
top by construction.

**"How would you know if your backtester itself is wrong?"**
Test against a case with a closed-form answer. Buy-and-hold with zero frictions must end at
`cash + shares × (last − first)`. If that's off by a cent, something double-charged,
mis-dispatched, or marked at the wrong moment. Beyond that: run a strategy that should lose
exactly the spread and check that it does; run with zero signals and check equity is flat.

**"What's the difference between this and Backtrader/Zipline?"**
Same architecture, vastly more surface: multiple data feeds and resolutions, live broker
adapters, order types, analyzers, corporate actions calendars. Nothing conceptually different in
the core loop — which is why building the small one first makes their source readable.

**"Floats or Decimals?"**
Floats here, with tolerance-based tests. Production accounting uses integer minor units (cents,
or exchange ticks) because `0.1 + 0.2 != 0.3` and a fraction of a cent that repeats across a
million fills becomes a reconciliation break. Know why, know that it's a real class of
production bug, and know that switching is mostly mechanical.

**"How do you handle survivorship bias here?"**
This project doesn't — it trades one liquid ETF. The general answer: use a point-in-time universe
that includes delisted names with their delisting prices. Backtesting today's S&P 500 constituents
over the last decade means you selected the survivors in advance, which reliably inflates returns
by a few percent a year.

**"Multiple symbols — what breaks?"**
The loop already iterates `data.symbols`. What breaks is everything sizing-related: fixed share
counts mean a $600 stock and a $30 stock get wildly different dollar exposure. You'd move to
dollar-notional or volatility-scaled sizing. Also: symbols with different trading calendars need
their bars aligned, and forward-filling a halted name creates fake fills.

---

## Things to admit before they ask

Volunteering the limitations reads as competence, not weakness. Listing them first also means you
control which one gets discussed.

- Same-bar-close fills are optimistic. Next-bar-open is the honest default.
- Fills are unlimited size. Real books have depth.
- Parameters (50/200) are conventional, not fitted — and fitting them on the same sample I
  reported on would have produced a better number and a worse answer.
- One asset, one decade, one regime pair. Not enough to conclude anything about the strategy
  class.
- No borrow costs, so the short side is priced too generously.

---

## Related

- The **pairs trading** project uses the vectorized approach — good contrast to discuss.
- The **limit order book** project is where the execution handler's fill assumptions get replaced
  with an actual matching engine.
