# Phase 21 — final report

Date: 2026-09-05 · Base commit: `71c64b8` · Scope: the memory layer, nothing else

Reference: `docs/PHASE_21_TRADING_MEMORY.md`. Every figure was measured
against the **production release asset** (`db-latest`, published
2026-09-04T23:06Z).

---

## PHASE 20 RECOMMENDATIONS REVIEW

| Item | State at entry | Verified now |
|---|---|---|
| **Attribution architecture** | 11 detectors, evidence-backed | **HOLDS.** `git diff HEAD -- src/attribution` is empty. Memory consumes it and changes nothing |
| **Outcome architecture (19)** | 6,510 measurements | **HOLDS.** `git diff HEAD -- src/outcomes` is empty |
| **`signal_outcomes` (Phase 10)** | 10 rows, superseded | **UNTOUCHED** |
| **`signal_evaluations`** | 18 rows | **UNTOUCHED** |
| **Error attribution tables** | 7,574 attributions, 19,140 evidence | **LINKED, not duplicated.** Only the labels and the evidence *count* travel into memory |
| **Model registry** | 4 models, none promoted | **USED** as a cohort. Consequence: every experience is EXPERIMENTAL |
| **Signal contributions** | direct model linkage, 408/408 | **USED** for model memory |
| **Market regime data** | NULL on all 6,510 rows | **STILL NULL.** Regime memory is defined and empty — by data, not by design |
| **Event context** | 4 event types, 1,288 acquisitions | **USED.** Two pattern families key on it |
| **IBKR execution records** | none exist | **Execution memory defined and empty** |
| **Six unassessable layers** | sizing, risk, execution, portfolio, regime | **CARRIED FORWARD** — memory reports the same gaps rather than papering over them |
| **TD-05 pipeline** | 0 runs, first cron 2026-09-06 02:00 UTC | **VERIFIED — IT RAN.** 2026-09-06 06:41 UTC, `event: schedule`, all 13 stages `success`, 18 minutes. See §45 |

Nothing in Phase 19's or Phase 20's architecture was modified.

---

## 1. Executive summary

```
6,510 experiences built      4,311 knowable (66.2%)      27 days of history
  610 patterns                 195 above the sample threshold
36,311 pattern→experience evidence links
  541 contradictions surfaced, none resolved
```

**The design turns on one column.** `available_at` is when an
experience became *knowable* — when its outcome window closed — not
when it was computed. Dated by `created_at`, the whole record would
appear at one instant and every historical query would return the
future. Dated correctly:

| `as_of` | experiences | patterns |
|---|---:|---:|
| 2026-08-10 | **38** | 54 |
| 2026-08-20 | **1,114** | 365 |
| 2026-09-01 | 3,825 | 585 |
| now | 4,311 | 610 |

**Most of what memory holds is weak, and it says so.** 415 of 610
patterns are `WEAK` and quote no rate at all. That is the correct
result for 27 days of data, and §64 asks for exactly this: fewer
high-quality memories over many low-quality ones.

---

## 2. Initial state

```
outcome_measurements   6,510   (4,311 available)
error_attributions     7,574   with 19,140 evidence rows
signals                  408 · predictions 549 · models 4, none promoted
market_regime           NULL   everywhere
positions / portfolios / risk_decisions / order_intents    ABSENT
history depth         27 days  (2026-08-05 .. 2026-09-03)
tests                  3,250
```

The 27-day depth is the binding constraint on this phase and is
reported throughout rather than worked around.

---

## 3–5. Experience architecture, raw layer, context

`EXPERIENCE = CONTEXT + DECISION + EXPECTATION + OUTCOME + ATTRIBUTION
+ EVIDENCE` — a join, not a paraphrase.

References rather than copies wherever a canonical record exists. Only
the values a memory query filters or aggregates on are mirrored;
joining three tables on every point-in-time retrieval would make
retrieval unusable, and that is the whole reason those columns exist.

**Context is decision-time only.** A test compares the context field
list against the outcome columns and fails if a realised return, MFE,
MAE or attribution ever appears wearing a context label. Context
versions separately (`ctx-v1`) from memory (`v1`), because context
definitions and aggregation rules change for different reasons — one
number covering both would make "why did this memory change"
unanswerable.

---

## 6. Experience quality

| Quality | n | Meaning |
|---|---:|---|
| `EXPERIMENTAL` | **4,311** | complete, from a model nobody promoted |
| `INCOMPLETE` | 2,199 | no measured outcome, or no attribution |
| `VALIDATED` | **0** | complete and production-facing |

