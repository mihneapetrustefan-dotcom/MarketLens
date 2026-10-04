# Phase 25.9I-F.6D — Completion Report

> **A MORE COMPLEX MODEL CANNOT CREATE INFORMATION THAT IS NOT IN THE FEATURES.**
>
> **FEATURE DISCOVERY IS NOT MODEL QUALIFICATION.**

Written 2026-10-04. Architecture: `docs/PHASE_25_9I_F6D_ARCHITECTURE.md`.
Frozen plan: `docs/PHASE_25_9I_F6D_PREREGISTRATION.md`, sha256
`4563b3ef1087f5e2a79597e6438b4ec525759d2d235242fdd2e84b11c0cce137`, committed at
**`2e3cc2d`**. Raw results: `docs/PHASE_25_9I_F6D_RESULTS.json`.

---

## 1. Phase implementation status

**COMPLETE.** All 132 pre-registered cells evaluated, 0 errors.

---

## 2. Feature information outcome

**MIXED / CONDITIONAL INFORMATION** — and the qualifier is doing almost all the
work.

| | |
|---|---|
| **stable univariate feature candidates** | **0 of 78** |
| nominally significant univariate cells (p ≤ 0.05, uncorrected) | **13 of 78** |
| **surviving Benjamini-Hochberg at q = 0.05** | **0** |
| group probes flagged promising (uncorrected) | 2 of 21 |
| **surviving the holdout** | **1** — `volatility` at d3, by **direction only** |

The single survivor is reported as the frozen rule assigns it, and its weakness
is reported with equal prominence: its holdout ρ is **−0.0152** against a
research-region mean of **−0.1613**, i.e. **9.4% of the effect retained**, and it
is one of 21 probes whose p-values are uncorrected by design.

**If the honest summary has to be one sentence: this phase found no defensible
univariate feature information, and one group-level signal too weak to build on.**

---

## 3. Model qualification

```
models qualified = 0
models promoted  = 0
```

As expected. This phase cannot qualify a trading model and contains no code path
that could.

---

## 4. F.6B / F.6C preservation

Verified by `preflight()` **before** the run, not assumed, and asserted by test:

| | F.6B | F.6C |
|---|---|---|
| execution | `f6b-647dd37b65e14046` | `f6c-3a9e75bdcca141a5` |
| status | **complete** | **complete** |
| qualified count | **0** | **0** |
| candidate rows / qualified | **16 / 0** | **12 / 0** |
| pre-registration | unchanged | unchanged |
| holdout boundary | `2026-08-18T00:00:00Z` | same, unchanged |
| results artifact | not regenerated | not regenerated |
| negative knowledge | retained (§30) | retained (§30) |

---

## 5. Git state

| | |
|---|---|
| Branch | `ibkr-paper-validation-fixes` |
| Baseline HEAD | **`7914aee`** (F.6C completion report) |
| Pre-registration commit | **`2e3cc2d`** — plan only, no harness/results/marker |
| Final HEAD | **`a8a8747`** |
| Working tree at start | clean |
| Commits | 2 |
| **Files modified** | **0** |

Purely additive: `docs/PHASE_25_9I_F6D_PREREGISTRATION.md`,
`src/modeling/feature_information.py`, `scripts/research_f6d.py`,
`tests/adaptive/test_f6d_feature_information.py`,
`docs/PHASE_25_9I_F6D_ARCHITECTURE.md`, `docs/PHASE_25_9I_F6D_RESULTS.json`,
this report.

---

## 6. Baseline tests

Run at `7914aee` before any F.6D file was added:

```
Ran 5405 tests in 566.444s
OK (skipped=1)
```

| Tests | Pass | Fail | Errors | Skipped | Runtime |
|---|---|---|---|---|---|
| **5,405** | 5,404 | **0** | **0** | 1 | 566.444 s |

Uncontaminated: only new files were added while it ran, and `unittest discover`
enumerates at start.

---

## 7. F.6D pre-registration

| | |
|---|---|
| experiment ID | **`f6d-a24902c869064bbb`** |
| pre-registration sha256 | **`4563b3ef1087f5e2a79597e6438b4ec525759d2d235242fdd2e84b11c0cce137`** |
| snapshot | `research_snapshot_v2`, hash `4ccab384d37a9eb2…`, **recomputes** |
| corpus | `canonical_corpus_v2`, hash `4247f543904204c9…` |
| **feature inventory hash** | `45dddece5017387d5c6590ad14da701dfcca372e2548dcb8576d4460af671147` |
| **hypothesis family hash** | `293060c829933584b9983855df5f31d1aa49a099805e9278cd798314e72d414c` |
| horizons | **d1, d3, d5** |
| metrics | Spearman ρ per window, mean ρ, sign consistency, dispersion, 5%-trimmed ρ; ridge probe scored by rank |
| testing family | **78** = 26 numeric features × 3 horizons, declared in advance |
| multiple-testing rule | **Benjamini-Hochberg, q = 0.05** |
| null | block permutation within windows, **500** draws, seed **20261004** |
| probe budget | 21 group + 24 ablation + 9 interaction = 54 |
| interaction budget | **3**, named and structurally justified in advance |
| holdout policy | inherited `2026-08-18T00:00:00Z`; never used for selection; opened only for a frozen, hashed survivor list |

The hash is **pinned inside the harness**, which refuses to run against an edited
plan. Commit `2e3cc2d`'s tree contains the plan and nothing else — ordering
provable from git.

---

## 8. Plan defects corrected from F.6C

F.6C's own limitations carried into this phase's design:

| F.6C limitation | What F.6D did |
|---|---|
| only 2 horizons (the calibration window cost d5) | F.6D needs no calibration window, so **d5 is eligible**: 3 horizons |
| probe p-values uncorrected | the **univariate family is corrected** (BH over 78); probe p-values remain uncorrected **and their chance exposure is reported arithmetically** |
| 26 features assumed | the **real inventory was derived**: 33 registered, and two of the 26 "numeric" are constants |
| d10 excluded on empty holdout | **the same rule applied unchanged**, not relaxed |

