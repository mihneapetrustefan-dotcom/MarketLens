# Phase 25.9 — Final Report

**Verdict** NO MODEL QUALIFIED
**Pre-registration** `c09ff19` (criteria) · `e71ca9a` (harness) — both
committed before any evaluation
**Protected window** NOT OPENED
**Written** 2026-09-13

---

## A. Executive summary

**FACT** No candidate qualified under the pre-registered contract.
**EVIDENCE** Four candidates, three valid walk-forward folds, full
per-fold results in §J. The protected window was never opened.
**STATUS** Correct and final for this dataset.

**The more important result is a defect in the existing gate.**
Candidate C1 beat every mandatory baseline in all three folds while
averaging **0.504 directional accuracy** — a coin flip. The Phase 18
deployability gate would have certified it. Only the calibration
criterion added in the pre-registration refused it. See §U.

---

## B. How the result was protected from bias

| Commitment | Commit | When |
|---|---|---|
| quality contract, candidates, protected window | `c09ff19` | 12:57:20, before any evaluation |
| evaluation harness | `e71ca9a` | 12:59:38, before its first run |
| first and only research run | — | after both |

The protected window did not exist before this phase
(`autoresearch_protected_windows` was empty). It was defined from
timestamps alone, with no label value read.

---

## C. Dataset

| | |
|---|---|
| label | `d5.abnormal_return`, 5 trading days |
| labelled observations | 972 |
| instruments | 299, ≈3 observations each |
| labelled span | 52 days, 2026-07-06 → 2026-08-27 |
| research region | 777 rows, 39 days |
| protected region | 195 rows, reserved |
| numeric features | 26 |

**This is a small dataset.** ≈3 observations per instrument means the
clustered effective sample is far below the row count.

---

## D. Leakage audit

| Control | Result |
|---|---|
| purge at label horizon | applied, **7 calendar days** |
| embargo before each test fold | applied, 6 days |
| protected rows in any development fold | none — asserted by test |
| C3 feature selection | computed per fold on training rows only — asserted |
| baselines fit on training rows only | yes, existing engine |

**One pre-registration deviation, disclosed.** I wrote "purge at 5
days". `d5` is five *trading* days, which spans up to seven calendar
days across a weekend, and `WalkForwardSplitter` warns that a short
horizon "silently under-protects". Seven can only purge more rows, so
it cannot admit leakage. Decided before evaluation.

**Status: PASS.**

---

## E–F. Candidates and hypotheses

| ID | Specification | Hypothesis |
|---|---|---|
| C0 | ridge α=1.0, incumbent | control |
| C1 | logistic direction, all features | direction more learnable than magnitude; independent objective |
| C2 | ridge α=10.0 | shrinkage stops overfitting 26 features on ~116 clusters |
| C3 | logistic, features ≥90% populated | dropping sparse features removes noise |

Four, fixed in advance. No fifth was added.

---

## H. Methodology

Expanding walk-forward, day-granular, inside the research region.
Built in days because `WalkForwardSplitter` works in months with a
36-month default and cannot form one fold from 39 days. `purge`,
`embargo` and `ModelingEngine.train_and_evaluate` are the library's
own, so baselines and metrics are unchanged.

| Fold | Test window | Train rows | Test rows | Purged |
|---|---|---|---|---|
| wf1 | 07-26 → 08-02 | 176 | 189 | 125 |
| wf2 | 08-02 → 08-09 | 307 | 166 | 183 |
| wf3 | 08-09 → 08-15 | 496 | 121 | 160 |

Exactly the three valid folds the contract requires.

---

## J. Walk-forward results

### C0 — incumbent ridge

| Fold | MAE | vs baselines |
|---|---|---|
| wf1 | 0.0828 | loses |
| wf2 | 0.0462 | loses |
| wf3 | 0.0414 | loses |

**0/3. NOT QUALIFIED.**

### C1 — logistic direction

