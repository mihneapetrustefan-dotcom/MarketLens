# Phase 19 — final report

Date: 2026-09-05 · Base commit: `6138c42` · Scope: the outcome layer, nothing else

Reference documentation: `docs/PHASE_19_OUTCOME_INTELLIGENCE.md`.

Every figure below was measured against the **production release
asset** (`db-latest`, 213.7 MB, published 2026-09-04T23:06Z),
downloaded and measured directly.

---

## 1. Phase 18 recommendations review

| Item | Status at entry | Verified now | Notes |
|---|---|---|---|
| **Model quality gate** | wired to `is_deployable` | **HOLDS** | Untouched. `git diff HEAD -- src/modeling` is empty |
| **Experimental / validated** | derived from `trained_models.status` | **HOLDS, and now propagates** | `model_status` is copied onto every measurement, so outcomes never pool research with production output |
| **Signal canonicalization (TD-03)** | labelled, not migrated | **HOLDS** | The outcome layer measures `signals` only. `recommendations` is not an input |
| **Article canonicalization (TD-02b)** | dashboard repointed, 4 readers blocked | **UNCHANGED** | Phase 19 touched no article reader |
| **Pipeline verification (TD-05)** | **0 runs, first cron 2026-09-06 02:00 UTC** | **STILL 0 RUNS** | Unchanged — and Phase 19 has now added a stage to that pipeline, so the first scheduled run tests twelve stages rather than eleven |
| **Confidence semantics (NEW-02)** | heuristic score, near-constant | **CONFIRMED IN THE OUTCOME DATA** | Every measured signal falls in one confidence bucket. Reported, not manufactured (§18) |
| **Model lineage** | direct via `signal_contributions` | **HOLDS** | Every signal outcome carries `trained_model_id`; used as a cohort |
| **IBKR safety** | 16/16 pass | **16/16 PASS** | Re-run at the end of this phase |
| **No model passes the gate** | 4 models, all failing | **UNCHANGED, now measurable forward** | See §13 |

Nothing in Phase 18's architecture was modified.

---

## 2. Initial state

```
production           47 tables · signal_outcomes: 10 rows (Phase 10, label-derived)
signals             408   (212 instruments, cutoffs 2026-08-05 .. 2026-09-03)
predictions         549
price_candle_cache  116,719 bars   1d: 2025-10-29 .. 2026-09-03
                                   1m: 2026-07-06 .. 2026-09-03
tests             3,000
```

Feasibility, checked before building: **all 212** signal instruments
have candles, and **394 of 408** signals have at least one future daily
bar. Both a daily and a minute series exist, so intraday horizons are
measurable rather than aspirational.

---

## 3. Outcome architecture

Four layers, kept apart (§2, §3):

| Layer | Built | Why |
|---|:---:|---|
| Prediction outcome | **yes** | `predictions` exist |
| Signal outcome | **yes** | `signals` exist |
| Execution outcome | no | `order_intents` does not exist in production |
| Portfolio outcome | no | `positions` does not exist; no order has ever been created |

The last two were not stubbed. A "return" that quietly assumed equal
position sizing would be a portfolio result wearing a signal's clothes,
which is precisely what §2 forbids.

**Files:** `src/domain/outcome_models.py` (definitions and pure
arithmetic), `src/data_access/outcome_schema.py`,
`src/outcomes/measurement.py` (bars → measurement),
`src/outcomes/pipeline.py` (batch, idempotent),
`src/outcomes/analytics.py` (distributions),
`src/outcomes/api.py` (queries), `scripts/measure_outcomes.py`.

---

## 4–5. Prediction and signal outcomes

One table, `subject_kind` distinguishing them. They are the same
measurement — a forward return over a window from a reference price —
asked about different claims. Two near-identical tables would drift.

`subject_kind` is part of the primary key, so a prediction and a signal
sharing an id cannot overwrite one another; a test asserts it.

**Measured against production:**

| | Signals | Predictions |
|---|---:|---:|
| Subjects | 395 | 535 |
| Measurements | 2,765 | 3,745 |
| Available | 1,823 | 2,488 |
| Pending | 263 | 342 |
| Insufficient | 679 | 915 |

