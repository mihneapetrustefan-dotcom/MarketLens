# Phase 25.9I-F.6B — Frozen Multi-Horizon Model Research & Qualification

> ## THE EXPERIMENT IS DEFINED BEFORE THE RESULT IS KNOWN.
>
> ## NO MODEL QUALIFYING IS A VALID RESULT.

> No threshold moved, no gate weakened, no model promoted, no order placed, no
> billable market-data request made. D20 remains sealed. The frozen dataset was
> read `mode=ro` and the production database was not opened for writing at all.

Written 2026-10-04. Completion report: `docs/PHASE_25_9I_F6B_REPORT.md`.
Frozen plan: `docs/PHASE_25_9I_F6B_PREREGISTRATION.md` and
`docs/PHASE_25_9I_F6B_HANDOFF.json`, both produced by Phase 25.9I-F.7 at
`209da23` **before** any candidate existed.

---

## A. Why F.6B exists, and what it is allowed to decide

Phase 25.9I-F.6 asked the modelling question and could not answer it: the
research region was 15 calendar days and a single legal walk-forward fold needed
16 to 27. **Zero folds formed on all four daily horizons.** Its result —
`NONE QUALIFIED / INSUFFICIENT DATA` — stands exactly as recorded and is not
reinterpreted here.

Phase 25.9I-F.7 found the cause upstream (an entity-resolution publication
floor), replayed the existing pipeline without it, and froze a **new dataset
version** plus a **new research environment**. It deliberately did not run any
model.

F.6B runs that frozen experiment **once**. It may conclude that nothing
qualifies, and that conclusion is as much a result as any other.

What F.6B is **not** allowed to decide: the holdout boundary, the purge and
embargo policy, the eligible horizons, the candidate budget, the baselines, the
metrics, or the qualification gate. Every one of those was fixed by F.7 and is
read from the handoff at runtime rather than restated in code.

---

## B. The frozen F.7 handoff, verified not assumed

`preflight()` reads `docs/PHASE_25_9I_F6B_HANDOFF.json` and checks it against
the artifacts it names. Any disagreement raises `FrozenPlanViolation` and the
run stops. There is no warning-and-continue path.

| | Authoritative value |
|---|---|
| F.7 final commit | `209da23` |
| corpus version | **`canonical_corpus_v2`** |
| corpus hash | `4247f543904204c9d60f86b9ab6858e3bd3bd9fcc635e5950c5b60e148cb2ddb` |
| research snapshot | **`research_snapshot_v2`**, state `ELIGIBLE_FOR_RESEARCH` |
| snapshot hash | `4ccab384d37a9eb249ce2bd61b44692c77a0df3dc7121dc79c3ed6940e66b0bc` |
| feature / label / universe / event version | `v1` each (the composite reads `v1\|v1\|…`) |
| as-of cutoff | `2026-08-30T14:35:00Z` |
| pre-registration | `docs/PHASE_25_9I_F6B_PREREGISTRATION.md` |
| pre-registration sha256 | `9150b988993dcac3789b99140eaec782df60e816c8fa77fd25be0d4f18087625` |
| eligible horizons | **d1, d3, d5, d10** |
| deferred horizons | intraday_5m / 15m / 30m / 60m |
| **holdout boundary** | **`2026-08-18T00:00:00Z`** |
| holdout rule | final 20% of the information span, floored to the UTC day — Phase 25.9's fraction |
| purge (calendar days) | d1→3, d3→5, d5→7, d10→14 |
| embargo | 6 days |
| test window / step | 7 / 7 days |
| minimum valid folds | 3 |
| candidate budget | **16 cells** |
| qualification gate | `src.domain.model_models.ModelEvaluation.is_deployable` |
| seed policy | none — every candidate is deterministic (closed-form ridge, deterministic logistic fit); no seed is drawn, so no seed can be searched |
| D20 | **excluded** |

### Checks that must all pass before anything is fitted

