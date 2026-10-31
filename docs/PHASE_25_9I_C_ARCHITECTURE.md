# Phase 25.9I-C — Adaptive Market Data & Cost Governor

> ## COST CAP REACHED DOES NOT MEAN EXECUTION-GRADE REALTIME IS VERIFIED.
> ## CHEAP REALTIME DOES NOT MEAN EVERY SYMBOL SHOULD BE HOT.

> `adaptive_market_data_enabled`, `market_data_cost_governor_enabled` and
> `adaptive_execution_freshness_gate_enabled` are **OFF**. Capture is unchanged,
> Portfolio remains authoritative, Risk remains the hard gate, Execution remains
> the only path to IBKR, and no subscription can be purchased by any code here.

Written 2026-09-29.

---

## A. The evidence, and exactly what it changed

IBKR Client Services answered ticket #561227 on 2026-09-29
(`docs/evidence/IBKR_MARKET_DATA_BILLING_561227.md`):

| | |
|---|---|
| Snapshot request | **USD 0.01** |
| Threshold | **150 requests per exchange per calendar month** |
| Cap | **USD 1.50 per exchange per month** |
| After the cap | realtime streaming *stated* to be free for the rest of the month |
| Counter | updated **once per business day at end of day** |
| Bundle | described as *replacing* snapshot delivery — an alternative |

**Superseded:** Phase 25.9I-A recorded an open question in which the variable
component might reach *"~USD 2,500/month at ~250,000 requests"*. That figure
modelled an **uncapped** charge. A per-exchange ceiling now exists, so the
variable term is bounded. The old text is kept, marked superseded, because it
records why the question was open.

**Not superseded, because it was never in doubt:** that this is a *provider
statement*. On 2026-09-24 IBKR's own assistant answered the same question two
contradictory ways within an hour.

---

## B. POLICY_KNOWN and ACCOUNT_BEHAVIOR_VERIFIED are different variables

This is the spine of the phase.

```
provider said it          →  PricingConfidence.PROVIDER_STATED
this account did it       →  PricingConfidence.ACCOUNT_VERIFIED   ← never yet
```

```
request count ≥ 150       →  CapState.CAP_REACHED         (arithmetic)
field 6509 contains 'R'   →  EntitlementStatus.REALTIME_VERIFIED  (evidence)
```

There is **no code path from the first to the second** in either pair. A cap is
a fact about a counter; realtime is a fact about a quote. `ExchangeEntitlement`
carries `expected_after_cap` and `observed` as separate fields with separate
provenance, and `expectation_unmet` names the venues where the provider's
promise and this account's behaviour disagree — the single most informative
thing this phase can produce.

---

## C. The question the reply did not answer

`transport.market_snapshot(conids, fields)` sends the **whole universe in one
HTTP call** (`src/marketdata/quotes.py`). "Per request" was never defined:

| Basis | One 60 s cycle | 390-minute session, **if the cap failed** |
|---|---|---|
| `PER_HTTP_CALL` | 1 request | ~USD 3.90 |
| `PER_INSTRUMENT` | 31 requests | ~USD 120.90 |

A **31× difference in the downside**. So `counting_basis` is
`RequestCountingBasis.UNKNOWN`, every ceiling is enforced on the conservative
(`PER_INSTRUMENT`) reading, and `project_exposure` reports **both** numbers.
An unknown cost is never the cheap answer.

---

## D. Exchange coverage, recomputed from the repository

Neither universe file carries an exchange, and the research `exchanges` table
holds `US_AND_INTL` — *"US & International (unspecified)"* — which is a research
bucket, not a billing venue. The only canonical source is
`broker_instrument_mapping.broker_payload_json.primary_exchange`.

**Not `exchange`**, which reads `SMART` for all 31: SMART is IBKR's order router
and has no market-data price list. Billing to it would collapse three counters
into one.

| Universe | Result |
|---|---|
| capture (31) | **NASDAQ 13, NYSE 17, ARCA 1** — matches the prior manual count |
| parent (130) | NYSE 11, NASDAQ 10, **UNKNOWN 109** |

The 109 is the finding that matters: a dynamic session universe drawn from the
parent can contain names whose venue — and therefore whose price — is unknown.
Those requests are **refused**, not estimated. A fourth venue gets its own
counter automatically; it does **not** get a price automatically.

