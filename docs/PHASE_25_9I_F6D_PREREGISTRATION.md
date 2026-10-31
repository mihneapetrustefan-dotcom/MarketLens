# Phase 25.9I-F.6D — Pre-registration

**Feature Information & Predictive Content Audit**

**Written 2026-10-04, and committed before any feature-outcome statistic was
computed.**

> ## A MORE COMPLEX MODEL CANNOT CREATE INFORMATION THAT IS NOT IN THE FEATURES.
>
> ## FEATURE DISCOVERY IS NOT MODEL QUALIFICATION.

Everything in §3 through §8 below was measured from **feature values, row counts,
timestamps and calendar geometry**. No label value enters any of it. That is what
makes it legitimate to measure before freezing — §4 of the brief requires the
real inventory to be derived, and §8 requires horizon eligibility proven in
advance.

**Nothing in this document was computed from a feature-outcome relationship.**
The first such statistic is computed by the harness, which does not exist at this
commit.

---

## 1. The question

F.6C established, verified from its own artifacts rather than from a summary:

| | |
|---|---|
| implementation | complete, 12/12 cells |
| qualification | **NONE QUALIFIED** |
| calibration | **NONE PASSED** |
| pre-holdout survivors | **0** — holdout never opened |
| ROC-AUC over 36 fold-evaluations | **0.3901 – 0.6163**, mean **0.5136** |
| above the 0.5 no-skill point | **18 of 36** — exactly half |
| mean balanced accuracy | **0.5112** |

F.6C's conclusion was that the problem lies **upstream of calibration**: there
was no discrimination to calibrate. That leaves one question before any further
modelling is defensible:

> **Do the current features contain stable, out-of-sample predictive information
> at all?**

Valid answers, all acceptable:

- **YES** — some features or groups show stable information
- **NO** — no defensible feature information found
- **MIXED** — information exists only in specific horizons, regimes or subgroups

**This phase launches no model search.** It fits no new family, tunes nothing,
and qualifies nothing. Its probes are information probes, not candidates.

---

## 2. Feature information is not model performance

The two questions are different and are kept apart:

| | Asks | Method |
|---|---|---|
| **F.6B / F.6C** | can *this model* trade? | fit, gate, qualify |
| **F.6D** | does *feature X* carry information? | measure the relationship directly |

A feature can carry information a particular model failed to exploit, and a model
can look useful through interactions its individual features do not show. So both
are measured: **univariate** information per feature, and **small
pre-registered group and interaction probes**. Neither is a model search.

---

## 3. The dataset — inherited, not rebuilt

| | |
|---|---|
| research snapshot | **`research_snapshot_v2`**, `ELIGIBLE_FOR_RESEARCH` |
| snapshot hash | `4ccab384d37a9eb249ce2bd61b44692c77a0df3dc7121dc79c3ed6940e66b0bc` |
| corpus version | `canonical_corpus_v2`, hash `4247f543904204c9…` |
| feature / label / universe / event version | `v1` each |
| as-of cutoff | `2026-08-30T14:35:00Z` |
| information coverage | 2026-07-02T07:00:00Z → 2026-08-30T14:35:00Z |

**Not rebuilt.** F.6C failing is not an integrity defect, and F.6D must not mutate
this dataset. The snapshot hash is verified before the run and again after.

---

## 4. Feature inventory — the real count is 33, not 26

Derived from `research_features`, not assumed.

| | Count |
|---|---|
| **features in the registry** | **33** |
| numeric and usable | **26** |
| categorical, excluded by the canonical loader | **5** |
| **registered but `null` in all 1,056 observations** | **2** |

| Inventory | SHA-256 |
|---|---|
| the 26 numeric features (sorted) | **`45dddece5017387d5c6590ad14da701dfcca372e2548dcb8576d4460af671147`** |
| the full 33-feature registry (sorted) | `898694fa2e6de2ea6010be5b32bd0b34fae43f792b537f68e2dcfa4705c111f9` |

### The 5 categorical exclusions

`entity.asset_class`, `entity.exchange_id`, `entity.ticker`, `event.category`,
`event.event_type`. Excluded by `train_models.load_dataset` because an arbitrary
integer encoding would imply an ordering that does not exist. **This is a
documented design decision, not a defect** — but it does mean any information
carried by event type or venue has been invisible to every model in Phase 25.9,
F.6B and F.6C, and remains invisible here.