A prediction's direction is derived from the sign of `predicted_value`
purely to make the excursion arithmetic well-defined. It is not a
trading claim; the signal layer is what turns a number into one.
Abstentions are skipped — scoring the absence of a claim would make
abstaining a way to improve one's record. 13 subjects were skipped on
that basis.

---

## 6. Outcome windows

Default ladder **15m · 1h · 4h · 1d · 3d · 5d · 10d** — intraday to
multi-day, because a ladder that only measured days could not discover
an edge that dies inside an hour.

`horizon_value` and `horizon_unit` are stored apart from the key.
`'10d'` sorts before `'5d'` as text, and a decay curve built on string
order is wrong in a way that looks entirely plausible on a chart; the
sort is arithmetic everywhere and a test pins it.

**The calendar rule (§34).** A daily horizon counts **BARS** — the
reference bar plus N trading sessions. Daily candles only exist for
sessions the market held, so counting bars respects weekends and
holidays exactly, without this project maintaining a per-venue holiday
table that would go stale and be wrong more quietly. A test seeds a
Friday→Monday gap and asserts the horizon is not consumed by the
weekend.

Intraday horizons are wall-clock over minute bars. Interval selection
is one-way and total: `d`→`1d`, `m`/`h`→`1m`. Measuring a 15-minute
horizon on daily candles would return the day's close and call it a
15-minute move.

---

## 7. Forward returns

Both simple and log are stored. Log returns add across time, simple
returns add across a portfolio; keeping one guarantees somebody
eventually uses it for the other job.

Either is `NULL` when a price is missing or non-positive — **never
zero**. A non-positive price is not a cheap stock, it is bad data, and
dividing by it yields something that looks like a return.

**Reference price:** `first_close_at_or_after_cutoff`, stored on every
row as `reference_rule`. Deliberately at-or-*after*: the close before
the cutoff would credit the signal with a move that had already
happened by the time it spoke. It is never an execution price — signal
quality must not depend on whether anyone traded it (§8, §37).

---

## 8. Directional analysis

`hit` · `miss` · `neutral` · `insufficient_data`, with the neutral
semantics §9 asks to be documented:

- a **directional** claim inside the 10bp dead band is **NEUTRAL, not
  MISS** — the market did not move enough to say the claim was wrong;
- a **neutral** claim is a HIT inside the band and a MISS outside it,
  in either direction;
- `no_signal` is never scored.

Neutrals are excluded from the denominator and reported separately.
`insufficient_data` is a real enum member, so a missing measurement
never reads as a failure (§10) — verified on production: **0** rows
where a null return produced a `miss`.

**Production result:** 1,934 hits, 1,856 misses, 521 neutrals.
Directional accuracy **51.0%** at **66.2%** coverage.

---

## 9–10. MFE and MAE

Signed favourable-positive for both directions, which is what lets
longs and shorts pool into one distribution. Highs and lows are scaled
by each bar's own close/adjusted-close ratio, since this cache stores
an adjusted close but raw highs and lows.

When any bar lacks a high or a low, both are left `NULL` rather than
computed over the subset — a partial excursion understates both without
saying so.

A cheap invariant runs on every measurement: the realized return must
sit between MAE and MFE. **0 violations** across 6,510 production rows.

Mean excursions widen with horizon exactly as they should:

| Horizon | mean MFE | mean MAE |
|---|---:|---:|
| 15m | +0.23% | −0.25% |
| 1d | +2.09% | −1.95% |
| 5d | +3.78% | −3.11% |
| 10d | +5.65% | −4.15% |

---

## 11. Time to outcome

`time_to_mfe_seconds` and `time_to_mae_seconds` on every measured row,
plus `time_to_threshold()` for an arbitrary target. It uses each bar's
favourable *extreme*, answering "when was this first reachable" rather
than "when did it close there" — the question a stop or a target
actually asks. A threshold never reached returns `None`, never the
window length, which would silently claim it was reached at the end.

---

## 12. Signal decay

The §14 question, measured (signals, overall):

