# Phase 25.9I-F.6C — Probability Calibration & Directional Signal Validation

> ## THE EXPERIMENT IS DEFINED BEFORE THE RESULT IS KNOWN.
>
> ## NO MODEL QUALIFYING IS A VALID RESULT.

> Read-only against the frozen snapshot. No threshold moved, no gate weakened,
> no model promoted, no order placed, zero billable market-data requests. F.6B's
> artifacts are untouched. D20 remains sealed.

Written 2026-10-04. Completion report: `docs/PHASE_25_9I_F6C_REPORT.md`.
Frozen plan: `docs/PHASE_25_9I_F6C_PREREGISTRATION.md`, sha256
`ba141fd9fdead1bc8b56d524a0596b03f7f8a97bde5b98bc8caaf9c8ede66ede`, committed at
**`2f1977f`** — a commit whose tree contains that document and **no harness, no
results artifact and no execution marker**. The ordering is provable from git.

---

## A. Why F.6C exists

F.6B ran a frozen 16-cell experiment and returned **NONE QUALIFIED**. Verified
from its own artifacts rather than from a summary:

- **8 ridge cells** lost the baseline-majority rule in **every fold** — 0/5,
  0/5, 0/4, 0/3.
- **2 logistic cells** (d1) failed both baseline majority and calibration.
- **6 logistic cells** (d3, d5, d10) **cleared** the baseline rule — 3/5, 3/4,
  2/3 — and failed **calibration alone**.

So the logistic family looked like it carried something the ridge family did
not. F.6C asks the narrow follow-up question:

> Does that directional information survive time-safe out-of-sample testing, and
> can the probabilities be calibrated into trustworthy decision confidence?

**It does not reopen a model-family search.** Ridge's failure stands as negative
knowledge; ridge is not re-run and no new family is added.

---

## B. Calibration cannot create discrimination

The hypothesis has two halves, scored and gated separately, never combined into
one number:

| | Question | Metrics |
|---|---|---|
| **DISCRIMINATION** | can it separate up from down at all? | ROC-AUC, balanced accuracy, canonical baseline comparison |
| **CALIBRATION** | when it says P, is P trustworthy? | Brier, Brier skill, log loss, ECE, calibration slope/intercept, reliability bins |

The decisive structural fact: **a strictly monotone calibrator cannot change
ROC-AUC.** Platt scaling is monotone, so whatever ranking ability the model has
after calibration is exactly what it had before. A test asserts this to six
decimal places on a fixture. Calibration can only ever fix *how the confidence
is stated*, never *whether there is anything to be confident about*.

---

## C. The dataset and the holdout — both inherited

| | |
|---|---|
| snapshot | **`research_snapshot_v2`**, `ELIGIBLE_FOR_RESEARCH` |
| snapshot hash | `4ccab384d37a9eb249ce2bd61b44692c77a0df3dc7121dc79c3ed6940e66b0bc` |
| corpus | `canonical_corpus_v2`, hash `4247f543904204c9…` |
| **holdout boundary** | **`2026-08-18T00:00:00Z`** |
| origin | **inherited unchanged from the F.7 handoff**; never opened by F.6B |

The corpus is not rebuilt. F.6B failing is not an integrity problem, and
rebuilding a dataset because an experiment on it failed is the opposite of
science.

**Inheriting the boundary is the most conservative option available.** It
predates this experiment, was derived by a rule that predates the dataset, and
has never been read. Defining a new boundary for F.6C would invite exactly the
suspicion that it had been placed where the numbers were kinder.

`preflight()` does not merely read the boundary — it **re-derives** it from the
snapshot and refuses to run if the rule no longer yields the same instant.

---

## D. Three windows, and what they cost

F.6B split each fold into train and test. A calibration experiment needs a third
window, because a calibrator fitted on the data used to score it is marking its
own homework:

```
TRAIN  --purge-->  CALIBRATE (7d)  --purge + embargo-->  TEST (7d)
```

**Purge appears at both internal boundaries.** A label at *T* is unresolved until
*T + horizon*, and that hazard exists at the train→calibration boundary exactly
as at the calibration→test boundary. Embargo — Phase 25.9's extra buffer at a
*test* boundary — is applied only before the test window, which is the boundary
it was written for.

### The price, measured before the plan was frozen

