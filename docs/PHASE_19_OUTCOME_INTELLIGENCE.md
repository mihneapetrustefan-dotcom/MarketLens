# Outcome Intelligence — reference

Phase 19 · methodology version `v1` · 2026-09-05

The factual record of what happened after every prediction and every
signal. Measurement only: nothing here explains a failure, scores a
strategy, or touches a model.

---

## 1. The four layers, and why they stay apart

A correct prediction is not a profitable trade, and a losing trade is
not a wrong prediction. Four different things can be measured, and
collapsing them into one "performance" number destroys the ability to
say which one failed.

| Layer | Question | Built? |
|---|---|:---:|
| **Prediction outcome** | was the number right? | **yes** |
| **Signal outcome** | was the claim right, at the stated horizon? | **yes** |
| Execution outcome | did we get the price the signal assumed? | no |
| Portfolio outcome | did the position make money? | no |

The last two are absent because the inputs are absent: `order_intents`,
`positions` and the entire Phase 14 execution schema **do not exist in
the production database**, and no order has ever been created.
Inventing them would produce a portfolio result wearing a signal's
clothes.

A signal has an outcome even when no order existed — that is the whole
point of the separation, and it is what makes suppressed signals
measurable (§38).

---

## 2. Outcome definitions

### Reference price

**Rule:** `first_close_at_or_after_cutoff` — the first close the market
printed at or after the signal's `source_information_cutoff`.

Stored on every row as `reference_rule`, not assumed. A return without
a stated starting point is not reproducible.

Deliberately at-or-*after*. The close **before** the cutoff would
credit the signal with a move that had already happened by the time it
spoke — the easiest way in the world to manufacture an edge that does
not exist.

**It is never an execution price.** Signal quality must not depend on
whether anyone traded it.

### Forward return

```
simple_return = (end_price / reference_price) - 1
log_return    = ln(end_price / reference_price)
```

Both are stored. Log returns add across time, simple returns add across
a portfolio; which is correct depends on the question, and keeping one
guarantees somebody eventually uses it for the other job.

Either is `NULL` when a price is missing or non-positive. **Never
zero.** A zero return where no price existed is indistinguishable from
a real flat move and biases every aggregate toward the middle.

### MFE and MAE

Signed so **favourable is positive and adverse is negative for both
directions**, which is what lets longs and shorts pool into one
distribution:

```
long   mfe = max(high)/ref - 1     mae = min(low)/ref - 1
short  mfe = 1 - min(low)/ref      mae = 1 - max(high)/ref
```

`time_to_mfe_seconds` and `time_to_mae_seconds` record when each
extreme occurred, measured from the reference bar.

Highs and lows are scaled by each bar's own close/adjusted-close ratio,
because this cache stores an adjusted close but unadjusted highs and
lows. When any bar in the window lacks a high or a low, MFE and MAE are
left `NULL` rather than computed over the subset — a partial excursion
understates both without saying so.

### Direction

`DirectionResult` ∈ `hit` · `miss` · `neutral` · `insufficient_data`.

Neutral semantics, stated explicitly:

- A **directional** claim whose realized move is inside the dead band
  is **NEUTRAL, not MISS**. The market did not move enough to say the
  claim was wrong, and recording a miss would punish a signal for an
  absence of evidence.
- A **neutral** claim is an active prediction that nothing much will
  happen. It is a **HIT** inside the band and a **MISS** outside it.
  Direction is irrelevant: broken upward is as wrong as broken down.
- `no_signal` is not a claim and is never scored. Counting abstentions
  would make abstaining a strategy for improving one's record.

The dead band is **10 basis points** (`NEUTRAL_BAND = 0.001`), under a
typical single-name spread — a smaller move was not tradeable in either
direction. Without a band, a 0.0001% drift decides a HIT and hit rate
becomes a measure of arithmetic noise.

Neutrals are **excluded from the denominator** of directional accuracy
and reported separately, so the exclusion is visible rather than
buried.

---

## 3. Horizons and the market calendar

Default ladder: **15m · 1h · 4h · 1d · 3d · 5d · 10d**

Spanning intraday to multi-day is deliberate. A ladder that only
measured days could not discover that an edge dies inside an hour.

`horizon_value` and `horizon_unit` are stored separately from the key,
because `'10d'` sorts before `'5d'` as text and a decay curve built on
string order is wrong in a way that looks entirely plausible.

### One calendar day is not one trading day

- **A daily horizon counts BARS.** `5d` takes the reference bar plus
  five trading sessions. Daily candles only exist for sessions the
  market actually held, so counting bars respects weekends and holidays
  exactly — without this project maintaining a holiday table per venue
  that would itself go stale and be wrong more quietly.