### The 2 features that carry nothing

`peer.dispersion_5d` and `peer.relative_return_5d` have **1,056 rows each and
`null` in every one**. They are registered, they occupy storage, and they have
never contributed a single value to any model. They are reported as
`NO_USABLE_VALUE` and cannot be audited for information, because there is nothing
to audit.

### Availability of the 26 numeric features

| Availability | Features |
|---|---|
| **100%** of all 1,056 observations | the 7 `event.*` features, `news.article_count_7d`, `news.source_diversity_7d`, `sentiment.mean_7d` |
| **98.9%** | `sentiment.dispersion_7d` |
| **85.3 – 86.7%** | the 6 `market.*`, 4 `liquidity.*`, 4 `volatility.*`, `technical.rsi_14` |

That 86% figure is misleading on its own and is reported with its correction:
missingness is **concentrated in the 141 observations marked
`insufficient_price_data`**, which the canonical loader already excludes as
`invalid`. Among the observations that actually enter research, complete-row
coverage is **98.5% (d1), 98.6% (d3), 98.8% (d5)**.

**No imputation is performed.** The canonical fitter drops rows with any missing
feature, so the probes' effective sample is the complete-row count. Missingness is
never silently replaced by zero.

---

## 5. Feature groups — the repository's own taxonomy

`FeatureNamespace` already exists. No new taxonomy is invented.

| Group | n | Features |
|---|---|---|
| **event** | 7 | confidence, count_30d, count_7d, days_since_last, independent_source_count, novelty_365d, quality_confidence |
| **market** | 6 | distance_from_ma20, drawdown_60d, return_1d, return_5d, return_20d, return_60d |
| **liquidity** | 4 | baseline_mean_volume, baseline_std_volume, relative_volume_20d, volume_zscore_20d |
| **volatility** | 4 | percentile_20d, pre_volatility, realized_20d, realized_5d |
| **news** | 2 | article_count_7d, source_diversity_7d |
| **sentiment** | 2 | dispersion_7d, mean_7d |
| **technical** | 1 | rsi_14 |

7 groups, 26 features. Namespaces present in the enum but absent from this
snapshot (`sector`, `macro`, `regime`, `relationship`, `fundamental`,
`cross_sectional`, `historical`, `peer` after the null exclusion) are reported as
absent, not as empty groups with results.

---

## 6. Horizon eligibility — measured before freezing

A horizon is eligible only with **≥ 3 valid evaluation windows AND a non-empty
final holdout**. Window existence alone is not adequacy — that rule was
established in F.6C and is applied unchanged rather than re-derived to suit this
phase.

| Horizon | Rows | Research region | Windows built | **Valid** | Test rows per window | **Holdout rows** | **Verdict** |
|---|---|---|---|---|---|---|---|
| **d1** | 891 | 737 | 5 | **5** | 94 / 143 / 166 / 133 / 52 | **154** | **ELIGIBLE** |
| **d3** | 878 | 735 | 5 | **5** | 100 / 154 / 163 / 138 / 28 | **143** | **ELIGIBLE** |
| **d5** | 805 | 735 | 4 | **4** | 126 / 159 / 143 / 122 | **70** | **ELIGIBLE** |
| d10 | 633 | 633 | 3 | 3 | 158 / 143 / 21 | **0** | **DEFERRED_INSUFFICIENT_HOLDOUT** |
| intraday ×4 | 901 | — | — | — | — | — | **DEFERRED_BY_F7_POLICY** |
| **d20** | — | — | — | — | — | — | **PROTECTED / SEALED** |

**d10 is deferred for the same reason F.6C deferred it** — its latest matured
label precedes the boundary, so it has no untouched confirmation region. It is
not rescued by relaxing the rule, and the boundary is not moved.

d5 **is** eligible here, where it was not in F.6C, and the reason is structural
rather than convenient: F.6C needed a third (calibration) window, which cost
roughly two extra weeks of region. F.6D needs only train and test, so d5 forms 4
valid windows.

---

## 7. Evaluation design

**Holdout boundary: `2026-08-18T00:00:00Z`** — inherited unchanged from the F.7
handoff, never opened by F.6B or F.6C.