---

## 9. Horizon eligibility

| Horizon | Rows | Complete rows | Clusters | Windows | Test rows | Holdout rows | Rows/cluster/window | Verdict |
|---|---|---|---|---|---|---|---|---|
| **d1** | 891 | 878 (98.5%) | 237 | **5** | 94/143/166/133/52 | **154** | 1.34 | **ELIGIBLE** |
| **d3** | 878 | 866 (98.6%) | 237 | **5** | 100/154/163/138/28 | **143** | 1.34 | **ELIGIBLE** |
| **d5** | 805 | 795 (98.8%) | 234 | **4** | 126/159/143/122 | **70** | 1.38 | **ELIGIBLE** |
| d10 | 633 | — | 211 | 3 | 158/143/21 | **0** | — | **DEFERRED_INSUFFICIENT_HOLDOUT** |
| intraday ×4 | 901 | — | 237 | — | — | — | — | **DEFERRED_BY_F7_POLICY** |
| **d20** | — | — | — | — | — | — | — | **PROTECTED / SEALED** |

No horizon unexplained. **1.34 rows per cluster per window** is measured, not
assumed, and it is what makes within-window permutation effectively
cluster-level.

---

## 10. What was implemented

**Inventory** — `audit_inventory` derives all 33 registered features with
namespace, kind, availability, contemporaneity, and an `as_of > cutoff` count per
feature.

**Fingerprints** — `vector_fingerprint` hashes the effective value vector with
`None` encoded distinctly from any number; `redundancy_map` classifies UNIQUE /
HIGHLY_REDUNDANT / DUPLICATE / ZERO_VARIANCE / NO_USABLE_VALUE and computes an
effective unique count.

**Timing audit** — per-feature count of values dated after their observation's
cutoff; a non-zero count disqualifies outright.

**Univariate analysis** — per-window Spearman, mean ρ, sign consistency,
dispersion, trimmed ρ.

**Null testing** — block permutation within windows, 500 draws, fixed seed,
`(k+1)/(B+1)` p-value.

**Multiple testing** — BH step-up over the 78 declared hypotheses, with
uncomputable hypotheses kept in the denominator.

**Concentration** — recompute with the largest-contributing instrument removed,
chosen by row count (outcome-blind).

**Group probes, ablation, interactions** — fixed ridge α=1, rank-scored, each
against its own block null.

**Holdout logic** — survivor list frozen and hashed before the holdout is
touched; features and groups both evaluated; an opened-for/evaluated mismatch is
a recorded error.

**Execution marker** — reuses F.6B's schema with the inventory hash, family hash,
cell list and survivor hash; an auditable `--invalidate`.

**Tests and docs** — 63 tests, the architecture document and this report.

---

## 11. Feature inventory — the real count is 33

| Kind | Count | Features |
|---|---|---|
| **numeric, in the design matrix** | **26** | the 7 `event.*`, 6 `market.*`, 4 `liquidity.*`, 4 `volatility.*`, 2 `news.*`, 2 `sentiment.*`, 1 `technical.*` |
| **categorical, excluded by the loader** | **5** | `entity.asset_class`, `entity.exchange_id`, `entity.ticker`, `event.category`, `event.event_type` |
| **`null` in all 1,056 observations** | **2** | `peer.dispersion_5d`, `peer.relative_return_5d` |
| **total registered** | **33** | |

### Two of the 26 "numeric" features are constants

| Feature | Distinct values | Value | Fingerprint |
|---|---|---|---|
| `event.days_since_last` | **1** | **0.0** | `05c27dc362dd0214` |
| `event.independent_source_count` | **1** | **0.0** | `05c27dc362dd0214` |

Identical vectors, zero variance. **They cannot carry information to any model**
and have been columns in every design matrix since Phase 25.9.
`event.independent_source_count = 0` is the I-F/F.7 corroboration gap surfacing
as a dead column; `event.days_since_last = 0` for every event looks like an
upstream computation defect.

**Usable feature count: 24. Effective unique count: 22.**

---

## 12. Feature groups

The repository's own `FeatureNamespace`; no taxonomy invented.

| Group | n | Members |
|---|---|---|
| event | 7 | confidence, count_30d, count_7d, days_since_last*, independent_source_count*, novelty_365d, quality_confidence |
| market | 6 | distance_from_ma20, drawdown_60d, return_1d, return_5d, return_20d, return_60d |
| liquidity | 4 | baseline_mean_volume, baseline_std_volume, relative_volume_20d, volume_zscore_20d |
| volatility | 4 | percentile_20d, pre_volatility, realized_20d, realized_5d |
| news | 2 | article_count_7d, source_diversity_7d |
| sentiment | 2 | dispersion_7d, mean_7d |
| technical | 1 | rsi_14 |

\* constant. The `event` group's effective size is **5**, not 7.

Namespaces in the enum but absent from this snapshot: `cross_sectional`,
`entity` (numeric), `fundamental`, `historical`, `macro`, `peer`, `regime`,
`relationship`, `sector`. **Reported absent, not as empty groups with results** —
which is also why §25 below answers UNKNOWN rather than inventing a regime
analysis.

---

## 13. What worked

1. **The real inventory was derived, not assumed** — and it is 33, not 26.
2. **Two dead constant features were found** that have been in every design
   matrix since Phase 25.9.
3. **Two null features** (`peer.*`) were found occupying 2,112 rows and
   contributing nothing.
4. **Timing is clean**: 0 feature values dated after their observation's cutoff,
   across all 33 features.
5. **The correction did its job**: 13 nominally significant cells → **0** after
   BH at q=0.05.
6. **The fingerprint rule caught what correlation could not** — two constants
   correlate to `None`, so a correlation-only rule would have called them
   independent sources.
7. **The concentration check works**, verified on a fixture where one instrument
   carries the effect and the effect correctly fails.
8. **The experiment is deterministic**: 78 of 78 cells byte-identical across two
   executions.