| Fold | Dir. acc. | Baseline | Brier | Base-rate Brier |
|---|---|---|---|---|
| wf1 | 0.456 | 0.270 | 0.367 | 0.293 |
| wf2 | 0.564 | 0.530 | 0.274 | 0.252 |
| wf3 | 0.492 | 0.479 | 0.299 | 0.253 |

Beats baselines **3/3**. Brier worse than base rate **3/3**.
**NOT QUALIFIED** on calibration.

### C2 — ridge α=10

| Fold | MAE | vs baselines |
|---|---|---|
| wf1 | 0.0700 | loses |
| wf2 | 0.0439 | loses |
| wf3 | 0.0414 | loses |

**0/3. NOT QUALIFIED.** Shrinkage narrowed the gap, as hypothesised,
and still lost — the expected failure mode, confirmed.

### C3 — logistic, dense features

| Fold | Dir. acc. | Baseline | Beats |
|---|---|---|---|
| wf1 | 0.529 | 0.270 | yes |
| wf2 | 0.518 | 0.530 | no |
| wf3 | 0.455 | 0.479 | no |

**1/3. NOT QUALIFIED.**

---

## K. Protected test

**NOT OPENED.** No candidate survived the research region
(pre-registration §3.4). The 195 protected rows remain untouched and
available for a future, un-tuned-against evaluation.

---

## L–Q. Calibration, robustness, economics, regime, instruments

| Dimension | Result |
|---|---|
| calibration | **FAIL** — both probabilistic candidates miscalibrated |
| robustness | ridge fails consistently; logistic inconsistent across folds |
| economic significance | **INSUFFICIENT DATA** — no candidate reached it |
| cost sensitivity | not reached |
| regime | a sharp shift in wf1 (58% → 27% up) dominates the direction results |
| instrument generalisation | **INSUFFICIENT DATA** — ≈3 observations per instrument |

---

## U. The model-quality gate has a safety defect

**FACT** The mandatory `majority_class` baseline scores **below 0.5**
whenever the test period's direction differs from training.

**EVIDENCE**

| Fold | Train up-rate | Test up-rate | Baseline scores |
|---|---|---|---|
| wf1 | 0.580 | **0.270** | **0.270** |
| wf2 | 0.580 | 0.530 | 0.530 |
| wf3 | 0.462 | 0.521 | 0.479 |

The baseline is correctly fit on training data. After a regime shift
it predicts the wrong majority, and any model no better than chance
beats it.

**RESULT** C1 beats every mandatory baseline in every fold, with
effective samples of 127, 116 and 93 — all above the gate's 30. So
`ModelEvaluation.is_deployable` returns **True** for a model averaging
0.504 directional accuracy. **The existing gate would certify a
coin-flip model as deployable.**

**STATUS** Not changed in this phase. It is a governance decision what
every future model must clear. Recommended fix, for a human decision:

- add a **chance floor** for direction tasks, so a model must beat 0.5
  as well as the fitted baselines; or
- make **calibration** mandatory for probabilistic models, which is the
  criterion that caught C1 here; or both.

Pinned by `TestTheMandatoryBaselineIsWeakUnderRegimeShift`.

---

## Z. Final decision

**NO MODEL QUALIFIED.** The research is sound, the protected window is
intact, and the negative results are recorded here permanently.

The deeper reason is the data, not the modelling. 52 days across 299
instruments, ≈3 observations each, cannot support confident claims
about predictive skill, and a single regime shift decides the direction
results. **More history is the prerequisite for the next attempt.**

---

## AA–AB. Readiness

**Phase 25.95 NOT READY** — there is no qualified model, so no
legitimate signal to place a paper order against.

**Phase 26 NOT READY** — shadow trading needs fresh legitimate
decisions, and there are none.

**Next required step: additional model research**, gated on
accumulating history, plus a governance decision on the gate defect
in §U before any future model is promoted.
