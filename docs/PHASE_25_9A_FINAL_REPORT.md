# Phase 25.9A — Additional Model Research & Edge Discovery Report

**Verdict** NO MODEL QUALIFIED · **Edge** WEAK, unconfirmed
**Protected window** NOT OPENED, preserved
**Pre-registration** `1938923` (Stage 1) · `99c6464` (diagnostic) · `f75b35e` (Stage 2)
**Written** 2026-09-13

---

## A. Executive summary

**The core question was whether the problem is the model or the
formulation. It is the data and the labels.**

**FACT** The pre-registered Stage 1 test said information is present,
p = 0.002.
**EVIDENCE** Most of it is manufactured by label construction.
`ImpactEngine` anchors every post-event window — including the
"5-minute" ones — to the same pre-event price. Cancelling that shared
anchor flips the strongest intraday correlation from −0.362 to +0.087.
**RESULT** The intraday "edge" is an artifact. A long-horizon reversal
effect survives artifact removal, but it cannot be confirmed with the
data that exists.
**STATUS** No model qualified. No model was even warranted.

The single most important output is not a model. It is a **label
defect**: the project's intraday targets do not measure what their names
say, and any intraday model trained on them would have learned noise
and reported it as edge.

---

## B. Why the previous models failed

Not primarily because of the algorithms.

1. **The data has no time-series depth.** Median **2** observations per
   instrument; 82 of 283 instruments have exactly one. No method learns
   an instrument's behaviour from two points.
2. **One market episode.** All 52 days sit inside one regime, including
   a sharp pullback. 25.9's direction results were decided by it.
3. **The intraday labels are mis-anchored** (§H).

---

## C–D. Dataset and target diagnosis

| | |
|---|---|
| research rows | 786 |
| instruments | 283; median 2 obs, 82 with one |
| dates | 41; median 21 obs/date, 24 with ≥20 |
| labelled per horizon | ≈780 (d20: 554) |
| feature missingness | 2.9% of cells |

**Every target labels the same ≈780 events.** Twenty targets are twenty
correlated views of one sample, not twenty datasets — which is why the
multiple-testing correction below was non-negotiable.

The data is **cross-sectional in shape**: shallow per instrument, dense
per date.

---

## E. Horizon analysis

Best absolute rank correlation per horizon, Stage 1:

| Horizon | Pooled | Cross-sectional | Best feature |
|---|---|---|---|
| d1 | −0.128 | −0.171 | rsi_14 |
| d5 | −0.156 | −0.166 | return_60d |
| d10 | −0.206 | −0.217 | return_60d |
| d20 | −0.280 | **−0.334** | return_60d |
| intraday_5m | −0.291 | −0.327 | return_1d |
| intraday_60m | −0.249 | −0.301 | return_1d |

The large intraday values are the artifact in §H. The daily pattern
grows with horizon.

---

## F. Feature information

Seven families exist: event, liquidity, market, news, sentiment,
technical, volatility. **No macro, cross-asset or explicit regime
features exist.**

**Stage 1 decision (pre-registered):** observed global-best |IC| 0.334
against a within-date permutation null 95th percentile of 0.167,
p = 0.002, 468 comparisons. **Information detected** — before accounting
for §H.

---

## H. The label defect

**FACT** Every post-event window uses one anchor.

```python
baseline = self._candle_at_or_before(view.known(), anchor)
...
before = baseline          # for every post-event window
```

**EVIDENCE** In all 1,002 studies `intraday_5m.price_before ==
d1.price_before`; all 1,013 share it between 5- and 60-minute windows.
"intraday_5m" is therefore *prior close → five minutes after the event*,
overnight gap included.

A shared noisy anchor mechanically anti-correlates any feature ending at
that price with any label starting from it. Cancelling the anchor by
differencing two windows that share it:

| `market.return_1d` vs | IC |
|---|---|
| intraday_5m, anchor shared | −0.362 |
| intraday_60m, anchor shared | −0.319 |
| **60m − 5m, anchor cancelled** | **+0.087** |

**RESULT** The sign flips. The intraday effect is **an artifact**.
Reproduced on a pure random walk with no signal in
`TestASharedAnchorManufacturesCorrelation`.

**STATUS** Not fixed here — label semantics change historical studies
and require a new `method_version` under project convention. It is the
**first required data fix** (§Y).

---

## I. Cross-sectional vs time-series

**H1**, pre-registered: cross-sectional ranking removes the common market
move, so information should be more detectable there.

| | mean |IC| |
|---|---|
| pooled | 0.065 |
| cross-sectional | 0.055 |

