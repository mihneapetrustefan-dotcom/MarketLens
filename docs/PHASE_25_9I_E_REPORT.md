# Phase 25.9I-E — Completion Report

**Position Thesis & Dynamic Exit.** Written 2026-09-29.
Architecture: [`PHASE_25_9I_E_ARCHITECTURE.md`](PHASE_25_9I_E_ARCHITECTURE.md).

---

## 1. Phase status

**COMPLETE.**

The thesis lifecycle exists end to end in shadow — creation, lifecycle states, target
zones, invalidation, horizons, time decay, immutable revisions, partial exits with exact
accounting, target extension and reduction, protection tightening, market-data state
requests, anti-overreaction with a safety override, capital release, append-only
persistence and a scenario CLI — and controls nothing. Both flags are OFF, every
recommendation reports `is_executable = False`, the engine imports no execution, trading
or portfolio module and reads no database, and broker writes, paper orders and live
orders are all zero.

---

## 2. Git state

- **Branch:** `ibkr-paper-validation-fixes`
- **Baseline HEAD:** `38b2328` · **Final HEAD:** `e26a03f` (implementation) → this report
- **Working tree:** clean
- **Diff:** 9 files, **+2,453 / −5**

| Kind | Files |
|---|---|
| New source (4) | `src/adaptive/thesis.py`, `src/adaptive/thesis_repository.py`, `src/data_access/adaptive_thesis_schema.py` |
| New script | `scripts/adaptive_thesis.py` |
| New tests (2) | `tests/adaptive/test_position_thesis.py`, `tests/adaptive/test_thesis_boundary.py` |
| New docs | architecture + this report |
| Modified (2) | `src/domain/thesis_models.py` (+155, purely additive), `tests/adaptive/test_freshness_and_legacy.py` (+9/−5, the persistence guard) |

No existing production module was modified. The only source change is additive fields and
types on the 25.9I-A thesis contracts, verified by re-running the I-A contract tests
(61 OK) immediately after the extension.

---

## 3. Baseline

| Item | Value |
|---|---|
| Baseline commit | `38b2328`, clean tree |
| Tests | **4,591 run — OK, 0 failures, 0 errors, 1 skipped** |
| Runtime | 460 s |

---

## 4. Repository findings

**Found in 25.9I-A and reused, not rebuilt:** `PositionThesis`, `TargetZone`,
`InvalidationZone`, `ExpectedHorizon`, `PositionThesisRevision` (with its two hard rules),
`ThesisRevisionReason`, `EVIDENCE_BEARING_REASONS`, `PositionAction`,
`PositionActionRecommendation`, `RevisionPolicy`.

**Reused from other phases:** 25.9I-D's `PositionRiskEstimator` (upgraded, not replaced),
25.9I-B's session/admission lineage, 25.9I-A's `MarketDataState`,
`MarketDataRequirement` and `RealtimeActivationReason`.

**Canonical models deliberately NOT duplicated:** `Position`, `PortfolioSnapshot`,
`AllocationProposal`, `RiskDecision`, `OrderIntent`, `TargetPosition`, execution fills and
broker reconciliation. The engine touches none of them.

**Extended only where justified:** `ThesisStatus` + legal transitions, `ThesisEvidence`,
remainder accounting (`remaining_fraction`, `realized_fraction`, notional helpers),
initial-vs-current horizon and confidence, and lineage ids. Every new field has a default,
so 25.9I-A constructions remain valid.

---

## 5. What was implemented

| Area | Files | Behaviour |
|---|---|---|
| Lifecycle | `thesis_models.py` | 7 states, declared legal transitions, CLOSED terminal |
| Target zones | reused | zone not point; validated bounds |
| Invalidation | reused + evidence | price-based and non-price (`invalidates_thesis`) |
| Horizon / time decay | `thesis.py` | expiry detected; **never auto-extends** |
| Revisions | `thesis.py` | immutable, predecessor named, deterministic ids |
| Partial exits | `thesis.py` | fractions of the original, exact reconciliation |
| Extension / reduction / tightening | `thesis.py` | evidence-gated; I-A rules re-checked at construction |
| Full exit | `thesis.py` | invalidation, price breach, expiry |
| Market-data requests | `thesis.py` | COLD/WARM/HOT + freshness; never activates |
| Anti-overreaction | `thesis.py` | upside cooldown with safety override |
| Capital release | `thesis.py` | statement only, never reallocated |
| Risk upgrade | `thesis.py` | PARTIAL → MEASURED via invalidation |
| Persistence | 2 files | 3 append-only tables, idempotent |
| CLI | `scripts/adaptive_thesis.py` | 7 deterministic scenarios, dry-run default |
| Tests | 2 files, 73 tests | §45 coverage |

