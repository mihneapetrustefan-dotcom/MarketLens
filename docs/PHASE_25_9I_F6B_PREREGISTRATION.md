# Phase 25.9I-F.6B — Pre-registration (dataset and research environment)

**Written 2026-10-01 by Phase 25.9I-F.7, before any candidate has been
evaluated against the recovered dataset.**

F.7 built this environment and froze it. **F.7 did not run F.6B.** No model of
any kind has been fitted against `research_snapshot_v2`; the snapshot's database
contains zero rows in `trained_models`, `model_evaluations` and `predictions` by
construction, and a test asserts it.

> **A MODEL IS NOT QUALIFIED BECAUSE WE NEED ONE.**
>
> **MORE HISTORICAL ROWS ARE USEFUL ONLY IF THEIR TEMPORAL AND PROVENANCE
> INTEGRITY IS VALID.**

---

## 1. Why a new pre-registration exists

Phase 25.9I-F.6 researched four daily horizons against `research_snapshot_v1`
and returned **INSUFFICIENT DATA** on all four: the research region was 15
calendar days and a single legal fold needed 16–27. That result stands and is
not reinterpreted here.

F.7 then found the cause upstream. Entity resolution had been run with a
publication floor, so 17,789 articles published before 2026-07-30T13:04:26Z were
never scanned, and no event could be extracted from them. Replaying the existing
stages without that floor produced a new corpus.

**A new dataset requires a new experiment.** This document freezes the
environment for it.

---

## 2. The dataset

| | |
|---|---|
| Snapshot | **`research_snapshot_v2`** |
| Corpus | **`canonical_corpus_v2`** |
| Storage | `data/marketlens_corpus_v2.db` |
| Corpus content hash | `4247f543904204c9...` (full value in the registry) |
| Snapshot content hash | `4ccab384d37a9eb2...` |
| Recovery run | `rec-a8472b411f4b4073`, status **COMPLETE** |
| Built at commit | recorded in `recovery_runs.code_commit` |
| State | **FROZEN** |

### What it contains, measured

| | v1 | **v2** |
|---|---|---|
| Canonical events | 581 | **1,056** |
| Research observations | 581 | **1,056** |
| Distinct clusters (instruments) | 244 | **301** |
| Research labels | 9,192 | **16,116** |
| Research features | 19,103 | **34,397** |
| Earliest information time | 2026-07-30T13:34:32Z | **2026-07-02T07:00:00Z** |
| Latest information time | 2026-08-27T20:04:01Z | **2026-08-30T14:35:00Z** |
| Information span | 28 days | **59 days** |
| Observations marked invalid | 16 (2.8%) | **141 (13.4%)** |

The invalid share rose and that is reported rather than smoothed: July
observations are **24.5%** invalid against August's **2.8%**, every one of them
for `insufficient_price_data`. The recovered July region therefore carries less
usable evidence per row than the August region does.

**`research_snapshot_v1` is unchanged.** Its manifest hashes were measured before
and after the recovery and are identical. F.6's result remains addressable
against the data it actually used.

---

## 3. The holdout

| | |
|---|---|
| **Boundary** | **`2026-08-18T00:00:00Z`** |
| Rule | the final **20%** of the snapshot's information-time span, floored to the start of the UTC day |
| Research region | 2026-07-02T07:00:00Z → 2026-08-18T00:00:00Z, **46 days** |
| Holdout region | 2026-08-18T00:00:00Z → 2026-08-30T14:35:00Z, **12 days (21.26%)** |
| Decided | 2026-10-01, before any candidate was fitted |

**The fraction is Phase 25.9's, carried over unchanged.** That is the point: the
rule predates this dataset, so it cannot have been chosen to suit it. The
boundary was computed from the information range by
`research_snapshot.deterministic_holdout()` and was not adjusted afterwards.

`HoldoutDecision` refuses to construct when `observed_model_outcome` is set, so
a boundary chosen after a result cannot be frozen as a holdout at all.