| Horizon | Folds valid | Holdout rows | Verdict |
|---|---|---|---|
| **d1** | **3** | **154** | **ELIGIBLE** |
| **d3** | **3** | **143** | **ELIGIBLE** |
| d5 | **2** (needs 3) | 70 | **DEFERRED_INSUFFICIENT_FOLDS** |
| d10 | **0** | **0** | **DEFERRED_INSUFFICIENT_HOLDOUT** |

The third window consumes roughly two extra weeks of research region. d5's
7-day purge makes it the first casualty; d10 had no holdout to begin with.

**Both are deferred, not rescued.** The design was fixed on principle before
these counts were measured, and it was not retuned afterwards. The cost is stated
plainly: with this dataset, the calibration question can only be asked on **d1
and d3**.

This is F.6B's d10 lesson promoted to a rule: **fold existence is not adequacy.**
A horizon needs a non-empty train window, a non-empty calibration window, a
non-empty test window **and** a non-empty final holdout, or it cannot qualify.

---

## E. Where the absolute-quality numbers come from

F.6B credited a cell a baseline "win" at **31.2%** directional accuracy against a
**26.0%** baseline. Both numbers are bad; the rule only asked which was higher.

F.6C adds an absolute requirement whose thresholds **are the no-skill points of
the metrics themselves**:

| Requirement | Threshold | Why it is not a chosen number |
|---|---|---|
| balanced accuracy > 0.5 | 0.5 | mean of sensitivity and specificity, so **any** constant predictor scores exactly 0.5 whatever the class balance |
| ROC-AUC > 0.5 | 0.5 | the AUC of any constant or random ranking, by construction |
| Brier skill score > 0 | 0 | `1 − BS_model/BS_base` is zero **exactly** when the model matches its base-rate baseline |

Verified in a test: an always-up predictor on a 23.5%-positive window scores raw
accuracy **0.235** and balanced accuracy **exactly 0.500**. A further test sweeps
the positive share from 10% to 90% and gets 0.5 every time.

**Raw accuracy is deliberately not gated on**, because it inherits the fold's
class balance — which is precisely how the 31.2% cell slipped through.

---

## F. Gate order — discrimination first

```
1. SAMPLE / DATA INTEGRITY   >= 3 valid folds, pooled test clusters >= 30,
                             non-empty calibration, test and holdout regions
2. DISCRIMINATION            canonical baseline majority
3. ABSOLUTE QUALITY          balanced accuracy > 0.5 AND ROC-AUC > 0.5
4. ROBUSTNESS                the strict-majority rule itself: no single fold
                             can carry a candidate
5. CALIBRATION               Brier skill score > 0, strict majority of folds
6. CANONICAL + HOLDOUT       survivors only, `is_deployable` True, once
```

Each gate is a **strict majority of folds**. A candidate that fails a gate does
not proceed, and the gates it never reached are recorded as `NOT_REACHED` —
deliberately distinct from `FAIL`, because a gate that was never evaluated has
not been passed.

**Calibration cannot rescue a model with no discrimination**, because gate 3
stops it first. **Discrimination cannot bypass bad calibration**, because gate 5
still has to pass. Both directions are tested with fixtures.

---

## G. Qualification: two verdicts, side by side

The canonical gate is `beats_all_baselines AND NOT small_sample`. It does **not**
include probability calibration. Rather than extend it — and rather than quietly
substitute a different gate — F.6C reports both:

| | |
|---|---|
| `canonically_qualified` | the existing gate's answer on the holdout, unchanged |
| `f6c_qualified` | canonical **AND** every F.6C gate |

**F.6C only ever adds requirements.** It cannot qualify anything the canonical
gate rejects, and a test asserts that `f6c_qualified` depends on
`is_deployable`. Existing governance is preserved exactly; no F.6C-only shortcut
exists.

**Ranking rule**, fixed in advance in case more than one qualifies: holdout Brier
skill descending, then holdout ROC-AUC descending, then cell id. "Best" may only
ever mean that ordering.

---

## H. The calibration module

`src/modeling/calibration.py`, **pure standard library — a test asserts its only
imports are `math`, `dataclasses`, `typing` and `__future__`.**

| Arm | Method | Role |
|---|---|---|
| **raw** | identity | **the control.** Without it, "calibration helped" is an assertion rather than a measurement |
| **platt** | `sigmoid(a·logit(p) + b)`, unregularized MLE | parametric; the standard correction for systematic over- or under-confidence |
| **isotonic** | pool-adjacent-violators | non-parametric; fixes shapes no sigmoid can, and overfits a thin window more easily |