---

## 6–12. What worked — actual engine output

All output below is from `scripts/adaptive_thesis.py`. Base thesis: entry 100, target
108–112, invalidation 93–94, horizon 7 days, notional 50.

### §9 Positive revision (fixture-supplied evidence)

```
action : partial_exit 40%
data   : requires hot (exit_candidate) | executable False
before : target 108-112 | invalidation 93-94 | horizon 7d | remaining 1.00
after  : target 117-122 | invalidation 101-102 | horizon 12d
         remaining 0.60 realized 0.40 | status partially_exited
frees  : 40% of the position = 20.00 (not reallocated here)
why    : ev-positive: material positive development, supplied by the fixture
```

### §10 Negative revision

```
action : full_exit 100%
data   : requires hot (material_negative_event) | executable False
after  : status invalidated
frees  : 100% of the position = 50.00 (not reallocated here)
why    : the thesis was invalidated by ev-negative
```

### §11 Price-only — the target does **not** move

```
action : hold
data   : requires warm (target_proximity) | executable False
why    : price 109.0 is approaching the target zone 108.0-112.0; reassessment
         requested. Price movement alone is not evidence and cannot extend a target.
```

And when an extension is explicitly *requested* on price alone:

```
action : hold
held   : extension refused: no material evidence
```

### §12 Time decay — the horizon does **not** extend

```
action : full_exit 100%
after  : horizon 7d (unchanged) | status expired
why    : the expected horizon ended and no material new evidence supports
         extending it; a horizon does not extend itself
```

### §13 Partial-exit accounting

Successive exits of 40% then 30% of the **original** give remaining 0.30 / realized 0.70;
notional follows (30.00 / 20.00 after the first). Exiting the whole remainder closes the
thesis. `PARTIAL_EXIT(1.0)`, 0.0, negatives and over-exits are all refused. A five-step
lifecycle (entry → partial → extension → tightening → invalidation) keeps every
predecessor, and the original thesis still reads target 108–112, remaining 1.00.

---

## 7. What did not work

**No unresolved implementation defects remain.** Honestly, this phase went in cleanly:
both new test files passed on their first run (48 and 25), which is unusual and worth
recording rather than dressing up.

**What did need fixing / attention:**

1. **The persistence guard failed again, by design.** `test_persistence_is_added_only_where_a_phase_justified_it`
   asserted `src/data_access/` held exactly the universe and allocation schemas, and
   explicitly asserted no "thesis" module existed. I-E adds one. *Fix:* the assertion now
   lists all three and records which phase argued for each. This is the third phase the
   guard has caught, which is the point of it.

2. **A shell heredoc could not carry the patch script.** The additive edit to
   `thesis_models.py` contains nested triple-quoted docstrings and backticks; the heredoc
   terminated early and the edit aborted with a parse error before writing anything.
   *Fix:* wrote the patch to a scratch file and executed it. No repository file was left
   half-edited.

3. **A risk of silent contract breakage that did not materialise.** Extending a frozen
   dataclass used by three earlier phases could have broken them. Mitigated by giving
   every new field a default and re-running the I-A contract suite immediately (61 OK)
   before continuing.

**Assumption checked rather than assumed:** the cooldown/safety interaction (§24) is the
one place where a plausible implementation is silently wrong — a policy gate evaluated
before the invalidation check would suppress exits. The engine checks invalidation first,
and two tests assert an invalidation and a price breach both pass through a live cooldown.

---

## 8. What was deferred

