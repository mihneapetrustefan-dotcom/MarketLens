# Phase 25.9I-F.6C — Pre-registration

**Probability Calibration & Directional Signal Validation**

**Written 2026-10-04, and committed before any F.6C candidate was evaluated.**

> **THE MODEL MUST EARN DISCRIMINATION.**
> **THE PROBABILITY MUST EARN TRUST.**
> **THE HOLDOUT MUST REMAIN UNTOUCHED UNTIL THE RULES SAY OTHERWISE.**

Every number in §3, §4 and §5 below was measured from fold **geometry** —
row counts, calendar spans and feature-column identities. None of it involves a
label value, a score or a model outcome. That is what makes it legitimate to
measure before freezing: §8 and §9 of the brief require horizon adequacy to be
proven *before* the plan is fixed.

---

## 1. The question, and why it is new

Phase 25.9I-F.6B ran a frozen 16-cell experiment and returned **NONE
QUALIFIED**. Verified from its artifacts, not from a summary:

| | |
|---|---|
| cells / evaluated / unevaluated | 16 / 16 / 0 |
| fold evaluations | 68 |
| execution errors | 0 |
| qualified | **0** |
| holdout evaluations | **0** — never opened |
| failed-gate tally | `FAILED_BASELINE` 10, `FAILED_CALIBRATION` 8 |

The precise decomposition, since the two tallies overlap:

- **8 ridge cells** failed the baseline-majority rule and nothing else. Baseline
  wins 0/5, 0/5, 0/4, 0/3 on d1, d3, d5, d10 — **zero in every fold**.
- **2 logistic cells** (d1:C1, d1:C3) failed **both** baseline majority (2/5) and
  calibration.
- **6 logistic cells** (d3, d5, d10 × C1, C3) **cleared** the baseline-majority
  rule — 3/5, 3/4, 2/3 — and failed **calibration alone**.

So the logistic family carried something the ridge family did not, and was
stopped by one specific thing: its probabilities were worse than predicting the
base rate.

**F.6C asks a narrower question than F.6B did:**

> Does the directional information in the logistic family survive time-safe
> out-of-sample testing, and can its probabilities be calibrated well enough to
> become trustworthy decision confidence?

**NO is a valid and expected answer.**

### What this phase explicitly does not do

It does not reopen a model-family search. Ridge's failure stands as negative
knowledge and ridge is not re-run. No new family is added. If the logistic
family fails here, the next question needs its own pre-registration.

---

## 2. Calibration cannot create discrimination

The hypothesis has two independent halves and they are scored separately,
never combined:

| | Question | Metrics |
|---|---|---|
| **A. DISCRIMINATION** | can the model separate up from down at all? | ROC-AUC, balanced accuracy, canonical baseline comparison |
| **B. CALIBRATION** | when it says P, is P trustworthy? | Brier, Brier skill score, log loss, ECE, calibration slope/intercept, reliability bins |

A model can be perfectly calibrated and useless — predict the base rate every
time — or sharply discriminative and wildly overconfident. **No combined score
is computed**, and a better Brier is not evidence of a trading edge.

A monotone calibrator cannot change ROC-AUC at all. That is a property of the
method, and it is the clearest statement of why calibration is not a rescue:
whatever ranking ability the model has is exactly what it had before.

---

## 3. The dataset — inherited, not rebuilt

| | |
|---|---|
| research snapshot | **`research_snapshot_v2`** |
| snapshot hash | `4ccab384d37a9eb249ce2bd61b44692c77a0df3dc7121dc79c3ed6940e66b0bc` |
| corpus version | **`canonical_corpus_v2`** |
| corpus hash | `4247f543904204c9d60f86b9ab6858e3bd3bd9fcc635e5950c5b60e148cb2ddb` |
| feature / label / universe / event version | `v1` each |
| as-of cutoff | `2026-08-30T14:35:00Z` |
| information coverage | 2026-07-02T07:00:00Z → 2026-08-30T14:35:00Z (59 days) |
| storage | `data/marketlens_corpus_v2.db` |
| state | `ELIGIBLE_FOR_RESEARCH` |

The corpus is **not** rebuilt. F.6B failing is not an integrity problem, and
rebuilding a dataset because an experiment on it failed is the opposite of
science. The snapshot hash is verified before the run and again after.

---

## 4. The holdout — inherited and still unopened

