# Phase 25.9I-F.6C — Completion Report

> **THE MODEL MUST EARN DISCRIMINATION. THE PROBABILITY MUST EARN TRUST.**

Written 2026-10-04. Architecture: `docs/PHASE_25_9I_F6C_ARCHITECTURE.md`.
Frozen plan: `docs/PHASE_25_9I_F6C_PREREGISTRATION.md`, sha256
`ba141fd9fdead1bc8b56d524a0596b03f7f8a97bde5b98bc8caaf9c8ede66ede`, committed at
**`2f1977f`**. Raw results: `docs/PHASE_25_9I_F6C_RESULTS.json`.

---

## 1. Phase implementation status

**COMPLETE.**

All 12 pre-registered cells evaluated, 0 unevaluated, 0 execution errors.

---

## 2. Model qualification outcome

**NONE QUALIFIED.**

**0 of 12 candidates survived the pre-holdout gates, so the holdout was never
opened.** No model has been fitted against data at or after
`2026-08-18T00:00:00Z` by any phase.

---

## 3. Calibration outcome

**NONE PASSED.**

Reported separately from §2, as required. **0 of 12 cells cleared the calibration
gate** — Brier skill score above zero in a strict majority of folds. Only **2 of
36** fold-evaluations had positive Brier skill at all, and those two are the
*same fold* under two configurations that fingerprint identically there, so they
are **one observation**, not two.

**But calibration itself worked.** Measured, pooled over 12 fold-evaluations per
arm:

| Arm | Mean Brier | Mean Brier skill | Mean ECE | Mean ROC-AUC | Mean balanced accuracy |
|---|---|---|---|---|---|
| **raw** | 0.3529 | **−0.3623** | 0.2898 | 0.5326 | 0.5210 |
| **platt** | **0.2830** | **−0.1003** | **0.1831** | 0.4915 | 0.5082 |
| **isotonic** | 0.2949 | −0.1446 | 0.2029 | 0.5167 | 0.5043 |

Platt cut the excess Brier by **72%** (skill −0.362 → −0.100) and expected
calibration error by **37%**. It did everything a calibrator can do, and it was
not enough — because **every arm remains below zero skill**, i.e. worse than
simply predicting the base rate.

That is the finding: **calibration measurably improved the probabilities and
could not make them useful, because there was no discrimination to preserve.**

---

## 4. F.6B preservation

| | |
|---|---|
| F.6B result unchanged? | **YES** — `complete`, `qualified_total = 0`, 16/16 cells |
| F.6B pre-registration unchanged? | **YES** — sha256 `9150b988993dcac3…` still matches its handoff |
| F.6B holdout unchanged? | **YES** — `2026-08-18T00:00:00Z` |
| F.6B results artifact unchanged? | **YES** — not regenerated |
| F.6B candidate rows unchanged? | **YES** — 16 rows, 0 qualified |
| Negative knowledge retained? | **YES** — §24 |

`preflight()` does not merely assume this: it **verifies** F.6B's marker reads
`complete` with `qualified_count = 0` and its 16 rows with 0 qualified, and
refuses to run if any of it moved. A test asserts the same.

---

## 5. Git state

| | |
|---|---|
| Branch | `ibkr-paper-validation-fixes` |
| Baseline HEAD | **`83948af`** (F.6B completion report) |
| Pre-registration commit | **`2f1977f`** — contains the plan and **no** harness, results or marker |
| Final HEAD | **`cc96927`** |
| Working tree at start | clean |
| Commits | 2 (`2f1977f` plan, then the implementation) |
| Diff against baseline | **6 files changed, 9,667 insertions(+), 0 deletions(-)** |
| **Files modified** | **0** |

Purely additive. Nothing was changed, only added.

| File | Lines | Purpose |
|---|---|---|
| `docs/PHASE_25_9I_F6C_PREREGISTRATION.md` | 493 | the frozen plan |
| `src/modeling/calibration.py` | 682 | calibrators and probability metrics |
| `scripts/research_f6c.py` | 1,305 | pre-flight, three-window folds, gates, run-once |
| `tests/adaptive/test_f6c_calibration.py` | 842 | 71 tests |
| `docs/PHASE_25_9I_F6C_ARCHITECTURE.md` | 419 | documentation |
| `docs/PHASE_25_9I_F6C_RESULTS.json` | 5,926 | the full fold-level record |

---

## 6. Baseline tests

Run at `83948af` **before** any F.6C file was added:

```
PYTHONPATH=src python -m unittest discover -s tests -t . -b
Ran 5334 tests in 1074.567s
OK (skipped=1)
```

| | |
|---|---|
| Tests | **5,334** |
| Pass | **5,333** |
| Fail / Errors | **0 / 0** |
| Skipped | **1** |
| Runtime | **1074.567 s** |

**This baseline is uncontaminated.** In F.6B a baseline run was invalidated
because I edited an existing module while it ran. This time only *new* files were
added during the run — `unittest discover` enumerates at start, so new files are
not picked up and no existing file changed underneath it.

### Pre-research model state

| | v1 (production) | v2 (research snapshot) |
|---|---|---|
| model specifications | 1 (`ridge_abnormal_return:v1`) | **0** |
| qualified models | **0** | **0** |
| promoted (status active) | **0** | **0** |
| signal-capable horizons | **0** | **0** |
| signals | **0** | **0** |
| defensible targets | **0** | **0** |
| F.5 funnel exit | 244 × `model_not_qualified` | 301 × `no_model_available` |

---

## 7. F.6C pre-registration

| | |
|---|---|
| experiment id | **`f6c-3a9e75bdcca141a5`** |
| pre-registration | `docs/PHASE_25_9I_F6C_PREREGISTRATION.md` |
| **sha256** | **`ba141fd9fdead1bc8b56d524a0596b03f7f8a97bde5b98bc8caaf9c8ede66ede`** |
| snapshot | `research_snapshot_v2`, hash `4ccab384d37a9eb2…`, **recomputes** |
| corpus | `canonical_corpus_v2`, hash `4247f543904204c9…` |
| horizons | **d1, d3** |
| deferred | d5 (`INSUFFICIENT_FOLDS`), d10 (`INSUFFICIENT_HOLDOUT`), intraday ×4 (`BY_F7_POLICY`), d20 (`PROTECTED`) |
| model family | logistic regression only — Phase 25.9's `C1`, verbatim |
| feature configurations | `all_numeric`, `dense_only` (both kept; fingerprint rule) |
| calibration methods | `raw` (control), `platt`, `isotonic` |
| **candidate budget** | **12 cells**, hash `98ac3f0dd48fd620…` |
| metrics | ROC-AUC, balanced accuracy, accuracy, confusion, positive rate; Brier, Brier skill, log loss, ECE (with status), calibration slope/intercept, reliability bins |
| purge | d1→3, d3→5 calendar days — **inherited** |
| embargo | **6 days** — inherited |
| calibration / test / step windows | 7 / 7 / 7 days |
| min train / calibration / test rows | 30 / 30 / 20 |
| min valid folds | **3** |
| holdout policy | **inherited** `2026-08-18T00:00:00Z`; survivors only, once; not opened if nothing survives |
| qualification rule | canonical `is_deployable` **AND** every F.6C gate |

