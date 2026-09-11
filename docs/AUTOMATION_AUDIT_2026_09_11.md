# FULL AUTOMATED TRADING STATUS AUDIT

**Repository** MarketLens · branch `ibkr-paper-validation-fixes` at `e9a2a3d`
**Date** 2026-09-11
**Method** Every claim below was checked against the repository, the
workflow files, or the production database snapshot
(`db-latest`, 276,164,608 bytes, updated 2026-09-10T23:15Z).
Prior reports were treated as claims to verify, not as evidence.

Where something was verified against the real Interactive Brokers
paper account today it says **VERIFIED LIVE**. Where it was only ever
exercised against `MockIBKRTransport` it says **MOCK ONLY**. Those two
are never merged.

---

## 1. Executive Verdict

> **Today the system can automatically ingest news three times a day and
> regenerate its entire research-to-signal chain twice a week, but it
> stops before any order is ever created — because no trading loop is
> scheduled, no price newer than the last pipeline run exists anywhere
> in the system, and no model has ever qualified to trade.**

To reach genuine autonomous paper trading from market open to market
close, the sequence is:

> **A. operational market-data layer → B. a loop that actually runs on a
> schedule → C. intraday signal refresh → D. real IBKR paper order
> validation → E. shadow → F. controlled live.**

**Phase 26 (Shadow) is NOT the correct next phase.** Shadow trading
compares decisions against market execution. This system produces no
decisions during market hours and has no current prices to compare
against. Shadow would measure nothing. Two remediation phases must come
first. See §25.

### A–P, answered directly

| | Question | Answer |
|---|---|---|
| A | What has been built for automated trading? | A complete, well-separated code path from news to attribution, and a full IBKR adapter. 3,877 tests pass. |
| B | What is genuinely automated? | News ingestion (3×/day) and the research→signal pipeline (2×/week). Nothing else. |
| C | What is only code-level? | Portfolio, risk, execution, trading loop, memory-of-trades, challengers, paper validation. Zero production rows. |
| D | What is only mock-tested? | Order submission, fills, positions from orders, trade outcomes, duplicate-order protection. |
| E | What is verified against real IBKR? | Session auth, account state, balances, contract resolution, live quotes, reconciliation (clean), restart recovery, heartbeat. **No order has ever been sent.** |
| F | What stops autonomous trading today? | Three independent things, all sufficient alone: no scheduled loop, no current prices, no qualifying model. |
| G | Can it receive continuous prices open-to-close? | **No.** There is no streaming, no polling loop, no intraday price path at all. |
| H | What architecture should provide it? | A bounded IBKR snapshot-polling market-data service with a persisted latest-price state and 1-minute bars. See §6. |
| I | What happens to prices after ingestion? | Normalise → in-memory latest state → persisted 1m bars → derived features. Not every tick. |
| J | How should prices drive the chain? | Event-driven evaluation on bar close, not on every tick; bounded scheduled reconciliation alongside. |
| K | What manual actions remain? | Gateway start, browser login, workflow dispatch, ordering-gate flag, decision selection. |
| L | Which are legitimate governance? | Browser login, model promotion approval, live promotion, ordering-gate flag. |
| M | Which are accidental gaps? | Gateway lifecycle, workflow dispatch, loop scheduling, price refresh. |
| N | Five biggest blockers? | §23. |
| O | Correct order from here? | §24. |
| P | Is Phase 26 correct next? | **No.** Insert Phase 25.7 (market data) and 25.8 (scheduled loop) first. |

---

## 2. Current System State

**FACT** The repository is healthy and the test suite is green.
**EVIDENCE** `PYTHONPATH=src python -m unittest discover -s tests -t . -b`
→ `Ran 3877 tests ... OK (skipped=1)`. `scripts/audit_live_safety.py
--untracked` → 16/16 PASS.
**IMPACT** Code quality is not the constraint.
**STATUS** ✅ VERIFIED

**FACT** Five real defects were found and fixed in this session, all
invisible to a passing suite because they only appear against a real
venue or real data.
**EVIDENCE** Commits `98e2de3`, `d3cec67`, `ecfc92c`, `7cd520f`,
`fd96894`, `e9a2a3d`.

| Defect | Why the suite missed it |
|---|---|
| Bodyless POST on auth/tickle → IBKR 400 | mock never inspected request bodies |
| `requests` never installed | mock transport needs no HTTP |
| Retrains counted as corroboration | no fixture had two models |
| Probability scored against zero threshold | no logistic model had ever been trained |
| Approved-but-empty decision authorised any instrument | the code path had no test |

**IMPACT** A 3,877-test suite is not evidence of venue readiness.
**STATUS** ✅ FIXED, pushed

---

## 3. Actual Automation Pipeline

Classification per arrow, from the workflow files and the code.

| Stage → Stage | Exists | Called | Automated | Cadence | Data | Broker |
|---|---|---|---|---|---|---|
| NEWS → ingestion | ✅ | ✅ | ✅ scheduled | 3×/day (13:00, 16:30, 21:15 UTC) | production | — |
| MARKET DATA → cache | ✅ | ✅ | ✅ scheduled | **2×/week, event windows only** | production | — |
| MARKET DATA → *current price* | ❌ | ❌ | ❌ | **never** | — | — |
| cache → FEATURE UPDATE | ✅ | ✅ | ✅ scheduled | 2×/week | production | — |
| FEATURE → MODEL INFERENCE | ✅ | ✅ | ✅ scheduled | 2×/week | production | — |
| INFERENCE → SIGNAL | ✅ | ✅ | ✅ scheduled | 2×/week | production | — |
| SIGNAL → ELIGIBILITY | ✅ | ❌ | ❌ | **never run** | none | — |
| ELIGIBILITY → PORTFOLIO | ✅ | ❌ | ❌ | never | none | — |
| PORTFOLIO → RISK | ✅ | ❌ | ❌ | never | none | — |
| RISK → ORDER INTENT | ✅ | ❌ | ❌ | never | none | — |
| INTENT → EXECUTION | ✅ | ❌ | ❌ | never | none | — |
| EXECUTION → IBKR | ✅ | ❌ | ❌ | never | none | **never sent** |
| IBKR → ORDER STATUS | ✅ | ⚠️ | ❌ | manual | — | MOCK ONLY |
| STATUS → FILL | ✅ | ⚠️ | ❌ | manual | — | MOCK ONLY |
| FILL → POSITION | ✅ | ⚠️ | ❌ | manual | — | MOCK ONLY |
| POSITION → RECONCILIATION | ✅ | ✅ | ❌ manual | on demand | — | **VERIFIED LIVE** (6 checks, clean, 0 orders) |
| RECONCILIATION → P&L | ✅ | ❌ | ❌ | never | none | — |
| P&L → OUTCOME (trade) | ✅ | ❌ | ❌ | never | none | — |
| OUTCOME (signal) → ATTRIBUTION | ✅ | ✅ | ✅ scheduled | 2×/week | production (13,405 rows) | — |
| ATTRIBUTION → MEMORY | ✅ | ✅ | ✅ scheduled | 2×/week | production (11,641 rows) | — |