| | |
|---|---|
| **boundary** | **`2026-08-18T00:00:00Z`** |
| origin | **inherited unchanged from the F.7 handoff**, which derived it as the final 20% of the snapshot's information span (Phase 25.9's fraction, floored to the UTC day) |
| opened by F.6B? | **NO** — zero candidates survived, so it was never read |
| research region | 2026-07-02T07:00Z → 2026-08-18T00:00Z, **46 days** |
| holdout region | 2026-08-18T00:00Z → 2026-08-30T14:35Z, **12 days** |

**Inheriting is the most conservative option available and that is why it is
chosen.** The boundary predates this experiment, was derived by a rule that
predates the dataset, and has never been looked at. Defining a new boundary for
F.6C would invite exactly the suspicion that it had been placed where the
numbers were kinder.

F.6B's holdout is not moved. It is the same instant, still untouched.

---

## 5. Horizon eligibility — measured before freezing

A horizon is eligible only if **all** of the following are non-empty and
sufficient under the fold design in §6. Fold existence alone is not adequacy —
that is F.6B's d10 lesson, and it is now a rule.

| Horizon | Rows | Research region | Folds built | **Folds valid** | Calibration rows (per fold) | Test rows (per fold) | **Holdout rows** | **Verdict** |
|---|---|---|---|---|---|---|---|---|
| **d1** | 891 | 737 | 4 | **3** | 77 / 100 / 154 | 159 / 143 / 124 | **154** | **ELIGIBLE** |
| **d3** | 878 | 735 | 4 | **3** | 73 / 126 / 159 | 163 / 132 / 42 | **143** | **ELIGIBLE** |
| d5 | 805 | 735 | 3 | **2** | 86 / 138 | 125 / 104 | 70 | **DEFERRED_INSUFFICIENT_FOLDS** |
| d10 | 633 | 633 | 1 | **0** | — | — | **0** | **DEFERRED_INSUFFICIENT_HOLDOUT** |
| intraday_5m / 15m / 30m / 60m | 901 | — | — | — | — | — | — | **DEFERRED_BY_F7_POLICY** (no pre-registered intraday purge) |
| **d20** | — | — | — | — | — | — | — | **PROTECTED / SEALED** |

### d5 and d10 are excluded, and the boundary is not moved to save them

**d5** forms only **2** valid folds against the canonical minimum of **3**. The
three-way split costs calendar: adding a calibration window and its purge gap
consumes roughly two extra weeks of the research region compared with F.6B's
two-way split. d5's 7-day purge makes it the first casualty.

**d10** has **zero holdout rows** — its latest matured label is 2026-08-15, three
days before the boundary — and zero valid folds under this design.

Both are **DEFERRED, not adjusted**. The design in §6 was fixed on principle
before these counts were measured, and it is not being retuned to rescue a
horizon. The cost is stated plainly: **the calibration question can only be
asked on d1 and d3 with this dataset.**

---

## 6. Fold design — three windows, fixed on principle

Per fold, walking forward inside the research region:

```
TRAIN  [region start → train_end)
         ↓  purge  (the label's calendar span)
CALIB  [cal_start → cal_end)          7 days
         ↓  purge + embargo           (canonical treatment of a test boundary)
TEST   [test_start → test_end)        7 days
```

| Parameter | Value | Source |
|---|---|---|
| purge | label's **calendar** span: d1→3, d3→5 days | F.7 handoff, unchanged |
| embargo | **6 days** | Phase 25.9, unchanged |
| calibration window | **7 days** | symmetric with the test window |
| test window | **7 days** | Phase 25.9, unchanged |
| step | **7 days** | Phase 25.9, unchanged |
| min train rows | **30** | `ModelEvaluation.MIN_EFFECTIVE_SAMPLE` |
| min calibration rows | **30** | same canonical floor — a calibrator fitted on less is not evidence |
| min test rows | **20** | Phase 25.9, unchanged |
| **min valid folds** | **3** | `MIN_VALID_FOLDS`, unchanged |

**Why purge appears twice.** Purge exists because a label at time *T* is not
resolved until *T + horizon*. That hazard applies at the train→calibration
boundary exactly as it does at the calibration→test boundary, so it is applied
at both. Embargo — Phase 25.9's additional buffer at a *test* boundary — is
applied only before the test window, which is the boundary it was written for.