**The pre-registration hash is pinned inside the harness.** `preflight()` refuses
to run if the document no longer hashes to `ba141fd9…`, so editing the plan after
seeing a result breaks the harness rather than quietly changing the experiment.

**Proof of ordering:** commit `2f1977f`'s tree contains the pre-registration and
**no** `research_f6c.py`, no results artifact and no execution marker. Git, not
assertion.

---

## 8. Plan defects corrected from F.6B

Without rewriting F.6B, which stands as recorded.

### 8.1 Feature-configuration duplication

**F.6B finding:** `dense_only` duplicated `all_numeric` in 15 of 16 cells.

**What F.6C did:** audited it rather than acting on it. Under F.6C's fold
geometry the effective column sets **differ in 2 of 6 folds** (25 vs 26 columns).
So F.6B's duplication was a property of **F.6B's fold geometry**, not of the
configuration — and removing the configuration would have been removing it
because its *result* was poor.

**Both configurations are kept.** The structural rule is added instead: each
carries a per-fold fingerprint (SHA-256 of sorted effective column names), and
identical fingerprints mean identical evidence that may not be counted twice or
treated as model agreement. Computed from column identities only — no label
value enters it.

**It mattered immediately.** The only two positive-Brier-skill fold-evaluations
in the entire experiment share fingerprint `45dddece5017387d` in `wf4` and have
**identical** skill values. Without the rule they would have looked like two
independent signs of life. They are one.

### 8.2 Weak absolute quality

**F.6B finding:** a cell credited a baseline win at **31.2%** accuracy against
**26.0%**.

**What F.6C did:** added an absolute requirement whose thresholds are the
**no-skill points of the metrics themselves** — balanced accuracy > 0.5,
ROC-AUC > 0.5, Brier skill > 0. None is a chosen number; each is where its metric
says "no skill". Raw accuracy is deliberately **not** gated on, because it
inherits the fold's class balance, which is exactly how 31.2% slipped through.

**Verified in tests:** an always-up predictor on a 23.5%-positive window scores
raw accuracy **0.235** and balanced accuracy **exactly 0.500**; sweeping the
positive share from 10% to 90% gives 0.5 every time. A fixture reproducing the
F.6B shape fails `FAILED_ABSOLUTE_QUALITY` while still passing the relative
baseline gate.

### 8.3 Non-empty holdout adequacy

**F.6B finding:** d10 formed 3 valid folds and had **0 holdout rows**, so it could
never have qualified — and F.7's adequacy check had declared the design ADEQUATE
without testing holdout emptiness.

**What F.6C did:** promoted it to a rule. A horizon is eligible only with a
non-empty train window, calibration window, test window **and** final holdout.
`holdout_split()` returns `valid = False` when the holdout is empty, and a test
constructs that case directly. Measured before freezing, this excluded **d5**
(2 valid folds, needs 3) and **d10** (0 folds, 0 holdout rows).

**Neither was rescued by moving the boundary.** The design was fixed on principle
before the counts were measured.

---

## 9. Horizon eligibility

| Horizon | Rows | Clusters | Folds built | **Folds valid** | Calibration rows | Test rows | **Holdout rows** | Holdout valid | **Verdict** |
|---|---|---|---|---|---|---|---|---|---|
| **d1** | 891 | 237 | 4 | **3** | 77 / 100 / 154 | 159 / 143 / 124 | **154** | **YES** | **ELIGIBLE** |
| **d3** | 878 | 237 | 4 | **3** | 73 / 126 / 159 | 163 / 132 / 42 | **143** | **YES** | **ELIGIBLE** |
| d5 | 805 | 234 | 3 | **2** | 86 / 138 | 125 / 104 | 70 | — | **DEFERRED_INSUFFICIENT_FOLDS** |
| d10 | 633 | 211 | 1 | **0** | — | — | **0** | **NO** | **DEFERRED_INSUFFICIENT_HOLDOUT** |
| intraday ×4 | 901 | 237 | — | — | — | — | — | — | **DEFERRED_BY_F7_POLICY** |
| **d20** | — | — | — | — | — | — | — | — | **PROTECTED / SEALED** |

No horizon is unexplained.

### Class balance, which is the context the calibration result sits in

| Horizon | Fold | Positive rate, calibration | Positive rate, test |
|---|---|---|---|
| d1 | wf2 | 55.8% | **31.4%** |
| d1 | wf3 | 62.0% | 51.0% |
| d1 | wf4 | **35.1%** | **56.5%** |
| d3 | wf2 | 56.2% | 46.6% |
| d3 | wf3 | 45.2% | 53.8% |
| d3 | wf4 | **30.8%** | **64.3%** |

The calibration window's base rate and the test window's **move in opposite
directions** in three of six folds, by up to 33 percentage points. A calibrator
fitted on a 31%-positive window and applied to a 64%-positive one is
miscalibrated through no fault of the method. This is a real property of the data
and it is reported, not blamed on the calibrators.

---

## 10. What was implemented

**Pre-registration** — frozen and committed at `2f1977f` before the harness
existed; its hash pinned inside the harness.

**Calibration pipeline** — `src/modeling/calibration.py`: Platt scaling
(unregularized MLE), isotonic regression (pool-adjacent-violators), and an
identity control arm. `CalibratorFit.apply` passes abstentions through as `None`
and bounds every output to [0, 1].

**Baseline probability model** — a constant base-rate predictor, with the rate
taken from `TRAIN ∪ CALIBRATION` and never from test outcomes.

**Brier skill** — `1 − BS_model/BS_base`, returning `None` rather than infinity
when the baseline Brier is zero.

**Reliability diagnostics** — equal-width bins with support counts, a 10-observation
minimum, and ECE that returns `NOT_COMPUTED` with a reason when no bin reaches it.

**Absolute-quality gate** — balanced accuracy and ROC-AUC against their no-skill
points, in a strict majority of folds.

**Feature fingerprinting** — per-fold SHA-256 of sorted effective column names.

**Holdout adequacy** — `holdout_split()` with a `valid` flag that requires a
non-empty holdout and sufficient train and calibration rows.

**Three-window folds** — `build_f6c_folds()` with purge at both internal
boundaries and embargo before the test window, reusing `src/modeling/splits`.

**Abstention** — **NOT TESTED** (§19).