9. **Two real defects were found and fixed** (§14.1, §14.2), both in protections
   rather than in results.
10. **The holdout rejected one of the two group candidates** on a sign flip,
    which is the gate working.
11. **Zero regressions.** 5,405 → 5,468 tests, all passing.

---

## 14. What did not work

**Mandatory, and complete.**

### 14.1 The run-once guard was dead, and my own test re-ran the experiment

- **Symptom:** `test_a_completed_execution_refuses_a_second_run` failed with
  *"FrozenPlanViolation not raised"* — and in failing, it **executed the frozen
  experiment a second time**, writing `f6d-bfc1421240bd4a7c` and overwriting the
  results artifact.
- **Root cause:** `preflight()` computed `existing = find_execution(...)` and
  **dropped it from its return dict**. `run()` then read
  `context.get("existing_execution")` → `None` forever. The protection against
  re-executing a frozen experiment did not exist.
- **Fix:** `existing_execution` is returned, and a test now holds it in place.
  Verified: a second `--run` exits with `STOP: already executed as …`.
- **Status:** resolved. **This is the most serious defect in the phase** — a
  frozen experiment with no run-once protection is one accidental invocation away
  from being re-rolled. It was caught only because a test asserted the protection
  rather than assuming it.
- **Consequence handled:** both prior complete executions were retired with
  reasons (§20), and one clean authoritative run was executed by the corrected
  harness. The 78-cell byte-identity across runs establishes determinism, not
  independent evidence, and is reported as such.

### 14.2 The holdout was opened and evaluated nothing

- **Symptom:** the first complete build recorded `holdout opened = True` with
  **zero results**.
- **Root cause:** the gate opened on `survivors or promising_groups`, but the
  evaluation loop iterated only the feature survivor list. With 0 feature
  survivors and 2 promising groups, it claimed to have tested unseen time and
  tested nothing — and the two groups never received the confirmation the plan
  requires.
- **Fix:** promising groups are now evaluated on the holdout, and an
  opened-for/evaluated mismatch is appended to `errors` rather than passing
  silently.
- **Status:** resolved; execution `f6d-c52aa9f1e5304330` marked `invalidated`
  with the reason and kept on the register.

### 14.3 A classification rule did not match its own written definition

- **Symptom:** the two constant features were labelled `INSUFFICIENT_SAMPLE`.
- **Root cause:** my implementation keyed on *scored windows = 0*, but the frozen
  rule defines `INSUFFICIENT_SAMPLE` as *"fewer than 3 valid windows, or fewer
  than 30 clusters"*. A constant feature has **5 windows and 237 clusters** — an
  adequate design with nothing to measure.
- **Fix:** zero variance is now `NO_INFORMATION` with the reason recorded. Uses
  only pre-registered labels; no label was added after seeing results.
- **Status:** resolved. Worth recording because the wrong label would have blamed
  the dataset for a dead feature.

### 14.4 The funnel lost its order in the artifact

- **Symptom:** `test_the_funnel_reconciles_downwards` failed — the stage counts
  read `[78, 72, 78, 72, 72, 7, 0, 0, 78, 78]`.
- **Root cause:** the result JSON is written with `sort_keys=True`, which
  alphabetised the funnel's keys. The funnel itself was monotone; the artifact
  made it look broken.
- **Fix:** the funnel now carries `stage_order` and an ordered `stages` list, so
  the pipeline order is part of the evidence rather than inferred from key order.
- **Status:** resolved.

### 14.5 One of my tests forbade the phase's own subject

- **Symptom:** `test_no_protected_label_or_validator_is_referenced` failed on
  `value_json` appearing in `research_f6d.py`.
- **Root cause:** the harness reads `research_features.value_json` — which is
  *the feature values*, the entire subject of a feature audit. The real
  prohibition is reaching into `research_labels`.
- **Fix:** the test now asserts the harness never touches `research_labels`
  (outcomes arrive only through the canonical loader) and that the statistics
  module contains no `value_json` at all.
- **Status:** resolved. Third phase running in which a blunt substring seal
  flagged legitimate code; the AST-based approach is now standard here.

### 14.6 No univariate feature carries defensible information

- **Symptom:** 71 of 78 cells `NO_INFORMATION`, 7 `WEAK_INFORMATION`, **0
  candidates**.
- **Root cause:** this is the result, not a defect. 13 cells reach nominal
  p ≤ 0.05 — more than the 3.9 chance expectation — but **none survives BH at
  q=0.05** (smallest adjusted p = **0.1246**).
- **Status:** the phase's primary finding.

### 14.7 The surviving group candidate is extremely weak

- **Symptom:** `volatility|d3` passed every pre-registered step including the
  holdout, with holdout ρ = **−0.0152** against research −0.1613.
- **Root cause:** the pre-registered holdout criterion is **direction only** — I
  did not pre-register a magnitude requirement.
- **Fix:** **none, deliberately.** Tightening the criterion after seeing a result
  I find unconvincing is the same offence as loosening one, in the opposite
  direction. The mechanical verdict stands and the weakness is reported beside
  it.
- **Status:** open as a limitation, and the reason §32 answers PARTIAL rather
  than YES.

### 14.8 Deliberate non-actions

- I did **not** tighten the holdout criterion after seeing the survivor.
- I did **not** apply a correction to the 21 probe p-values as a verdict (the
  plan declared them exploratory); I report the chance arithmetic instead.
- I did **not** remove the constant or null features from the inventory — they
  are reported, because their presence is the finding.
- I did **not** try a more complex model, a new family, or a fourth interaction.
- I did **not** move the holdout to rescue d10.

---

## 15. Feature coverage

| Availability | Features |
|---|---|
| **100%** | 7 `event.*`, `news.article_count_7d`, `news.source_diversity_7d`, `sentiment.mean_7d` |
| 98.9% | `sentiment.dispersion_7d` |
| 85.3–86.7% | 6 `market.*`, 4 `liquidity.*`, 4 `volatility.*`, `technical.rsi_14` |
| **0%** | `peer.dispersion_5d`, `peer.relative_return_5d` |

