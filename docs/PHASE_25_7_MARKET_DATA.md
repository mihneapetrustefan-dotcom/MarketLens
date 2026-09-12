# Phase 25.7 — Operational Market Data Layer

**Status** COMPLETE in code · **Real IBKR validation** UNVERIFIED
**Written** 2026-09-12 · **Branch** `ibkr-paper-validation-fixes`

---

## A. Executive summary

Before this phase the project could reconstruct market history
perfectly and could not answer *"what is this worth right now"*. The
only price source was `price_candle_cache`, which
`cache_price_candles.py` fills with windows around canonical events so
event studies stay reproducible. Nothing fetched a current price, and
`MarketDataStatus.is_cached` in `paper_models.py` carried the comment
*"Every price in this system currently does"*.

Phase 25.7 builds the missing sensory layer: a bounded polling service
that acquires current IBKR quotes, judges their freshness, stores them
separately from the research corpus, builds one-minute bars, and
exposes a price boundary that portfolio and risk can consume without
ever mistaking a cached close for a live market.

It decides no trades, places no orders, and enables no scheduler.

---

## B. Before / after

| | Before | After |
|---|---|---|
| Current price | did not exist | `market_data_state`, one row per instrument |
| Price age | unknowable | three timestamps, re-judged per read |
| Live vs delayed | not distinguished | `MarketDataAvailability`, delayed never FRESH |
| Intraday bars | none | `market_data_bars`, gaps explicit |
| Trading price source | research cache | operational only, no fallback path exists |
| Session awareness | cached-bar inference | calendar first, venue second |
| Budget accounting | none | computed before any request |

---

## C. Architecture

```
MARKET SESSION  (MarketSessionView -> gateway.market_status)
      |
IBKR SNAPSHOT   (one batched request for the whole universe)
      |
QUOTE NORMALISE (quotes.acquire -> OperationalQuote)
      |
FRESHNESS       (OperationalQuote.freshness, ONE definition)
      |
CURRENT STATE   (market_data_state, upserted)
      |
1-MINUTE BARS   (MinuteBarBuilder -> market_data_bars)
      |
PRICE BOUNDARY  (prices.operational_price / research_price)
      |
PORTFOLIO / RISK
```

### Files

| File | Responsibility |
|---|---|
| `src/domain/market_data_models.py` | operational vocabulary and rules |
| `src/data_access/market_data_schema.py` | three tables, retention |
| `src/marketdata/universe.py` | deterministic pollable universe |
| `src/marketdata/quotes.py` | batched acquisition, cold contracts |
| `src/marketdata/bars.py` | one-minute bars, gaps, ordering |
| `src/marketdata/repository.py` | persistence |
| `src/marketdata/service.py` | one cycle, health, capacity |
| `src/marketdata/prices.py` | **the price boundary** |
| `scripts/run_market_data.py` | operator CLI, observe-only |

---

## D. Why polling, not a websocket

Checked against the repository, not assumed:

- **No persistent runtime exists.** Every entry point is a batch job
  under GitHub Actions cron. `docs/API_AUDIT.md` records the
  deliberate absence of any server, worker or queue. A websocket needs
  a process to hold it.
- **The endpoint is already batched.** `transport.market_snapshot`
  takes a *sequence* of conids, so the whole active universe costs
  **one request per cycle**.
- **No strategy reads below 5 minutes.**

Measured capacity at the 60-second default:

| | |
|---|---|
| requests per cycle | 1 |
| requests per minute | 1 |
| budget | 50 |
| headroom | 49 |

A websocket would add a daemon, a reconnect state machine and an
ordering problem to buy resolution nothing consumes. `service.py` is
the seam if a sub-minute strategy ever appears.

---

## E. Freshness — one definition

`OperationalQuote.freshness()` is the **only** place freshness is
decided. `prices.operational_price` rebuilds the stored row into an
`OperationalQuote` and asks it, rather than reimplementing the rules.

An earlier draft did reimplement them and immediately disagreed with
itself: the same quote counted as `unavailable` in the acquisition
cycle and `fresh` in the status view. That is exactly the defect §7
forbids, and it is why there is now one path.

Vocabulary is Phase 13's, reused rather than reinvented:
`DataFreshness`, `FreshnessPolicy`, `HealthState`.

| Condition | Result |
|---|---|
| live, within 120s | FRESH |
| live, within 300s | AGING |
| live, within 900s | STALE |
| beyond 900s | INVALID |
| **delayed, any age** | never better than AGING, never tradeable |
| unknown / restricted availability | UNAVAILABLE |
| no timestamp | UNAVAILABLE |
| negative age | INVALID |
| no price | INVALID |

