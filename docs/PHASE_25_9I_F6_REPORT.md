# Phase 25.9I-F.6 — Completion Report

> **A MODEL IS NOT QUALIFIED BECAUSE WE NEED ONE.**
> **FAILURE TO FIND EDGE IS A VALID RESEARCH RESULT.**

Written 2026-10-01. Architecture: `docs/PHASE_25_9I_F6_ARCHITECTURE.md`.
Pre-registration: `docs/PHASE_25_9I_F6_PREREGISTRATION.md`, committed at
`ed016e8` **before** any candidate was run.

---

## 1. Phase implementation status

**COMPLETE.**

Every deliverable was built, tested and committed: sample diagnostics, the
multi-horizon harness, the pre-registration, 57 new tests, and this
documentation. The harness ran against all four research-ready horizons and
returned a verdict for each.

---

## 2. Model qualification outcome

**NONE QUALIFIED.**

More precisely, and this distinction matters: **INSUFFICIENT DATA on all four
daily horizons.** Zero candidates were fitted, because zero legal walk-forward
folds could be formed. Nothing was tested and found wanting — nothing could be
tested at all.

**This is not evidence that no edge exists.** The question was never asked.

---

## 3. Git state

| | |
|---|---|
| Branch | `ibkr-paper-validation-fixes` |
| Baseline HEAD | `6762edd` (Phase 25.9I-F.5 completion report) |
| Final HEAD | `1be725d` |
| Working tree at start | clean |
| Commits | **2** |
| Diff | **5 files changed, 1517 insertions(+), 0 deletions(-)** |
| **Modified files** | **0** |

Purely additive. Nothing was changed, only added.

| Commit | Content |
|---|---|
| `ed016e8` | pre-registration only — committed **before** any candidate was run |
| `1be725d` | the diagnostic module, the harness and 57 tests |

### New files

| File | Lines | Purpose |
|---|---|---|
| `docs/PHASE_25_9I_F6_PREREGISTRATION.md` | 187 | every choice frozen in advance |
| `src/adaptive/sample_adequacy.py` | 376 | effective sample from current evidence |
| `scripts/research_multi_horizon.py` | 372 | the harness; read-only, opens no holdout |
| `tests/adaptive/test_sample_adequacy.py` | 230 | 24 tests |
| `tests/adaptive/test_research_discipline.py` | 352 | 33 tests |

Documentation added after the research run (this report and the architecture
document) changes no code and no result.

---

## 4. Baseline

Recorded **before** any F.6 change, at `6762edd`:

```
PYTHONPATH=src python -m unittest discover -s tests -t . -b
Ran 5062 tests in 1065.874s
OK (skipped=1)
```

| | |
|---|---|
| Tests | **5062** |
| Pass | **5061** |
| Fail | **0** |
| Errors | **0** |
| Skipped | **1** |
| Runtime | **1065.874 s** |

No regressions existed before F.6 began.

A note on method, because it nearly went wrong: the first baseline attempt used
`pytest`, which **is not installed**, and the command returned **exit code 0**
while printing `No module named pytest`. A baseline taken from that exit code
alone would have been fiction. It was re-run with the project's
`unittest discover` convention.

---

## 5. Effective sample root cause

F.5 reported `effective sample = 4`. Here is exactly what that number was.

### The stored artefact

Read-only from `data/marketlens.db`:

| Field | Value |
|---|---|
| `trained_models.trained_model_id` | `tm-0be69458d6554731` |
| `trained_models.train_sample_size` | 320 |
| `trained_models.train_cluster_count` | **4** |
| `model_evaluations.window_label` | `single_chronological_split` |
| `model_evaluations.sample_size` | **5** |
| `model_evaluations.cluster_count` | **4** |
| `model_evaluations.effective_sample_size` | **4** |
| `evaluated_at` | **2026-08-29T10:27:44Z** |

### Cluster definition

`event_cluster_id` **equals** `instrument_id` for every row of
`research_observations` — 0 mismatches, asserted by a test. A cluster is an
instrument, so the effective sample answers *how many distinct companies is this
evidence about*.

`ModelEvaluation.effective_sample_size = cluster_count or sample_size`. It is a
derived property, not a stored input — during development I tried to pass it to
the constructor and it does not accept one.

### Independent clusters, actually

| Horizon | Raw rows | **Effective clusters** | Floor | Clears it |
|---|---|---|---|---|
| d1 | 544 | **236** | 30 | yes, 7.9× |
| d3 | 531 | **236** | 30 | yes, 7.9× |
| d5 | 458 | **223** | 30 | yes, 7.4× |
| d10 | 286 | **159** | 30 | yes, 5.3× |
| corpus total | 581 | **244** | — | — |

### Why raw rows did not count independently

They do not count independently and they never should. 320 training rows came
from **4** instruments — about 80 observations of each company's overlapping
forward windows. `effective_sample_of()` counts distinct clusters; duplicating a
cluster 50× cannot raise it, and the first test in the suite asserts that.

### The actual mistake