**The chain is automated from NEWS to ATTRIBUTION/MEMORY, and entirely
dormant from SIGNAL ELIGIBILITY onward.**

---

## 4. Exact Current Stopping Point

> ### SIGNAL → **STOP**
>
> The pipeline writes a signal to the `signals` table and ends. Nothing
> in any scheduled job ever reads it for trading.

**FACT** The trading loop has no active schedule.
**EVIDENCE** `.github/workflows/run_trading_loop.yml` lines 79–80:

```yaml
  # schedule:
  #   - cron: '*/15 13-20 * * 1-5'   # in timpul sesiunii US, zilele lucratoare
```

Both lines are commented out. The same is true of
`run_paper_session.yml` lines 87–88. Confirmed by counting uncommented
`cron:` keys across all 27 workflows: only `pipeline.yml` (2) and
`daily.yml` / `archive_articles.yml` (3 each) are active.
**IMPACT** The only component that reads signals for trading runs solely
on manual `workflow_dispatch`.
**STATUS** ❌ BLOCKING — this is the precise automation boundary.

### Concrete code path

`scripts/generate_signals.py` (pipeline stage 10) writes rows via
`SignalRepository.save`. The next consumer is
`src/trading/loop.py::TradingLoop.run_cycle`, reached only from
`scripts/run_trading_loop.py`, whose workflow has no cron. The
production database proves it has never been reached: `signal_eligibility`
— the table that receives **one row per signal the loop sees, always** —
does not exist.

**Even if it were scheduled, two further stops follow immediately:**

1. **No current price.** `run_trading_loop.yml` line 129 hard-codes
   `--mock`, and prices come from `price_candle_cache` whose newest bar
   is 2026-09-05 against a run date of 2026-09-11.
2. **No qualifying signal.** Every signal ever produced carries
   confidence 0.30 or 0.15 against a hard floor of 0.40.

---

## 5. Automation Maturity Matrix

"Automated" means *runs without a human on a schedule*. "Verified"
means *demonstrated against real data or the real venue*.

| Component | Exists | Automated | Prod-connected | Real data | Verified | Blocking |
|---|---|---|---|---|---|---|
| News ingestion | ✅ | ✅ 3×/day | ✅ | ✅ 49,686 articles | ✅ | — |
| Historical market data | ✅ | ✅ 2×/week | ✅ | ✅ 129k candles | ✅ | — |
| **Live/intraday market data** | ❌ | ❌ | ❌ | ❌ | ❌ | **CRITICAL** |
| Feature refresh | ✅ | ✅ 2×/week | ✅ | ✅ 34,724 | ✅ | HIGH (cadence) |
| Model inference | ✅ | ✅ 2×/week | ✅ | ✅ 1,316 | ✅ | — |
| Signal generation | ✅ | ✅ 2×/week | ✅ | ✅ 423 | ✅ | HIGH (cadence) |
| **Signal freshness** | ⚠️ | ❌ | ✅ | ✅ | ✅ measured | **CRITICAL** |
| Signal eligibility | ✅ | ❌ | ❌ table absent | ❌ | MOCK ONLY | HIGH |
| Portfolio evaluation | ✅ | ❌ | ❌ tables absent | ❌ | MOCK ONLY | HIGH |
| Risk | ✅ | ❌ | ❌ tables absent | ❌ | MOCK + 1 live probe | HIGH |
| Execution | ✅ | ❌ | ❌ tables absent | ❌ | MOCK ONLY | HIGH |
| IBKR session | ✅ | ❌ manual | ✅ | ✅ | **VERIFIED LIVE** | MEDIUM |
| Account state | ✅ | ❌ manual | ✅ | ✅ | **VERIFIED LIVE** | — |
| Positions | ✅ | ❌ | ✅ | ✅ (empty) | **VERIFIED LIVE** | — |
| Order status | ✅ | ❌ | ❌ | ❌ | MOCK ONLY | HIGH |
| Fills | ✅ | ❌ | ❌ | ❌ | MOCK ONLY | HIGH |
| Reconciliation | ✅ | ❌ manual | ✅ | ✅ | **VERIFIED LIVE** (clean, 0 orders) | — |
| P&L | ✅ | ❌ | ❌ | ❌ | MOCK ONLY | MEDIUM |
| Outcomes (signal) | ✅ | ✅ 2×/week | ✅ | ✅ 11,641 | ✅ | — |
| Outcomes (trade) | ✅ | ❌ | ❌ | ❌ | MOCK ONLY | MEDIUM |
| Error attribution | ✅ | ✅ 2×/week | ✅ | ✅ 13,405 | ✅ | — |
| Memory | ✅ | ✅ 2×/week | ✅ | ✅ 11,641 | ✅ | — |
| Scheduler | ⚠️ | ⚠️ partial | ✅ | — | ✅ | **CRITICAL** |
| Monitoring | ⚠️ static | ✅ 2×/week | ✅ | ✅ | ✅ | HIGH |
| Kill switch | ✅ | n/a | ❌ table absent | ❌ | MOCK ONLY | MEDIUM |
| Recovery / restart | ✅ | ✅ every run | ✅ | ✅ | **VERIFIED LIVE** | — |
| Paper trading | ✅ | ❌ | ❌ | ❌ | MOCK ONLY | HIGH |
| Shadow trading | ❌ | ❌ | ❌ | ❌ | ❌ | — |
| Live trading | ❌ refused | n/a | n/a | n/a | ✅ refusal verified | — |

---

## 6. CONTINUOUS MARKET PRICE ARCHITECTURE

### CURRENT STATE

**FACT** There is no real-time market data anywhere in this project.
**EVIDENCE** Every occurrence of `websocket`, `WebSocket` or `wss://`
in `src/` is prose in a docstring stating its absence:

- `src/domain/paper_models.py:10` — "websocket, and no streaming market feed"
- `src/execution/adapters/ibkr/gateway.py:35` — "IBKR's Client Portal offers a websocket, and this repository has no persistent runtime to hold one"

There is no `asyncio` event loop, no polling daemon, no subscription
manager.
**STATUS** ❌ ABSENT BY DESIGN — correctly documented, never built.

Answers to the twenty questions in the brief:

| # | Question | Answer |
|---|---|---|
| 1 | Where do prices come from? | `price_candle_cache`, filled by `cache_price_candles.py` from Polygon |
| 2 | Historical / delayed / snapshot / polling / streaming? | **Historical daily and minute candles**, fetched in windows around canonical events |
| 3 | Update frequency | **2×/week**, and only for event windows |
| 4 | Websocket? | **No** |
| 5 | IBKR market-data subscription? | Snapshot endpoint only, called ad hoc; no subscription manager |
| 6 | Local cache? | Yes — SQLite `price_candle_cache` (129,122 daily rows) |
| 7 | Ticks stored? | **No** |
| 8 | Quotes stored? | **No** — `IBKRQuote` is in-memory only, `self._quotes` |
| 9 | Bars from live ticks? | **No** |
| 10 | Features from live prices? | **No** — `compute_features.py:125` reads `price_candle_cache` |
| 11 | Signals react intraday? | **No** — regenerated 2×/week |
| 12 | Portfolio/risk react to current prices? | **No** — `prices_as_of` reads cached candles |
| 13 | Continuous revaluation? | **No** |
| 14 | Stops/risk react to market data? | **No** |
| 15 | Market closed? | Calendar says closed; since today, the venue may override towards open |
| 16 | Disconnect? | `connect()` retries with bounded backoff, then AUTH_FAILED → blocks |
| 17 | Reconnect? | Automatic on next invocation; **VERIFIED LIVE** |
| 18 | Stale data? | `EligibilityPolicy.max_price_age_days = 5.0` refuses; quote freshness 60s |
| 19 | Timestamps? | `broker_at` and `received_at` kept separate; UTC enforced |
| 20 | Point-in-time correct? | **Yes** for research. Verified: 398 multi-prediction observations share one `information_cutoff` |

### PROBLEM

**FACT** The price layer was built to reconstruct history reproducibly,
and live trading needs something it was never designed to do.
**EVIDENCE** `cache_price_candles.py` module docstring: "Caches Polygon
daily and minute candles for the primary instrument of every canonical
event, so EventStudyEngine never depends on a live API call." Its range
is `min(anchors) - BASELINE_CALENDAR_DAYS` to `max(anchors) +
FORWARD_CALENDAR_DAYS` — anchored on events, never on "now". It is the
only price-fetching script in the repository.
**IMPACT** No component can obtain a price for the current moment. On
2026-09-11 the newest bar for any instrument was 2026-09-05 while IBKR
quoted AAPL live at 334.37.
**STATUS** ❌ CRITICAL GAP

### PROPOSED ARCHITECTURE

Not implemented. This is the recommendation.

**Primary mechanism: bounded snapshot polling of the IBKR Client Portal
REST endpoint.** Not streaming, for three evidence-based reasons:

1. This project has **no persistent runtime**. Every component is a
   batch job under GitHub Actions. A websocket needs a process that
   outlives a job; introducing one is a larger architectural change than
   the trading problem requires.
2. `IBKR_MAX_REQUESTS_PER_MINUTE = 50` already exists and is enforced by
   a sliding budget that **refuses rather than sleeps**
   (`transport.py::_throttle`).
3. No current strategy needs tick resolution. Labels are `d1`, `d3`,
   `d5`, `d10`, `d20` and intraday 5/15/30/60-minute windows. The
   shortest horizon in use is 5 minutes.

**Fallback:** the existing `price_candle_cache` for anything the venue
cannot answer, with the age recorded and propagated, never silently
substituted.

### DATA FLOW

```
MARKET OPEN (session engine decides)
    ↓
MarketDataService.poll()            every 60s, one batched snapshot
    ↓                               for the whole active universe
normalise → IBKRQuote               (exists: mapper.quote_from_ibkr)
    ↓
LatestPriceState                    in-memory + one persisted row per
    ↓                               instrument (upsert, not append)
1-minute bar builder                aggregate polls into OHLCV
    ↓
market_data_bars (persisted)        retained ~30 days, then rolled into
    ↓                               price_candle_cache dailies
feature refresh (intraday subset)
    ↓
signal evaluation
    ↓
portfolio / risk
    ↓
execution
```

### STATE / CACHE MODEL

| Layer | Where | Lifetime | Why |
|---|---|---|---|
| Latest quote | memory + 1 row/instrument | current session | what risk and sizing need |
| 1-minute bars | `market_data_bars` | ~30 days | the shortest horizon in use |
| Daily bars | `price_candle_cache` | forever | research reproducibility |
| Ticks | **not stored** | — | no strategy needs them; cost without benefit |

### PERSISTENCE MODEL

**Do not put every tick into SQLite.** At 50 requests/minute over a
6.5-hour session that is ~19,500 snapshot rows/day; as ticks it would be
orders of magnitude more. SQLite in a GitHub Release asset already sits
at 276 MB. Persist the 1-minute bars (~390 rows/instrument/day) and one
latest-price row per instrument. Keep the rest in memory.

Critically: **live operational prices must be a separate table from
research candles.** `price_candle_cache` underpins reproducible event
studies. Writing live partial bars into it would make a study computed
today differ from the same study recomputed tomorrow, which is exactly
the failure its docstring says caching exists to prevent.

### UPDATE FREQUENCY

| Layer | Proposed | Rationale |
|---|---|---|
| Quote poll | 60s | well inside the 50/min budget for a ≤25 universe |
| Bar close | 60s | one bar per poll |
| Feature refresh | 5 min | shortest label window is `intraday_5m` |
| Signal evaluation | 5 min, event-driven on bar close | |
| Portfolio/risk | on signal change **and** every 15 min | revaluation must not wait for a signal |
| Reconciliation | 15 min | matches `DEFAULT_CYCLE_SECONDS = 900` |

### MARKET SESSION HANDLING

Covered in §9. The session engine must gate the poller: no polling
outside a session, no orders outside a permitted session.

### STALE DATA HANDLING

