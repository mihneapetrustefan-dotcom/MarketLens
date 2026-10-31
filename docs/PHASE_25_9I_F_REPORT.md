# Phase 25.9I-F — Completion Report

## 1. Phase status

**COMPLETE.**

Event intelligence now reaches a position thesis through a canonical,
lineage-complete path and stops there. Every output carries
`is_executable = False`, no broker write exists, and the three things this
layer must never invent — a target, a horizon, an exit fraction — have no field
to carry them.

---

## 2. Git state

| | |
|---|---|
| Branch | `ibkr-paper-validation-fixes` |
| Baseline HEAD | `05584db` (Phase 25.9I-C completion report) |
| Final HEAD | `1d1e0df` |
| Working tree at start | clean |
| Commits | 1 |
| Diff | **14 files changed, 6522 insertions(+), 0 deletions(-)** |

**Modified: 1 file, +8 / −0.**

| File | Change |
|---|---|
| `tests/adaptive/test_freshness_and_legacy.py` | persistence guard list extended, with the reason (§17) |

**New: 12 files.**

| File | Purpose |
|---|---|
| `src/domain/event_position_models.py` | event↔position contracts |
| `src/adaptive/event_sources.py` | read-only canonical event reader, instrument resolution |
| `src/adaptive/event_relevance.py` | relation + thesis relevance |
| `src/adaptive/event_assessment.py` | direction, materiality, confidence, conflict, urgency |
| `src/adaptive/event_reassessment.py` | the engine, the ThesisEvidence adapter, advisories |
| `src/adaptive/event_thesis_repository.py` | assessments + consumption ledger |
| `src/data_access/adaptive_event_schema.py` | three additive tables |
| `scripts/adaptive_event_reassessment.py` | shadow CLI, 11 scenarios, real-data pass |
| `tests/adaptive/test_event_reassessment.py` | 75 tests |
| `tests/adaptive/test_event_safety.py` | 32 tests |
| `tests/adaptive/test_event_boundary.py` | 37 tests |
| `docs/PHASE_25_9I_F_ARCHITECTURE.md`, `docs/PHASE_25_9I_F_REPORT.md` | documentation |

---

## 3. Baseline

Recorded **before** any change, at `05584db`:

```
PYTHONPATH=src python -m unittest discover -s tests -t . -b
Ran 4816 tests in 651.266s
OK (skipped=1)
```

| tests | pass | fail | errors | skipped | runtime |
|---|---|---|---|---|---|
| 4816 | 4815 | 0 | 0 | 1 | 651.3 s |

---

## 4. Repository / Event Intelligence findings

The audit's main result: **almost everything this phase needed already
existed**, and one thing that looked available was a trap.

