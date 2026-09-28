# Phase 25.9I-A — Adaptive Decision Layer: Architecture and Contracts

**Status: FOUNDATION ONLY. Every flag is OFF. No production behaviour changed.**
Written 2026-09-28.

This phase adds vocabulary, not decisions. Nothing here selects a universe, opens a
subscription, allocates capital, revises a thesis or touches an order. What it does is
fix the contracts that 25.9I-B..H will implement against, and encode the invariants as
validation that fails loudly rather than as prose everyone agrees with and then forgets.

---

## A. Why this layer exists

The system currently captures a fixed universe of 31 instruments every session, sizes
positions by a fixed fraction, and holds until a target is reached. Each of those is a
deliberate simplification, and each has the same shape of flaw: **a resource is consumed
because it exists**, not because the expected value justified it.

- 31 slots exist, so 31 slots get filled.
- Capital exists, so capital gets deployed.
- A market-data subscription exists, so every instrument gets polled at the same rate.
- A target was set at entry, so the position waits for it whatever else happens.

The adaptive layer is the answer to all four. Its single rule:

> **Capacity is not a target. Capital is not required to be deployed. Realtime data is
> not required to be consumed. Every resource is allocated only when expected value and
> risk justify it.**

---

## B. The full flow, and where it stops today

```
parent universe (75-150 liquid US equities)
  -> dynamic session universe (0..31, never required to fill)
  -> opportunity admission -> validity / expiry
  -> market-data state: COLD / WARM / HOT
  -> adaptive realtime activation (costed)
  -> opportunity scoring
  -> capital allocation -> risk budgeting
  -> PORTFOLIO TARGET
  -> RISK HARD GATE
  -> fresh-price requirement
  -> ORDER INTENT -> EXECUTION -> IBKR
  -> position thesis
  -> event / price / time / regime reassessment
  -> hold | partial exit | full exit | extend target | reduce target | tighten stop
  -> outcome -> error attribution -> memory
```

**Implemented in 25.9I-A: none of it.** The contracts for every box exist; the arrows do
not. The three capitalised stages are unchanged Phase 11/14 components, and nothing in
this layer may bypass them — see §F.

---

## C. What was reused rather than rebuilt

The repository already had most of the vocabulary. Reusing it matters more than the code
saved: a second `DataFreshness` with different thresholds would let two parts of the
system disagree about whether a quote is usable, and the disagreement would surface as a
trade.

| Concept | Existing home | How this phase uses it |
|---|---|---|
| Data age classification | `DataFreshness`, `FreshnessPolicy` (paper_models, Phase 13) | Unchanged; the new policy maps decisions onto them |
| Live vs delayed marker | `MarketDataAvailability` (market_data_models, 25.7) | Unchanged; read by the freshness policy |
| Allocation and approval | `AllocationProposal`, `RiskDecision`, `OrderIntent` (portfolio_models, Phase 11) | Remain the production path; new proposals are inputs to them |
| Position sizing | `PositionSizingStrategy` ABC (portfolio/sizing.py) | Still the only allocator in production |
| Real-money refusal | `ExecutionSafety` (Phase 14) | Untouched and still authoritative |
| Durable mode / kill switch | `TradingModeStore` (25.9E) | Untouched; the adaptive flags cannot reach it |
| Capture universe | `config/capture_universe_v1.json` | **Untouched.** A dynamic universe would be a separate artifact |

## D. What was added

| File | Purpose |
|---|---|
| `src/domain/adaptive_models.py` | Universe, opportunity, market-data state, allocation, economics, lineage contracts |
| `src/domain/thesis_models.py` | Position thesis, revisions, dynamic-exit recommendations, revision policy, event risk |
| `src/adaptive/flags.py` | Feature flags; all OFF; fail-closed parsing; dependency suppression |
| `src/adaptive/cost_governor.py` | Configurable market-data cost model and ALLOW/WARN/THROTTLE/BLOCK verdicts |
| `src/adaptive/freshness.py` | Which decisions delayed data may support |
| `scripts/adaptive_status.py` | Read-only diagnostic proving the layer is inactive |
| `tests/adaptive/` | 109 tests over the invariants above |

---

## E. Feature flags

All default **OFF**. Environment variables share the prefix `MARKETLENS_ADAPTIVE_`.

| Flag | Variable | Requires |
|---|---|---|
| `adaptive_universe_enabled` | `…UNIVERSE_ENABLED` | — |
| `adaptive_market_data_enabled` | `…MARKET_DATA_ENABLED` | `market_data_cost_governor_enabled` |
| `market_data_cost_governor_enabled` | `…COST_GOVERNOR_ENABLED` | — |
| `smart_capital_allocation_enabled` | `…CAPITAL_ALLOCATION_ENABLED` | — |
| `position_thesis_enabled` | `…POSITION_THESIS_ENABLED` | — |
| `dynamic_exit_enabled` | `…DYNAMIC_EXIT_ENABLED` | `position_thesis_enabled` |
| `event_driven_position_reassessment_enabled` | `…EVENT_REASSESSMENT_ENABLED` | `position_thesis_enabled` |
| `adaptive_execution_freshness_gate_enabled` | `…EXECUTION_FRESHNESS_GATE_ENABLED` | — |
| `layer_enabled` (informational) | `MARKETLENS_ADAPTIVE_LAYER_ENABLED` | enables nothing on its own |

**Fail-closed:** an unparseable value leaves the flag OFF and is reported in
`problems`. It does not raise: a typo in one variable must not stop a capture session
that was otherwise healthy. A flag whose prerequisite is off is forced off and reported
in `suppressed`, so a half-running subsystem is never the outcome.