- **An intraday horizon is wall-clock**, resolved against minute bars.
  Those also only exist during sessions, so a 4h horizon opened near
  the close runs out of bars and is reported as such rather than
  spilling into the next day.

### Interval selection

| Horizon unit | Interval used |
|---|---|
| `m`, `h` | `1m` |
| `d` | `1d` |

Measuring a 15-minute horizon on daily candles would return the day's
close and call it a 15-minute move, and nothing on the stored row would
reveal it. When the required interval is unavailable the answer is
`INSUFFICIENT_DATA`, never a substitution.

---

## 4. Status

| Status | Meaning | Response |
|---|---|---|
| `pending` | the window has not closed yet | come back later |
| `available` | measured; every number is real | use it |
| `insufficient_data` | the window closed and the data cannot answer | stop waiting |
| `invalid` | contradictory inputs; an implausible return | investigate |
| `superseded` | a later methodology measured the same subject | read the newer row |

`pending` and `insufficient_data` are kept distinct because they demand
opposite responses. A signal issued an hour ago and one whose
instrument stopped trading must not be filed under the same heading.

**The clock is the DATA's, not the wall's.** `data_as_of` is the newest
bar in `price_candle_cache`. The question is not "has enough time
passed in the world" but "has enough time passed in the data we hold",
and those differ by however stale the cache is.

For a daily horizon the boundary uses a generous calendar bound —
`N × 7/5 + 4` days — because N sessions span more than N calendar days
over a weekend or a holiday. Generous on purpose: being wrong this way
leaves a row `pending` slightly too long and it resolves on the next
run, whereas being wrong the other way would declare a live instrument
permanently unmeasurable and nothing would ever revisit it.

### Invalid rather than clamped

A move larger than **±300%** over a single horizon is almost always a
corporate action the adjusted series did not cover. It is flagged
`invalid` with a note, **never clamped** — a clamped 900% split looks
exactly like a real 100% move, and clamping hides the defect while
keeping the wrong sign.

---

## 5. Versioning and reprocessing

Identity is:

```
(subject_kind, subject_id, horizon, method_version)
```

`method_version` is **part of the primary key**. Three things make a
stored measurement stale, and they are handled differently on purpose:

| Cause | Behaviour |
|---|---|
| New market data closed a `pending` window | Re-measured under the same version — a correction to something explicitly incomplete |
| The methodology changed | Bump `OUTCOME_METHOD_VERSION`. **New rows beside the old ones.** Nothing historical is touched |
| A data correction | `--rescore`, off by default. Silently rewriting a number somebody has read is what §31 warns about |

Writes are `INSERT OR REPLACE`, so running twice with the same
methodology **cannot change the row count**. That is stronger than
remembering what was done: it survives a crash halfway through and
needs no bookkeeping table.

Aggregates *are* recomputed wholesale. An aggregate is a derived view —
a stale one is worse than a rebuilt one — whereas a measurement is an
observation and is never recomputed under the same version.

---

## 6. Point-in-time rules

Outcome measurement is the **one place in this repository allowed to
read prices dated after an information cutoff**. Everywhere else that
is a bug and `PointInTimeView` raises `LookAheadViolation`.

That permission is safe only because the flow is one-directional:

```
future prices  ->  outcome         ALLOWED — it is the job
outcome        ->  prediction      FORBIDDEN
outcome        ->  feature         FORBIDDEN
outcome        ->  signal          FORBIDDEN
outcome        ->  model           FORBIDDEN
outcome        ->  training set    FORBIDDEN without an explicit
                                   dataset version and cutoff
```

Enforced three ways:

1. **Structurally** — `tests/outcomes/test_leakage.py` parses the
   AST of every module in `src/outcomes/`, collects the strings handed
   to `execute`/`executemany`, and asserts that the only tables written
   are `outcome_measurements` and `outcome_aggregates`.
2. **By ordering** — outcome measurement is stage 11 of 12, after
   signal generation. A test fails if the workflow order changes, and
   another asserts no earlier script reads the outcome tables.
3. **Behaviourally** — measurements are run against spectacular future
   bars and the feature and prediction tables are compared byte for
   byte before and after.

---

## 7. Statistics, and what is deliberately not claimed

**Distributions, not win/loss.** Every cohort carries mean, median,
standard deviation, min, max and the 10/25/75/90 percentiles alongside
the counts. A win rate is compatible with a strategy that makes a penny
51 times and loses a pound once.

**Sample size is never optional.** `sample_size` and `small_sample` are
`NOT NULL`. The threshold is `ModelEvaluation.MIN_EFFECTIVE_SAMPLE`
(30) — the Phase 9 evaluator's own number, reused rather than
redefined, because two definitions of "too small to mean anything" in
one repository is one too many.