1. the pre-registration file still hashes to the value the handoff records;
2. the snapshot is registered, and its content still hashes to its frozen value;
3. the snapshot state is `ELIGIBLE_FOR_RESEARCH`;
4. **the holdout rule, re-applied to the snapshot, still yields the frozen
   boundary** — recorded is not enough, it has to be reproducible;
5. the handoff does not record that an outcome was observed before the boundary
   was chosen;
6. the protected horizon is excluded, and no eligible horizon is protected;
7. every eligible horizon has a declared purge;
8. **no COMPLETE execution already exists** for this (pre-registration, snapshot)
   pair.

The eighth is the one that protects the science. Running a frozen experiment
twice and keeping the better outcome would not be independent evidence, so
`--run` refuses and `--show` reports what was already found.

---

## C. Dataset identity on every record

No candidate without lineage. Each horizon record and each persisted candidate
row carries the corpus version, snapshot id, snapshot hash, feature version,
as-of cutoff, pre-registration hash, execution id and code commit. The
`research_candidate_results` table stores those columns per cell, so a future
phase can tell which dataset a failure belongs to without reading a document.

---

## D. The candidate budget, enumerated before execution

```
TOTAL ALLOWED RESEARCH CELLS = 16
```

| Cell | Horizon | Family | Task | Features | Purge |
|---|---|---|---|---|---|
| d1:C0 · d3:C0 · d5:C0 · d10:C0 | each | ridge (α=1) | abnormal return | all numeric | 3/5/7/14 |
| d1:C1 · d3:C1 · d5:C1 · d10:C1 | each | logistic | direction | all numeric | 3/5/7/14 |
| d1:C2 · d3:C2 · d5:C2 · d10:C2 | each | ridge (α=10) | abnormal return | all numeric | 3/5/7/14 |
| d1:C3 · d3:C3 · d5:C3 · d10:C3 | each | logistic | direction | dense only | 3/5/7/14 |

`enumerate_cells()` builds this list from the frozen plan and **raises** if its
length differs from the budget. Adding a horizon or a candidate therefore breaks
a check rather than quietly producing cell 17 — two tests exercise exactly that.

The four specifications are Phase 25.9's, imported verbatim from
`research_model_quality.spec_for`. They predate F.6, F.7 and this phase, which is
why they cannot have been chosen to suit this dataset.

**The budget did not grow with the corpus.** F.6 had 16 cells against 581
observations; F.6B has 16 against 1,056. A larger dataset is not a licence for a
wider search.

---

## E. Independence of model evidence

| | |
|---|---|
| C0 vs C2 | **one family**, two regularisation strengths — not independent |
| C1 vs C3 | **one family**, two feature sets — not independent |
| distinct families across the whole budget | **two** |

A refit is not a second model, and a seed is not a specification. Nothing in this
phase may count C0 and C2 as model agreement, and a test asserts the family
identity so the claim cannot drift. If exactly one specification qualifies,
confidence must not be inflated by pretending it was corroborated.

---

## F. Fold design, and one implementation of it

Folds are expanding, day-granular, and live strictly inside the research region.

F.6's `build_folds` had the right mechanics and the wrong boundary: the holdout
instant was a module constant. Rather than copy it, F.6B **parameterised it** —
`build_folds(cutoffs, purge_days, *, holdout_start=..., embargo_days=..., ...)`
with defaults equal to F.6's constants, so every existing caller behaves exactly
as before and there is still only one fold builder.

That refactor is behaviour-preserving and was verified by re-running F.6 on
snapshot v1: `folds_valid = 0` and `INSUFFICIENT DATA` on all four horizons,
holdout not opened, unchanged.

### Measured fold design on snapshot v2

| Horizon | Rows | Clusters | Sessions | Research rows | Research clusters | Holdout rows | Folds built | **Folds valid** |
|---|---|---|---|---|---|---|---|---|
| d1 | 891 | 237 | 55 | 737 | 227 | 154 | 5 | **5** |
| d3 | 878 | 237 | 53 | 735 | 227 | 143 | 5 | **5** |
| d5 | 805 | 234 | 51 | 735 | 227 | 70 | 4 | **4** |
| d10 | 633 | 211 | 45 | 633 | 211 | **0** | 3 | **3** |