The mechanisms already exist and should be reused, not rebuilt:
`IBKRQuote.is_fresh` (60s, rejects negative age),
`MarketDataAvailability.is_tradeable` (only `AVAILABLE`; `DELAYED`
excluded), and `EligibilityPolicy.max_price_age_days`. Staleness must
propagate into eligibility as a named refusal code, never as a silent
substitution of an older price.

### RECONNECT HANDLING

`connect()` already retries with bounded backoff and returns
`AUTH_FAILED` rather than hanging; recovery runs on every invocation and
is **VERIFIED LIVE**. What is missing is *session continuity*: a
brokerage session that lapses mid-day currently requires a human at a
browser. `/iserver/auth/ssodh/init` was added today (best effort,
**UNVERIFIED**) and `heartbeat()` is now called each cycle.

### FEATURE / SIGNAL / PORTFOLIO / RISK / EXECUTION UPDATE

- **Feature**: only the intraday subset needs recomputation; event-study
  features are point-in-time and must not be recomputed live.
- **Signal**: evaluation on bar close, not regeneration of history.
- **Portfolio**: revalue on every price update; propose on signal change.
- **Risk**: re-evaluate before every order **and** on a timer, because
  exposure and drawdown change with price, not with signals.
- **Execution**: triggered by an eligible signal, never by a price tick
  directly.

### END-OF-DAY HANDLING

At session close: stop polling, roll 1-minute bars into daily candles,
run reconciliation, compute trade outcomes, and emit the daily report.

### TEST PLAN

Smallest high-value set, roughly 25 tests:

| Area | Cases |
|---|---|
| Poller | budget respected; refuses rather than sleeps; batches one request for N instruments |
| Staleness | stale quote refuses; delayed quote refuses; negative age refuses |
| Bars | bars built from polls; a gap produces a gap, not an interpolation; out-of-order polls rejected |
| Session | open/close transition; holiday; early close; instrument-specific |
| Reconnect | mid-session disconnect blocks; reconnect resumes; session lapse detected |
| Restart | mid-session restart reconstructs state |
| Propagation | stale price → eligibility refusal, by name |

---

## 7. Current Market Data System

Covered in §6. One point deserves separate emphasis.

**FACT** Today's venue wiring feeds the **session check only**, not
pricing.
**EVIDENCE** `IBKRGateway.market_status` consults `quote()` to decide
whether the market is open (commit `fd96894`, **VERIFIED LIVE**). But
`TradingLoop._prices_for` (`loop.py:1190`) calls
`service.prices.prices_as_of`, which reads `price_candle_cache`
(`valuation.py:126`). The two are unconnected.
**IMPACT** Even with the session correctly detected as open, every price
used for sizing, valuation and risk is a stale daily close.
**STATUS** ⚠️ PARTIAL — session solved, pricing not.

---

## 8. Pipeline / Scheduler Analysis

**FACT** Three workflows have active schedules out of 27.
**EVIDENCE** Uncommented `cron:` keys:

| Workflow | Cron (UTC) | What it runs |
|---|---|---|
| `daily.yml` | `0 13`, `30 16`, `15 21` daily | `run_daily.py` (news), archive, migrate, size reduction |
| `archive_articles.yml` | `0 13`, `30 16`, `15 21` daily | archive, size reduction |
| `pipeline.yml` | `0 2 * * 0`, `0 2 * * 3` | 16 stages, entities → … → signals → outcomes → attribution → memory → dashboard |

**Every other workflow, including `run_trading_loop.yml`,
`evaluate_portfolio_risk.yml`, `generate_signals.yml`, `predict.yml`,
`compute_features.yml` and `run_paper_session.yml`, is manual dispatch
only.**

### Actual timing diagram

```
Sun 02:00 UTC ─┐
Wed 02:00 UTC ─┴─ pipeline.yml: features → train → predict → SIGNALS
                  → outcomes → attribution → memory → dashboard
                  (ONE job, 16 stages, ~2×/week)

13:00 / 16:30 / 21:15 UTC daily ─ news ingestion, archiving
                                  NO features, NO signals, NO dashboard

(no schedule) ─ trading loop          ← THE GAP
(no schedule) ─ paper session
(no schedule) ─ portfolio/risk evaluation
```

**IMPACT** Between Wednesday 02:00 and Sunday 02:00 the system's view of
the market does not change at all, except for news text.
**STATUS** ❌ CRITICAL

### The loop is not a session runner

**FACT** `--cycles N` does not run for N intervals of wall-clock time.
**EVIDENCE** `scripts/run_trading_loop.py:216`:

```python
moment = now + timedelta(seconds=index * args.cycle_seconds)
```

There is no sleep. `--cycles 28` executes 28 cycles in seconds with
anchors spaced 15 minutes apart in *simulated* time, most of them in the
future. `MAX_ANCHOR_DRIFT_SECONDS = 14400` would then block trading on
anchors more than four hours ahead.
**IMPACT** The loop is a bounded batch advancer, not a process that can
hold a market session. Scheduling it every 15 minutes with `--cycles 1`
is the intended shape, and that schedule is commented out.
**STATUS** ❌ BLOCKING for continuous operation

---

## 9. Signal Freshness Analysis

**FACT** The Phase 25.5 finding still holds, and is slightly worse than
reported.
**EVIDENCE** Measured on the current production snapshot:

| Measure | Value |
|---|---|
| Median information lag, active signals | **40.5 h** (25.5 reported 39.0 h) |
| Median information lag, all 423 signals | **433.3 h** (18 days) |
| `DEFAULT_MAX_SIGNAL_AGE_HOURS` | 48.0 |
| Active signals | 4 |
| All expired at | 2026-09-09 — **2 days before audit** |
| Newest signal | 2026-09-09T06:57 |
| Newest daily price bar | 2026-09-05T00:00 |
| Newest news article | 2026-09-10T22:54 |

**IMPACT** Three separate freshness failures compound:

1. **Signals expire before the next pipeline run.** Validity is 5 days;
   the pipeline runs every 3–4 days; the tradeable window after the
   ~40 h information lag is roughly 9 hours, twice a week.
2. **Prices lag signals by 4 days.** News is current to within hours;
   prices are not.
3. **All four active signals were already expired** at audit time.

**This is "the system cannot see opportunities in time", not "there is
no opportunity."** The distinction matters: news arrives within hours,
and nothing consumes it for trading until the next twice-weekly batch.

**STATUS** ❌ CRITICAL — cadence and price freshness, not thresholds.