The 86% figure is reported with its correction: **missingness is concentrated in
the 141 `insufficient_price_data` observations the loader already excludes as
invalid.** Among rows that enter research, complete-row coverage is **98.5% (d1),
98.6% (d3), 98.8% (d5)**.

**No imputation is performed.** Incomplete rows are dropped, so a missingness
pattern cannot manufacture a relationship. Missingness is never silently zero.

---

## 16. Temporal integrity

| | |
|---|---|
| features with any value dated after its observation's cutoff | **0 of 33** |
| features rejected for timing | **none** |
| contemporaneous-by-design | `entity.*` (3), `event.category`, `event.event_type`, `event.confidence`, `event.quality_confidence`, `event.independent_source_count` |

All 1,056 rows of each contemporaneous feature carry `as_of = information_cutoff`
— correct for a fact known exactly at the event, and the documented Phase 7
exception. Every other feature's `as_of` precedes its cutoff.

---

## 17. Univariate results

78 cells. **0 stable candidates, 7 weak, 71 no information.**

| Horizon | NO_INFORMATION | WEAK_INFORMATION | STABLE |
|---|---|---|---|
| d1 | 23 | 3 | **0** |
| d3 | 23 | 3 | **0** |
| d5 | 25 | 1 | **0** |

### The 7 sign-consistent cells — the strongest univariate evidence in the phase

| Feature | Horizon | mean ρ | trimmed ρ | p (block null) | **BH-adjusted p** | windows |
|---|---|---|---|---|---|---|
| `event.novelty_365d` | d5 | **−0.1321** | −0.1481 | 0.0040 | **0.1246** | 4/4 neg |
| `event.count_7d` | d3 | **−0.1379** | −0.1351 | 0.0080 | **0.1246** | 5/5 neg |
| `market.return_5d` | d1 | −0.1055 | −0.1039 | 0.0240 | 0.2076 | 5/5 neg |
| `market.return_60d` | d3 | −0.1058 | **−0.0652** | 0.0279 | 0.2180 | 5/5 neg |
| `market.return_5d` | d3 | −0.0936 | −0.0967 | 0.0559 | 0.3114 | 5/5 neg |
| `market.return_20d` | d1 | −0.0820 | **−0.0321** | 0.0599 | 0.3114 | 5/5 neg |
| `market.return_60d` | d1 | −0.0655 | **−0.0074** | 0.1277 | 0.5060 | 5/5 neg |

**Every one is negative** — reversal-shaped — and **not one survives
correction.** The largest effect in the entire audit is |ρ| = 0.138.

The trimmed column matters: for `market.return_20d` at d1 the effect falls from
−0.082 to −0.032, and for `market.return_60d` at d1 from −0.066 to **−0.007**.
Those apparent relationships are substantially carried by extreme returns, which
is exactly what the trimmed statistic is reported to expose.

---

## 18. Fold stability

| | |
|---|---|
| cells with the sign consistent in **every** window | **7 of 78** |
| cells whose sign flips | **71 of 78** |
| **features whose mean ρ changes sign across horizons** | **12 of 24** |

Half the usable features point one way at one horizon and the other way at
another. Examples: `event.count_30d` (+0.029 d1, −0.037 d3, +0.024 d5),
`market.return_1d` (+0.022, −0.001, −0.026), `news.article_count_7d` (−0.069,
−0.014, +0.043).

Per §29 these are preserved as `HORIZON_DEPENDENT / SIGN_REVERSAL` rather than
averaged away. But at these magnitudes the honest reading is **noise, not
horizon structure** — a coin flip across horizons is what 12 of 24 looks like.

---

## 19. Multiple testing

| | |
|---|---|
| hypothesis family | **78**, declared in advance, hash `293060c829933584…` |
| tested (p computable) | **72** (6 cells are the constants) |
| uncomputable, kept in the denominator | 6 |
| correction | **Benjamini-Hochberg, q = 0.05** |
| **nominally significant (p ≤ 0.05)** | **13** |
| **rejected after correction** | **0** |
| smallest adjusted p | **0.1246** |

Chance expectation at p ≤ 0.05 over 78 tests is **3.9**; the observed 13 is more
than chance, which is why the correction — not the nominal count — is the
reportable result. **This is the single clearest demonstration in the phase of
why the family had to be declared before testing.**

Nominally significant cells that the correction removed: `market.return_60d` (d5
p=0.0020, d3 p=0.0279), `event.novelty_365d` (d5 p=0.0040),
`market.drawdown_60d` (d5 p=0.0060), `market.distance_from_ma20` (d5 p=0.0080,
d1 p=0.0379), `event.count_7d` (d3 p=0.0080), `volatility.percentile_20d` (d5
p=0.0140), `market.return_20d` (d5 p=0.0140), `technical.rsi_14` (d5 p=0.0220),
`market.return_5d` (d1 p=0.0240, d5 p=0.0319), `volatility.realized_5d` (d1
p=0.0399).

---

## 20. Null / permutation results

| | |
|---|---|
| method | block permutation **within** evaluation windows, across clusters |
| permutations | **500** per hypothesis |
| seed | **20261004**, fixed and recorded |
| budget | 39,000 permutations for the univariate family |
| row-wise IID shuffling | **never used** — the module does not offer it |
| p-value convention | `(k+1)/(B+1)`, so `p = 0` is unreportable |

Measured **1.34 rows per cluster per window**, so within-window permutation is
effectively cluster-level — stated as a measurement, not assumed.

The nulls are well-behaved: for `volatility|d3` the observed statistic 0.1613
sits far above the null median 0.0344 and the null 95th percentile 0.0936.

### Execution register — the full history

