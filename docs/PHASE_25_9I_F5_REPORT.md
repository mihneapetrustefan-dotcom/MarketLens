# Phase 25.9I-F.5 — Completion Report

## 1. Phase status

**COMPLETE.**

Every instrument now carries a reason instead of a blank. The headline finding
is not flattering and is not meant to be: **of 244 instruments, the system
currently makes zero decisions.** All 244 are blocked at one place — no
qualified model exists — and that is the truth the phase was asked to surface,
not a number to be improved by moving a threshold.

---

## 2. Git state

| | |
|---|---|
| Branch | `ibkr-paper-validation-fixes` |
| Baseline HEAD | `3fb21c9` (Phase 25.9I-F completion report) |
| Final HEAD | `128e9b5` |
| Working tree at start | clean |
| Commits | 1 |
| **Modified files** | **0** |
| Diff | **11 files changed, 4887 insertions(+), 0 deletions(-)** |

Purely additive. 10 new files:

| File | Purpose |
|---|---|
| `src/domain/signal_availability_models.py` | the no-signal taxonomy, horizon, target and coverage contracts |
| `src/adaptive/horizon_inventory.py` | horizon inventory from evidence; the structural seal |
| `src/adaptive/model_inventory.py` | model inventory; reuses the canonical gate |
| `src/adaptive/horizon_intelligence.py` | preferred horizon, or None |
| `src/adaptive/target_estimation.py` | target zone, or a reasoned refusal |
| `src/adaptive/signal_availability.py` | the instrument-first funnel and coverage |
| `scripts/signal_horizon_status.py` | read-only diagnostic + scenarios |
| `tests/adaptive/test_signal_availability.py` | 62 tests |
| `tests/adaptive/test_signal_boundary.py` | 40 tests |
| `docs/PHASE_25_9I_F5_ARCHITECTURE.md`, `docs/PHASE_25_9I_F5_REPORT.md` | documentation |

---

## 3. Baseline

Recorded **before** any change, at `3fb21c9`:

```
PYTHONPATH=src python -m unittest discover -s tests -t . -b
Ran 4960 tests in 424.445s
OK (skipped=1)
```

| tests | pass | fail | errors | skipped | runtime |
|---|---|---|---|---|---|
| 4960 | 4959 | 0 | 0 | 1 | 424.4 s |

---

## 4. Repository findings

| Area | Found | Verdict |
|---|---|---|
| Signal engine | `src/signals/engine.py`, candidate-driven, with `GenerationReport.is_balanced` | reused |
| Suppression taxonomy | `SuppressionReason`, 11 members | **reused and mapped**, not duplicated |
| Direction semantics | `SignalDirection` — `NEUTRAL` and `NO_SIGNAL` already distinct | reused |
| Model registry | `trained_models`, `model_evaluations`, `model_baseline_comparisons` | reused |
| Qualification gate | **`ModelEvaluation.is_deployable`** — beats every baseline AND effective sample ≥ 30 | **reused, never reimplemented** |
| Promotion | `src/modeling/promotion.py` | not imported |
| Confidence formula | `src/signals/strategy.py`, multiplicative | reused, unchanged |
| Uncertainty | `Prediction.prediction_interval_low/high`, `uncertainty_basis`, `is_abstention` | reused |
| Expected return | `Prediction.predicted_value`; `signals.expected_return` | reused |
| Label horizons | `research_labels.window_name` — **9 horizons** | derived, not assumed |
| Target model | **none exists** | — |
| Horizon model | **none exists** | — |

### The structural gap

The pipeline is candidate-driven, so an instrument that never produced a
prediction never reaches the engine and **never acquires a reason**. It is not
suppressed; it is absent. Measured: **4 of 244 instruments have ever produced a
prediction.** This phase inverts the traversal to start from the instrument.

### The trap

`ImpactProfile.dimensions.direction` looked usable but is derived from abnormal
returns; separately, `src/impact` is not imported here at all.

---

## 5. Current horizon inventory

From `research_labels` (9,192 rows) cross-referenced against `trained_models`:

| Horizon | Labels | Models | Qualified | Data sufficiency | Protection |
|---|---|---|---|---|---|
| intraday_5m | 554 | 0 | 0 | ample | open |
| intraday_15m | 554 | 0 | 0 | ample | open |
| intraday_30m | 554 | 0 | 0 | ample | open |
| intraday_60m | 554 | 0 | 0 | ample | open |
| d1 | 544 | 0 | 0 | ample | open |
| d3 | 531 | 0 | 0 | ample | open |
| **d5** | 458 | **1** | **0** | ample | open |
| d10 | 286 | 0 | 0 | adequate | open |
| **d20** | **7** | 0 | 0 | **1.2% matured** | **SEALED** |

**Signal-capable horizons: NONE.**

d20 readiness metadata only: 7 of 581 observations have a matured label. No
outcome statistic is exposed, for this or any horizon.

---

## 6. Why the dashboard showed NO SIGNAL

**This is the phase's key output.**

```
TOTAL instruments evaluated: 244
TOTAL with a signal:           0

valid no edge (a decision):    0
no model available:            0
MODEL NOT QUALIFIED:         244   <-- every single one
missing data:                  0
stale:                         0
unsupported horizon:           0
protected:                     0
error:                         0
```

**Decisions the system actually made: 0 of 244.**

Not one blank cell represented a finding about the market. Every one meant
*"I cannot make a scientific decision."*

### The single root cause

One model specification exists (`ridge_abnormal_return:v1`, d5). It fails
**three** independent gates:

| Gate | Result |
|---|---|
| `SAMPLE_TOO_SMALL` | effective sample **4**, floor 30 |
| `BASELINE_NOT_BEATEN` | loses on MAE to historical-mean **and** majority-class |
| `MODEL_NOT_ACTIVE` | status `evaluated`, never promoted |

R² = **−0.197** — worse than predicting the mean.

---

## 7. Coverage funnel

Real numbers, computed from the corpus:

```
   244  universe
   244  has research observations
   244  has enough history
   244  has computed features
   244  has at least one unsealed horizon
   244  has a supported horizon
   244  has a model for some horizon
     0  that model is qualified            -244 model_not_qualified
     0  its prediction is fresh
     0  its horizons do not conflict
     0  uncertainty is acceptable
     0  quality gates pass
     0  confidence is adequate
     0  edge clears the threshold
     0  edge survives costs
     0  actual signals
```

The funnel does not narrow gradually. It falls off a cliff at one step.
**Data is not the problem. Features are not the problem. The model is.**

---

## 8. What was implemented

No-signal taxonomy (19 reasons, 5 classes, explicit ordering) · valid-decision
vs capability-gap separation · horizon inventory from evidence · per-instrument
per-horizon status · model inventory with gate failures · horizon intelligence
with disagreement handling · target-estimation foundation with refusal ·
decomposed confidence/uncertainty reporting · coverage metrics that reconcile ·
a derived funnel · the read-only CLI · 102 tests · documentation.

**Not implemented, deliberately:** no persistence, no feature flag, no research
run, no threshold change, no promotion. See §11 and architecture §J.

---

## 9. What worked

- **Every instrument now has a reason and an explanation.** The contract
  *refuses* a blank explanation, so a silent blank is unrepresentable.
- **Coverage reconciles exactly**: 244 by class, 244 by reason, verified.
- **Nine horizons found**, four of them intraday — more than a hard-coded guess
  would have contained.
- **The ~0.30 confidence question is answered arithmetically** (§16).
- **The canonical gate produced the right verdict** once queried correctly:
  FAILED on three gates.
- **Scenario H proves a target *can* be produced** when a validated estimate is
  supplied: 100.0 × (1 + 0.023) ± uncertainty → **[101.2, 103.4]**, with model
  lineage.
- **Scenario G proves it is refused** without one.
- **The seal held** under every probe: no value column named, guard raises,
  contracts refuse a protected preference or expected return.

---

## 10. What did not work

**Mandatory. Nothing here is hidden because it was later fixed.**

### 10.1 The funnel ordering was wrong, and it mislabelled four instruments
**Symptom:** four crypto instruments reported `stale_data`; 240 reported
`model_not_qualified`.
**Root cause:** `STALE_DATA` was ordered **before** `MODEL_NOT_QUALIFIED`,
contradicting the funnel documented in the same module. A prediction can only be
stale if a model made one, so an unqualified model is the earlier and more
fundamental blocker. The reason named the wrong repair — someone would have gone
to re-run a pipeline when the model had never been qualified.
**Fix:** staleness moved after the model checks; suppressions are now *weighed
against* horizon-level blockers rather than short-circuiting them.
**Status:** resolved. All 244 now report the single true root cause, and a
regression test covers it.