**F.5 integration** — none required; zero qualified candidates, so nothing was
fed to the shadow path.

**Execution marker** — reuses F.6B's `research_executions` and
`research_candidate_results` schema, with the cell-list hash, survivor-list hash
and an auditable `--invalidate` path.

**Tests and docs** — 71 tests; the architecture document and this report.

---

## 11. What worked

Evidence-backed only.

1. **The frozen plan was verified, not trusted.** Snapshot and corpus hashes
   recompute; the pre-registration hash matches its pinned value; and the
   inherited holdout rule **re-derives** `2026-08-18T00:00:00Z` rather than
   merely agreeing with a recorded value.
2. **F.6B was verified intact before anything ran** — marker `complete`,
   `qualified_count = 0`, 16 rows, 0 qualified.
3. **Horizon adequacy caught what F.7 missed.** d5 and d10 were excluded on
   measured geometry before the plan was frozen, and the boundary was not moved.
4. **The absolute-quality gate does exactly what it was added for**, verified on
   a fixture reproducing the F.6B 31.2%-vs-26.0% shape.
5. **The fingerprint rule prevented a real double-count** — the only two
   positive-skill results are one observation.
6. **Calibration worked as a method**: Platt cut excess Brier 72% and ECE 37%.
7. **A monotone calibrator provably cannot change ROC-AUC** — asserted to six
   decimal places on a fixture, and visible in the data.
8. **The gate order held**: nothing reached calibration without discrimination,
   and nothing passed on calibration alone.
9. **The holdout was never opened**, and the survivor list was hashed and
   persisted before that decision point regardless.
10. **A defect was found by distrusting a suspicious result**, fixed, the run
    invalidated on the record, and the experiment re-run (§12.1).
11. **Zero regressions.** 5,334 → 5,405 tests, all passing.
12. **The production database was never opened for writing.**

---

## 12. What did not work

**Mandatory, and complete.**

### 12.1 An implementation defect in my own optimiser, which would have been reported as a data property

- **Symptom:** the first execution reported Platt `NOT_FITTED` on **8 of 12** fold
  attempts, on calibration windows of 73–159 rows with both classes present, with
  the note "degenerate fit (single class, no spread, or singular)".
- **Why it was caught:** the numbers were implausible. 125 rows with 69/56 class
  split is not a degenerate fit.
- **Root cause:** `_fit_scalar_logistic` initialised Newton-Raphson at
  `slope = 1`. Its input is `logit(p)`, which reaches ±14 on this data, so a unit
  slope saturated every sigmoid on the first step, the IRLS weights `p(1−p)`
  underflowed, the Hessian went singular, and the fit bailed out. Traced
  explicitly: from `slope = 1` the iteration went 1.0 → −1.72 → 8.99 → determinant
  3.4e-01 → abort. From a null-model start it converged in **three** iterations to
  slope **0.0921**, intercept **−0.3212**.
- **Fix:** null-model initialisation (`slope = 0`, `intercept = logit(ȳ)`) plus
  damped Newton — halve the step until the likelihood improves. Genuinely
  degenerate inputs are still refused, and the known-good fixture reproduces its
  slope `0.408276` to the digit, so nothing that already worked changed.
- **Status:** resolved. The affected execution `f6c-885a0443fef0462b` was marked
  **`invalidated`** with the reason and **kept on the record**; the experiment was
  re-run as `f6c-3a9e75bdcca141a5`. The register shows one invalidated attempt and
  one valid execution, which is the truthful history rather than the tidy one.
- **What it changed:** materially. On d1 `all_numeric:platt`, Brier
  0.3537 → **0.2782** and skill −0.334 → **−0.0626**. Reporting the defective run
  would have understated how much calibration actually helped, and would have
  blamed the data for my arithmetic.

### 12.2 Three of my own tests failed on first run

- **One wrong contract reference.** I asserted `HorizonStatus` has a
  `DISAGREEMENT` member; it does not — disagreement lives on `HorizonAgreement`.
  Replaced with a stronger assertion: `HorizonIntelligenceResult` **raises** if a
  preferred horizon is set while the horizons disagree.
- **Two from an under-powered fixture.** My §45 "overconfident" fixture inflated
  the logit by 3×, and the model still **beat** the base-rate Brier
  (skill +0.062). Real discrimination buys a lot of slack in the Brier score.
  Raised to 12× to reach the regime the scenario is about.
- **Status:** resolved. Worth recording because the fixture failure is itself
  informative: it means the real model's Brier being *far worse* than base rate
  (−0.36 mean) is strong evidence its discrimination is near zero.

### 12.3 Discrimination was absent, which was the question

- **Symptom:** ROC-AUC across all 36 fold-evaluations ranges **0.3901 to
  0.6163**, mean **0.5136**, and is above the 0.5 no-skill point in **exactly 18
  of 36** — a coin flip, precisely.
- **Root cause:** F.6B's six "baseline-majority wins" were **not discrimination.**
  They were an artifact of comparing a model against a constant baseline in folds
  whose class balance swings 31%–64%. When the realised direction flips relative
  to the training majority, both model and baseline score badly and the rule only
  asks which is higher.
- **Fix:** none possible within this plan. This *is* the result.
- **Status:** the phase's primary finding.

### 12.4 Calibration improved the probabilities and could not rescue them

- **Symptom:** every arm's mean Brier skill is negative: raw **−0.362**, isotonic
  **−0.145**, Platt **−0.100**.
- **Root cause:** calibrating a model with no discrimination collapses its
  probabilities toward the base rate. That is the correct behaviour and it
  produces a model which is better calibrated and still has no skill — the §47
  scenario, observed on real data rather than only in a fixture.
- **Visible mechanism:** Platt *reduced* mean ROC-AUC (0.533 → 0.492) and mean
  balanced accuracy (0.521 → 0.508). Shrinking toward the base rate moves the 0.5
  crossing, so thresholded classification degrades even as reliability improves.
  That is why the Platt arms failed at `FAILED_ABSOLUTE_QUALITY` while raw and
  isotonic failed later at `FAILED_CALIBRATION`.
- **Status:** expected, measured, reported.

### 12.5 Calibration slope estimates are unstable on these windows

- **Symptom:** residual calibration slopes on the test windows read 1.77, 5.39,
  −3.42, −0.43, −7.88, 2.95 across folds.
- **Root cause:** 42–159 rows is not enough to estimate a slope stably, and a
  slope of −7.88 is noise, not a finding.
- **Fix:** none. Reported with its sample size and **not** used by any gate.
- **Status:** a stated limitation (§39).

### 12.6 One fold produced positive Brier skill, and it is the weakest fold