**The old holdout (`2026-08-15T01:23:31Z`, snapshot v1) is untouched.** It belongs
to F.6's experiment and stays there.

**Rule carried over from Phase 25.9 and F.6:** if no candidate survives the
research region, the holdout is not opened.

---

## 4. Horizons

### Pre-registered for F.6B: d1, d3, d5, d10

Four daily horizons, each with its own labels, its own calendar-span purge and
its own independent verdict. **They are never pooled.**

| Horizon | Research-region rows | **Clusters** | Purge (days) | First fold needs | 3 folds need | **Max folds** |
|---|---|---|---|---|---|---|
| **d1** | 737 | **227** | 3 | 16 | 30 | **5** |
| **d3** | 735 | **227** | 5 | 18 | 32 | **5** |
| **d5** | 735 | **227** | 7 | 20 | 34 | **4** |
| **d10** | 633 | **211** | 14 | 27 | 41 | **3** |

Research region: **46 days**. Every horizon forms at least the required three
folds. Effective sample floor: **30**, unchanged; every horizon clears it by a
factor of seven.

### Data-eligible but deferred: all four intraday horizons

The recovered corpus gives intraday horizons **57 distinct sessions** against a
floor of 30 — F.6 deferred them at 29. They now clear every data threshold.

They are still **deferred**, for a reason that has nothing to do with the data:
the only purge horizon available to them is the **14-day fallback written for
d10**. Fourteen calendar days of purge around a five-minute forward window is
not a policy, it is a placeholder. An intraday purge must be pre-registered on
its own evidence in a document of its own.

It must not be chosen because it produces more folds. That is why it is not
being chosen here, under time pressure, in a document whose other numbers are
already known.

### Out of scope: d20 — **SEALED**

Not researched, not inspected, not inferred, not consumed. The horizon appears in
inventories as PROTECTED and nowhere else. `research_snapshot.effective_sample()`
raises if asked about it, and `ResearchHandoff` refuses to construct unless
`d20_excluded` is true.

---

## 5. Candidates and budget

The same four specifications Phase 25.9 pre-registered and F.6 reused, taken
verbatim from `scripts/research_model_quality.spec_for`.

| | Family | Task | Features |
|---|---|---|---|
| C0 | ridge (α=1) | abnormal return | all numeric |
| C1 | logistic | direction | all numeric |
| C2 | ridge (α=10) | abnormal return | all numeric |
| C3 | logistic | direction | dense only |

**Budget: 4 candidates × 4 horizons = 16 cells. It will not grow.** The same
budget as F.6, deliberately: a larger corpus is not a licence for a wider search.

No hyperparameter sweep. No additional families. No seeds explored. No candidate
retried with different hyperparameters after failing. There is no branch in this
plan that responds to a result.

**Independence:** C0 and C2 are one family at two regularisation strengths and are
**not** independent evidence of each other. Neither are C1 and C3. Nothing in
F.6B may count them as model agreement.

---

## 6. Split policy

Carried over from Phase 25.9 and F.6 without modification.

| Parameter | Value |
|---|---|
| folds | expanding, day-granular, strictly inside the research region |
| purge | the label's **calendar** span: d1→3, d3→5, d5→7, d10→14 days |
| embargo | **6 days** |
| test window | **7 days** |
| step | **7 days** |
| min train rows | 30 |
| min test rows | 20 |
| **minimum valid folds** | **3** |

Purge is in calendar days so a weekend cannot carry a label across a fold
boundary. Each purge is at least the trading-day span it protects; over-purging
removes rows and cannot admit leakage.

---

## 7. Baselines, metrics and the gate

Unchanged and reused, never redefined.

| | |
|---|---|
| Baselines | `ModelingEngine.train_and_evaluate`'s mandatory baselines |
| Primary metric | per task, as the engine defines it |
| Probabilistic candidates | Brier must beat the base-rate Brier |
| Qualification gate | `src.domain.model_models.ModelEvaluation.is_deployable` |
| Effective sample floor | `ModelEvaluation.MIN_EFFECTIVE_SAMPLE` = **30** |
| Signal confidence floor | `min_signal_confidence` = **0.40** |