### Windows

Expanding, day-granular, strictly inside the research region, built by
`research_multi_horizon.build_folds` — the same function F.6B and F.6C used, with
the same parameters:

| Parameter | Value | Source |
|---|---|---|
| purge | label's calendar span: d1→3, d3→5, d5→7 days | F.7 handoff |
| embargo | **6 days** | Phase 25.9 |
| test window / step | **7 / 7 days** | Phase 25.9 |
| min train / test rows | 30 / 20 | Phase 25.9 |
| min valid windows | **3** | `MIN_VALID_FOLDS` |

### Univariate measurement needs no training window

For a univariate audit the question is whether the feature-outcome relationship
holds **within each disjoint evaluation window** and whether it is **stable across
them**. So the statistic is computed inside each window's test period; no model is
fitted and nothing is learned from the training region.

### Probes do use train → test

Group and interaction probes fit on the training region and are scored on the
test window, with purge and embargo as above.

---

## 8. Redundancy — measured from feature values only

| | Count |
|---|---|
| exact / near-exact duplicates (\|ρ\| > 0.999) | **0** |
| high redundancy (0.90 < \|ρ\| ≤ 0.999) | **3 pairs** |

| Pair | ρ |
|---|---|
| `liquidity.relative_volume_20d` ↔ `liquidity.volume_zscore_20d` | **0.9627** |
| `liquidity.baseline_mean_volume` ↔ `liquidity.baseline_std_volume` | **0.9420** |
| `event.confidence` ↔ `event.quality_confidence` | **0.9364** |

**Effective unique feature count: 23** of 26, counting each high-redundancy pair
once.

No feature is deleted — this is diagnostic. But a member of a redundant pair
cannot be reported as independent evidence of the other, and two features whose
fingerprints coincide are one source, not two.

---

## 9. Metrics — fixed now

**Univariate, continuous target (`{horizon}.abnormal_return`):**

- **Spearman rank correlation** ρ between feature and outcome, per window. Rank
  correlation rather than Pearson, because it is monotone-invariant and far less
  sensitive to the return outliers this corpus contains.
- **mean ρ across windows** — the primary effect size.
- **sign consistency** — the fraction of windows sharing the mean's sign.
- **dispersion** — min, max, spread of ρ across windows.

**Probes (group, ablation, interaction):** ridge regression (α = 1) via the
canonical `algorithms.fit`, scored by **Spearman(prediction, outcome)** on the
test window. A rank score, because a probe exists to detect *information*, not to
produce a calibrated magnitude.

**Not computed:** Pearson correlation (outlier-fragile on these returns), mutual
information (no estimator exists in the project and adding one for 78 thin cells
would produce numbers whose assumptions could not be defended), PR-AUC, and any
classification metric — the target here is continuous.

Mutual information is named in the brief as optional. It is **declined**, with
the reason recorded, rather than added because it was mentioned.

---

## 10. The null test — block-aware, never row-wise

A parametric p-value for ρ assumes independent observations. These are not
independent: rows cluster by instrument and by session.

So significance comes from a **block permutation null**:

- the outcome vector is permuted **within each evaluation window**, across
  clusters, preserving window membership and window size;
- ρ is recomputed on each permutation;
- the empirical two-sided p-value is the fraction of permutations whose
  \|mean ρ\| is at least the observed \|mean ρ\|.

**Row-wise IID shuffling is not used**, because it would destroy the temporal and
cluster structure the null is supposed to preserve.

| | |
|---|---|
| permutations per hypothesis | **500** |
| seed | **20261004**, fixed and recorded |
| null statistic | \|mean ρ across windows\| |

---

## 11. Multiple testing — family declared before testing

| | |
|---|---|
| **hypothesis family** | **78** univariate tests = 26 numeric features × 3 eligible horizons |
| family hash | `293060c829933584b9983855df5f31d1aa49a099805e9278cd798314e72d414c` |
| correction | **Benjamini-Hochberg FDR** |
| level | **q = 0.05** |

The family is the **whole** 78, declared now, not a subset chosen after seeing
which look interesting. q = 0.05 is the conventional level and the more
conservative of the two common choices; a looser exploratory rate is not used,
because the point of the correction here is to stop p-value mining across 78
tests.

