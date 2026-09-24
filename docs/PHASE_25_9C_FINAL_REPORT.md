# Phase 25.9C — Data, Label & Pre-Protected Readiness Report

**Written** 2026-09-14 · **Protected test** NOT EXECUTED · **Protected result** UNSEEN

---

## A. Executive summary

**The future protected test would have failed silently. It no longer
will.**

**FACT** The price cache would never have fetched the forward prices the
D20 test needs.
**EVIDENCE** On the production data, a refresh run on 2026-09-29 would
have skipped **149 of 149** protected instruments, and the SPY
benchmark. Fixed logic skips **0**.
**STATUS** Fixed and tested.

Four more readiness problems were found and closed: the single-use lock
lived in a disposable database, "waiting for the future" and "missing
data" were indistinguishable, a refresh during a trading session was
counted as having observed that session's close, and split-adjusted
prices could silently mix vintages. The protected window was touched
only through counts, dates and hashes.

---

## B. Pre-change baseline

| | |
|---|---|
| branch / commit | `ibkr-paper-validation-fixes` / `04ba986`, clean |
| production snapshot | 276,164,608 bytes |
| labels | 18,874, all v1 |
| newest daily / minute candle | 2026-09-05 / 2026-09-04 17:29 |
| protected test | never executed |
| full suite | 4,022 OK |

---

## C. Anchor-v2 re-audit

Independently re-verified on non-protected data:

| Check | Result |
|---|---|
| deterministic across 3 independent builds | identical hash `933aee42…` |
| rebuild on the same copy | same 10,504 rows, same hash |
| v1 labels after every build | byte-identical to production |
| holiday absent from data is skipped | tested (Labor Day) |
| year boundary | tested |
| DST, early close | not applicable: daily windows use session dates |

**One semantic worth knowing, unchanged:** session 0 is the first session
**at or after** the event. So an after-close Friday event's `d1` ends at
the second session after it. That is the engine's existing definition.

**Status: PASS.**

---

## D. Method versioning

v2 labels carry `name …anchor-v2`, `label_version v2`, `calculation
anchor-v2`. The readiness checker only counts a label if all three
match; a v1 row is never reinterpreted as v2 (tested).

**Status: PASS.**

---

## E–I. Price, benchmark and label readiness

### The refresh defect — the most important finding

`cache_price_candles.py` requests daily prices to `anchor + 35 days`, and
`is_range_cached` treated a recorded range as complete **all the way to
its end, including the part that was still in the future when
requested**.

| Refresh on 2026-09-29 | Protected instruments skipped |
|---|---|
| old logic | **149 of 149** |
| fixed logic | **0** |

The 25.9B runbook said "a normal pipeline run covers it". It would not
have. Every protected d20 label would have stayed unresolved forever.

**Fix:** coverage ends at `min(range_end, requested_at)`; a refresh
fetches only the uncovered tail, never asks for the future, and records
an end that is never in the future again.

### Adjustment vintages

Polygon's daily close is split-adjusted **as of the fetch date**, and
stored rows are never overwritten. An incremental fetch after a split
would join two vintages inside one window and fabricate a move. Each
incremental fetch now re-reads ten stored days and records a break in
`price_cache_vintage_checks`. Affected windows become
`PRICE_VINTAGE_BREAK`, not silently priced.

**Classification: HIGH severity if it occurs, rare. Now detected.**

### The readiness states

| State | Meaning |
|---|---|
| `NOT_YET_OBSERVABLE` | session has not closed yet — healthy |
| `STALE_CACHE` | session closed, cache not refreshed past it — run the refresh |
| `EXPECTED_DATA_MISSING` | cache refreshed past it, still no price — a real problem |
| `LABEL_NOT_BUILT` | prices present, no anchor-v2 row |
| `MISSING_BENCHMARK_PRICE` | no SPY close on a weekday end |
| `BENCHMARK_CALENDAR_MISMATCH` | crypto window ends on a weekend — structural |
| `PRICE_VINTAGE_BREAK` | adjustment changed inside the window |
| `FEATURE_MISSING`, `MISSING_MAPPING`, `INVALID_ANCHOR` | named exclusions |

**A checker bug found and fixed.** Measured: 226 of 305 US equity caches
end exactly one day before their request date, because a fetch during a
trading day cannot contain that day's close. The first version counted
the request date as observed and wrongly flagged 9 observations as
missing data.

---

## J. Working-copy workflow

`docs/D20_PROTECTED_TEST_RUNBOOK.md`. The label builder refuses
`data/marketlens.db`. No step writes to trading, execution, risk or
session tables.

---

## M. Dataset identity

`dataset_identity` hashes the observations, the frozen feature, the
anchor-v2 labels and each instrument's price horizon. Same data, same
identity; one changed label, a different one (tested). Recorded in the
ledger at OPENING and CONSUMED.

---

## N–P. Hypothesis freeze and the single-use guard

### The lock was in the wrong place

25.9B locked the test with a row in `experiments`. But the procedure
runs on a **disposable working copy**. Take a fresh snapshot and that
lock is gone.

**Fix:** `research/protected_tests/ledger.jsonl`, git-tracked and
append-only, each entry hash-chained to the previous one. Editing or
deleting a line breaks the chain and the ledger refuses.