**A flag cannot enable a broker write.** `src/adaptive/flags.py` imports only
`__future__`, `os`, `dataclasses` and `typing`, and a test asserts exactly that list. No
adaptive module imports `src.execution` or `src.trading` at all.

---

## F. Safety boundaries

| Invariant (spec §3) | How it is enforced |
|---|---|
| A. Capacity is not a target | `SessionUniverse` accepts 0..max; over-capacity raises; `unused_capacity` is a normal reading |
| B. Capital need not be deployed | `CapitalAllocationProposal` refuses overspending only; cash is a first-class line; an empty proposal is valid |
| C. Admission ≠ allocation | Four separate types; `CapitalAllocationDecision(amount_usd=0.0)` is valid and carries its reasons |
| D. Delayed data may analyse, not execute | `ExecutionFreshnessPolicy`; execution-sensitive decisions require HOT, a realtime marker and a measured age |
| E. Signals never send orders | No adaptive module imports execution or trading; recommendations carry no venue, account or executable flag |
| F. Target position ≠ broker position | Untouched; `trading/targets.py` remains the only reconciliation path |
| G. Loss ≠ error | No type in this phase classifies an outcome |
| H. New information may change a thesis | `PositionThesisRevision` requires an evidence-bearing reason to extend a target; `TARGET_APPROACH` alone is refused |
| I. Target and stop are separate | A revision that raises the target and loosens the invalidation zone is refused |
| J. Nothing is consumed merely because it exists | `MarketDataStateTransition` requires a named activation reason to reach HOT; the governor blocks what it cannot price |

---

## G. The cost governor, and why it assumes nothing

On 2026-09-24 IBKR's own assistant answered the same billing question two ways inside an
hour: first that ~250,000 monthly snapshot requests were covered by the subscription,
then that each is charged at USD 0.01. Their subscription page carries the second
reading in fine print. At this project's polling rate that is roughly **USD 15 a month
against roughly USD 2,500**. Support ticket **#561227** was still open when this was
written.

So `MarketDataCostModel` hard-codes no price. An unset price is **UNKNOWN, never zero**,
and `MarketDataCostGovernor.assess` returns **BLOCK** for any plan it cannot price. The
monthly budget defaults to USD 20 — the user's stated ceiling — as a configurable
default, overridable per call and by `MARKETLENS_MDCOST_MONTHLY_BUDGET_USD`.

The principle it will eventually serve, **inactive in this phase**: a position should not
be opened if the system cannot afford the data needed to monitor and exit it safely.

---

## H. Persistence: deliberately deferred

§14 permits documenting a deferral instead of creating speculative tables, and that is
the choice here. No schema was added, no migration was written, and no existing store was
touched. Every contract in this phase is an in-memory value object.

**Why:** the shape of what needs storing depends on decisions 25.9I-B and 25.9I-E have
not made yet — whether session universes are snapshotted per day or per change, whether
thesis revisions live beside positions or in their own lineage table. Tables written now
would be guesses, and a migration away from a guess is more expensive than the delay.

**When it lands**, it needs: deterministic ids, immutable revisions, as-of semantics,
lineage ids, no destructive migration, and migration tests. A test in this phase asserts
that `src/data_access/` contains no adaptive or thesis schema module, so the deferral
cannot lapse silently.

---

## I. Interaction with existing subsystems

| Subsystem | Relationship in 25.9I-A |
|---|---|
| Event Intelligence | May eventually supply `EventRiskExposure` and revision triggers. It may never modify an order directly |
| Quant / Models | Supply score components and confidence. `OpportunityScore` keeps components, not just a scalar |
| Signals | Unchanged. Still upstream of Portfolio |
| Portfolio | Unchanged and still authoritative. Adaptive proposals are inputs |
| Risk | Unchanged and still the hard gate. No flag bypasses it |
| Execution / IBKR | Untouched. No new writes, no cancel/replace, no bracket orders |
| Outcomes / Attribution | `DecisionLineage` is the thread they will need; nothing computes attribution yet |
| Memory | Unchanged |
| Capture (25.9G/H) | **Operationally unchanged**: same transport, same cadence, same universe file |

---

## J. Separation from D20 protected research

The protected experiment is **sealed, unexecuted, unseen and unconsumed**. Its
hypothesis (`market.return_60d`, NEGATIVE, anchor-cancelled day5→20) is untouched;
`scripts/validate_d20_reversal.py` was not run; the protected ledger is not imported by
any file in this phase. Two tests assert it: no adaptive module names a protected
artefact, and none imports `src.research`.

---

## K. What remains for the later sub-phases

| Phase | Scope |
|---|---|
| **25.9I-B** Dynamic Universe | Parent universe artifact, daily scoring, session selection, persistence of decisions |
| **25.9I-C** Adaptive Market Data & Cost Governor | COLD/WARM/HOT driving real subscriptions; streaming transport; throttling; live cost accounting |
| **25.9I-D** Smart Capital Allocation & Risk Budgeting | Allocator behind the flag, still passing through Portfolio and Risk |
| **25.9I-E** Position Thesis & Dynamic Exit | Thesis persistence, revision calibration, partial-exit intents |
| **25.9I-F** Event-Driven Reassessment | Event triggers, materiality thresholds, pre-event sizing |
| **25.9I-G** Shadow / Simulation Validation | Run the layer in shadow against captured sessions; attribution |
| **25.9I-H** Controlled IBKR Paper Integration | The freshness gate switched on; paper orders through the existing path |

**Blocking 25.9I-C in particular:** the market-data cost question (#561227) and the fact
that, as of 2026-09-25, capture still receives delayed quotes. A cost governor cannot be
calibrated against prices nobody has confirmed.