**The benchmark is not free.** SPY is on ARCA and bills like anything else.

---

## E. Cap arithmetic

```
variable_cost(E) = min(request_count(E) × 0.01, 1.50)
```

| requests | cost | state |
|---|---|---|
| 0 | 0.00 | below threshold |
| 149 | 1.49 | below threshold |
| 150 | **1.50** | **cap reached** |
| 10,000 | **1.50** | cap reached |

Applied to the **cost**, not the count, so a provider capping at a non-multiple
of its unit price is still represented correctly. Incremental cost is computed
as `cost(after) − cost(before)`, so it is right at the boundary too: 149 → 159
requests costs **USD 0.01**, not 0.10.

Periods are `BillingPeriod` values keyed `YYYY-MM`. October's counters start at
zero because they are **different rows**, not because September's were cleared.
The billing timezone is **not known** — the reply did not say — so UTC is a
*declared, overridable assumption* (`timezone_assumed=True`) and any other zone
must be supplied explicitly rather than guessed.

**Monthly forecast, from the current universe:** 3 capped venues × 1.50 =
**USD 4.50** against a USD 20 budget. Computed from whatever venues are actually
present, never from the constant 4.50.

---

## F. The local ledger, and why it had to exist

IBKR's counter updates **once per business day**. During a session the
provider's meter reads zero no matter what is happening. "Watch the portal and
stop if it climbs" is not a control — it is a next-morning autopsy.

`adaptive_market_data_requests` is the only intraday meter, and every ceiling in
the validation harness is enforced against it. The provider's figure arrives the
next day and is written **beside** the local estimate
(`portal_reported_count`), never over it: a disagreement would mean the counting
basis or the cap is not what was described, and an overwrite would erase it.
`NULL` means *not yet compared*, never *agreed*.

---

## G. COLD / WARM / HOT — attention, not capital

25.9I-D allocates money; this allocates **looking**. Conflating them produces
the error where the biggest position is assumed to need the freshest price. A
small position one tick from invalidation needs realtime far more than a large
quiet one.

| State | Means | Channel |
|---|---|---|
| COLD | delayed, daily or cached is fine | none |
| WARM | a decision may be near | bounded snapshot |
| HOT | execution-sensitive | streaming if it existed; snapshot, *and it says so* |

Four things the word "realtime" was hiding, now separate fields on
`InstrumentDataStatus`: `desired_state`, `availability`, `entitlement`,
`freshness`. `execution_freshness_ready()` is their conjunction plus a measured
age, fails closed at every step, and returns the reason.

**HOT + delayed = NOT READY.** That is the state all four Phase 25.9H sessions
were in, and it must be expressible without evaluating as ready.

### Capacity is a ceiling, not a target

`max_hot_symbols` has **no production default** — spec §14, and there is no
evidence for one, because no strategy has ever run on realtime data here.
`unused_hot_capacity` is reported as a normal, healthy outcome.

Even at the cap, where the marginal monetary cost is genuinely zero, nothing
escalates without a reason. Request rate is still a limit; a HOT instrument
carries a freshness *obligation*; and attention spread over 31 names is not
attention.

### Priority is a class, not a score

`POSITION_EXIT → POSITION_MONITOR → ENTRY_CANDIDATE → BROAD_SCREENING`.
A hidden number is unarguable after the fact — nobody can say whether 0.62
should have beaten 0.58. The order encodes one judgement: a missed entry costs
an opportunity, a missed exit costs money that exists.

### Deduplication is cost safety

Four subsystems wanting one instrument is **one** activation that remembers all
four requesters. The merge happens before anything is priced, so it cannot be
defeated by pricing each copy separately.

### The refusal that must never resolve quietly

When a plan an **open position's exit** needs is refused on cost, the verdict
carries `safety_conflict` and `SAFETY_DATA_REQUIRED` / `BUDGET_CONFLICT`, and
the desired state is left standing. Delayed data is not the cheap substitute for
an exit.

---

## H. Streaming: audited, and deliberately not built

`ClientPortalTransport` implements exactly one market-data call —
`market_snapshot`. There is **no WebSocket client, no subscribe/unsubscribe pair,
and no persistent runtime** anywhere in the project to hold one;
`src/marketdata/service.py` documents that absence as a decision.