Zero validated experience is the honest consequence of Phase 18: no
model passes the quality gate, so nothing production-facing exists to
remember. Experimental experience is **kept** (§6) and **never pooled**
— `experimental_count` appears on every pattern.

Incomplete experience is kept too, with its reasons recorded, and never
enters a point-in-time result.

---

## 7–9. Expectation, outcome, attribution linkage

Expectation is what the original system actually produced — never
reconstructed. Outcome and attribution are referenced by natural key;
only labels and the evidence *count* travel, so a consumer knows how
much backs a claim without the text being stored twice.

Primary and contributing errors stay distinct, because "was the main
cause" and "was involved" are different claims.

### Classification — attribution-driven, not profit-driven

| Class | n |
|---|---:|
| `MIXED` | **1,540** |
| `UNSUCCESSFUL` | 1,102 |
| `EXPECTED_LOSS` | 795 |
| `EXPECTED_WIN` | 386 |
| `UNEXPECTED_LOSS` | 308 |
| `NO_CLEAR_RESULT` | 180 |

**`MIXED` — an error was attributed and it still made money — is the
largest class.** That is §13's exact warning made measurable: 1,540
times the system was right about direction while something in the
reasoning was wrong. Calling those successful would teach it that being
right by accident is being right.

---

## 10–17. The memory types

| Memory | Status |
|---|---|
| **Signal** | live — direction, horizon, strength and confidence filters |
| **Model** | live — 15 cohorts; states it is a record, not a verdict |
| **Event** | live — 4 event types; disclaims causation explicitly |
| **Instrument** | live — warns that one instrument is the easiest place to overfit |
| **Regime** | **defined, empty** — `market_regime` is NULL throughout |
| **Execution** | **defined, empty** — no order has ever been placed |
| **Risk** | signal suppression only; no `risk_decisions` table |
| **Portfolio** | **defined, empty** — no portfolio or position exists |

Each empty one names the tables it is waiting for. The shape exists so
the first real execution, position or regime label has somewhere to go,
and so the absence is visible rather than looking like a clean record.

**Risk memory labels neither side a mistake** (§21). It reports
withheld-but-right beside wrong-calls-not-withheld, so the trade-off
can be studied. A suppression that avoided a loss is the rule working;
one that withheld a correct call has a cost.

---

## 18–20. Patterns, evidence, confidence

Eleven stated families, not a cross product — §51 warns against
precomputing every combination, and at this depth the cross product
would be tens of thousands of cohorts of three.

**610 patterns, 36,311 evidence links, zero patterns without
evidence.** §25 forbids orphaned knowledge and the integrity check
enforces it.

| Quality | n | | Confidence | n |
|---|---:|---|---|---:|
| `WEAK` | **415** | | `INSUFFICIENT_EVIDENCE` | 415 |
| `REQUIRES_REVIEW` | 110 | | `MEDIUM` | 111 |
| `CONFIRMED` | 44 | | `HIGH` | 43 |
| `UNSTABLE` | 41 | | `LOW` | 41 |

A cohort keyed on a missing dimension is **skipped**, not bucketed as
"unknown" — an unknown cohort would be the largest pattern in the
database and would mean nothing.

Memory confidence is kept strictly separate from model confidence and
signal confidence (§27), with disjoint vocabularies and a test
asserting it.

---

## 21–24. Recency, stability, regime dependence, conflict

**Stability**: 525 of 610 patterns report `insufficient_history` — two
sub-periods must each clear 30 experiences, and at 27 days most cannot.
44 are `stable`, 41 `unstable`. A pattern whose sub-periods diverge by
more than 15% is `UNSTABLE`, not "good with noise": an average over a
good period and a bad one describes neither, which is §29's point
exactly.

**Regime dependence** is stored beside the all-regime numbers, never
collapsed.

**Contradictions: 541 surfaced, none resolved.** Two forms — within a
pattern (regimes disagreeing makes it `CONFLICTING` and it refuses to
quote a rate) and between patterns (`find_contradictions()`).

> **A bug the tests caught.** The between-pattern check originally
> compared only `left > 0.5 >= right`, so whether a genuine
> disagreement was reported depended on the order the two patterns
> happened to come out of the grouping. It found 262. Checking both
> orderings finds **541** — it had been missing half of them. A
> contradiction that appears or vanishes with a sort is worse than one
> never reported at all.

---

## 25–27. Versioning, snapshots, point-in-time

Four stamps travel with every experience and export: memory `v1`,
context `ctx-v1`, outcome `v1`, attribution `v1`. A bump writes new
rows beside the old ones; a test compares v1 before and after a v2
build and asserts byte-identity.

