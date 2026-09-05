# Error Attribution — reference

Phase 20 · methodology version `v1` · 2026-09-05

Where a deviation occurred, and the evidence that says so. Diagnosis
only: nothing here changes a model, a strategy, a threshold, a risk
limit or a capital figure.

---

## 1. What this layer refuses to be

Not a scoreboard. **A loss is not a mistake and a win is not a correct
decision.** A good decision can lose and a bad one can win, and a
diagnostic layer that forgets this becomes a machine for rationalising
noise.

So three verdicts are not errors, and they are first-class:

| Verdict | Meaning |
|---|---|
| `NO_ERROR` | every assessable layer behaved as designed |
| `EXPECTED_LOSS` | a loss inside the cohort's own usual range |
| `UNKNOWN` | the evidence does not support a conclusion |

`INSUFFICIENT_EVIDENCE` is a **status**, not a failure.

---

## 2. Error types

| Type | Fires when | Evidence it needs |
|---|---|---|
| `PREDICTION_ERROR` | direction wrong at the stated horizon | Phase 19 `direction_result` |
| `MAGNITUDE_ERROR` | direction right, size badly estimated | expected vs realised return |
| `HORIZON_MISMATCH` | wrong at the horizon, right later | the same subject at other horizons |
| `TIMING_ERROR` | the favourable move happened and was not kept | MFE, capture ratio, `time_to_mfe` |
| `SIGNAL_ERROR` | the prediction→signal step cost something | signal status and suppression note |
| `DATA_ERROR` | decision-time data was bad | `research_observations.quality_level` |
| `REGIME_ERROR` | the cohort is systematically worse in this regime | a regime label **and** a cohort ≥ 30 |
| `SIZING_ERROR` | position wrong for the risk budget | `positions`, `portfolios` |
| `RISK_ERROR` | a risk decision contradicted its own policy | `risk_decisions` |
| `EXECUTION_ERROR` | the fill deviated materially from the decision price | `execution_fills` |
| `PORTFOLIO_ERROR` | concentration above its limit | `portfolios` |

---

## 3. What can and cannot be diagnosed here

Measured against the production database on 2026-09-05, **six of the
nine layers have no evidence source at all**:

| Layer | Evidence | Status |
|---|---|---|
| prediction | 549 predictions | **assessable** |
| signal | 408 signals with model linkage | **assessable** |
| timing / horizon | MFE, MAE and their timestamps, seven horizons | **assessable** |
| data | observation quality levels | **assessable** |
| regime | `market_regime` is **NULL on all 6,510 rows** | not assessable |
| sizing | `portfolios`, `positions` absent | not assessable |
| risk | `risk_decisions` absent | not assessable |
| execution | `order_intents`, `execution_fills` absent | not assessable |
| portfolio | `positions` absent | not assessable |

Those six detectors are **implemented, not stubbed**. They return
`INSUFFICIENT_EVIDENCE` naming the table that is missing, and tests
drive each of them with the inputs it is waiting for to prove it works.
They begin producing findings the moment their inputs exist.

Naming the missing table is the point. A layer that silently produced
no findings would look like a clean bill of health, which is the most
flattering possible lie for a diagnostic system to tell.

`signals.volatility_percentile` is populated on 404 of 408 signals and
could support a future regime definition. Inventing one here is exactly
what §15 forbids.

---

## 4. Rules and thresholds

Every threshold is named, documented, and reused rather than reinvented.

| Constant | Value | Why |
|---|---|---|
| `NEUTRAL_BAND` | 0.001 | **Imported from Phase 19.** A move under 10bp is not evidence of anything |
| `MAGNITUDE_SHORTFALL_RATIO` | 0.25 | realised under a quarter of expected |
| `MAGNITUDE_OVERSHOOT_RATIO` | 4.0 | a wild overshoot is a calibration failure too |
| `HORIZON_RESCUE_RETURN` | 0.02 | without a floor, a 2bp drift would "rescue" every miss |
| `TIMING_MFE_FLOOR` | 0.02 | a 0.3% blip is not a missed opportunity |
| `TIMING_CAPTURE_RATIO` | 0.4 | kept under 40% of what was offered |
| `MIN_COHORT_SAMPLE` | 30 | **the Phase 9 evaluator's own number**, reached through Phase 19 |

Two rules deserve their reasoning spelled out:

**Magnitude only applies once the sign was right.** Otherwise every
wrong call would also be a magnitude error and neither label would
distinguish anything.

**Timing is signed by direction.** A short that fell is a gain; without
signing the capture ratio, the rule is meaningless for half the book.

---

## 5. Primary versus contributing