Beta calibration is excluded: a third method for the same job, against a
deliberately small budget.

**Platt scaling and the calibration curve are the same regression.** Fitting
`y ~ sigmoid(a·logit(p) + b)` gives the calibration slope `a` and intercept `b`
directly, with `a = 1, b = 0` meaning already calibrated. One fitter serves both,
so the correction applied cannot disagree with the miscalibration diagnosed.

Honest refusals rather than invented numbers: the curve returns `NOT_COMPUTED`
with a reason on perfect separation (no finite MLE exists), ECE returns
`NOT_COMPUTED` when no probability bin reaches 10 observations, and `NOT_COMPUTED`
is deliberately distinct from `0.0` — which would mean *perfect* calibration.

### The defect that was found by distrusting a result

The first execution reported Platt `NOT_FITTED` on **8 of 12** fold attempts, on
windows of 73–159 rows with both classes present. That looked wrong, and it was:

`_fit_scalar_logistic` initialised Newton-Raphson at `slope = 1`. An input of
`logit(p)` reaches ±14, so a unit slope saturates every sigmoid on the first
step, the IRLS weights `p(1−p)` underflow, the Hessian goes singular, and the fit
reports "degenerate" on data that converges in **three** iterations from a sane
start.

Fixed by **null-model initialisation** (`slope = 0`, `intercept = logit(ȳ)`) plus
**damped Newton** (halve the step until the likelihood improves). Genuinely
degenerate inputs — single class, no spread, too few rows — are still refused,
and the known-good fixture reproduces its slope to the digit, so the fix changed
nothing that already worked.

The affected execution was **marked `invalidated` with the reason and kept on the
record**, and the experiment was re-run with a new execution id. The register now
shows one invalidated attempt and one valid execution, which is the truthful
history rather than the tidy one.

---

## I. Feature configuration identity

F.6B reported that `dense_only` duplicated `all_numeric`. **Audited, and not
generally true.** Under F.6C's fold geometry the effective column sets differ in
**2 of 6** folds (25 columns versus 26).

So F.6B's duplication was a property of **F.6B's fold geometry**, not of the
configuration. Removing the configuration would therefore be removing it because
its *result* was poor. **Both are kept**, and the structural rule is added
instead:

> Every configuration carries a **fingerprint** — the SHA-256 of its sorted
> effective column names for that fold. Where two configurations fingerprint
> identically in a fold, they are the **same evidence** there, and may never be
> counted as two observations or as model agreement.

Computed from column identities only; a test asserts the function body mentions
no label, outcome or `value_json`.

**It earned its place immediately.** Exactly two fold-evaluations in the whole
experiment produced positive Brier skill — and they are the *same fold* under two
configurations whose fingerprints are identical there, with identical skill
values. Counting them as two would have been counting one thing twice. A test
pins that.

---

## J. Calibration data separation

| Window | Used for |
|---|---|
| TRAIN | fitting the logistic model |
| CALIBRATION | fitting the calibrator, on the model's predictions for that window |
| TEST | reporting discrimination and calibration. Fits **nothing** |
| HOLDOUT | one evaluation, survivors only, once. Fits **nothing** — not a model, not a calibrator, not a threshold |

The base rate used by the probability baseline comes from **`TRAIN ∪
CALIBRATION`** — exactly the information available before the test window opens,
and never from the test outcomes themselves.

Tests assert, for every fold of every horizon: train precedes calibration
precedes test; no window reaches into the holdout; purge is applied at the
train/calibration boundary; and fold construction is deterministic.

---

## K. What a qualified directional model would not provide

| | |
|---|---|
| `directional_signal_capable` | possibly YES |
| `expected_return_capable` | **NO** |
| `target_capable` | **NO** |

**P(price up) is not an expected return.** A directional probability carries no
magnitude, so no expected return may be derived from it and no numeric target may
be produced from it. Those need a separate qualified magnitude model, and F.6B's
ridge result says the project does not have one.

The three capabilities are reported separately and never collapsed. A model that
is directionally useful while expected return and target remain **UNAVAILABLE**
would be a legitimate outcome, not a half-failure.

---

## L. Abstention — NOT TESTED