| Deferred | Owner |
|---|---|
| Live Event Intelligence feeding `ThesisEvidence` | 25.9I-F |
| Real event materiality values | 25.9I-F |
| Broker-native protective stops / brackets | 25.9I-H |
| Order modification, cancel/replace | 25.9I-H |
| Paper or live execution of any recommendation | 25.9I-H |
| Realtime activation of the requested HOT state | 25.9I-C (blocked) |
| Target-calibration model (what a target *should* be) | needs a promoted model |
| Reallocating released capital / real rebalancing | 25.9I-D + Portfolio, later |
| Counterfactual attribution of revisions | 25.9I-G |

---

## 14. Risk estimation upgrade

```
with a thesis   : MEASURED, risk_per_dollar 0.06   (entry 100, invalidation 94)
volatility only : PARTIAL                           (25.9I-D behaviour)
neither         : UNKNOWN, and carries no number at all
```

Tested three ways, including that a supplied reference price overrides the entry price.

---

## 15. Anti-overreaction

| Situation | Result |
|---|---|
| second upside revision, 10 min after the last, cooldown 6 h | **withheld** — `blocked_by_policy: minimum revision interval has not elapsed` |
| invalidating evidence, same 10 min, same cooldown | **FULL_EXIT allowed**, `blocked_by_policy` empty |
| price breaching invalidation, 1 min after a revision | **FULL_EXIT allowed** |
| `max_revisions_per_day = 1`, already 1 today | upside **withheld** |

Default policy is inert: zero interval, zero cooldown, no partial-exit fraction.

---

## 16. Market-data requirement

| Action | Requested state | Activated? |
|---|---|---|
| quiet hold | COLD | no |
| target proximity | WARM | no |
| material negative evidence, no proposal | WARM | no |
| partial exit | HOT, realtime marker, ≤60 s | no |
| full exit | HOT, realtime marker, ≤60 s | no |

**Nothing is activated.** No adaptive module calls `subscribe`, `market_snapshot` or
`init_brokerage_session` (AST-asserted), and market-data behaviour is unchanged.

---

## 17. Persistence / lineage

Three tables: `adaptive_position_theses` (one row per revision),
`adaptive_thesis_revisions`, `adaptive_exit_recommendations`.

- **Idempotent on real data:** two `--persist` runs → 5 new recommendations, then 0.
- **History preserved:** after a revision, the predecessor still reads target 108–112 and
  remaining 1.00 while the successor reads 117–122 and 0.60.
- **Non-executable by record:** `is_executable` stored as literal 0; a query for
  executable rows returns **0**.
- **Lineage stored:** `session_snapshot_id = su-x`, `admission_decision_id = adm-x`,
  `allocation_plan_id = alloc-x` on the thesis row.
- **Production databases hold no `adaptive_*` tables**; the demonstration ran on a scratch
  copy.

---

## 18. Feature flags

| Flag | Value |
|---|---|
| `position_thesis_enabled` | **False** |
| `dynamic_exit_enabled` | **False** (and suppressed if enabled alone) |
| all other adaptive flags | **False** |

With both forced on, `describe()` still reports `enables_broker_writes: False`.

---

## 19. Production behaviour

Portfolio **NO** · Risk **NO** · OrderIntent **NO** · Execution **NO** · capture **NO** ·
IBKR **NO** · subscriptions **NO**.

---

## 20. Broker safety

| Metric | Value |
|---|---|
| Broker writes attempted | **0** |
| Broker writes succeeded | **0** |
| Paper orders caused by this phase | **0** |
| Live orders | **0** |

---

## 21. Test results

| Suite | Result | Runtime |
|---|---|---|
| Targeted `test_position_thesis` | **48 OK**, 0 fail, 0 skip | 0.01 s |
| Targeted `test_thesis_boundary` | **25 OK**, 0 fail, 0 skip | 1.0 s |
| Adaptive suite (all) | **320 OK**, 0 fail, 0 skip | 6.2 s |
| Portfolio suite | **273 OK** | 0.9 s |
| Research integrity | CLEAN, exit 0 | — |
| Trading readiness | exit 0 | — |
| Live safety | exit 0, 16/16 | — |
| **Full suite** | **4,664 run — OK, 0 failures, 0 errors, 1 skipped** | 361 s |