| Execution | Status | Why |
|---|---|---|
| `f6d-c52aa9f1e5304330` | **invalidated** | holdout opened and evaluated nothing (§14.2) |
| `f6d-393f974236124f5c` | **invalidated** | superseded — predates the funnel ordering fix |
| `f6d-bfc1421240bd4a7c` | **invalidated** | **unintended re-execution** by a test, while the run-once guard was dead (§14.1) |
| **`f6d-a24902c869064bbb`** | **complete** | **the authoritative run**, by the corrected harness |

Nothing was deleted. Three invalidated attempts with reasons and one complete
execution is the truthful history, not the tidy one.

---

## 21. Redundancy

| | |
|---|---|
| nominal feature count | **26** |
| **effective unique count** | **22** |
| exact / near duplicates (\|ρ\| > 0.999) | 0 |
| **identical fingerprints** | **1 pair** — `event.days_since_last` ↔ `event.independent_source_count` (both constant 0.0) |
| high redundancy (0.90 < \|ρ\| ≤ 0.999) | **3 pairs** |

| Pair | ρ |
|---|---|
| `liquidity.relative_volume_20d` ↔ `liquidity.volume_zscore_20d` | **0.9627** |
| `liquidity.baseline_mean_volume` ↔ `liquidity.baseline_std_volume` | **0.9420** |
| `event.confidence` ↔ `event.quality_confidence` | **0.9364** |

**Nothing was deleted** — this is diagnostic. But a redundant partner may not be
counted as independent evidence, and the constant pair is one source, not two.

---

## 22. Feature group results

21 group probes, ridge α=1, rank-scored, **uncorrected p-values** (exploratory by
plan).

| Group | Horizon | n | mean ρ | windows +/− | p | Verdict |
|---|---|---|---|---|---|---|
| **volatility** | **d3** | 4 | **−0.1613** | **0/5** | **0.0040** | **PROMISING_INFORMATION_GROUP** |
| **liquidity** | **d3** | 4 | **+0.1369** | **5/0** | **0.0140** | **PROMISING_INFORMATION_GROUP** |
| market | d5 | 6 | +0.1139 | 2/2 | 0.0140 | UNSTABLE |
| technical | d5 | 1 | +0.0971 | 3/1 | 0.0220 | UNSTABLE |
| event | d1 | 7 | −0.1021 | 1/4 | 0.0259 | UNSTABLE |
| volatility | d5 | 4 | −0.0806 | 1/3 | 0.0659 | NO_EVIDENCE |
| technical | d1 | 1 | +0.0817 | 4/1 | 0.0778 | NO_EVIDENCE |
| market | d3 | 6 | +0.0858 | 5/0 | 0.0858 | WEAK |
| market | d1 | 6 | +0.0577 | 5/0 | 0.2176 | WEAK |
| *(12 further groups)* | | | \|ρ\| < 0.05 | mixed | > 0.43 | NO_EVIDENCE |

### The multiple-testing exposure, stated arithmetically

**5 of 21** probes fall below an uncorrected 0.05. Chance expectation is
**21 × 0.05 = 1.05**.

The plan declared probe p-values uncorrected and exploratory, so the verdicts
above stand as the rule assigns them. But the arithmetic is reportable without
running a new test: against a BH threshold over 21, rank 1 would need
p ≤ 0.00238 and `volatility|d3` has **0.0040**; rank 2 would need p ≤ 0.00476 and
`liquidity|d3` has **0.0140**. **Neither would survive a correction over the
probe family.** This is an observation, not the pre-registered verdict, and is
labelled as such.

---

## 23. Ablation

The FULL probe is **not significant on any horizon**: d1 ρ=−0.0329 (p=0.455),
d3 ρ=+0.0204 (p=0.681), d5 ρ=+0.0625 (p=0.138).

Removing a group moves it in **both** directions and by less than 0.07:

| Horizon | Largest effect of removing a group |
|---|---|
| d1 | minus `event` → ρ −0.033 → **+0.027** (Δ +0.060) |
| d3 | minus `market` → ρ +0.020 → **−0.041** (Δ −0.062) |
| d5 | minus `market` → ρ +0.062 → **+0.014** (Δ −0.048) |

**No group is necessary and no group is sufficient.** On d1 removing the event
group *improves* the probe, which is what a group of noise plus two constants
would do.

---

## 24. Horizon dependence

Preserved per feature, never collapsed into one label:

| | |
|---|---|
| features whose mean ρ changes sign across d1/d3/d5 | **12 of 24** |
| features sign-consistent across all three horizons | 12 of 24 |
| cells marked WEAK on more than one horizon | `market.return_5d` (d1, d3), `market.return_60d` (d1, d3) |

`market.return_5d` and `market.return_60d` are negative at both d1 and d3 — the
only features with a repeated direction across horizons, and neither survives
correction at either.

---

## 25. Regime dependence

**UNKNOWN.**

The `regime` namespace exists in `FeatureNamespace` and is **absent from this
snapshot** — no regime feature has a single row. There is no canonical regime
representation to condition on, and §30 of the brief forbids building a regime
engine here.

Reported UNKNOWN rather than approximated by, say, splitting on volatility
terciles — that would be inventing a regime definition mid-phase.

---

## 26. Sector / instrument concentration

The concentration check runs only for cells that would otherwise be candidates,
since it cannot rescue a cell that already failed. **No cell reached that
condition** — zero cells were both FDR-significant and sign-consistent — so no
concentration check was triggered on real data.

The mechanism is verified on fixtures: an effect carried by one dominant
instrument correctly **fails** the check, and a broad effect across 20
instruments correctly **survives**. The dominant instrument is chosen by row
count, which is outcome-blind.

Sector-level analysis was **not** performed: the `sector` namespace is absent from
this snapshot.

---

## 27. Pre-holdout survivors

```
features: []                                   (0)
groups:   ['liquidity|d3', 'volatility|d3']    (2)
survivor list hash: eef0d26225dfc591...
```

Frozen and hashed **before** the holdout was touched.

---

## 28. Holdout

| | |
|---|---|
| **opened?** | **YES** |
| why | two groups were candidates after §13, so the frozen list was confirmed on unseen time |
| used for selection? | **never** |