**The 4 was never a corpus property.** It was the cluster count of one test
window of one evaluation taken on 2026-08-29: five test rows spanning four
instruments. F.5's model inventory read the stored evaluation correctly and
reported the number it found. The error was in interpreting a historical
evaluation artefact as current dataset capacity.

Re-running the identical split today gives **32 test rows spanning 27
instruments** — labels matured as the calendar advanced. Nothing was added to the
corpus.

### What can legitimately increase the sample

| Does | Does not |
|---|---|
| observations about a new instrument | more rows about instruments already present |
| forward labels maturing on new instruments | re-running the same split |
| a genuinely wider universe | lowering the floor |

`SampleAdequacy.row_inflation_ratio` (rows ÷ clusters) makes a narrow corpus
wearing a large row count visible, and construction is **refused** if clusters
exceed rows.

---

## 6. Horizon research readiness

Measured 2026-09-30, read-only, via `assess_horizon()`.

| Horizon | Labels (rows) | Matured | Instruments / clusters | Sessions | Effective sample | Status | Reason |
|---|---|---|---|---|---|---|---|
| intraday_5m | 554 | 554 | 237 | 29 | 237 | DATA_ACCUMULATION_REQUIRED | 29 of 30 sessions |
| intraday_15m | 554 | 554 | 237 | 29 | 237 | DATA_ACCUMULATION_REQUIRED | 29 of 30 sessions |
| intraday_30m | 554 | 554 | 237 | 29 | 237 | DATA_ACCUMULATION_REQUIRED | 29 of 30 sessions |
| intraday_60m | 554 | 554 | 237 | 29 | 237 | DATA_ACCUMULATION_REQUIRED | 29 of 30 sessions |
| **d1** | 544 | 544 | **236** | 27 | **236** | **RESEARCH_READY** | sample and sessions clear |
| **d3** | 531 | 531 | **236** | 25 | **236** | **RESEARCH_READY** | sample and sessions clear |
| **d5** | 458 | 458 | **223** | 23 | **223** | **RESEARCH_READY** | sample and sessions clear |
| **d10** | 286 | 286 | **159** | 17 | **159** | **RESEARCH_READY** | sample and sessions clear |
| **d20** | — | — | — | — | — | **PROTECTED** | sealed; no counts collected |

Intraday is short by **one session**, reported as one session rather than rounded
away.

`RESEARCH_READY` means a fair test is **possible**. It says nothing about whether
an edge exists, and the judgement carries that sentence in its own reasons.

---

## 7. Research plan

As pre-registered at `ed016e8`.

