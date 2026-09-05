# Phase 20 — final report

Date: 2026-09-05 · Base commit: `cacc2b5` · Scope: the diagnostic layer, nothing else

Reference: `docs/PHASE_20_ERROR_ATTRIBUTION.md`. Every figure was
measured against the **production release asset** (`db-latest`,
published 2026-09-04T23:06Z).

---

## PHASE 19 RECOMMENDATIONS REVIEW

| Item | State at entry | Verified now |
|---|---|---|
| **Outcome architecture** | 4 layers, 2 built | **HOLDS.** `git diff HEAD -- src/outcomes` is empty. Attribution consumes it and changes nothing |
| **Prediction outcomes** | 3,745 measurements | **USED.** Diagnosed as a distinct subject kind |
| **Signal outcomes** | 2,765 measurements | **USED.** Never pooled with predictions |
| **Forward returns** | simple and log | **USED** for direction and magnitude |
| **MFE / MAE** | signed favourable-positive | **USED** — the whole basis of the timing detector |
| **Outcome windows** | 7 horizons per subject | **USED** — the basis of horizon mismatch, which needs siblings |
| **Confidence semantics** | heuristic, one bucket | **CONFIRMED AGAIN.** Every attributed signal lands in one bucket. Reported, not manufactured |
| **Signal strength** | varies 0–1, distinct from confidence | **KEPT DISTINCT** — separate cohorts, disjoint labels, asserted by test |
| **Model linkage** | direct via `signal_contributions` | **USED** as a cohort dimension |
| **Point-in-time controls** | one-directional, 3 enforcement layers | **EXTENDED** with the same three for attribution |
| **Outcome versioning** | `method_version` in the PK | **MIRRORED** exactly |
| **`signal_outcomes` (Phase 10)** | 10 rows, label-derived, superseded | **UNTOUCHED** |
| **`signal_evaluations`** | 18 rows | **UNTOUCHED** |
| **TD-05 pipeline** | 0 runs, first cron 2026-09-06 02:00 UTC | **STILL 0 RUNS.** Now thirteen stages |

Nothing in Phase 19's architecture was modified.

---

## 1. Executive summary

Eleven deterministic detectors run over every measured outcome. Each
either cites numbers or stays silent; none infers a cause from the fact
that a result was bad.

```
6,510 subjects        4,311 assessable (66.2%)      19,140 evidence rows
7,574 attributions      180 queued for human review        0.5s
```

**The finding that shaped the whole design:** six of the nine layers
§2 lists have **no evidence source in the production database**. Sizing,
risk, execution and portfolio have no tables; regime has a column that
is NULL on all 6,510 rows. Those detectors are implemented, tested
against the inputs they await, and return `INSUFFICIENT_EVIDENCE`
naming the missing table — because a layer that silently produced no
findings would read as a clean bill of health.

**A loss is not a mistake.** 428 outcomes are `NO_ERROR`, 753 are
`EXPECTED_LOSS`, and 2,199 are `INSUFFICIENT_EVIDENCE`. Together that
is 45% of the record that this layer declines to call a fault.

---

## 2. Initial state

```
outcome_measurements   6,510   (4,311 available, 605 pending, 1,594 insufficient)
signals                  408   with model linkage and suppression notes
predictions              549
research_observations  1,049   1,019 high quality, 30 invalid
market_regime           NULL   on every row
portfolios / positions / risk_decisions / order_intents      ABSENT
tests                  3,116
```

---

## 3. Outcome architecture used

Phase 19's `outcome_measurements` is the sole input, joined to
`signals` and `research_observations` for decision-time context and to
`outcome_aggregates` for the expected/unexpected judgement.

Nothing is recomputed. No return, MFE, MAE or price is stored again.

---

## 4. Error attribution architecture

```
outcome -> 11 detectors -> findings with evidence -> rank by causal depth
        -> status -> review queue where the rules cannot decide -> store
```

`src/domain/attribution_models.py` (taxonomy, thresholds, evidence),
`src/data_access/attribution_schema.py` (3 tables),
`src/attribution/detectors.py` (the rules),
`src/attribution/engine.py` (ranking),
`src/attribution/pipeline.py` (batch),
`src/attribution/analytics.py` (profiles),
`src/attribution/api.py` (queries, export, counterfactuals),
`scripts/attribute_errors.py`.

---

## 5. Error types — measured

| Primary | n | | Contributing | n |
|---|---:|---|---|---:|
| `unknown` | 2,379 | | `timing_error` | 617 |
| `prediction_error` | 1,856 | | `horizon_mismatch` | 361 |
| `expected_loss` | 753 | | `signal_error` | 86 |
| `magnitude_error` | 689 | | | |
| `no_error` | 428 | | | |
| `timing_error` | 222 | | | |
| `signal_error` | 183 | | | |

