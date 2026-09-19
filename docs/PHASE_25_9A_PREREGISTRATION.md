# Phase 25.9A — Pre-Registration

Two stages, each committed before it runs.

- **Stage 1** (this section, now) — an exploratory diagnostic of whether
  the features contain information at all, and in what shape.
- **Stage 2** — confirmatory candidates, appended to this file **after**
  Stage 1 and **before** any confirmatory run.

Written 2026-09-13, before Stage 1 was executed.

---

## 0. What the data already told us (counts only, no outcomes)

Measured on the research region, reading no label values:

| | |
|---|---|
| observations per **instrument** | median **2**; 82 of 283 have exactly one |
| observations per **date** | median **21**; 24 of 41 dates have ≥20 |
| labelled rows, every horizon | ≈780 (d20: 554) |

Two consequences, fixed here as premises:

1. **Per-instrument time-series modelling is infeasible.** Two
   observations cannot describe an instrument's dynamics.
2. **Every target labels the same ≈780 events.** Twenty targets are
   twenty correlated looks at one sample, not twenty datasets.

## 1. The central hypothesis

**H1.** Phase 25.9's direction results were decided by a market-wide
regime shift (training 58% up, test week 27% up). A common move is
removed by ranking instruments **against each other within the same
date**. Therefore information should be more detectable
cross-sectionally than pooled.

H1 is falsified if the cross-sectional formulation shows no more
information than the pooled one.

---

## 2. Stage 1 protocol

**Region.** Research region only: information cutoff before
2026-08-15T01:23:31. The protected window is not read.

**Targets (9).** Abnormal return at d1, d3, d5, d10, d20, intraday_5m,
intraday_15m, intraday_30m, intraday_60m.

Raw returns are excluded because they carry the common market move
that abnormal return is defined to strip; the volume labels are not
return targets.

**Features (26).** Every numeric feature in `research_features`.

**Two formulations.**

| | Statistic |
|---|---|
| POOLED | Spearman rank correlation over all rows |
| CROSS-SECTIONAL | Spearman within each date with ≥10 rows, averaged across dates |

**Stability.** Each (feature, target, formulation) IC is also computed
per chronological week; sign consistency is reported.

**Search size.** 9 targets × 2 formulations × 26 features = **468**
comparisons.

---

## 3. Stage 1 decision rule — fixed before running

468 comparisons will produce large correlations by chance. The bar is
a **permutation null**, not a fixed IC threshold.

- Shuffle each target **within date**, preserving date structure.
- Recompute the **global maximum |IC|** across all 468 comparisons.
- Repeat **500** times.

**Information is PRESENT** only if the observed global-best |IC|
exceeds the **95th percentile** of that null distribution.

Per-(target, formulation) nulls are reported for description, but no
claim of information is made except at the global level.

If Stage 1 finds nothing above the null, the Stage 2 budget is still
available but the verdict will state that no information was detected.

---

## 4. Multiple testing, cumulative

| Phase | Looks at the research region |
|---|---|
| 25.9 | 4 candidates |
| 25.9A Stage 1 | 468 comparisons, corrected by the global null |
| 25.9A Stage 2 | capped at **4** candidates |

The research region has now been examined heavily. **Any positive
result there is weak evidence.** The only independent test remaining
is the protected window.

---

## 5. Protected window — unchanged

Same 195 rows as Phase 25.9, still unopened. Opened **once**, only for
a Stage 2 candidate that survives, after every choice is frozen. A
failure there is a failure.

---

## 6. Governance that does not move

`min_signal_confidence` (0.40), the deployability gate, promotion,
human approval, mandatory baselines, the paper/live boundary. The
gate defect found in 25.9 remains a pending human decision and is not
changed here.

---
---

# STAGE 2 — Appended after Stage 1, before any confirmatory run

Written 2026-09-13 after Stage 1 (`99c6464`) and before any protected
row's label was read. Only label **counts** were consulted to write this.

## 7. What Stage 1 found, and what it turned out to be

Stage 1's pre-registered test returned **INFORMATION PRESENT**
(best |IC| 0.334 vs null 95th pct 0.167, p = 0.002). Two follow-up
checks, run before this stage was written, show most of it is an
artifact of how the labels are built.

**Every post-event window shares one anchor price.** In all 1,002
studies `intraday_5m.price_before == d1.price_before`, and all 1,013
share it between the 5- and 60-minute windows. `ImpactEngine` sets
`before = baseline` — the last candle at or before the event — for
every post-event window regardless of resolution. So "intraday_5m" is
really *prior close → five minutes after the event*.

A shared noisy anchor mechanically creates negative correlation with
any feature ending at that price. Cancelling the anchor by differencing
two windows that share it:

| `market.return_1d` vs | IC |
|---|---|
| intraday_5m, anchor shared | −0.362 |
| 60m − 5m, anchor cancelled | **+0.087** |

**The intraday effect flips sign. It is an artifact.**

The daily reversal effect does **not** flip:

| `market.return_60d` vs | shared | anchor cancelled |
|---|---|---|
| d20 (day 5→20) | −0.284 | −0.183 |
| d10 (day 3→10) | −0.223 | −0.159 |

It survives artifact removal. It is also post-hoc, discovered in the
research region, drawn from one 52-day market episode, and measured on
heavily overlapping forward windows.

## 8. Stage 2 decision: NO confirmatory test. Protected window preserved.

The only independent test is the protected window. It cannot power
one:

| Horizon | Resolved protected labels |
|---|---|
| d20 | **0** |
| d10 | 92 |

d20 cannot be tested at all. For d10, power to detect the observed
|IC| of 0.159 is **33%** at n = 92 ignoring overlap, and roughly
**9–13%** at a realistic effective sample. 80% power needs |IC| ≥ 0.29.

Running it would most likely return inconclusive **and** permanently
spend the only clean holdout. **The protected window is not opened.**

Stage 2 candidate budget used: **0 of 4.**