**Nothing is tuned.** Every parameter is either inherited unchanged or derived
from an existing canonical constant. The one new number is the 7-day calibration
window, set equal to the test window for symmetry rather than chosen from a
range.

---

## 7. Model family and feature configurations

**Family: logistic regression only.** The specification is Phase 25.9's `C1`,
imported verbatim from `research_model_quality.spec_for` — the same fitter,
hyperparameters and target as F.6B used, so that F.6C changes the *evaluation*
and not the model. Ridge is excluded on F.6B's evidence.

### Feature configurations: both kept, with a structural dedup rule

F.6B reported that `dense_only` duplicated `all_numeric`. **Audited, and it is
not generally true.** Under F.6C's fold geometry, the effective column sets are:

| Fold | `all_numeric` | fingerprint | `dense_only` | fingerprint | identical |
|---|---|---|---|---|---|
| d1 wf1 | 26 | `45dddece5017387d` | **25** | `4d39a08f2fb56b24` | **NO** |
| d1 wf2 | 26 | `45dddece5017387d` | 26 | `45dddece5017387d` | yes |
| d1 wf3 | 26 | `45dddece5017387d` | 26 | `45dddece5017387d` | yes |
| d3 wf1 | 26 | `45dddece5017387d` | **25** | `4d39a08f2fb56b24` | **NO** |
| d3 wf2 | 26 | `45dddece5017387d` | 26 | `45dddece5017387d` | yes |
| d3 wf3 | 26 | `45dddece5017387d` | 26 | `45dddece5017387d` | yes |

So `dense_only` differs in **2 of 6** folds. **F.6B's duplication was a property
of F.6B's fold geometry, not of the configuration.** Removing the configuration
would therefore be removing it because its *result* was poor, which §12 of the
brief forbids. **Both are kept.**

What is added instead is the structural rule §13 asks for:

> Every feature configuration carries a **fingerprint**: the SHA-256 of its
> sorted effective column names for that fold. Where two configurations
> fingerprint identically in a fold, they are the **same evidence** in that fold
> — they are reported as collapsed, and they may never be counted as two
> independent observations or as model agreement.

The fingerprint is computed from column identities only. No label value enters
it.

---

## 8. Calibration methods — three arms, one of them a control

| Arm | Method | Why it is in the plan |
|---|---|---|
| **raw** | none — the identity map | **The control.** Without it, "calibration helped" is an assertion. With it, it is a measured comparison, and F.6B's finding is reproduced under the new design. |
| **platt** | `sigmoid(a·logit(p) + b)`, unregularized MLE | parametric; the standard correction for systematic over- or under-confidence. Fitting it *is* fitting the calibration curve, so the correction applied and the miscalibration diagnosed cannot disagree. |
| **isotonic** | pool-adjacent-violators | non-parametric; can fix shapes no sigmoid can, and is correspondingly easier to overfit on a thin window. Included as the methodological contrast, not as a favourite. |

Implemented in `src/modeling/calibration.py`, **pure standard library, no new
dependency**. Beta calibration is **not** included: it would be a third method
for the same job, and the budget is deliberately small.

---

## 9. Calibration data separation — the hard rule

**A calibrator is never fitted on the observations used to report its
calibration quality.**

| Window | Used for |
|---|---|
| TRAIN | fitting the logistic model |
| CALIBRATION | fitting the calibrator, on the model's predictions for that window |
| TEST | reporting discrimination and calibration. **Not** used to fit anything |
| HOLDOUT | one evaluation, survivors only, once. **Never** used to fit a model, a calibrator or a threshold |

Time ordering, purge and embargo hold between all three research windows. **No
future label may influence an earlier calibrator**, and the base rate used by the
baseline predictor is computed from `TRAIN ∪ CALIBRATION` — exactly the
information available before the test window opens, and never from the test
outcomes themselves.

---

## 10. Metrics — a small justified set, fixed now

**Discrimination:** ROC-AUC, balanced accuracy, raw accuracy (reported for
context, never gated on), confusion counts, class positive rate.

**Calibration:** Brier score, **Brier skill score** against the base-rate
predictor, log loss, expected calibration error (with an explicit
`COMPUTED` / `NOT_COMPUTED` status and reason), calibration slope and intercept,
reliability bins with support counts.

**Stability:** per-fold values of every gated metric, plus mean, worst, best and
spread.

