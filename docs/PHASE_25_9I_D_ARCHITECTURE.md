# Phase 25.9I-D — Smart Capital Allocation & Risk Budgeting

> **THE ADAPTIVE ALLOCATOR DOES NOT CONTROL THE PRODUCTION PORTFOLIO IN THIS PHASE.**
> `smart_capital_allocation_enabled` is OFF. Output is a proposal record. Canonical
> Portfolio remains authoritative, canonical Risk remains the hard gate, and Execution
> remains the only path to IBKR.

Written 2026-09-29.

The rule this phase encodes:

> **Capital existing is not a reason to deploy it. An opportunity existing is not a
> reason to fund it.**

---

## A. Capital and risk are different quantities

Two $20 positions can carry $1 and $6 of downside. An allocator that counts only notional
treats them as the same commitment and is wrong about the second.

- `PositionRiskEstimate.risk_per_dollar` — the fraction of notional at risk
- `AllocationLine.capital_at_risk` — notional × that fraction
- `RiskBudgetState` — account budget, existing risk, proposed risk, remaining

The allocator spends **two** budgets: capital and risk. Either can bind first. Evidence
from the fixtures: at $100, $250 and $500 of equity with the same three candidates,
deployment stays ≈$47.70 — because the **$5 account risk budget binds, not the capital**.

### Three ways to estimate downside, in order of honesty

| Method | Status | When |
|---|---|---|
| Invalidation distance | `MEASURED` | a defensible invalidation level and reference price exist |
| Volatility × horizon × multiple | `PARTIAL` | only volatility exists; a band is not an invalidation level |
| Nothing | `UNKNOWN` | neither — **and the dataclass refuses to carry a number** |

This phase **does not invent invalidation levels**. A stop created because sizing wanted
one is not a risk model; 25.9I-E owns theses.

---

## B. Cash is first-class; zero is an answer

- `minimum_cash_fraction` is held back **before** anything is allocated.
- `opportunity_reserve_fraction` holds more back for later in the session.
- Capital not earned by any candidate **stays as cash** and is never redistributed to
  weaker names. That redistribution is how a system funds its worst idea with the money
  its best idea could not take.
- A plan that funds nothing is `COMPLETE`, not an error.

**The accounting identity**, asserted by tests:

```
cash = deployed + unallocated + reserved + capital_above_ceiling
```

`capital_above_ceiling` is cash excluded by the deployment ceiling rather than by choice.
The first smoke test found a double-count here — the reserve was subtracted both inside
`deployable_capital` and again in `unallocated`, producing **−1.58** — which is why the
identity is now a method the tests call rather than a comment.

---

## C. Desirability is not expected return

25.9I-B established that the session score ranks attention, liquidity and data quality,
and that `expected_return` and `model_confidence` are unavailable because no model is
promoted. Nothing here upgrades that.

**Measured:** `opportunity_rank`, `liquidity`, `data_quality` (positives);
`volatility`, `existing_exposure`, `sector_crowding`, `risk_uncertainty` (penalties).

**Declared unavailable, each with a reason:** `expected_return`, `model_confidence`,
`event_strength`, `regime_compatibility`, `spread_cost`, `slippage_estimate`,
`market_data_cost`.

Penalties **scale** the positive rather than subtracting from it, because subtraction can
produce a negative preference and "how much capital" has no negative answer. Unknown risk
is itself a penalty (`risk_uncertainty = 1.0`), never a neutral.

**No Kelly sizing.** It needs a calibrated probabilistic edge, and there is none. A test
asserts the word does not appear in the allocator.

---

## D. The algorithm

1. Candidates below `minimum_desirability` get nothing, with a reason.
2. Survivors get a desirability **share** of deployable capital:
   `d_i^k / Σ d^k`, `k = concentration_exponent`. Not equal weight (candidates differ),
   not winner-take-all (a score is not a forecast).
3. Each share is cut by whichever limits bind, in a fixed order, and **every cut is
   recorded**: position cap → sector cap → correlation → per-position risk → account risk
   → unknown-risk cap → remaining capital → minimum size.
4. Whatever is left stays cash.

**Determinism:** candidates are sorted by `(-desirability, instrument_id)` before
anything is allocated, so input order, dict order and wall-clock time cannot change the
result. A test permutes the input and asserts identical economics and plan id.

### The minimum-size policy

A $100 account with 31 candidates gives each ≈$1.94 — below any sane minimum. Two
behaviours, configurable:

- `skip` — every share below `minimum_allocation_usd` becomes zero. Result: **nothing is
  funded at all**.
- `concentrate` *(default)* — shares below the minimum are raised to it **in rank order
  while capital lasts**; the rest are zero with a reason.