| Candidate | Holdout rows | Research mean ρ | **Holdout ρ** | Same direction | Verdict |
|---|---|---|---|---|---|
| `liquidity\|d3` | 143 | **+0.1369** | **−0.0255** | **NO** | **REJECTED_AT_HOLDOUT** |
| `volatility\|d3` | 143 | **−0.1613** | **−0.0152** | YES | **GROUP_INFORMATION_CONFIRMED** |

**One of two confirmed by direction — which is what a coin flip gives.**

And the confirmation is thin: `volatility|d3` retained **9.4%** of its effect
magnitude. The pre-registered criterion was direction only (§14.7); ρ = −0.015 on
143 rows is not distinguishable from zero, and the verdict should be read as
*"the sign did not flip"* rather than *"the information survived"*.

---

## 29. Confirmed feature information

**One group×horizon, with every qualification attached.**

| | |
|---|---|
| candidate | **`volatility` group at d3** |
| members | `percentile_20d`, `pre_volatility`, `realized_20d`, `realized_5d` |
| relationship | **negative** — the probe's rank prediction is inversely related to the d3 abnormal return |
| research effect | mean ρ **−0.1613**, per-window −0.0869 / −0.2413 / −0.1630 / −0.1598 / −0.1554 |
| stability | **5 of 5 windows negative** — genuinely consistent |
| null | p = **0.0040**; observed 0.1613 vs null median 0.0344, null p95 0.0936 |
| holdout | ρ **−0.0152** on 143 rows — same sign, **9.4% of magnitude** |
| **limitations** | p is **uncorrected** among 21 probes (chance expectation 1.05); **would not survive** a BH correction over that family; holdout confirmation is **direction-only**; the group contains a 0.94-redundant pair at the feature level; no univariate member of the group is itself a candidate |

**This is a hypothesis worth one dedicated pre-registered test. It is not
established information, and it authorises no model.**

---

## 30. Rejected features / negative knowledge

Persisted in `research_candidate_results` (78 rows, queryable by horizon and
verdict) with per-window coefficients, nulls, adjusted p and concentration.

### Carried forward unchanged

Ridge's total failure (F.6B), the raw-logistic calibration failure (F.6C), the
absence of discrimination (F.6C), the F.6B degenerate-configuration and
weak-absolute-quality findings, and d10's empty holdout.

### New from F.6D

1. **No univariate feature in this set carries defensible information at d1, d3
   or d5.** 13 nominal hits, **0** surviving BH at q=0.05, largest |ρ| = 0.138.
   **Do not re-test these 26 features univariately on this snapshot without new
   data or a changed methodology.**
2. **`event.days_since_last` and `event.independent_source_count` are constant
   0.0** and cannot carry information to any model. Fix upstream or drop them;
   do not include them in a design matrix again.
3. **`peer.dispersion_5d` and `peer.relative_return_5d` are `null` in every
   row** — 2,112 rows of nothing.
4. **Event-type and venue information has never been tested**, because the loader
   excludes categoricals by design. That is an untested hypothesis, not a
   negative result.
5. **Half the usable features reverse sign across horizons** (12 of 24), at
   magnitudes consistent with noise.
6. **The apparent effect of `market.return_20d`/`market.return_60d` at d1 is
   largely carried by extreme returns** — trimmed ρ collapses to −0.032 and
   −0.007.
7. **No feature group is necessary or sufficient**: the FULL probe is
   insignificant on every horizon, and removing `event` on d1 *improves* it.
8. **`liquidity|d3` looked promising in-region (ρ +0.137, p 0.014) and reversed
   sign on the holdout** (−0.026). Do not revive it without new evidence.
9. **`volatility|d3` is the only survivor** and retained 9.4% of its effect
   (§29).
10. **Regime conditioning cannot be tested on this snapshot** — the `regime`
    namespace has zero rows.
11. **Sector concentration cannot be tested** — the `sector` namespace has zero
    rows.

---

## 31. Information funnel

```
inventoried               78     (26 numeric features x 3 horizons)
time_safe                 78     0 features dated after their cutoff
usable_values             78
adequate_sample           78     every cell has >= 3 windows and >= 30 clusters
has_variance              72     -6: two constant features x 3 horizons
non_duplicate             72
measurable_effect         72
sign_consistent            7     -65: the sign flips across windows
survived_fdr_and_null      0     -7: none survives BH at q = 0.05
stable_candidates          0
```

Monotone at every stage. The two large losses are **sign instability** (65 cells)
and **the multiple-testing correction** (the final 7).

---

## 32. Implication for modeling

**D. FEATURE ENGINEERING REQUIRED BEFORE MODELING.**

Evidence only:

- **0 of 78** univariate cells carry defensible information; the largest effect
  anywhere is |ρ| = 0.138 and none survives correction.
- The **FULL probe over all 26 features is insignificant on every horizon**
  (p = 0.455 / 0.681 / 0.138). A model given everything these features have finds
  nothing — which is consistent with F.6C's mean AUC of 0.5136 and explains it.
- **No group is necessary or sufficient**, and removing the event group *improves*
  the d1 probe.
- **2 of 26 features are constants and 2 more are entirely null** — 4 of 30
  nominal inputs are dead weight.
- **Event type and venue have never been tested at all**, because categoricals
  are excluded by design.
- The one survivor retained **9.4%** of its effect and would not survive a
  correction over its own probe family.

A more powerful model cannot extract what is not there. The defensible next step
is to **change the inputs** — repair the two constant features, encode the
excluded categoricals properly, or add a new data source — under a new
pre-registered plan. Not to fit a bigger model on these 26 columns.

---

## 33. Expected return

**UNAVAILABLE.** A feature relationship is not a magnitude. No expected return
was computed, stored or passed downstream, and `expected_return_capable` is
`false` in the artifact.

---

## 34. Target capability

**UNAVAILABLE.** 0 of 301 instruments and 0 of 3 horizons can receive a
defensible target. `target_capable` is `false`.

---

## 35. F.5 status