### The confidence ceiling is structural, not a threshold choice

**FACT** No signal can reach the 0.40 floor under the current model
architecture.
**EVIDENCE** `compute_confidence` = `base × quality × agreement ×
sample`. In production: quality is `high` (1.0, already maximal);
`model_confidence` is NULL on all 1,316 predictions so base defaults to
0.5; all 423 signals have exactly one contribution from one
specification (`ridge_abnormal_return:v1`) so agreement is
`insufficient_evidence` (0.6). `0.5 × 1.0 × 0.6 = 0.30` exactly.
Distribution: 418 signals at 0.300, 5 at 0.150. **Zero at or above
0.40.**
**IMPACT** Waiting for a qualifying signal will never succeed. Only a
second, genuinely independent and preferably probabilistic model
specification changes this.
**STATUS** ❌ CRITICAL — and **must not be fixed by lowering the floor.**

---

## 10. Portfolio Automation

**FACT** The portfolio subsystem cannot react to current prices.
**EVIDENCE** `PortfolioValuator.prices_as_of` reads
`price_candle_cache` for "the most recent cached price at or before
`as_of`". With the newest bar 6 days old, "current" valuation is a
6-day-old close.
**IMPACT** Exposure, weights, target-vs-actual and position deltas are
all computed from stale prices. Revaluation of an open position during a
session is impossible.
**STATUS** ⚠️ CODE COMPLETE, CANNOT OPERATE LIVE

Can it, without manual intervention: revalue ❌ · exposure ⚠️ (stale) ·
weights ⚠️ (stale) · target vs actual ✅ (logic verified, MOCK ONLY) ·
position delta ✅ (10 cases verified) · react to current prices ❌.

**Blocker:** the price source, not the portfolio code.

---

## 11. Risk Automation

**FACT** Risk is evaluated only when something calls it; there is no
timer.
**EVIDENCE** `RISK` is a stage inside `run_cycle`'s decide half, and
`run_cycle` is only reached from a manual CLI invocation.
**IMPACT** Risk controls that depend on price move only when a signal
appears — which is at most twice a week.

| Control | Needs fresh price | Reliable today |
|---|---|---|
| `min_signal_confidence` | no | ✅ |
| Position exposure | **yes** | ❌ stale |
| Concentration / HHI | **yes** | ❌ unmeasurable (recorded as "could not run") |
| Portfolio drawdown | **yes** | ❌ needs snapshot history, none exists |
| Market value | **yes** | ❌ stale |
| Buying power | **yes** | ✅ from IBKR, **VERIFIED LIVE** |
| Intraday loss | **yes** | ❌ no intraday data |
| Volatility | **yes** | ❌ "portfolio holds no priced positions" |
| Stop logic | **yes** | ❌ not implemented |

**Verified live today:** the risk gate correctly refused an order for an
instrument its decision never named, after a defect fix. Refusal message:
`risk decision risk-c5a01ae32253c224 approved no position change, so it
authorises no instrument`.
**STATUS** ⚠️ GATE WORKS, PRICE-DEPENDENT CONTROLS UNRELIABLE

---

## 12. Execution Automation

**FACT** A valid paper signal cannot currently progress to a broker
without a human at five separate points.
**EVIDENCE** Attempted end-to-end today; each step required manual
action.

| Step | Manual? | Classification |
|---|---|---|
| Start Client Portal Gateway | ✅ manual | **AUTOMATION GAP** |
| Browser login | ✅ manual | **REQUIRED GOVERNANCE** — no credential may enter this codebase |
| Set `IBKR_*` environment | ✅ manual | AUTOMATION GAP |
| Dispatch the workflow | ✅ manual | **AUTOMATION GAP** — cron is commented out |
| `--allow-paper-orders` | ✅ manual | **REQUIRED GOVERNANCE** — second gate, deliberate |
| Supply `--decision-id` | ✅ manual | AUTOMATION GAP (the loop supplies it automatically; only the CLI needs it) |
| Model promotion | ✅ manual | **REQUIRED GOVERNANCE** — four-eyes, deliberate |

**STATUS** ❌ NOT AUTOMATED

---

## 13. IBKR Status

Strictly separating demonstrated from supported.

| Capability | Code | Demonstrated |
|---|---|---|
| Connect automatically | ✅ | ✅ **VERIFIED LIVE** |
| Maintain session | ✅ | ⚠️ session lapsed twice today; recovery needed a human |
| Heartbeat | ✅ | ✅ **VERIFIED LIVE** (`/tickle` → 200) |
| Reconnect | ✅ | ✅ **VERIFIED LIVE** (fresh process reconnects) |
| Brokerage session init | ✅ new today | ❌ **UNVERIFIED** — added best-effort, never seen to succeed |
| Subscribe to market data | ⚠️ snapshot only | ✅ **VERIFIED LIVE** (AAPL 334.37, MSFT 495.30, NVDA 219.02) |
| Resolve contracts | ✅ | ✅ **VERIFIED LIVE** (incl. correct ambiguity refusal on 5 candidates) |
| Place orders | ✅ | ❌ **NEVER — zero orders sent** |
| Observe orders | ✅ | ❌ MOCK ONLY |
| Observe fills | ✅ | ❌ MOCK ONLY |
| Observe positions | ✅ | ✅ **VERIFIED LIVE** (empty account) |
| Reconcile | ✅ | ✅ **VERIFIED LIVE** (6 checks, clean) |

**Account** `DUT101249`, `DU`-prefixed, confirmed PAPER by the code's own
check. Equity 1,000,096.19 EUR, buying power 6,667,308.00.

**Cold-subscription behaviour, measured:** the first snapshot for an
unsubscribed contract returns no fields; a cold contract stayed empty
across 4 attempts over 6 seconds in one process, then answered in 0.4 s
from the next. Retrying within one invocation does not hurry it. The
residual behaviour is fail-closed: the first check on a new instrument
withholds a trade, never invents one.

---

## 14. Paper vs Shadow vs Live

| Term | Meaning here | State |
|---|---|---|
| **PAPER** | Orders sent to a real IBKR paper account; real venue, simulated money | Code complete, **zero orders ever sent** |
| **SHADOW** | Real decisions recorded, no orders anywhere; compared against what the market did | **Not implemented** |
| **LIVE** | Real money | **Refused at four independent points** |

`src/paper/` (Phase 13) is a *separate* simulated path whose fills come
from cached bars and which cannot reach a broker. It is not the IBKR
paper path.

