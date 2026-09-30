# Phase 25.9I-F.6 — Pre-registration

**Written 2026-09-30, before any candidate was evaluated on any horizon.**

This document fixes every choice that could otherwise be made after seeing a
result. Where F.6 departs from the Phase 25.9 pre-registration, the departure is
named in §8 and was decided here, in advance.

> **A MODEL IS NOT QUALIFIED BECAUSE WE NEED ONE.**
>
> **FAILURE TO FIND EDGE IS A VALID RESEARCH RESULT.**

---

## 1. What changed since Phase 25.9, and why re-running is legitimate

Phase 25.9 researched **d5** with four candidates and found **NO MODEL
QUALIFIED**. Its protected window was **never opened**. §29 of the F.6 brief
forbids repeating a rejected hypothesis *without new evidence*. There is new
evidence, and it is of two kinds:

1. **Labels matured.** The stored d5 evaluation (2026-08-29) rested on 5 test
   rows spanning 4 instruments. Re-running the identical split today gives 32
   test rows spanning **27** instruments. Nothing was added to the corpus; the
   calendar advanced and forward labels resolved.
2. **Three horizons have never been researched at all** — d1, d3 and d10 have
   no model of any kind, and no negative result exists for them.

## 2. The corpus, as it actually is

Measured 2026-09-30, read-only.

| | |
|---|---|
| Observations | 581, all created 2026-08-28T21:42:54 in one rebuild |
| Information cutoffs | 2026-07-30 → 2026-08-27 (29 days) |
| Distinct clusters | 244 |
| Quality | 565 high, 16 invalid (excluded) |
| Features | 26 numeric, 19,103 feature rows |

**A cluster is an instrument.** `event_cluster_id` equals `instrument_id` for
every row, so `effective_sample` answers *"how many distinct companies is this
evidence about"*. That is the right question and it is not changed here.

### Sample adequacy, per horizon

| Horizon | Rows | **Clusters** | Sessions | Research-region clusters | Readiness |
|---|---|---|---|---|---|
| intraday_5m | 554 | 237 | 29 | 179 | DATA_ACCUMULATION_REQUIRED |
| intraday_15m | 554 | 237 | 29 | 179 | DATA_ACCUMULATION_REQUIRED |
| intraday_30m | 554 | 237 | 29 | 179 | DATA_ACCUMULATION_REQUIRED |
| intraday_60m | 554 | 237 | 29 | 179 | DATA_ACCUMULATION_REQUIRED |
| **d1** | 544 | **236** | 27 | 178 | **RESEARCH_READY** |
| **d3** | 531 | **236** | 25 | 178 | **RESEARCH_READY** |
| **d5** | 458 | **223** | 23 | 178 | **RESEARCH_READY** |
| **d10** | 286 | **159** | 17 | 157 | **RESEARCH_READY** |
| d20 | — | — | — | — | **PROTECTED** |

Floor: `ModelEvaluation.MIN_EFFECTIVE_SAMPLE = 30`, unchanged.

## 3. Horizons selected, and horizons deferred

**Researched: d1, d3, d5, d10.** Four daily horizons, each with its own target,
its own labels and its own verdict. They are never pooled.

**Deferred: all four intraday horizons.** 29 distinct sessions against a stated
floor of 30. Thousands of minute bars from 29 days are many views of a few days,
not many independent days. The shortfall is **one session**, and it will be
reported as such rather than rounded away.

**Out of scope: d20.** Sealed.

## 4. The holdout boundary is frozen where Phase 25.9 put it

`2026-08-15T01:23:31Z`, unchanged.

Phase 25.9 computed it as the last 20% of a **972-row** corpus. That corpus was
rebuilt on 2026-08-28 and today holds **581** rows, so the same instant now sits
at **60.6%** — a 39% holdout rather than a 20% one.

**It is not recomputed.** Moving a holdout boundary after discovering the old
one is inconvenient is holdout-shopping, and a larger holdout is more
conservative, not less. The research region is everything before that instant;
nothing after it is read during development.

The Phase 25.9 rule is carried over unchanged: **if no candidate survives the
research region, the holdout is not opened at all.**

## 5. Candidates — fixed at four, applied to four horizons