**Unchanged, and correctly so.** Feature research cannot create signal
capability.

| | Before | After |
|---|---|---|
| v1 (production) | 244 × `model_not_qualified` | **244 × `model_not_qualified`** |
| v2 (snapshot) | 301 × `no_model_available` | **301 × `no_model_available`** |
| qualified models / signals / targets | 0 / 0 / 0 | **0 / 0 / 0** |

No `feature_information_available` flag was written into any production path.

---

## 36. As-of / leakage

| | |
|---|---|
| feature timestamps | **0 of 33** features have any value dated after its observation's cutoff |
| normalization | fitted inside the canonical ridge fitter on **training rows only** |
| imputation | **none performed** — incomplete rows dropped, never zero-filled |
| cross-sectional integrity | no cross-sectional feature exists in this snapshot (namespace absent) |
| physical truncation | each evaluation window is a disjoint period; the holdout's rows are strictly at or after the boundary and the research region strictly before |
| future exclusion | no window's rows reach into the holdout; asserted per fold per horizon |
| outcome access | only via the canonical loader; the harness never touches `research_labels` |

---

## 37. Pre-registration integrity

| | |
|---|---|
| changed after results? | **NO** — sha256 matches the value pinned in the harness, which refuses to run otherwise |
| feature list expanded? | **NO** — inventory hash `45dddece…` verified at pre-flight |
| metrics changed? | **NO** |
| multiple-testing rule changed? | **NO** — BH q=0.05 over the 78 declared in advance |
| interaction budget expanded? | **NO** — 3, named in advance |
| probe budget expanded? | **NO** — 54 probe cells |
| holdout changed? | **NO** — inherited, and the rule re-derives it |
| holdout criterion tightened after seeing the survivor? | **NO** (§14.7) |

Every answer is NO.

**One qualification, stated plainly:** the experiment was executed four times —
once with a defective holdout, once superseded by an artifact-format fix, once
*accidentally* by a test while the run-once guard was dead, and once
authoritatively. The first three are on the register as `invalidated` with
reasons. All 78 cells were byte-identical across runs, which is a determinism
result and not independent evidence.

---

## 38. D20 status

| | |
|---|---|
| executed? | **NO** |
| inspected? | **NO** |
| inferred? | **NO** |
| consumed? | **NO** |
| used for feature ranking? | **NO** |
| used for feature selection? | **NO** |
| used for interaction discovery? | **NO** |
| used for horizon selection? | **NO** |
| hypothesis changed? | **NO** |
| anchor-v2 changed? | **NO** |
| protected window changed? | **NO** |

`guard_no_protected_horizon` raises on `d20`; it is absent from `HORIZONS` and
marked `PROTECTED`. A test asserts **no result cell** — univariate, group,
ablation or interaction — carries horizon `d20`, while also asserting
`market.return_60d` **is** present in the audit.

**On `market.return_60d` specifically.** It is one of the 26 legitimate features
and was audited against d1, d3 and d5 exactly as F.6B and F.6C already used it.
Its measured d1/d3/d5 results appear in §17 and §19. **No inference about the
protected d20 relationship is drawn here, and none should be read into these
numbers** — they concern different horizons, different labels, and none of them
survives correction in any case.

---

## 39. Model promotion

```
models promoted = 0
```

No promotion path exists in this phase. A test asserts the harness contains no
`promote_model` or `PRODUCTION_ACTIVE`.

---

## 40. Production behaviour

| | |
|---|---|
| Portfolio / Risk / OrderIntent / Execution changed? | **NO** |
| Capture / IBKR / Subscriptions changed? | **NO** |
| Production model changed? | **NO** |
| **Files modified** | **0** |

The production database was never opened for writing; a test asserts it holds 581
observations and no `research_executions` table.

---

## 41. Broker safety

```
broker writes attempted:       0
broker writes succeeded:       0
paper orders:                  0
live orders:                   0
billable market-data requests: 0
```

Tests assert the harness imports no execution, broker, portfolio, risk, capture
or network module, and that the statistics module imports only `hashlib`, `json`,
`math`, `random`, `dataclasses`, `enum` and `typing`.

---

## 42. Test results

### Full suite

| | Baseline `7914aee` | **Final** |
|---|---|---|
| Tests | 5,405 | **5,468** |
| Pass | 5,404 | **5,467** |
| Fail / Errors | 0 / 0 | **0 / 0** |
| Skipped | 1 | **1** |
| Runtime | 566.444 s | **761.246 s** |

```
PYTHONPATH=src python -m unittest discover -s tests -t . -b
Ran 5468 tests in 761.246s
OK (skipped=1)
```

**+63 tests, exactly the new file. Zero regressions.**

### Targeted suites

| Suite | Tests | Result | Runtime |
|---|---|---|---|
| **F.6D — `test_f6d_feature_information`** | **63** | **OK** | 6.0 s |
| F.6C — `test_f6c_calibration` | 71 | OK | 6.4 s |
| F.6B — `test_f6b_frozen_experiment` | 59 | OK | 3.2 s |
| F.7 — corpus recovery / snapshot integrity / boundary | 71 / 55 / 30 | OK | 42.7 / 0.6 / 11.7 s |
| F.6 — sample adequacy / research discipline | 24 / 33 | OK | 0.1 / 6.9 s |
| adaptive (all of the above plus I-F…I-B) | **1,124** | OK | 162.1 s |
| modeling | 142 | OK | 3.2 s |
| signals / research / experiments | 129 / 104 / 83 | OK | — |
| portfolio / trading / capture | 273 / 224 / 126 | OK | — |
| **full suite** | **5,468** | **OK** (skipped=1) | **761.2 s** |

A side effect worth noting: the F.6D suite dropped from 143 s to 6 s once the
run-once guard worked, because the guard-test no longer re-executes the whole
experiment.

---

## 43. Compute / performance