### Shadow readiness

| Requirement | Ready |
|---|---|
| Generate real decisions | ❌ none produced in production |
| Record hypothetical orders | ✅ `OrderIntent` is inert by construction |
| Compare hypothetical vs actual execution | ❌ **needs current prices** |
| Measure slippage | ❌ needs a current reference price |
| Simulate fills | ✅ exists (Phase 13) |
| Compare target vs actual | ✅ verified, MOCK ONLY |
| No-capital boundary | ✅ enforced |

**Shadow is blocked by exactly the same missing layer as paper: current
prices.** Without them it would compare a decision to nothing.

---

## 15. Human Intervention Inventory

From market open to market close today:

| # | Action | Classification |
|---|---|---|
| 1 | Start the gateway in a terminal | **AUTOMATION GAP** |
| 2 | Open browser, accept certificate, log in | **REQUIRED GOVERNANCE** |
| 3 | Keep the terminal open (it died twice) | **AUTOMATION GAP** |
| 4 | Export `IBKR_*` variables | AUTOMATION GAP |
| 5 | Dispatch the workflow manually | **AUTOMATION GAP** |
| 6 | Pass `--allow-paper-orders` | **REQUIRED GOVERNANCE** |
| 7 | Inspect status manually | AUTOMATION GAP (observability) |
| 8 | Re-login after session lapse | **AUTOMATION GAP** |
| 9 | Approve model promotion | **REQUIRED GOVERNANCE** |
| 10 | Approve live promotion | **REQUIRED GOVERNANCE** (must stay) |

**Five governance controls to keep. Five automation gaps to close.**
The gateway lifecycle (1, 3, 8) is the largest single source of manual
work and is genuinely accidental — it exists because there is no
supervised long-running process anywhere in the design.

---

## 16. Restart / Recovery

Restart at 09:45, 12:30, 15:59 during an active session:

| Reconstructed | How | Verified |
|---|---|---|
| Account state | fetched from IBKR each run | ✅ **VERIFIED LIVE** |
| Positions | fetched and reconciled | ✅ **VERIFIED LIVE** (empty) |
| Open orders | `ExecutionRepository.restore` on every invocation | MOCK ONLY |
| **Current market prices** | ❌ **cannot** — no live price state exists | ❌ |
| Active signals | read from `signals` | ✅ |
| Portfolio targets | recomputed from the decision | MOCK ONLY |
| Risk state | recomputed | MOCK ONLY |
| Pending execution | idempotency index restored; in-flight → UNKNOWN, never assumed | MOCK ONLY |
| Reconciliation status | re-run on demand | ✅ **VERIFIED LIVE** |

**FACT** Recovery of *state* is well designed; recovery of *market
context* is impossible.
**IMPACT** After a restart the system knows what it holds but not what
anything is worth right now.
**STATUS** ⚠️ PARTIAL

---

## 17. Failure Modes

| Failure | Behaviour | Verdict |
|---|---|---|
| Market data disconnects | no live feed exists to disconnect | n/a |
| IBKR disconnects | bounded retries → AUTH_FAILED → submission blocked | **SAFE / BLOCK** |
| Prices stop updating | already the steady state; age checked against 5-day policy | **DEGRADED** |
| Prices become stale | `max_price_age_days` refuses; quote freshness 60 s | **BLOCK** ✅ |
| Signal becomes stale | `max_signal_age_hours` refuses by name | **BLOCK** ✅ |
| Feature update fails | pipeline job fails; previous rows stand | **DEGRADED / MANUAL** |
| Model inference fails | signals not regenerated; old ones expire | **DEGRADED** |
| Portfolio calc fails | stage records the failure; block raised | **BLOCK** ✅ |
| Risk calc fails | `RiskNotApproved` raised before anything is read | **BLOCK** ✅ |
| Order submission times out | `SubmissionAck(timed_out=True)` → UNKNOWN, never FAILED, never retried | **SAFE** ✅ |
| Broker accepts, local write fails | reconciliation flags `unknown_broker_order`; next cycle blocked | **BLOCK** ✅ |
| Fill arrives late | `_record_unpaired` folds it; state machine advanced | **RECOVER** ✅ (fixed in 25.5) |
| Duplicate fill | `QUANTITY_MISMATCH` recorded, never applied | **SAFE** ✅ |
| Market closes unexpectedly | calendar/venue says closed → refuse | **BLOCK** ✅ |
| Restart during hours | state restored; prices cannot be | **PARTIAL** |

**The failure-mode design is the strongest part of this system.** Every
path fails closed. The weakness is not safety; it is that the system is
so rarely running that most of these paths are never exercised.

---

## 18. Observability

Could an operator answer "what is the system thinking right now?" for
one instrument?

| Question | Available | How stale |
|---|---|---|
| Current price | ❌ | no current price exists |
| Market state | ⚠️ via CLI only | on demand |
| Latest prediction | ✅ dashboard | up to 3.5 days |
| Latest signal | ✅ dashboard | up to 3.5 days |
| Signal age | ✅ computed | up to 3.5 days |
| Portfolio position | ✅ but empty | — |
| Target position | ✅ but empty | — |
| Risk state | ⚠️ | never run in production |
| Pending order | ✅ but none exist | — |
| Broker state | ❌ not on the dashboard | CLI only |
| Last update | ✅ | — |

**FACT** The dashboard is a static artifact rebuilt only by the
twice-weekly pipeline.
**EVIDENCE** `build_dashboard.py` appears in `pipeline.yml` and in
`rebuild_dashboard.yml` (no cron). The generated page contains zero
instances of `fetch(`, `XMLHttpRequest`, `WebSocket` or `<form>` — it
has no network capability at all.
**IMPACT** Real-time observability does not exist. During a trading
session an operator would be reading a page up to 3.5 days old.
**STATUS** ❌ HIGH GAP (and the read-only design is correct and should
be preserved — the gap is freshness, not safety)

---

## 19. Database / Data Reality

Production snapshot, 276,164,608 bytes, 72 tables.

**REAL RECORDS** (produced by scheduled jobs on production data):

| Table | Rows |
|---|---|
| `price_candle_cache` | 129,122 |
| `news_articles` | 49,686 |
| `articles` | 47,857 |
| `article_entities` | 44,500 |
| `research_features` | 34,724 |
| `research_labels` | 18,874 |
| `error_attributions` | 13,405 |
| `outcome_measurements` | 11,641 |
| `trading_experiences` | 11,641 |
| `predictions` | 1,316 |
| `signals` | 423 |
| `signal_contributions` | 423 |
| `experiments` | 6 |
| `trained_models` | 6 (all status `evaluated`) |