- **Symptom:** `d3:*:raw` `wf4` has skill **+0.1054** — the only positive value.
- **Root cause:** it is the smallest test window in the experiment, **n = 42**,
  and it appears twice because two configurations fingerprint identically there.
- **Fix:** none. The strict-majority rule correctly refused to let 1 of 3 folds
  carry a candidate, and the fingerprint rule correctly refused to count it twice.
- **Status:** correct behaviour, recorded so it cannot later be quoted as
  encouragement.

### 12.7 Deliberate non-actions

- I did **not** move the holdout to rescue d5 or d10.
- I did **not** remove `dense_only` despite F.6B's duplication finding, because
  the audit showed it was not degenerate under this geometry.
- I did **not** add beta calibration, a fourth arm, or any new family after
  seeing the arms fail.
- I did **not** relax the no-skill floors after seeing AUC sit on 0.5.
- I did **not** test abstention, which would have meant inventing a threshold.
- I did **not** regenerate F.6B's results artifact.

---

## 13. What was deferred

| Deferred | Why |
|---|---|
| **expected-return magnitude model** | P(up) is not a magnitude; needs its own experiment |
| **target model** | requires a qualified magnitude model |
| **model promotion** | zero qualified candidates; no code path exists here |
| **I-G shadow validation** | nothing to shadow |
| **I-H paper integration** | not authorised here, nothing to authorise |
| **d5** | 2 valid folds against a minimum of 3 |
| **d10** | zero holdout rows |
| **intraday ×4** | no pre-registered intraday purge policy |
| **abstention / coverage-vs-quality** | needs its own pre-registration |
| **further model families** | needs a new hypothesis and pre-registration |
| **D20** | **sealed** |
| **IBKR operational tasks** | October billing validation and the DU guard decision are outside this experiment |

---

## 14. Candidate inventory

All 12 frozen cells. Means are over the 3 valid folds.

| Cell | Config | Arm | Folds | Mean AUC | Mean bal.acc | Mean Brier | Mean skill | Baseline wins | **Verdict** |
|---|---|---|---|---|---|---|---|---|---|
| d1:all_numeric:raw | all | raw | 3 | 0.5402 | 0.5277 | 0.3736 | **−0.4101** | 2/3 | **FAILED_CALIBRATION** |
| d1:all_numeric:platt | all | platt | 3 | 0.5186 | 0.5015 | 0.2782 | −0.0626 | 2/3 | **FAILED_ABSOLUTE_QUALITY** |
| d1:all_numeric:isotonic | all | isotonic | 3 | 0.5347 | 0.5079 | 0.2978 | −0.1333 | 2/3 | **FAILED_CALIBRATION** |
| d1:dense_only:raw | dense | raw | 3 | 0.5496 | 0.5170 | 0.3800 | **−0.4332** | 2/3 | **FAILED_CALIBRATION** |
| d1:dense_only:platt | dense | platt | 3 | 0.5280 | 0.5092 | 0.2796 | −0.0677 | 2/3 | **FAILED_ABSOLUTE_QUALITY** |
| d1:dense_only:isotonic | dense | isotonic | 3 | 0.5376 | 0.5069 | 0.2944 | −0.1211 | 2/3 | **FAILED_CALIBRATION** |
| d3:all_numeric:raw | all | raw | 3 | 0.5170 | 0.5155 | 0.3323 | −0.3163 | **0/3** | **FAILED_BASELINE + FAILED_ABSOLUTE_QUALITY** |
| d3:all_numeric:platt | all | platt | 3 | 0.4630 | 0.5142 | 0.2867 | −0.1340 | **0/3** | **FAILED_BASELINE + FAILED_ABSOLUTE_QUALITY** |
| d3:all_numeric:isotonic | all | isotonic | 3 | 0.5025 | 0.4984 | 0.2927 | −0.1576 | **0/3** | **FAILED_BASELINE + FAILED_ABSOLUTE_QUALITY** |
| d3:dense_only:raw | dense | raw | 3 | 0.5237 | 0.5240 | 0.3255 | −0.2897 | 1/3 | **FAILED_BASELINE + FAILED_ABSOLUTE_QUALITY** |
| d3:dense_only:platt | dense | platt | 3 | 0.4563 | 0.5079 | 0.2875 | −0.1368 | 1/3 | **FAILED_BASELINE + FAILED_ABSOLUTE_QUALITY** |
| d3:dense_only:isotonic | dense | isotonic | 3 | 0.4921 | 0.5039 | 0.2949 | −0.1665 | 1/3 | **FAILED_BASELINE + FAILED_ABSOLUTE_QUALITY** |

Pooled test clusters per cell: 440 (d1), 436 (d3) — the sample gate never fired.

---

## 15. Discrimination results

Per fold, all 36 fold-evaluations.

| Cell | wf2 AUC / bal.acc | wf3 AUC / bal.acc | wf4 AUC / bal.acc |
|---|---|---|---|
| d1:all_numeric:raw | 0.5883 / 0.5793 | **0.4372** / **0.4511** | 0.5953 / 0.5527 |
| d1:all_numeric:platt | 0.5883 / 0.5046 | 0.5628 / **0.5000** | **0.4047** / **0.5000** |
| d1:all_numeric:isotonic | 0.5820 / 0.5296 | **0.4789** / **0.4884** | 0.5432 / 0.5056 |
| d1:dense_only:raw | 0.6163 / 0.5472 | **0.4372** / **0.4511** | 0.5953 / 0.5527 |
| d1:dense_only:platt | 0.6163 / 0.5275 | 0.5628 / **0.5000** | **0.4047** / **0.5000** |
| d1:dense_only:isotonic | 0.5906 / 0.5267 | **0.4789** / **0.4884** | 0.5432 / 0.5056 |
| d3:all_numeric:raw | **0.4711** / **0.4989** | **0.4700** / 0.5179 | 0.6099 / 0.5296 |
| d3:all_numeric:platt | 0.5289 / 0.5417 | **0.4700** / 0.5008 | **0.3901** / **0.5000** |
| d3:all_numeric:isotonic | **0.4992** / **0.4974** | **0.4849** / **0.4979** | 0.5235 / **0.5000** |
| d3:dense_only:raw | **0.4912** / 0.5244 | **0.4700** / 0.5179 | 0.6099 / 0.5296 |
| d3:dense_only:platt | 0.5088 / 0.5228 | **0.4700** / 0.5008 | **0.3901** / **0.5000** |
| d3:dense_only:isotonic | **0.4680** / 0.5139 | **0.4849** / **0.4979** | 0.5235 / **0.5000** |

Bold marks a value at or below its no-skill point.

| | |
|---|---|
| ROC-AUC range | **0.3901 – 0.6163** |
| ROC-AUC mean | **0.5136** |
| **above 0.5** | **18 of 36** |
| balanced accuracy range | 0.4511 – 0.5793 |
| balanced accuracy mean | **0.5112** |
| above 0.5 | 20 of 36 |

