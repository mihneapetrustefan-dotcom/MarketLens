# Phase 25.9I-F.6 — Model Qualification & Multi-Horizon Research

> ## A MODEL IS NOT QUALIFIED BECAUSE WE NEED ONE.
>
> ## FAILURE TO FIND EDGE IS A VALID RESEARCH RESULT.

> Read-only throughout. No threshold moved, no gate weakened, no model
> promoted, no holdout opened. Portfolio, Risk, Execution, capture and IBKR are
> untouched. D20 remains sealed.

Written 2026-09-30. Pre-registration: `docs/PHASE_25_9I_F6_PREREGISTRATION.md`,
committed at `ed016e8` **before** the harness was run against any horizon.

---

## A. Why F.6 exists

Phase 25.9I-F.5 proved that all 244 instruments produce `NO SIGNAL` for one
reason — `model_not_qualified` — and that the single existing model
specification failed on an **effective sample of 4** against a floor of 30.

F.5 correctly reported what it found. It did not establish *what the 4 was*.
Two readings were possible and they lead to opposite work:

| Reading | What it would mean | What you would do |
|---|---|---|
| the corpus is about 4 companies | the evidence base is nearly empty | collect data for months |
| 4 is an artefact of one stored evaluation | the corpus may be fine | find the real constraint |

F.6 exists to decide between them with evidence, and then to research every
horizon that the evidence says can carry a fair test.

The answer turned out to be the second reading — and the real constraint was
somewhere neither reading had looked.

---

## B. Effective sample versus row count

The repository already had the right definition; nothing here invents one.

```
ModelEvaluation.effective_sample_size  =  cluster_count or sample_size
ModelEvaluation.MIN_EFFECTIVE_SAMPLE   =  30
```

`src/adaptive/sample_adequacy.py` **aliases** that constant rather than
restating it:

```python
MIN_EFFECTIVE_SAMPLE = ModelEvaluation.MIN_EFFECTIVE_SAMPLE
```

A test forbids any numeric literal being assigned to that name in the module, so
the floor cannot drift out of sync with the canonical gate.

### A cluster is an instrument

Measured, not assumed: `event_cluster_id` equals `instrument_id` for **every**
row of `research_observations` (0 mismatches). So the effective sample answers

> *how many distinct companies is this evidence about?*

which is the question that matters, because 300 observations of one company
under one regime are one observation repeated.

### The property everything rests on

`effective_sample_of()` counts **distinct non-blank clusters**. The first test in
`tests/adaptive/test_sample_adequacy.py` asserts that duplicating a cluster 50×
cannot raise it. If that were ever false, every qualification decision
downstream would be built on a number inflatable by copying rows.

`SampleAdequacy` also exposes `row_inflation_ratio` (rows ÷ clusters) so a
narrow corpus wearing a large row count is visible at a glance, and it **refuses
to construct** with more clusters than rows.

### What legitimately increases the sample

| Does increase it | Does not |
|---|---|
| observations about a new instrument | more rows about instruments already present |
| forward labels maturing on new instruments | re-running the same split |
| a genuinely wider universe | lowering the floor |

---

## C. Root cause of `effective sample = 4`

Evidence, read-only, from `data/marketlens.db`:

| Artefact | Value |
|---|---|
| `trained_models.train_cluster_count` | **4** |
| `model_evaluations.sample_size` | **5** |
| `model_evaluations.cluster_count` | **4** |
| `model_evaluations.effective_sample_size` | **4** |
| `evaluated_at` | **2026-08-29T10:27:44Z** |

The 4 is a property of **one test window of one evaluation taken on
2026-08-29**: five test rows spanning four instruments. It was never a property
of the corpus. F.5's model inventory read the stored evaluation — correctly —
and reported the number it found.

Re-running the identical split today yields **32 test rows spanning 27
instruments**, because forward labels matured as the calendar advanced. Nothing
was added to the corpus.

The corpus itself holds **244 distinct clusters** and, per daily horizon,
**159–236**, every one of them above the floor of 30.

**Design consequence:** a stored evaluation sample is historical evidence about
that evaluation. It is not the current sample capacity of the dataset. F.6
recomputes capacity from canonical current evidence and keeps the two clearly
distinct.

---

## D. Researchable-horizon rules

`assess_horizon()` reads rows, clusters, sessions and dates per horizon — never
a label *value* — and returns one `ResearchReadiness`:

| Readiness | Rule |
|---|---|
| `PROTECTED` | horizon is sealed (D20). No counts are even collected. |
| `INSUFFICIENT_EFFECTIVE_SAMPLE` | research-region clusters < 30 |
| `DATA_ACCUMULATION_REQUIRED` | intraday horizon with < 30 distinct sessions |
| `RESEARCH_READY` | everything above satisfied |

Two rules deserve naming:

**The holdout is not borrowable.** A horizon with 100 clusters overall but 5 in
the research region is `INSUFFICIENT_EFFECTIVE_SAMPLE`. Clusters that live only
behind the holdout boundary cannot help a candidate pass; a test asserts this.

**Sessions, not bars, for intraday.** 554 rows across **29** distinct sessions
are 29 independent days viewed many times. The floor is 30 sessions. The
shortfall is one session and is reported as one session rather than rounded
away — all four intraday horizons are `DATA_ACCUMULATION_REQUIRED`.

`RESEARCH_READY` promises **a fair test, not an edge**. The judgement carries
that sentence in its own reasons, and a test asserts it is there.

### Measured readiness, 2026-09-30

| Horizon | Rows | Clusters | Sessions | Research-region clusters | Readiness |
|---|---|---|---|---|---|
| intraday_5m/15m/30m/60m | 554 | 237 | 29 | 179 | DATA_ACCUMULATION_REQUIRED |
| **d1** | 544 | **236** | 27 | 178 | **RESEARCH_READY** |
| **d3** | 531 | **236** | 25 | 178 | **RESEARCH_READY** |
| **d5** | 458 | **223** | 23 | 178 | **RESEARCH_READY** |
| **d10** | 286 | **159** | 17 | 157 | **RESEARCH_READY** |
| d20 | — | — | — | — | **PROTECTED** |

---

## E. Research splits, purge and embargo

`scripts/research_multi_horizon.py` imports `src/modeling/splits.purge` and
`.embargo`. It does not reimplement them.

| Parameter | Value | Source |
|---|---|---|
| purge horizon | label's **calendar** span: d1→3, d3→5, d5→7, d10→14 days | pre-reg §7 |
| embargo | 6 days | Phase 25.9 |
| test window | 7 days | Phase 25.9 |
| step | 7 days | Phase 25.9 |
| min train rows | 30 | Phase 25.9 |
| min test rows | 20 | Phase 25.9 |
| min valid folds | **3** | pre-reg §6.4 |

Purge is expressed in calendar days so a weekend cannot smuggle a label across a
fold boundary. Each purge is ≥ the trading-day span it protects; over-purging
can only remove rows, never admit leakage.

Folds are **expanding** and day-granular, strictly inside the research region.

### The span arithmetic — and why it decided this phase

`span_requirement()` computes, per horizon, the calendar days a legal fold design
needs and compares it with what exists:

```
days_for_first_fold   = purge + embargo + test
days_for_three_folds  = purge + embargo + test + 2 × step
```

| Horizon | purge | first fold needs | 3 folds need | region has | short by |
|---|---|---|---|---|---|
| d1 | 3 | 16 | 30 | **15** | **15** |
| d3 | 5 | 18 | 32 | **15** | **17** |
| d5 | 7 | 20 | 34 | **15** | **19** |
| d10 | 14 | 27 | 41 | **15** | **26** |

The research region — everything before the frozen holdout instant — spans
**15 days**. Not one horizon can form even its *first* legal fold.

**The binding constraint was never sample size. It is calendar span.** The
diagnostic says so in the output itself, together with the sentence that reaching
it "buys a fair test, not an edge".

---

## F. Holdout discipline

The boundary is `2026-08-15T01:23:31Z`, exactly where Phase 25.9 put it.

Phase 25.9 derived it as the last 20% of a **972-row** corpus. That corpus was
rebuilt on 2026-08-28 and now holds **581** rows, so the same instant sits at
**60.6%** — a 39% holdout rather than a 20% one, which is why the research region
is only 15 days wide.

**It was not recomputed.** Recomputing a boundary *after* discovering the old one
is inconvenient is holdout-shopping, and it would have been holdout-shopping
precisely because the new value would have been chosen knowing it produced folds.
A larger holdout is more conservative, not less.

The Phase 25.9 rule carries over unchanged: **if no candidate survives the
research region, the holdout is not opened at all.** It was not opened. The
harness reports `holdout_opened: false` and every fold it would build lives
strictly before the boundary.

---

## G. Baseline competition

Untouched and reused. `ModelingEngine.train_and_evaluate` supplies the mandatory
baselines, the primary metric and the deployability rule.