Nothing else is computed. In particular PR-AUC is **not** in the set: the class
balance here ranges from 31% to 64% positive across folds, which is not the
severe imbalance PR-AUC exists for, and adding a metric per gate is how a plan
turns into a leaderboard.

**Reliability bins:** 5 bins, and a bin with fewer than **10** observations is
marked unsupported and excluded from the ECE. A ten-bin curve with one point per
bin will not be produced.

---

## 11. The absolute-quality requirement, and where its numbers come from

F.6B exposed a cell credited with a baseline "win" at **31.2%** directional
accuracy against a **26.0%** baseline. Both numbers are bad; the rule only asked
which was higher.

F.6C adds an absolute requirement. **Its thresholds are not invented — they are
the no-skill points of the metrics themselves:**

| Requirement | Threshold | Why that number is not a choice |
|---|---|---|
| **balanced accuracy > 0.5** | 0.5 | Balanced accuracy is the mean of sensitivity and specificity. **Any** constant predictor scores exactly 0.5, whatever the class balance. Verified: a predictor that always says "up" on a 23.5%-positive window scores raw accuracy 0.235 and balanced accuracy **exactly 0.500**. |
| **ROC-AUC > 0.5** | 0.5 | 0.5 is the AUC of any constant or random ranking, by construction. |
| **Brier skill score > 0** | 0 | BSS is `1 − BS_model/BS_baseline`, which is zero **exactly** when the model matches its base-rate baseline. |

Each is the point at which a metric says "no skill". None was selected by looking
at F.6B's numbers or at any F.6C result. Raw accuracy is deliberately **not**
gated on, because it inherits the fold's class balance — which is precisely how
the 31.2% cell slipped through.

---

## 12. Baselines

| Purpose | Baseline |
|---|---|
| canonical comparison | `ModelingEngine.train_and_evaluate`'s mandatory baselines, unchanged |
| probability metrics | **base-rate predictor** — a constant probability equal to the positive rate of `TRAIN ∪ CALIBRATION` |
| discrimination | the 0.5 no-skill points of balanced accuracy and ROC-AUC (§11) |

A Brier score is compared only against a probability baseline, and a directional
accuracy only against a directional baseline. They are not crossed.

---

## 13. Gate order — discrimination before calibration

Evaluated in this order, and a candidate that fails a gate **does not proceed**:

```
1. SAMPLE / DATA INTEGRITY   research-region clusters ≥ 30; ≥ 3 valid folds;
                             non-empty calibration, test and holdout regions
2. DISCRIMINATION            canonical baseline majority
                             AND balanced accuracy > 0.5
                             AND ROC-AUC > 0.5
                             — each in a STRICT MAJORITY of folds
3. ROBUSTNESS                the strict-majority requirement is the robustness
                             rule: a candidate carried by one fold cannot pass
4. CALIBRATION               Brier skill score > 0 in a STRICT MAJORITY of folds
5. CANONICAL + HOLDOUT       survivors only. `ModelEvaluation.is_deployable`
                             must be True on one holdout evaluation, once
```

**Calibration cannot rescue a model with no discrimination**, because gate 2
comes first and stops it. **Discrimination cannot bypass bad calibration**,
because gate 4 still has to pass. Both directions are tested.

---

## 14. Qualification, and the canonical gate

A candidate is a **QUALIFIED DIRECTIONAL RESEARCH CANDIDATE** only if it passes
**every** gate in §13, including the canonical `is_deployable` on the holdout.

The canonical gate does **not** currently include probability calibration — it is
`beats_all_baselines AND NOT small_sample`. F.6C therefore reports **two
verdicts side by side**:

- `canonically_qualified` — the existing gate's answer, unchanged;
- `f6c_qualified` — canonical **AND** every F.6C gate.

**F.6C only ever adds requirements.** It cannot qualify anything the canonical
gate rejects, and it is not a shortcut around it. The canonical gate is not
modified, extended or bypassed, and existing governance is preserved exactly.

**Ranking rule**, fixed now in case more than one candidate qualifies: order by
holdout Brier skill score descending, then holdout ROC-AUC descending, then
cell id ascending. "Best" may only ever mean this ordering.

---

## 15. Candidate budget

```
2 horizons (d1, d3) × 2 feature configurations × 3 calibration arms = 12 CELLS
```

