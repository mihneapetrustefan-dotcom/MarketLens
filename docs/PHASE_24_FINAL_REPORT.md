# PHASE 24 — FINAL REPORT

**Challenger Models & Strategy Variants**
Date: 2026-09-08 · Method version `v1`

---

## PHASE 23.5 RECOMMENDATIONS REVIEW

The audit closed with no critical or high issues, two medium and four
low. Each was re-checked against code and database, not against the
report.

| Item | Status | Evidence |
|---|---|---|
| **M1** 28 days of record, one 8-day window | **VERIFIED, and now binding harder** | the record spans 29 days — shorter than one walk-forward window, so `stability` is unmeasurable and `SUPERIOR` is unreachable (§20) |
| **M2** Pipeline stages 13–15 never run under automation | **VERIFIED** | the production release asset still has no `trading_experiences`, `memory_patterns`, `experiments` or `autoresearch_*` table |
| **L3** Reuse counted at interpretation time | **UNCHANGED, by design** | the ledger shows the current count beside stored conclusions; challenger results carry `window_reuse_count` the same way |
| **L4** Blank feature/label/model/strategy versions | **CARRIED FORWARD** | challengers have the columns and populate what applies; the signal-cohort case genuinely has no feature or model version (§8) |
| **L5** Research concentration 1.00 | **VERIFIED, unchanged** | one challenger, signal type; the other seven types are declared and blind for stated reasons |
| **L6** No static analysis or coverage tooling | **VERIFIED, unchanged** | still none configured; stated rather than substituted with a number |

**The audit's five repairs were re-verified as still fixed**, and three
of them are now load-bearing in Phase 24:

- the **stale-cache fix** is inherited by construction — the dataset
  cutoff is inside `Challenger.fingerprint`, so a grown record is a
  different challenger (§56, tested)
- the **atomic queue claim** and **stale reclaim** are re-implemented
  against the challenger tables (§53, §55)
- the **row-counting boundary test** is the model for this phase's
  boundary test, rather than an AST scan that cannot see transitive
  writes

Candidate registry, hypothesis families, experiment engine, model
governance, production boundary and IBKR safety were all inspected
directly and are unchanged. `audit_live_safety.py`: **16/16 PASS**.

---

## 1. EXECUTIVE SUMMARY

Phase 24 turns a Phase 23 candidate into a versioned challenger and
puts it through a harder comparison against a **named, versioned
baseline**.

The one candidate the research layer produced became one challenger,
was evaluated, and returned:

| | |
|---|---|
| out-of-sample effect | **+15.15%** directional accuracy |
| in-sample effect | +4.81% |
| bootstrap interval | [+0.22%, +29.39%] — excludes zero |
| slices favourable | 4 of 4 |
| walk-forward folds | **0 of 0 — unmeasurable** |
| complexity | 3× baseline |
| sample | 41 out-of-sample observations, against the baseline's 553 |
| **verdict** | **REQUIRES_REVIEW** |

It did not reach SUPERIOR, and the reason is the phase working: the
record spans 29 days, shorter than a single walk-forward window, so
`stability` could not be measured — and **an unmeasured dimension is
not a dimension that passed**.

That distinction was a real bug in the first implementation. `decide()`
counted only dimensions reading "worse", so the challenger reached
SUPERIOR while its reasons list printed *"met: stability — walk-forward
could not be run on this record"*. Three defects were found and fixed
during the build (§43).

A human then reviewed it and marked it `PAPER_CANDIDATE`, with a named
reviewer and a recorded reason. That is the furthest this phase reaches.

---

## 2. INITIAL STATE

Phase 23 left one candidate in `autoresearch_candidates` carrying its
hypothesis, conclusion, experiment, base version, change definition and
a review note. There was no notion of a challenger, a versioned
baseline, a comparison scorecard, a challenger queue or a review.

---

## 3. CANDIDATE REGISTRY

Audited, not rebuilt. `api.candidates()` reads Phase 23's registry and
annotates each row with `qualifies` and `blocking_reasons`.
`validate_candidate` refuses candidates missing a hypothesis,
conclusion, experiment, base version, evaluator or measured effect.

---

## 4. CHALLENGER ARCHITECTURE

Six tables (`challengers`, `challenger_runs`, `challenger_results`,
`challenger_reviews`, `challenger_queue`, `challenger_audit`), 2,791
lines across six modules plus a 413-line CLI. Full architecture in
[`PHASE_24_CHALLENGERS.md`](PHASE_24_CHALLENGERS.md).

---

## 5. CHALLENGER TYPES

