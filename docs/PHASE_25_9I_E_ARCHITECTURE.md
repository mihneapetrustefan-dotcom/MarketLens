# Phase 25.9I-E — Position Thesis & Dynamic Exit

> **THE THESIS ENGINE DOES NOT CONTROL BROKER ORDERS IN PHASE 25.9I-E.**
> `position_thesis_enabled` and `dynamic_exit_enabled` are OFF. Every output is a
> recommendation with `is_executable` permanently False. Canonical Portfolio remains
> authoritative, canonical Risk remains the hard gate, Execution remains the only path
> to IBKR.

Written 2026-09-29.

The model this phase replaces:

> buy → wait for a fixed target → sell

with:

> entry → record the thesis → monitor → new evidence → reassess → hold / partial exit /
> full exit / extend target / reduce target / tighten protection → record why

---

## A. What a position thesis is

A record that can answer, at any later moment: **why did we enter, what did we expect,
how long did we expect it to take, what would satisfy it, what would invalidate it, what
risks did we accept, what has changed, and why do we still hold it?**

Not `symbol + entry + target`. That triple cannot answer any of the questions above, and
a position nobody can explain is a position nobody can close for a good reason.

`PositionThesis` (extended from 25.9I-A) carries the target zone, invalidation zone,
horizon, assumptions, confidence, remainder accounting, evidence references and the
lineage back through 25.9I-D's allocation plan to 25.9I-B's session universe.

---

## B. Lifecycle

```
DRAFT → ACTIVE → PARTIALLY_EXITED → CLOSED
              ↘ EXIT_RECOMMENDED ↗
              ↘ INVALIDATED / EXPIRED → CLOSED
```

Legal moves are declared in `LEGAL_THESIS_TRANSITIONS` and illegal ones raise.

- **PARTIALLY_EXITED is still active.** The remainder is held for the same stated reason,
  and treating it as finished is how a system forgets why it holds 60% of something.
- **INVALIDATED is not "lost money".** The reason for holding stopped being true; that can
  happen at a profit.
- **EXPIRED is not a loss either.** The time-based thesis ran out without new evidence.
- **CLOSED is terminal.** Re-entering the same instrument is a **new thesis with a new
  id** — a reopened thesis would make "what did we believe, and when" unanswerable.

---

## C. The three rules that do the work

### 1. Price alone moves nothing (§12)

A price near the target produces `TARGET_APPROACH` → **HOLD plus a reassessment request**
and a WARM data request. It can never, by itself, produce `EXTEND_TARGET`. Raising a
target because the price rose is a position following its own P&L.

Enforced twice: the engine refuses it, and `PositionThesisRevision` (25.9I-A) refuses at
construction any extension whose reason is not evidence-bearing.

### 2. Time does not extend itself (§9)

A 7-day thesis at day 7 is **expired**, not a 14-day thesis. Extension requires material
new evidence, recorded. With none, the policy decides — `FULL_EXIT` by default, `HOLD`
with a WARM re-check if configured. The horizon in the revised thesis is unchanged.

### 3. Cooldown restrains greed, never safety (§24)

The anti-overreaction policy blocks repeated **upside** revisions inside a cooldown
window. It never blocks an exit, a target reduction or a protection tightening. In the
engine, invalidation is checked **before** any policy gate, so a cooldown physically
cannot suppress a risk response.

---

## D. Target zones, invalidation, horizon

**Target zone**, not a single price: `108–112`, never `110.372814`. A zone also makes
partial exits expressible without inventing a second number at the moment of selling.

**Invalidation is a statement about the thesis**, and is deliberately distinct from:

| Concept | What it is | Where it lives |
|---|---|---|
| Thesis invalidation | the reason for holding stopped being true | this phase |
| Risk stop | the most the portfolio will tolerate losing | 25.9I-D risk budgets |
| Broker protective order | an instruction at the venue | **not created by this phase** |

Non-price invalidation is expressible through `ThesisEvidence.invalidates_thesis`, which
covers confidence collapse, event-thesis failure, regime incompatibility and data
integrity failure without pretending they are price levels.

**Horizon** keeps both `initial_horizon` and the current one, so a lengthened horizon is
visible as a change rather than as the way it always was.

---

## E. Revisions and remainder accounting

