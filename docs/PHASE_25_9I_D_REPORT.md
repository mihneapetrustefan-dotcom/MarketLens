# Phase 25.9I-D — Completion Report

**Smart Capital Allocation & Risk Budgeting.** Written 2026-09-29.
Architecture: [`PHASE_25_9I_D_ARCHITECTURE.md`](PHASE_25_9I_D_ARCHITECTURE.md).

---

## 1. Phase status

**COMPLETE.**

The adaptive allocator exists end to end in shadow — capital state, desirability, risk
estimation and budgets, cash reservation, concentration and sector ceilings, as-of
correlation, constrained allocation with recorded reasons, append-only persistence and a
dry-run CLI — and controls nothing. `smart_capital_allocation_enabled` is OFF, no adaptive
module imports execution, trading or portfolio, no `OrderIntent` is constructed anywhere,
and broker writes, paper orders and live orders are all zero. Two real defects surfaced
during implementation; both were found by running the thing and both are fixed.

---

## 2. Git state

- **Branch:** `ibkr-paper-validation-fixes`
- **Baseline HEAD:** `80d8434` · **Final HEAD:** `f701e41` · working tree clean
- **Commit:** `f701e41` "Phase 25.9I-D: an allocator that is allowed to say no"

**Diff: 12 files, +3,200 / −5.**

| Kind | Files |
|---|---|
| New source (9) | `src/domain/allocation_models.py`, `src/adaptive/{allocator,desirability,risk_budget,correlation,allocation_repository}.py`, `src/data_access/adaptive_allocation_schema.py` |
| New script | `scripts/adaptive_allocation.py` |
| New tests | `tests/adaptive/{test_capital_allocation,test_allocation_boundary}.py` |
| New docs | architecture + this report |
| Modified (1) | `tests/adaptive/test_freshness_and_legacy.py` (+12/−5) — the persistence guard, updated deliberately |

No existing source file was modified.

---

## 3. Baseline before the phase

| Item | Value |
|---|---|
| Baseline commit | `80d8434`, clean tree |
| Tests | **4,516 run — OK, 0 failures, 0 errors, 1 skipped** |
| Runtime | 444 s |

---

## 4. Repository findings

**Found and reused:**

| Abstraction | Where | Use here |
|---|---|---|
| `CapitalAllocationProposal`, `CashReserve`, `RiskBudget`, `AllocationConstraint`, `AllocationRejectionReason`, `ExecutionEconomics`, `DecisionLineage` | 25.9I-A `adaptive_models` | Reused directly; not re-declared |
| `SessionSelection`, admission ids, sectors | 25.9I-B | Candidate source and lineage |
| `AllocationProposal` → `RiskEngine` → `RiskDecision` → `OrderIntent` | Phase 11 `portfolio_models` | Left as the only production path |
| `PositionSizingStrategy` ABC | `portfolio/sizing.py` | Untouched; not subclassed or referenced |
| `ExecutionSafety`, `TradingModeStore` | Phase 14 / 25.9E | Untouched; not imported |
| `MarketHistory` (as-of reads) | 25.9I-B | Volatility and correlation inputs |

**Deliberately NOT duplicated:** no second risk engine, no second sizing strategy, no
second order-intent shape, no second exposure model.

**Two findings that changed the design:**

1. **No fractional-share evidence.** `broker_instrument_mapping.minimum_quantity`,
   `quantity_increment` and `lot_size` are **NULL for every mapped instrument**. So the
   allocator proposes notional only and never share quantities.
2. **No promoted model.** No promotion table exists (2 trained models, 2 evaluations, 5
   signals), confirming 25.9I-B: `expected_return` and `model_confidence` stay
   unavailable.

---

## 5. What was implemented