| | |
|---|---|
| Horizons selected | **d1, d3, d5, d10** — never pooled, each with its own labels, purge and verdict |
| Horizons deferred | all four intraday (29 of 30 sessions) |
| Out of scope | **d20 — sealed** |
| Model families | ridge regression, logistic regression (Phase 25.9's, verbatim) |
| Feature sets | all numeric (26 features); dense only |
| Candidate budget | **16 cells (4 × 4), fixed and not grown** |
| Baselines | `ModelingEngine` mandatory baselines, unchanged |
| Metrics | primary metric per task; Brier vs base rate for probabilistic candidates |

### Candidates, verbatim from Phase 25.9

| | Family | Task | Features |
|---|---|---|---|
| C0 | ridge (α=1) | abnormal return | all numeric |
| C1 | logistic | direction | all numeric |
| C2 | ridge (α=10) | abnormal return | all numeric |
| C3 | logistic | direction | dense only |

C0/C2 are one family at two regularisation strengths and are **not** independent
evidence of each other. Neither are C1/C3.

### Train / validation / holdout structure

| Parameter | Value |
|---|---|
| holdout boundary | **`2026-08-15T01:23:31Z`** — frozen where Phase 25.9 put it |
| research region | everything strictly before that instant |
| folds | expanding, day-granular, inside the research region only |
| purge (calendar days) | d1→3, d3→5, d5→7, d10→14 |
| embargo | 6 days |
| test window | 7 days |
| step | 7 days |
| min train / test rows | 30 / 20 |
| **min valid folds** | **3** |

### Stopping rules

- all four candidates evaluated for a horizon;
- **fewer than 3 valid folds ⇒ INSUFFICIENT DATA** — this fired, on all four;
- research-region effective sample below 30.

No candidate is retried with different hyperparameters after failing. There is no
branch in the plan that responds to a result.

---

## 8. What was implemented

**Sample diagnostics** — `src/adaptive/sample_adequacy.py`: `SampleAdequacy`
(effective sample always distinct clusters, `row_inflation_ratio`, refuses
clusters > rows), `effective_sample_of()`, `assess_horizon()`,
`ResearchReadiness`, and `MIN_EFFECTIVE_SAMPLE` **aliased** from
`ModelEvaluation` rather than restated.

**Dataset changes** — **none.** The corpus was read `mode=ro` and not modified,
rebuilt or extended.

**Model candidates** — none created. The four pre-registered specifications are
imported verbatim from `scripts/research_model_quality.spec_for`.

**Evaluation** — `scripts/research_multi_horizon.py` reuses
`rmq.evaluate_fold`, `rmq.verdict`, `ModelingEngine.train_and_evaluate`,
`splits.purge` and `splits.embargo`. No second qualification gate, registry or
research stack exists.

**Robustness** — the fold-majority rule, implemented but **never reached**
(0 folds).

**Calibration** — Brier-versus-base-rate required for probabilistic candidates;
**never reached**.

**Uncertainty** — would derive from fold dispersion; **nothing measurable**.

**Multi-horizon logic** — the same procedure across four horizons, each with its
own calendar-span purge and its own independent verdict. Horizons are never
pooled. `span_requirement()` reports what a legal fold design needs versus what
exists.

**Target / horizon integration** — none required or performed. With zero
qualified models, `target_estimation` and `horizon_intelligence` correctly emit
nothing.

**F.5 integration** — `model_inventory` and `horizon_inventory` are reused, not
duplicated. The F.5 diagnostic was re-run and compared (§22).

**Tests** — 57 new (24 + 33). **Docs** — pre-registration, architecture, this
report.

---

## 9. What worked

Evidence-backed only.

1. **The root cause was found and proved from stored records.** `cluster_count=4`
   on a 5-row test window dated 2026-08-29 — the 4 was an evaluation artefact,
   not a corpus property.
2. **The corpus clears the sample floor comfortably**: 159–236 clusters per daily
   horizon against 30. Asserted by a test against the real database.
3. **Row inflation cannot fake a sample.** Duplicating a cluster 50× leaves the
   effective sample unchanged; asserted first in the suite.
4. **The real constraint was located**: calendar span, not sample size. Shortfalls
   of 15/17/19/26 days for d1/d3/d5/d10.
5. **The holdout stayed frozen and was never opened.** `holdout_opened: false`.
6. **The phase was willing to finish with nothing**, and did.
7. **The pre-registration was committed before the run** (`ed016e8` precedes
   `1be725d`), so the git history itself proves no choice was made after seeing a
   result.
8. **Zero regressions.** 5062 → 5119 tests, all passing.

---

## 10. What did not work

Mandatory, and complete. Every failure below was mine, found during development,
and is now fixed with a test holding it fixed.

### 10.1 A bash heredoc destroyed a 500-line module mid-write

- **Symptom:** `unexpected EOF`; the file was left unusable.
- **Root cause:** writing a large Python module through a shell heredoc.
- **Fix:** verified nothing partial had been written, then used the file-write
  tool for every Python file thereafter.
- **Status:** resolved.

### 10.2 The `pytest` baseline returned exit code 0 while printing "No module named pytest"

- **Symptom:** a green-looking baseline that ran no tests.
- **Root cause:** `pytest` is not installed; the exit code did not reflect it.
- **Fix:** re-ran with `PYTHONPATH=src python -m unittest discover -s tests -t . -b`.
- **Status:** resolved. Had this gone unnoticed, the entire phase would have rested
  on a fabricated baseline.

### 10.3 Wrong `BaselineComparison` field names made the gate answer *softer* than the truth

- **Symptom:** baseline comparisons came back empty, so the qualification gate
  returned `UNJUDGED` where the correct verdict was `FAILED`.
- **Root cause:** I used `baseline_family` / `metric` / `baseline_value` /
  `model_value` / `model_wins`. The real fields are `baseline_name` /
  `metric_name` / `baseline_score` / `model_score` / `beats_baseline`.
- **Fix:** corrected to the canonical names; the model now correctly reports
  losing to `baseline_historical_mean` and `baseline_majority_class`.
- **Status:** resolved. Worth naming because the failure mode was a *more
  forgiving* verdict than reality — exactly the direction that hides problems.

### 10.4 `effective_sample_size` passed as a constructor field

- **Symptom:** construction error.
- **Root cause:** it is a **derived property** (`cluster_count or sample_size`),
  not an input.
- **Fix:** stopped passing it.
- **Status:** resolved.

### 10.5 My own docstring restated the frozen D20 hypothesis

- **Symptom:** `test_boundary` failed on my own module.
- **Root cause:** I wrote the protected hypothesis into a docstring while
  explaining what must not be touched.
- **Fix:** removed. `PROTECTED_HORIZONS` names the horizon and nothing more.
- **Status:** resolved. The boundary test caught its own author, which is the
  point of having it.

### 10.6 `test_f6_defines_no_gate_of_its_own` forbade a legitimate alias

- **Symptom:** the test failed on correct code.
- **Root cause:** it forbade the **name** `MIN_EFFECTIVE_SAMPLE = `, but the
  module legitimately contains
  `MIN_EFFECTIVE_SAMPLE = ModelEvaluation.MIN_EFFECTIVE_SAMPLE` — an alias, which
  is the desired design.
- **Fix:** the assertion now forbids a **numeric literal**:
  `re.search(r"MIN_EFFECTIVE_SAMPLE\s*=\s*\d+", source)` must be `None`.
- **Status:** resolved. The test was wrong; the code was right.

### 10.7 `--run --json` printed a human header and poisoned stdout

- **Symptom:** two JSON tests failed — `RUNNING PRE-REGISTERED RESEARCH (n cells)`
  preceded the JSON document.
- **Root cause:** the header and per-horizon prints were unconditional.
- **Fix:** gated on `if not args.json:`, and `verbose=not args.json` passed through.
- **Status:** resolved.

### 10.8 Unclosed file handles in boundary tests

- **Symptom:** resource warnings.
- **Fix:** context managers throughout.
- **Status:** resolved.

### 10.9 Sample insufficiency — and the discovery that it was the wrong diagnosis

- **Symptom:** F.5 reported the sample as the blocker.
- **Root cause:** a stale evaluation artefact (§5). The corpus clears the floor by
  5–8×.
- **Status:** the diagnosis was **wrong and is now corrected**. The real blocker is
  §10.10.

### 10.10 Zero valid folds on every horizon — the finding that ended the phase

- **Symptom:** `folds_built: 0`, `folds_valid: 0`, verdict `INSUFFICIENT DATA`, on
  all four horizons.
- **Root cause:** the research region spans **15 calendar days**. A single legal
  fold needs purge + embargo + test = 16/18/20/27 days; three folds need
  30/32/34/41. The region cannot hold even the first fold on any horizon.
- **Why the region is 15 days:** the holdout instant was derived by Phase 25.9 from
  a **972-row** corpus. That corpus was rebuilt on 2026-08-28 and now holds **581**
  rows, so the same instant sits at **60.6%** instead of 20%.
- **Fix:** **none applied, deliberately.** See §10.11 and §10.12.
- **Status:** open, and it is an upstream data problem, not a modelling one.

### 10.11 Refusal: I did not recompute the holdout boundary

Recomputing it after seeing zero folds would have been holdout-shopping — the new
value would have been chosen *knowing* it produced folds. The pre-registration
exists to forbid exactly this. The boundary remains `2026-08-15T01:23:31Z`.

### 10.12 Refusal: I did not rebuild the corpus

A rebuild would invalidate the frozen holdout and silently change the dataset
underneath Phase 25.9's recorded negative results. Historical evidence stays
historical evidence.

### 10.13 No candidate overfitted, lost a baseline, or failed calibration *in F.6*

Because none was fitted. Reporting overfitting or calibration failures here would
be fabrication. The one *pre-existing* model does lose both its baselines — see
§14 — but that is Phase 25.9's result, recorded in August, not F.6's.

---

## 11. What was deferred

| Deferred | Why |
|---|---|
| **all four intraday horizons** | 29 of 30 required distinct sessions — short by one |
| **target calibration** | requires a qualified model; none exists |
| **horizon calibration** | same |
| **model promotion** | nothing to promote; no code path exists in this phase |
| **shadow validation (I-G)** | nothing to shadow |
| **production wiring** | out of scope; Portfolio/Risk/Execution untouched |
| **the 15-day research region** | upstream corpus recovery, explicitly not this phase |
| **D20** | **sealed.** Not executed, inspected, inferred or consumed |

---

## 12. Data / sample expansion

**Additional legitimate independent evidence obtained: none. Zero rows were
added.** The database was opened read-only for the entire phase.

What *was* made usable, and it is a real gain though not a row gain:

| | Before F.6 | After F.6 |
|---|---|---|
| believed effective sample (d5) | 4 (stale artefact) | **223 clusters**, measured |
| horizons with a measured readiness verdict | 1 | **9** (4 daily, 4 intraday, 1 protected) |
| horizons with any research history | 1 (d5) | 4 attempted, 4 verdicts recorded |

Forward labels matured naturally as the calendar advanced — the same split that
had 5 test rows spanning 4 instruments on 2026-08-29 now has **32 rows spanning 27
instruments**. That is genuine new independent evidence, created by time, not by
duplication.

**No duplicated rows are reported as expansion.** `row_inflation_ratio` exists
specifically so that could not happen by accident.

---

## 13. Model candidate inventory

**Candidates evaluated in F.6: 0 of 16 cells.** Every cell terminated at fold
construction before any fit.

| Cell | Family | Horizon | Features | Sample | Effective sample | Metrics | Baselines | Robustness | Calibration | Uncertainty | Verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|
| C0–C3 × d1 | ridge / logistic | d1 | 26 / dense | 337 region rows | 178 clusters | **not computed** | **not run** | **not run** | **not run** | **not measurable** | **INSUFFICIENT DATA** |
| C0–C3 × d3 | ridge / logistic | d3 | 26 / dense | 335 region rows | 178 clusters | **not computed** | **not run** | **not run** | **not run** | **not measurable** | **INSUFFICIENT DATA** |
| C0–C3 × d5 | ridge / logistic | d5 | 26 / dense | 335 region rows | 178 clusters | **not computed** | **not run** | **not run** | **not run** | **not measurable** | **INSUFFICIENT DATA** |
| C0–C3 × d10 | ridge / logistic | d10 | 26 / dense | 284 region rows | 157 clusters | **not computed** | **not run** | **not run** | **not run** | **not measurable** | **INSUFFICIENT DATA** |

Reason, identical on all four: `only 0 valid fold(s); the contract requires 3`.

The empty cells are not omissions. There is no metric to report, and inventing one
would be the single worst thing this report could do.

### The one pre-existing model, for completeness

| | |
|---|---|
| id | `ridge_abnormal_return:v1` (`tm-0be69458d6554731`) |
| family / task | ridge regression / abnormal return |
| horizon | d5 |
| features | 26, `all_numeric_v1` |
| train sample | 320 rows / **4 clusters** |
| test sample | **5 rows / 4 clusters** |
| effective sample | **4** (floor 30) |
| metrics | MAE 0.039314, RMSE 0.048282, **R² −0.197236**, directional accuracy 0.600 |
| baselines | **loses to both** |
| robustness | single chronological split — no folds |
| calibration | not applicable (regression) |
| uncertainty | not measurable at n=5 |
| **verdict** | **FAILED** — `SAMPLE_TOO_SMALL`, `BASELINE_NOT_BEATEN`, `MODEL_NOT_ACTIVE` |

Directional accuracy of 0.6 on five rows is noise. The gate treats it as such.

---

## 14. Baseline comparison

For the pre-existing d5 model. Scores are stored negated so that higher is better;
magnitudes below are MAE.

| Baseline | Metric | Baseline score | Model score | Model beats it |
|---|---|---|---|---|
| `baseline_historical_mean` | MAE | **0.037681** | 0.039314 | **NO** |
| `baseline_majority_class` | MAE | **0.037999** | 0.039314 | **NO** |

`beats_all_baselines = False`. The historical mean predicts the abnormal return
better than the model does.

**For F.6's own 16 cells: no baseline comparison was run, because no candidate was
fitted.** No cell can be said to have beaten or lost to anything.

---

## 15. Qualification gates

### F.6's 16 cells

| Gate | d1 | d3 | d5 | d10 |
|---|---|---|---|---|
| research-region effective sample ≥ 30 | **PASS** (178) | **PASS** (178) | **PASS** (178) | **PASS** (157) |
| horizon unsealed | **PASS** | **PASS** | **PASS** | **PASS** |
| **≥ 3 valid walk-forward folds** | **FAIL (0)** | **FAIL (0)** | **FAIL (0)** | **FAIL (0)** |
| beats every mandatory baseline | **NOT REACHED** | **NOT REACHED** | **NOT REACHED** | **NOT REACHED** |
| strict fold majority | **NOT REACHED** | **NOT REACHED** | **NOT REACHED** | **NOT REACHED** |
| Brier beats base rate | **NOT REACHED** | **NOT REACHED** | **NOT REACHED** | **NOT REACHED** |
| holdout evaluation | **NOT REACHED** (holdout never opened) | " | " | " |

The fold gate is the first hard failure and the phase stopped there, as
pre-registered. `NOT REACHED` is deliberately distinct from `FAIL`: a gate that
was never evaluated has not been passed *or* failed.

### The pre-existing d5 model

| Gate | Result |
|---|---|
| `SAMPLE_TOO_SMALL` | **FAIL** — effective sample 4, floor 30 |
| `BASELINE_NOT_BEATEN` | **FAIL** — loses on MAE to both baselines |
| `MODEL_NOT_ACTIVE` | **FAIL** — status `evaluated`, not active |

**No gate was weakened, and no threshold was lowered.** A test asserts
`MIN_EFFECTIVE_SAMPLE is ModelEvaluation.MIN_EFFECTIVE_SAMPLE` and that the module
contains no numeric literal for it.

---

## 16. Multi-horizon result

| | |
|---|---|
| Horizons researched | **4** (d1, d3, d5, d10) |
| Horizons deferred | 4 intraday (one session short) |
| Horizons protected | 1 (d20) |
| **Horizons qualified** | **0** |
| Horizon disagreement | **not applicable** — disagreement needs ≥ 2 qualified horizons; there are 0 |
| Preferred-horizon capability | **none.** `horizon_intelligence` returns `None`, and every instrument shows `pref=none` |

Horizons were never pooled. Each carries its own labels, its own calendar-span
purge and its own independent verdict.

---

## 17. Confidence

**No genuinely independent model evidence exists. None was created.**

Of the 16 pre-registered cells, C0/C2 are one family at two regularisation
strengths, and C1/C3 are one family on two feature sets. Even had they all fitted,
they would not have constituted model agreement, and the pre-registration says so
in advance.

The confidence mechanics, unchanged, are `src/signals/strategy.py`:

```
confidence = base × quality × agreement × sample
```

The ≈0.30 cluster observed earlier is arithmetic, not a bug:
`0.5 × 1.0 × 0.6 × 1.0 = 0.30`, and with the small-sample factor
`0.30 × 0.5 = 0.15`. Against `min_signal_confidence = 0.40`, both are correctly
below threshold.

**No confidence value was produced by F.6, and none was inflated.** With zero
qualified models the formula is never reached.

---

## 18. Uncertainty

**Nothing newly measurable.**

Uncertainty would come from dispersion across valid folds. There were zero folds,
so there is no dispersion, no interval and no variance estimate. The honest value
is absent, and the contracts emit absent rather than a placeholder.

What *is* now measurable and was not before: `row_inflation_ratio`,
`clusters_short_of_floor`, `research_region_clusters` versus total clusters, and
`span_requirement` per horizon — i.e. we can now quantify *how far from being
answerable* each question is.

---

## 19. Expected return

**0 qualified candidates can provide a defensible expected return.**

Expected return requires a model that beat its baselines out-of-sample across a
majority of valid folds. Zero candidates reached a fold. No expected return was
fabricated anywhere in this phase.

---

## 20. Target capability

**Zero.**

**0 of 244 instruments** and **0 of 4 researched horizons** can receive a
defensible target zone, in shadow or anywhere else.
`src/adaptive/target_estimation.py` emits a zone only when a `TargetEstimate` is
`AVAILABLE`, which requires a qualified model. It correctly emits a reasoned
refusal instead.

---

## 21. Horizon capability

Measured across all 244 instruments via `scripts/signal_horizon_status.py`:

| | Count |
|---|---|
| usable horizon | **0** |
| preferred horizon | **0** |
| horizon disagreement | **0** (needs ≥ 2 usable horizons) |
| **no usable horizon** | **244** |

Every instrument reports `pref=none`. Not one of these is a statement about the
market.

---

## 22. F.5 before / after

The F.5 diagnostic was re-run at `1be725d` and compared with its state at
`128e9b5`.

| | Before F.6 (`128e9b5`) | After F.6 (`1be725d`) |
|---|---|---|
| instruments evaluated | 244 | **244** |
| signals | 0 | **0** |
| valid no-edge (a decision) | 0 | **0** |
| `model_not_qualified` | **244** | **244** |
| missing data / stale / unsupported / protected / error | 0 each | **0 each** |
| preferred horizon | none | **none** |
| funnel exit point | `that model is qualified` → 0 | **identical** |

**Unchanged, exactly as predicted.** F.6 was read-only; it could not have moved
these numbers, and had they moved, that would have been evidence of a bug.

What did change is the *explanation*. Before F.6, `model_not_qualified` was
attributed to an effective sample of 4. After F.6 it is attributed to a research
region too short to test anything — which points at entirely different repair
work.

---

## 23. Failed research / negative knowledge

Recorded so no future phase repeats it blindly.

1. **d1, d3, d5 and d10 cannot be researched against this corpus at this holdout
   boundary.** Not because the candidates are weak — no legal fold exists.
   Shortfalls: **15 / 17 / 19 / 26** calendar days.
2. **The constraint is calendar span, not cluster count.** Collecting more
   observations inside the same 29-day window cannot help. This is the single most
   useful thing F.6 learned, and it inverts F.5's diagnosis.
3. **A stored `effective_sample_size` is not current dataset capacity.** The 4 was
   one test window on 2026-08-29. Never read it as a corpus property again.
4. **Intraday is short by one session**, not by thousands of bars. 554 rows across
   29 sessions are 29 independent days.
5. **Rebuilding a corpus under a frozen holdout has research consequences.** The
   2026-08-28 rebuild took the corpus from 972 to 581 rows, moving the frozen
   instant from the 20th to the 60.6th percentile and leaving a 15-day research
   region.
6. **Canonical events begin 2026-07-30 although 16,737 articles predate that
   date**; July articles largely lack entity links (1,483 versus 22,021). That is
   an upstream extraction / entity-resolution gap, and it is the real reason the
   research region is short.
7. **The pre-existing d5 ridge model loses to the historical mean** (MAE 0.039314
   vs 0.037681) with **R² −0.197**. Do not revive that specification expecting a
   different answer from a wider window alone.

---

## 24. As-of / leakage

| | |
|---|---|
| Train cutoff | per fold, expanding, always **strictly inside** the research region |
| Validation cutoff | each fold's test window, separated by purge **and** embargo |
| Purge | `src/modeling/splits.purge`, reused — **calendar** days: 3 / 5 / 7 / 14 |
| Embargo | `src/modeling/splits.embargo`, reused — 6 days |
| **Holdout isolation** | boundary `2026-08-15T01:23:31Z`; **`holdout_opened: false`** |
| Physical truncation | `assess_horizon(as_of=…)` physically excludes later rows; a test proves an earlier `as_of` sees strictly fewer rows and no more clusters |
| Protected isolation | `sample_adequacy.py` contains no reference to `value_json` — asserted by test, so no label value can be read at all |

Purge is in calendar days precisely so a weekend cannot carry a label across a fold
boundary. Each purge is ≥ the trading-day span it protects; over-purging removes
rows but cannot admit leakage.

`ExpectedHorizon(7)` is **seven trading days**, not calendar days — a distinction
that caused a real bug in an earlier phase and is honoured here by converting at
7/5 where needed.

---

## 25. D20 status

| | |
|---|---|
| executed? | **NO** |
| inspected? | **NO** |
| inferred? | **NO** |
| consumed? | **NO** |
| trained on? | **NO** |
| used for feature selection? | **NO** |
| used for model selection? | **NO** |
| used for hyperparameter / threshold / target / horizon selection? | **NO** |
| hypothesis changed? | **NO** |
| anchor-v2 changed? | **NO** |
| protected window changed? | **NO** |

`scripts/validate_d20_reversal.py` was not run. `research_multi_horizon.py` raises
`ProtectedHorizonRequested` before any query if pointed at d20.
`guard_protected()` raises. `PROTECTED_HORIZONS` names the horizon without
restating the hypothesis — a boundary test enforces that, and it caught my own
docstring doing so during development. d20 appears as `PROTECTED` with zero counts
and nowhere else.

---

## 26. Model promotion

```
models promoted = 0
```

No promotion occurred, and **no code path in this phase could perform one**. The
harness contains no promotion call. Qualification and promotion remain distinct
acts; the second is a human-governed canonical action outside this phase.

---

## 27. Production behaviour

| | |
|---|---|
| Portfolio changed? | **NO** |
| Risk changed? | **NO** |
| OrderIntent changed? | **NO** |
| Execution changed? | **NO** |
| Capture changed? | **NO** |
| IBKR changed? | **NO** |
| Subscriptions changed? | **NO** |
| Production model changed? | **NO** |

0 files modified. Every database connection was opened `mode=ro`. Existing
Portfolio remains authoritative, existing Risk remains the hard gate, and
Execution remains the only path toward IBKR.

---

## 28. Broker safety

```
broker writes attempted:  0
broker writes succeeded:  0
paper orders:             0
live orders:              0
```

No executable `OrderIntent` was created. No IBKR connection was opened. No
market-data subscription was purchased or changed.

---

## 29. Test results

### Full suite

| | Baseline `6762edd` | Final `1be725d` |
|---|---|---|
| Tests | 5062 | **5119** |
| Pass | 5061 | **5118** |
| Fail | 0 | **0** |
| Errors | 0 | **0** |
| Skipped | 1 | **1** |
| Runtime | 1065.874 s | **488.078 s** |

```
PYTHONPATH=src python -m unittest discover -s tests -t . -b
Ran 5119 tests in 488.078s
OK (skipped=1)
```

+57 tests, exactly the two new files. **Zero regressions.**

### Targeted and requested suites, at `1be725d`

| Suite | Tests | Pass | Fail | Err | Skip | Runtime |
|---|---|---|---|---|---|---|
| **F.6 — `test_sample_adequacy`** | **24** | 24 | 0 | 0 | 0 | 0.060 s |
| **F.6 — `test_research_discipline`** | **33** | 33 | 0 | 0 | 0 | 3.447 s |
| adaptive (incl. F.5, I-F, I-E, I-D, I-C, I-B) | **775** | 775 | 0 | 0 | 0 | 25.428 s |
| modeling | 142 | 142 | 0 | 0 | 0 | 2.714 s |
| signals | 129 | 129 | 0 | 0 | 0 | 3.349 s |
| research / research integrity | 104 | 104 | 0 | 0 | 0 | 12.010 s |
| experiments (Experiment Engine) | 83 | 83 | 0 | 0 | 0 | 5.087 s |
| portfolio | 273 | 273 | 0 | 0 | 0 | 0.899 s |
| trading (readiness, live safety, Risk) | 224 | 224 | 0 | 0 | 0 | 4.543 s |
| capture / session | 126 | 126 | 0 | 0 | 0 | 276.339 s |
| events | 81 | 81 | 0 | 0 | 0 | 0.063 s |
| entities | 84 | 84 | 0 | 0 | 0 | 0.176 s |
| fusion | 47 | 47 | 0 | 0 | 0 | 0.028 s |
| news | 107 | 107 | 0 | 0 | 0 | 0.081 s |
| **full suite** | **5119** | **5118** | **0** | **0** | **1** | **488.078 s** |

The runtime difference between the two full runs is machine load, not scope: the
baseline ran concurrently with other work.

---

## 30. Compute / performance

| | |
|---|---|
| Experiments run | **0** — no candidate was fitted |
| Total training runtime | **0 s** |
| Largest experiment | none |
| Harness runtime, `--run` end to end | **0.42 s** |
| Harness runtime, `--plan` | < 1 s |
| Memory / storage impact | **0 bytes written.** All connections `mode=ro` |
| Bounded-search evidence | budget fixed at **16 cells** in the pre-registration; `CANDIDATES` is imported verbatim, no sweep, no seed loop, no retry-after-failure branch |

The run took under half a second because it correctly refused to fit anything. A
cheap honest answer.

---

## 31. Scope-creep review

| | |
|---|---|
| Unrelated changes | **none.** 5 files, all new, all F.6 |
| Qualification gate changes | **none.** The gate is imported, not redefined |
| Threshold changes | **none.** `MIN_EFFECTIVE_SAMPLE` 30, `min_signal_confidence` 0.40, `MIN_VALID_FOLDS` 3 — all unchanged, one aliased from the canonical constant |
| Promotion changes | **none.** No promotion code path exists |
| Execution changes | **none** |
| Protected research changes | **none.** D20 untouched |
| New dependencies | **none.** Standard library plus existing project modules |
| Paid external calls | **none** |
| Files modified | **0** |

---

## 32. Known limitations

1. **The 15-day research region makes every daily horizon untestable.** Not a
   limitation of the harness — a limitation of the dataset under the frozen
   boundary.
2. **Intraday is one session short** (29 of 30). One more trading day of capture
   resolves it, and nothing was rounded to pretend otherwise.
3. **A cluster is an instrument, so the effective sample cannot distinguish
   genuinely independent events within one company.** Two earnings surprises for
   the same issuer count as one cluster. Conservative, but coarse.
4. **Source independence remains UNKNOWN** corpus-wide (an I-F finding, unchanged).
   F.6 neither improved nor worked around it.
5. **`event_time` is widely missing** (also I-F). Information timing rests on
   publication and ingestion timestamps.
6. **No uncertainty, calibration or expected-return capability exists**, because
   no model was fitted. These remain designs, not measurements.
7. **The upstream gap is unaddressed:** canonical events start 2026-07-30 while
   16,737 articles predate it, and July articles largely lack entity links (1,483
   vs 22,021). Deliberately out of scope here.
8. **One skipped test** in the full suite, unchanged from baseline.

---

## 33. Readiness for I-G

**NO.**

| Requirement | State |
|---|---|
| ≥ 1 qualified research candidate | **0** |
| defensible expected return | **none** |
| defensible target zone | **0 of 244 instruments** |
| usable preferred horizon | **none** |
| ≥ 3 valid walk-forward folds | **0** |

I-G is shadow validation, and shadow validation requires something to shadow.
With zero qualified candidates there is nothing to observe, no prediction to score
and no thesis to track.

This is not a pipeline failure. The pipeline refused correctly. The unblocking work
is upstream data recovery — a research region wide enough to hold a legal fold
design — not modelling.

---

## 34. Readiness for I-H

**NO.**

I-H additionally requires every one of the following, and none is satisfied:

| Requirement | State |
|---|---|
| realtime operational verification | **not performed.** Entitlement remains unverified; no realtime observation exists |
| shadow evidence | **none.** I-G has not run |
| execution freshness | **not established** |
| qualified model / signal path | **none.** 0 qualified models, 0 signals |
| safety gates | in place and unchanged, but with nothing to gate |

---

## 35. Final verdict

> ## PHASE 25.9I-F.6 IMPLEMENTATION = COMPLETE
>
> ## MODEL QUALIFICATION OUTCOME = NONE QUALIFIED

**Evidence.**

The phase is COMPLETE because everything asked for was built and tested: the
sample diagnostic, the multi-horizon harness, the pre-registration committed
before the run at `ed016e8`, 57 new tests, and documentation. 5119 tests pass with
zero regressions against a 5062-test baseline, and zero files were modified.

The qualification outcome is NONE QUALIFIED because **no candidate was fitted on
any horizon.** The research region spans **15 calendar days**; a single legal fold
needs purge + embargo + test = **16, 18, 20 and 27** days for d1, d3, d5 and d10,
and three folds need **30, 32, 34 and 41**. Every horizon is short — by **15, 17,
19 and 26** days respectively. `folds_valid: 0` on all four, verdict
`INSUFFICIENT DATA`, `qualified_total: 0`, `models_promoted: 0`,
`thresholds_changed: 0`, `holdout_opened: false`.

**F.5's diagnosis was wrong and F.6 corrected it.** The sample was never the
constraint: the stored `effective_sample = 4` was the cluster count of a five-row
test window dated 2026-08-29, while the corpus holds **159–236 clusters per daily
horizon** against a floor of 30. The binding constraint is **calendar span**, and
it became binding when the 2026-08-28 rebuild took the corpus from 972 to 581 rows
and left the frozen holdout instant sitting at the 60.6th percentile instead of the
20th.

**Two things were deliberately not done**, and both refusals are part of the
result: the holdout boundary was not recomputed after zero folds were observed
(that is holdout-shopping, and the pre-registration exists to forbid it), and the
corpus was not rebuilt (that would invalidate the frozen holdout and silently
change the dataset underneath Phase 25.9's recorded negative results).

**D20 remains sealed** — not executed, inspected, inferred or consumed. Broker
writes, paper orders and live orders: **0 / 0 / 0**. Portfolio, Risk, OrderIntent,
Execution, capture, IBKR and subscriptions: **unchanged**.

And the thing this report must not let slip: **zero folds means the question was
never asked.** This is not evidence that no edge exists on d1, d3, d5 or d10. It is
evidence that the dataset, as currently reconstructed, cannot answer it.