The same four specifications Phase 25.9 pre-registered, reused verbatim so the
new horizons are tested against a contract that was written before any of this.

| | Family | Task | Features |
|---|---|---|---|
| C0 | ridge (α=1) | abnormal return | all numeric |
| C1 | logistic | direction | all numeric |
| C2 | ridge (α=10) | abnormal return | all numeric |
| C3 | logistic | direction | dense only |

**16 candidate-horizon cells. The budget is 16 and will not grow.** No
hyperparameter sweep, no additional families, no seeds explored.

**Independence:** C0 and C2 are one family at two regularisation strengths and
are **not** independent evidence of each other. Neither are C1 and C3. Nothing
in this phase may count them as model agreement.

## 6. The quality contract — Phase 25.9's, unchanged

A candidate is **QUALIFIED** only if **every** condition holds:

1. Beats **every** mandatory baseline on the primary metric, out-of-sample
   (`ModelEvaluation.beats_all_baselines`).
2. Effective sample ≥ 30 (`ModelEvaluation.MIN_EFFECTIVE_SAMPLE`).
3. Expanding walk-forward over the research region, beating the baselines in a
   **strict majority** of folds — a high mean carried by one fold does not count.
4. **At least 3 valid folds** must be formable. Fewer ⇒ **INSUFFICIENT DATA**,
   regardless of any score.
5. Probabilistic candidates: Brier must beat the base-rate Brier.
6. Survivors only: one evaluation on the frozen holdout, once.

**Does not count:** positive R², accuracy above 50%, beating the previous model,
a single winning window, or "close".

## 7. Fold construction

Per horizon, expanding day-granular folds inside the research region:

| Parameter | Value | Source |
|---|---|---|
| purge horizon | label's calendar span | `splits.purge` |
| embargo | 6 days | Phase 25.9 |
| test window | 7 days | Phase 25.9 |
| step | 7 days | Phase 25.9 |
| min train rows | 30 | Phase 25.9 |
| min test rows | 20 | Phase 25.9 |

Purge horizons are the **calendar** span of each label, so a weekend cannot
smuggle a label across a boundary: d1→3, d3→5, d5→7, d10→14 days. Each is ≥ the
trading-day span it protects; over-purging can only remove rows, never admit
leakage.

## 8. Deviations from the Phase 25.9 pre-registration

1. **Four horizons instead of one.** Each is evaluated independently with its
   own labels, folds and verdict.
2. **Purge horizon varies by label**, per the table above, instead of a fixed 7.
3. **Multiple testing is now 16 cells, not 4.** The defence is unchanged and
   remains the fold-majority rule: a candidate must win consistently on its own
   horizon. No threshold is relaxed to compensate, and the count is fixed here.

## 9. Stopping rules

Research stops for a horizon when any of these is true:

- all four candidates have been evaluated (the budget is 16 cells total);
- fewer than 3 valid folds can be formed (⇒ INSUFFICIENT DATA);
- the research-region effective sample falls below 30.

**No candidate is retried with different hyperparameters after failing.** There
is no branch in the plan that responds to a result.

## 10. Valid outcomes

All of these are acceptable, and the phase is COMPLETE in every case:

- **QUALIFIED RESEARCH CANDIDATE(S)** — one or more meet every condition in §6.
- **NONE QUALIFIED** — the research is sound and nothing passed.
- **INSUFFICIENT DATA** — folds or sample could not support a fair test.

A correct "none qualified" is better than a false trading model.

## 11. Governance that does not move

`MIN_EFFECTIVE_SAMPLE` (30), `min_signal_confidence` (0.40), the deployability
gate, the promotion gate, the baselines, the confidence formula, the paper/live
boundary, and the D20 seal. **None is changed to make any candidate pass.**

Qualification is not promotion. This phase may produce a **QUALIFIED RESEARCH
CANDIDATE**; it may not produce a `PRODUCTION ACTIVE` model, and it contains no
code path that could.

## 12. D20

Not trained, not evaluated, not read, not inferred, not used for feature,
family, hyperparameter, threshold, target or horizon selection. The horizon
appears in inventories as **PROTECTED** and nowhere else.