| Horizon | n | dir. accuracy | mean return | median |
|---|---:|---:|---:|---:|
| 15m | 211 | 52.9% | −0.06% | −0.04% |
| 1h | 216 | 47.0% | +0.02% | +0.00% |
| 4h | 216 | 47.0% | +0.03% | +0.00% |
| 1d | 369 | 46.6% | +0.09% | +0.01% |
| 3d | 351 | 56.3% | −0.09% | −0.39% |
| 5d | 314 | 52.1% | +0.00% | −0.09% |
| 10d | 146 | 55.0% | +0.30% | −0.06% |

There is no decay curve here because there is no edge to decay.
Accuracy oscillates between 46.6% and 56.3% with no monotone structure,
and every mean return is inside a tenth of a percent of zero. That is
the expected result for models that fail their own baselines, and it is
the first time the system has been able to say so from realized data
rather than from a held-out split.

---

## 13. Model analysis, and §45

Training metrics and realized outcomes are stored and displayed in
**separate columns**, never pooled (§44):

| Model | r² (training) | Beats baselines | Forward n (5d) | Forward accuracy |
|---|---:|:---:|---:|---:|
| `tm-37886ecf` | −0.239 | no | 200 | 51.3% |
| `tm-6666c760` | −0.313 | no | 110 | 52.8% |
| `tm-99df38e0` | −0.197 | no | 4 | **75.0%** |
| `tm-0be69458` | −0.197 | no | — | — |

That 75.0% is the most instructive number in this report and it means
nothing: it is **four observations**. It is exactly what §41 warns
about, it is flagged `small_sample` in the data, and it is why nothing
here is presented as significant.

`model_quality_versus_outcome()` returns the two sets side by side and
draws no conclusion. It becomes genuinely interesting the moment a
model is promoted — the question it exists to answer is whether
`beats_all_baselines` predicts forward performance at all.

---

## 14. Signal analysis

Cohorts by direction, instrument, event type, regime, strategy, signal
status, model, model status, confidence bucket and strength bucket —
**3,528 cohorts**, each carrying its own sample size.

Long 51.9% (n=107) versus short 52.2% (n=207) at 5d. Both are inside
the noise, which is the honest reading.

Suppressed signals are measured too (§38). A suppressed signal still
made a claim, and measuring it is the only way to learn whether the
suppression rule is any good. They are outcomes, never trades — no
order existed and none is implied.

---

## 15. Regime analysis

Sliced on `research_observations.market_regime`, the project's existing
classification. No regime label was invented (§20).

---

## 16. Confidence analysis — the honest limitation

Phase 18 established that confidence is a **heuristic score, not a
probability**, and that 403 of 408 signals carry exactly 0.30 because
three of its four factors are structurally constant.

The outcome data confirms it: **every measured signal falls into a
single confidence bucket**. Confidence analysis is therefore
**degenerate today** — there is no variation to correlate with
anything.

§18 says to report that limitation rather than manufacture diversity,
so the cohort exists, is populated, shows one row, and the dashboard
says why. Nothing was rebucketed to produce a spread.

---

## 17. Strength analysis

Kept strictly separate from confidence (§19), with different bucket
labels so the two can never silently merge. A test asserts the label
sets are disjoint. Unlike confidence, `strength` does vary across the
full 0–1 range in production, so this cohort is the one that can
actually discriminate.

---

## 18. Data quality

Verified on all 6,510 production measurements:

| Check | Result |
|---|---|
| A non-available row carrying a return | **0** |
| A null return recorded as a `miss` | **0** |
| `sample_size` null on an aggregate | **0** |
| `small_sample` null on an aggregate | **0** |
| Return outside [MAE, MFE] | **0** |
| Rows flagged `invalid` | 0 |

Extreme returns are **flagged, never clamped** (§56). Beyond ±300% a
row becomes `invalid` with a note, because a clamped 900% split looks
exactly like a real 100% move and clamping hides the defect while
keeping the wrong sign. Adjusted closes are preferred where present
(§25).

