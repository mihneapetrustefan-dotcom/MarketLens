# Phase 25.9I-F — Event-Driven Position Reassessment

> ## EVENT INTELLIGENCE DOES NOT CONTROL BROKER ORDERS.
>
> ## EVENT EVIDENCE MAY JUSTIFY A REASSESSMENT.
> ## IT MAY NOT INVENT A TARGET, HORIZON OR EXIT FRACTION.

> `event_driven_position_reassessment_enabled` is **OFF** and depends on
> `position_thesis_enabled`, which is also OFF. Every output carries
> `is_executable = False`. Portfolio remains authoritative, Risk remains the
> hard gate, Execution remains the only route to IBKR.

Written 2026-09-30.

---

## A. The question this layer asks

Not *"is this news good or bad"* — that is what a headline reader asks. It asks:

> **Does this change the reason we own this?**

Those are different questions with different answers. A company can report an
excellent quarter that has nothing to do with the deleveraging thesis the
position was opened under. The event is positive; the thesis is untouched.

```
canonical event → relevance → assessment → ThesisEvidence
                → 25.9I-E reassessment → recommendation
                → a market-data REQUEST → recorded
```

It stops at the recommendation.

---

## B. Event ≠ article — and the pipeline already knew

Fusion collapsed **846 reports into 581 canonical events** before this layer
sees anything. Five articles about one acquisition arrive as **one**
`canonical_event_id`, so one reaction is the default rather than something
this phase must suppress. 442 events carry one report; 139 carry two to six.

What this phase *does* suppress is the same canonical event arriving again
tomorrow — see §F.

**Nothing was rebuilt.** Ingestion, normalisation, entity resolution,
extraction, clustering and corroboration all run today and are untouched.

---

## C. What was reused rather than redefined

Audited 2026-09-30. Almost every vocabulary needed already existed:

| Concept | Canonical home | Used for |
|---|---|---|
| `ImpactDirection` | `impact_models` | positive/negative/**mixed**/neutral/**unknown** — exactly the five §8 asks for |
| `ImpactLevel` | `impact_models` | low/medium/high/unknown — exactly the four §9 asks for |
| `ParticipationRole` | `event_models` | primary / secondary / affected / **referenced** — issuer directness |
| `CorroborationState` | `fusion_models` | `MULTI_SOURCE` ≠ `INDEPENDENTLY_CORROBORATED` |
| `SourceLineage.is_independent()` | `fusion_models` | only `ORIGINAL_REPORT` counts |
| `EventLifecycleState` | `fusion_models` | rumoured → reported → confirmed → … |
| `RelationshipType` + `ProvenanceKind` | `entity_models` | competitor/supplier/customer, with mandatory provenance |
| `EventType` / `EventCategory` | `events/taxonomy` | the canonical taxonomy |
| `ThesisEvidence`, `reassess()` | 25.9I-E | the consumer |
| `DataAttentionRequest`, `MarketDataState` | 25.9I-C | the request, not the activation |

Defining a second `EventDirection` beside `ImpactDirection` would have produced
two enums with identical members and a mapping function whose only job was to
lose information at the seam.

---

## D. Where direction may and may not come from

**May**, and only these two:

1. **The canonical subtype** the existing extractor already derives by
   deterministic rule — `earnings_beat`, `earnings_miss`, `guidance_raised`,
   `guidance_cut`. Reading those is not inventing a rule; the rule has been in
   `src/events/extractor.py` since Phase 4.
2. **An explicit direction supplied by the caller** — a fixture, or a future
   classifier that can justify itself.

Everything else is **UNKNOWN**. There is no "acquisition = bullish" table,
because an acquisition is good for the target and usually bad for the
acquirer's balance sheet, and which of those we hold is exactly what a headline
does not say.

> Measured: **27 of 581** events carry a directional subtype. So direction is
> genuinely unknown for ~95% of this corpus, and the engine says so 554 times
> rather than guessing 581 times.

**May not:** `ImpactProfile.dimensions.direction`. `src/impact/engine.py`
derives that from **abnormal returns after the event**. Using it would read the
price reaction back into its cause — the leakage §21 forbids — and would be
future information relative to the assessment. **No module in this phase
imports `src.impact` except for the two enums**, asserted by AST test.

---

## E. Relevance: two questions, kept apart

| | |
|---|---|
| **instrument relevance** | is the event about something we hold? |
| **thesis relevance** | does it touch the reason we hold it? |

Relations are established only by evidence:

- **DIRECT** — the event *names* an entity resolving to this instrument
  (PRIMARY/SECONDARY/AFFECTED). **REFERENCED does not qualify**: appearing in
  the reporting is not participation.
- **INDIRECT** — only through a **sourced** `EntityRelationship`. An *inferred*
  one is refused.
- **SECTOR**, **MARKET_WIDE**, **UNKNOWN** otherwise.

Ticker substring matching is never used.

> **Measured limitation:** this repository persists **no entity relationships**
> at all. So INDIRECT cannot currently be established in production, and the
> classifier returns UNKNOWN rather than reaching for a plausible guess about a
> supply chain.

### UNKNOWN is not NOT_RELEVANT

`PositionThesis` assumptions are free text and the taxonomy has no mapping to
them, so the classifier takes an **explicit** assumption → event-type mapping:

```
no mapping supplied          → UNKNOWN       (cannot tell; look anyway)
mapping supplied, no match   → NOT_RELEVANT  (can tell, and it does not)
```

`is_relevant_to_thesis` (gates a directional **revision**) excludes UNKNOWN.
`warrants_reassessment` (gates a **reassessment**) includes it for a direct
issuer event. Collapsing the two would either silence the engine or make it
chase every headline.

---

## F. Novelty: one event, one reaction

Tracked per **(canonical event, revision, thesis)**. `canonical_events` has no
`version` column, so a revision is a content hash over lifecycle state,
corroboration state, report count, contradictions and subtype.

| Change | Status | Re-triggers? |
|---|---|---|
| nothing | `ALREADY_ASSESSED` | no |
| **more report copies** | `UPDATED` | **no** — syndication is not news |
| lifecycle / corroboration / contradiction | `MATERIAL_REVISION` | **yes**, linked to its predecessor |
| older than the configured window | `STALE` | urgency capped at NORMAL |

A rumour becoming a confirmed filing is a material revision. The same rumour
re-crawled tomorrow is not.

---

## G. Corroboration: syndication is not confirmation

> Measured: `independent_source_count` is **0 for all 581 events**.

That 0 could mean "checked, none" or "never established". It is the second, so
`source_independence_known` is a **separate boolean** and
`is_independently_corroborated` cannot be True however many reports arrive. The
contract *refuses* a bundle claiming a count while independence is unknown.

Report count alone does **not** raise materiality — asserted by test.

---

## H. Materiality is a band, not a probability

25.9I-E's `ThesisEvidence.materiality` is a 0–1 float whose docstring says
*"25.9I-F owns event materiality"*. It does — but a float implies a calibration
nobody has. So materiality is decided as a **band** (`ImpactLevel`) from
evidenced components, and the float handed over is a documented band midpoint
(0.80 / 0.50 / 0.20), **None when UNKNOWN**. Unmeasured evidence cannot clear a
threshold.

Components, used only where evidence exists: issuer directness · canonical
subtype · lifecycle (a rumour scores **−1**, a filing **+1**) · established
corroboration · contradictions · thesis relevance.

**Confidence stays in pieces** — four axes, never averaged. `overall()` is the
**weakest known axis**, because the weakest axis is the one that will be wrong.

---

## I. Conflict, and what is never done about it

`CONFLICTED` is never resolved by counting headlines, source order, or
last-write-wins. A 3-to-2 split is CONFLICTED, not "positive". Conflicted
evidence **cannot strengthen** a thesis, **cannot invalidate** one, and raises
urgency to ELEVATED instead.

---

## J. The three prohibitions, enforced structurally

`PositionEventImpactAssessment` has **no field** for a revised target zone, a
revised horizon or an exit fraction. Not "defaults to None" — **absent**. Event
intelligence cannot supply what it has no field to carry.

| Event says | This layer produces | It does **not** produce |
|---|---|---|
| material positive | `target_reestimation_required` | a number |
| material positive | `horizon_reestimation_required` | +3 / +5 / +10 days |
| material negative | `partial_exit_review_required` | 25% / 40% / 70% |

`PositionThesisEngine.reassess` takes `proposed_target`, `proposed_horizon` and
`exit_fraction` as parameters **separate from** `evidence`. This engine fills in
`evidence` and passes the other three **through untouched**. Scenario B proves
it: the same event yields no numbers in A and the caller's numbers in B.

---

## K. Invalidation is evidenced, not asserted

A caller may state that a core assumption is contradicted. That is **not
sufficient**. The contract requires all of:

- materiality **HIGH**
- direction **NEGATIVE**
- relation **DIRECT** (a sector event cannot invalidate a company thesis)
- conflict state not CONFLICTED

A caller cannot end a thesis by setting a boolean.

**Cooldown restrains greed, never safety.** 25.9I-E checks invalidation *before*
any policy gate, so an anti-overreaction cooldown physically cannot suppress an
invalidating event. Tested end to end with the cooldown fully engaged: still
CRITICAL → HOT → FULL_EXIT.

---

## L. Urgency asks for a look, not a trade

`NORMAL` → monitor · `ELEVATED` → reassess, WARM · `CRITICAL` → **reassess
now**, HOT.

> **CRITICAL means REASSESS NOW. It does not mean SELL NOW.**

Requests go to 25.9I-C, which owns whether they are granted. This account has
**never served a realtime marker**, so a HOT request routinely yields delayed
data and a non-executable outcome. That is the separation working.

A **stale** event is capped at NORMAL: reprocessing yesterday's news today does
not make it urgent.

---

## M. Data problems are not bearish events

A stale quote, unresolved entity or timestamp conflict raises
`data_integrity_review_required` and can suppress a directional claim. It never
becomes NEGATIVE. A system that sells on its own outages is reading itself, not
the market.

`MISSING_EVENT_TIME` alone is **not** a review trigger — it is true of all 581
events here, and flagging the whole corpus would mean nothing.

---

## N. As-of, enforced three ways

1. **In SQL** — every read bounds `first_reported_at <= as_of`.
2. **In the contract** — `EventEvidenceBundle` refuses a timestamp after its own
   `as_of`.
3. **By physical truncation test** — the same moment re-read from a corpus that
   also holds later rows gives a byte-identical assessment id.

Price is never an input to direction or materiality: passing 50.0 and 200.0 for
the same event yields identical classifications.

**Deterministic ordering** (§48): known occurrence time → first report →
canonical id. An undated event sorts **last**, not first — it is the least
placeable, not the oldest.

---

## O. Persistence

Three additive, append-only tables with content-derived ids and
`INSERT OR IGNORE`:

| Table | Question |
|---|---|
| `adaptive_position_event_assessments` | the verdict and every reason behind it |
| `adaptive_event_thesis_links` | **the consumption ledger** |
| `adaptive_event_reassessment_requests` | what was asked for |

**Only the ledger had to exist.** Relevance, direction, materiality and urgency
are pure functions and can be recomputed; whether a thesis already reacted is
history, and unwritten the system revises a position every time the crawler runs.

`is_executable` is stored as a literal **0** so a future reader can see in the
data that these were never instructions. No target, horizon or fraction column
exists. No credential or account identifier is stored.

---

## P. Production boundary

| Path | Changed? |
|---|---|
| capture / Portfolio / Risk / OrderIntent / Execution / IBKR / subscriptions | **No** |

No module imports `src.execution`, `src.trading` or `src.portfolio`; none calls
`place_order`, `cancel_order`, `market_snapshot`, `subscribe` or
`init_brokerage_session`; the string `OrderIntent` appears nowhere. All asserted
by AST test.

---

## Q. Interaction with the other sub-phases

| Phase | Relationship |
|---|---|
| **25.9I-B** | session universe supplies the instruments a thesis can exist for |
| **25.9I-C** | I-F **requests** COLD/WARM/HOT; I-C owns entitlement. HOT requested ≠ realtime available |
| **25.9I-D** | exposure read-only for the multi-position diagnostic; no allocation changes |
| **25.9I-E** | the consumer. Evidence in, recommendation out, rules unchanged |
| **25.9I-F.5** | Signal & Horizon Intelligence — the intended supplier of the target, horizon and fraction this phase refuses to invent |
| **25.9I-G** | the assessment → evidence → revision lineage is what attribution walks |
| **25.9I-H** | still blocked: no realtime verified, no target model, no sizing model |