Horizons are never pooled to create folds, no fold was dropped for being
difficult, and no test window was shortened. All pre-registered folds count.

### Purge boundary semantics, stated precisely

`splits.purge` removes a training row only when `T + horizon > test_start`,
strictly. A label that resolves **exactly at** the boundary was determined
entirely before the test period and leaks nothing, so it is kept. d5's first
fold contains such a row at `2026-07-15T07:00Z`. A test asserts the `<=`
invariant rather than a stricter one that would have been wrong.

---

## G. The two-stage contract, and what "qualified" means

Pre-registration §7 defines two stages. **Surviving the first is not
qualification.**

### Stage 1 — the research region

Reused wholesale: `research_model_quality.evaluate_fold` scores each fold
through `ModelingEngine.train_and_evaluate`, and `research_model_quality.verdict`
applies the survival rule:

- beats **every** mandatory baseline in a **strict majority** of folds;
- pooled effective sample ≥ 30;
- probabilistic candidates: Brier below the base rate in a majority of folds.

Status `SURVIVES RESEARCH REGION` means only that — the candidate has earned one
look at the holdout.

### Stage 2 — one holdout evaluation, once

Survivors only. Fit on the whole research region with the same purge and embargo
applied at the boundary; evaluate on everything at or after it. The verdict is
the canonical `ModelEvaluation.is_deployable`:

```
beats_all_baselines is None  ->  None   (cannot be judged)
small_sample                 ->  False
otherwise                    ->  beats_all_baselines
```

**`None` is not `False`, and neither is a pass.** A candidate qualifies only when
`is_deployable is True`. A gate that was never evaluated has not been passed, and
`failed_gates()` maps that to `FAILED_OTHER_CANONICAL_GATE` rather than letting
it slide.

A candidate that never survived stage 1 is **never shown the holdout**.

### A defect found and fixed in F.6's harness

F.6's `research_horizon` ended with

```python
qualified = [c for c in candidates if c.get("qualified")]
```

but `rmq.verdict()` returns `status`, never a `qualified` key — so that list was
**always empty**. The harness would have reported `NONE QUALIFIED` whatever the
candidates did.

It never affected F.6's recorded result, provably: zero folds formed, so the
`INSUFFICIENT DATA` return fired before any candidate was scored, and a test pins
that. But it would have silently falsified F.6B. It is fixed, F.6's harness now
reports *survival* rather than claiming qualification it cannot assess, and the
holdout stage lives in F.6B where the pre-registration puts it.

This is the class of fix §14 of the brief allows: a genuine implementation
defect, fixed, documented, with the affected scope stated.

---

## H. Baselines, metrics and what is deliberately absent

Baselines are `ModelingEngine`'s mandatory set, untouched and compared per fold
and again on the holdout. A candidate does not qualify because R² > 0 or
directional accuracy > 50%; the canonical gate is authoritative.

| Metric | Status |
|---|---|
| MAE, RMSE, R², directional accuracy | computed by the engine, reported per fold and on the holdout |
| Brier vs base rate | computed for probabilistic candidates — the **frozen calibration check** |
| abstention rate | reported |
| fold stability: mean, worst, best, spread, stdev, sign consistency | reported |
| expected calibration error | **NOT COMPUTED** — it needs per-row predicted probabilities, which the reused `evaluate_fold` does not return; retrofitting that after seeing results would be a post-hoc diagnostic |
| rank correlation | **not in the frozen metric set** — not computed, not reported as if it were |
| prediction-interval coverage | **UNAVAILABLE** — no interval machinery is in the frozen plan |

Nothing unfavourable is omitted, and nothing absent is invented. Fold dispersion
is reported under stability and is explicitly **not** an interval.