A candidate must beat **every** mandatory baseline out-of-sample, and must do so
in a **strict majority of folds**. A high mean carried by one lucky window does
not qualify. Probabilistic candidates must also beat the base-rate Brier score.

What explicitly does **not** count: positive R², accuracy above 50%, beating the
previous model, one winning window, or "close".

The existing model is the cautionary example: MAE 0.039314 against
`baseline_historical_mean` 0.037681 and `baseline_majority_class` 0.037999 — it
**loses to both**, and R² is **−0.197**. Directional accuracy of 0.6 on five rows
is noise, and the gate treats it as such.

### `None` is not `False`

`ModelEvaluation.is_deployable` returns `True`, `False`, or **`None`** when it
cannot judge. `model_inventory.py` maps that third state to a distinct
`QualificationVerdict.UNJUDGED` rather than folding it into `FAILED`. During
development a field-name error made baseline comparisons come back empty, and the
gate answered `UNJUDGED` where the truth was `FAILED` — a *softer* verdict than
reality. Collapsing `None` into `False` would have hidden that; keeping it
separate exposed it.

---

## H. Model-family selection, feature sets, ablations

Fixed in advance, reused verbatim from Phase 25.9 via
`scripts/research_model_quality.spec_for`:

| | Family | Task | Features |
|---|---|---|---|
| C0 | ridge (α=1) | abnormal return | all numeric |
| C1 | logistic | direction | all numeric |
| C2 | ridge (α=10) | abnormal return | all numeric |
| C3 | logistic | direction | dense only |

**Budget: 4 candidates × 4 horizons = 16 cells, and it does not grow.** No
hyperparameter sweep, no extra families, no seed exploration, no branch in the
plan that responds to a result.

The only feature ablation is the pre-registered C1-versus-C3 contrast (all
numeric versus dense only). No ablation was added after the fact.

**Independence:** C0 and C2 are one family at two regularisation strengths and are
**not** independent evidence of each other. Neither are C1 and C3. Nothing in
this phase may count them as model agreement.

**No candidate was selected, tuned or ordered using any D20 outcome.** The four
specifications predate F.6 entirely.

---

## I. Multi-horizon research

Phase 25.9's harness is hard-wired to `d5.abnormal_return` with a single purge
horizon. F.6 runs the *identical* procedure across four daily horizons, each with
its own labels, its own calendar-span purge and its own independent verdict.

**Horizons are never pooled.** Pooling d1 and d10 rows would present the same
instrument's overlapping forward windows as independent observations.

d20 is not merely excluded — `research_multi_horizon.py` raises
`ProtectedHorizonRequested` before any query runs if pointed at it, and
`horizon_inventory.guard_protected()` enforces the same boundary upstream.

---

## J. Robustness, regime conditionality, calibration, uncertainty, expected return

These are the parts of the design that **were never exercised**, and saying so
plainly is more useful than describing machinery that produced nothing.

| Capability | Design | Status after F.6 |
|---|---|---|
| fold-majority robustness | strict majority of ≥ 3 folds | **not reached** — 0 folds formed |
| regime conditionality | not in scope for F.6 | deferred |
| calibration (Brier vs base rate) | required for probabilistic candidates | **not reached** |
| uncertainty | would come from fold dispersion | **nothing measurable** |
| expected return | requires a qualified model | **0 candidates can provide one** |

Zero folds means zero candidates were fitted, so there is no calibration curve,
no fold dispersion, no interval and no defensible expected return. Reporting any
of these would be fabrication. The relevant architectural property is that the
harness **refused to proceed** rather than relaxing a fold requirement to produce
numbers.

---

## K. Target and horizon estimation

Unchanged from F.5 and deliberately so.

`src/adaptive/target_estimation.py` yields a target zone only when a
`TargetEstimate` is `AVAILABLE`, which requires a qualified model. With zero
qualified models, **0 of 244 instruments can receive a defensible target zone**,
and the contract refuses to emit one rather than guessing.

`horizon_intelligence.py` returns a preferred horizon or `None`; it raises if a
preferred horizon is set while horizons disagree. With no qualified model on any
horizon, every instrument has **no usable horizon** and `pref=none` — which is
exactly what the F.5 diagnostic prints.

F.6 did not invent a target, a horizon or an exit fraction. It could not: there
is no code path from this phase to those fields.

---

## L. Qualification versus promotion

Two different acts, and F.6 performs only the first — and in the event, not even
that.