**Point-in-time is the phase's whole purpose**, and it is real:

- `memory_as_of(T)` filters on `available_at <= T`;
- it **rebuilds patterns** from the visible experience rather than
  reading the stored table — a stored pattern was aggregated over the
  whole record and carries the future inside its averages;
- an experience with no `available_at` never enters a time query;
- a snapshot taken at T is unaffected by experience added afterwards,
  and a test proves it.

---

## 28–29. Retrieval and similarity

Structured filtering, not vector search. §40 asks to start there, and
there is a stronger reason: an embedding match cannot say *which*
dimensions matched, so a response could not honestly report what it
relaxed.

Relaxation is progressive and **recorded**. "20 similar experiences"
means something very different when similarity was reduced to "any
short signal", so every response names the dimensions it dropped.

Every response carries a summary, supporting ids, sample size, evidence
state, time range and **limitations** — never empty in practice. A
large-sample summary ends *"this describes what happened under these
conditions, not what will happen"*, and a test asserts that sentence
survives.

---

## 30–33. Dashboard, API, database, export

A **"Memorie"** workspace: quality and class breakdowns, pattern
quality, the most-supported patterns, model / event / instrument /
regime memory tables, the accumulation timeline built from
`available_at`, snapshots, and recent experiences.

The page states in prose that a pattern describes co-occurrence rather
than a cause, that experimental experience is never pooled, that regime
memory is empty by data rather than by design, that memory confidence
is a different thing from model and signal confidence, that
contradictions are kept rather than resolved, and that nothing here
modifies a model, threshold, strategy, sizing, risk, execution or
capital figure.

Eight routes as typed functions, all accepting `as_of`. Four tables.
Four export formats.

### One index worth naming

`experience_id` is the join key from pattern evidence and is **not**
the primary key. Without a unique index the integrity check joined
36,311 evidence rows against 6,510 experiences by full scan and took
**over five minutes**; with it, **0.42s**. Found by measuring a query
that hung, not by adding indexes speculatively (§50).

---

## 34. Performance

| Stage | Time |
|---|---|
| Experience build (6,510) | 1.2s |
| Pattern build (610 over 36,311 links) | 3.5s |
| Integrity check | 0.42s |
| `memory_as_of` (full record, rebuilding patterns) | 0.8s |
| Similarity query with relaxation | 0.14s |
| Timeline | 0.04s |

All lookups are loaded once and indexed in memory rather than queried
per row — 6,510 experiences with per-row joins would be an N+1 problem
four times over.

---

## 35–37. Testing

```
Ran 3342 tests
OK (skipped=1)
exit 0
```

**+92 tests.** None suppressed, none deleted, no existing test modified.

| File | Tests |
|---|---:|
| `tests/memory/test_experience.py` | 30 |
| `tests/memory/test_patterns_and_leakage.py` | 62 |

All 23 areas in §58 covered. All eleven adversarial cases in §59:

| Case | Test |
|---|---|
| Future experience leaks into past memory | `test_no_experience_from_after_the_cut_is_returned` |
| Future outcome leaks into a historical snapshot | `test_a_later_experience_cannot_change_an_earlier_snapshot` |
| Duplicate experience | `test_a_duplicate_experience_cannot_be_created` |
| Duplicate pattern | `test_a_duplicate_pattern_cannot_be_created` |
| Small sample becomes high confidence | `test_a_small_sample_can_never_reach_high_confidence` |
| Contradictions averaged into certainty | `test_contradictory_experiences_are_not_averaged_into_certainty` |
| Experimental treated as validated | `test_an_experimental_experience_is_never_marked_validated` |
| Stale pattern treated as current | `test_a_stale_pattern_is_labelled_rather_than_deleted` |
| Deleted history breaks provenance | `test_deleting_evidence_is_visible_rather_than_silent` |
| Methodology change without version | `test_a_memory_version_change_does_not_rewrite_the_old_one` |
| Retrieval returns unsupported conclusion | `test_retrieval_never_returns_a_conclusion_without_a_sample_size` |

**Leakage (§39, §72).** An AST scan asserts the only tables written are
the four memory tables; no module imports a decision-making engine; no
module contains `promote`, `train` or `.fit(`; **no LLM appears
anywhere** in the package; no earlier pipeline script reads the memory
tables; building memory leaves `outcome_measurements` byte-identical.