`concentrate` is not a fill rule: every candidate still clears minimum desirability and
every cap, and leftover capital still stays cash. It reflects that holding fewer, viable
positions beats spreading capital below a workable size.

---

## E. Concentration, sectors, correlation

`max_position_fraction`, `max_sector_fraction` and `max_deployment_fraction` are
**ceilings, never targets**, and they count what is **already held**: a portfolio at the
deployment ceiling has zero deployable capital, not a fresh allowance.

Constraints may only **subtract**. If technology hits its cap, the excess becomes cash —
it is never pushed into a weaker healthcare name for the look of diversification.

**Correlation** is implemented as an as-of provider over daily returns from the research
cache (`window` 120, `min_observations` 30, read-only, bounded at `as_of`). The
**threshold is OFF by default** (`max_pairwise_correlation = None`): this repository has
no evidence for a particular cut-off, and §15 forbids inventing one. A missing
correlation returns `None` and earns **no diversification credit** — zero would be the
most dangerous possible default, since it claims two positions diversify each other on
the strength of data that does not exist.

---

## F. Unknowns degrade; they never default

| Unknown | Treated as |
|---|---|
| downside | capped by `unknown_risk_notional_cap_usd`, or zero allocation when no cap is set — **never risk-free** |
| correlation | unmeasured; no diversification credit |
| expected return | absent, not positive |
| model confidence | absent, not high |
| market-data cost | unknown, not free — ticket #561227 is still unresolved |
| existing position risk | makes remaining risk capacity **unknown**, so the budget cannot be spent against it |
| account capital | no plan: status `BLOCKED` |

Plan status is `COMPLETE`, `PARTIAL`, `INSUFFICIENT_DATA` or `BLOCKED`, and a non-complete
plan must state its gaps or the dataclass refuses construction.

---

## G. Small accounts

The design target is $100–$500, not institutional capital.

- Allocations are stated in **notional currency only**. No share quantity is ever
  produced, because that needs an execution-grade price and this phase has none (§22).
- **No fractional-share assumption:** `broker_instrument_mapping.minimum_quantity`,
  `quantity_increment` and `lot_size` are **NULL for every mapped instrument** in this
  repository, so nothing here may assume fractional trading works.
- `minimum_allocation_usd` exists because a $2 position cannot carry its own transaction
  costs.

---

## H. Lineage and persistence

`session_snapshot_id` and `admission_decision_id` travel from the 25.9I-B selection onto
every candidate, every line and the stored plan, so a later attribution phase can ask
which universe decision led to which money and read the answer rather than reconstruct it.

Two tables, not the four the brief offered: `adaptive_allocation_plans` and
`adaptive_allocation_lines`. Constraints and risk-budget state are properties **of** a
plan and **of** a line; separate tables would add joins and partial-write risk for data
never queried independently — the same choice 25.9I-B made for score components.

Content-derived ids, `INSERT OR IGNORE`, append-only revisions. Nothing outside
`adaptive_allocation_*` is written, asserted by a test that reads every write statement in
the repository module.

---

## I. Production boundary

| Path | Changed? |
|---|---|
| Portfolio | **No** — `src/portfolio` untouched; no adaptive module imports it |
| Risk | **No** — still the hard gate; `OrderIntent.require_approval` still refuses |
| Position sizing | **No** — `PositionSizingStrategy` not subclassed or referenced |
| OrderIntent | **No** — no adaptive module constructs one |
| Execution / IBKR | **No** |
| Capture / subscriptions | **No** |

The allocator imports no execution, trading or portfolio module, calls no broker method,
and constructs no `OrderIntent`, `AllocationProposal` or `TargetPosition` — all asserted
by AST tests.

**Allocation vs rebalancing:** this phase implements **new capital allocation** only. It
never sells an existing position to fund a better one. Existing holdings reduce available
capacity and nothing else. Rebalancing needs a thesis (25.9I-E).

---

## J. Interaction with the other sub-phases

| Phase | Relationship |
|---|---|
| **25.9I-A** | Contracts reused: `CapitalAllocationProposal`, `CashReserve`, `RiskBudget`, `AllocationConstraint`, `AllocationRejectionReason`, `ExecutionEconomics`, `DecisionLineage` |
| **25.9I-B** | Supplies candidates, scores, sectors and the lineage ids |
| **25.9I-C** | Would supply real market-data cost; **externally blocked** on ticket #561227 |
| **25.9I-E** | Position thesis → real invalidation levels → `MEASURED` risk instead of `PARTIAL`; also owns rebalancing |
| **25.9I-F** | `event_strength` for desirability, and pre-event sizing |
| **25.9I-G** | Shadow validation and attribution — where performance questions belong |
| **25.9I-H** | The first controlled path from a proposal toward paper execution |