Spec §16 says reuse streaming if it exists, otherwise implement only the minimum
abstraction required. A second market-data stack with its own daemon and
reconnect state machine is the opposite of minimum, so **it was not built**.
`ChannelAvailability` records the audit (`stream_supported=False`) and lets HOT
state that streaming was wanted and is unavailable, rather than quietly being
served a snapshot and believing it streamed.

Subscriptions exist anyway: IBKR's first snapshot for an unseen conid returns no
fields — the request *opens* a subscription (measured 2026-09-11). That state
does not survive a gateway restart, which this repository has already hit in
production. `SubscriptionRegistry.reconcile` compares desired vs believed vs
observed and returns `hot_desired_but_not_execution_grade`.

§42 — prefer streaming once a venue is capped — is **pending**: it requires both
policy and operational verification to agree, and neither half is verified.

---

## I. Persistence

Four additive tables, append-only, content-derived ids, `INSERT OR IGNORE`:

| Table | Question |
|---|---|
| `adaptive_market_data_requests` | what was asked for (the evidence, the intraday meter) |
| `adaptive_market_data_exchange_usage` | per-venue per-month rollup; where the portal counter is later reconciled |
| `adaptive_market_data_state_transitions` | every COLD/WARM/HOT move and its reason |
| `adaptive_market_data_entitlements` | provider expectation beside account observation |

No order, intent, fill or position row. **No credential, token or account
identifier** — asserted by test against every column.

Lineage carried, never derived: `session_snapshot_id → admission_decision_id →
allocation_plan_id → thesis_id → lineage_id`, so 25.9I-G can ask whether a HOT
activation was worth what it cost.

---

## J. Controlled October validation

Read-only. No orders. `scripts/validate_market_data_billing.py`, **`--plan` by
default**; `--execute` requires `--max-requests`, `--max-estimated-cost-usd` and
`--timeout-seconds`, none of which has a default — those ceilings are the
operator's to justify, not this file's to invent.

- **Stage A — pre-flight.** Gateway authenticated, no competing session, paper
  account, `ExecutionSafety.allow_real_orders` False, `capture_only()` transport,
  every instrument attributable, period priceable. Any failure refuses.
- **Stage B — bounded run.** Ceilings checked *before* each cycle. For the
  current universe at 60 s: NYSE crosses 150 at **8.8 min**, NASDAQ at
  **11.5 min**, ARCA at **150 min**. A 30-minute run costs **USD 3.30 if the
  cap holds, USD 9.30 if it does not**.
- **Stage C — stop.** Deliberate. Stopping is the success condition.
- **Stage D — reconcile.** Next business day, `--reconcile` writes the portal
  counter beside the local estimate.
- **Stage E — entitlement.** Only an observed `R` marker sets
  `REALTIME_VERIFIED`. Cap reached with delayed data is recorded as
  `expectation_unmet` — investigate, assume nothing.

> The paper-account check is **this tool's own guard**. It is not a resolution
> of the separate, still-open question of whether the production capture runner
> should hard-guard on a DU account. That decision is the operator's and has not
> been made.

---

## K. Production boundary

| Path | Changed? |
|---|---|
| capture / Portfolio / Risk / OrderIntent / Execution / IBKR orders / subscriptions | **No** |

`src/adaptive/` imports no `src.execution`, `src.trading` or `src.portfolio`
module, and **no adaptive module calls `market_snapshot`** — asserted by AST
test. The only place a billable request can originate is the validation harness,
behind three explicit ceilings.

---

## L. Interaction with the other sub-phases

| Phase | Relationship |
|---|---|
| **25.9I-A** | `MarketDataState`, `MarketDataStateTransition`, `BudgetPolicy` reused; `MarketDataCostGovernor` left intact for uncapped plans |
| **25.9I-B** | session universe ≠ HOT set; parent universe's 109 unpriced members are the constraint |
| **25.9I-D** | capital and attention stay separate resources |
| **25.9I-E** | `PositionActionRecommendation → DataAttentionRequest`; an exit asks HOT, `EXTEND_TARGET` does not |
| **25.9I-F** | event urgency will enter through the same request interface |
| **25.9I-G** | lineage answers "was HOT worth it" |
| **25.9I-H** | still blocked: no realtime has ever been verified on this account |