**Confidence intervals only where computed.** A deterministic
percentile bootstrap (2,000 resamples, seeded from the data itself, so
the same cohort gives the same interval every run). Below 30
observations it returns `NULL` and `ci_method` is empty. **`NULL` means
"not calculated", never "zero width".**

**No significance is claimed anywhere.** No p-value is computed and
`outcome_aggregates` has no column that could hold one. Slicing the
same measurements by horizon × model × instrument × regime × direction
× confidence produced **3,514 cohorts**; at the conventional 5% level
roughly one in twenty will look notable through chance alone, and the
most extreme-looking cohort is the one most likely to be noise. That
caveat is rendered on the dashboard page beside the tables, not filed
in a document nobody opens while reading them.

---

## 8. Database

### `outcome_measurements`

One row per subject × horizon × methodology. Groups: identity, status,
window, prices, returns, excursions, direction, error, measurement
provenance (`data_source`, `data_interval`, `bars_observed`,
`data_as_of`), and context copied at measurement time for slicing
without a join.

Context is **copied, not joined**, so a later demotion or relabelling
cannot silently rewrite what a past measurement was made under.

### `outcome_aggregates`

One row per cohort × horizon × methodology, rebuilt on each run.

### Why not `signal_outcomes`?

The Phase 10 table is audited and kept, and is **not** extended, for
two structural reasons:

1. It is keyed `PRIMARY KEY (signal_id, horizon)`. There is nowhere to
   put a methodology version, so re-measuring under a new rule would
   overwrite the old measurement. SQLite cannot alter a primary key in
   place.
2. It is **label-derived**: `realized_return` comes from the Phase 7
   research label. Phase 19 measures from price candles — a reference
   price, an end price, and the highs and lows between — and there is
   no column for any of those, nor any concept of a window still open.

It keeps its ten rows and its history, documented as the predecessor.
Two tables that *measure* the same thing would be redundant; a
superseded table left intact for its history is not.

---

## 9. API

`docs/API_AUDIT.md` records that this repository has **no HTTP layer**,
by decision, with the assessment *KEEP*. The §46 routes are therefore
implemented as typed functions over a connection — the same convention
as `ExecutionService` and the fourteen repository classes.

| Route | Function |
|---|---|
| `GET /outcomes` | `list_outcomes()` |
| `GET /signals/{id}/outcomes` | `outcomes_for_signal()` |
| `GET /predictions/{id}/outcomes` | `outcomes_for_prediction()` |
| `GET /models/{id}/outcomes` | `outcomes_for_model()` |
| `GET /outcomes/summary` | `summary()` |
| `GET /outcomes/by-horizon` | `by_horizon()` |
| `GET /outcomes/by-regime` | `by_cohort("regime")` |
| `GET /outcomes/by-instrument` | `by_cohort("instrument")` |
| §45 comparison | `model_quality_versus_outcome()` |

All read-only, all paginated where they list (`MAX_LIMIT = 1000`).
Adding a web server to satisfy the letter of §46 would introduce the
project's first inbound network surface and contradict a documented
decision.

---

## 10. Running it

```bash
python scripts/measure_outcomes.py --apply                 # signals
python scripts/measure_outcomes.py --apply --predictions   # both
python scripts/measure_outcomes.py --horizons 1d,5d,20d --apply
python scripts/measure_outcomes.py --apply --rescore       # data correction
```

In the pipeline it is **stage 11 of 12**, immediately after signal
generation and immediately before the dashboard rebuild. That position
is the leakage control, and it is asserted by a test.

Measured cost on the production database (6,510 measurements over 408
signals and 549 predictions, seven horizons): **1.4s to measure,
~27s to aggregate.**

---

## 11. What future learning can now ask

| Question | Answered by |
|---|---|
| What did the system predict? | `expected_return`, `expected_direction` |
| What happened? | `simple_return`, `log_return`, `realized_direction` |
| How large was the error? | `error`, `absolute_error` |
| How quickly did it happen? | `time_to_mfe_seconds`, `time_to_mae_seconds` |
| Was the direction correct? | `direction_result` |
| Was the horizon right? | the same subject across seven horizons |
| What regime was active? | `market_regime` |
| Which model produced it? | `trained_model_id`, `model_status` |
| Was it validated or experimental? | `model_status` |
| How much can I trust the answer? | `status`, `bars_observed`, `sample_size` |

**Phase 20 is error attribution** — separating prediction error from
timing, sizing, risk, execution, regime and data error. It needs this
record and must not be built on top of guesses about cause. Nothing in
this layer says *why* an outcome happened; `direction_result` says HIT
or MISS and never "the model was wrong". A field that guessed at cause
would be an opinion stored as a fact, and every later analysis would
inherit it.

First measure. Then attribute. Then learn.