### 10.2 The model inventory reported UNJUDGED for a model that plainly failed
**Symptom:** verdict `unjudged` / `NO_BASELINE_COMPARISON`, while the stored
row said `beats_all_baselines = 0`.
**Root cause:** I reconstructed `BaselineComparison` with invented field names
(`baseline_family`, `metric`, `baseline_value`, `model_value`) instead of the
real ones (`baseline_name`, `metric_name`, `baseline_score`, `model_score`), and
called a non-existent `model_wins` instead of `beats_baseline`. The comparisons
silently came back empty, so the canonical gate correctly answered "cannot
judge".
**Fix:** corrected to the real schema and contract.
**Status:** resolved — and instructive: the gate was right, my reconstruction
was wrong, and the failure mode was a *softer* verdict than the truth.

### 10.3 `effective_sample_size` is a derived property, not a field
**Symptom:** `TypeError` constructing `ModelEvaluation`.
**Root cause:** assumed a constructor field; it is computed as
`cluster_count or sample_size`.
**Fix:** stopped passing it.
**Status:** resolved.

### 10.4 My own code restated the frozen hypothesis
**Symptom:** the D20 boundary test failed on `horizon_inventory.py`.
**Root cause:** a docstring named the protected feature and relation while
explaining what was sealed. Documenting the seal by reciting its contents puts
the frozen specification somewhere it can be read or edited.
**Fix:** the note now says the horizon is sealed without saying what it tests.
**Status:** resolved. **The test caught my own leak**, which is what it was for.

### 10.5 The seal test was too blunt to distinguish use from mention
**Symptom:** a comment reading "the value column is never selected" tripped the
test looking for that column name.
**Root cause:** the test grepped raw source, so an explanatory mention failed
identically to an actual read.
**Fix:** the guarantee is that no *query* names the column, so the comments were
reworded and the property is asserted against the module source without
forbidding the concept being discussed.
**Status:** resolved.

### 10.6 The real corpus can demonstrate only ONE no-signal category — UNRESOLVED
**Symptom:** §61 asks for real examples of several root causes. All 244 resolve
to `model_not_qualified`; all have observations and features.
**Root cause:** there is one model, and it fails.
**Fix:** none available. The other categories are shown as **fixtures, labelled
as fixtures** in `--scenario all`. Inventing real examples would be the exact
dishonesty this phase exists to remove.
**Status:** **OPEN**, and correctly so.

### 10.7 No horizon can produce a signal — UNRESOLVED
**Symptom:** `signal_capable` is empty.
**Root cause:** 8 of 9 horizons have never been modelled; the ninth failed.
**Fix:** not attempted. §51 says report weakness rather than manufacture
strength, and a new candidate trained on 4 effective clusters would inherit the
same defect.
**Status:** **OPEN.** This is the finding, not a bug.

---

## 11. What was deferred

| Deferred | Later owner |
|---|---|
| Model promotion | canonical governance (`src/modeling/promotion.py`) |
| Non-protected research for the 8 unmodelled horizons | research / autoresearch, once the sample problem is solved |
| Target calibration | a calibrated estimator |
| Horizon calibration | a horizon estimator |
| Dynamic partial-exit sizing | a sizing layer |
| Shadow outcome validation | **25.9I-G** |
| Protected D20 | remains sealed, permanently out of scope |
| Cost-aware net edge | needs a real expected return first |
| Dashboard UI change | the export contract exists; the UI change is documented, not made |

---

## 12. Real no-signal examples

Three real instruments, and the honest note that they share one cause:

```
bvb-brd      capability_gap  model_not_qualified  cannot decide
   ridge_abnormal_return:v1 exists for d5 but failed: SAMPLE_TOO_SMALL,
   BASELINE_NOT_BEATEN, MODEL_NOT_ACTIVE

crypto-ada   capability_gap  model_not_qualified  cannot decide
   (this one DID reach the signal engine and was suppressed for
    low_confidence, small_sample_evidence, stale_prediction -- but the
    earlier blocker is that its model was never qualified)

us_and_intl-aapl  capability_gap  model_not_qualified  cannot decide
```