`SIGNAL` runs end to end. Seven more are declared and blind, each with
a stated reason: MODEL (no promoted model), REGIME (`market_regime` is
NULL throughout), PORTFOLIO/RISK (no position exists), EXECUTION (no
order has been placed), FEATURE/STRATEGY (the evaluators are
signal-level). Declaring them keeps the gap named rather than missing.

---

## 6. BASELINE

Every challenger declares a versioned baseline, resolved through Phase
22's own registry — a baseline invented for one comparison is one
chosen to flatter it. `BaselineSpec.validate()` refuses an unversioned
baseline, and the version is inside the fingerprint, so a moved
baseline is a different challenger.

Because no model has been promoted, `active_model_baseline()` returns
None — measured through Phase 18's `select(ACTIVE_ONLY)`, not assumed —
and every challenger is flagged `experimental_basis` with a note saying
it competes against a signal rule rather than a validated model.

---

## 7. CANDIDATE LINKAGE

`validate()` requires `candidate_id`, `hypothesis_id`, `experiment_id`
and `conclusion_id`. `integrity_check` reports zero challengers without
a candidate. `api.lineage()` resolves the whole chain as records.

---

## 8. VERSIONING

Primary key `(challenger_id, version)`. Ten versions recorded:
candidate, challenger, baseline, dataset cutoff, dataset, feature,
label, model, strategy, code. The signal-cohort case genuinely has no
feature or model version; those columns exist and stay blank rather
than being filled with something invented.

---

## 9. IMMUTABILITY

A started challenger refuses a changed definition at the write.
`new_version()` is the supported path and keeps the previous version's
results. Tested for the plan and for the baseline separately.

---

## 10–17. CHALLENGER TYPES IN DETAIL

**Signal challengers** record the rule, its parameters, the cohort
condition and the complexity delta — the one type fully implemented.

**Model, feature, strategy, regime, portfolio, risk and execution
challengers** are declared with their required inputs and refuse
rather than returning an empty comparison. Risk challengers are
research-only by construction: nothing in the package can write a risk
limit, and the boundary test proves it by counting rows.

---

## 18. BACKTEST

Phase 12's `BacktestEngine` is the executor for strategy challengers;
there is no second backtester. Signal challengers do not need it — they
compare cohort metrics over Phase 21 experiences, which is what the
registered evaluators measure.

---

## 19. WALK-FORWARD

Delegated to Phase 9's `WalkForwardSplitter` through Phase 22, purge
and embargo included. **It generates zero windows on this record**,
because 29 days is shorter than one window. That is reported as a
limitation and blocks SUPERIOR rather than passing silently.

---

## 20. OOS

The reported effect is always out-of-sample; the in-sample effect is
kept beside it so the gap is visible. On the live challenger the gap is
**−10.35%** — the out-of-sample effect is *larger* than the in-sample
one, which is unusual and worth a reviewer's attention rather than
celebration.

---

## 21. PROTECTED TEST

Phase 23's protected windows are honoured: an evaluation whose held-out
half overlaps a protected region fails with the reason and writes no
result. Tested.

---

## 22. MULTIPLE TESTING

`family_challenger_count` and `family_run_count` travel with every
verdict, and `assert_family_budget` refuses more than eight challengers
in one family. The evidence dimension degrades once a family exceeds
three attempts.

---

## 23. OVERFITTING

Phase 23's eight shapes, reused rather than reimplemented. The live
result carries `narrow_time_period` and `single_regime`.

---

## 24. ROBUSTNESS

Time slices, instrument slices and horizon slices, each kept
individually. Groups below 30 observations are skipped rather than
reported — a 4-observation instrument slice that happens to favour the
challenger is noise, and including it could label a genuinely global
result context-dependent by accident.

---

## 25. SENSITIVITY

Numeric parameters get a five-point sweep classified as `plateau`,
`single_point` or `no_effect`. A categorical cohort returns
`not_applicable` rather than a fabricated surface — the live
challenger's `event_type=acquisition` has no neighbours, and inventing
some would produce a plateau that means nothing.

---

## 26. COMPLEXITY

Measured against the baseline: 3× on the live challenger, within the 5×
ceiling. Above the ceiling forces REQUIRES_REVIEW.

---

## 27. ECONOMIC SIGNIFICANCE

Phase 22's signed test, reused. A statistically clear but economically
trivial win forces REQUIRES_REVIEW rather than SUPERIOR.

---

## 28. CONTEXT DEPENDENCE

`CONTEXT_DEPENDENT` is reached **before** any global verdict when
measured slices disagree between 25% and 75%. The slices are stored
individually and shown individually; nothing averages them. On the live
challenger all four measured contexts favour it, so the verdict is not
context-dependent — which is itself a finding.

---