Choosing a probability band around the decision boundary would mean either
inventing a threshold — the thing §E exists to prevent — or spending budget
learning one, which would enlarge an experiment whose question is narrower.
Coverage-versus-quality deserves its own pre-registration.

Reported as **NOT TESTED**: not zero, not unavailable, not quietly omitted.

---

## M. Negative knowledge, and F.6B's immutability

F.6B remains **COMPLETE / NONE QUALIFIED**. Its pre-registration, handoff,
results artifact, execution marker, candidate rows and holdout boundary are not
edited, regenerated, recomputed under new rules or reinterpreted. `preflight()`
**verifies** F.6B's marker still reads `complete` with `qualified_count = 0` and
its 16 candidate rows with 0 qualified, and refuses to run if any of that moved.

F.6C creates a new experiment id, a new pre-registration, a new execution marker
and new result artifacts. F.6B's negative knowledge is carried forward, not
erased: ridge's failure, the raw logistic calibration failure, the
degenerate-configuration finding, the weak-absolute-quality finding and the
empty-d10-holdout finding.

Failed candidates are data. `research_candidate_results` retains every cell with
its gates, stability, full fold-level evidence and lineage, so a future
autonomous phase can see what has already lost on this snapshot.

---

## N. Run-once discipline

`research_executions` records the pre-registration hash, snapshot hash, holdout
boundary, code commit, the full cell list, its hash, the survivor list hash, the
completed cells and the status.

- a COMPLETE marker for the same (pre-registration, snapshot) pair **blocks** a
  second run;
- the **survivor list is hashed and persisted before the holdout is touched**, so
  the set of candidates that earned a holdout look cannot be revised afterwards;
- an invalidated run is annotated and kept, never deleted.

If zero candidates survive, the holdout is **not opened** and the record says so
explicitly.

---

## O. D20 isolation

| | |
|---|---|
| executed / inspected / inferred / consumed | **NO** |
| trained on / calibrated on / evaluated | **NO** |
| used for horizon, feature, family, method, threshold or abstention selection | **NO** |
| used for expected return or target estimation | **NO** |
| hypothesis / anchor-v2 / protected window changed | **NO** |

`DEFERRED["d20"] = "PROTECTED"`, it is absent from `HORIZONS`, and
`rs.is_protected` agrees. Tests assert that neither the harness nor the
calibration module contains `d20.abnormal_return`, `d20.raw_return`,
`validate_d20_reversal`, `anchor_v2`, `return_60d`, `check_d20_readiness` or
`value_json` — so no protected label value can be read at all.

---

## P. Production boundary

| | |
|---|---|
| broker writes attempted / succeeded | 0 / 0 |
| paper orders / live orders | 0 / 0 |
| billable market-data requests | **0** |
| Portfolio / Risk / OrderIntent / Execution | unchanged |
| capture / IBKR / subscriptions | unchanged |
| models promoted | **0** |

The snapshot is read `mode=ro` for every evaluation; the only writes are the
execution marker and candidate rows. The production database
(`data/marketlens.db`) is never opened for writing, and a test asserts it holds
no `research_executions` table. Tests assert the harness imports no execution,
broker, portfolio, risk, capture or network module.

The hard separation stands: **research qualified ≠ shadow validated ≠ paper
authorised ≠ live authorised.** F.6C ends at the first.

---

## Q. Handoff to I-G

Only if a candidate legitimately qualifies. It would carry the candidate ids,
horizon, snapshot and hash, pre-registration and hash, discrimination evidence,
calibration evidence, holdout evidence, probability semantics, abstention
semantics, confidence semantics, known limitations,
`expected_return_capable` / `target_capable`, and `production_active = FALSE` /
`broker_authorized = FALSE`.

If zero qualify, **no I-G trading handoff is fabricated.** The honest output is
**RESEARCH FOLLOW-UP REQUIRED**, and the follow-up needs a new hypothesis and a
new pre-registration — not another pass at this one.

---

## R. What this phase can and cannot settle

It can settle whether these probabilities, from this specification, on this
dataset, are trustworthy enough to carry decision confidence.

It cannot settle whether the features are too weak, whether direction is
regime-conditional, or whether the classification target itself is the wrong
question. Those are real possibilities and each needs its own experiment.

> **Calibration is not a tool for turning a failed model into a passing model.**
> It answers a narrower question: **when the model says 70%, does 70% mean
> something?** First the model must contain legitimate directional information.
> Only then may its probability contribute to decision confidence.