> **§61 compliance:** the real corpus contains exactly one no-signal category.
> `NO_DATA`, `MISSING_FEATURES`, `STALE_DATA`, valid no-signal, horizon
> agreement and disagreement are demonstrated as **fixtures**, labelled as such
> in `--scenario all`. None is presented as a real observation.

---

## 13. Model inventory

| | |
|---|---|
| Trained rows | 2 |
| **Distinct specifications** | **1** |
| Evaluated | 2 |
| **Qualified** | **0** |
| **Promoted / active** | **0** |
| By horizon | d5: 2 · every other horizon: 0 |
| Family | `ridge_regression` |
| Task | `abnormal_return` |

---

## 14. Model gate failures

| Gate | Count | Detail |
|---|---|---|
| `SAMPLE_TOO_SMALL` | 2 | effective sample **4** vs floor **30** |
| `BASELINE_NOT_BEATEN` | 2 | lost to `baseline_historical_mean` and `baseline_majority_class` on MAE |
| `MODEL_NOT_ACTIVE` | 2 | status `evaluated`; never promoted |

Metrics: `mae 0.039314 · rmse 0.048282 · r_squared −0.197236 ·
directional_accuracy 0.6`. Training note, already on the record:
*"320 rows from only 4 clusters — effective sample is far smaller than it
appears."*

---

## 15. Signal coverage

| | |
|---|---|
| Overall | 0 signals / 244 instruments |
| By reason | `model_not_qualified`: 244 |
| By class | `capability_gap`: 244 |
| By horizon | d5 → `no_qualified_model` ×244; d20 → `protected` ×244; all others → `no_qualified_model` ×244 |
| By model | `ridge_abnormal_return:v1` → 0 usable outputs |

Coverage **reconciles** exactly. Reported as a diagnostic, not a score.

---

## 16. Confidence

**The ~0.30 cluster is explained, and it is structural.**

```
confidence = base × quality × agreement × sample

0.5 × 1.0 × 0.6 × 1.0 = 0.30   historical cluster
0.5 × 1.0 × 0.6 × 0.5 = 0.15   the five signals in the corpus today
```

- **base 0.5** — the model reports no confidence of its own; 0.5 rather than 1.0
  because unknown is not certain.
- **agreement 0.6** — `INSUFFICIENT_EVIDENCE`: one model is not corroboration.
- **sample 0.5** — the effective sample is small.

**Verdict: intended, not a bug.** A previous phase already found and fixed the
real defect here — counting walk-forward refits of one specification as
agreement lifted the multiplier to 1.0 and confidence to 0.50, clearing a 0.40
floor. *A signal made tradeable by nothing more than having been predicted more
times.* Corroboration is now counted in specifications, not rows.

**Nothing was inflated.** All five signals in the corpus sit at exactly 0.15 and
are suppressed.

---

## 17. Uncertainty

Measurable today: `Prediction.prediction_interval_low/high`,
`uncertainty_basis`, `is_abstention`/`abstention_reason`, and
`ModelEvaluation.small_sample`. **None is populated** for the one model.

`EventAssessmentConfidence`-style decomposition is applied here too: four
confidence axes, never averaged — `overall()` returns the **weakest known axis**,
because the weakest axis is the one that will be wrong. Unmeasured stays None.

---

## 18. Horizon intelligence examples

```
AGREEMENT (fixture)      d1 long 0.55, d5 long 0.68
                         -> preferred d5, alternatives (d1), agreement

DISAGREEMENT (fixture)   d1 short 0.60, d5 long 0.71
                         -> preferred NONE, agreement=disagreement
                         "usable horizons point in opposite directions --
                          this is reported, not voted on"

3-vs-1 majority (test)   d1 long, d3 long, d5 short 0.9
                         -> preferred NONE (majority does not decide)

NO USABLE (real)         every horizon no_qualified_model or protected
                         -> preferred NONE, insufficient_evidence
```

---

## 19. Target intelligence

**Instruments that can currently receive a defensible target: ZERO.**

Because zero models are qualified, so no calibrated expected return exists
anywhere in the repository. No target was invented for any instrument.