Every material change creates a **new immutable revision**: a new `PositionThesis` row
naming its predecessor, plus a `PositionThesisRevision` recording old → new, the reason
and the evidence. Nothing is ever mutated in place.

**Fraction convention, stated once and applied everywhere:** `remaining_fraction` and
`realized_fraction` are fractions **of the original position** and always sum to 1.0. An
exit of "40%" means 40% of the original, never 40% of what is left. Two conventions in
one codebase eventually meet in one expression and quietly produce the wrong size.

A partial exit of the entire remainder closes the thesis; `PARTIAL_EXIT(1.0)` is refused
because that is `FULL_EXIT`.

---

## F. What the engine will not invent

- **No expected return, no confidence** where none was supplied — 25.9I-B established
  there is no promoted model, and that holds here.
- **No partial-exit fraction.** `partial_exit_fraction` defaults to `None`: a caller that
  wants a partial exit must say how much. The 40% in the fixtures is a **fixture input**,
  not engine policy.
- **No materiality.** Evidence carries its own, supplied by the caller; 25.9I-F owns event
  materiality. Evidence whose materiality was never measured **cannot clear a materiality
  threshold** — unmeasured is not strong.
- **No calibrated thresholds.** Every policy value is configurable and inert by default.

---

## G. Market-data requests (COLD / WARM / HOT)

The engine **requests** a state; it never activates one. 25.9I-C owns subscriptions and is
externally blocked.

| Situation | Requested | Why |
|---|---|---|
| quiet hold | COLD | delayed data is fine for a multi-day thesis |
| price approaching target | WARM | a decision may be near |
| material negative evidence | WARM/HOT | look now |
| partial or full exit | **HOT**, realtime marker required, ≤60 s | execution-sensitive (25.9I-A freshness policy) |

No adaptive module calls `subscribe`, `market_snapshot` or `init_brokerage_session` —
asserted by AST test.

---

## H. Capital release

A proposed exit emits a `CapitalReleaseRecommendation`: "this would free 40% of the
position, ≈$20". It is a **statement**, not an instruction. 25.9I-D deferred rebalancing
and this does not undo that — nothing allocates the freed capital, and no allocator reads
it automatically.

---

## I. Risk estimation upgrade

25.9I-D's estimator prefers an invalidation distance over volatility. Before a thesis
exists there is no invalidation, so estimates are `PARTIAL`. With a thesis:

```
entry 100, invalidation 94  →  MEASURED, risk_per_dollar 0.06
volatility only             →  PARTIAL
neither                     →  UNKNOWN (and never a number)
```

The dependency runs one way — thesis reads the estimator, the estimator knows nothing
about theses — so there is no cycle between allocation and thesis code.

---

## J. Persistence and lineage

Three append-only tables: `adaptive_position_theses` (one row per revision),
`adaptive_thesis_revisions` (the transition, its reason and evidence),
`adaptive_exit_recommendations` (with `is_executable` stored as a literal 0, so a future
reader can see in the data that these were never orders).

Content-derived ids, `INSERT OR IGNORE`, nothing updated after writing. Lineage carries
`session_snapshot_id` → `admission_decision_id` → `allocation_plan_id` → thesis →
revision → recommendation, which is what 25.9I-G needs to ask whether a revision helped.

---

## K. Production boundary

| Path | Changed? |
|---|---|
| Portfolio / Risk / OrderIntent / Execution / IBKR / capture / subscriptions | **No** |

The engine imports no execution, trading or portfolio module, constructs no `OrderIntent`,
reads no database (every input is an argument), and creates no broker order of any kind.

---

## L. Interaction with the other sub-phases

| Phase | Relationship |
|---|---|
| **25.9I-A** | Contracts extended additively: `PositionThesis`, `TargetZone`, `InvalidationZone`, `ExpectedHorizon`, `PositionThesisRevision`, `RevisionPolicy`, `PositionActionRecommendation` |
| **25.9I-B** | Session/admission lineage carried onto every thesis |
| **25.9I-D** | Risk estimator reused and upgraded; allocation plan id carried; capital release stays unallocated |
| **25.9I-C** | Would honour the HOT requests. **Externally blocked** on ticket #561227 |
| **25.9I-F** | Supplies real event materiality into `ThesisEvidence`; the hooks exist now |
| **25.9I-G** | Uses the revision lineage to ask whether each revision helped |
| **25.9I-H** | The first controlled path from a recommendation toward paper execution |