| Subsystem | Files | Behaviour | Rationale |
|---|---|---|---|
| Capital state | `allocation_models.py` | equity / cash / existing exposures / status, no buying power, no leverage | Conflating equity, cash and buying power is a known source of overspending; no leverage policy exists to inherit |
| Desirability | `desirability.py` | positives + penalties + weights + unavailable + completeness | A scalar cannot be argued with later |
| Risk estimation | `risk_budget.py` | invalidation → MEASURED, volatility → PARTIAL, nothing → UNKNOWN | Downside and size are different quantities |
| Risk budgets | `risk_budget.py` | account + per-position, unknown existing risk ⇒ unknown remaining | Cannot spend against a number that cannot be computed |
| Cash reservation | `allocator.py` | minimum cash + opportunity reserve, held before allocation | Cash is first-class, not residue |
| Constraints | `allocator.py` | position / sector / correlation / risk / capital / minimum size, each recorded | §26: why this amount, why not more |
| Correlation | `correlation.py` | as-of daily returns, min 30 observations, threshold OFF | Missing ≠ zero; no evidence for a cut-off |
| Allocation | `allocator.py` | desirability share → ordered cuts → leftover stays cash | Neither equal weight nor winner-take-all |
| Persistence | `adaptive_allocation_schema.py`, `allocation_repository.py` | 2 append-only tables, content ids, idempotent | Minimal and justified |
| Shadow CLI | `scripts/adaptive_allocation.py` | dry-run default, `--compare`, `--persist`, `--hold` | Usable while the flag is off |
| Tests | 2 files, 75 tests | §31–§33 coverage | — |

---

## 6. What worked — with evidence

All figures below are actual allocator output.

**A. $100, strong / moderate / weak** — unequal, all three reduced by different limits:

```
strong    reduced  20.00  risk 1.34  des 0.738  max_position_fraction
moderate  reduced  17.89  risk 2.00  des 0.530  max_position_fraction; per_position_risk_budget
weak      reduced   8.24  risk 1.66  des 0.320  per_position_risk_budget; account_risk_budget
deployed 46.13 | reserved 10.00 | unallocated 13.87 | cash 53.87 | risk 5.00 | identity True
```

**B. $100, only weak candidates → 100% cash:**

```
w1 not_funded 0.00  des 0.248  "desirability 0.248 is below the minimum of 0.25"
w2 not_funded 0.00  des 0.224
deployed 0.00 | cash 100.00 | identity True
```

**C. $100, five technology candidates → sector cap leaves cash, not forced diversification:**

```
t0/t1/t2 funded 12.29 / 12.14 / 12.00
t3/t4    not_funded 0.00  "max_sector_fraction; minimum_allocation"
deployed 36.43 | unallocated 23.57 | cash 63.57
```

**D. $100 with $15 already held in `strong` → existing exposure consumes its own cap:**

```
strong reduced  5.00  (position ceiling already 15% used)
other  reduced 20.00
deployed 25.00 | cash 60.00
```

**E. Unknown downside → capped, and total risk reported as unknown:**

```
known  reduced 20.00  risk 1.79
nodata reduced  8.00  risk UNKNOWN  "unknown_risk_notional_cap"
total_capital_at_risk = None | status PARTIAL
```

**Determinism:** identical inputs give identical plan ids and lines; permuting candidate
order gives identical economics (tests assert both).

**Performance:** 5 candidates 0.18 ms · 15 → 0.57 ms · 31 → 0.79 ms · 130 → 3.07 ms per
allocation. No bottleneck; the loop is linear in candidates with a small constant.

**Persistence:** two `--persist` runs against a real-data copy → 1 plan, 31 lines,
`new=True` then `new=False`; `execution_orders` and `paper_orders` stayed at 0.

---

## 7. What did not work

**Two real defects, both found by running the code, both fixed.**

**(a) The cash reserve was subtracted twice.** The first $100 smoke test produced
`unallocated = −1.58`.
*Root cause:* `deployable_capital` is already net of reserves, and `unallocated` subtracted
them again. *Fix:* `unallocated = deployable − deployed`; added `capital_above_ceiling`
and a `check_identity()` method asserting
`cash = deployed + unallocated + reserved + above_ceiling`. *Status:* fixed, asserted in
three tests.