One outcome may carry several attributions — §19 requires it, because a
bad result frequently has more than one cause.

The primary is chosen by **causal depth**, not by severity and not by
detector order. An error early in the chain explains the ones after it:

```
0  DATA_ERROR          bad data explains everything downstream
1  REGIME_ERROR
2  PREDICTION_ERROR    a wrong direction makes timing a consequence
3  HORIZON_MISMATCH
4  MAGNITUDE_ERROR
5  SIGNAL_ERROR
6  TIMING_ERROR
7  SIZING_ERROR
8  RISK_ERROR
9  EXECUTION_ERROR     can only ever add to a loss already decided
10 PORTFOLIO_ERROR
```

Confidence breaks ties **within** a depth. That ordering is a claim
about causation, so it lives in one documented table rather than
implicit in a comparator — disagreeing with it means editing
`_CAUSAL_DEPTH`, not reverse-engineering a sort.

The primary attribution records **why** it outranked the others, as
evidence.

---

## 6. Evidence

Every attribution carries the numbers that produced it. `ErrorAttribution`
**cannot be written without at least one `Evidence`** — `require_evidence()`
raises, and the repository calls it before every insert.

Each evidence row has a `kind`, a `statement` a person reads, a `source`
naming the table and column, and the structured `value`/`comparison`.
Structured as well as formatted, because an aggregate over evidence is
useful and an aggregate over prose is not.

A row saying "timing error" is an opinion. A row saying *"a +6.5%
favourable excursion was available; the close kept +2.4% (37%)"* is a
finding somebody can check and disagree with.

Evidence lives in its own table rather than a JSON blob, so *"show me
every attribution resting on a capture ratio"* is answerable and an
orphan check is one SQL statement.

---

## 7. Confidence and severity

`HIGH` · `MEDIUM` · `LOW` · `INSUFFICIENT_EVIDENCE`.

**Ordinal labels, not probabilities.** Nothing has ever checked how
often an attribution turns out to be right, so emitting `0.87` would
imply a calibration that does not exist. `error_attributions` has no
column that could hold one, and a test asserts that.

Severity — `INFO` · `LOW` · `MEDIUM` · `HIGH` · `CRITICAL` — is
**independent**. It says how much this mattered, not how sure we are.
`CRITICAL` severity with `LOW` confidence is a useful thing to be able
to say: *if this is what happened it matters a great deal, and we are
not sure it is.* Collapsing the two makes that sentence impossible.

Two detectors cap themselves at `MEDIUM` on purpose:

- **Timing** — there is no order and no exit rule, so "not captured" is
  inferred from the price path rather than observed from a fill.
- **Signal** — a suppression may still have been correct policy on the
  information available at the time. Hindsight is not a verdict on a
  rule.

Both carry that caveat as evidence rather than only in a docstring.

---

## 8. Expected versus unexpected

A result inside the cohort's own **10th–90th percentile band** is
ordinary; outside it is worth a look. The percentiles come from Phase
19's aggregates, so no new distributional assumption is introduced.

Below 30 observations there is **no expectedness judgement at all**. A
cohort of eleven cannot say what is unusual, and treating it as though
it could is how a small sample becomes a confident claim.

An unusual result that no layer explains becomes `UNKNOWN` +
`REQUIRES_REVIEW`, never a manufactured cause. §27 applies to wins too:
a surprising win can be luck, a regime shift, or a data issue, and it
does not validate the model.

---

## 9. Status and the review queue

`PENDING` · `ATTRIBUTED` · `PARTIALLY_ATTRIBUTED` ·
`INSUFFICIENT_EVIDENCE` · `REQUIRES_REVIEW` · `SUPERSEDED`

`PARTIALLY_ATTRIBUTED` means a finding was made **and** some layer
could not be assessed — the conclusion says so rather than implying
completeness.

A case reaches `REQUIRES_REVIEW` when two findings share the same
causal depth **and** the same confidence. Picking one by tie-break would
manufacture a certainty the evidence does not contain.

The queue records the reason, the candidate types, and a recommended
check. **It is never auto-closed.** The queue exists because a rule
could not decide, so a rule must not decide it is finished either.

---

## 10. Counterfactuals

`counterfactual()` returns the **question**, the observed facts bearing
on it, what would be needed to answer it, and
`observability='hypothetical'`.

It computes no alternative outcome. Doing that honestly needs an
execution model, a fill model and a sizing rule, and none exists here. A
number produced without them would be a fabrication wearing a result's
clothes.

Six questions are named so the vocabulary is fixed before anything
computes against it: `earlier_entry`, `half_size`, `risk_not_rejected`,
`better_fill`, `different_horizon`, `different_model`. Only
`different_horizon` is answerable from data that exists today.