> **A second bug the tests caught.** `test_patterns_are_recomputed_not_
> read_from_storage` originally compared pattern *counts* and passed
> `9 == 9` for the wrong reason — the fixture holds every condition
> constant, so the same cohorts exist at every date and only their
> sizes differ. It now asserts on aggregated sample size, which is the
> property that actually matters.

---

## 38–39. Security and IBKR safety

No new credentials, secrets, dependencies or inbound surface. Standard
library only. No IBKR or account information reaches memory — there are
no orders to remember.

**`git diff HEAD` is empty** for `src/execution`, `src/risk`,
`src/portfolio`, `src/paper`, `src/brokers`, `src/pointintime`,
`src/modeling`, `src/outcomes` and `src/attribution`.

`audit_live_safety.py`: **16 of 16 PASS**.

---

## 40–44. Documentation, files, migrations

**Created (9):** `src/domain/memory_models.py`,
`src/data_access/memory_schema.py`, `src/memory/__init__.py`,
`experience.py`, `patterns.py`, `retrieval.py`, `api.py`,
`scripts/build_memory.py`, two test modules, and two documents.

**Modified (2):** `src/dashboard.py` (collector, workspace, nav,
router), `.github/workflows/pipeline.yml` (stage 13 of 14).

**Removed:** none.

**Migrations:** four additive tables and seven indexes, created
idempotently on first use. No existing table altered, no column
dropped, no row rewritten.

---

## 45. Remaining issues

### TD-05 is closed — the scheduled pipeline ran

Outstanding since Phase 18 and reported unverified in three
consecutive final reports. It fired while this phase was being
committed:

```
run      2026-09-06T06:41:13Z   event: schedule   conclusion: success
duration 18 minutes (06:41 -> 06:59 UTC)
stages   13 of 13 success — including the price cache, outcome
         measurement and error attribution
```

Every stage Phases 18, 19 and 20 added ran on a schedule, unattended,
and passed. The chain is now proven end to end by the mechanism rather
than by a local rehearsal.

Two caveats worth stating. It ran the **13-stage** version — Phase 21's
memory stage was added after the run, so stage 14 has not yet executed
on a schedule. And a single green run proves the wiring, not that it
will stay green; the next one is Wednesday 02:00 UTC.

### Still open

1. **Zero validated experience.** No model is promoted, so everything
   is experimental. Correct, and a real limit on what memory can say.
2. **27 days of history.** 525 of 610 patterns cannot have their
   stability assessed. Time is the only fix.
3. **Regime memory is empty** — `market_regime` is NULL throughout.
4. **Execution, portfolio and risk memory are defined and empty**
   because their source tables do not exist.
5. **Memory confidence is uncalibrated.** Nothing has checked how often
   a pattern holds on data it was not built from. Deliberate — ordinal
   labels, not probabilities — but it means the field cannot yet weight
   anything.

---

## 46. Future learning readiness

Phase 22 inherits memory that is **evidence-linked** (36,311 links,
zero orphans), **point-in-time safe** (`memory_as_of` rebuilds rather
than reads), **versioned** four ways, **idempotent**, **honest about
weakness** (68% of patterns quote no rate), and **contradiction-
preserving** (541 surfaced, none resolved).

The four capabilities an Experiment Engine actually needs:

- a hypothesis can **name its evidence** — every pattern lists the
  experiences behind it;
- a claim can be **checked for stability** — sub-period breakdowns are
  stored;
- a backtest can ask **what the system knew at time T** without seeing
  its own future;
- a disagreement stays **visible** rather than being averaged into a
  number that describes neither side.

---

## 47. Next phase

Phase 22 — **Experiment Engine**: turning memory observations into
explicit, controlled hypotheses. Deliberately not started here.

---

# READY FOR PHASE 22

**The standing watch is discharged.** The scheduled pipeline ran on
2026-09-06 at 06:41 UTC and all thirteen stages passed. TD-05 had been
open since Phase 18 and reported unverified in three final reports; it
is now verified by the mechanism rather than by rehearsal.

One item replaces it, smaller: Phase 21's memory stage was added after
that run, so **stage 14 has not yet executed on a schedule**. The next
run is Wednesday 02:00 UTC.

Phase 21 built the memory layer and nothing else. It changes no model,
strategy, threshold, feature, sizing, risk limit, execution setting or
capital figure — §63 lists all eight and an AST scan enforces every
one.

It remembers 6,510 experiences and refuses to describe a regularity in
415 of the 610 patterns it found. It surfaces 541 contradictions
without resolving one of them. It has zero validated experience,
because no model has earned promotion, and it says so rather than
quietly counting research as production.

Memory built from evidence. Learning comes later.