**(b) A $100 account with 31 candidates funded nothing.** Every proportional share was
≈$1.94, below the $5 minimum, so all 31 lines were zeroed.
*Root cause:* the only minimum-size behaviour was "skip". *Fix:* added `min_size_policy`
with `skip` and `concentrate`, defaulting to `concentrate` — shares below the minimum are
raised to it in rank order while capital lasts. Every cap and the desirability floor still
apply, and leftover capital still stays cash. *Status:* fixed; both policies tested.

**(c) Zero-allocation reasons were misleading.** Lines that got nothing reported the
"raised to minimum size" note instead of the binding constraint.
*Root cause:* the note was prepended before the constraint reason was computed.
*Fix:* the binding reason is inserted first. *Status:* fixed — output now reads
"reduced to zero by account_risk_budget, minimum_allocation".

**(d) Two test-fixture gaps, correct behaviour in both cases.** A correlation test funded
nothing because the smooth synthetic series has ~zero volatility → risk UNKNOWN → no cap
configured → zero allocation. And the 25.9I-A persistence guard failed because I-D adds a
second schema module. Both fixtures/assertions updated deliberately; no production code
changed for either.

**Assumption disproven.** I expected capital to be the binding constraint for small
accounts. It is not: at $100, $250 and $500 with identical candidates, deployment stays
≈$47.70 because the **$5 account risk budget** binds first. More capital did not produce
more deployment, which is the phase's thesis demonstrated by accident.

**No unresolved implementation defects remain.** The limitations in §23 are design
boundaries, not bugs.

---

## 8. What was deferred

| Deferred | Owner |
|---|---|
| Real Portfolio control / promoting the allocator | 25.9I-H, behind the flag |
| `OrderIntent` construction from a plan | later phase; no hand-off exists yet |
| Paper or live execution | 25.9I-H |
| Dynamic rebalancing (selling to fund a better idea) | 25.9I-E, once a thesis exists |
| Real invalidation levels (MEASURED instead of PARTIAL risk) | 25.9I-E |
| Event strength in desirability, pre-event sizing | 25.9I-F |
| Market-data cost as a live input | 25.9I-C, blocked on #561227 |
| Correlation threshold calibration | needs shadow evidence (25.9I-G) |
| Performance attribution of allocation quality | 25.9I-G |

---

## 9–10. Capital vs risk evidence

Equal notional, different risk, allocator responds: with `per_position_risk_budget = 2.0`,
a candidate at 2% downside per dollar and one at 20% receive different sizes, and the
risky one's line records `per_position_risk_budget` as the binding constraint. Test:
`test_higher_risk_reduces_allocation`.

Aggregate: with a $5 account risk budget and six candidates at 10% downside, total
capital at risk never exceeds $5 — `test_account_risk_budget_limits_aggregate_risk`.

---

## 11. Cash reserve evidence

- `minimum_cash_fraction = 0.25` on a $100 account: reserved ≥ $25 and retained cash ≥ $25
  after allocation.
- Two reserves (minimum + opportunity) are held separately and both survive.
- **Ceiling ≠ target:** `max_deployment_fraction = 0.7` with one strong candidate deploys
  well under $70; a portfolio already at the ceiling has **zero** deployable capital.

---

## 12. Concentration / correlation

- **Sector:** `max_sector_fraction` counts existing exposure; excess becomes cash
  (example C). Forced diversification is impossible — no code path adds a candidate to
  reach a target.
- **Correlation:** implemented over daily returns from `price_candle_cache`, window 120,
  minimum 30 overlapping observations, read-only and bounded at `as_of`.
  **Threshold disabled by default** (`max_pairwise_correlation = None`). Unmeasured pairs
  return `None` and earn no diversification credit.
