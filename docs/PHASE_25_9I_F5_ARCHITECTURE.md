# Phase 25.9I-F.5 — Signal & Horizon Intelligence Hardening

> ## NO SIGNAL IS A DECISION ONLY WHEN THE SYSTEM HAD ENOUGH INFORMATION TO MAKE THE DECISION.
>
> ## ABSENCE OF A MODEL IS NOT EVIDENCE OF ABSENCE OF EDGE.

> Read-only throughout. No threshold was changed, no model promoted, no gate
> weakened, no signal manufactured. Portfolio, Risk, Execution, capture and
> IBKR are untouched.

Written 2026-09-30.

---

## A. The problem, stated precisely

The dashboard rendered `NO SIGNAL` for almost every instrument. That string was
hiding at least six different situations:

| What actually happened | What the repair is |
|---|---|
| a qualified model looked and found no edge | nothing — that is a **decision** |
| no model exists for the horizon | train one |
| a model exists but failed its gates | improve it, or get more data |
| the data is missing | fix the data path |
| the prediction is stale | run the pipeline |
| the horizon is a sealed experiment | nothing — it is **protected** |

Only the first is a finding about the market. The rest are findings about us.

---

## B. Why the pipeline could not answer this

Audited 2026-09-30. The signal pipeline is **candidate-driven**:

```
prediction → strategy → SignalCandidate → engine → accept / suppress / reject
```

Every stage records good reasons — Phase 11's `SuppressionReason` is thorough
and is **reused** here, mapped rather than duplicated.

But an instrument with no prediction never becomes a candidate, so it never
reaches the engine and **never acquires a reason at all**. It is not suppressed;
it is absent. And absence is most of the universe: of 244 instruments, **4 have
ever produced a prediction**.

This phase inverts the traversal — it starts from the *instrument* and walks
forward, so every instrument gets a verdict whether or not the pipeline ever
touched it.

---

## C. The taxonomy, and the ordering rule

`NoSignalReason` has 19 members in four classes:

| Class | Meaning | Counts as a decision? |
|---|---|---|
| `SIGNAL` | a view was formed | **yes** |
| `VALID_NO_SIGNAL` | looked, found nothing sufficient | **yes** |
| `CAPABILITY_GAP` | could not decide | **no** |
| `PROTECTED` | sealed experiment | **no** |
| `ERROR` | the pipeline broke | **no** |

**The first blocker wins.** Reasons carry an explicit `order` and the funnel
reports the earliest one, so a reason always names something whose repair would
actually help. An instrument with neither data nor a model is `NO_DATA`, not
`NO_MODEL_AVAILABLE` — fixing the model would not have helped it.

> A pipeline error is `ERROR`, never `VALID_NO_SIGNAL`. A crash must not look
> like a considered abstention.

---

## D. The horizon inventory, from evidence

The horizon set is declared nowhere, so it is derived from
`research_labels.window_name`. Writing `("d1", "d5", "d20")` into a constant
would have been a guess, and it would have been wrong:

| Horizon | Labels | Models | Qualified | Note |
|---|---|---|---|---|
| intraday_5m | 554 | 0 | 0 | never modelled |
| intraday_15m | 554 | 0 | 0 | never modelled |
| intraday_30m | 554 | 0 | 0 | never modelled |
| intraday_60m | 554 | 0 | 0 | never modelled |
| d1 | 544 | 0 | 0 | never modelled |
| d3 | 531 | 0 | 0 | never modelled |
| **d5** | 458 | **1** | **0** | the only modelled horizon; it failed |
| d10 | 286 | 0 | 0 | never modelled |
| **d20** | **7** | 0 | 0 | **SEALED**, and label-immature |

**Signal-capable horizons: none.**

Per-instrument status is never collapsed to a company-level boolean: the same
instrument can be AVAILABLE at d1, NO_QUALIFIED_MODEL at d10 and PROTECTED at
d20, and one flag would have to lie about two of them.

---

## E. The protected horizon

d20 is sealed, and the seal is **structural rather than procedural**:

1. **No query names the label value column** — for any horizon. Counting rows
   cannot leak a return, and a query that never names the column cannot be made
   to leak one by a caller passing the wrong argument.
2. `guard_protected()` **raises** rather than returning None. A silent None
   would let a caller conclude "no edge" from a horizon it was never allowed to
   look at.
3. The contracts refuse it: a `PROTECTED` horizon status cannot carry an
   expected return, a protected horizon can never be the preferred horizon, and
   an `ExpectedReturnEstimate` cannot be constructed for it.
4. `HorizonDescriptor.can_produce_signal` is False for a sealed horizon **even
   with a qualified model**.
5. The frozen hypothesis is **not restated anywhere in this layer** — repeating
   it would put the specification somewhere it could be read or tuned.

Readiness metadata *is* exposed, because it is not an outcome: 7 of 581
observations have a matured d20 label (1.2%). A 20-day horizon needs 20 trading
days to resolve. Even unsealed, it could not support a model.

The dashboard shows d20 as **SEALED**, never as `NO SIGNAL`.

---

## F. The model inventory, and the gate

The qualification gate is `ModelEvaluation.is_deployable` (Phase 10) — **reused,
never reimplemented**. The evaluation object is reconstructed from its stored
row and *asked*, so the rule cannot drift into a second, kinder copy.

It returns three values, and the difference matters:

```
True   earned further consideration
False  failed a gate
None   cannot be judged -- no baseline comparison exists
```

**None is not False.** A model nobody compared has not lost.

### What the corpus contains

| | |
|---|---|
| trained rows | 2 |
| **distinct specifications** | **1** (`ridge_abnormal_return:v1`, refitted) |
| horizon | d5, and only d5 |
| status | `evaluated` — **never ACTIVE** |
| R² | **−0.197** (worse than predicting the mean) |
| beats all baselines | **FALSE** — loses to historical-mean *and* majority-class on MAE |
| effective sample | **4**, against a floor of 30 |

Three independent gate failures: `SAMPLE_TOO_SMALL`, `BASELINE_NOT_BEATEN`,
`MODEL_NOT_ACTIVE`. The model is correctly unpromoted.

> Rows are counted as **specifications**, not rows. Walk-forward training writes
> a new `trained_model_id` per run, and counting refits as independent models is
> the same mistake that once inflated signal confidence from 0.30 to 0.50.

---

## G. Confidence — the ~0.30 question, answered

Confidence is multiplicative (`src/signals/strategy.py`):

```
confidence = base × quality × agreement × sample
```

| Factor | Value | Why |
|---|---|---|
| base | **0.5** | the model reports no confidence; 0.5 rather than 1.0 because unknown ≠ certain |
| quality | 1.0 | data quality high |
| agreement | **0.6** | `INSUFFICIENT_EVIDENCE` — one model is not corroboration |
| sample | 0.5 or 1.0 | halved when the effective sample is small |

```
0.5 × 1.0 × 0.6 × 1.0 = 0.30   ← the historical cluster
0.5 × 1.0 × 0.6 × 0.5 = 0.15   ← the five signals in the corpus today
```

**Verified by running the canonical function.** This is **structural and
intended**, not a bug. A previous phase already found and fixed the real bug
here: counting refits of one specification as agreement lifted the multiplier to
1.0 and confidence to 0.50, clearing a 0.40 floor — *a signal made tradeable by
nothing more than having been predicted more times*.

**Nothing was inflated to produce more signals.**

---

## H. Horizon intelligence

> **AVAILABLE HORIZON ≠ PREFERRED HORIZON.**

Choosing "d5 because d5 exists" gives a position a holding period nobody can
defend. `preferred_horizon = None` is a first-class answer, and on this corpus
it is the *only* correct one.

- **Disagreement is not voted on.** If d1 says down and d5 says up, the answer is
  `DISAGREEMENT` and no preference — enforced by the contract, which *raises* if
  a preference accompanies disagreement.
- **One usable horizon is `INSUFFICIENT_EVIDENCE`**, not agreement — the same
  rule Phase 11 applies to model contributions.
- **An unmeasured confidence sorts last**, never as zero or as average.

---

## I. Targets — the only path, and the refusal

```
a QUALIFIED model → a CALIBRATED expected return → a deterministic,
uncertainty-aware transformation → a zone WITH LINEAGE
```

There is no `current_price × multiplier` path. A multiplier is a made-up number
wearing a decimal point, and the resulting zone would look exactly as
authoritative as a real one.

Refused when: the model is unqualified, uncalibrated, has no uncertainty, there
is no reference price, or the horizon is sealed. **A zone whose width was
guessed is a point estimate with decoration.**

`TargetEstimator.estimate` takes **no event parameter** — event materiality can
*request* a target but can never determine one. 25.9I-F's
`target_reestimation_required` is answered with `UNAVAILABLE` and a reason,
which is a complete answer.

**Today: zero instruments can receive a defensible target.**

---

## J. What was NOT done, deliberately

- **No new persistence.** Every output is a pure function of the corpus and
  `as_of`, so it can be recomputed. Unlike 25.9I-F's novelty ledger — which
  recorded *history* and therefore had to be stored — nothing here is a fact
  about the past. The minimum justified persistence was zero.
- **No new feature flag.** The layer is read-only; there is nothing to gate.
- **No threshold changed.** Asserted by test against the signal engine's own
  constants.
- **No model promoted.** No module here imports `src.modeling.promotion`, and
  none calls `promote`, `train` or `fit`.
- **No research run.** §22 permits it; §51 says report weakness rather than
  manufacture strength. One model on 4 effective clusters is a data problem, and
  new candidates would inherit it.

---

## K. Production boundary

| Path | Changed? |
|---|---|
| Portfolio / Risk / OrderIntent / Execution / IBKR / capture / subscriptions | **No** |
| Signal engine / model registry / qualification gates / thresholds | **No** |

No module imports `src.execution`, `src.trading` or `src.portfolio`; none
constructs an `OrderIntent`; none calls a broker or market-data method.

---

## L. Interaction with the other sub-phases

| Phase | Relationship |
|---|---|
| **25.9I-B** | the attention score is **never** treated as alpha — asserted by import test |
| **25.9I-C** | unchanged; market-data state is a separate axis from signal availability |
| **25.9I-D** | the contract for supplying expected return, confidence and uncertainty exists; **not wired** |
| **25.9I-E** | a defensible target zone would enter here, when one exists |
| **25.9I-F** | its `target_reestimation_required` and `horizon_reestimation_required` are now *answerable* — currently with a reasoned refusal |
| **25.9I-G** | the coverage funnel is the honest denominator attribution will need |
| **25.9I-H** | still blocked: zero qualified models, zero signal-capable horizons, no verified realtime |