| Rule | Enforced by |
|---|---|
| registered before any d20 data exists | ledger entry, 2026-09-14 |
| spec cannot change silently | fingerprint `2d4d4bb1534b3e238a26ee51` must match |
| changed hypothesis needs a new identity | re-registration refused |
| **OPENING written before the statistic** | a crash still consumes the test |
| a fresh database cannot reopen it | tested |

### Access boundary

| Reader | Can it see the pending result? |
|---|---|
| dashboard | no — filters `experiments` on method version `v1`; D20 uses `anchor-v2` |
| autoresearch hypotheses | no — filters on a different evaluator |
| any workflow or scheduler | no — none invokes the tooling |
| validator, readiness checker | the only two callers |

Before execution the pending record holds only readiness counts.

---

## Q–S. Readiness checker and current state

`scripts/check_d20_readiness.py` — prints states, counts, dates, hashes
and ledger status. Asserted by test never to expose a correlation,
spread, p-value or verdict.

**Current state, 2026-09-14:**

| | |
|---|---|
| protected observations | 220 |
| `NOT_YET_OBSERVABLE` | 179 |
| `STALE_CACHE` | 28 |
| data-quality exclusions | **11** |
| structural exclusions | 2 |
| resolvable | 0 |
| ledger | NOT_CONSUMED |
| **verdict** | **NOT READY** |

### Earliest test date

| | |
|---|---|
| latest required d20 session, projected | 2026-09-24 |
| earliest theoretical date (lower bound) | **2026-09-25** |
| Labor Day inside the projected span | 2026-09-07 |
| realistic earliest | **2026-09-28** |

The calendar has no holiday table by design, so projection ignores
holidays and yields a lower bound. READY never rests on it alone.

---

## V. Failure injection

Every case returns NOT READY with a named reason (all tested):
missing price, missing benchmark, stale cache, labels not built, wrong
method version, malformed anchor, missing feature, vintage break,
insufficient coverage, consumed test, unregistered test, changed spec
fingerprint, tampered ledger, **date passed but data absent**. Duplicate
candles are impossible by primary key.

---

## W. Database

One table added: `price_cache_vintage_checks`. No other schema change.

---

## X. Tests

| Suite | Tests |
|---|---|
| `tests/research/test_protected_ledger.py` | 13 |
| `tests/impact/test_label_readiness.py` | 25 |
| `tests/scripts/test_cache_refresh_horizon.py` | 10 |

Full suite **4,070 OK**, 1 skipped. Every ledger test uses a temporary
file, and asserts the real ledger is byte-identical afterwards.

---

## Y. Safety

No protected statistic computed. No IBKR, paper, shadow or live order.
Trading, execution, risk, signal, modeling and governance code verified
unchanged since `04ba986`. `ImpactEngine` and `anchoring.py` unchanged.
Live safety 16/16.

---

## Z. Remaining risks

1. **The exclusion cap is exactly met.** 11 of 220 exclusions against a
   5% cap of 11.0. One more broken instrument blocks readiness. The
   causes are real ingestion faults: **BK**'s series stops on 20 May
   despite requests into September, **AVB** stops 14 Aug, **EA** is
   missing, **PARA** has no 60-day return. Investigate the mappings
   before the refresh.
2. **Ledger discipline.** The lock only protects if the ledger is
   committed and pushed right after the run.
3. **Minute-cache ranges** use the same coverage rule; not needed for
   d20, and not refetched by this fix.
4. **The null's overlap** (disclosed in 25.9B) stays frozen.

---

## AA. Exact next step

1. **Now:** investigate the BK, AVB, EA and PARA mappings.
2. **Phase 25.9D** can proceed; it does not touch the protected test.
3. **On or after 2026-09-28:** follow the runbook exactly, and stop at
   step 7 unless it says READY.

---

```
PHASE 25.9C STATUS:              COMPLETE
ANCHOR-V2:                       PASS
METHOD VERSIONING:               PASS
PRICE CACHE READINESS:           PARTIAL
BENCHMARK READINESS:             PASS
LABEL REBUILD:                   READY
LABEL DETERMINISM:               PASS
LABEL IDEMPOTENCY:               PASS
PROTECTED HYPOTHESIS:            FROZEN
PROTECTED RESULT:                UNSEEN
PROTECTED TEST:                  NOT EXECUTED
PROTECTED TEST CONSUMPTION:      UNCONSUMED
PROTECTED READINESS CHECKER:     OPERATIONAL
EARLIEST THEORETICAL TEST DATE:  2026-09-25 (lower bound; realistic 2026-09-28)
CURRENT PROTECTED WINDOW:        NOT READY
WORKING-COPY RUNBOOK:            READY
LEAKAGE:                         PASS
MODEL GOVERNANCE:                UNCHANGED
CONFIDENCE POLICY:               UNCHANGED
PAPER TRADING:                   NOT ATTEMPTED
REAL IBKR ORDER:                 NOT ATTEMPTED
PHASE 25.9D:                     READY
NEXT REQUIRED STEP:              PHASE 25.9D — RESEARCH / EXPERIMENT INFRASTRUCTURE HARDENING
```
