# Phase 25.9I-F.6D — Feature Information & Predictive Content Audit

> ## A MORE COMPLEX MODEL CANNOT CREATE INFORMATION THAT IS NOT IN THE FEATURES.
>
> ## FEATURE DISCOVERY IS NOT MODEL QUALIFICATION.

> Read-only against the frozen snapshot. No model family fitted, nothing tuned,
> nothing qualified, nothing promoted, no order placed, zero billable
> market-data requests. F.6B and F.6C are untouched and verified before the run.
> D20 remains sealed.

Written 2026-10-04. Completion report: `docs/PHASE_25_9I_F6D_REPORT.md`.
Frozen plan: `docs/PHASE_25_9I_F6D_PREREGISTRATION.md`, sha256
`4563b3ef1087f5e2a79597e6438b4ec525759d2d235242fdd2e84b11c0cce137`, committed at
**`2e3cc2d`** — a commit whose tree contains that document and **no harness, no
results artifact and no execution marker**.

---

## A. Why F.6D exists

F.6C established that the problem was upstream of calibration: ROC-AUC over 36
fold-evaluations spanned 0.390–0.616, meaned **0.5136**, and cleared its
no-skill point in exactly **18 of 36** — a coin flip. Calibration improved the
probabilities measurably and could not make them useful, because there was no
discrimination to preserve.

That leaves one question that has to be answered before any further modelling is
defensible:

> **Do these features contain stable, out-of-sample predictive information at
> all?**

F.6D asks it directly rather than through a model. It is not asking "can
logistic regression trade" — it is asking whether the inputs carry anything a
model could use.

**No model search happens here.** No new family, no tuning, no qualification.

---

## B. The real inventory is 33, not 26

Derived from `research_features`, not assumed. This matters: every prior phase
reported "26 features", which is the design-matrix count, not the registry.

| | Count | |
|---|---|---|
| registered features | **33** | |
| numeric and usable | **26** | the design matrix every model has used |
| categorical | **5** | `entity.asset_class`, `entity.exchange_id`, `entity.ticker`, `event.category`, `event.event_type` |
| **registered but `null` in all 1,056 rows** | **2** | `peer.dispersion_5d`, `peer.relative_return_5d` |

The 5 categorical exclusions are a **documented design decision** — the loader
refuses an arbitrary integer encoding that would imply an ordering that does not
exist. But the consequence is worth stating: any information carried by **event
type or venue has been invisible to every model** in Phase 25.9, F.6B and F.6C,
and remains invisible here.

The 2 null features have never contributed a value to anything.

### And two of the 26 "numeric" features are constants

`event.days_since_last` and `event.independent_source_count` are **0.0 in every
one of the 1,056 observations**. Identical vectors, identical fingerprint
`05c27dc362dd0214`, zero variance.

They cannot carry information — not to a linear model, not to a forest, not to a
network. They have been columns in every design matrix since Phase 25.9.
`event.independent_source_count = 0` is the I-F/F.7 corroboration gap surfacing
as a dead column; `event.days_since_last = 0` everywhere looks like an upstream
computation defect and is reported as one.

**So the usable feature count is 24, and the effective unique count is 22.**

---

## C. Feature information is not model performance

| | Asks | Method |
|---|---|---|
| F.6B / F.6C | can *this model* trade? | fit, gate, qualify |
| **F.6D** | does *feature X* carry information? | measure the relationship directly |

Both are measured here, because they can disagree: a feature can carry
information a particular model failed to exploit, and a model can look useful
through interactions its individual features do not show.

- **Univariate**: 26 features × 3 horizons = 78 cells.
- **Group and interaction probes**: fixed low-capacity ridge probes, explicitly
  exploratory, which justify a *future* hypothesis and never a finding of their
  own.

---

## D. Why rank correlation, and why the null is block-aware

**Spearman, not Pearson.** The outcome is an abnormal return and this corpus
contains returns large enough that one observation can dominate a Pearson
coefficient. Spearman is monotone-invariant, which is also why no outlier
deletion step is needed — the robustness lives in the statistic rather than in a
cleaning step that could be tuned. A 5%-trimmed value is reported alongside every
candidate, never instead of it.