**H1 FALSIFIED** on average. The single strongest comparison is
cross-sectional, but the formulation shows no general advantage.

---

## M–P. The one signal that survives

Long-horizon reversal: past 60-day winners lag over following weeks.

| `return_60d` vs | shared anchor | anchor cancelled |
|---|---|---|
| d20 (day 5→20) | −0.284 | **−0.183** |
| d10 (day 3→10) | −0.223 | **−0.159** |
| d5 (day 1→5) | −0.173 | −0.079 |

It **survives** artifact removal and is **stable**: sign agrees in 5/5
weeks. Long-horizon reversal is a well-documented effect with an
economic rationale.

It is **not an edge yet**, for three reasons:

1. **Post-hoc.** Found in the research region; re-testing there is
   circular.
2. **Overlapping windows.** 15-day forward windows from nearby dates in a
   52-day dataset share most future prices. Effective sample ≪ 554.
3. **One regime.** A reversal during a single market pullback is exactly
   what a regime-specific effect looks like.

---

## V. Protected window

**NOT OPENED.** The best candidate cannot be tested; the next best
cannot be tested meaningfully.

| Horizon | Resolved protected labels |
|---|---|
| d20 | **0** — resolves late September |
| d10 | 92 |

| d10 test scenario | Power at the observed |IC| 0.159 |
|---|---|
| n = 92, ignoring overlap | 33% |
| realistic effective n | 9–13% |

80% power would need |IC| ≥ 0.29. A test would most likely return
inconclusive and permanently spend the only independent holdout.

---

## T. Multiple testing

| Phase | Looks at the research region |
|---|---|
| 25.9 | 4 candidates |
| 25.9A Stage 1 | 468 comparisons, permutation-corrected |
| 25.9A Stage 2 | **0 of 4** used |

The research region is heavily examined. Nothing positive found there
counts as confirmation.

---

## U. Negative results

| Result | Status |
|---|---|
| intraday predictability from recent returns | **FAILED** — label artifact |
| H1, cross-sectional advantage | **FAILED** — falsified |
| 25.9 C0–C3 | **FAILED** — recorded in 25.9 |
| d20/d10 reversal | **UNCONFIRMED** — cannot be tested yet |

---

## W. Best research direction

**Long-horizon cross-sectional reversal on anchor-corrected returns**,
tested on data that did not exist when it was found.

---

## X. Model qualification decision

**NO MODEL QUALIFIED.** No candidate was fit in Stage 2, because the
evidence did not warrant spending the protected window.

---

## Y. Required data improvements, in priority order

1. **Fix the intraday label anchor.** Anchor post-event windows at the
   event-time minute price, as a new `method_version` beside the old
   rows. Without this, intraday research produces false edges.
2. **Let time pass.** d20 protected labels resolve in late September.
   That alone makes the reversal hypothesis testable.
3. **More instruments per date and more dates.** Effective sample is the
   binding constraint.
4. **Macro and cross-asset features.** None exist; a reversal effect
   needs market-state context to be distinguished from a regime.

---

## Z. Next phase recommendation

Not more algorithms. **Fix the label defect, then wait for the d20
protected labels and test the one pre-specified hypothesis once.**

The 25.9 gate defect — a coin-flip model can pass the deployability
gate — remains a pending human governance decision.

---

```
PHASE 25.9A STATUS:        COMPLETE
EDGE DISCOVERED:           WEAK
TARGET QUALITY:            POOR
FEATURE INFORMATION:       WEAK
BEST FORMULATION:          d20 abnormal return, anchor-cancelled (day 5->20),
                           cross-sectional rank on market.return_60d, negative
BEST MODEL FAMILY:         NONE
BASELINE COMPARISON:       INCONCLUSIVE
OUT-OF-SAMPLE:             INCONCLUSIVE
ROBUSTNESS:                INCONCLUSIVE
CALIBRATION:               N/A
ECONOMIC SIGNIFICANCE:     INSUFFICIENT DATA
MODEL QUALIFIED:           NO
CONFIDENCE POLICY:         UNCHANGED
MODEL GOVERNANCE:          PASS
PAPER TRADING:             NOT ATTEMPTED
REAL IBKR ORDER:           NOT ATTEMPTED
PHASE 25.95:               NOT READY
PHASE 26:                  NOT READY
NEXT REQUIRED STEP:        Fix the intraday label anchor as a new method_version,
                           then test the pre-specified d20 reversal hypothesis once
                           on the protected window after its labels resolve.
```