- **Limitation:** with the cache ending 2026-08-27 and a 120-day window, correlations are
  computable for the parent universe but have never been validated against a threshold —
  that needs 25.9I-G evidence.

---

## 13. Execution economics

| Field | Status |
|---|---|
| `expected_gross_edge` | **UNAVAILABLE** — no promoted model |
| `spread_cost` | **UNAVAILABLE** — no spread history in the repository |
| `slippage_estimate` | **UNAVAILABLE** — no fitted model |
| `commission_estimate` | **UNKNOWN** — not supplied by this phase |
| `market_data_cost_estimate` | **UNKNOWN** — ticket #561227 unresolved; not $0.01, not free, not "covered by the $14.50 subscription" |
| `other_execution_cost` | **UNKNOWN** |

No unknown was converted to zero anywhere. `ExecutionEconomics.expected_net_edge` returns
`None` when the gross edge is unknown, and `is_complete` reports the gap.

---

## 14. Small-account result

| Equity | Deployed | Funded | Cash | Binding constraint |
|---|---|---|---|---|
| $100 | 46.13 | 3 | 53.87 | account risk budget |
| $250 | 47.70 | 2 | 202.30 | account risk budget |
| $500 | 47.70 | 2 | 452.30 | account risk budget |

**Limitations discovered:** no fractional-share support is evidenced anywhere in the
repository (all quantity metadata is NULL), so allocations are notional only; and with 31
candidates and $100 the proportional shares are unusable without the `concentrate`
policy (§7b).

---

## 15. Lineage / persistence

Two tables — `adaptive_allocation_plans`, `adaptive_allocation_lines`. Content-derived
plan ids from (as_of, config version, revision, candidate set). `INSERT OR IGNORE`
throughout. Revisions append and name their predecessor; the earlier plan stays readable.
`session_snapshot_id` and `admission_decision_id` are stored on both the plan and every
line, linking back to the 25.9I-B selection. Production databases contain **no**
`adaptive_*` tables — the demonstration ran on a scratch copy.

---

## 16. Feature flag state

| Flag | Value |
|---|---|
| `smart_capital_allocation_enabled` | **False** |
| all other adaptive flags | **False** |
| `layer_enabled` | **False** |

---

## 17. Production behaviour

| Path | Changed? |
|---|---|
| Portfolio | **NO** |
| Risk | **NO** — not imported, not wrapped, still the hard gate |
| Position sizing | **NO** |
| OrderIntent | **NO** — not constructed anywhere in `src/adaptive` |
| Execution | **NO** |
| Capture | **NO** |
| IBKR | **NO** |
| Subscriptions | **NO** |

---

## 18. Broker safety

| Metric | Value |
|---|---|
| Broker writes attempted | **0** |
| Broker writes succeeded | **0** |
| Paper orders caused by this phase | **0** |
| Live orders | **0** |

---

## 19. Test results

| Suite | Result | Runtime |
|---|---|---|
| Targeted `test_capital_allocation` | **55 OK**, 0 fail, 0 skip | 0.03 s |
| Targeted `test_allocation_boundary` | **20 OK**, 0 fail, 0 skip | 1.1 s |
| Adaptive suite (all) | **247 OK**, 0 fail, 0 skip | 5.3 s |
| Portfolio suite | **273 OK**, 0 fail, 0 skip | 1.0 s |
| Capture / session | 126 OK (unchanged by this phase) | — |
| Research integrity | CLEAN, exit 0, `d20_state NOT_CONSUMED` | — |
| Trading readiness | exit 0, order submission impossible | — |
| Live safety | exit 0, 16/16 | — |
| **Full suite** | **4,591 run — OK, 0 failures, 0 errors, 1 skipped** | 362 s |

Baseline 4,516 + 75 new = 4,591. ✓

---

## 20. As-of / leakage evidence