**The null is a block permutation.** A parametric p-value for a correlation
assumes independent rows. These cluster by instrument and by session, so that
assumption is false and the p-value would be far too small. Outcomes are
permuted **within each evaluation window**, preserving window membership, window
size and the window's outcome distribution; only the pairing with the feature is
broken. Row-wise shuffling across the whole sample is **not offered by the
module at all**.

Measured: **1.34 rows per cluster per window**. With clusters near-unique inside
a window, within-window permutation is effectively cluster-level — which is
exactly what the design needs and is stated as a measurement rather than assumed.

500 permutations, seed **20261004**, and the p-value uses the `(k+1)/(B+1)`
convention so it is strictly positive — claiming `p = 0` from a finite sample
would overstate the evidence.

---

## E. The thresholds are the metrics' own zero points

The decision rule contains **no magnitude threshold on ρ**, deliberately. A
threshold would be a number I chose. What decides instead:

| Gate | Basis |
|---|---|
| significance | block-permutation p, corrected by **Benjamini-Hochberg at q = 0.05** over a family of **78 declared in advance** |
| stability | the sign must agree in **every** window |
| concentration | the effect must survive removing its largest-contributing instrument |
| redundancy | a feature cannot be independent evidence of its > 0.90 partner |
| timing | `as_of ≤ information_cutoff`, or the feature is disqualified outright |

Effect size is always reported beside the label, so a statistically
distinguishable but tiny effect stays visibly tiny.

q = 0.05 is the conventional level and the more conservative of the two common
choices. A looser exploratory rate was available and not used, because the point
of the correction is to stop p-value mining across 78 tests.

---

## F. Horizon eligibility

| Horizon | Windows | Holdout rows | Verdict |
|---|---|---|---|
| **d1** | 5 | 154 | **ELIGIBLE** |
| **d3** | 5 | 143 | **ELIGIBLE** |
| **d5** | 4 | 70 | **ELIGIBLE** |
| d10 | 3 | **0** | **DEFERRED_INSUFFICIENT_HOLDOUT** |
| intraday ×4 | — | — | DEFERRED_BY_F7_POLICY |
| **d20** | — | — | **PROTECTED / SEALED** |

d10 is deferred by **the same rule F.6C applied**, not a rule relaxed for this
phase. d5 *is* eligible here where it was not in F.6C, for a structural reason:
F.6C needed a third calibration window, which cost roughly two extra weeks of
research region. F.6D needs only train and test.

---

## G. The sharpest D20 boundary in the whole sequence

`market.return_60d` is one of the 26 legitimate features. **What is protected is
not the feature — it is the frozen hypothesis relating it to the d20 abnormal
return.**

So:

- **d20 is not an eligible horizon**, and `guard_no_protected_horizon` raises if
  anything tries to pair a feature with it;
- `market.return_60d` **is** audited against d1, d3 and d5, exactly as F.6B and
  F.6C already used it;
- the feature is **not** excluded, because excluding it would distort the
  inventory and would protect nothing the seal covers.

A test asserts no result cell — univariate, group, ablation or interaction —
carries horizon `d20`, while also asserting `market.return_60d` **is** present in
the audit. Both halves matter.

**This phase draws no inference about d20 from its d1/d3/d5 results, and none
should be read into them.**

---

## H. What the audit measures, cell by cell

Per feature × horizon: per-window Spearman, mean ρ as the effect size, sign
consistency, dispersion, a 5%-trimmed ρ, the block-permutation p, the
BH-adjusted p, the concentration check (for cells that would otherwise be
candidates), the redundancy class, the timing status, availability, distinct
value count, pooled pairs and pooled clusters.

Classification, applied mechanically from the frozen rules:
`STABLE_INFORMATION_CANDIDATE`, `WEAK_INFORMATION`, `UNSTABLE`, `REDUNDANT`,
`NO_INFORMATION`, `NO_USABLE_VALUE`, `TEMPORAL_INTEGRITY_FAILED`,
`INSUFFICIENT_SAMPLE`.

**No feature is labelled a "trading feature" in this phase.**

### One rule needed sharpening during the run, and it was a gap not a change

A constant feature produced `scored windows = 0`, which my implementation routed
to `INSUFFICIENT_SAMPLE`. But the frozen rule defines that label as *"fewer than
3 valid windows, or fewer than 30 clusters"* — and a constant feature has 5
windows and 237 clusters. Its design is perfectly adequate; there is simply
nothing to measure.