Timing is far more often a **contributor** (617) than a primary cause
(222) — which is what the causal ordering predicts: when the direction
was also wrong, the timing of a move that was never going to happen is
a consequence.

---

## 6–8. Prediction, magnitude, horizon

**Prediction (1,856 primary).** Fires only on an explicit `miss`, which
Phase 19 already filtered through the neutral band — so a move too small
to mean anything cannot produce one. Confidence `HIGH`: it is an
arithmetic fact. It says the direction was wrong; it does **not** say
the model is faulty, which is what the profiles are for.

**Magnitude (689).** Direction right, size wrong — a separate finding
from a wrong sign, because the remedy differs entirely. Fires in both
directions: a wild overshoot is a calibration failure too, and counting
only shortfalls would bias the profile. It refuses to fire when the
direction was wrong, or neither label would distinguish anything.

**Horizon (361, all contributing).** Wrong at the stated horizon, right
later — needs the sibling measurements Phase 19 provides. The rescue
must clear 2%, or a two-basis-point drift at 10d would "rescue" every
1d miss.

---

## 9–10. Timing and signal

**Timing (222 primary, 617 contributing).** Two conditions, both
required: the excursion was worth having (≥2%) **and** the close kept
under 40% of it. `time_to_mfe_seconds` sharpens it — an extreme on the
reference bar means the move was already over when the signal spoke.

Capped at `MEDIUM` confidence, deliberately: there is no order and no
exit rule, so "not captured" is inferred from the price path rather
than observed from a fill. That caveat is stored as evidence.

**Signal (183 primary, 86 contributing).** Fires when a suppressed
signal turned out right **and** worth ≥2%. A suppression that avoided a
loss is the rule working. Also `MEDIUM`: the suppression may still have
been correct on the information available at the time — hindsight is
not a verdict on a rule.

---

## 11–14, 16. The layers with no evidence

| Layer | Missing | Detector behaviour |
|---|---|---|
| Sizing | `positions`, `portfolios` | `INSUFFICIENT_EVIDENCE` ×6,510 |
| Risk | `risk_decisions` | `INSUFFICIENT_EVIDENCE` ×6,510 |
| Execution | `execution_fills`, `order_intents` | `INSUFFICIENT_EVIDENCE` ×6,510 |
| Portfolio | `portfolios`, `positions` | `INSUFFICIENT_EVIDENCE` ×6,510 |

All four are implemented and tested with the inputs they await:

- **Risk** distinguishes `EXPECTED_RISK_BLOCK` from a policy violation.
  A block followed by a favourable move is risk working; grading it on
  hindsight would train it to decline less. Only an approval that
  contradicts its own recorded limits fires, at `CRITICAL`.
- **Execution** compares the fill against the **decision price only**.
  It never reads the realised return, so a wrong direction cannot become
  an execution finding (§14). A test drives a clean fill on a −20% call
  and asserts silence.
- **Sizing** compares notional against the risk budget.
- **Portfolio** compares concentration against its limit.

---

## 15. Regime

`market_regime` is **NULL on all 6,510 rows**, so the detector returns
`INSUFFICIENT_EVIDENCE` everywhere. Given a label it also requires a
cohort of ≥30 before calling a regime the difference — §15 forbids
labelling every high-volatility loss a regime error, and that needs a
population, not an anecdote. A test proves both halves.

`signals.volatility_percentile` is populated on 404 of 408 signals and
could support a future regime definition. Inventing one here is exactly
what §15 forbids.

---

## 17–19. Expected losses, no-error, unknown

| Verdict | n | Meaning |
|---|---:|---|
| `EXPECTED_LOSS` | 753 | inside the cohort's 10th–90th percentile band |
| `NO_ERROR` | 428 | every assessable layer behaved as designed |
| `INSUFFICIENT_EVIDENCE` | 2,199 | the outcome itself was never measured |
| `UNKNOWN` + review | 180 | unusual result, no layer explains it |

**A bug this caught in development.** The engine originally reached
"nothing fired → NO_ERROR" for outcomes that were merely `pending` —
peripheral detectors like data quality can still answer on them. That
declared 2,199 unmeasured outcomes clean. You cannot call a decision
sound when you do not know what happened; there is now a gate before
every detector and a test pinning it.

---

## 20. Primary versus contributing