**Coverage is 66.2%**, and the gap is real rather than hidden: 1,594
rows are `insufficient_data`, overwhelmingly because minute bars only
begin 2026-07-06 and cover only studied instruments, so 151 signals
have no intraday coverage at all. That is a data limitation reported as
one.

---

## 19–20. Point-in-time safety and leakage tests

Outcome measurement is the one place allowed to read prices dated after
a cutoff. The permission is safe because the flow is one-directional,
and that is enforced three ways:

1. **Structurally.** `tests/outcomes/test_leakage.py` parses the AST of
   every module in `src/outcomes/`, collects the strings handed to
   `execute`/`executemany`, and asserts the only tables written are
   `outcome_measurements` and `outcome_aggregates`. Also: no module
   imports the training or inference engine, and none contains
   `promote` or `.fit(`.
2. **By ordering.** Outcomes are stage 11 of 12, after signal
   generation. One test fails if the workflow order changes; another
   asserts no earlier script reads the outcome tables; a third asserts
   the feature engine, modeling engine, inference and signal strategy
   do not mention them.
3. **Behaviourally.** Measurements run against spectacular future bars
   (highs of 1e6, lows of 0.01) and the feature and prediction tables
   are compared byte for byte before and after.

**A note on the first version of the structural test.** It scanned
every string literal and flagged this package's own docstrings — which
say "it does not write to `signals`" and "writes are `INSERT OR
REPLACE`" — as evidence of the thing they promise not to do. Scoping it
to executed SQL is both narrower and stricter: prose cannot trip it,
and a real write cannot hide from it.

---

## 21. Idempotency

Identity is `(subject_kind, subject_id, horizon, method_version)`;
writes are `INSERT OR REPLACE`. Verified on the production copy:

```
run 1                    6,510 rows
run 2                    6,510 rows   (+0)   5,834 settled skipped, 676 pending revisited
run 2 with --rescore     6,510 rows   (+0)
```

Stronger than remembering what was done: it survives a crash halfway
through and needs no bookkeeping table. A `pending` row is **never**
skipped — it exists to be revisited — and a test drives one from
`pending` to `available` by adding data and asserts the row count does
not grow.

---

## 22. Versioning

```
method v2 run            9,275 rows   (+2,765 NEW rows)
v1 rows afterwards       6,510        (untouched)
```

`method_version` is part of the primary key, so a methodology change
writes new rows beside the old ones and cannot rewrite a number
somebody has already read (§26, §31). Aggregates *are* rebuilt
wholesale — a derived view, where a stale one is worse than a rebuilt
one.

---

## 23. API

`docs/API_AUDIT.md` records that this repository has no HTTP layer, by
decision, assessed *KEEP*. All eight §46 routes are implemented as
typed functions over a connection — the convention `ExecutionService`
and the fourteen repository classes already use — plus
`model_quality_versus_outcome()` for §45. All read-only; the list
endpoints paginate with a hard `MAX_LIMIT = 1000`, because
`outcome_measurements` grows linearly in every new signal.

Adding a web server to satisfy the letter of §46 would have introduced
the project's first inbound network surface and contradicted a
documented architectural decision.

---

## 24. Database

`outcome_measurements` (44 columns, PK includes `method_version`) and
`outcome_aggregates` (35 columns). Five indexes. Both created
idempotently; **no existing table was altered, and no row was
rewritten**.

**Why `signal_outcomes` was not extended.** §48 asks to reuse it where
semantically correct. It is neither:

1. It is keyed `(signal_id, horizon)` — nowhere to put a methodology
   version, so re-measuring would overwrite. SQLite cannot alter a
   primary key in place.
2. It is **label-derived**: `realized_return` comes from the Phase 7
   research label. Phase 19 measures from candles — reference price,
   end price, highs and lows — and it has no column for any of them,
   nor any concept of a window still open.

It keeps its ten rows and its history, documented as the predecessor.
Two tables that *measure* the same thing would be the redundancy §48
warns about; a superseded table left intact for its history is not.

---

## 25. Dashboard

A new **"Rezultate reale"** workspace. Coverage is presented **first**,
before any rate: "51% directional accuracy" means something very
different at 66% coverage than at 95%, and a page that led with the
rate would invite a reader to skip the number that qualifies it.

Shows: status mix, the decay table, training-versus-realized model
comparison, cohorts by direction / model status / regime / confidence /
strength / instrument, and 40 recent measurements with return, MFE, MAE
and bar count.

Every cohort table flags small samples. The multiple-testing caveat is
rendered **on the page beside the tables**, naming the cohort count —
a caveat in a document nobody opens while reading a table is not a
caveat. The page states plainly that it does not measure profit, and
that `insufficient_data` is never a zero and never a miss.

It renders on a database where Phase 19 has never run; four tests cover
that, including one that drops `trained_models` entirely.

---

## 26. Test results

```
PYTHONPATH=src python -m unittest discover -s tests -p 'test_*.py' -t .