Probe cells (group, ablation, interaction) are **not** in the univariate family.
They are reported against their own block-permutation nulls and are explicitly
labelled exploratory — they justify a future hypothesis, never a finding of their
own.

---

## 12. Probe and interaction budget

```
UNIVARIATE      26 features x 3 horizons                      =  78 cells
GROUP PROBES     7 groups   x 3 horizons                       =  21 cells
ABLATION         (FULL + FULL-minus-each-of-7) x 3 horizons    =  24 cells
INTERACTIONS     3 pairs    x 3 horizons                       =   9 cells
                                                      TOTAL    = 132 cells
```

**The budget is 132 and will not grow.** No hyperparameter search, no additional
probe family, no post-result cell.

### The three interactions, justified structurally and fixed now

| Interaction | Why it is conceptually motivated |
|---|---|
| `market.return_5d` × `volatility.realized_20d` | momentum conditioned on volatility regime — the standard reason a trend signal behaves differently in calm and turbulent periods |
| `event.confidence` × `liquidity.relative_volume_20d` | event materiality conditioned on whether the market was actually transacting on it |
| `market.return_20d` × `market.distance_from_ma20` | medium-term return conditioned on position relative to trend — extension versus reversion |

No other interaction is tested, and **no combinatorial search over pairs is
performed.** 26 features would offer 325 pairs; testing them would be exactly the
mining this plan exists to prevent.

### Probe model rule

The probe is ridge α = 1: simple, fixed, deterministic, low-capacity, configured
blind to any outcome. It is **not** a model candidate, is never promoted, and a
good probe score authorises only a **future** modelling hypothesis.

---

## 13. Classification rules — fixed now

Each feature × horizon receives exactly one label.

| Label | Condition |
|---|---|
| `TEMPORAL_INTEGRITY_FAILED` | any feature value with `as_of` > its observation's `information_cutoff` |
| `INSUFFICIENT_SAMPLE` | fewer than 3 valid windows, or fewer than `MIN_EFFECTIVE_SAMPLE` (30) distinct clusters pooled across windows |
| `NO_USABLE_VALUE` | the feature is `null` in every observation |
| **`STABLE_INFORMATION_CANDIDATE`** | time-safe **AND** adequate sample **AND** BH-FDR significant at q=0.05 **AND** sign consistent in **every** window **AND** survives the concentration check **AND** not the redundant partner of an already-labelled candidate |
| `WEAK_INFORMATION` | BH-FDR significant but sign-inconsistent or failing concentration; **or** sign-consistent but not distinguishable from the null |
| `UNSTABLE` | BH-FDR significant and the sign flips across windows |
| `NO_INFORMATION` | not FDR-significant and sign-inconsistent |
| `REDUNDANT` | would be a candidate, but its \|ρ\| > 0.90 partner is already one |

**There is no magnitude threshold on ρ**, deliberately. A threshold would be a
number chosen by me; the null test and the sign-consistency requirement are
properties of the data and the design. Effect size is always reported alongside
the label so a statistically distinguishable but tiny effect stays visibly tiny.

Group labels: `PROMISING_INFORMATION_GROUP`, `WEAK`, `UNSTABLE`, `REDUNDANT`,
`NO_EVIDENCE`, `UNKNOWN`, by the same logic applied to the probe score.

**No feature is labelled a "trading feature" in this phase.**

### Concentration check

For any feature that would otherwise be a candidate, the statistic is recomputed
with the single largest-contributing instrument removed. If the FDR-significance
or the sign consistency does not survive, the feature carries
`CONCENTRATION_WARNING` and is demoted to `WEAK_INFORMATION`. One ticker must not
be allowed to produce a corpus-wide finding.

### Outlier sensitivity

Spearman is already rank-based and therefore robust to extreme returns by
construction; that is the reason it is the primary metric rather than Pearson.
Both the full-sample ρ and a 5%-trimmed ρ are reported for every candidate, and
**no observation is deleted** — the comparison is reported, not substituted.

---

## 14. Holdout discipline

| | |
|---|---|
| boundary | `2026-08-18T00:00:00Z`, inherited |
| used for feature selection? | **NO** — never |
| when opened | only if ≥ 1 feature or group is a candidate after §13, and the survivor list has been frozen and hashed first |
| if zero candidates | **holdout remains unopened**, and the record says so |
| evaluation | once, on the frozen survivor list, with no replacement and no added interaction afterwards |