Ranked by **causal depth**, not severity and not detector order —
`DATA(0) → REGIME(1) → PREDICTION(2) → HORIZON(3) → MAGNITUDE(4) →
SIGNAL(5) → TIMING(6) → SIZING(7) → RISK(8) → EXECUTION(9) →
PORTFOLIO(10)`. Confidence breaks ties within a depth.

That is a claim about causation, so it lives in one documented table
rather than implicit in a comparator. Equal depth **and** equal
confidence goes to review rather than an arbitrary winner.

Every primary records why it outranked the others, and — where a layer
could not be assessed — that a cause may lie there.

---

## 21–22. Evidence and confidence

**19,140 evidence rows** for 7,574 attributions: 2.5 facts per
conclusion. `ErrorAttribution` cannot be written without one —
`require_evidence()` raises, the repository calls it before every
insert, and a test asserts the message.

Evidence kinds by volume: `coverage` 5,900 · `direction` 5,090 ·
`missing_input` 4,398 · `neutral_band` 3,712 · `expectedness` 2,722 ·
`checks` 2,722.

**Confidence:** high 2,545 · medium 1,586 · low 180 · insufficient
2,199. Ordinal labels, not probabilities — nothing has ever checked how
often an attribution is right, and `error_attributions` has no column
that could hold a p-value.

**Severity** (independent): critical 56 · high 228 · medium 1,167 ·
low 1,679 · info 3,380.

---

## 23. Counterfactuals

Six named questions, every one returning `observability='hypothetical'`
and `result=None`. No alternative outcome is computed, because doing so
honestly needs an execution model, a fill model and a sizing rule, and
none exists. Only `different_horizon` is answerable from data that
exists today.

Every query and every profile filters to `observed` by default; a test
flips an attribution to hypothetical and asserts it vanishes from the
profiles.

---

## 24–25. Versioning and recomputation

Verified against the production copy:

```
run 1                  7,574 attributions   19,140 evidence
run 2                  7,574               19,140     (+0, 6,510 skipped)
run 2 --recompute      7,574               19,140     (+0)
method v2             15,148                          (+7,574 NEW rows)
v1 afterwards          7,574   untouched
```

`compare_versions()` reports where two methodologies reached different
**primary** conclusions.

---

## 26. Review queue

180 cases, each with a reason, candidate types, and a recommended
check. **Never auto-closed** — the queue exists because a rule could not
decide, so a rule must not decide it is finished. Re-queuing preserves a
`reviewed` state; a test pins that.

---

## 27. Dashboard

A **"Diagnostic erori"** workspace. Coverage first, then the
distribution — an error rate over two thirds of the record means
something different from one over all of it.

Shows: status mix, primary-versus-contributing distribution, rate by
horizon, per-model profile (no rate below 30 observations), evidence by
kind, the most severe individual cases with their expected/realised
values and evidence counts, and the review queue.

The page states in prose that a loss is not a mistake, that six layers
have no evidence source, that confidence is not a probability, that
severity says how much it mattered rather than how sure we are, and
that nothing here modifies a model, threshold, strategy, risk limit or
capital figure.

It renders on a database where Phase 20 has never run, and it pins a
single methodology version — a page that did not filter would add two
methodologies together and double the findings over the same subjects.

---

## 28–30. API, database, export

Nine routes as typed functions, following the convention Phase 19
established. Every listed attribution arrives **with its evidence
attached**, never optional.

Three tables. Evidence is a table rather than a JSON blob so *"show me
every attribution resting on a capture ratio"* is answerable and an
orphan check is one SQL statement.

`integrity_check()` — seven queries, all must be zero. On production:

```
orphan_attributions                0     invalid_error_type                0
attributions_without_evidence      0     invalid_confidence                0
orphan_evidence                    0     subjects_without_a_primary        0
hypothetical_counted_as_observed   0
```

Export: `export_csv()` joins each attribution to the outcome it
diagnosed and reports how many evidence rows back it;
`export_evidence_csv()` writes the facts so a conclusion can be
re-derived from source. CSV rather than Parquet — pyarrow is not a
dependency and a format that needs one will one day not work.

---

## 31–33. Testing

```
Ran 3250 tests in 171.972s
OK (skipped=1)
exit 0
```

**+134 tests** (52 + 46 + 36). None suppressed, none deleted, no
existing test modified.

| File | Tests |
|---|---:|
| `tests/attribution/test_detectors.py` | 52 |
| `tests/attribution/test_engine_and_leakage.py` | 46 |
| `tests/attribution/test_api_and_dashboard.py` | 36 |

All 24 areas in §63 covered. All eleven adversarial cases in §64:

| Case | Test |
|---|---|
| Future outcome in attribution inputs | `test_attributing_does_not_modify_the_outcome_it_reads` |
| Future data changes the original decision | AST scan: no write to any decision table |
| Missing data becomes an error | `test_missing_data_does_not_become_a_data_error` |
| Small sample becomes high confidence | `test_a_small_cohort_cannot_declare_anything_unusual` |
| Successful trade labelled correct | `test_a_winning_result_is_not_automatically_correct` |
| Failed trade labelled prediction error | `test_a_losing_result_is_not_automatically_a_prediction_error` |
| Execution blamed for prediction error | `test_execution_is_not_blamed_for_a_wrong_direction` |
| Risk rejection labelled a failure | `test_risk_distinguishes_an_expected_block_from_a_violation` |
| Counterfactual reported as observed | `test_a_counterfactual_is_never_stored_as_observed` |
| Duplicate attribution | `test_a_duplicate_attribution_cannot_be_created` |
| Methodology change without version | `test_the_old_version_is_untouched` |

Leakage (§39, §40): an AST scan of every module asserts the only tables
written are the three attribution tables; no module imports the
training, inference, promotion, feature or signal engines; no module
contains `promote`, `train` or `.fit(`; no earlier pipeline script reads
the attribution tables; and attributing a real outcome leaves
`outcome_measurements` byte-identical.

Determinism (§65): a test asserts the detectors contain no `random.`,
`datetime.now`, `time.time` or `uuid`.

---

## 34. Performance

**0.5s** for 6,510 outcomes → 7,574 attributions. Evidence, signals,
observations and cohorts are each loaded once and indexed in memory
rather than queried per outcome; siblings are grouped once for the
horizon detector.

No optimisation was needed and none was made.

---

## 35. Security

No new credentials, secrets, dependencies or inbound surface. Standard
library only. `audit_live_safety.py`: **16 of 16 PASS**.

---

## 36–38. Files and migrations

**Created (9):** `src/domain/attribution_models.py`,
`src/data_access/attribution_schema.py`, `src/attribution/__init__.py`,
`detectors.py`, `engine.py`, `pipeline.py`, `analytics.py`, `api.py`,
`scripts/attribute_errors.py`, three test modules, and two documents.

**Modified (2):** `src/dashboard.py` (collector, workspace, nav,
router), `.github/workflows/pipeline.yml` (stage 12 of 13).

**Removed:** none.

**Migrations:** three additive tables and five indexes, created
idempotently on first use. No existing table altered, no column
dropped, no row rewritten.

---

## 39. Remaining issues

1. **TD-05 still unverified.** `pipeline.yml` has run zero times; the
   first fires 2026-09-06 02:00 UTC, now with thirteen stages.
2. **Six of nine layers unassessable.** Not a defect of this phase —
   the tables do not exist. Each is implemented and waiting.
3. **`market_regime` is NULL everywhere**, so regime analysis is empty.
   The column exists; nothing populates it.
4. **Confidence analysis is degenerate** — one bucket, as Phase 18
   predicted and Phase 19 confirmed.
5. **68% error rate on the largest model** over 3,121 assessed
   outcomes. Consistent with a model that fails its own baselines. This
   is a measurement, not a recommendation: no model was changed.
6. **Attribution confidence is uncalibrated.** Nothing has checked how
   often an attribution is right. Deliberate — labels, not
   probabilities — but it means the confidence field cannot yet be used
   to weight anything.

---

## 40. Future learning readiness

Phase 21 inherits diagnoses that are **evidence-backed** (2.5 facts
per conclusion, none writable without one), **versioned** (a rule change
cannot rewrite history), **idempotent**, **layered** (primary versus
contributing preserved), and **honest about their limits** (45% of the
record is explicitly not called a fault, and six layers say which table
they are waiting for).

A memory built on forced explanations would remember fabrications with
the same fidelity as facts. This one can say `NO_ERROR`, `UNKNOWN` and
`INSUFFICIENT_EVIDENCE`, and it does, for 45% of what it looked at.

---

## 41. Next phase

Phase 21 — **Trading Memory**. Deliberately not started here.

---

# READY FOR PHASE 21

With the same standing watch, now one stage larger:

**Confirm the 2026-09-06 02:00 UTC pipeline run.** Thirteen stages.

Phase 20 built the diagnostic layer and nothing else. It promotes no
model, modifies no model, trains nothing, changes no threshold, no
strategy, no risk limit and no capital figure, and enables no
execution. The IBKR safety chain and the entire Phase 19 outcome layer
are byte-identical — `git diff` for both is empty.

It diagnosed 6,510 outcomes and declined to explain 45% of them.
That restraint is the deliverable.