**Eighteen of thirty-six is a coin flip, exactly.** In `d1 wf3` the ranking is
*inverted* (AUC 0.437). There is no stable directional discrimination here.

---

## 16. Absolute quality results

This is where F.6B's weakness is repaired, and the repair changes the conclusion.

**F.6B credited six logistic cells with baseline-majority wins** (3/5, 3/4, 2/3)
and called the family promising. **F.6C's absolute floor shows those wins were
not skill:**

| | F.6B's view | F.6C's measurement |
|---|---|---|
| evidence of direction | 6 cells beat the baselines in a majority of folds | mean ROC-AUC **0.5136**, above no-skill in exactly half the folds |
| what the baseline comparison measured | model vs constant predictor | which of two poor predictors scored higher in a fold whose class balance had flipped |
| absolute quality | not assessed | **mean balanced accuracy 0.5112** against a 0.5 no-skill point |

A concrete illustration from F.6B, reproduced as a test here: an always-up
predictor on a 23.5%-positive window scores raw accuracy **0.235** and balanced
accuracy **exactly 0.500**. Relative victory over a constant baseline is not
evidence of skill when the class balance moves.

**Nothing in this phase clears the absolute floor by a meaningful margin.** The
best mean balanced accuracy of any cell is **0.5277** (d1:all_numeric:raw), on
three folds, with one of the three below 0.5.

---

## 17. Calibration results — raw versus calibrated

| Arm | Mean Brier | Mean skill | Mean ECE | Mean AUC | Mean bal.acc |
|---|---|---|---|---|---|
| raw | 0.3529 | −0.3623 | 0.2898 | 0.5326 | 0.5210 |
| **platt** | **0.2830** | **−0.1003** | **0.1831** | 0.4915 | 0.5082 |
| isotonic | 0.2949 | −0.1446 | 0.2029 | 0.5167 | 0.5043 |

Worst individual raw miscalibration: `d1:dense_only:raw` `wf2`, Brier **0.5263**
against a base-rate Brier of 0.2785 — skill **−0.8902**, ECE **0.5933**. Platt
brought the same fold to Brier **0.2845**, skill **−0.0218**, ECE **0.2725**.

So: **calibration cut a catastrophic miscalibration down to roughly the base
rate, and the base rate is not a trading edge.** That is what calibrating a
non-discriminating model does, and it is the clearest possible demonstration that
calibration is not a rescue.

**Positive Brier skill: 2 of 36 fold-evaluations** — `d3:all_numeric:raw` and
`d3:dense_only:raw` at `wf4`, skill **+0.1054**, **n = 42**, and they share
fingerprint `45dddece5017387d`, so they are **one observation**.

---

## 18. Reliability

ECE was **COMPUTED** for all 36 fold-evaluations — every one had at least one
probability bin reaching the 10-observation minimum.

| | |
|---|---|
| ECE range | 0.0770 – 0.5933 |
| mean ECE, raw | **0.2898** |
| mean ECE, platt | **0.1831** |
| mean ECE, isotonic | 0.2029 |
| bins | 5, with support counts recorded per bin |

No ten-bin curve was produced on thin data: `expected_calibration_error`
excludes bins below 10 observations and returns `NOT_COMPUTED` with a reason when
none qualifies. `NOT_COMPUTED` is distinct from `0.0`, which would mean perfect
calibration.

---

## 19. Abstention

**NOT TESTED.**

Pre-registered as out of scope. Testing it would have required either inventing a
probability band — the thing the no-skill floors exist to prevent — or spending
budget learning one, enlarging an experiment whose question is narrower.
Coverage-versus-quality needs its own pre-registration.

Not zero, not unavailable, not quietly omitted.

---

## 20. Pre-holdout survivors

```
[]
```

**Zero.** The survivor list was hashed and persisted
(`4f53cda18c2baa0c…`, the SHA-256 of the empty list) **before** the holdout
decision point, so the set that earned a holdout look is on the record even
though it is empty.

---

## 21. Holdout

| | |
|---|---|
| **opened?** | **NO** |
| survivors | **0** |
| rows available | d1 **154**, d3 **143** |
| results | **none — the holdout was never read** |

Stated explicitly, as required: **the holdout was not opened.** No model has been
fitted against data at or after `2026-08-18T00:00:00Z` by F.6B or F.6C. Every
candidate's `holdout` field is `null`, and a test asserts it.

---

## 22. Qualification gates

Every candidate × every gate.

| Cell | Sample | Discrimination (baseline) | Absolute quality | Robustness | Calibration | Holdout | **Qualified** |
|---|---|---|---|---|---|---|---|
| d1:all_numeric:raw | **PASS** 440 | **PASS** 2/3 | **PASS** AUC 2/3, bal 2/3 | **PASS** | **FAIL** 0/3 | NOT_REACHED | **NO** |
| d1:all_numeric:platt | **PASS** 440 | **PASS** 2/3 | **FAIL** bal 1/3 | NOT_REACHED | NOT_REACHED | NOT_REACHED | **NO** |
| d1:all_numeric:isotonic | **PASS** 440 | **PASS** 2/3 | **PASS** | **PASS** | **FAIL** 0/3 | NOT_REACHED | **NO** |
| d1:dense_only:raw | **PASS** 440 | **PASS** 2/3 | **PASS** | **PASS** | **FAIL** 0/3 | NOT_REACHED | **NO** |
| d1:dense_only:platt | **PASS** 440 | **PASS** 2/3 | **FAIL** bal 1/3 | NOT_REACHED | NOT_REACHED | NOT_REACHED | **NO** |
| d1:dense_only:isotonic | **PASS** 440 | **PASS** 2/3 | **PASS** | **PASS** | **FAIL** 0/3 | NOT_REACHED | **NO** |
| d3:all_numeric:raw | **PASS** 436 | **FAIL** 0/3 | **FAIL** AUC 1/3 | NOT_REACHED | NOT_REACHED | NOT_REACHED | **NO** |
| d3:all_numeric:platt | **PASS** 436 | **FAIL** 0/3 | **FAIL** AUC 1/3 | NOT_REACHED | NOT_REACHED | NOT_REACHED | **NO** |
| d3:all_numeric:isotonic | **PASS** 436 | **FAIL** 0/3 | **FAIL** AUC 1/3 | NOT_REACHED | NOT_REACHED | NOT_REACHED | **NO** |
| d3:dense_only:raw | **PASS** 436 | **FAIL** 1/3 | **FAIL** AUC 1/3 | NOT_REACHED | NOT_REACHED | NOT_REACHED | **NO** |
| d3:dense_only:platt | **PASS** 436 | **FAIL** 1/3 | **FAIL** AUC 1/3 | NOT_REACHED | NOT_REACHED | NOT_REACHED | **NO** |
| d3:dense_only:isotonic | **PASS** 436 | **FAIL** 1/3 | **FAIL** AUC 1/3 | NOT_REACHED | NOT_REACHED | NOT_REACHED | **NO** |