---

## I. Calibration, uncertainty and expected return

**Calibration.** The frozen methodology is Brier against the base rate on
research-region folds, and that is the whole of the calibration gate.
**Nothing is calibrated on the holdout.** Where a candidate is not
probabilistic, calibration is `NOT_APPLICABLE` — not a silent pass.

Expected calibration error is **not computed**. `compute_calibration` exists in
the engine, but it needs the per-row predicted probabilities and the reused
`rmq.evaluate_fold` returns only the Brier values. Adding that retention after
seeing the results would be introducing a diagnostic because the primary result
disappointed, which §21 of the brief forbids. It is therefore reported as
`NOT_COMPUTED` rather than as an empty field that looks computed.

**Uncertainty.** Reported as `UNAVAILABLE` with the reason stated. There is no
prediction-interval machinery in the frozen plan, and fold dispersion is a
different quantity. Manufacturing an interval to unlock target estimation is
exactly the failure the contract is built to prevent, so the field stays absent.

**Expected return.** Available only when the target genuinely represents return,
the candidate passes qualification, and lineage is complete. Otherwise
`UNAVAILABLE`, even when the raw model emitted a number.

---

## J. Target and horizon integration — shadow only

Unchanged from F.5 and not redesigned. `target_estimation` yields a zone only
when a `TargetEstimate` is `AVAILABLE`, which requires a qualified model.
`horizon_intelligence` returns a preferred horizon or `None`, and raises if a
preferred horizon is set while horizons disagree. A majority vote never overrides
directional disagreement: if d1 and d5 point different ways, `preferred_horizon`
may legitimately remain `None`.

**QUALIFIED_RESEARCH_CANDIDATE ≠ PRODUCTION_ACTIVE_MODEL.** The second stays
false. A qualified candidate may feed the F.5 diagnostic in shadow, and shadow is
where it stops.

If a qualified candidate exists but its edge or confidence is insufficient, F.5
may finally emit **`VALID_NO_SIGNAL`** — which is scientifically different from
`MODEL_NOT_QUALIFIED`, and the distinction is the whole point of F.5's taxonomy.

---

## K. Transaction costs

Evaluated only where expected return is legitimately available and canonical cost
estimates exist. Unknown cost must not become zero. If costs exceed the expected
edge, `VALID_NO_SIGNAL / COST_EXCEEDS_EDGE` is the correct answer, not a
disappointment to be engineered around.

---

## L. Something the plan could not deliver, found before any result

**d10 has zero holdout rows.**

Its latest matured label sits at `2026-08-15`, three days before the frozen
boundary, because a d10 label needs ten trading days after its event and the
price history ends `2026-08-27`. So the holdout region for d10 is empty.

The consequence is structural: the pre-registered protocol reserves one holdout
evaluation for survivors, and d10 has nothing to evaluate on. **No d10 candidate
can reach qualification under this plan**, whatever its research-region
behaviour.

Two things about how that is handled:

- **The boundary was not moved.** Moving it would make d10 qualifiable and would
  be precisely the holdout shopping every preceding phase refused.
- **It was found at plan time**, before a single fit, and is recorded here and in
  a test rather than surfacing later as an excuse.

It is also a gap in F.7's own adequacy check, stated plainly: F.7 verified fold
viability and effective sample per horizon, and declared the design `ADEQUATE`.
It did **not** verify that each horizon's holdout region was non-empty. For d10
the design is not fully adequate, and F.7 should have caught it.

---

## M. No post-hoc expansion

Once execution begins:

- no candidate is added because the first ones failed;
- no feature is created to fix a failed candidate;
- no robustness test is introduced because the primary result disappointed;
- no horizon is added because the corpus turned out large enough;
- no threshold is relaxed, and no signal threshold is lowered for coverage;
- there is no round two.

A new hypothesis requires a new pre-registration and a new experiment. Bug fixes
are different and are handled as in §G: fix, document, state the affected scope,
and never silently continue.

---