## 29. CHALLENGER COMPARISON

`comparison()` lays the two arms side by side across the §21 metrics,
the six scorecard dimensions and every context, and **does not rank**.
The live comparison is worth reading in full because it is not
flattering: the challenger wins directional accuracy by 15 points and
has **41 observations against the baseline's 553**, and **27
instruments against 150**.

---

## 30. CHALLENGER DECISIONS

Five states. `SUPERIOR` requires every dimension measured and
favourable, the interval to exclude zero, the sample to clear 30, the
complexity ceiling to hold and no economically-meaningless win.

---

## 31. NEGATIVE KNOWLEDGE

No DELETE statement targets any challenger table. Rejected challengers
stay listed, and the Lab counts `rejected` and `not_superior` beside
`superior`. There is no `promising_only` parameter on any API.

---

## 32. MEMORY INTEGRATION

Challenger results reach memory the way Phase 23 established: through
the structured research pathway, never by mutating Phase 21's raw
record. The boundary test confirms `trading_experiences` is unchanged
by a full cycle.

---

## 33. ERROR INTEGRATION

Where a candidate came from a recurring error, the linkage survives:
challenger → candidate → conclusion → hypothesis → error attribution,
resolved by `api.lineage()`.

---

## 34. OUTCOME INTEGRATION

Phase 19's outcomes reach the comparison through Phase 21 experiences
and Phase 22 evaluators. No duplicate outcome definition exists.

---

## 35. PAPER CANDIDATE

Reached only through `review()` with a named reviewer and a reason.
Refused for a challenger never evaluated, and refused for one the
latest comparison found INFERIOR.
`paper_candidates_without_a_review` reports zero.

---

## 36. IBKR PAPER SAFETY

Nothing is sent anywhere. `PAPER_CANDIDATE` is a label; the package
contains no IBKR import, no order path and no reference to any other
broker. Phase 25 may act on the label, under its own human control.

---

## 37. FRONTEND

The **Challengeri** workspace shows candidates and whether each
qualifies, every challenger including rejected ones, the human reviews,
and a permanent statement that a research result is not a production
approval.

The detail page shows the change, the versioned baseline, the full
lineage, the side-by-side metrics, the six-dimension scorecard, every
context, the sensitivity shape, every reason and every limitation.

Verified in a browser: **all 21 views render, zero console errors.**

---

## 38. API

`challengers`, `create`, `detail`, `run`, `cancel`, `runs`, `results`,
`comparison`, `queue`, `candidates`, `review`, `paper_candidates`,
`integrity_check`, `observability` — the §66 routes in the project's
established connection-passing style.

---

## 39. DATABASE

Six new tables, ten indexes, an additive column migration. No candidate,
experiment, model, strategy or backtest table was duplicated (§67).

---

## 40. AUDIT TRAIL

Every action records actor, action, decision, reason and timestamp. A
review is recorded as `human`; everything else as `system`. Reviews are
append-only.

---

## 41. SECURITY

No credentials, no environment access, no shell, no `eval`/`exec`/
`compile`/`__import__`, no destructive SQL, no second broker, no live
order path. Scans read **code only** — comments and string literals
tokenised away — after a bare `environ` matched the legitimate
identifier `RunEnvironment`.

---

## 42. PERFORMANCE

| Stage | Time |
|---|---|
| challenger creation | <0.1s |
| evaluation over 4,311 rows | 0.30s |
| comparison assembly | <0.1s |

No bottleneck worth optimising; **no change made**, because there is no
evidence supporting one.

---

## 43. REPAIRS MADE DURING THE BUILD

**43.1 — An unmeasured dimension counted as passed.** `decide()`
counted only dimensions reading "worse", so the live challenger reached
SUPERIOR while its stability had never been measured, printing *"met:
stability — walk-forward could not be run"*. Fixed with
`Scorecard.unknown()`; an unmeasured dimension now forces
REQUIRES_REVIEW. Regression test:
`test_an_unmeasured_dimension_blocks_superior`.

**43.2 — A malformed shim silently produced zero folds.** The
walk-forward call passed a namespace carrying only two attributes;
Phase 9's splitter needs six, so it raised `AttributeError` — which a
bare `except Exception` converted into "0 folds". Fixed by building a
real `EvaluationProtocol` and removing the broad except. The record is
genuinely too short for a window, and that is now *reported* rather
than *assumed*.

**43.3 — A missing model table crashed challenger creation.**
`active_model_baseline` let `sqlite3.OperationalError` escape on a
database without `trained_models`. Narrowed to "no such table" — which
means no promoted model, the same answer — and left every other
OperationalError raising, per the Phase 23.5 discipline.