### Gate funnel

```
12  cells evaluated
 0  failed the sample gate
 6  stopped at discrimination / absolute quality   (all six d3 cells)
 2  stopped at absolute quality alone              (both platt arms on d1)
 4  reached calibration and failed it              (raw and isotonic on d1)
 0  survived the pre-holdout gates
 0  shown the holdout
 0  QUALIFIED
```

Four cells reached the calibration gate — the furthest anything travelled — and
all four failed it with Brier skill above zero in **0 of 3** folds.

**`canonically_qualified` is also 0 for all 12 cells**, because the canonical gate
is evaluated on the holdout and the holdout was never opened. F.6C therefore
qualified nothing the canonical gate would have accepted and nothing it would
have rejected — it never got that far, which is the correct consequence of the
pre-registered sequence.

---

## 23. Qualified candidates

**Zero.**

No candidate reached the holdout, so none can be described as qualified, nearly
qualified, or usable. The two cells that travelled furthest —
`d1:all_numeric:isotonic` and `d1:dense_only:isotonic`, which passed sample,
discrimination, absolute quality and robustness — failed calibration with skill
above zero in **0 of 3** folds. Per §51 of the brief they are **strongest
rejected candidates**, not a "best model".

---

## 24. Negative knowledge

Persisted in `research_candidate_results` (24 F.6C rows across the invalidated and
valid executions, queryable by horizon and qualification) and summarised so no
future phase repeats it blindly.

### Carried forward from F.6B, unchanged

1. Ridge regression on these 26 features does not beat the historical-mean
   baseline for abnormal return on any daily horizon — 34 of 34 fold losses.
2. Raw logistic probabilities are badly miscalibrated.
3. The `dense_only` configuration was degenerate **under F.6B's geometry**.
4. Baseline-relative victory alone can credit very poor absolute accuracy.
5. d10 has an empty holdout under this boundary.

### New from F.6C

6. **The logistic family's apparent directional edge was not discrimination.**
   Mean ROC-AUC **0.5136**, above no-skill in exactly 18 of 36 fold-evaluations,
   range 0.390–0.616. F.6B's six baseline-majority wins were an artifact of
   class-balance swings, not skill. **Do not re-run this specification expecting
   direction from a different evaluation.**
7. **Calibration cannot rescue it, and that is now measured rather than assumed.**
   Platt cut excess Brier 72% and ECE 37% and every arm still has negative skill.
   A calibrator applied to a non-discriminating model collapses it toward the base
   rate — and *lowers* AUC and balanced accuracy while doing so.
8. **The next question is not calibration.** F.6B's result pointed at calibration;
   F.6C shows the problem is upstream of it. Candidate directions, none of which
   may be pursued inside this phase: whether the 26-feature representation
   carries directional information at all; whether direction is
   regime-conditional; whether the binary target at a zero threshold is the right
   question.
9. **`dense_only` is not generally degenerate** — it differs in 2 of 6 folds under
   a three-window geometry. The duplication was geometry-dependent.
10. **A three-window split costs about two weeks of research region.** On this
    dataset that is the difference between four researchable horizons and two.
11. **Calibration slope cannot be estimated stably on 42–159 rows** — observed
    values spanned −7.88 to +5.39.
12. **The calibration window's base rate diverges from the test window's by up to
    33 percentage points** on this data, which makes calibration intrinsically
    hard here regardless of method.

---

## 25. Confidence

Reported as four distinct quantities, never conflated:

| | |
|---|---|
| **raw model probability** | produced; badly miscalibrated (mean ECE 0.290, worst 0.593) |
| **calibrated probability** | produced; materially better (mean ECE 0.183 under Platt) and still without skill |
| **calibration quality** | **FAILED** on every cell — Brier skill above zero in 0 of 3 folds for all four cells that reached the gate |
| **canonical signal confidence** | **never computed.** It requires a qualified model; there is none |

The canonical confidence formula (`base × quality × agreement × sample`) is
**unchanged and was never reached**. No confidence value was produced and none was
inflated.

**No genuinely independent qualified specification exists.** Had any qualified,
the independence accounting was fixed in advance and would not have been
flattering: the three calibration arms share one model fit per (horizon, config,
fold) and are **not** three models; and two feature configurations that fingerprint
identically in a fold are **not** two observations.

---

## 26. Expected return

**UNAVAILABLE** — and by design, not by failure.

**P(price up) is not an expected return.** A directional probability carries no
magnitude. Even a fully qualified directional model here would have reported
`expected_return_capable = False`, because the magnitude model that would be
required does not exist — F.6B's ridge result is precisely the finding that it
does not.

The recorded result carries `expected_return_capable: false`, and a test asserts
it.

---

## 27. Target capability

**UNAVAILABLE.**

**0 of 301 instruments** and **0 of 2 researched horizons** can receive a
defensible target. No numeric target may be produced from a directional
probability alone; `target_capable: false` is recorded and asserted.

---

## 28. Horizon capability

| | Count |
|---|---|
| directional-capable horizons | **0** |
| preferred horizons | **0** |
| horizon disagreements | **0** (disagreement needs ≥ 2 capable horizons) |
| no-preference cases | **all** |

The disagreement contract was verified by fixture rather than by data:
`HorizonIntelligenceResult` **raises** if a preferred horizon is set while
`HorizonAgreement.DISAGREEMENT` holds, so a majority vote cannot overwrite a
directional disagreement.

---

## 29. F.5 before / after

| | Before (v1) | After (v1) | Before (v2) | After (v2) |
|---|---|---|---|---|
| instruments evaluated | 244 | **244** | 301 | **301** |
| qualified models | 0 | **0** | 0 | **0** |
| `model_not_qualified` | 244 | **244** | 0 | **0** |
| `no_model_available` | 0 | **0** | 301 | **301** |
| signal-capable horizons | 0 | **0** | 0 | **0** |
| directional-capable horizons | 0 | **0** | 0 | **0** |
| expected-return-capable horizons | 0 | **0** | 0 | **0** |
| target-capable horizons | 0 | **0** | 0 | **0** |
| `valid_no_signal` | 0 | **0** | 0 | **0** |
| capability gaps | 244 | **244** | 301 | **301** |
| shadow signals | 0 | **0** | 0 | **0** |
| preferred horizons | 0 | **0** | 0 | **0** |
| disagreements | 0 | **0** | 0 | **0** |

