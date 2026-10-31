# Phase 25.9B — Label Anchor Correction & Protected Hypothesis Validation

**Label anchor** FIXED as `anchor-v2` · **Protected test** NOT EXECUTED — window NOT READY
**Evaluation** INSUFFICIENT DATA · **Written** 2026-09-13

| Commitment | Commit | Time |
|---|---|---|
| anchor-v2 + frozen hypothesis | `754a7bd` | 13:35:54 |
| label builder + one-shot harness | `f684d76` | 13:39:39 |
| first protected-facing run | — | after both |

---

## A. Executive summary

**Objective 1 — done.** The label defect is reproduced exactly, its root
cause identified, and a corrected method built as a separate version.
v1 still reproduces every stored value.

**Objective 2 — correctly not run.** d20 resolves for **0 of 253**
protected observations: the forward prices for most of those events do
not yet exist. Per §9 the test stopped at its readiness gate, computed
no statistic, and left the window closed.

**The hypothesis survives the fix** in non-protected data (IC −0.189 on
corrected abnormal returns). That is robustness evidence, not
confirmation: the effect was discovered in that same region.

---

## B. The original bug

**FACT** The root cause is in how candles reach the engine.

`build_event_studies.load_candles` merges daily and minute candles into
one list, and `Candle` has **no resolution field** — the `interval`
column is read and discarded. Daily candles are timestamped at the
session **date** (04:00 UTC), not at the close.

So "latest candle at or before the event" picks whichever timestamp
sorts last. That produced two defects.

**Defect 1 — a stale, mixed-resolution base.** Every post-event window
ignores its own computed `start` and uses one pre-event price.

**Defect 2 — intraday windows snap across closures.**
`_candle_at_or_after(end)` has no upper bound.

**EVIDENCE** Study `es-86b69ecaf738ef05`, `us_and_intl-ge`:

| | Value |
|---|---|
| anchor | **Saturday** 2026-08-01 10:02 UTC |
| v1 base, every window | 355.94 = Friday 08:03 UTC **pre-market minute print** |
| Friday's actual close | 360.07 |
| intraday_5m after | 368.93 |
| intraday_60m after | **368.93** — identical |

A "5-minute return" spanned three days, and a daily window was measured
from a 4 a.m. ET trade.

**RESULT** 25.9A described this as "anchored to the prior daily close".
That was imprecise: the base was a pre-market minute print, and the
snapping was a second, separate defect.

---

## C. Corrected anchor semantics

Implemented in `src/impact/anchoring.py`. v2 takes minute and daily
candles **separately**, so the v1 confusion cannot occur.

| Window unit | before | after | otherwise |
|---|---|---|---|
| MINUTES | minute price ≤ 5 min before start, same date | minute price ≤ 5 min after end, same date | MISSING, named reason |
| TRADING_DAYS | last daily close from a **strictly earlier date** | daily close of the session `window_bounds` selects | MISSING, named reason |

```
ANCHOR     market_visibility_latest of the event
CUTOFF     information known at the anchor
BASE       daily: close of the last session dated before the anchor
           intraday: minute price at the anchor, within 5 min
START      as base
END        daily: Nth session at or after the anchor (unchanged)
           intraday: minute price at anchor + N min, within 5 min
```

**Trading days, not calendar days.** `window_bounds` already walks the
real session list, so weekends and holidays are handled by the data
rather than by arithmetic. §5 passed before any change. Early closes
are not modelled: no venue calendar exists that knows them.

**Point-in-time.** A daily base is only ever a close from a strictly
earlier calendar date, so it had necessarily happened by the event —
without inventing session close times.

**Not changed — deliberately.** The window **end**, and the abnormal
return formula. v2 is an anchor fix, not a target redefinition.

**Left open, and documented.** Whether a daily window should start from
the prior close or from the first session after the event. That changes
what "d20" measures, which is a definition decision.

---

## D. Method version

| | v1 | v2 |
|---|---|---|
| name | `anchor-v1` | `anchor-v2` |
| used by | `ImpactEngine`, all of `event_study_returns` | `build_anchor_v2_labels.py` |
| label names | `{window}.abnormal_return` | `{window}.abnormal_return.anchor-v2` |
| label_version | v1 | v2 |
| modified here | **no** | new |

**Why distinct names:** `research_labels` keys on
`(observation_id, name)` and `event_study_returns` on
`(study_id, window_name)`. Neither includes a version, so a same-name v2
rebuild would silently overwrite v1.

**v1 reproducibility, verified against production:**

| Window | Stored | `resolve_v1` | `resolve_v2` |
|---|---|---|---|
| intraday_5m | 355.94 → 368.93 | exact | MISSING |
| intraday_60m | 355.94 → 368.93 | exact | MISSING |
| d1 | 355.94 → 377.28 | exact | **360.07** → 377.28 |
| d5 | 355.94 → 366.70 | exact | **360.07** → 366.70 |
| d20 | 355.94 → 335.71 | exact | **360.07** → 335.71 |

---

## E. Label resolution under v2

Every observation accounted for. Nothing zero-filled or dropped.

| Window | Research | Protected |
|---|---|---|
| intraday_5m | 121 / 786 | 96 / 253 |
| d5 | 771 / 786 | 193 / 253 |
| d10 | 741 / 786 | 91 / 253 |
| **d20** | 563 / 786 | **0 / 253** |

**The scale of the intraday defect, measured:** 641 of 786 research
intraday labels (82%) are events outside any observable session. Under
v1, every one was measured across a closure and reported as a
short-horizon return.

Source daily data ends **2026-09-05**.

---

## F. Point-in-time validation