Every query filters to `observed` by default. A caller must ask for
hypotheticals by name, and profiles exclude them — a counterfactual
counted alongside history would manufacture a track record that never
happened.

---

## 11. Versioning and recomputation

Identity is:

```
(subject_kind, subject_id, horizon, method_version, error_type)
```

`method_version` is part of the primary key, so a rule change writes
**new rows beside the old ones** and never rewrites a conclusion
somebody has read. `error_type` is part of it because one outcome may
legitimately carry several attributions.

`compare_versions()` shows where two methodologies reached different
**primary** conclusions — which is the question a rule change actually
raises.

Writes are `INSERT OR REPLACE`; running twice cannot change the row
count. Evidence is deleted and rewritten for its attribution in the same
transaction, so a re-run cannot accumulate duplicate evidence behind a
stable conclusion.

---

## 12. Point-in-time

Attribution may inspect outcomes. It must never modify a decision, a
feature, a model or a strategy. Enforced three ways:

1. **Structurally** — an AST scan of every module in `src/attribution/`
   collects the strings handed to `execute`/`executemany` and asserts
   the only tables written are `error_attributions`,
   `attribution_evidence` and `attribution_review_queue`. Also: no
   module imports the training, inference, promotion, feature or signal
   engines, and none contains `promote`, `train` or `.fit(`.
2. **By ordering** — attribution is stage 12 of 13, after outcome
   measurement. A test fails if that order changes, and another asserts
   no earlier script reads the attribution tables.
3. **Behaviourally** — a test attributes a real outcome and compares
   `outcome_measurements` byte for byte before and after.

---

## 13. Database

`error_attributions` (conclusion) · `attribution_evidence` (facts) ·
`attribution_review_queue` (cases for a person).

**Nothing duplicates Phase 19.** No return, MFE, MAE or price is stored
again; an attribution references the measurement by its natural key and
copies only the handful of context columns needed to slice without a
join.

`integrity_check()` runs seven queries that must all return zero:
orphan attributions, attributions without evidence, orphan evidence,
invalid error type, invalid confidence, subjects without exactly one
primary, and invalid observability. The CLI runs it after every pass and
exits non-zero if any fires.

---

## 14. API

Following the convention `docs/API_AUDIT.md` records — no HTTP layer,
by decision — the §53 routes are typed functions over a connection:

| Route | Function |
|---|---|
| `GET /error-attribution` | `list_attributions()` |
| `GET /signals/{id}/errors` | `errors_for_signal()` |
| `GET /predictions/{id}/errors` | `errors_for_prediction()` |
| `GET /models/{id}/errors` | `errors_for_model()` |
| `GET /errors/summary` | `summary()` |
| `GET /errors/by-type` | `by_type()` |
| `GET /errors/by-model` | `by_model()` |
| `GET /errors/by-regime` | `by_regime()` |
| `GET /errors/review` | `review_queue()` |

Every listed attribution arrives **with its evidence attached** —
never optional, so a consumer cannot be built that never looks at any.

Export (§57): `export_csv()` and `export_evidence_csv()`. CSV rather
than Parquet, because Parquet needs pyarrow and this repository computes
research numbers on the standard library everywhere else.

---

## 15. Running it

```bash
python scripts/attribute_errors.py --apply
python scripts/attribute_errors.py --apply --recompute
python scripts/attribute_errors.py --export data/exports/attribution.csv
python scripts/attribute_errors.py --compare-versions v1 v2
```

Pipeline **stage 12 of 13**, after outcome measurement and before the
dashboard rebuild. Measured cost on the production database: **0.5s**
for 6,510 outcomes and 7,574 attributions.

---

## 16. What future learning can ask

| Question | Answered by |
|---|---|
| What went wrong? | `error_type` where `role='primary'` |
| How often? | `analytics.build_profiles()`, with sample sizes |
| Under what conditions? | twelve cohort dimensions |
| Which layer? | the taxonomy, with six layers honestly marked unassessable |
| How confident are we? | `confidence`, ordinal and uncalibrated |
| What evidence supports it? | `attribution_evidence`, joinable and countable |
| Was it the main cause or a contributor? | `role` |
| Did the rules struggle? | `attribution_review_queue` |
| Did a rule change alter this conclusion? | `compare_versions()` |

**Phase 21 is Trading Memory.** It inherits diagnoses that are
evidence-backed, versioned, idempotent, and honest about what they
cannot see. A memory built on forced explanations would remember
fabrications with the same fidelity as facts.

Measure first. Attribute second. Learn later.