**Unchanged on both snapshots**, as it must be: nothing was promoted, so the
production diagnostic cannot move. Had it moved, that would be evidence of a bug.

`valid_no_signal` remains **0** and that is the correct value. It requires a
qualified model that looked and found no edge. There is still no qualified model,
so every instrument remains a **capability gap** — and F.5's taxonomy keeps the
two distinct, which is what it was built for.

---

## 30. As-of / leakage

| | |
|---|---|
| train bounds | region start → `train_end`, per fold, expanding |
| calibration bounds | `calibration_start` → `calibration_end`, 7 days, strictly after train + purge |
| validation (test) bounds | `test_start` → `test_end`, 7 days, strictly after calibration + purge + embargo |
| holdout | `2026-08-18T00:00:00Z` onward — **never read** |
| purge | **both** internal boundaries: d1→3, d3→5 calendar days |
| embargo | 6 days, before the test window only |
| base rate | from `TRAIN ∪ CALIBRATION` only — never from test outcomes |
| future-label exclusion | asserted: no fold's train, calibration or test index has a cutoff at or after the boundary |
| physical truncation | the holdout split's train and calibration indices are asserted strictly before the boundary, and its test indices strictly at or after |

Tests assert, for every fold of both horizons: train precedes calibration
precedes test; purge is applied at the train/calibration boundary; no window
reaches into the holdout; and fold construction is deterministic across repeated
calls.

**Nothing was fitted on the data used to score it.** The calibrator sees only the
calibration window; the test window fits nothing; the holdout fits nothing and
was never opened.

---

## 31. Pre-registration integrity

| | |
|---|---|
| changed after results? | **NO** — sha256 `ba141fd9…` matches the value pinned in the harness, which refuses to run otherwise |
| candidate budget expanded? | **NO** — 12, enforced by a check that raises |
| calibration methods added after results? | **NO** — three arms, one a control |
| feature config added after results? | **NO** — and none removed, despite F.6B's duplication finding |
| holdout changed after results? | **NO** — inherited, and the rule re-derives it |
| quality threshold changed after results? | **NO** — the no-skill points are metric properties |
| second round run? | **NO** — a completed execution refuses a rerun |
| F.6B artifacts changed? | **NO** — verified before the run |

Every answer is NO. The scientific result stands.

One qualification stated plainly: **the experiment was executed twice** — once
with a defective optimiser and once after fixing it. The first execution is
recorded as `invalidated` with its reason and was not used as evidence. That is
the integrity rule for implementation defects (§12.1), not a second attempt at a
result.

---

## 32. D20 status

| | |
|---|---|
| executed? | **NO** |
| inspected? | **NO** |
| inferred? | **NO** |
| consumed? | **NO** |
| trained on? | **NO** |
| calibrated on? | **NO** |
| used for thresholding? | **NO** |
| used for model selection? | **NO** |
| used for horizon selection? | **NO** |
| hypothesis changed? | **NO** |
| anchor-v2 changed? | **NO** |
| protected window changed? | **NO** |

`DEFERRED["d20"] = "PROTECTED"`, absent from `HORIZONS`, and `rs.is_protected`
agrees. Tests assert neither the harness nor the calibration module contains
`d20.abnormal_return`, `d20.raw_return`, `validate_d20_reversal`, `anchor_v2`,
`return_60d`, `check_d20_readiness` or `value_json` — so no protected label value
can be read at all.

---

## 33. Model promotion

```
models promoted = 0
```

No promotion occurred and no code path in this phase could perform one. A test
asserts the harness contains no `promote_model` or `PRODUCTION_ACTIVE`, and that
`trained_models WHERE status='active'` is 0.

---

## 34. Production behaviour

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

**0 files modified.** The production database `data/marketlens.db` was never
opened for writing; a test asserts it holds 581 observations and no
`research_executions` table.

---

## 35. Broker safety

```
broker writes attempted:       0
broker writes succeeded:       0
paper orders:                  0
live orders:                   0
billable market-data requests: 0
```

No `OrderIntent` created, no IBKR connection opened, no subscription touched.
Tests assert the harness imports no execution, broker, portfolio, risk, capture
or network module, and that the calibration module imports only `math`,
`dataclasses`, `typing` and `__future__`.

---

## 36. Test results

### Full suite

| | Baseline `83948af` | **Final** |
|---|---|---|
| Tests | 5,334 | **5,405** |
| Pass | 5,333 | **5,404** |
| Fail / Errors | 0 / 0 | **0 / 0** |
| Skipped | 1 | **1** |
| Runtime | 1074.567 s | **994.320 s** |

```
PYTHONPATH=src python -m unittest discover -s tests -t . -b
Ran 5405 tests in 994.320s
OK (skipped=1)
```

**+71 tests, exactly the new file. Zero regressions.**

### Targeted and requested suites

| Suite | Tests | Pass | Fail | Err | Runtime |
|---|---|---|---|---|---|
| **F.6C — `test_f6c_calibration`** | **71** | 71 | 0 | 0 | 6.4 s |
| F.6B — `test_f6b_frozen_experiment` | 59 | 59 | 0 | 0 | 3.2 s |
| F.7 — `test_corpus_recovery` | 71 | 71 | 0 | 0 | 42.7 s |
| F.7 — `test_research_snapshot_integrity` | 55 | 55 | 0 | 0 | 0.6 s |
| F.7 — `test_recovery_boundary` | 30 | 30 | 0 | 0 | 11.7 s |
| F.6 — `test_sample_adequacy` / `test_research_discipline` | 24 / 33 | all | 0 | 0 | 0.1 / 6.9 s |
| adaptive (F.6C + F.6B + F.7 + F.6 + F.5 + I-F + I-E + I-D + I-C + I-B) | **1,061** | 1,061 | 0 | 0 | 114.8 s |
| modeling | 142 | 142 | 0 | 0 | 3.2 s |
| signals | 129 | 129 | 0 | 0 | 9.3 s |
| research / research integrity | 104 | 104 | 0 | 0 | 31.0 s |
| experiments (Experiment Engine) | 83 | 83 | 0 | 0 | 15.8 s |
| portfolio | 273 | 273 | 0 | 0 | — |
| trading (readiness, live safety, Risk) | 224 | 224 | 0 | 0 | — |
| capture / session | 126 | 126 | 0 | 0 | — |
| **full suite** | **5,405** | **5,404** | **0** | **0** | **994.3 s** |

---

## 37. Compute / performance