With a validated estimate supplied externally (fixture H): reference 100.0,
expected return +0.023, standard error 0.011 → zone **[101.2, 103.4]**, carrying
`model_qualified_id`, horizon and method. Deterministic across runs.

---

## 20. Event → target/horizon request (25.9I-F integration)

25.9I-F emits `target_reestimation_required` and
`horizon_reestimation_required`. F.5 can now answer both — currently with a
reasoned refusal, which is a complete answer:

```
target_reestimation_required  -> answered: False
                                 target_status: unavailable
                                 target_zone: None
                                 reason: "no qualified model produces a
                                 calibrated expected return"

horizon_reestimation_required -> answered: False
                                 preferred_horizon: None
                                 note: "no horizon extension is proposed; a
                                 duration must come from a qualified estimator
                                 or not at all"
```

`TargetEstimator.estimate` takes **no event parameter** — asserted by
introspection test. Event materiality can request; it can never determine.

---

## 21. Dashboard / export result

Before — one string for six situations:

```
AAPL   NO SIGNAL
SPY    NO SIGNAL
ADA    NO SIGNAL
```

After — `python scripts/signal_horizon_status.py`:

```
us_and_intl-aapl  capability_gap  model_not_qualified  pref=none  cannot decide
    ridge_abnormal_return:v1 exists for d5 but failed: SAMPLE_TOO_SMALL, ...
crypto-ada        capability_gap  model_not_qualified  pref=none  cannot decide
d20 horizon                        PROTECTED / SEALED  (never "NO SIGNAL")
```

The export carries per-instrument: availability class, reason, explanation,
direction, confidence, uncertainty, expected return, preferred horizon, every
horizon's status, model id, model status, gate failures, missing inputs, data
as-of and lineage. No credential or broker detail.

---

## 22. As-of / leakage

| Bound | How |
|---|---|
| Observations | SQL `information_time <= as_of` |
| Features | SQL `as_of <= as_of` |
| Predictions | SQL `predicted_at <= as_of` |
| Model evaluation | a later evaluation cannot retroactively qualify an earlier model — the verdict is computed from rows visible at `as_of` |
| Label maturity | counted, never valued |
| Protected isolation | the value column is never named in any query, for any horizon |
| Physical truncation | an earlier `as_of` returns strictly fewer observations; repeated reads at the same `as_of` are identical |
| Determinism | identical inputs → identical verdict dict; horizon order does not change the outcome |

---

## 23. D20 status

| | |
|---|---|
| Executed? | **NO** |
| Inspected? | **NO** |
| Inferred? | **NO** |
| Consumed? | **NO** |
| Hypothesis changed? | **NO** |
| anchor-v2 changed? | **NO** |
| Protected window changed? | **NO** |

`scripts/validate_d20_reversal.py` was not run. No module imports `src.research`
or names the protected feature, relation, ledger or script. The seal is
structural: no query names a label value column; `guard_protected` raises; a
protected horizon cannot carry an expected return, be preferred, or produce a
signal even with a qualified model. **A test caught my own docstring leaking the
hypothesis** (§10.4) and it was removed.

---

## 24. Production behaviour

| Path | Changed? |
|---|---|
| Portfolio | **No** |
| Risk | **No** |
| OrderIntent | **No** |
| Execution | **No** |
| Capture | **No** |
| IBKR | **No** |
| Subscriptions | **No** |
| Signal engine / thresholds / model registry | **No** |

**Zero files modified.**

---

## 25. Broker safety

| | |
|---|---|
| Broker writes attempted | **0** |
| Broker writes succeeded | **0** |
| Paper orders | **0** |
| Live orders | **0** |
| Models promoted | **0** |
| Thresholds changed | **0** |

---

## 26. Test results

| Suite | Tests | Result | Runtime |
|---|---|---|---|
| **F.5** `test_signal_availability` | 62 | **OK** | 0.04 s |
| **F.5** `test_signal_boundary` | 40 | **OK** | 6.40 s |
| **F.5 total** | **102** | **OK** | — |
| Signals | 129 | **OK** | 7.73 s |
| Modeling | 142 | **OK** | 7.41 s |
| Adaptive suite (incl. I-B…I-F) | 718 | **OK** | 35.7 s |
| Portfolio | 273 | **OK** | 1.39 s |
| Risk engine | 39 | **OK** | 0.02 s |
| Research integrity | 39 | **OK** | 21.7 s |
| Trading readiness | 44 | **OK** | 4.67 s |
| Live safety | 57 | **OK** | 0.03 s |
| **FULL SUITE** | **5062** | **OK (skipped=1)** | **1214.9 s** |