`is_tradeable` requires **both** live availability and tradeable
freshness. Freshness alone is insufficient, because a delayed quote is
capped at AGING and AGING is otherwise tradeable.

---

## F. The separation this phase enforces

```
price_candle_cache    "what happened, reproducibly"    RESEARCH
market_data_state     "what is happening, right now"   OPERATIONAL
```

The forbidden sequence, named in the spec:

> IBKR unavailable → use a five-day-old research close → present it as
> current → trade on it

**Prevented structurally, not by discipline.** `operational_price`
reads `market_data_state` and nothing else. It has no access to the
research cache, so there is no fallback for it to take. When there is
no live state it returns an explicit absence.

`research_price` exists so research consumers have a supported way to
ask, and it always returns `PriceSource.RESEARCH` with the age
attached. It can never satisfy `is_tradeable` — not because of its
age, but because of its provenance.

---

## G. Bars

A bar here is a **summary of what we observed**, not a vendor fact. A
poller sampling once a minute sees a few prices per minute, not every
trade, so `observation_count` is recorded on every bar: a bar built
from one sample is one price wearing OHLC clothing, and a consumer
deserves to be able to tell. This is also why they never go near the
research cache.

- **Complete only when the minute is over**, decided by the clock and
  never by the caller running out of quotes.
- **Gaps are recorded, never interpolated.** An invented bar is
  indistinguishable from a real one once stored.
- **Older never overwrites newer.** A quote for a sealed minute is
  refused with a reason; an out-of-order quote inside the open minute
  does not set the close.
- The final partial minute at session close is emitted labelled
  `is_complete=False`.

---

## H. Storage and retention

| Table | Shape | Retention |
|---|---|---|
| `market_data_state` | one row per instrument, upserted | never pruned, no history by design |
| `market_data_bars` | completed minutes + gap rows | 30 days |
| `market_data_cycles` | one row per acquisition | 7 days |

**No tick table.** The production database is already ~276 MB as a
release asset. An unbounded tick store would dwarf the research corpus
within days and buy nothing at current strategy horizons. If ticks are
ever genuinely needed that is a decision to take deliberately, not a
side effect of this phase.

---

## I. Failure behaviour

| Condition | Result |
|---|---|
| transport failure | every instrument UNAVAILABLE with the reason, cycle DEGRADED/FAILED |
| cold contract | UNAVAILABLE, retry next cycle, never filled from history |
| instrument absent from response | present-and-unavailable, never silently missing |
| market closed | cycle PAUSED, nothing acquired, old state left with its own timestamps |
| universe exceeds budget | cycle FAILED and refuses; the broker limit is never raised |
| cycle overruns its interval | detected, recorded, health degraded |
| no resolved contract | instrument blocked by name, never substituted |

---

## J. Known limitations

1. **Real IBKR validation is UNVERIFIED.** The gateway was down and
   the market closed (Saturday) when this was written. The underlying
   `market_snapshot` path *was* verified live on 2026-09-11 through
   `gateway.quote` — real prices for AAPL, MSFT and NVDA — but the
   `MarketDataService` itself has never run against the venue.
2. **Session granularity is coarse.** `MarketSessionView` reports
   OPEN / CLOSED / UNKNOWN only. Pre-market, after-hours, holidays and
   early closes are declared in `MarketStatus` but never guessed,
   because no venue calendar exists to support them. A real session
   engine is Phase 25.8 work.
3. **Session id is one UTC day.** Adequate for a single-venue
   universe; insufficient once instruments span venues that open at
   different moments.
4. **Bar builder state is in memory.** A restart loses at most the
   minute in progress. Persisting partial minutes was rejected
   deliberately.
5. **Cold contracts cost one cycle.** Measured: retrying inside one
   invocation does not hurry the subscription.
6. **No scheduler.** Deliberate — §27.

---

## K. Phase 25.8 prerequisites

Satisfied by this phase:

- current price state exists and is trustworthy
- freshness is explicit and single-sourced
- `tradeable_prices()` gives the loop exactly the instruments it may act on
- health reports whether state is usable
- budget accounting exists

Still required by 25.8:

- a real session engine (pre-market, holidays, early closes)
- a scheduled invocation of the market-data service
- wiring `tradeable_prices` into `PortfolioService` valuation
- wiring freshness into the risk stale-price guards