**The budget is 12 and will not grow.** No hyperparameter sweep, no additional
family, no seed exploration, no extra calibration method, and no branch in this
plan that responds to a result.

Expected compute: 3 folds × 2 configurations × 2 horizons = **12 model fits**;
12 fits × 2 fitted calibrators = **24 calibrator fits**; plus at most 12 holdout
model fits and 24 holdout calibrator fits if every candidate survives.

**Independence:** the three calibration arms share one underlying model fit per
(horizon, configuration, fold). They are **not** three independent models and may
never be counted as model agreement. Nor may two feature configurations that
fingerprint identically in a fold (§7).

---

## 16. Abstention — NOT TESTED

F.6C does **not** test abstention.

Choosing a probability band around the decision boundary would mean either
inventing a threshold — the thing §11 exists to prevent — or spending budget
learning one, which would enlarge an experiment whose question is narrower than
that. Coverage-versus-quality is a real question and it deserves its own
pre-registration.

Reported as **NOT TESTED**, not as zero and not as unavailable.

---

## 17. What a qualified directional model would and would not provide

| | |
|---|---|
| `directional_signal_capable` | possibly **YES** |
| `expected_return_capable` | **NO** |
| `target_capable` | **NO** |

**P(price up) is not an expected return.** A directional probability carries no
magnitude, so no expected return may be derived from it and no numeric target may
be produced from it. Those require a separate qualified magnitude model, and
F.6B's ridge result says the project does not have one.

These three capabilities are reported separately and never collapsed. A model
that is directionally useful while expected return and target remain
**UNAVAILABLE** is a legitimate outcome, not a half-failure.

---

## 18. Downstream boundary

A qualified candidate may feed the **F.5 shadow diagnostic only**. The canonical
signal-confidence formula is **not** modified. `PRODUCTION_ACTIVE` stays false.
No production capital allocation, no `OrderIntent`, no execution, no broker
write, no paper or live order, and **zero billable market-data requests**.

Horizon disagreement is preserved: if d1 and d3 point different ways, the
preferred horizon may legitimately remain `None`. A majority vote never overrides
directional disagreement.

---

## 19. Stopping rules

Research stops for a cell when any of these is true:

- all three calibration arms have been evaluated (the budget is 12 cells total);
- fewer than 3 valid folds can be formed ⇒ **INSUFFICIENT DATA**;
- the research-region effective sample falls below 30;
- the candidate fails a gate in §13 — it does not proceed to the next.

**There is no round two.** If nothing qualifies, the phase reports that and ends.
A new hypothesis needs a new pre-registration.

---

## 20. Valid outcomes

All acceptable, and the phase is COMPLETE in every case:

- **N QUALIFIED DIRECTIONAL RESEARCH CANDIDATES** — one or more pass every gate.
- **NONE QUALIFIED** — the research is sound and nothing passed.
- **INSUFFICIENT DATA** — folds, calibration windows or holdout could not support
  a fair test.

**Calibration is not a tool for turning a failed model into a passing model.**
If the logistic family has no real discrimination, no calibrator will give it
any, and this plan is built so that the attempt cannot succeed by accident.

---

## 21. Governance that does not move

`MIN_EFFECTIVE_SAMPLE` (30), `MIN_VALID_FOLDS` (3), `min_signal_confidence`
(0.40), `ModelEvaluation.is_deployable`, the mandatory baselines, the confidence
formula, the promotion gate, the paper/live boundary and the D20 seal. **None is
changed to make any candidate pass.**

---

## 22. F.6B is immutable

F.6B remains **PHASE IMPLEMENTATION = COMPLETE / MODEL QUALIFICATION = NONE
QUALIFIED**. Its pre-registration, handoff, results artifact, execution marker,
candidate rows and holdout boundary are not edited, regenerated, recomputed under
new rules, or reinterpreted. F.6C creates a new experiment id, a new
pre-registration, a new execution marker and new result artifacts.

Its negative knowledge is carried forward, not erased: ridge's failure, the raw
logistic calibration failure, the degenerate-configuration finding, the
weak-absolute-quality finding and the empty-d10-holdout finding.

---

## 23. D20

Not trained on, not calibrated on, not evaluated, not read, not inferred. Not
used for horizon, feature, family, method, threshold or abstention selection, and
not used for expected return or target estimation. The horizon appears as
**PROTECTED** in inventories and nowhere else.