- **Truncation test:** a plan built from a database containing future candles is identical
  — plan id, per-line allocations and per-line `risk_per_dollar` — to one built from a
  database physically truncated at `as_of`.
- **Volatility** and **correlation** inputs both come from `MarketHistory`, read-only and
  bounded by `timestamp <= as_of`; tests assert the series carry no later timestamps.
- **Missing correlation** returns `None` with a stated reason and grants no credit.
- **Protected data:** no allocation module references `anchor_v2`, `validate_d20`,
  `protected_ledger` or `return_60d`; none imports `src.research`.
- **No future P&L** is used anywhere — there is no outcome input in the allocator at all.

---

## 21. D20 status

| Question | Answer |
|---|---|
| Protected test executed? | **NO** |
| Result inspected? | **NO** |
| Result inferred? | **NO** |
| Result consumed? | **NO** — ledger `NOT_CONSUMED` |
| Hypothesis changed? | **NO** |
| anchor-v2 changed? | **NO** |
| Protected window changed? | **NO** |

The calendar boundary has passed, but D20 remains a separate one-shot procedure and was
not touched by this phase.

---

## 22. Scope-creep review

| Check | Finding |
|---|---|
| Unrelated files | None |
| Execution changes | None |
| Capture changes | None |
| IBKR changes | None |
| Model promotion changes | None |
| Protected research changes | None |
| Configuration changes | None — no config file added or edited |
| Modified existing files | One test file (+12/−5), the persistence guard, updated deliberately |

---

## 23. Known limitations / technical debt

1. **No qualified alpha model**, so `expected_return` and `model_confidence` are absent
   and desirability is an attention-weighted preference. Allocation is conservative by
   construction, not by calibration.
2. **Risk estimates are PARTIAL at best** in practice: without theses there are no
   invalidation levels, so every real estimate comes from volatility.
3. **Risk budgets have no defaults.** Unset means "not configured" and the plan reports a
   gap; production values need evidence this repository does not have.
4. **Correlation thresholds are unvalidated** and disabled by default.
5. **Market-data cost is unknown** while #561227 is open, so `market_data_cost` never
   enters desirability.
6. **No rebalancing:** existing positions only reduce capacity; nothing is ever sold.
7. **Notional only** — no share quantities, and no evidence that fractional trading is
   available for any instrument.
8. **`concentrate` deploys more than a strict proportional read would** for small
   accounts. It is bounded by every cap, but it is a policy choice, and it is on by
   default.
9. **Capital state is supplied, not read from the broker.** Deliberate for shadow; a live
   integration needs a freshness rule of its own.

---

## 24. Readiness

**Readiness for 25.9I-E: YES.** The thesis phase has what it needs — `PositionThesis` and
`InvalidationZone` from I-A, a risk estimator that already prefers invalidation distance
over volatility, and an allocator that will upgrade from PARTIAL to MEASURED the moment
real invalidation levels exist. No blockers.

**25.9I-C: BLOCKED EXTERNALLY.** No new repository evidence about ticket #561227 — no
reply is recorded anywhere in the repo, and the cost model still reports every price as
unknown. The paper account also still receives delayed quotes. I-C cannot be calibrated
until IBKR answers whether `/iserver/marketdata/snapshot` calls are billed per request.

---

## 25. Final verdict

**PHASE 25.9I-D = COMPLETE.**

Cash is first-class and 100% cash is a valid, tested outcome; zero allocation is valid and
always carries a reason; capital and risk are spent as separate budgets, demonstrated by
deployment staying ≈$47.70 across $100/$250/$500 because the risk budget binds first;
unknown downside, correlation, cost and expected return are never converted to zero;
allocation is deterministic and order-invariant; lineage reaches back to the I-B session
snapshot; persistence is append-only and idempotent on real data; and the flag is OFF with
Portfolio, Risk, Execution, capture and IBKR all untouched. Full suite 4,591 OK, 1 skipped.