| | |
|---|---|
| candidate cells | **12** |
| **model fits** | **12** (3 folds × 2 configs × 2 horizons; the three arms share each fit) |
| **calibrator fits** | **24** (12 fits × 2 fitted methods; `raw` fits nothing) |
| **holdout model fits** | **0** — no survivor earned one |
| runtime, end to end | **9.58 s** |
| plan runtime (read-only) | ~6 s |
| failures / retries | 0 / 0 |
| storage growth | **+~200 KB** in the snapshot database (2 markers + 24 candidate rows) |
| result artifact | `docs/PHASE_25_9I_F6C_RESULTS.json`, 184 KB |
| paid API / LLM / network calls | **0 / 0 / 0** |

Nine and a half seconds, because the three calibration arms share one model fit
and the gates stopped eight of twelve cells before calibration.

---

## 38. Scope-creep review

| | |
|---|---|
| Dataset changes | **none** — read `mode=ro`; only markers and candidate rows written |
| F.6B artifact changes | **none** — verified before the run and asserted by test |
| Gate changes | **none** — `is_deployable` referenced, never redefined; F.6C only adds requirements |
| Threshold changes | **none** — the no-skill points are metric properties; a test forbids assigning a numeric literal to `MIN_EFFECTIVE_SAMPLE` or `MIN_VALID_FOLDS` |
| Model-family changes | **none** — logistic only, Phase 25.9's `C1` verbatim; no family added after seeing failures |
| Model promotion | **none** |
| Production changes | **none** — 0 files modified |
| IBKR | **untouched**, 0 requests |
| D20 | **untouched** |
| New dependencies | **none** — the calibration module imports only `math`, `dataclasses`, `typing`, `__future__` |

---

## 39. Known limitations

1. **Two horizons only.** The three-window split costs ~2 weeks of research
   region, so d5 (2 folds) and d10 (0 holdout rows) were excluded. This phase
   says nothing about them.
2. **Three folds per horizon** is the canonical minimum, not a comfortable
   margin. Test windows range 42–163 rows.
3. **Calibration slope is unstable** on these windows (−7.88 to +5.39) and is
   reported as diagnostic only, never gated on.
4. **The calibration and test base rates diverge by up to 33 points**, which
   makes calibration intrinsically hard here regardless of method.
5. **One model family, one specification.** Nothing non-linear, and no
   hyperparameter variation; the phase deliberately did not search.
6. **26 numeric features, no categoricals.** `event_type`, ticker and exchange
   are excluded rather than arbitrarily encoded, so any signal carried by event
   type was invisible.
7. **Abstention untested** (§19).
8. **The binary target is direction at a zero threshold.** Whether that is the
   right question is itself a hypothesis, untested here.
9. **ECE used 5 bins.** A finer curve would need more data than any fold has.
10. **`expected_calibration_error` and the reliability bins are descriptive**;
    only Brier skill gates calibration.
11. **Isotonic can lower ROC-AUC** by creating ties (observed: 0.533 → 0.517
    mean). Platt cannot, being strictly monotone — but Platt with a near-zero
    fitted slope flattens probabilities enough to move the 0.5 crossing, which is
    why its balanced accuracy fell.

---

## 40. Readiness for I-G

**NO.**

| Requirement | State |
|---|---|
| ≥ 1 canonically qualified directional candidate | **0** |
| trustworthy out-of-sample calibration | **none** — 0 of 12 cells passed |
| untouched final holdout success | **not applicable** — the holdout was never opened |
| reproducible shadow predictions | **none** — no model to predict with |
| complete lineage | present, for failures |
| no unresolved integrity blocker | the open items in §39 are documented limitations |

| | |
|---|---|
| expected-return capability exists? | **NO** |
| target capability exists? | **NO** |

**No I-G trading handoff was created.** The honest output is:

> **RESEARCH FOLLOW-UP REQUIRED.**

And the follow-up is **not** another calibration attempt. F.6B's result pointed at
calibration; F.6C shows the problem is upstream of it — there is no
discrimination to calibrate. The candidate next hypotheses are in §24.8, and each
needs its own pre-registration.

---

## 41. Readiness for I-H

**NO.**

Paper trading is not authorised by this phase and nothing in it moves toward
authorisation. The hard separation stands: **research qualified ≠ shadow
validated ≠ paper authorised ≠ live authorised.** F.6C ends before the first.

---

## 42. Final verdict

> ## PHASE 25.9I-F.6C IMPLEMENTATION = COMPLETE
>
> ## MODEL QUALIFICATION OUTCOME = NONE QUALIFIED
>
> ## CALIBRATION OUTCOME = NONE PASSED
>
> ## I-G READINESS = NO

The plan was frozen at `2f1977f` — a commit containing the pre-registration and
no harness, no results and no marker — with its hash pinned inside the harness so
that editing it afterwards breaks the run. Horizon adequacy was measured from
geometry before freezing, which excluded d5 on folds and d10 on an empty holdout
and cost this phase half its horizons; the boundary was inherited unchanged from
F.7 and was not moved to recover either. All 12 cells were evaluated in 9.58 s,
and **0 survived the pre-holdout gates, so the holdout was never opened.**

**The finding is that F.6B's directional signal was not discrimination.** ROC-AUC
across 36 fold-evaluations spans 0.390–0.616, means **0.5136**, and clears its
no-skill point in **exactly 18 of 36** — a coin flip. Balanced accuracy means
**0.5112** against a 0.5 floor. F.6B's six baseline-majority wins were an artifact
of comparing a model with a constant baseline across folds whose positive rate
swings from 31% to 64%; the absolute-quality floor, whose thresholds are the
metrics' own no-skill points rather than chosen numbers, removes that artifact.

**Calibration worked and could not help.** Platt cut mean excess Brier by 72%
(skill −0.362 → −0.100) and mean ECE by 37%, taking the worst fold from Brier
0.526 down to 0.285 — and every arm remains below zero skill, because calibrating
a model without discrimination collapses it toward the base rate, visibly
*lowering* AUC (0.533 → 0.492) and balanced accuracy as it does so. Exactly 2 of
36 fold-evaluations had positive Brier skill, on the smallest window (n = 42), and
the fingerprint rule shows they are one observation rather than two.

**One defect was found by distrusting a result**: Platt reported NOT_FITTED on 8
of 12 folds because my Newton iteration initialised at slope = 1 and saturated on
±14 logits. Fixed with null-model initialisation and damped Newton, the defective
execution marked `invalidated` with its reason and kept on the record, and the
experiment re-run. The fix mattered — it is the difference between reporting
calibration as broken and reporting it as effective but insufficient.

**D20 remains sealed.** Models promoted 0. Broker writes, paper orders, live
orders, billable market-data requests: **0 / 0 / 0 / 0**. Portfolio, Risk,
OrderIntent, Execution, capture, IBKR and subscriptions: unchanged, with 0 files
modified. F.6B stands exactly as recorded, verified before the run.

**The next question is not calibration.** It is whether this feature
representation carries directional information at all — and that needs a new
hypothesis, a new pre-registration, and no more passes at this one.