| | Qualification | Promotion |
|---|---|---|
| decided by | the research gate | human-governed canonical action |
| output | QUALIFIED RESEARCH CANDIDATE | `PRODUCTION ACTIVE` model |
| in F.6 | attempted, nothing qualified | **no code path exists** |

`models_promoted = 0`, and the harness contains no promotion call.
`MIN_EFFECTIVE_SAMPLE` (30), `min_signal_confidence` (0.40), the deployability
gate, the promotion gate, the baselines and the confidence formula were all left
exactly as they were.

---

## M. Challenger preservation and negative knowledge

The Phase 25.9 d5 negative result is preserved, not overwritten. The two stored
evaluations from 2026-08-29 remain in `model_evaluations` as historical evidence
of what was true then, with their stale sample of 4 intact — that record is
precisely how the root cause in §C was proved.

Negative knowledge F.6 adds, so no future phase repeats it blindly:

1. **d1, d3, d5, d10 cannot be researched against this corpus at this holdout
   boundary.** Not because the candidates are weak — because no legal fold exists.
2. **The constraint is calendar span, not cluster count.** Collecting more rows
   about the same 29-day window will not help. Shortfalls: 15/17/19/26 days.
3. **Intraday is one session short of its floor**, not far short.
4. **The stored `effective_sample = 4` is not a corpus property** and must never
   again be read as one.
5. **The 2026-08-28 rebuild shortened the research region**, moving the holdout
   from 20% to 60.6% of the corpus. Rebuilding a corpus under a frozen boundary
   has research consequences.

---

## N. Stopping rules

Pre-registered, and each one fired or was available to fire:

- all four candidates evaluated for a horizon (budget 16 cells total);
- **fewer than 3 valid folds ⇒ INSUFFICIENT DATA** — this is the one that fired,
  on all four horizons;
- research-region effective sample below 30.

No candidate is retried with different hyperparameters after failing.

The phase was **willing to finish with nothing**, and did. "INSUFFICIENT DATA" is
reported as a conclusion, not as a problem to be engineered around.

---

## O. D20 isolation

| | |
|---|---|
| executed | **NO** |
| inspected | **NO** |
| inferred | **NO** |
| consumed | **NO** |
| trained on | **NO** |
| used for feature/family/hyperparameter/threshold/target/horizon selection | **NO** |
| hypothesis changed | **NO** |
| anchor-v2 changed | **NO** |
| protected window changed | **NO** |

Enforced structurally, not by intention:

- `horizon_inventory.PROTECTED_HORIZONS` names the horizon and **does not restate
  the hypothesis**. During development my own docstring restated it; my own
  boundary test caught that and it was removed.
- `guard_protected()` raises; `research_multi_horizon.py` raises
  `ProtectedHorizonRequested` before any query.
- `sample_adequacy.py` contains **no reference to `value_json`** — a test asserts
  the string is absent, so the module cannot read a label value at all, protected
  or otherwise.
- D20 appears in inventories as `PROTECTED` with zero counts, and nowhere else.

---

## P. Production boundary

Nothing in F.6 can reach a broker. The phase added one module and one script,
both read-only, and neither imports an execution path.

| | |
|---|---|
| broker writes attempted / succeeded | 0 / 0 |
| paper orders / live orders | 0 / 0 |
| Portfolio / Risk / OrderIntent / Execution | unchanged |
| capture behaviour / IBKR / subscriptions | unchanged |
| production model | unchanged |
| databases | opened `mode=ro` throughout |

Existing Portfolio remains authoritative, existing Risk remains the hard gate,
and Execution remains the only path toward IBKR.

---

## Q. Readiness criteria for I-G

I-G is shadow validation. Its precondition is something to validate.

| Requirement | State |
|---|---|
| ≥ 1 qualified research candidate | **0** |
| a defensible expected return | **none** |
| a defensible target zone | **0 of 244 instruments** |
| a usable preferred horizon | **none** |
| ≥ 3 valid walk-forward folds | **0** |

**I-G readiness = NO.** Not because the pipeline is broken — it refused
correctly — but because shadow validation would have nothing to shadow.

The unblocking work is not modelling. It is a research region wide enough to hold
a legal fold design, which means recovering the historical information set that
the 2026-08-28 rebuild left behind: canonical events start 2026-07-30 while
16,737 articles predate that date. That is upstream data integrity, and it is
explicitly **not** this phase.

---

## R. What this phase is not

It is not evidence that no edge exists. Zero folds means **the question was never
asked**, on any horizon. An absent test is not a negative result about the market;
it is a negative result about us.

> **RESEARCH_READY means a fair test is possible. It never meant an edge is there.**