A candidate is **QUALIFIED** only if **every** condition holds:

1. beats **every** mandatory baseline on the primary metric, out of sample;
2. effective sample ≥ 30, recomputed from this snapshot's own clusters and never
   read from a stored evaluation;
3. expanding walk-forward over the research region, beating the baselines in a
   **strict majority** of folds — a high mean carried by one fold does not count;
4. at least **3** valid folds formable; fewer ⇒ **INSUFFICIENT DATA**, whatever
   the scores;
5. probabilistic candidates: Brier beats the base rate;
6. survivors only: one evaluation on the frozen holdout, once.

**Does not count:** positive R², accuracy above 50%, beating the previous model,
one winning window, or "close".

**Qualification is not promotion.** F.6B may produce a QUALIFIED RESEARCH
CANDIDATE. It may not produce a `PRODUCTION ACTIVE` model.

---

## 8. Effective sample is recomputed, never inherited

F.6's root cause was a stored `effective_sample_size` of **4** — the cluster count
of a five-row test window dated 2026-08-29 — being read as the corpus's capacity.

Two defences, both structural:

1. `research_snapshot.effective_sample()` counts distinct `event_cluster_id`
   values in the snapshot and **does not consult `model_evaluations` at all**.
2. `trained_models`, `model_evaluations` and `model_baseline_comparisons` are
   **empty** in the v2 database. There is no stale evaluation there to misread.

A cluster is an instrument, so the effective sample answers *how many distinct
companies is this evidence about*. That definition is v1's and is not changed.

---

## 9. Stopping rules

Research stops for a horizon when any of these is true:

- all four candidates have been evaluated (the budget is 16 cells in total);
- fewer than 3 valid folds can be formed ⇒ **INSUFFICIENT DATA**;
- the research-region effective sample falls below 30.

---

## 10. Valid outcomes

All of these are acceptable, and F.6B is COMPLETE in every case:

- **QUALIFIED RESEARCH CANDIDATE(S)** — one or more meet every condition in §7.
- **NONE QUALIFIED** — the research is sound and nothing passed.
- **INSUFFICIENT DATA** — folds or sample could not support a fair test.

A correct "none qualified" is better than a false trading model. **Failure to
find edge is a valid research result.**

The recovered dataset makes the question *askable*. It says nothing whatever
about the answer.

---

## 11. Governance that does not move

`MIN_EFFECTIVE_SAMPLE` (30), `MIN_VALID_FOLDS` (3), `min_signal_confidence`
(0.40), the deployability gate, the promotion gate, the mandatory baselines, the
confidence formula, the paper/live boundary and the D20 seal. **None is changed
to make any candidate pass, and none was changed by F.7.**

---

## 12. Known limitations of this environment

Stated here, before any result, so none of them can later be presented as a
discovery that excuses one.

1. **July observations are 24.5% invalid** for `insufficient_price_data`, against
   2.8% in August. The recovered region is thinner in usable evidence than its
   row count suggests.
2. **`event_time` is missing for all 1,056 canonical events**, exactly as in v1.
   Recovery did not improve event-occurrence timing, and nothing here infers it.
3. **`independent_source_count` is 0 for every canonical event**, in v2 as in v1.
   Source independence remains **UNKNOWN** and F.6B may not treat multi-source as
   independently corroborated.
4. **1,036 of 1,056 canonical events are single-source.** Corroboration is
   mostly absent.
5. **A cluster is an instrument**, so two separate events at the same issuer count
   as one cluster. Conservative, and coarse.
6. **The holdout is 12 days.** It satisfies the inherited rule, but it is short in
   absolute terms, and a single holdout evaluation on 12 days of data is weak
   evidence even when it passes.
7. **Intraday is deferred on policy, not on data** (§4).

---

## 13. D20

Not trained, not evaluated, not read, not inferred. Not used for feature,
family, hyperparameter, threshold, target or horizon selection. Not used to
choose which records to recover, and not used to place this holdout.