What the holdout tests is narrow: **does the measured relationship survive unseen
time?** It trains no production model, and surviving it confirms *feature
information*, not a model.

---

## 15. Temporal integrity

Every feature value must satisfy `as_of ≤ information_cutoff`. Any feature with an
ambiguous or violating timestamp is marked `TEMPORAL_INTEGRITY_UNCERTAIN` or
`TEMPORAL_INTEGRITY_FAILED` and is **excluded from qualification evidence**.

Specifically audited: rolling-window endpoints, the `is_contemporaneous` flag,
event-derived fields, and the physical-truncation property — a feature's value
computed as-of T must equal its value in a dataset physically truncated at T.

Normalization is fitted inside the canonical ridge fitter on **training rows
only**, so no full-dataset scaling precedes a temporal split. No imputation is
performed at all.

---

## 16. D20 — and the sharpest boundary in this phase

D20 is sealed: not executed, inspected, inferred or consumed, and not used for
feature ranking, selection, grouping, interaction discovery, dimensionality
reduction or horizon selection.

**One point needs stating explicitly, because this is the phase where it could go
wrong by accident.** `market.return_60d` is a legitimate feature in this corpus
and is one of the 26. The protected artifact is **not the feature** — it is the
frozen *hypothesis* relating that feature to the **d20** abnormal return.

Therefore:

- **d20 is not an eligible horizon**, so the pair
  (`market.return_60d`, d20) is never computed;
- `market.return_60d` **is** audited against d1, d3 and d5, exactly as F.6B and
  F.6C already used it;
- the feature is **not** excluded from the audit, because excluding it would
  distort the inventory and would not protect anything the seal covers.

A test asserts that no F.6D output pairs any feature with d20.

---

## 17. What this phase will not produce

| | |
|---|---|
| models qualified | **0** — this phase cannot qualify one |
| models promoted | **0** |
| expected return | **UNAVAILABLE** — a feature relationship is not a magnitude |
| target | **UNAVAILABLE** |
| production signal capability | **unchanged** |
| F.5 semantics | **unchanged** |

A stable directional feature does not provide expected return, sizing or a
target. Finding one authorises a **new pre-registered modelling experiment** and
nothing else.

---

## 18. Stopping rules

- all 132 cells evaluated;
- fewer than 3 valid windows for a horizon ⇒ that horizon is `INSUFFICIENT_SAMPLE`;
- fewer than 30 pooled clusters ⇒ same;
- zero candidates after §13 ⇒ the holdout is not opened and the phase ends.

**There is no second round.** If no feature survives, the conclusion is recorded
and the next question needs a new pre-registration. **No more complex model is
tried inside F.6D** — not XGBoost, not a forest, not a network, not another
logistic or ridge search.

---

## 19. Valid outcomes

All acceptable, and the phase is COMPLETE in every case:

- **N STABLE FEATURE/HORIZON CANDIDATES**
- **NO DEFENSIBLE INFORMATION FOUND**
- **MIXED / CONDITIONAL INFORMATION**

> **Do not ask a more powerful model to find signal until you know whether the
> inputs contain signal.** If the answer is none, that is valuable negative
> knowledge. If the answer is some, the next modelling hypothesis finally has a
> scientific foundation.

---

## 20. Governance that does not move

`MIN_EFFECTIVE_SAMPLE` (30), `MIN_VALID_FOLDS` (3), `min_signal_confidence`
(0.40), `ModelEvaluation.is_deployable`, the mandatory baselines, the confidence
formula, the promotion gate, the paper/live boundary and the D20 seal. **None is
changed by this phase**, and this phase defines no gate of its own that could
substitute for one.

---

## 21. F.6B and F.6C are immutable

Both remain exactly as recorded — F.6B **COMPLETE / NONE QUALIFIED**, F.6C
**COMPLETE / NONE QUALIFIED / NONE PASSED**. Their pre-registrations, handoffs,
result artifacts, execution markers, candidate rows and holdout boundaries are not
edited, regenerated, recomputed under new rules or reinterpreted. F.6D creates a
new experiment id, a new pre-registration, a new execution marker and new result
artifacts, and its pre-flight **verifies** both predecessors before running.