```
Ran 5062 tests in 1214.946s
OK (skipped=1)
```

**5062 − 4960 = 102**, exactly the new tests. Zero regressions.

---

## 27. Performance

| Operation | Time |
|---|---|
| Model inventory (2 rows) | 2.98 ms |
| Horizon inventory (9 horizons) | 23.17 ms |
| Collect inputs (244 instruments) | 264.3 ms |
| Assess 1 instrument × 9 horizons | 0.38 ms |
| Assess 31 instruments | 18.6 ms |
| Assess 130 instruments | 34.3 ms |
| Assess all 244 instruments | 57.6 ms |
| Coverage funnel (244 verdicts) | 5.16 ms |
| Status export (244 describes) | 16.0 ms |

---

## 28. Scope-creep review

| Check | Result |
|---|---|
| Unrelated files | none — **0 files modified** |
| Model promotion | none |
| Execution / capture / IBKR changes | none |
| Protected research | untouched |
| **Threshold changes** | **none** — asserted by test against the signal engine's constants |
| New persistence | **none** — nothing here is a fact about history |
| New feature flag | **none** — the layer is read-only |
| New external dependencies | **none** (stdlib only) |

---

## 29. Known limitations

1. **Zero qualified models.** One specification, three gate failures, R² −0.197.
2. **Zero signal-capable horizons.** 8 of 9 never modelled; the 9th failed.
3. **Effective sample 4 against a floor of 30** — the binding constraint, and a
   data problem more than a modelling one.
4. **d20 label maturity is 1.2%** (7 of 581). Even unsealed it could not support
   a model.
5. **No calibration exists** for the one model, so no target can be defended.
6. **No uncertainty is populated** on any prediction.
7. **Confidence is structurally capped at 0.30** while one specification
   provides all evidence — raising it requires genuinely independent models.
8. **The real corpus demonstrates one no-signal category**; the rest are
   labelled fixtures.
9. **Cost-aware net edge is unreachable** — it needs an expected return first.
10. **Regime support is not represented** — no regime-conditional evaluation
    exists to read.
11. **The dashboard UI itself was not changed**; the export contract is in
    place and the UI change is documented.
12. **Nothing is wired into production.**

---

## 30. Readiness for 25.9I-G

**PARTIAL.**

What is ready: an honest denominator. Attribution can now ask "of the
instruments we *could* have decided about, how did we do" instead of dividing by
a universe the system never evaluated. Coverage reconciles, the funnel is
derived from data, and every verdict carries lineage.

What is missing: **there is nothing to attribute.** Zero signals, zero
decisions, zero qualified models. Shadow validation needs outputs to validate,
and the honest reading of this phase is that the modelling layer must come
first.

## 31. Readiness for 25.9I-H

**NO.** Zero qualified models, zero signal-capable horizons, no calibrated
expected return, no defensible target, and realtime market data still
operationally unverified from 25.9I-C. Every gate is unsatisfied.

---

## 32. Final verdict

> **PHASE 25.9I-F.5 = COMPLETE**
>
> 10 new files, **0 modified**, 102 new tests, full suite **5062 passing**
> against a **4960** baseline — no regressions. Generic `NO SIGNAL` is
> decomposed into a 19-reason taxonomy across 5 classes, with a decision
> distinguished structurally from a capability gap. Nine horizons were derived
> from evidence rather than assumed; the sealed horizon is represented as
> PROTECTED and its outcome is unreachable by construction. The canonical
> qualification gate was reused and not weakened, and it reports three failures
> for the single model that exists. The ~0.30 confidence cluster is explained
> arithmetically as intended structure.
>
> **The headline result is unflattering and correct: of 244 instruments, the
> system makes 0 decisions, and all 244 are blocked at the same place — no
> qualified model. Data is not the problem; features are not the problem; the
> model is.** No threshold was moved to improve that number.

---

*No broker order was created. No model was promoted. No threshold was changed.
No signal was manufactured. The protected D20 experiment was not touched.*