**ABSENT — the owning script has never run on production:**

`execution_orders` · `execution_fills` · `risk_decisions` ·
`order_intents` · `positions` · `position_actuals` · `trade_outcomes` ·
`trading_cycles` · `signal_eligibility` · `trading_mode` ·
`paper_loop_sessions` · `trade_lineage` · `paper_validations` ·
`challengers` · `model_promotions`

**Three distinct reasons nothing exists, which must not be conflated:**

1. **table absent** — the script never ran (all of the above);
2. **table present but empty** — ran, correctly produced nothing;
3. **risk declined** — would apply if the loop ran, because 0.30 < 0.40.

**FACT** `model_promotions` is absent: **no model has ever been
promoted.**
**EVIDENCE** All 6 trained models carry status `evaluated`. Dry runs
today on production data:

| Model | Primary metric | Baseline | Beats |
|---|---|---|---|
| `ridge_abnormal_return:v1` | MAE 0.0397, R² −0.2369 | MAE 0.0381 | **No** |
| `logistic_direction:v1` (new) | dir. acc. 0.4167 | 0.4419 | **No** |

**IMPACT** The quality gate is refusing for the correct reason. A
negative R² means the model is beaten by predicting the mean.
`src/modeling/selection.py` records the identical finding from
2026-09-05.
**STATUS** ❌ CRITICAL — the deepest blocker, and not a software defect.

**Correction to the handoff:** it records the memory and experiment
tables as absent from production. They now hold 11,641 and 6 rows
respectively. That document is stale.

---

## 20. Test Coverage

**FACT** 3,877 tests pass; 1 skipped.
**EVIDENCE** Full run, 393 s.

Largest suites: execution 426 · paper 314 · backtest 300 · portfolio 273
· scripts 153 · trading 146 · outcomes 134 · attribution 134.

**Missing categories for automation**, counted by test-name search:

| Concept | Tests |
|---|---|
| `market_open` | **0** |
| `market_close` | **0** |
| `session_transition` | **0** |
| `live_price` | **0** |
| `quote_update` | **0** |
| `backpressure` | **0** |
| `intraday` | 4 |
| `stream` | 2 |
| `out_of_order` | 1 |

Well covered already: disconnect, duplicate fills, partial fills,
restart recovery, stale data refusal.

**Recommended smallest high-value set: ~25 tests**, listed in §6 TEST
PLAN. Do not add hundreds; add the ones that pin the new market-data
boundaries.

---

## 21. Security / Live Safety

**FACT** All live-safety guarantees hold, re-verified today with an
authenticated real gateway present.
**EVIDENCE** `scripts/audit_live_safety.py --untracked` → **16/16 PASS**.
Direct probe: `IBKR_ENVIRONMENT=live` →
`IBKRConfigurationError: IBKR_ENVIRONMENT=live is refused.`

| Check | Result |
|---|---|
| LIVE disabled | ✅ four independent refusals |
| PAPER explicit | ✅ |
| No Trading 212 | ✅ |
| No MT5 | ✅ |
| No broker bypass | ✅ one found and closed today |
| No secret leakage | ✅ no credential literals; no IBKR username/password variable exists |
| No CLI credential arguments | ✅ asserted by parsing `add_argument` calls |
| No automatic live promotion | ✅ |
| Kill switch | ✅ code verified, MOCK ONLY (table absent in production) |
| Fail closed | ✅ demonstrated repeatedly today |

**One note, not a repository issue:** the Client Portal Gateway writes
the IBKR username in plain text to its own log in the user's Downloads
folder. Outside the repo; worth knowing before sharing those logs.

---

## 22. Remaining Automation Gaps

| Gap | Where |
|---|---|
| Market-data streaming/polling | no implementation at all |
| Session management | calendar-based; venue override added today |
| Scheduler | trading loop cron commented out |
| Feature refresh | 2×/week only |
| Signal freshness | expire before the next run |
| Portfolio refresh | stale prices |
| Risk refresh | no timer, price-dependent controls unreliable |
| Execution automation | 5 manual steps |
| IBKR session lifecycle | manual start, manual login, manual recovery |
| Observability | static page, up to 3.5 days old |
| Recovery | state yes, market context no |
| Database scalability | fine today; needs a separate live-price table |

---

## 23. Ranked Blockers

| # | Gap | Severity | Blocks paper autonomy | Blocks shadow | Blocks live | Phase |
|---|---|---|---|---|---|---|
| 1 | **No current market price anywhere** | CRITICAL | ✅ | ✅ | ✅ | 25.7 |
| 2 | **Trading loop has no schedule** | CRITICAL | ✅ | ✅ | ✅ | 25.8 |
| 3 | **No model beats its baselines** | CRITICAL | ✅ | ⚠️ partial | ✅ | 25.9 |
| 4 | **Signals expire before they can be used** | HIGH | ✅ | ✅ | ✅ | 25.8 |
| 5 | **Gateway lifecycle is manual** | HIGH | ✅ | ✅ | ✅ | 25.7 |
| 6 | Loop cannot hold a session (simulated clock) | HIGH | ✅ | ✅ | ✅ | 25.8 |
| 7 | No real IBKR order ever placed | HIGH | ⚠️ | ❌ | ✅ | 25.95 |
| 8 | Risk price-dependent controls unreliable | HIGH | ⚠️ | ⚠️ | ✅ | 25.7 |
| 9 | Observability is 3.5 days stale | MEDIUM | ❌ | ❌ | ✅ | 25.8 |
| 10 | `detect_portfolio_error` unmeasured | MEDIUM | ❌ | ❌ | ⚠️ | 26 |
| 11 | Duplicate `cOID` unverified at venue | LOW | ❌ | ❌ | ✅ | 25.95 |
| 12 | Live-price table does not exist | MEDIUM | ✅ | ✅ | ✅ | 25.7 |

### The five biggest

1. **No current market price.** Nothing can fetch a price for now.
2. **No scheduled loop.** The one component that trades runs only by hand.
3. **No model that beats a baseline.** The deepest, and not a code problem.
4. **Signals expire before use.** Cadence, not thresholds.
5. **Manual gateway lifecycle.** No supervised process exists to hold a session.

