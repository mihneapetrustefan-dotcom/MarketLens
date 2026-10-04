# Phase 25.9I-F.6B — Completion Report

> **THE EXPERIMENT IS DEFINED BEFORE THE RESULT IS KNOWN.**
>
> **NO MODEL QUALIFYING IS A VALID RESULT.**

Written 2026-10-04. Architecture: `docs/PHASE_25_9I_F6B_ARCHITECTURE.md`.
Frozen plan: `docs/PHASE_25_9I_F6B_PREREGISTRATION.md` (sha256
`9150b988993dcac3…`) and `docs/PHASE_25_9I_F6B_HANDOFF.json`, both produced by
Phase 25.9I-F.7 at `209da23` **before** any candidate existed. Raw results:
`docs/PHASE_25_9I_F6B_RESULTS.json`.

---

## 1. Phase implementation status

**COMPLETE.**

The frozen experiment was verified, enumerated, executed once, and recorded. All
16 pre-registered cells were evaluated.

---

## 2. Model qualification outcome

**NONE QUALIFIED.**

More precisely, and the precision matters: **no candidate survived the research
region, so the holdout was never opened.** The pre-registered rule — *if no
candidate survives the research region, the holdout is not opened* — fired as
written.

**This is not evidence that no edge exists.** It is evidence that these four
specifications, on this dataset, do not clear the canonical gates.

---

## 3. F.7 frozen handoff

Authoritative values, read from the repository and verified against the artifacts
they name. Every check passed before anything was fitted.