---

## 44. IMPROVEMENTS MADE

| Change | Reason | Risk |
|---|---|---|
| slices below 30 observations are skipped | a 4-row slice favouring the challenger could label a global result context-dependent | none — fewer, better slices |
| credential scan uses precise tokens | `environ` matched `RunEnvironment`; a scan that fires on correct code gets switched off | none |

---

## 45. TEST RESULTS

**103 new tests, all passing.** Full suite **3,689, OK, 1 skipped**
(490s), up from 3,586.

| File | Tests |
|---|---|
| `tests/challengers/test_challenger_lifecycle.py` | 57 |
| `tests/challengers/test_boundary_and_adversarial.py` | 33 |
| `tests/test_dashboard_challengers.py` | 13 |

---

## 46. ADVERSARIAL TESTING

All sixteen §78 cases have a named test, including the ones that
already cost this project real debugging: future data, protected-test
reuse, a definition changing mid-evaluation, a cache hiding new data, a
duplicate run, a concurrent race, a worker crash, and an evaluation
that produced nothing being read as a verdict.

---

## 47. LEAKAGE TESTING

The cohort is ordered by `available_at` and bounded by the dataset
cutoff; no row past the cutoff enters. Protected windows block an
overlapping evaluation. Verified.

---

## 48. FILES CREATED

`src/domain/challenger_models.py`, `src/data_access/challenger_schema.py`,
`src/challengers/{__init__,registry,evaluation,workflow,api}.py`,
`scripts/run_challenger.py`,
`tests/challengers/{__init__,test_challenger_lifecycle,test_boundary_and_adversarial}.py`,
`tests/test_dashboard_challengers.py`,
`docs/PHASE_24_CHALLENGERS.md`, `docs/PHASE_24_FINAL_REPORT.md`.

## 49. FILES MODIFIED

`src/dashboard.py` (collector, Lab, detail page, nav).

## 50. MIGRATIONS

`_add_missing_columns` adds `economic_note`, `window_reuse_count` and
`experimental_basis` to pre-existing tables. Additive and defaulted.

---

## 51. REMAINING ISSUES

**CRITICAL / HIGH** — none.

**MEDIUM**

1. **29 days of record makes SUPERIOR unreachable.** Walk-forward
   generates zero windows, so `stability` is unmeasurable and every
   challenger lands in REQUIRES_REVIEW. Time is the only fix; this is
   the correct behaviour, not a defect.
2. **Pipeline stages 13, 14 and 15 have still never run under
   automation**, so production carries no memory, experiment, research
   or challenger tables.

**LOW**

3. **Seven of eight challenger types are declared and blind** — each
   for a stated infrastructure reason.
4. **No strategy challenger has exercised the Phase 12 backtester**;
   the integration exists and is untested end to end because no
   strategy candidate has been produced.
5. **Sensitivity is `not_applicable` for categorical cohorts**, which
   is correct but means the plateau/spike distinction is currently
   unexercised on real data.

---

## 52. FUTURE PAPER TRAINING READINESS

One `PAPER_CANDIDATE` exists, carrying its reviewer, reason, versioned
baseline, full lineage and every limitation. Phase 25 inherits a label
and a record — not an instruction, and not an execution path.

## 53. FUTURE AUTONOMOUS LEARNING READINESS

The structured evidence §43 asks for exists: baseline, challenger,
difference, conditions, contexts, warnings and robustness, all stored
per run. No learning is implemented.

---

## 54. NEXT PHASE

Phase 25 — IBKR Paper Training & Controlled Strategy Validation. The
binding constraint is unchanged and is not Phase 25's to fix by writing
code: **the record is too short for a held-out half to be worth holding
out**, and until it grows, every comparison this system makes will end
in REQUIRES_REVIEW for an honest reason.

---

# READY FOR PHASE 25

Phase 24 built the bridge from research candidate to reviewed paper
candidate, and used it once end to end.

The single challenger it produced won on effect, interval, robustness
and complexity — and **did not reach SUPERIOR**, because a 29-day
record cannot support a walk-forward fold and an unmeasured dimension
is not a dimension that passed. A person reviewed it, recorded why, and
marked it a paper candidate.

Three defects were found and fixed during the build, the most important
being a gate that counted an unmeasured dimension as met.

Nothing in production changed. No model, strategy, threshold, risk
limit or capital figure was touched; no Phase 6/19/20/21/22 table was
written; Interactive Brokers remains the only broker; live trading
stays disabled; and there is no state, outcome or code path in this
phase that reaches production.

A challenger is successful only when the evidence supports the change.
This one has promising evidence and a hole in it, and the system said
so rather than rounding up.
