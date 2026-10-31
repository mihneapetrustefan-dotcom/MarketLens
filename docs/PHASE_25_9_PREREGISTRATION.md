# Phase 25.9 — Pre-Registration

**Committed BEFORE any candidate was evaluated.** The git timestamp on
this file is the evidence that the criteria below were not chosen after
seeing results. If a later commit edits a criterion, that edit is itself
the finding.

Written 2026-09-13.

---

## 1. Why pre-register

Phase 25.9 asks whether any model deserves a place in the trading
pipeline. The easiest way to get a false "yes" is to look at results,
then define success so that something passes. That is ruled out here by
writing the definition down first.

---

## 2. The data, as it actually is

Measured on the production database, timestamps only — no label value
was read to produce any number in this section.

| | |
|---|---|
| labelled observations (`d5.abnormal_return`) | 972 |
| distinct instruments | 299 (≈3 observations each) |
| labelled span | 52 days, 2026-07-06 → 2026-08-27 |
| label horizon | 5 trading days |

**This is a small dataset.** 299 instruments with about three
observations each means the effective sample, clustered by instrument,
is far smaller than 972. The criteria below are written knowing that.

---

## 3. Protected window — defined here, first

No protected window existed (`autoresearch_protected_windows` was
empty). One is defined now, before any evaluation, as the last 20% of
labelled observations by information cutoff.

| Region | From | To | Rows | Use |
|---|---|---|---|---|
| research | 2026-07-06T07:00:00 | 2026-08-15T01:05:00 | 777 | all development |
| **protected** | 2026-08-15T01:23:31 | 2026-08-27T17:09:32 | 195 | **one final evaluation, once** |

The boundary was computed from sorted timestamps alone.

**Rules for the protected window:**

1. No candidate, feature, hyperparameter, threshold or selection
   decision may consult it.
2. It is evaluated **exactly once**, after every choice is frozen.
3. A candidate that fails there **fails**. It is not adjusted and
   retested against the same data.
4. If no candidate survives the research region, the protected window
   is **not opened at all** and remains untouched for future work.

---

## 4. Mandatory baselines — unchanged

From `src/modeling/engine.py` `MANDATORY_BASELINES`:

- `baseline_historical_mean`
- `baseline_majority_class`

No baseline is added, removed or weakened.

---

## 5. The quality contract

A candidate is **QUALIFIED** only if **every** condition holds.

### 5.1 Statistical — the existing gate, unchanged

- beats **every** mandatory baseline on the primary metric,
  out-of-sample (`ModelEvaluation.beats_all_baselines`)
- effective sample ≥ 30 (`ModelEvaluation.MIN_EFFECTIVE_SAMPLE`)

Primary metric per task (`primary_metric_name`, unchanged):
`directional_accuracy` for DIRECTION, `mae` for regression.

### 5.2 Robustness — added because one split proves nothing

- evaluated with **expanding walk-forward** over the research region
- must beat the baselines in a **strict majority** of folds, not merely
  in aggregate — a high mean carried by one lucky fold does not count
- **at least 3 valid folds** must be formable. Fewer ⇒ verdict
  **INSUFFICIENT DATA**, regardless of any score

### 5.3 Calibration — probabilistic candidates only

- Brier score must beat the base-rate Brier score (always predicting
  the training base rate)

### 5.4 Protected window

- must also beat every mandatory baseline on the protected window, in
  its single evaluation

### 5.5 Multiple-testing

Four candidates are tested (§6). With four shots, one aggregate win is
weak evidence. The fold-majority rule (§5.2) is the defence: a
candidate must win consistently, not once. The candidate count is
fixed here and will not grow.

### 5.6 What does NOT count

Positive R², accuracy above 50%, a positive Sharpe, recent positive
P&L, beating the *previous* model, or a single winning window.

---

## 6. Candidates — fixed at four

| ID | Specification | Hypothesis | Expected failure |
|---|---|---|---|
| **C0** | `ridge_abnormal_return:v1`, α=1.0 | control: the incumbent, re-measured under walk-forward | already known to lose to baselines on one split; walk-forward should confirm |
| **C1** | `logistic_direction:v1`, all numeric features | direction is more learnable than magnitude; a different target and objective make it a genuinely independent specification, not a re-seed of C0 | small sample and noisy features may leave it at or below base rate |
| **C2** | ridge, α=10.0 | 26 features on ~116 effective clusters overfit; heavy shrinkage should at least stop it losing to the mean | shrinkage collapses toward the mean and **ties** the baseline rather than beating it |
| **C3** | logistic on low-missingness features only | sparse, frequently-missing features add noise; restricting to well-populated ones may clean the signal | fewer features means less information; may underfit |

**C3's feature rule is mechanical, fixed now:** keep a feature only if
it is non-missing in ≥ 90% of **research-region training rows**. It is
computed per fold from that fold's training rows alone, so it cannot
see the fold's test rows or the protected window.

No fifth candidate will be added after results are seen.

---

## 7. Leakage controls to verify before evaluating

- information cutoff strictly precedes the label window
- purge applied at the label horizon (5 days)
- embargo applied before each test fold
- any scaling fitted on training rows only
- C3 feature selection computed on training rows only
- no protected-window row in any development fold

---

## 8. Valid outcomes

All three are acceptable results of this phase:

- **QUALIFIED MODEL FOUND** — a candidate meets every condition in §5
- **NO MODEL QUALIFIED** — the research is sound and nothing passed
- **INSUFFICIENT DATA** — fewer than 3 folds could be formed, or the
  effective sample is below the gate

A correct "none qualified" is better than a false trading model.

---

## 9. Governance that does not move

`min_signal_confidence` (0.40), the deployability gate, the promotion
gate, human approval, the paper/live boundary, and the baselines above.
None of these is changed to make any candidate pass.