| | |
|---|---|
| feature × horizon cells | **78** |
| group / ablation / interaction cells | 21 / 24 / 9 |
| **total cells** | **132** |
| permutation runs | **39,000** (78 × 500) |
| probe fits | **254 ridge fits** — (7 groups + 8 ablation configs + 3 interactions) × (5+5+4 windows) = 252, plus 2 holdout group probes |
| runtime, end to end | **84.5 s** |
| plan runtime (read-only) | ~12 s |
| storage growth | ~+250 KB (4 markers + 78 candidate rows) |
| result artifact | `docs/PHASE_25_9I_F6D_RESULTS.json` |
| paid API / LLM / network calls | **0 / 0 / 0** |

---

## 44. Scope-creep review

| | |
|---|---|
| dataset changes | **none** — read `mode=ro`; only markers and candidate rows written |
| F.6B / F.6C changes | **none** — verified at pre-flight and asserted by test |
| qualification-gate changes | **none** — this phase defines no gate that could substitute for one |
| signal-threshold changes | **none** |
| model promotion | **none** |
| production changes | **none** — 0 files modified |
| IBKR changes | **none**, 0 requests |
| D20 changes | **none** |
| new dependencies | **none** |

---

## 45. Known limitations

1. **Three horizons only** (d1, d3, d5); d10 has no holdout and intraday has no
   pre-registered purge policy.
2. **4 to 5 evaluation windows per horizon**, with test windows as small as 28
   rows (d3 wf5) and 52 (d1 wf5).
3. **The holdout confirmation criterion is direction-only**, which is why a 9.4%
   magnitude retention counts as confirmed (§14.7).
4. **Probe p-values are uncorrected**, by plan; the chance exposure is reported
   but not corrected into the verdicts.
5. **Rank correlation measures monotone association only.** A purely non-monotone
   relationship would be invisible univariately — which is part of why the probes
   and interactions exist, but three interactions is not a search.
6. **Event-type and venue are untested**, excluded by the loader's design.
7. **No regime and no sector analysis** — both namespaces have zero rows.
8. **Mutual information was declined**, with the reason recorded: no estimator
   exists in the project and one added for 78 thin cells would produce numbers
   whose assumptions could not be defended.
9. **Cross-sectional features do not exist** in this snapshot, so universe-bias
   and survivorship questions could not be tested at feature level.
10. **One group×horizon survived** and the phase does not treat it as information
    (§29).

---

## 46. Readiness for the next modeling experiment

**PARTIAL.**

| Requirement | State |
|---|---|
| ≥ 1 scientifically defensible feature/group hypothesis | **one, and weakly** — `volatility` at d3 (§29) |
| univariate foundation | **none** — 0 of 78 |
| a feature set worth modelling as-is | **no** — the FULL probe is insignificant on every horizon |

There is exactly one hypothesis worth a dedicated pre-registered test. There is
**no** basis for a general modelling experiment on these 26 columns, and §32's
answer is that the inputs need work before the models do.

---

## 47. Readiness for I-G

**NO.**

There is still no qualified trading model, and F.6D cannot create one. Feature
research does not produce shadow-validatable predictions.

---

## 48. Final verdict

> ## PHASE 25.9I-F.6D IMPLEMENTATION = COMPLETE
>
> ## FEATURE INFORMATION OUTCOME = MIXED / CONDITIONAL INFORMATION
>
> ## NEXT MODELING EXPERIMENT READINESS = PARTIAL
>
> ## I-G READINESS = NO

The plan was frozen at `2e3cc2d` — a commit holding the pre-registration and no
harness, results or marker — with its hash pinned in the harness and the
78-hypothesis family declared before any feature-outcome statistic was computed.
All 132 cells were evaluated in 84.5 s.

**The inventory itself was the first finding.** The real registry is **33**
features, not the 26 every prior phase reported: 5 are categorical and excluded
by design, **2 are `null` in all 1,056 rows**, and **2 of the 26 "numeric"
features are constant 0.0** — identical vectors that cannot carry information to
any model and have been columns in every design matrix since Phase 25.9. Four of
thirty nominal inputs are dead weight.

**No univariate feature carries defensible information.** 71 of 78 cells are
NO_INFORMATION, 7 are WEAK, **0 are candidates**. Thirteen cells reach a nominal
p ≤ 0.05 against a chance expectation of 3.9 — and **none survives
Benjamini-Hochberg at q = 0.05**, the smallest adjusted p being 0.1246. The
largest effect anywhere is |ρ| = 0.138, and for two of the seven strongest cells
the effect halves or vanishes under trimming. Half the usable features reverse
sign across horizons.

**And the group probes agree.** The FULL probe over all 26 features is
insignificant on every horizon (p = 0.455 / 0.681 / 0.138); no group is necessary
or sufficient; removing the event group *improves* the d1 probe. Of two
pre-holdout group candidates, `liquidity|d3` **reversed sign on the holdout** and
`volatility|d3` kept its sign while retaining **9.4%** of its magnitude — a
direction-only confirmation that is not distinguishable from zero, from a probe
family whose p-values are uncorrected and whose chance expectation is 1.05 hits.

**This explains F.6C rather than contradicting it.** A mean ROC-AUC of 0.5136 is
what a model should produce from inputs that carry this little. **So the answer
is D: feature engineering is required before modelling** — repair the constants,
encode the excluded categoricals, or add a data source, under a new
pre-registered plan. A more powerful model cannot extract what is not there.

**Two defects were found in protections, not results**, and both are reported in
full: the holdout opened once and evaluated nothing, and — worse — the run-once
guard was dead, so the test written to assert it instead *re-executed the frozen
experiment*. Both are fixed, both defective runs are on the register as
`invalidated` with reasons, and the 78-cell byte-identity across executions is
reported as determinism rather than as evidence.

**D20 remains sealed**, including the one place it could have gone wrong by
accident: `market.return_60d` was audited against d1, d3 and d5 and never
against d20, and no inference about the protected relationship is drawn from
those results. Models qualified and promoted: **0**. Broker writes, paper orders,
live orders, billable market-data requests: **0 / 0 / 0 / 0**. 0 files modified.
