# Phase 25.9B — Frozen Hypothesis

**Committed before any protected-window statistic under anchor-v2 was
computed** — including label counts under v2. The git timestamp on this
file is the evidence.

Written 2026-09-13.

---

## 1. The hypothesis — frozen, no drift

**D20 REVERSAL.** Across instruments on the same date, a higher trailing
60-day return is associated with a *lower* subsequent abnormal return
over trading days 5 → 20 after the event.

| Element | Frozen value |
|---|---|
| feature | `market.return_60d` |
| sign | **negative** |
| target | `d20` abnormal return **minus** `d5` abnormal return, both under **anchor-v2** |
| horizon | trading days 5 → 20 after the event |
| formulation | cross-sectional, within date |
| label method | `anchor-v2` |

### Why "d20 minus d5"

Both windows share one base price under anchor-v2, for the instrument
and for its benchmark. Subtracting cancels that base, leaving the
abnormal move from day 5 to day 20 — the anchor-cancelled formulation
identified in 25.9A.

### Disclosed discrepancy, resolved in favour of the stated definition

25.9A's **stated** formulation is "d20 *abnormal* return,
anchor-cancelled". Its **exploratory** cancellation check computed
*raw* returns. §11 forbids drifting between the two. The stated,
abnormal definition is frozen here. It is also the more defensible
target: it strips the market component that raw returns carry.

---

## 2. Statistic

For each protected date with **≥ 10** observations having both the
feature and the target: Spearman rank correlation between
`market.return_60d` and the target. The statistic is the **mean across
eligible dates**.

Secondary, descriptive only: the mean target spread between the top and
bottom tercile of `market.return_60d`, within date, averaged across
dates.

---

## 3. Baseline — frozen

**Zero predictive edge.** The repository's mandatory baselines
(`historical_mean`, `majority_class`) are defined for direct prediction,
not cross-sectional ranking; for a rank hypothesis the correct baseline
is no rank information at all.

It is tested with a **within-date permutation null**, 2,000 draws, seed
**20260913**, preserving each date's composition.

---

## 4. Sample requirement — frozen

All must hold, or the result is **INSUFFICIENT DATA**:

- ≥ **100** protected observations with both feature and target resolved
- ≥ **8** eligible dates (≥ 10 observations each)
- minimum detectable |IC| at 80% power ≤ **0.20**, using the count of
  resolved observations

---

## 5. Decision rule — frozen

| Condition | Verdict |
|---|---|
| sample requirement not met | **INSUFFICIENT DATA** |
| mean IC < 0 **and** one-sided permutation p < 0.05 | **SUPPORTED** |
| mean IC ≥ 0, **or** p ≥ 0.05 | **NOT SUPPORTED** |

No "weak support" category exists. A p of 0.06 is NOT SUPPORTED.

---

## 6. Protected window

| | |
|---|---|
| start | 2026-08-15T01:23:31 UTC |
| end | 2026-08-27T17:09:32 UTC |
| rows | 195 |
| previously opened | never |

Evaluated **exactly once**, only if §4 is met. If §4 is not met, the
evaluation **does not run** and the window stays closed.

---

## 7. What does not move

`min_signal_confidence`, the deployability gate, promotion, risk,
signal rules, the paper/live boundary. No model is trained or promoted.
No order of any kind is placed.