---

## 24. Recommended Implementation Sequence

Preserving what works. **Do not rewrite execution, risk, portfolio or
the outcome pipeline** — all are well-designed and fail closed. The work
is connecting existing components and adding one missing layer.

### Phase 25.7 — Operational Market Data Layer

- **Purpose** Give the system a current price.
- **Dependencies** IBKR gateway (done, verified live).
- **Changes** New `MarketDataService` (bounded snapshot polling, 60 s);
  new `market_data_quotes` (latest, upsert) and `market_data_bars` (1 m,
  ~30 day retention); wire `PortfolioValuator` to prefer live state and
  fall back to `price_candle_cache` with the age recorded; a supervised
  gateway process with automatic session recovery.
- **Do NOT change** `price_candle_cache` semantics, event studies,
  point-in-time guarantees, or the existing freshness refusals.
- **Acceptance** A price no older than 60 s is available for every
  universe instrument throughout a session; staleness propagates as a
  named eligibility refusal; the 50/min budget is never exceeded; a
  disconnect blocks rather than serves a stale price.

### Phase 25.8 — Scheduled, Session-Aware Trading Loop

- **Purpose** Make the loop actually run, and run correctly across a session.
- **Dependencies** 25.7.
- **Changes** Uncomment and correct the cron (`*/15 13-20 * * 1-5`) with
  `--cycles 1`; remove the hard-coded `--mock` behind an explicit input;
  replace the simulated-clock advance with real wall-clock anchoring;
  add intraday signal refresh so signals do not expire unused; extend the
  session engine (pre-market, early close, holidays, per-instrument).
- **Do NOT change** idempotency, the atomic cycle claim, or the anchor
  drift guard.
- **Acceptance** The loop runs unattended for a full session; every
  signal receives exactly one eligibility verdict; no duplicate orders
  across restarts; blocks are recorded with reasons.

### Phase 25.9 — Model Quality

- **Purpose** Produce a model that legitimately qualifies.
- **Dependencies** none technical; this is research.
- **Changes** A second, independent, probabilistic specification
  (`fit_logistic` exists and is now selectable via `--family logistic`);
  a binary direction label; walk-forward evaluation; promotion by a
  named human.
- **Do NOT change** `min_signal_confidence`, the promotion gate, or
  `classify_agreement`.
- **Acceptance** At least one model beats all mandatory baselines out of
  sample, is promoted with `--approved-by`, and produces signals that
  clear 0.40 on their own merits.

### Phase 25.95 — Real IBKR Paper Order Validation

- **Purpose** Close gate items 4–7.
- **Dependencies** 25.7, 25.8, and a qualifying signal from 25.9.
- **Changes** None structural. Run the session, record the evidence.
- **Acceptance** Order submitted, acknowledged, filled, position
  reconciled, lineage complete, outcome recorded, duplicate `cOID`
  behaviour observed.

### Phase 26 — Shadow · Phase 27 — Controlled Live

Unchanged in intent, but both now correctly sequenced after the above.

---

## 25. Phase 26 Decision

**FACT** Phase 26 (Shadow Trading) is not the correct next phase.
**EVIDENCE** Shadow requires generating real decisions during market
hours and comparing them against actual market execution. Today the
system generates no decisions during market hours (no scheduled loop),
has no current prices to compare against, and no signal that would
produce a decision even if it ran.
**IMPACT** Starting Phase 26 now would produce a shadow log of zero
decisions compared against stale closes — a component that appears to
work and measures nothing. That is precisely the "fake metric" this
project's own rules forbid.
**STATUS** ❌ **INSERT 25.7 AND 25.8 FIRST.** 25.9 may run in parallel,
being research rather than plumbing.

---

## 26. Automation Scores

Evidence-based. Code existing is not credit.

| Dimension | Score | Justification |
|---|---|---|
| **Data automation** | **75** | News 3×/day and a 16-stage pipeline 2×/week run unattended on production and produce real rows. Cadence is the only weakness. |
| **Market-data automation** | **10** | Historical candles only, fetched 2×/week in event windows. No current price exists anywhere. The 10 is for a working historical cache. |
| **Signal automation** | **40** | Generated automatically from real data, but 2×/week, expiring before use, and capped below the risk floor by construction. |
| **Portfolio automation** | **20** | Logic complete and well tested; never run on production; every price it would use is days stale. |
| **Risk automation** | **30** | Gate logic correct and verified live today; no timer; price-dependent controls unreliable; zero production decisions. |
| **Execution automation** | **20** | Full path exists and validates correctly to the mapping and risk stages; five manual steps; zero orders ever sent. |
| **IBKR automation** | **45** | Session, account, contracts, quotes, reconciliation and recovery all verified live. Ordering never exercised; gateway lifecycle fully manual. |
| **Recovery automation** | **50** | Restore runs on every invocation and reconnect is verified live; market context cannot be reconstructed; order recovery mock-only. |
| **Observability** | **20** | A correct, safe, read-only dashboard rebuilt at most twice weekly. No real-time view of anything. |
| **OVERALL TRADING AUTOMATION** | **25** | Research automation is genuinely strong. Trading automation stops at the signal table. |

---

## 27. Exact Next Steps

**Immediate, this week, no new architecture:**

1. **Decide the market-data approach** (§6). Recommendation: bounded
   60-second snapshot polling, not streaming, because no persistent
   runtime exists and no strategy needs tick resolution.
2. **Update the handoff document.** It is stale: memory and experiment
   tables are populated, and the confidence floor is presented as a
   tunable threshold when the ceiling is structural.
3. **Open the pull request** for the seven pushed commits on
   `ibkr-paper-validation-fixes`.

**Then, in order: 25.7 → 25.8 → 25.95, with 25.9 in parallel.**

**Do not:** lower `min_signal_confidence`; relax model governance to
produce trades; implement a second broker; enable live; or start
Phase 26 before 25.7 and 25.8 are complete.

---

### One-sentence summary

> **Today the system can automatically research the market — ingesting
> news three times a day and regenerating features, models, signals,
> outcomes and attribution twice a week — but it stops at the signal
> table, because no trading loop is scheduled, no component can obtain a
> price for the current moment, and no model has ever qualified to
> trade.**
>
> **To reach genuine autonomous paper trading from open to close:
> A. an operational market-data layer → B. a scheduled, session-aware
> loop with intraday signal refresh → C. a model that beats its
> baselines → D. real IBKR paper order validation.**