So zero variance is now classified `NO_INFORMATION` with the reason recorded:
*"constant across every observation; zero variance, so no relationship is
measurable by any model."* That uses only pre-registered labels and matches the
written rule rather than my first implementation of it.

---

## I. Probes are probes

The probe is ridge α = 1 via the canonical `algorithms.fit`: simple, fixed,
deterministic, low-capacity, configured blind to any outcome, scored by
Spearman(prediction, outcome) on the test window against its own block null.

| Budget | Cells |
|---|---|
| univariate | 78 |
| group probes (7 namespaces × 3 horizons) | 21 |
| ablation (FULL + FULL−group) × 3 | 24 |
| interactions (3 × 3) | 9 |
| **total** | **132** |

The three interactions were justified structurally and named in advance —
momentum × volatility, event materiality × liquidity, return × trend position.
**26 features offer 325 pairs; testing them would be exactly the mining the plan
exists to prevent.**

Normalization is fitted inside the canonical ridge fitter on **training rows
only**. **No imputation is performed** — incomplete rows are dropped, so a
missingness pattern cannot manufacture a relationship.

**Probe p-values are uncorrected and labelled exploratory**, per the frozen plan.
That exposure is reported arithmetically rather than hidden: 21 probes at an
uncorrected 0.05 have a chance expectation of ≈1.05 below threshold.

---

## J. Holdout discipline

| | |
|---|---|
| boundary | `2026-08-18T00:00:00Z`, **inherited** from the F.7 handoff |
| used for feature selection | **never** |
| opened only if | ≥ 1 feature **or group** is a candidate, and the survivor list has been frozen and hashed first |
| if zero candidates | the holdout stays unopened and the record says so |

### A defect here was found by the run and fixed, not patched over

The first complete build opened the holdout on the strength of two promising
groups and then iterated only the (empty) feature survivor list. It recorded
`holdout opened = True` with **zero results** — it claimed to have tested unseen
time and tested nothing, and the two groups never received the confirmation the
plan requires.

Fixed by evaluating promising groups on the holdout, and by making an
opened-for/evaluated mismatch a **recorded error** rather than a silent gap. The
defective execution was marked `invalidated` with its reason and kept on the
register.

### And a second, worse defect: the run-once guard was dead

`preflight()` computed `existing_execution` and **dropped it from its return**,
so `run()`'s run-once check read `None` every time. The protection against
re-executing a frozen experiment did not exist.

It was caught by the test written to assert the protection — which, because the
guard was dead, **actually re-executed the experiment**. That accidental run is
on the register as `invalidated`, with the cause named.

A silver lining worth recording: all **78 cells were byte-identical** across the
two executions, which establishes determinism (§61 of the brief) rather than
independent evidence.

---

## K. Production boundary

| | |
|---|---|
| models qualified / promoted | **0 / 0** |
| broker writes / paper / live orders | **0 / 0 / 0** |
| billable market-data requests | **0** |
| Portfolio / Risk / OrderIntent / Execution | unchanged |
| capture / IBKR / subscriptions | unchanged |
| F.5 semantics | unchanged |
| expected return / target | **UNAVAILABLE** |

A stable directional feature provides no magnitude, no sizing and no target.
Finding one authorises a **new pre-registered modelling experiment** and nothing
else.

The snapshot is read `mode=ro`; the only writes are the execution marker and the
candidate rows. The production database is never opened for writing, and a test
asserts it holds no `research_executions` table. The statistics module imports
only `hashlib`, `json`, `math`, `random`, `dataclasses`, `enum` and `typing` — a
test enforces it.

---

## L. Negative knowledge

Persisted in `research_candidate_results`, queryable by horizon and verdict, with
per-window coefficients, the null, the adjusted p and the concentration result
for every one of the 78 cells. A future autonomous phase can see what has already
been measured and found wanting on this snapshot.

---

## M. What this phase can and cannot settle

It can settle whether **these** features, under **this** test, show stable
out-of-sample rank information at **these** horizons.

It cannot settle whether a different feature representation would, whether
direction is regime-conditional (no canonical regime representation exists in
this snapshot — reported `UNKNOWN`, not invented), or whether the binary-at-zero
target is the right question. Each needs its own experiment.

> **Do not ask a more powerful model to find signal until you know whether the
> inputs contain signal.** If the answer is none, that is valuable negative
> knowledge. If the answer is some, the next modelling hypothesis finally has a
> scientific foundation.