## N. Negative knowledge is persisted

Failed candidates are data. `research_candidate_results` retains, per cell: the
execution id, specification, horizon, snapshot id and hash, pre-registration
hash, code commit, survival status, holdout verdict, failed gates, metrics, full
fold-level evidence and notes.

A future autonomous phase can therefore see that a specification has already lost
on this snapshot, and not spend a budget rediscovering it.

---

## O. Execution marker and interruption

`research_executions` records the pre-registration hash, snapshot hash, holdout
boundary, code commit, the full cell list, the cells that completed, the status
and the qualified count.

- a COMPLETE marker for the same frozen plan blocks a second run;
- a failure persists `failed` with the cells that did finish;
- a run whose cell list and completed-cell list differ cannot read as complete.

A horizon that returns `INSUFFICIENT DATA` evaluates no cell. That is a
legitimate frozen outcome rather than a partial execution, and it is reported
separately as `cells_unevaluated` so the two cannot be confused.

---

## P. D20 isolation

| | |
|---|---|
| executed / inspected / inferred / consumed | **NO** |
| trained on / evaluated | **NO** |
| used for feature, family, hyperparameter, horizon or target selection | **NO** |
| used for calibration | **NO** |
| used to judge whether the recovered dataset is better | **NO** |
| hypothesis / anchor-v2 / protected window changed | **NO** |

Structural: the horizon is absent from the handoff's eligible list, effective
sample and purge policy; `research_horizon()` raises
`ProtectedHorizonRequested`; `rs.effective_sample()` raises if asked;
`ResearchHandoff` refuses to construct unless `d20_excluded` is true. Boundary
tests assert the harness contains no `d20.abnormal_return`, `d20.raw_return`,
`validate_d20_reversal`, `anchor_v2`, `return_60d` or `check_d20_readiness`, and
no `value_json` — so it cannot read a label value at all.

The one permitted appearance of the name is the exclusion flag the harness reads
and asserts.

---

## Q. Production boundary

| | |
|---|---|
| broker writes attempted / succeeded | 0 / 0 |
| paper orders / live orders | 0 / 0 |
| billable market-data requests | 0 |
| Portfolio / Risk / OrderIntent / Execution | unchanged |
| capture / IBKR / subscriptions | unchanged |
| production model | unchanged |
| models promoted | **0** |

The frozen snapshot is read `mode=ro` for every evaluation. The only writes are
the execution marker and candidate results, inside the research database. The
production database (`data/marketlens.db`) is never opened for writing, and a
test asserts it holds no `research_executions` table.

The hard separation stands: **research qualified ≠ shadow validated ≠ paper
authorised ≠ live authorised.** F.6B ends at the first.

---

## R. Handoff to I-G

Only if one or more candidates legitimately qualify. The I-G handoff would carry
the qualified candidate ids and versions, horizons, snapshot id and hash,
pre-registration id, the qualification evidence, expected-return capability,
calibration and uncertainty semantics, target capability, confidence semantics,
known failure modes, the shadow-only activation requirements, and
`production_active = FALSE` / `broker_authorized = FALSE`.

I-G validates them in shadow. It does **not** reinterpret F.6B's qualification.

If zero qualify, no I-G trading handoff is fabricated. The honest output is
**RESEARCH FOLLOW-UP REQUIRED**, and the follow-up needs a new hypothesis and a
new pre-registration — not another pass at this one.

---

## S. What this phase cannot tell you

A model that qualifies here has beaten naive baselines out of sample across a
majority of folds and survived one holdout evaluation on a **12-day** holdout.
That is the bar the project set, and it is a research bar, not a trading licence.

A phase that qualifies nothing has learned that the four pre-registered
specifications do not carry sufficient predictive evidence on this dataset. That
is a real finding about those specifications on this data — and it is not a
finding that no edge exists.

> **THE EXPERIMENT IS FROZEN BEFORE THE RESULT. THE MODEL MUST EARN
> QUALIFICATION.**