| Check | Result |
|---|---|
| daily base strictly before the event date | asserted by test |
| intraday base within 5 min, same date | asserted |
| no window snaps across a closure | asserted |
| window end from the real session list | asserted |
| unresolvable horizon named, not guessed | asserted |

**Status: PASS.**

---

## G–I. Protected window, frozen hypothesis, frozen baseline

Frozen in `754a7bd`, before any v2 protected statistic.

| | |
|---|---|
| window | 2026-08-15T01:23:31 → 2026-08-27T17:09:32 |
| feature | `market.return_60d` |
| relation | **negative** |
| target | d20 minus d5 abnormal return, anchor-v2 |
| formulation | cross-sectional Spearman, mean across dates |
| baseline | zero edge, within-date permutation, 2,000 draws, seed 20260913 |
| sample | ≥100 rows, ≥8 dates, MDE ≤ 0.20 |

**Disclosed discrepancy.** 25.9A's stated formulation was *abnormal*;
its exploratory check used *raw*. The stated abnormal definition was
frozen, per the no-drift rule.

---

## J–K. The protected test

**NOT EXECUTED.** The readiness gate stopped it.

| Requirement | Required | Actual |
|---|---|---|
| resolved rows | ≥ 100 | **0** |
| eligible dates | ≥ 8 | **0** |
| min detectable \|IC\| | ≤ 0.20 | 1.00 |

No statistic computed. Window closed. Persisted as
`exp-d20-reversal-anchor-v2-protected`, status `insufficient_data`,
`opened: false`, fingerprint `2d4d4bb1534b3e238a26ee51`, code
`f684d76`.

**Why:** protected events need twenty trading days of forward prices —
through roughly **2026-09-25** for the latest. Today is 2026-09-13.
Those prices do not exist yet.

**Result: INSUFFICIENT DATA.** Not weak support. Not a failure.

---

## L. Economic check

Only available from non-protected data, so non-confirmatory:

| | |
|---|---|
| top vs bottom tercile of `return_60d`, abnormal, days 5→20 | **−4.6%** |

A 4.6-point spread over roughly fifteen sessions would comfortably
exceed round-trip costs for liquid US equities — **if** it held out of
sample. It has not been tested out of sample.

**Status: INSUFFICIENT DATA.**

---

## M. Robustness from non-protected data

The frozen statistic, research region, anchor-v2:

| | |
|---|---|
| rows / eligible dates | 558 / 22 |
| mean cross-sectional IC | **−0.189** |
| one-sided p | 0.0005 |

**What it shows:** the reversal is **not** an artifact of the v1 anchor
defect. On corrected abnormal labels it lands almost exactly on 25.9A's
−0.183 on raw.

**What it does not show:**

1. **Confirmation.** It was discovered in this region.
2. **That p-value.** The null treats 22 dates as independent while
   their 15-session forward windows overlap heavily. True significance
   is weaker than printed.
3. **Generality.** One market episode.

---

## P. Tests

| Suite | Tests |
|---|---|
| `tests/impact/test_anchoring.py` | 13 |
| `tests/scripts/test_validate_d20_reversal.py` | 15 |

Includes: v1 reproduces the defects; v2 corrects them without moving the
window end; off-session intraday refused; trading-day semantics;
point-in-time base; readiness gate; a final verdict locks the window;
an unready attempt does not; the wrong sign is a failure; no
weak-support category; the frozen spec cannot drift.

---

## Q. Security and trading safety

No order, paper trade, IBKR call, shadow or live execution. No model
trained or promoted. Governance files unchanged. `ImpactEngine` and
`build_event_studies` unmodified. Live remains disabled.

---

## R. Remaining limitations

1. **Protected test pending** until forward prices exist.
2. **The price cache must be refreshed** after late September;
   `cache_price_candles.py` fetches post-event windows, so a normal
   pipeline run covers it.
3. **Overlap in the null.** The frozen within-date permutation
   understates dependence across dates. It is frozen and will not be
   changed before the test; it is disclosed now.
4. **Daily window start is an open definition decision.**
5. **Production still uses v1.** Existing studies, labels and every
   intraday model input remain v1 until a deliberate migration.

---

## S. Next research recommendation

1. **After 2026-09-28:** run the pipeline so the price cache covers the
   protected forward windows.
2. **Rebuild anchor-v2 labels** on a fresh working copy.
3. **Run `validate_d20_reversal.py` exactly once.** The hypothesis,
   baseline and harness are already frozen and committed.

**Do not train a model before that result.** If it is NOT SUPPORTED,
the reversal is recorded as a failed hypothesis and research moves on.

---

```
PHASE 25.9B STATUS:        COMPLETE
LABEL ANCHOR:              FIXED
METHOD VERSION:            anchor-v2
LABEL RESOLUTION:          PARTIAL
PROTECTED WINDOW:          NOT READY
HYPOTHESIS:                D20 REVERSAL
FEATURE:                   market.return_60d
RELATION:                  NEGATIVE
EVALUATION:                INSUFFICIENT DATA
BASELINE:                  INCONCLUSIVE
ECONOMIC SIGNIFICANCE:     INSUFFICIENT DATA
LEAKAGE:                   PASS
PROTECTED TEST:            NOT EXECUTED
MODEL QUALIFIED:           NO - THIS PHASE DOES NOT QUALIFY A MODEL
PAPER TRADING:             NOT ATTEMPTED
REAL IBKR ORDER:           NOT ATTEMPTED
PHASE 25.95:               NOT READY
PHASE 26:                  NOT READY
NEXT REQUIRED STEP:        After 2026-09-28, refresh the price cache, rebuild
                           anchor-v2 labels on a working copy, and run
                           scripts/validate_d20_reversal.py exactly once.
```