Baseline 4,591 + 73 new = 4,664. ✓

**Performance:** 100 thesis creations 3.1 ms · 100 reassessments 1.9 ms · 10 sequential
revisions 0.6 ms · 1,000 create-and-reassess cycles 53 ms. No bottleneck; the engine does
no I/O.

---

## 22. As-of / leakage

- **Truncation test:** risk estimate and assessment (including the recommendation id) are
  identical between a database containing future candles and one physically truncated at
  `as_of`.
- **The engine reads no database at all** — no `sqlite3`, no `connect(`, no `SELECT` in
  `thesis.py`. Every input is an argument, so there is no query to bound.
- **No future outcome or P&L** appears anywhere in the engine.
- **No protected artefact** is referenced.

---

## 23. D20 status

| Question | Answer |
|---|---|
| Protected test executed? | **NO** |
| Result inspected? | **NO** |
| Result inferred? | **NO** |
| Result consumed? | **NO** — ledger `NOT_CONSUMED` |
| Hypothesis changed? | **NO** |
| anchor-v2 changed? | **NO** |
| Protected window changed? | **NO** |

---

## 24. Scope-creep review

| Check | Finding |
|---|---|
| Unrelated files | None |
| Portfolio / Risk / execution / capture / IBKR changes | None |
| Model promotion changes | None |
| Protected research changes | None |
| Config changes | None |
| Modified existing files | Two: additive contract fields, and the persistence guard |

---

## 25. Known limitations / technical debt

1. **No event source.** `ThesisEvidence` is supplied by the caller; nothing produces it
   automatically. Until 25.9I-F, every revision is fixture- or operator-driven.
2. **No target model.** The engine can record and revise a target zone but has no basis
   for proposing one — that needs a promoted model, which does not exist.
3. **Materiality is an input, never a measurement.** Thresholds are configurable and
   default to 0.0, so today any *measured* materiality clears them.
4. **Partial-exit fractions are always supplied.** The engine will not compute one, so
   §15's "dynamic fraction" remains conceptual until upside, uncertainty and event
   materiality inputs exist.
5. **Time-decay policy is binary** (FULL_EXIT, PARTIAL_EXIT or HOLD at expiry). A graded
   response needs evidence to calibrate.
6. **No executable quantity.** Recommendations are fractions; share conversion needs an
   execution-grade price and fractional-share facts the repository does not have.
7. **Capital release is inert** — nothing consumes it, by design.
8. **Non-price invalidation is a boolean** (`invalidates_thesis`) rather than a structured
   condition language. Sufficient for now; 25.9I-F may need more.

---

## 26. Readiness

**25.9I-F: YES.** The hooks it needs exist — `ThesisEvidence` with direction, materiality,
confidence and an invalidation flag; revision reasons already covering material positive
and negative events; and a reassessment path that turns evidence into recommendations. F
supplies real events; nothing in E blocks it.

**25.9I-C: BLOCKED EXTERNALLY.** No new repository evidence on ticket #561227. The engine
now *requests* HOT state for exits, which sharpens the dependency: until the market-data
cost and realtime questions are answered, those requests cannot be honoured.

**25.9I-G prerequisites:** the lineage is in place (session → admission → allocation →
thesis → revision → recommendation) and persisted. What remains before attribution is
meaningful: real evidence from F, and shadow sessions long enough to contain revisions
worth attributing. G also needs outcome data, which needs realtime capture — so G is
downstream of C in practice.

---

## 27. Final verdict

**PHASE 25.9I-E = COMPLETE.**

Price alone produces HOLD and a reassessment request, never a raised target; a horizon
that ends does not extend itself; a cooldown withholds an upside revision while an
invalidation in the same window still produces a full exit; partial-exit fractions are of
the original and reconcile exactly across a five-revision lifecycle; the thesis upgrades
I-D's risk estimate from PARTIAL to MEASURED; HOT data is requested and never activated;
persistence is append-only and idempotent with `is_executable` stored as 0; both flags are
OFF and Portfolio, Risk, Execution, capture and IBKR are untouched. Full suite 4,664 OK,
1 skipped.