Ran 3116 tests in 384.544s
OK (skipped=1)
exit 0
```

**+134 tests**, none suppressed, none deleted, no existing test
modified.

| File | Tests |
|---|---:|
| `tests/outcomes/test_measurement.py` | 55 |
| `tests/outcomes/test_pipeline.py` | 47 |
| `tests/outcomes/test_leakage.py` | 16 |
| `tests/outcomes/test_dashboard_outcomes.py` | 16 |

All 24 required areas in §53 are covered.

---

## 27. Adversarial tests (§54)

| Case | Covered by |
|---|---|
| Future candle loaded before prediction | `test_a_spectacular_future_move_changes_no_stored_input` |
| Outcome written into the feature store | `test_no_module_writes_to_a_model_input_table` (AST) |
| Outcome included in a training dataset | `test_no_earlier_pipeline_script_reads_the_outcome_tables` |
| Duplicate outcome calculation | `test_running_five_times_does_not_change_the_row_count` |
| Insufficient horizon treated as zero | `test_an_insufficient_horizon_is_not_a_zero_return` |
| Missing market data treated as a miss | `test_missing_market_data_is_not_a_miss` |
| Delisted asset treated incorrectly | `test_a_delisted_instrument_is_insufficient_not_a_total_loss` |
| Corporate action creates a false return | `test_an_adjusted_close_is_preferred_over_the_raw_close`, `test_an_implausible_return_is_flagged_invalid_not_clamped` |
| Duplicate signal outcome | `test_a_prediction_and_a_signal_never_share_a_row` |
| Methodology change without versioning | `test_the_old_version_is_left_completely_untouched` |

**The delisted case found a real bug.** My first `pending` rule
compared the reference bar against `data_as_of`, which meant a delisted
instrument stayed `pending` forever — never resolving and never
admitting it could not. It now uses a deliberately generous calendar
bound (`N × 7/5 + 4` days): wrong in that direction leaves a row
pending slightly too long and it resolves next run; wrong the other way
would declare a live instrument permanently unmeasurable.

---

## 28. Performance

Measured on the production database — 408 signals, 549 predictions,
seven horizons, 6,510 measurements:

| Stage | Time |
|---|---|
| Measurement | **1.4s** |
| Aggregation (3,528 cohorts) | **26.6s** |

Aggregation was **84.7s** before one change. §55 says optimise only
after measuring, so I profiled first: 99.6% of the time sat in
`bootstrap_mean_interval`, in **82.4 million** `rng.randrange(n)` calls.
Replacing the resampling loop with `random.choices` — same algorithm,
same seed, same numbers, executed in C — cut it to 26.6s. That is the
only optimisation in this phase and it was measured, not guessed.

Bars are loaded once per `(instrument, interval)` and reused across all
seven horizons rather than reloaded per horizon, which is a sevenfold
reduction in queries by construction.

**One finding outside this phase.** The full suite takes 384s, and
`tests/scripts/test_migrate_registries_to_canonical.py` accounts for
**255s of it — 66%**. Verified as pre-existing by stashing every Phase
19 change and re-timing it at `HEAD`: **268.5s**. The 134 new outcome
tests total **7.2s**. Recorded, not fixed: it is unrelated to this
phase and optimising it is not a licence this phase carries.

---

## 29. Security

| Check | Result |
|---|---|
| New credentials | none |
| New secrets | none |
| New inbound surface | **none** — no framework, no bound socket |
| New dependency | **none** — standard library only |
| Live execution | still impossible |
| MT5 | absent |
| New broker | none |

`scripts/audit_live_safety.py`: **16 of 16 PASS**.

---

## 30. IBKR safety

**Untouched.** `git diff HEAD -- src/execution src/risk src/portfolio
src/paper src/brokers src/pointintime src/modeling` is **empty**.

Signal → Portfolio → Risk → OrderIntent → Intake → Orchestrator →
Safety → Validation → IBKR Paper is byte-for-byte what Phase 17.5
verified and Phase 18 left alone.

The outcome layer sits strictly *downstream* of everything and writes
only its own two tables, so it cannot affect what reaches risk.

---

## 31–33. Files and migrations

**Created (12)** — `src/domain/outcome_models.py`,
`src/data_access/outcome_schema.py`, `src/outcomes/__init__.py`,
`src/outcomes/measurement.py`, `src/outcomes/pipeline.py`,
`src/outcomes/analytics.py`, `src/outcomes/api.py`,
`scripts/measure_outcomes.py`, four test modules, plus
`docs/PHASE_19_OUTCOME_INTELLIGENCE.md` and this report.

**Modified (2)** — `src/dashboard.py` (collector, workspace, nav,
router), `.github/workflows/pipeline.yml` (stage 11 of 12).

**Removed** — none.

**Migrations** — two additive tables and five indexes, created
idempotently on first use. No existing table altered, no column
dropped, no row rewritten. Nothing in this phase modified production
data.

---

## 34. Remaining issues

1. **TD-05 is still unverified.** `pipeline.yml` has run zero times;
   the first fires 2026-09-06 02:00 UTC. Phase 19 has added a stage to
   it, so that run now tests twelve stages.
2. **Coverage is 66.2%.** 151 signals have no intraday price coverage,
   because minute bars begin 2026-07-06 and cover only studied
   instruments. `--include-unstudied` (Phase 18) has not run yet.
3. **Confidence analysis is degenerate** — one bucket. Not fixable
   here; it needs a second model family.
4. **No model passes the gate**, so the §45 comparison has one side
   constant.
5. **Execution and portfolio outcomes do not exist**, because orders
   and positions do not.
6. **`test_migrate_registries_to_canonical` is 66% of suite runtime** —
   pre-existing, measured, unrelated to this phase.

---

## 35. Future learning readiness

Every question §50 asks is now answerable from a single table, with a
status saying how much to trust the answer. What Phase 20 inherits:

- **Measurement without attribution.** Nothing says *why* an outcome
  happened. `direction_result` says HIT or MISS and never "the model
  was wrong". A field guessing at cause would be an opinion stored as a
  fact, and every later analysis would inherit it.
- **The layers already separated.** Prediction outcome and signal
  outcome are distinct rows, so "the model was right and the signal
  layer ruined it" is a query rather than a theory.
- **Excursions and timing.** MAE and time-to-MAE are what sizing, stop
  and timing attribution will need, and they cannot be reconstructed
  later from a return alone.
- **Experimental output stays labelled.** Attribution that pooled
  research output with production output would measure nothing.
- **Versioned and idempotent**, so a methodology change during Phase 20
  cannot silently rewrite the record it is reasoning about.

---

## 36. Next phase

Phase 20 — **error attribution**: separating prediction error from
timing, sizing, risk, execution, regime and data error. Deliberately
not started here.

---

# READY FOR PHASE 20

With the same standing watch Phase 18 left open, now slightly larger:

**Confirm the 2026-09-06 02:00 UTC pipeline run.** It is still
unverified, and it now carries twelve stages rather than eleven.

Phase 19 built the factual layer and nothing else. It promotes no
model, modifies no model, trains nothing, and enables no execution. It
measured 6,510 outcomes against production and found what the model
evaluations already implied — **51% directional accuracy, mean returns
within a tenth of a percent of zero, no decay curve because there is no
edge to decay**.

That is the point. The system can now state what happened rather than
what it hoped would happen, and it can do so before anyone tries to
explain it.