| Concept | Found | Verdict |
|---|---|---|
| Canonical event | `CanonicalEvent` + `canonical_events` (**581 rows**) | reused |
| Article→event dedup | fusion clustering: **846 reports → 581 events** | reused; not rebuilt |
| Event taxonomy | `EventType` (30+), `EventCategory` (6) | reused |
| Issuer directness | `ParticipationRole` primary/secondary/affected/referenced | reused |
| Corroboration | `CorroborationState`, `count_independent_sources` | reused |
| Source independence | `SourceLineage.is_independent()` — only `ORIGINAL_REPORT` | reused |
| Lifecycle / revision | `EventLifecycleState`, `VALID_TRANSITIONS`, `EventRelationType` | reused |
| Entity relationships | `RelationshipType` + mandatory `ProvenanceKind` | reused |
| Direction vocabulary | `ImpactDirection` (5 members — exactly §8's) | reused |
| Materiality vocabulary | `ImpactLevel` (4 members — exactly §9's) | reused |
| Decomposed confidence | `EventConfidence` | pattern reused |
| Thesis consumer | 25.9I-E `ThesisEvidence`, `reassess()` | reused unchanged |
| Market-data request | 25.9I-C `DataAttentionRequest` | reused |
| Feature flag | `event_driven_position_reassessment_enabled` **already existed**, OFF, depending on `position_thesis_enabled` | reused |

### The trap

`ImpactProfile.dimensions.direction` looked like a ready-made event direction.
It is **derived from abnormal returns after the event**
(`src/impact/engine.py:466`). Using it would read the price reaction back into
its cause — the leakage §21 forbids — and is future information relative to the
assessment. **Not used.** An AST test asserts no module in this phase imports
`src.impact` for anything but the two enums.

### Gaps that justified new code

1. **No canonical-event reader existed.** `fusion_schema.py` has an initializer
   and no repository; `EventRepository` reads the *pre-fusion* table.
2. **`event_instruments` holds 0 rows** — the instrument path is a join through
   `companies → securities → instruments`.
3. **Materiality** — 25.9I-E's docstring explicitly says *"25.9I-F owns event
   materiality"*.
4. **Position-specific relevance, urgency, conflict, consumption** — none
   existed.

### What was NOT duplicated

No second event pipeline, entity resolver, deduplicator, clusterer,
corroboration scorer, taxonomy or thesis engine. Asserted by test
(`TestNoSecondPipeline`).

---

## 5. What was implemented

Event→position mapping (DIRECT/INDIRECT/SECTOR/MARKET_WIDE/UNKNOWN) ·
thesis relevance separate from instrument relevance · direction from canonical
subtypes only · materiality as an evidenced band · four-axis confidence ·
corroboration with independence stated separately · novelty per (event,
revision, thesis) · event-revision lineage · freshness/staleness · scheduled-
event advisories · urgency · the `Event → ThesisEvidence` adapter ·
multi-position reports · COLD/WARM/HOT **requests** · three additive tables ·
a shadow CLI with 11 scenarios and a real-data pass · 144 tests.

---

## 6. What worked

Actual engine output, `python scripts/adaptive_event_reassessment.py`.

- **Dedup is structural.** Scenario E: 7 reports, **one** canonical event, one
  reaction; the re-crawl returned `UPDATED` and *"no second reaction"*.
- **A material revision does re-trigger.** Scenario I: rumour →
  `mat=medium, dir=unknown`; filing → `MATERIAL_REVISION`,
  `mat=high, dir=positive`, linked to its predecessor.
- **Conflict is not voted on.** Scenario F: `conflict=conflicted`,
  `strengthens=False`, `target_reestimation=False` — the positive headline did
  not win on count.
- **Cooldown never blocks safety.** Scenario K: revision 10 minutes ago, 1
  today, cooldown engaged → still `CRITICAL` → `HOT` → `FULL_EXIT`.
- **Stale events are not urgent.** Scenario J: 9-day-old event →
  `urgency=normal`, no data request.
- **Multi-position stays per-position.** Scenario H: 3 theses, 3 independent
  assessments, aggregate notional 3000.0 as a **diagnostic**, `executable=False`.
- **Real corpus.** 581 events assessed: 123 offered evidence, 458 declined,
  **0 executable outcomes**, 0 billable market-data requests.

---

## 7. What did not work

**Mandatory. Nothing here is hidden because it was later fixed.**

### 7.1 `affected_assumptions` reported nothing for ~95% of real events
**Symptom:** a test asserting a matched assumption was returned got `()`.
**Root cause:** the property filtered out `UNKNOWN` impact — but an assumption
touched by a *directionless* event has still been touched. Since only 27 of 581
events carry a direction, this silently reported "nothing affected" for almost
the whole corpus.
**Fix:** exclude only `NOT_RELEVANT`.
**Status:** resolved. Caught by a test, not by reading the code.

### 7.2 A rumour scored the same materiality as a confirmed filing
**Symptom:** scenario I's rumour returned `materiality=HIGH`.
**Root cause:** `ESTABLISHED_LIFECYCLE` added +1 for a filing but nothing
subtracted for a rumour, so the first unsourced report moved the position and
the confirmation added nothing.
**Fix:** `rumored` scores −1. The rumour now reads MEDIUM and the filing HIGH.
**Status:** resolved.

### 7.3 `ExpectedHorizon(7)` means seven TRADING days, not calendar days
**Symptom:** pre-event advisories returned `horizon_days_remaining=None`.
**Root cause:** `ExpectedHorizon`'s first positional field is `trading_days`;
reading `calendar_days` alone yields None for 25.9I-E's own fixtures, silently
dropping the horizon from every advisory.
**Fix:** `_horizon_end()` prefers `expires_at`, then `calendar_days`, then
converts `trading_days` at 7/5 — an approximation, labelled as one, used only
to decide whether to *suggest* a review.
**Status:** resolved, with a regression test.

### 7.4 The cooldown test asserted a restraint that does not exist by default
**Symptom:** `test_a_cooldown_does_restrain_a_repeated_upside_revision` failed.
**Root cause:** **the test was wrong.** `RevisionPolicy` defaults to
`cooldown=0` and `max_revisions_per_day=None` — inert, because no calibrated
value exists. The engine was right.
**Fix:** the test configures a cooldown explicitly, and a companion test proves
the *same* cooldown still does not block invalidation.
**Status:** resolved.

### 7.5 The closed-thesis fixture was not a closed thesis
**Symptom:** `ValueError: a CLOSED thesis cannot still hold exposure`.
**Root cause:** the fixture set `status=CLOSED` while leaving
`remaining_fraction=1.0`. 25.9I-E was right to refuse it.
**Fix:** the fixture closes the position properly. **The contract was not
relaxed.**
**Status:** resolved.

### 7.6 The reader dropped the only evidenced source of direction
**Symptom:** the first real-data pass returned `direction=unknown` for all 200
events.
**Root cause:** `EventEvidenceBundle` had no `subtype` field, so the canonical
`guidance_cut` / `earnings_beat` subtypes never reached the assessor. The 4.7%
of the corpus that *can* state a direction was being discarded.
**Fix:** `subtype` added to the bundle, read by the reader, and folded into the
revision key — a subtype appearing is new information about what the event
said. The pass now returns 26 positive and 1 negative.
**Status:** resolved.

### 7.7 Unclosed file handles in the boundary tests
**Symptom:** `ResourceWarning: unclosed file` across the AST tests.
**Root cause:** bare `open(...).read()`.
**Fix:** context managers throughout.
**Status:** resolved.

### 7.8 Entity relationships are not persisted — UNRESOLVED
**Symptom:** INDIRECT relations can never be established in production.
**Root cause:** there is no `entity_relationships` table; `RelationshipGraph`
is built in memory from relationships nobody stores.
**Fix:** none attempted. The classifier returns UNKNOWN rather than guessing at
a supply chain, and the INDIRECT path is exercised by a test with a supplied
graph.
**Status:** **OPEN by design.** A limitation of the data, not the code.

### 7.9 `event_time` is NULL for every canonical event — UNRESOLVED
**Symptom:** occurrence time is unavailable corpus-wide.
**Fix:** fall back to `first_reported_at` and report
`occurrence_is_inferred=True` plus a `MISSING_EVENT_TIME` concern. That concern
alone does **not** trigger a data-integrity review — it is true of all 581
events and flagging the whole corpus would mean nothing.
**Status:** **OPEN**, belongs to the ingestion layer.

### 7.10 Source independence is never established — UNRESOLVED
**Symptom:** `independent_source_count = 0` for all 581 events.
**Fix:** `source_independence_known` is a separate boolean, and the contract
**refuses** a bundle claiming a count while independence is unknown. Report
count alone cannot raise materiality.
**Status:** **OPEN**, belongs to fusion.

---

## 8. What was deferred

| Deferred | Later owner |
|---|---|
| Numeric target estimation | **25.9I-F.5** Signal & Horizon Intelligence |
| Horizon estimation | **25.9I-F.5** |
| Dynamic partial-exit sizing | **25.9I-F.5** / a sizing layer |
| Calibrated materiality thresholds | 25.9I-F.5 / 25.9I-G |
| Assumption→event-type mapping derived from text | 25.9I-F.5 |
| Persisted entity relationships (INDIRECT) | entity layer |
| `event_time` at ingestion | news/event ingestion |
| Source-independence lineage | fusion |
| Realtime market-data activation | **25.9I-C**, operationally pending |
| Counterfactual attribution | **25.9I-G** |
| Paper execution, broker-native stops | **25.9I-H** |

---

## 9. Positive event example (Scenario A)

```
event ce-fixture  guidance "Guidance raised"  subtype=guidance_raised
instrument inst-aa        thesis: entry 100, target 108-112, horizon 7d, day 4
relation        direct (named as a primary participant)
thesis relevance high  (affects stated assumption "margin expansion")
direction       positive   (canonical subtype states it)
materiality     high (0.8)  conflict consistent_positive
corroboration   unconfirmed | source independence established: NO
urgency         elevated
ThesisEvidence  eve-e1b89615e7d2b0af867f  direction=positive materiality=0.8
                references: event, revision, assessment, report (4)
recommendation  HOLD
target changed  False        horizon changed  False
partial fraction None
market data     WARM (requested, not activated)
executable      False
TARGET_REESTIMATION_REQUIRED = True   HORIZON_REESTIMATION_REQUIRED = True
```

---

## 10. Positive event + explicit external estimate (Scenario B)

**The same event.** The only difference is that the caller supplied
`proposed_target=117-122`, `proposed_horizon=12d`, `exit_fraction=0.40`.

```
recommendation  PARTIAL_EXIT
target changed  True   -> 117.0-122.0
horizon changed True   -> 12 calendar days
partial fraction 0.40
executable      False
```

The event assessment is **byte-identical to scenario A** — same
`assessment_id`, same evidence id, same direction, same materiality. And:

```
assertNotIn("117", str(assessment.describe()))   # passes
assertNotIn("122", str(assessment.describe()))   # passes
```

**The event layer did not produce 117-122, 12 days or 40%.** It cannot: the
three fields do not exist on `PositionEventImpactAssessment` (§17–19).

---

## 11. Negative invalidation example (Scenario C)

```
direction       negative (subtype guidance_cut)   materiality high (0.8)
relation        direct        conflict consistent_negative
invalidates_thesis  TRUE
urgency         CRITICAL      market data  HOT (requested)
recommendation  FULL_EXIT     exit_fraction 1.0
executable      False         broker writes 0
```

> The `1.0` is **25.9I-E's definition of a full exit**, not an invented
> fraction. I-F passed `exit_fraction=None`; verified directly.

Invalidation required **all** of HIGH materiality, NEGATIVE direction, DIRECT
relation and a non-conflicted reading. A claim without those is refused with
*"the evidence does not meet the bar for invalidation"*.

---

## 12. Minor news example (Scenario D)

```
event product_launch "Company refreshes mobile app icon"
relation direct | thesis relevance NOT_RELEVANT | direction unknown
materiality LOW (0.2) | urgency normal
evidence        NONE OFFERED
recommendation  none      market data  none
SKIPPED: the event does not warrant offering evidence to this thesis
```

No revision chasing, no HOT, no target change.

---

## 13. Duplicate / corroboration example (Scenario E)

```
first sighting : 5 reports, ONE canonical event -> ONE assessment, ONE evidence
re-crawl (+2)  : 7 reports, same event -> consumption=UPDATED
                 SKIPPED: "already assessed ce-dup (updated); no second reaction"
independence established: NO  ->  7 reports did NOT raise materiality
```

Five articles produced one reaction, not five.

---

## 14. Conflicted evidence example (Scenario F)

```
direction positive (subtype) + opposing NEGATIVE evidence
conflict_state          CONFLICTED
strengthens_thesis      False
target_reestimation     False
urgency                 ELEVATED   (uncertainty raised)
recommendation          HOLD
```

Not resolved by majority: a 3-positive / 1-negative split is still CONFLICTED.

---

## 15. Scheduled event example (Scenario G)

```
earnings in 20.0h | recommend_thesis_review True | recommend_risk_review True
requested market data WARM | executable False
```

`PreEventRiskAdvisory` has **no** `exit_fraction`, `reduce_by`, `quantity`,
`stop_price` or `action` field — asserted by test. It cannot express a trade.

---

## 16. Multi-position event (Scenario H)

```
one sector event, three theses in "semiconductors"
inst-aa  sector | relevance medium | negative | medium(0.5) | HOLD | not executable
inst-bb  sector | relevance medium | negative | medium(0.5) | HOLD | not executable
inst-cc  sector | relevance medium | negative | medium(0.5) | HOLD | not executable
aggregate notional 3000.0   (a DIAGNOSTIC)     report executable: False
```

`MultiPositionEventReport` has no `recommendation`, `action`, `exit_fraction` or
`portfolio_action` attribute — "sell all tech" is unspellable, not merely
discouraged.

---

## 17. Target fabrication safety

| Check | Result |
|---|---|
| Material positive event, no estimator → target moved? | **No** |
| `target_reestimation_required` | **True** |
| Fields matching `target*` on the assessment | **none exist** |
| External estimate supplied → target moved? | Yes, to the caller's numbers |

## 18. Horizon fabrication safety

| Check | Result |
|---|---|
| Material positive event → horizon extended? | **No** |
| `horizon_reestimation_required` | **True** |
| Fields matching `horizon`/`days` on the assessment | **none exist** |
| External horizon supplied → applied? | Yes, 12 calendar days |

## 19. Partial-exit fraction safety

| Check | Result |
|---|---|
| Material negative event → fraction produced? | **None** |
| `partial_exit_review_required` | **True** |
| Fields matching `fraction`/`quantity`/`size` | **none exist** |
| `ThesisPolicy.partial_exit_fraction` default | **None** (unchanged) |
| External fraction supplied → applied? | Yes, 0.40 |

---

## 20. Event novelty / consumption

| Sequence | Status | Reassessed? |
|---|---|---|
| first sighting | `NEW` | **yes** |
| identical re-read | `ALREADY_ASSESSED` | no |
| +2 syndicated copies | `UPDATED` | **no** |
| rumour → confirmed filing | `MATERIAL_REVISION` | **yes**, linked |
| contradiction appears | `MATERIAL_REVISION` | yes |
| closed thesis | `IGNORED` | no |

Scenario E: **2 sightings → 1 reassessment.** Scenario I: **2 sightings → 2
reassessments**, the second carrying `previous_assessment_id`.

---

## 21. As-of / leakage

| Bound | How enforced |
|---|---|
| Publication | SQL `first_reported_at <= as_of` |
| Contract | `EventEvidenceBundle` raises on any timestamp after `as_of` |
| Event revisions | a later revision does not alter an earlier assessment — replay yields the identical id, materiality and direction |
| Corroboration | later reports cannot strengthen an earlier reading |
| Future price | direction/materiality identical at price 50.0 and 200.0 |
| Impact engine | **not imported** — its direction comes from abnormal returns |
| Deterministic replay | same inputs → same assessment id, evidence id, ordering |

**Physical truncation test:** the same moment re-read from a corpus that also
contains later rows produces a byte-identical assessment id.

**Ordering** (§48): known occurrence → first report → canonical id. Undated
events sort **last**. Reversing the input list changes nothing.

---

## 22. Persistence / lineage

Three tables, append-only, content-derived ids, `INSERT OR IGNORE`.
Re-saving writes **0** new rows (verified). `record_consumption` twice → 0.

Lineage on every `ThesisEvidence`:

```
references = ("event:ce-…", "revision:rev-…", "assessment:pea-…", "report:rep-…")
source     = "event_intelligence:ce-…"
```

No anonymous evidence. `previous_assessment_id` chains revisions;
`assessment_chain()` walks them in order. `is_executable` stored as a literal
**0**; `executable_rows()` returns 0.

No `order_id`, `quantity`, `exit_fraction`, `target_zone`, `stop_price`,
`password`, `token` or `account_id` column exists in any of the three tables —
asserted.

---

## 23. Market-data requests

| Scenario | Requested |
|---|---|
| A, B, E, F, I positive/material | **WARM** |
| C, K invalidation | **HOT** |
| D minor, H sector, J stale | **none** |

```
billable market-data requests generated by I-F : 0
subscriptions activated by I-F                 : 0
market-data states ACTIVATED by I-F            : 0  (it only requests)
```

§31 honoured: a HOT **request** does not imply realtime availability. The
request object has no `granted` or `entitlement` attribute, and this account has
never served a realtime marker.

---

## 24. Feature flags

| Flag | Default |
|---|---|
| `event_driven_position_reassessment_enabled` | **False** |
| `position_thesis_enabled` | **False** |
| `dynamic_exit_enabled` | **False** |

Dependency already declared in `FLAG_DEPENDENCIES`:
`event_driven_position_reassessment_enabled → position_thesis_enabled`.
`suppression_reason()` reports suppression up front rather than letting it be
discovered at runtime. With every flag forced on, `enables_broker_writes` and
`enables_live_trading` remain False.

---

## 25. Production behaviour

| Path | Changed? |
|---|---|
| capture / Portfolio / Risk / OrderIntent / Execution / IBKR / subscriptions | **No** |

No module under `src/` outside `src/adaptive/` and `src/domain/` imports any
Phase 25.9I-F module. The one modified file is a test.

---

## 26. Broker safety

| | |
|---|---|
| Broker writes attempted | **0** |
| Broker writes succeeded | **0** |
| Paper orders caused | **0** |
| Live orders | **0** |
| Billable market-data requests | **0** |

No module imports `src.execution`, `src.trading` or `src.portfolio`; none calls
`place_order`, `submit_order`, `cancel_order`, `modify_order`, `reply`,
`market_snapshot`, `subscribe` or `init_brokerage_session`; the string
`OrderIntent` appears nowhere in this phase.

---

## 27. Test results

| Suite | Tests | Result | Runtime |
|---|---|---|---|
| **I-F** `test_event_reassessment` | 75 | **OK** | 0.02 s |
| **I-F** `test_event_safety` | 32 | **OK** | 0.01 s |
| **I-F** `test_event_boundary` | 37 | **OK** | 1.52 s |
| **I-F total** | **144** | **OK** | — |
| I-E position thesis | 48 | **OK** | 0.01 s |
| I-D capital allocation | 55 | **OK** | 0.03 s |
| I-C billing / attention / boundary | 64 / 51 / 37 | **OK** | 0.03 / 0.01 / 11.5 s |
| I-B dynamic universe | 45 | **OK** | 1.57 s |
| Adaptive suite | 616 | **OK** | 25.3 s |
| Events | 81 | **OK** | 0.09 s |
| Fusion | 47 | **OK** | 0.04 s |
| Entities | 84 | **OK** | 0.20 s |
| Market data | 57 | **OK** | 0.05 s |
| Portfolio | 273 | **OK** | 0.77 s |
| Risk engine | 39 | **OK** | 0.01 s |
| Capture / session | 126 | **OK** | 338.4 s |
| Research integrity | 39 | **OK** | 13.7 s |
| Trading readiness | 44 | **OK** | 3.96 s |
| Live safety | 57 | **OK** | 0.03 s |
| **FULL SUITE** | **4960** | **OK (skipped=1)** | **653.0 s** |

```
Ran 4960 tests in 653.025s
OK (skipped=1)
```

**4960 − 4816 = 144**, exactly the new I-F tests. Zero failures, zero errors,
zero regressions.

---

## 28. Performance

| Operation | Time |
|---|---|
| 1 event × 1 thesis | 0.130 ms |
| 10 events × 10 theses (100 pairs) | 5.74 ms |
| 100 events × 31 theses (3,100 pairs) | 156.3 ms |
| Sector event across 31 theses | 3.15 ms |
| Duplicate lookup over 2,000 ledger rows | 0.080 ms |
| Persistence lookup (`load_history`) | 0.011 ms |
| Canonical reader build (389 entities) | 5.49 ms |
| Read all 581 canonical events | 189.4 ms |

---

## 29. D20 status

| | |
|---|---|
| Executed? | **NO** |
| Inspected? | **NO** |
| Inferred? | **NO** |
| Consumed? | **NO** |
| Hypothesis changed? | **NO** |
| anchor-v2 changed? | **NO** |
| Protected window changed? | **NO** |

`scripts/validate_d20_reversal.py` was not run. No new module imports anything
under `src/research`, and none mentions `anchor-v2`, `return_60d`,
`protected_ledger` or `validate_d20` — asserted over every new module and the
script. No threshold was tuned against any protected outcome; every policy value
is either read from canonical data or `None`.

---

## 30. Scope-creep review

| Check | Result |
|---|---|
| Unrelated files touched | none |
| Portfolio / Risk / Execution / capture / IBKR changes | none |
| Market-data production changes | none |
| Model promotion | none |
| Protected research | untouched |
| Automatic subscription behaviour | none |
| New external dependencies | **none** (stdlib only: `hashlib`, `json`, `sqlite3`) |

1 file modified (a test), 12 added.

---

## 31. Known limitations / technical debt

1. **Materiality is a band, not a calibration.** The 0.80/0.50/0.20 midpoints
   are documented plain numbers with no evidence behind them.
2. **Direction is available for ~4.7% of events** — 27 of 581 carry a
   directional subtype. The other 95% are honestly UNKNOWN.
3. **Source independence is never established** (0 of 581), so
   `INDEPENDENTLY_CORROBORATED` is unreachable on this corpus.
4. **`event_time` is NULL for all 581 events**; occurrence is inferred from
   first report.
5. **No entity relationships are persisted**, so INDIRECT cannot be established
   in production.
6. **Thesis relevance needs a caller-supplied assumption mapping.** Without one
   every answer is UNKNOWN — deliberately, since assumptions are free text.
7. **No target model, no horizon model, no sizing model.** This phase refuses to
   fill those gaps; 25.9I-F.5 owns them.
8. **Realtime market data is still operationally unverified** (25.9I-C), so a
   HOT request cannot become execution-grade freshness.
9. **Lifecycle is `reported` for all 581 events** — the rumour→confirmed
   transition is exercised by fixtures, not yet by production data.
10. **Nothing is wired into production.** Everything is shadow.

---

## 32. Readiness for 25.9I-F.5 — Signal & Horizon Intelligence

**YES.**

Available to it now:

- `PositionEventImpactAssessment` — direction, materiality band + score,
  four-axis confidence, conflict state, urgency, relation, thesis relevance,
  affected assumptions, staleness, full lineage.
- `EventReassessmentRequest` — what needed looking at, and how urgently.
- `target_reestimation_required` / `horizon_reestimation_required` /
  `partial_exit_review_required` — **explicit, unanswered demands** for exactly
  the three numbers F.5 exists to supply.
- The consumption ledger and revision chains.
- A proven seam: `reassess(..., proposed_target=…, proposed_horizon=…,
  exit_fraction=…)` already works (Scenario B) and is independent of event code.

## 33. Readiness for 25.9I-G

**PARTIAL.** The lineage is complete —
`event → assessment → evidence → thesis revision → recommendation` is walkable
in both directions. What is missing is what G needs to measure *against*: a
stronger signal/horizon layer, enough shadow sessions, and operational
market-data evidence. No outcomes exist to attribute yet.

## 34. Readiness for 25.9I-H

**NO.** Realtime has never been verified on this account, there is no target
model, no horizon model and no sizing model, and no shadow session history
exists. Paper execution is not authorised.

---

## 35. Final verdict

> **PHASE 25.9I-F = COMPLETE**
>
> 12 new files, 1 documentation-only test modification, 144 new tests, full
> suite **4960 passing** against a **4816** baseline — no regressions. Event
> intelligence reaches a position thesis through a canonical, lineage-complete,
> as-of-bounded path and stops at a non-executable recommendation. The existing
> event pipeline was reused in full; no second pipeline, resolver, deduplicator
> or taxonomy was created. The three prohibitions are enforced structurally —
> the fields do not exist — and proven by running the same event with and
> without an external estimator. Invalidation requires converging evidence, and
> a fully engaged cooldown still cannot suppress it. Measured on 581 real
> canonical events: 123 offered evidence, 0 executable outcomes, 0 broker
> writes, 0 billable market-data requests.

---

*No broker order was created, modified or cancelled. No subscription was
purchased. No billable market-data request was made. No capture behaviour
changed. The protected D20 experiment was not touched.*