| | |
|---|---|
| F.7 final commit | **`209da23`** |
| corpus version | **`canonical_corpus_v2`** |
| corpus hash | `4247f543904204c9d60f86b9ab6858e3bd3bd9fcc635e5950c5b60e148cb2ddb` — **recomputes** |
| research snapshot | **`research_snapshot_v2`**, state `ELIGIBLE_FOR_RESEARCH` |
| snapshot hash | `4ccab384d37a9eb249ce2bd61b44692c77a0df3dc7121dc79c3ed6940e66b0bc` — **recomputes** |
| feature / label / universe / event version | `v1` each |
| as-of cutoff | `2026-08-30T14:35:00Z` |
| pre-registration | `docs/PHASE_25_9I_F6B_PREREGISTRATION.md` |
| pre-registration sha256 | `9150b988993dcac3789b99140eaec782df60e816c8fa77fd25be0d4f18087625` — **matches the handoff** |
| eligible horizons | **d1, d3, d5, d10** |
| deferred horizons | intraday_5m / 15m / 30m / 60m |
| **holdout boundary** | **`2026-08-18T00:00:00Z`** |
| holdout rule | final 20% of the information span, floored to the UTC day (Phase 25.9's fraction) |
| **rule re-derives the frozen boundary** | **YES** — recorded was not enough |
| purge (calendar days) | d1→3, d3→5, d5→7, d10→14 |
| embargo / test / step | 6 / 7 / 7 days |
| minimum valid folds | 3 |
| folds formed | **5 / 5 / 4 / 3** |
| candidate budget | **16 cells** |
| qualification gate | `src.domain.model_models.ModelEvaluation.is_deployable` |
| seed policy | none — every candidate is deterministic, so no seed can be searched |
| D20 | **excluded** |
| prior execution marker | **none** — verified before the run |

---

## 4. Git state

| | |
|---|---|
| Branch | `ibkr-paper-validation-fixes` |
| Baseline HEAD | **`209da23`** (F.7 completion report) |
| Final HEAD | **`1dc77b2`** |
| Working tree at start | clean |
| Commits | 1 |
| Diff | **5 files changed, 4,957 insertions(+), 17 deletions(-)** |
| **Files modified** | **1** |

### New files (4)

| File | Lines | Purpose |
|---|---|---|
| `scripts/research_f6b.py` | 1,129 | pre-flight, cell enumeration, two-stage execution, marker |
| `tests/adaptive/test_f6b_frozen_experiment.py` | 739 | 59 tests |
| `docs/PHASE_25_9I_F6B_ARCHITECTURE.md` | 453 | documentation |
| `docs/PHASE_25_9I_F6B_RESULTS.json` | 2,590 | the full fold-level record |

### Modified file (1)

| File | Change | Why |
|---|---|---|
| `scripts/research_multi_horizon.py` | +63/−17 | `build_folds` and `span_requirement` parameterised on the holdout boundary with F.6's constants as defaults, so there is still one fold builder; plus the `qualified`-key defect fixed (§12.1) |

No production module was modified. `data/marketlens.db` was never opened for
writing.

---

## 5. Baseline tests

**The pre-F.6B baseline is F.7's final full-suite run at the identical commit
`209da23` with a clean tree:**

```
PYTHONPATH=src python -m unittest discover -s tests -t . -b
Ran 5275 tests in 563.048s
OK (skipped=1)
```

| | |
|---|---|
| Tests | **5,275** |
| Pass | **5,274** |
| Fail / Errors | **0 / 0** |
| Skipped | **1** |
| Runtime | **563.048 s** |

**A re-run of the baseline was attempted at the start of this phase and was
stopped by the harness after reaching its background time limit**, with no
`Ran N tests` line produced. It would not have been usable even if it had
finished: it was still running while I edited `scripts/research_multi_horizon.py`
and added two new test files, so its result would have reflected a working tree
that changed underneath it. Reporting it as a baseline would have been fiction.

What is reported above is a genuine full-suite result at the exact baseline
commit with a clean tree. In addition, a fresh uncontaminated pre-execution check
was run on the current tree immediately before the experiment:

| Suite | Tests | Result | Runtime |
|---|---|---|---|
| adaptive | 990 | **OK** (skipped=9) | 166.0 s |
| modeling | 142 | **OK** | 5.6 s |
| signals | 129 | **OK** | 9.3 s |
| research | 104 | **OK** | 31.0 s |
| experiments | 83 | **OK** | 15.8 s |

---

## 6. Pre-research model state

Measured on both snapshots before the experiment.

| | v1 (production) | v2 (research snapshot) |
|---|---|---|
| model specifications | 1 (`ridge_abnormal_return:v1`) | **0** |
| **qualified models** | **0** | **0** |
| **promoted (status active)** | **0** | **0** |
| trained models | 2 | **0** |
| model evaluations | 2 | **0** |
| predictions / signals | 5 / 5 | **0 / 0** |
| signal-capable horizons | **0** | **0** |
| signals | **0** | **0** |
| defensible targets | **0** | **0** |
| usable preferred horizons | **0** | **0** |
| F.5 funnel exit | 244 × `model_not_qualified` | 301 × `no_model_available` |

The two reasons differ correctly: v1 holds a model that fails its gates; v2 holds
no model at all, by construction, because F.7 cleared v1's experiment artifacts
out of the new database so a stale evaluation could not be read as the new
snapshot's capacity.

Prior negative knowledge carried in: Phase 25.9 (d5, four candidates, none
qualified), F.6 (d1/d3/d5/d10, zero folds, INSUFFICIENT DATA).

---

## 7. Frozen research plan

```
TOTAL ALLOWED RESEARCH CELLS = 16
```

| Cell | Horizon | Family | Task | Features | Purge |
|---|---|---|---|---|---|
| d1:C0 | d1 | ridge_regression (α=1) | abnormal_return | all_numeric | 3 |
| d1:C1 | d1 | logistic_regression | direction | all_numeric | 3 |
| d1:C2 | d1 | ridge_regression (α=10) | abnormal_return | all_numeric | 3 |
| d1:C3 | d1 | logistic_regression | direction | dense_only | 3 |
| d3:C0 | d3 | ridge_regression (α=1) | abnormal_return | all_numeric | 5 |
| d3:C1 | d3 | logistic_regression | direction | all_numeric | 5 |
| d3:C2 | d3 | ridge_regression (α=10) | abnormal_return | all_numeric | 5 |
| d3:C3 | d3 | logistic_regression | direction | dense_only | 5 |
| d5:C0 | d5 | ridge_regression (α=1) | abnormal_return | all_numeric | 7 |
| d5:C1 | d5 | logistic_regression | direction | all_numeric | 7 |
| d5:C2 | d5 | ridge_regression (α=10) | abnormal_return | all_numeric | 7 |
| d5:C3 | d5 | logistic_regression | direction | dense_only | 7 |
| d10:C0 | d10 | ridge_regression (α=1) | abnormal_return | all_numeric | 14 |
| d10:C1 | d10 | logistic_regression | direction | all_numeric | 14 |
| d10:C2 | d10 | ridge_regression (α=10) | abnormal_return | all_numeric | 14 |
| d10:C3 | d10 | logistic_regression | direction | dense_only | 14 |

### Proof the plan preceded the result

1. The pre-registration and handoff were committed in **`209da23`** (2026-10-01),
   and this phase ran on 2026-10-04. Git history, not assertion.
2. The pre-registration's sha256 is recorded inside the handoff and both still
   match the file on disk — neither was edited after the fact.
3. `enumerate_cells()` builds the list from the handoff and **raises** if its
   length differs from the frozen budget. Two tests confirm that adding a horizon
   or inflating the budget breaks the check rather than producing cell 17.
4. The execution marker persists the full cell list at insert time, **before** any
   candidate is scored, alongside the pre-registration and snapshot hashes.
5. The four specifications are Phase 25.9's, imported verbatim from
   `research_model_quality.spec_for`. They predate F.6, F.7 and this phase.

---

## 8. Dataset / sample

| Horizon | Observations | Instruments | Clusters | Sessions | Research rows | Research clusters | Holdout rows | **Folds** |
|---|---|---|---|---|---|---|---|---|
| **d1** | 891 | 237 | 237 | 55 | 737 | 227 | 154 | **5** |
| **d3** | 878 | 237 | 237 | 53 | 735 | 227 | 143 | **5** |
| **d5** | 805 | 234 | 234 | 51 | 735 | 227 | 70 | **4** |
| **d10** | 633 | 211 | 211 | 45 | 633 | 211 | **0** | **3** |

Features: 26 numeric. Effective-sample floor: 30, cleared by 7.0–7.6× in every
research region. `FAILED_SAMPLE` never fired on any cell.

**d10's holdout region is empty**, and that was established at plan time before
any fit. See §12.2.

---

## 9. Walk-forward design

| Horizon | Folds | Purge | Embargo | Test | Step | Train rows (first → last) | Test rows per fold |
|---|---|---|---|---|---|---|---|
| d1 | 5 | 3 | 6 | 7 | 7 | expanding | 93–166 |
| d3 | 5 | 5 | 6 | 7 | 7 | expanding | 77–166 |
| d5 | 4 | 7 | 6 | 7 | 7 | expanding | 125–159 |
| d10 | 3 | 14 | 6 | 7 | 7 | expanding | 21–158 |

Expanding, day-granular, strictly inside the research region. Every fold's test
window ends at or before `2026-08-18T00:00:00Z`.

**Holdout semantics:** survivors only, one evaluation, once. Zero candidates
survived, so the holdout was **not opened**. No model has ever been fitted
against data at or after the boundary.

No horizon was pooled with another to create folds. No fold was dropped. No test
window was shortened. All pre-registered folds counted.

---

## 10. What was implemented

**Execution harness** — `scripts/research_f6b.py`: `preflight()` verifying eight
frozen-plan conditions, `enumerate_cells()` with a budget check that raises,
`research_horizon()` running the two-stage contract, `holdout_evaluation()`
implementing stage 2, `failed_gates()` mapping outcomes to canonical gate names,
`fold_stability()`, and `--plan` / `--run` / `--show`.

**Model research** — none written. The four specifications are imported verbatim;
fold scoring is `rmq.evaluate_fold`; the survival rule is `rmq.verdict`; the fit
and gate are `ModelingEngine.train_and_evaluate`.

**Fold evaluation** — `build_folds` from F.6, parameterised on the boundary
rather than copied, so one implementation serves both phases.

**Qualification** — the canonical `ModelEvaluation.is_deployable`, referenced and
never redefined. A test asserts F.6B assigns no numeric literal to
`MIN_EFFECTIVE_SAMPLE` or `MIN_VALID_FOLDS`.

**Calibration** — the frozen check (Brier vs base rate on research folds) applied
through `rmq.verdict`; nothing calibrated on the holdout. Expected calibration
error deliberately not computed (§38.10).

**Uncertainty** — reported `UNAVAILABLE` with the reason stated. Fold dispersion
is reported under stability and is explicitly not an interval.

**Expected return integration** — not reached; zero qualified candidates.

**Target / horizon integration** — not reached, and correctly so.

**Negative knowledge** — `research_candidate_results`, 16 rows, one per cell,
with survival status, holdout verdict, failed gates, metrics and full fold
evidence.

**Execution marker** — `research_executions`, with the pre-registration hash,
snapshot hash, holdout boundary, code commit, full cell list, completed cells and
status.

**Tests and docs** — 59 tests; the architecture document and this report.

---

## 11. What worked

Evidence-backed only.

1. **The frozen plan verified against its artifacts.** Snapshot hash and corpus
   hash recompute; the pre-registration hash matches the handoff; the holdout
   rule **re-derives** the frozen boundary rather than merely agreeing with a
   recorded value.
2. **The experiment ran once and cannot run again.** A second `--run` exits 2
   with `STOP: this frozen experiment has already been executed as
   f6b-647dd37b65e14046`.
3. **All 16 cells were evaluated.** 0 unevaluated, 0 errors, 59.25 s.
4. **The gates did their job in the right order and for the right reasons.** 10
   cells stopped at the baseline-majority rule, 6 at calibration, 0 survived.
5. **The holdout was never opened**, exactly as the pre-registered rule requires
   when nothing survives.
6. **Fold-level evidence was retained for every candidate**, including the
   failures — 2,590 lines of it.
7. **F.6's result reproduced bit-identically** after the fold-builder refactor:
   0 valid folds, INSUFFICIENT DATA on all four horizons, holdout unopened.
8. **The production database was never written to.** Both v1 hashes match and its
   mtime is still 2026-09-11 11:21.
9. **A latent defect that would have silently falsified this phase was found and
   fixed before the run** (§12.1).
10. **d10's empty holdout was found at plan time**, not after a disappointing
    score (§12.2).

---

## 12. What did not work

**Mandatory, and complete.**

### 12.1 A latent defect in F.6's harness that would have falsified this phase

- **Symptom:** none visible. That is what made it dangerous.
- **Root cause:** `research_multi_horizon.research_horizon` ended with
  `qualified = [c for c in candidates if c.get("qualified")]`, but
  `rmq.verdict()` returns a `status` key and **never** a `qualified` key. The
  list was always empty, so the harness would have reported `NONE QUALIFIED`
  whatever the candidates did.
- **Scope:** F.6's recorded result is unaffected, provably — zero folds formed on
  every horizon, so the `INSUFFICIENT DATA` return fired before any candidate was
  scored. Re-running F.6 on snapshot v1 after the fix reproduces its result
  exactly, and a test pins it.
- **Fix:** F.6's harness now reports *survival* rather than claiming
  qualification it cannot assess, and the holdout stage lives in F.6B where the
  pre-registration puts it.
- **Status:** resolved. Had it gone unnoticed, this phase would have reported the
  right answer for the wrong reason — and would have reported it even if
  something had qualified.

### 12.2 d10 cannot reach qualification under the frozen plan

- **Symptom:** `holdout_rows = 0` for d10 in the plan output.
- **Root cause:** d10's latest matured label is `2026-08-15`, three days before
  the boundary, because a d10 label needs ten trading days after its event and
  the price history ends `2026-08-27`. The holdout region is therefore empty.
- **Consequence:** stage 2 is impossible for d10, so **no d10 candidate could
  have qualified** regardless of its research-region behaviour.
- **Fix:** **none, deliberately.** Moving the boundary would make d10 qualifiable
  and would be exactly the holdout shopping every preceding phase refused.
- **Also a gap in F.7's own adequacy check**, stated plainly: F.7 verified fold
  viability and effective sample per horizon and declared the design `ADEQUATE`.
  It did **not** verify each horizon's holdout region was non-empty. For d10 the
  design is not fully adequate, and F.7 should have caught it. A test now pins
  the fact.
- **Status:** open as a limitation; it did not change this phase's outcome,
  because no d10 candidate survived stage 1 anyway.

### 12.3 The pre-registered feature ablation was almost entirely inert

- **Symptom:** C1 and C3 produced **identical** metrics to five decimals on d3,
  d5 and d10, and differed on only one fold of d1.
- **Root cause:** `dense_columns` selects columns that are non-null across the
  training rows. On this snapshot that is **all 26** features in 16 of 17 folds;
  only d1's `wf1` dropped one (25 vs 26).
- **Consequence:** the C1-vs-C3 contrast carried essentially no information.
  **7 of 8 logistic cells were literal duplicates of their partner.** The
  nominal budget was 16 cells; the number of genuinely distinct evaluations was
  closer to **9**.
- **Fix:** **none applied.** Replacing C3 with a different feature configuration
  after seeing this would be post-result plan modification. It is recorded as
  negative knowledge for the next pre-registration instead.
- **Status:** open, and it is a real criticism of the frozen plan rather than of
  the data.

### 12.4 The baseline-majority rule can be satisfied by two bad numbers

- **Symptom:** `d3:C1` was credited with a baseline win on `wf2` with
  **directional accuracy 0.312** against a baseline of **0.260**.
- **Root cause:** `baseline_historical_mean` predicts a constant direction, so in
  a fold where the realised direction distribution is skewed, both the model and
  the baseline score far below 0.5. The rule asks only which is higher.
- **Consequence:** three logistic cells cleared the majority rule partly on folds
  where every number was poor. Had calibration not stopped them, they would have
  reached the holdout on weak evidence.
- **Fix:** none — changing the rule after seeing it would be exactly the
  forbidden move. Recorded as negative knowledge: **a future pre-registration
  should require the primary metric to clear an absolute floor as well as the
  baseline**, decided in advance.
- **Status:** open. It strengthens the NONE QUALIFIED conclusion rather than
  weakening it.

### 12.5 The baseline re-run was stopped by the harness, and was contaminated anyway

- **Symptom:** the background full-suite run was killed at its time limit with no
  `Ran N tests` line.
- **Root cause:** it was still running while I edited
  `scripts/research_multi_horizon.py` and added two test files, so its working
  tree changed underneath it.
- **Fix:** reported as stopped; the baseline used is F.7's full-suite result at
  the identical commit with a clean tree, plus a fresh pre-execution check of the
  targeted suites (§5).
- **Status:** resolved, and stated rather than papered over.

### 12.6 Six of my own tests failed on first run

- **Three constructor errors.** `ModelEvaluation` requires `evaluation_id` as its
  first positional field; I omitted it in three gate tests. Fixed.
- **One wrong invariant.** I asserted a *strict* purge inequality, but
  `splits.purge` removes a row only when `T + horizon > test_start` — a label
  resolving **exactly at** the boundary was determined entirely before the test
  period and leaks nothing. d5's `wf1` contains such a row at
  `2026-07-15T07:00Z`. **My test was wrong, the code was right**; the assertion is
  now `<=` with the reason recorded.
- **Two blunt substring assertions flagging their own subject.** A test forbidding
  the string `requests` matched the harness's own `market_data_requests: 0`
  accounting, and one forbidding `d20` matched the `d20_excluded` flag the harness
  reads in order to *enforce* the exclusion. Both now check imports and label
  queries rather than raw substrings.
- **Status:** all resolved. This is the third phase in which a blunt substring
  seal flagged the prose explaining the prohibition; it is now consistently fixed
  by parsing the AST instead.

### 12.7 Failed candidates, in full

All 16. No overfit survivor, no calibration success, no robust winner — see §14
through §19. Nothing was omitted and nothing was retried.

---

## 13. What was deferred

| Deferred | Why |
|---|---|
| **model promotion** | zero qualified candidates; and no code path in this phase could promote one |
| **I-G shadow validation** | nothing to shadow |
| **I-H paper integration** | not authorised here, and nothing to authorise |
| **intraday horizons** | data-eligible at 57 sessions, **deferred on policy**: the only purge available is d10's 14-day fallback, which must be pre-registered on its own evidence |
| **an absolute-floor baseline rule** (§12.4) | requires a new pre-registration |
| **a non-degenerate feature ablation** (§12.3) | requires a new pre-registration |
| **target / horizon calibration** | requires a qualified model |
| **expected-return and transaction-cost analysis** | requires a qualified model |
| **realtime IBKR validation** | separate Phase I-C work, still pending |
| **D20** | **sealed** |

---

## 14. Complete candidate inventory

All 16 frozen cells. None omitted.

| Cell | Family | Task | Features | Folds | Raw rows | Effective sample (pooled) | Primary metric | Mean | Worst | Best | Spread | Baseline wins | Calibration | Uncertainty | **Verdict** |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| d1:C0 | ridge α=1 | abnormal_return | all_numeric | 5 | 891 | 440 | MAE | 0.03306 | 0.04796 | 0.02151 | 0.02646 | **0/5** | n/a | UNAVAILABLE | **FAILED_BASELINE** |
| d1:C1 | logistic | direction | all_numeric | 5 | 891 | 440 | dir. acc. | 0.49623 | 0.40559 | 0.60215 | 0.19656 | **2/5** | **FAILED** 5/5 | UNAVAILABLE | **FAILED_BASELINE + FAILED_CALIBRATION** |
| d1:C2 | ridge α=10 | abnormal_return | all_numeric | 5 | 891 | 440 | MAE | 0.03109 | 0.04651 | 0.02070 | 0.02581 | **0/5** | n/a | UNAVAILABLE | **FAILED_BASELINE** |
| d1:C3 | logistic | direction | dense_only | 5 | 891 | 440 | dir. acc. | 0.50133 | 0.40559 | 0.62766 | 0.22207 | **2/5** | **FAILED** 5/5 | UNAVAILABLE | **FAILED_BASELINE + FAILED_CALIBRATION** |
| d3:C0 | ridge α=1 | abnormal_return | all_numeric | 5 | 878 | 436 | MAE | 0.04788 | 0.06211 | 0.03436 | 0.02776 | **0/5** | n/a | UNAVAILABLE | **FAILED_BASELINE** |
| d3:C1 | logistic | direction | all_numeric | 5 | 878 | 436 | dir. acc. | 0.44203 | 0.31169 | 0.57576 | 0.26407 | **3/5** ✓ | **FAILED** 4/5 | UNAVAILABLE | **FAILED_CALIBRATION** |
| d3:C2 | ridge α=10 | abnormal_return | all_numeric | 5 | 878 | 436 | MAE | 0.04500 | 0.05790 | 0.03267 | 0.02524 | **0/5** | n/a | UNAVAILABLE | **FAILED_BASELINE** |
| d3:C3 | logistic | direction | dense_only | 5 | 878 | 436 | dir. acc. | 0.44203 | 0.31169 | 0.57576 | 0.26407 | **3/5** ✓ | **FAILED** 4/5 | UNAVAILABLE | **FAILED_CALIBRATION** |
| d5:C0 | ridge α=1 | abnormal_return | all_numeric | 4 | 805 | 400 | MAE | 0.06014 | 0.07886 | 0.04243 | 0.03643 | **0/4** | n/a | UNAVAILABLE | **FAILED_BASELINE** |
| d5:C1 | logistic | direction | all_numeric | 4 | 805 | 400 | dir. acc. | 0.50967 | 0.46853 | 0.59200 | 0.12347 | **3/4** ✓ | **FAILED** 3/4 | UNAVAILABLE | **FAILED_CALIBRATION** |
| d5:C2 | ridge α=10 | abnormal_return | all_numeric | 4 | 805 | 400 | MAE | 0.05509 | 0.06478 | 0.04159 | 0.02319 | **0/4** | n/a | UNAVAILABLE | **FAILED_BASELINE** |
| d5:C3 | logistic | direction | dense_only | 4 | 805 | 400 | dir. acc. | 0.50967 | 0.46853 | 0.59200 | 0.12347 | **3/4** ✓ | **FAILED** 3/4 | UNAVAILABLE | **FAILED_CALIBRATION** |
| d10:C0 | ridge α=1 | abnormal_return | all_numeric | 3 | 633 | 229 | MAE | 0.10574 | 0.12773 | 0.06748 | 0.06025 | **0/3** | n/a | UNAVAILABLE | **FAILED_BASELINE** |
| d10:C1 | logistic | direction | all_numeric | 3 | 633 | 229 | dir. acc. | 0.51509 | 0.44937 | 0.57143 | 0.12206 | **2/3** ✓ | **FAILED** 3/3 | UNAVAILABLE | **FAILED_CALIBRATION** |
| d10:C2 | ridge α=10 | abnormal_return | all_numeric | 3 | 633 | 229 | MAE | 0.09447 | 0.11670 | 0.06623 | 0.05047 | **0/3** | n/a | UNAVAILABLE | **FAILED_BASELINE** |
| d10:C3 | logistic | direction | dense_only | 3 | 633 | 229 | dir. acc. | 0.51509 | 0.44937 | 0.57143 | 0.12206 | **2/3** ✓ | **FAILED** 3/3 | UNAVAILABLE | **FAILED_CALIBRATION** |

For MAE, *worst* is the largest error. ✓ marks a cell that cleared the
strict-majority baseline rule. R² was negative on every ridge fold (−0.12 to
−3.26). Robustness: every candidate is sign-consistent across folds; none is
carried by a single fold, and none needed to be — none passed.

---

## 15. Fold-level results

No cherry-picking. The complete record is in
`docs/PHASE_25_9I_F6B_RESULTS.json`; the primary metric per fold:

| Cell | wf1 | wf2 | wf3 | wf4 | wf5 |
|---|---|---|---|---|---|
| d1:C0 | 0.03727 ✗ | 0.04796 ✗ | 0.03504 ✗ | 0.02151 ✗ | 0.02351 ✗ |
| d1:C1 | 0.60215 ✗ | 0.40559 ✓ | 0.48795 ✓ | 0.44697 ✗ | 0.53846 ✗ |
| d1:C2 | 0.03075 ✗ | 0.04651 ✗ | 0.03435 ✗ | 0.02070 ✗ | 0.02314 ✗ |
| d1:C3 | 0.62766 ✗ | 0.40559 ✓ | 0.48795 ✓ | 0.44697 ✗ | 0.53846 ✗ |
| d3:C0 | 0.05766 ✗ | 0.06211 ✗ | 0.04442 ✗ | 0.03436 ✗ | 0.04087 ✗ |
| d3:C1 | 0.57576 ✗ | 0.31169 ✓ | 0.52761 ✓ | 0.43796 ✗ | 0.35714 ✓ |
| d3:C2 | 0.04987 ✗ | 0.05790 ✗ | 0.04359 ✗ | 0.03267 ✗ | 0.04095 ✗ |
| d3:C3 | 0.57576 ✗ | 0.31169 ✓ | 0.52761 ✓ | 0.43796 ✗ | 0.35714 ✓ |
| d5:C0 | 0.07886 ✗ | 0.06829 ✗ | 0.04243 ✗ | 0.05098 ✗ | — |
| d5:C1 | 0.59200 ✓ | 0.49057 ✓ | 0.46853 ✗ | 0.48760 ✓ | — |
| d5:C2 | 0.06478 ✗ | 0.06357 ✗ | 0.04159 ✗ | 0.05042 ✗ | — |
| d5:C3 | 0.59200 ✓ | 0.49057 ✓ | 0.46853 ✗ | 0.48760 ✓ | — |
| d10:C0 | 0.12773 ✗ | 0.06748 ✗ | 0.12201 ✗ | — | — |
| d10:C1 | 0.44937 ✓ | 0.52448 ✗ | 0.57143 ✓ | — | — |
| d10:C2 | 0.10047 ✗ | 0.06623 ✗ | 0.11670 ✗ | — | — |
| d10:C3 | 0.44937 ✓ | 0.52448 ✗ | 0.57143 ✓ | — | — |

✓ / ✗ = beat all baselines in that fold, or did not.

**Note on `d1:C1` wf1 and `d3:C1` wf1:** the highest directional accuracy in the
table (0.60–0.63) are *losses*, because the constant baseline scored higher
still. And `d3:C1` wf2 is a *win* at 0.312 accuracy. Both are the point of
reporting folds rather than means.

---

## 16. Baseline comparison

Every candidate against every required baseline, out of sample, per fold.

### Ridge — lost the historical-mean baseline in **34 of 34** fold evaluations

Magnitudes (lower MAE is better):

| Cell | Fold | Model MAE | Baseline MAE | Model worse by |
|---|---|---|---|---|
| d1:C0 | wf1–wf5 | 0.03727 / 0.04796 / 0.03504 / 0.02151 / 0.02351 | 0.02676 / 0.03857 / 0.02828 / 0.01912 / 0.02173 | **39.2% / 24.4% / 23.9% / 12.5% / 8.2%** |
| d1:C2 | wf1–wf5 | 0.03075 / 0.04651 / 0.03435 / 0.02070 / 0.02314 | same baselines | **14.9% / 20.6% / 21.5% / 8.3% / 6.5%** |
| d3:C0 | wf1–wf5 | 0.05766 / 0.06211 / 0.04442 / 0.03436 / 0.04087 | 0.04319 / 0.05103 / 0.03593 / 0.02923 / 0.03885 | **33.5% / 21.7% / 23.6% / 17.5% / 5.2%** |
| d3:C2 | wf1–wf5 | 0.04987 / 0.05790 / 0.04359 / 0.03267 / 0.04095 | same baselines | **15.5% / 13.5% / 21.3% / 11.8% / 5.4%** |
| d5:C0 | wf1–wf4 | 0.07886 / 0.06829 / 0.04243 / 0.05098 | 0.05267 / 0.05434 / 0.03628 / 0.04815 | **49.7% / 25.7% / 16.9% / 5.9%** |
| d5:C2 | wf1–wf4 | 0.06478 / 0.06357 / 0.04159 / 0.05042 | same baselines | **23.0% / 17.0% / 14.6% / 4.7%** |
| d10:C0 | wf1–wf3 | 0.12773 / 0.06748 / 0.12201 | 0.06625 / 0.05473 / 0.11465 | **92.8% / 23.3% / 6.4%** |
| d10:C2 | wf1–wf3 | 0.10047 / 0.06623 / 0.11670 | same baselines | **51.7% / 21.0% / 1.8%** |

**Not once better.** Both baselines (`baseline_historical_mean` and
`baseline_majority_class`) were beaten in zero folds by either ridge
specification on any horizon.

### Logistic — beat both baselines in a strict majority on d3, d5 and d10

| Cell | Baseline wins | Strict majority? |
|---|---|---|
| d1:C1 / d1:C3 | 2/5 | **no** |
| d3:C1 / d3:C3 | 3/5 | **yes** |
| d5:C1 / d5:C3 | 3/4 | **yes** |
| d10:C1 / d10:C3 | 2/3 | **yes** |

Six of eight logistic cells cleared the baseline rule. **All eight were stopped
by calibration.**

---

## 17. Qualification gates

Every candidate × every canonical gate. Repository semantics.

| Cell | Sample gate (≥30) | Baseline gate (strict majority) | Robustness (fold stability) | Calibration (Brier vs base rate) | Holdout gate (`is_deployable`) | **Final qualified?** |
|---|---|---|---|---|---|---|
| d1:C0 | **PASS** 440 | **FAIL** 0/5 | sign-consistent | n/a | **NOT REACHED** | **NO** |
| d1:C1 | **PASS** 440 | **FAIL** 2/5 | sign-consistent | **FAIL** 5/5 worse | **NOT REACHED** | **NO** |
| d1:C2 | **PASS** 440 | **FAIL** 0/5 | sign-consistent | n/a | **NOT REACHED** | **NO** |
| d1:C3 | **PASS** 440 | **FAIL** 2/5 | sign-consistent | **FAIL** 5/5 worse | **NOT REACHED** | **NO** |
| d3:C0 | **PASS** 436 | **FAIL** 0/5 | sign-consistent | n/a | **NOT REACHED** | **NO** |
| d3:C1 | **PASS** 436 | **PASS** 3/5 | sign-consistent | **FAIL** 4/5 worse | **NOT REACHED** | **NO** |
| d3:C2 | **PASS** 436 | **FAIL** 0/5 | sign-consistent | n/a | **NOT REACHED** | **NO** |
| d3:C3 | **PASS** 436 | **PASS** 3/5 | sign-consistent | **FAIL** 4/5 worse | **NOT REACHED** | **NO** |
| d5:C0 | **PASS** 400 | **FAIL** 0/4 | sign-consistent | n/a | **NOT REACHED** | **NO** |
| d5:C1 | **PASS** 400 | **PASS** 3/4 | sign-consistent | **FAIL** 3/4 worse | **NOT REACHED** | **NO** |
| d5:C2 | **PASS** 400 | **FAIL** 0/4 | sign-consistent | n/a | **NOT REACHED** | **NO** |
| d5:C3 | **PASS** 400 | **PASS** 3/4 | sign-consistent | **FAIL** 3/4 worse | **NOT REACHED** | **NO** |
| d10:C0 | **PASS** 229 | **FAIL** 0/3 | sign-consistent | n/a | **NOT REACHED** | **NO** |
| d10:C1 | **PASS** 229 | **PASS** 2/3 | sign-consistent | **FAIL** 3/3 worse | **NOT REACHED** | **NO** |
| d10:C2 | **PASS** 229 | **FAIL** 0/3 | sign-consistent | n/a | **NOT REACHED** | **NO** |
| d10:C3 | **PASS** 229 | **PASS** 2/3 | sign-consistent | **FAIL** 3/3 worse | **NOT REACHED** | **NO** |

`NOT REACHED` is deliberately distinct from `FAIL`: the holdout gate was never
evaluated for any candidate, because none survived stage 1 and the
pre-registration grants the holdout to survivors only.

### Gate funnel across all 16 cells

```
16  cells evaluated
 0  failed the effective-sample gate
10  stopped at the baseline-majority rule   (all 8 ridge + d1:C1, d1:C3)
 6  stopped at the calibration gate          (d3, d5, d10 logistic)
 0  survived the research region
 0  shown the holdout
 0  QUALIFIED
```

### Calibration detail — Brier against base rate

| Cell | wf1 | wf2 | wf3 | wf4 | wf5 | Worse in |
|---|---|---|---|---|---|---|
| d1:C1 | 0.301 vs 0.235 | 0.383 vs 0.274 | 0.302 vs 0.268 | 0.257 vs 0.250 | 0.254 vs 0.253 | **5/5** |
| d1:C3 | 0.286 vs 0.235 | 0.383 vs 0.274 | 0.302 vs 0.268 | 0.257 vs 0.250 | 0.254 vs 0.253 | **5/5** |
| d3:C1 / C3 | 0.274 vs 0.238 | 0.417 vs 0.292 | 0.293 vs 0.255 | 0.278 vs 0.255 | 0.263 vs 0.264 ✓ | **4/5** |
| d5:C1 / C3 | 0.242 vs 0.274 ✓ | 0.319 vs 0.274 | 0.332 vs 0.252 | 0.298 vs 0.264 | — | **3/4** |
| d10:C1 / C3 | 0.394 vs 0.291 | 0.292 vs 0.245 | 0.258 vs 0.253 | — | — | **3/3** |

Of 34 logistic fold evaluations, the model's Brier was worse than simply
predicting the base rate in **26**. Expected calibration error was **not**
computed — see §38.10. The Brier-vs-base-rate check is the entire pre-registered
calibration gate, so no verdict depended on the missing figure.

---

## 18. Qualified candidates

**Zero.**

No candidate reached the holdout, so no candidate can be described as qualified,
nearly qualified, or deployable in any sense.

---

## 19. Rejected candidates and binding reasons

| Binding reason | Cells | Negative knowledge |
|---|---|---|
| **lost the baseline in every fold** | 8 ridge cells (d1/d3/d5/d10 × C0/C2) | ridge on these 26 features does not predict abnormal return better than the historical mean, on any daily horizon, in any fold |
| **failed the baseline majority** | d1:C1, d1:C3 | logistic direction on d1 beat the baselines in only 2 of 5 folds |
| **failed calibration** | d3:C1, d3:C3, d5:C1, d5:C3, d10:C1, d10:C3 | logistic direction has *some* signal on d3/d5/d10 but its probabilities are worse than the base rate |

### Strongest rejected candidates

Per §51, no model is called "best". The candidates that travelled furthest
through the gates were **`d5:C1` and `d5:C3`** — identical, since the ablation was
inert — which cleared the sample and baseline gates (3 of 4 folds) and were
stopped by calibration alone (Brier worse in 3 of 4 folds). `d3:C1`/`d3:C3` (3/5)
and `d10:C1`/`d10:C3` (2/3) reached the same gate.

None of them is usable, and none is close to usable: a model whose stated
confidence is worse than the base rate cannot carry a position size.

---

## 20. Multi-horizon results

| Horizon | Qualified? | Expected return? | Calibration? | Uncertainty? | Signal capability? | Target capability? | Binding failure |
|---|---|---|---|---|---|---|---|
| **d1** | **NO** | UNAVAILABLE | **FAILED** | UNAVAILABLE | none | none | baseline majority (all four cells) |
| **d3** | **NO** | UNAVAILABLE | **FAILED** | UNAVAILABLE | none | none | calibration (logistic); baseline (ridge) |
| **d5** | **NO** | UNAVAILABLE | **FAILED** | UNAVAILABLE | none | none | calibration (logistic); baseline (ridge) |
| **d10** | **NO** | UNAVAILABLE | **FAILED** | UNAVAILABLE | none | none | calibration (logistic); baseline (ridge). Holdout also empty (§12.2) |
| intraday ×4 | — | — | — | — | — | — | **DEFERRED_BY_PREREGISTRATION** |
| **d20** | — | — | — | — | — | — | **PROTECTED** |

No horizon is unexplained. Each was evaluated independently with its own target,
candidates, folds, purge and verdict; none was pooled with another.

---

## 21. Confidence

**No genuinely independent qualified specification exists, because no
specification qualified.**

Had any qualified, the independence accounting was already fixed in advance and
would not have been flattering:

| | |
|---|---|
| C0 vs C2 | one family, two regularisation strengths — **not independent** |
| C1 vs C3 | one family, two feature sets — **not independent**, and on this snapshot **identical in 15 of 16 cells** |
| genuinely distinct families in the whole budget | **two** |

The canonical confidence formula (`base × quality × agreement × sample`) is
unchanged. It was never reached: with zero qualified models there is no
prediction to score, and the `agreement` term has nothing to agree about.

**No confidence value was produced and none was inflated.**

---

## 22. Uncertainty

**UNAVAILABLE for every candidate**, with the reason recorded on each: no
prediction-interval machinery is in the frozen plan.

What *is* measured and reported is fold dispersion — mean, worst, best, spread,
standard deviation and sign consistency (§14). That is not an interval and is
labelled as not an interval. Nothing was manufactured to unlock target
estimation.

---

## 23. Expected return

**0 of 16 candidates can provide a defensible expected return.**

Expected return requires a candidate that passes qualification. None did. No
expected-return value was emitted, stored or passed downstream, even where the
raw ridge models produced numbers.

---

## 24. Target capability

**Zero.**

**0 of 301 instruments** and **0 of 4 researched horizons** can receive a
defensible target zone, in shadow or anywhere else.
`src/adaptive/target_estimation.py` emits a zone only when a `TargetEstimate` is
`AVAILABLE`, which requires a qualified model, and it was never invoked because
there is nothing to invoke it with.

---

## 25. Horizon capability

| | Count |
|---|---|
| usable horizons | **0** |
| preferred horizons | **0** |
| horizon disagreements | **0** (disagreement needs ≥ 2 usable horizons) |
| no-preference cases | **all** |

---

## 26. F.5 before / after

Re-run after the experiment on both snapshots.

| | Before (v1) | After (v1) | Before (v2) | After (v2) |
|---|---|---|---|---|
| instruments evaluated | 244 | **244** | 301 | **301** |
| `model_not_qualified` | 244 | **244** | 0 | **0** |
| `no_model_available` | 0 | **0** | 301 | **301** |
| `valid_no_signal` | 0 | **0** | 0 | **0** |
| shadow signals | 0 | **0** | 0 | **0** |
| signals | 0 | **0** | 0 | **0** |
| preferred horizons | 0 | **0** | 0 | **0** |
| horizon disagreements | 0 | **0** | 0 | **0** |
| defensible targets | 0 | **0** | 0 | **0** |
| capability gaps | 244 | **244** | 301 | **301** |

**Unchanged on both, exactly as it must be.** F.6B promoted nothing, so the
production diagnostic cannot move; had it moved, that would have been evidence of
a bug.

`valid_no_signal` remains **0** and that is the correct value, not a
disappointment. It requires a qualified model that looked and found no edge.
There is still no qualified model, so every instrument remains a **capability
gap** — and the F.5 taxonomy keeps the two distinct, which is what it was built
for.

---

## 27. Transaction-cost effect

**UNAVAILABLE.**

The required input — a legitimately available expected return — does not exist
for any candidate. Computing a cost-adjusted edge from an unqualified model's raw
output would be inventing the very number the gates refused to grant. No
`COST_EXCEEDS_EDGE` determination was made, because there is no edge estimate to
compare costs against.

---

## 28. Negative knowledge

Recorded in `research_candidate_results` (16 rows, queryable by horizon and
qualification) and summarised here so a future phase does not blindly repeat it.

1. **Ridge regression on all 26 numeric features does not beat the historical-mean
   baseline for abnormal return on d1, d3, d5 or d10.** 34 of 34 fold
   evaluations lost, by 1.8% to 92.8% MAE. R² negative in every fold. Both α=1
   and α=10. Do not re-run this specification expecting a different answer from
   regularisation alone.
2. **Logistic direction has some directional signal on d3, d5 and d10 and is
   badly calibrated.** It cleared the strict-majority baseline rule on three
   horizons and its Brier was worse than the base rate in 26 of 34 folds. The
   next hypothesis about this family should be about calibration, not about
   accuracy.
3. **Logistic direction on d1 does not clear the baseline rule** (2 of 5 folds).
   d1 is the noisiest of the four.
4. **The `dense_only` feature configuration is degenerate on this snapshot.**
   `dense_columns` selects all 26 features in 16 of 17 folds, so C3 duplicated C1
   in 15 of 16 cells. A future ablation must be defined by feature *group*, not
   by null-density.
5. **The baseline-majority rule can be satisfied by two poor numbers** in folds
   with a skewed direction distribution — `d3:C1` wf2 "won" at 0.312 accuracy
   against 0.260. A future pre-registration should require an absolute floor on
   the primary metric as well as a baseline comparison.
6. **d10 cannot be qualified under this holdout** because its labels mature three
   days before the boundary (§12.2). Any future d10 experiment needs either more
   price history or its own boundary, decided in advance.
7. **A 46-day research region supports 3–5 folds but a 12-day holdout.** Even a
   survivor would have faced a single evaluation on 70–154 rows. That is a thin
   final test, and it is a property of the dataset, not of the candidates.

---

## 29. As-of / leakage

| | |
|---|---|
| temporal splits | expanding, day-granular, strictly inside the research region |
| train cutoff | per fold, always strictly before its test window — asserted for every fold of every horizon |
| purge | `splits.purge`, reused — calendar days 3 / 5 / 7 / 14 |
| embargo | `splits.embargo`, reused — 6 days; asserted to reduce training rows and to mark rows embargoed |
| feature cutoff | every feature's `as_of` ≤ its observation's `information_cutoff` (0 violations, verified in F.7 and unchanged) |
| future exclusion | no fold's train or test index has a cutoff at or after the boundary — asserted |
| **holdout isolation** | **never opened.** No model was fitted against data at or after `2026-08-18T00:00:00Z` |
| physical truncation | `effective_sample(before=…)` is tested to see strictly fewer rows and no more clusters at an earlier boundary |
| protected isolation | the harness contains no `value_json`, no `d20.*` label name, and no route to the protected validator |

**Purge boundary semantics, stated precisely:** `splits.purge` removes a training
row only when `T + horizon > test_start`, strictly. A label resolving exactly at
the boundary was determined entirely before the test period and leaks nothing, so
it is kept. d5's `wf1` holds such a row at `2026-07-15T07:00Z`. My first test
asserted the stricter inequality and was wrong.

---

## 30. Pre-registration integrity

| | |
|---|---|
| pre-registration changed after results? | **NO** — sha256 `9150b988993dcac3…` matches the handoff and the file |
| holdout changed? | **NO** — `2026-08-18T00:00:00Z`, and the rule re-derives it |
| old F.6 holdout changed? | **NO** — `2026-08-15T01:23:31Z` still belongs to snapshot v1 |
| candidate budget expanded? | **NO** — 16, enforced by a check that raises |
| new model added post-result? | **NO** |
| feature set changed post-result? | **NO** — including after discovering the ablation was degenerate (§12.3) |
| gate weakened? | **NO** |
| threshold changed? | **NO** |
| second round run? | **NO** — a second `--run` exits 2 |
| dataset modified? | **NO** — read `mode=ro`; only the marker and results tables were written |

Every answer is NO. The scientific result stands.

---

## 31. D20 status

| | |
|---|---|
| executed? | **NO** |
| inspected? | **NO** |
| inferred? | **NO** |
| consumed? | **NO** |
| trained on? | **NO** |
| evaluated? | **NO** |
| used for feature selection? | **NO** |
| used for model-family selection? | **NO** |
| used for hyperparameter selection? | **NO** |
| used for horizon selection? | **NO** |
| used for target estimation? | **NO** |
| hypothesis changed? | **NO** |
| anchor-v2 changed? | **NO** |
| protected window changed? | **NO** |

Structural enforcement: absent from the handoff's eligible list, effective sample
and purge policy; `research_horizon()` raises `ProtectedHorizonRequested`;
`rs.effective_sample()` raises if asked; tests assert the harness contains no
`d20.abnormal_return`, `d20.raw_return`, `validate_d20_reversal`, `anchor_v2`,
`build_anchor_v2_labels`, `return_60d`, `check_d20_readiness` or `value_json`.
The only permitted appearance of the name is the `d20_excluded` flag the harness
reads in order to enforce the exclusion.

---

## 32. Model promotion

```
models promoted = 0
```

No promotion occurred and no code path in this phase could perform one. A test
asserts the harness contains no `promote_model`, `PRODUCTION_ACTIVE`,
`production_active` or `ModelStatus.ACTIVE`, and that
`trained_models WHERE status='active'` is 0 in the snapshot.

---

## 33. Production behaviour

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

One file was modified and it is not a production module (`research_multi_horizon.py`,
a research script). `data/marketlens.db` was never opened for writing: both
manifest hashes match and its mtime is still **2026-09-11 11:21**. It holds no
`research_executions` table.

---

## 34. Broker safety

```
broker writes attempted:                       0
broker writes succeeded:                       0
paper orders:                                  0
live orders:                                   0
billable market-data requests caused by F.6B:  0
```

No `OrderIntent` was created. No IBKR connection was opened. No market-data
subscription was touched. Every evaluation read the frozen historical snapshot;
a test asserts the harness imports no network, provider or LLM client.

---

## 35. Test results

### Full suite

| | Baseline `209da23` | **Final** |
|---|---|---|
| Tests | 5,275 | **5,334** |
| Pass | 5,274 | **5,333** |
| Fail / Errors | 0 / 0 | **0 / 0** |
| Skipped | 1 | **1** |
| Runtime | 563.048 s | **1,232.828 s** |

```
PYTHONPATH=src python -m unittest discover -s tests -t . -b
Ran 5334 tests in 1232.828s
OK (skipped=1)
```

**+59 tests, exactly the new file. Zero regressions.**

Two things about these numbers worth stating rather than leaving to be noticed.

**The skip count did not rise, and that is the useful signal.** The
pre-execution adaptive run reported `skipped=9`, because nine F.6B tests guard on
`docs/PHASE_25_9I_F6B_RESULTS.json` and the execution marker, neither of which
existed yet — seven in `TestNegativeKnowledgePersisted`, two in
`TestRunsOnlyOnce`. After the experiment ran they all execute, leaving the single
pre-existing baseline skip. So the tests that verify the result are confirmed to
have actually run against it rather than passing vacuously.

**The runtime more than doubled** against the baseline, 563 s → 1,233 s, on an
unchanged test count plus 59. That is machine load, not scope: the capture suite
dominates and is wall-clock sensitive, and the same effect showed in the targeted
runs during this phase (adaptive 63 s → 166 s for the same tests).

### Targeted and requested suites

| Suite | Tests | Pass | Fail | Err | Runtime |
|---|---|---|---|---|---|
| **F.6B — `test_f6b_frozen_experiment`** | **59** | 59 | 0 | 0 | 3.2 s |
| F.7 — `test_corpus_recovery` | 71 | 71 | 0 | 0 | 42.7 s |
| F.7 — `test_research_snapshot_integrity` | 55 | 55 | 0 | 0 | 0.6 s |
| F.7 — `test_recovery_boundary` | 30 | 30 | 0 | 0 | 11.7 s |
| F.6 — `test_sample_adequacy` | 24 | 24 | 0 | 0 | 0.1 s |
| F.6 — `test_research_discipline` | 33 | 33 | 0 | 0 | 6.9 s |
| adaptive (F.6B + F.7 + F.6 + F.5 + I-F + I-E + I-D + I-C + I-B) | **990** | 990 | 0 | 0 | 166.0 s |
| modeling | 142 | 142 | 0 | 0 | 5.6 s |
| signals | 129 | 129 | 0 | 0 | 9.3 s |
| research / research integrity | 104 | 104 | 0 | 0 | 31.0 s |
| experiments (Experiment Engine) | 83 | 83 | 0 | 0 | 15.8 s |
| portfolio | 273 | 273 | 0 | 0 | — |
| trading (readiness, live safety, Risk) | 224 | 224 | 0 | 0 | — |
| capture / session | 126 | 126 | 0 | 0 | — |
| **full suite** | **5,334** | **5,333** | **0** | **0** | **1,232.8 s** |

Autonomous research is covered by the `research` and `experiments` suites; no
autonomous loop was started by this phase.

---

## 36. Compute / performance

| | |
|---|---|
| research cells | **16** |
| **model fits (research region)** | **68** (5+5+4+3 folds × 4 candidates) |
| **model fits (holdout)** | **0** — no survivor earned one |
| total fits | **68** |
| total runtime, end to end | **59.25 s** |
| longest horizon | d1, 5 folds × 4 candidates |
| failures / retries | **0 / 0** |
| plan runtime (read-only) | ~6 s |
| storage growth | **+80 KB** in the snapshot database (1 marker row + 16 candidate rows) |
| result artifact | `docs/PHASE_25_9I_F6B_RESULTS.json`, 86 KB |
| paid API calls / LLM calls / network calls | **0 / 0 / 0** |
| billable market-data requests | **0** |

The predicted budget was 68 research fits and at most 16 holdout fits. The
realised figure is 68 and 0 — the holdout fits never happened because nothing
survived, which is the budget being under-spent for the right reason.

---

## 37. Scope-creep review

| | |
|---|---|
| Unrelated files changed | **none** |
| Dataset modified | **none** — read `mode=ro`; only the marker and results tables written |
| Holdout modified | **none** |
| Pre-registration changed | **none** |
| Gate changes | **none** — `ModelEvaluation.is_deployable` referenced, never redefined |
| Threshold changes | **none** — a test forbids assigning a numeric literal to `MIN_EFFECTIVE_SAMPLE` or `MIN_VALID_FOLDS` in F.6B |
| Signal thresholds | **untouched** |
| Model promotion | **none** |
| Production changes | **none** |
| IBKR changes | **none** |
| Protected research changes | **none** |
| New dependencies | **none** — standard library plus existing project modules |
| Files modified | **1**, a research script, explained in §4 and §12.1 |

---

## 38. Known limitations

1. **The feature ablation was degenerate** (§12.3): C3 duplicated C1 in 15 of 16
   cells, so the 16-cell budget bought roughly 9 distinct evaluations.
2. **The baseline-majority rule has no absolute floor** (§12.4): a fold can be
   "won" at 31% directional accuracy.
3. **d10 cannot be qualified under this holdout** (§12.2) because its labels
   mature before the boundary.
4. **The holdout is 12 days** and 70–154 rows per horizon. Even a survivor would
   have faced a thin final test.
5. **Only two genuinely distinct model families** were tested — ridge and
   logistic, both linear. Nothing non-linear was in the frozen plan, so this
   phase says nothing about non-linear structure.
6. **26 numeric features, no categoricals.** `event_type`, ticker and exchange are
   excluded rather than arbitrarily encoded, so any signal carried by event type
   was invisible to every candidate.
7. **`event_time` is missing for all 1,056 canonical events** and
   `independent_source_count` is 0 everywhere — both inherited from the snapshot
   and unchanged. Source independence remains UNKNOWN.
8. **A cluster is an instrument**, so two events at one issuer count once.
9. **Intraday is deferred on policy**, not on data (§13).
10. **Expected calibration error was not computed.** `compute_calibration`
    exists in the engine but needs per-row predicted probabilities, and the
    reused `evaluate_fold` returns only Brier values. Adding that retention
    after seeing the results would be a post-hoc diagnostic, so the field reads
    `NOT_COMPUTED`. Only the pre-registered Brier-vs-base-rate check is
    populated — which is the entire calibration gate, so nothing in the verdict
    depended on it.

    One cosmetic consequence: `docs/PHASE_25_9I_F6B_RESULTS.json` was written by
    the run and records the field under its original name as
    `"exploratory_ece": null`. The harness was corrected afterwards to say
    `"expected_calibration_error": "NOT_COMPUTED"` instead. **The results
    artifact was deliberately not regenerated** — it is the record of what
    actually ran, and re-running the frozen experiment to tidy a field name is
    exactly what the run-once rule forbids.

---

## 39. Readiness for I-G

**NO.**

| Requirement | State |
|---|---|
| ≥ 1 legitimately qualified research candidate | **0** |
| reproducible shadow predictions | **none** — no model to predict with |
| lineage | present, but for failures only |
| calibration / uncertainty semantics sufficient for meaningful evaluation | **no** — calibration failed on every probabilistic candidate; uncertainty UNAVAILABLE |
| no unresolved research-integrity blocker | the open items in §12.2–12.4 are documented limitations of the frozen plan, not integrity failures |

I-G is shadow validation and there is nothing to shadow. **No I-G trading handoff
was created.** The honest output is:

> **RESEARCH FOLLOW-UP REQUIRED.**

The follow-up needs a new hypothesis and a new pre-registration — not another
pass at this one. The specific directions the evidence supports are in §28.

---

## 40. Readiness for I-H

**NO.**

Paper trading is not authorised by this phase and nothing in it moves toward
authorisation. I-H additionally requires realtime operational verification,
shadow evidence from I-G, execution freshness, a qualified model and signal path,
and the safety gates — of which only the last exists, with nothing to gate.

The hard separation stands: **research qualified ≠ shadow validated ≠ paper
authorised ≠ live authorised.** F.6B ends before the first.

---

## 41. Final verdict

> ## PHASE 25.9I-F.6B IMPLEMENTATION = COMPLETE
>
> ## MODEL QUALIFICATION OUTCOME = NONE QUALIFIED

The frozen plan was verified against its artifacts before anything was fitted —
snapshot and corpus hashes recompute, the pre-registration hash matches the
handoff, and the inherited 20% rule **re-derives** the frozen boundary
`2026-08-18T00:00:00Z` rather than merely agreeing with a recorded value. The 16
cells were enumerated from the handoff by a function that refuses to produce a
seventeenth, the experiment ran once in 59.25 s, and a second attempt exits with
`STOP`. All 16 cells were evaluated; none was omitted, retried or added.

**Nothing qualified, and the reasons are specific rather than diffuse.** The eight
ridge cells lost the historical-mean baseline in **34 of 34** fold evaluations —
by 1.8% to 92.8% MAE, with negative R² throughout — so ridge on these 26 features
does not predict abnormal return on any daily horizon. The eight logistic cells
did better on direction: six of them cleared the strict-majority baseline rule on
d3, d5 and d10. **Every one of them was stopped by calibration**, their Brier
score worse than simply predicting the base rate in 26 of 34 folds. A model whose
stated confidence is worse than the base rate cannot carry a position, and the
gate said so.

**Because no candidate survived the research region, the holdout was never
opened.** No model has ever been fitted against data at or after the boundary,
and the pre-registered rule that reserves the holdout for survivors did exactly
what it was written to do.

Three criticisms of the frozen plan surfaced during execution and none was acted
on, because acting on them afterwards is the forbidden move: the `dense_only`
ablation was degenerate and C3 duplicated C1 in 15 of 16 cells; the
baseline-majority rule has no absolute floor, so a fold can be won at 31%
accuracy; and d10's labels mature before the boundary, so no d10 candidate could
have qualified even in principle — a gap in F.7's own adequacy check that it
should have caught. All three are recorded as negative knowledge for the next
pre-registration.

A latent defect was also found **before** the run and fixed: F.6's harness read a
`qualified` key that `verdict()` never sets, so it would have reported
NONE QUALIFIED whatever the candidates did. F.6's recorded result is provably
unaffected — zero folds, no candidate scored — and re-running it reproduces
INSUFFICIENT DATA exactly.

**D20 remains sealed.** Models promoted: **0**. Broker writes, paper orders, live
orders: **0 / 0 / 0**. Billable market-data requests: **0**. Portfolio, Risk,
OrderIntent, Execution, capture, IBKR and subscriptions: **unchanged**. The
production database was never opened for writing.

F.7 gave the project enough temporal history to ask the modelling question
fairly. F.6B asked it once, under the frozen rules, and the answer was no. **That
is a result about four pre-registered specifications on one dataset — not a
finding that no edge exists.**
