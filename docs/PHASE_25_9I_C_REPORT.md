# Phase 25.9I-C — Completion Report

## 1. Phase status

| | |
|---|---|
| **IMPLEMENTATION STATUS** | **COMPLETE** |
| **OPERATIONAL IBKR VALIDATION STATUS** | **PENDING** |

These are deliberately not collapsed. The code that prices capped, per-exchange
billing, governs COLD/WARM/HOT attention and verifies realtime entitlement is
built and tested. **Nothing about this account's actual billing or entitlement
has been observed.** No cap has ever been reached on it, and no realtime marker
has ever been seen on it. The validation harness exists, has been exercised in
plan mode, and has never sent a billable request.

---

## 2. Git state

| | |
|---|---|
| Branch | `ibkr-paper-validation-fixes` |
| Baseline HEAD | `1968a2c` (Phase 25.9I-E completion report) |
| Final HEAD | `dc37c58` |
| Working tree at start | clean |
| Commits | 1 |
| Diff | **19 files changed, 6702 insertions(+), 3 deletions(−)** |

**Modified (3 files, +32 / −3):** documentation and a test list only — no
behaviour change.

| File | Change |
|---|---|
| `docs/PHASE_25_9I_A_ARCHITECTURE.md` | superseded banner over the uncapped USD 2,500 table |
| `src/adaptive/cost_governor.py` | superseded banner in the docstring; **code untouched** |
| `tests/adaptive/test_freshness_and_legacy.py` | persistence guard list extended, with the reason |

**New (14 files):**

| File | Purpose |
|---|---|
| `docs/evidence/IBKR_MARKET_DATA_BILLING_561227.md` | sanitized provider evidence |
| `docs/PHASE_25_9I_C_ARCHITECTURE.md` | architecture |
| `docs/PHASE_25_9I_C_REPORT.md` | this report |
| `src/domain/market_data_billing_models.py` | capped billing contracts |
| `src/domain/data_attention_models.py` | COLD/WARM/HOT and priority contracts |
| `src/adaptive/billing.py` | exchange-aware governor, forecast, exposure, comparison |
| `src/adaptive/exchange_map.py` | instrument → billing venue |
| `src/adaptive/request_ledger.py` | local intraday meter, dedup, rate limits |
| `src/adaptive/attention.py` | the attention allocator |
| `src/adaptive/entitlement.py` | marker verification, subscription reconciliation |
| `src/data_access/adaptive_market_data_schema.py` | four additive tables |
| `scripts/adaptive_market_data.py` | read-only diagnostic |
| `scripts/validate_market_data_billing.py` | controlled validation harness |
| `tests/adaptive/test_market_data_billing.py` + `test_data_attention.py` + `test_market_data_boundary.py` | 152 tests |

---

## 3. Baseline

Recorded **before** any change, at `1968a2c`:

```
PYTHONPATH=src python -m unittest discover -s tests -t . -b
Ran 4664 tests in 1065.438s
OK (skipped=1)
```

| tests | pass | fail | errors | skipped | runtime |
|---|---|---|---|---|---|
| 4664 | 4663 | 0 | 0 | 1 | 1065.4 s |

*(A first attempt used `pytest`, which is not installed in this environment; the
project's convention is `unittest discover`, and that is what was recorded.)*

---

## 4. IBKR support evidence

Encoded in `docs/evidence/IBKR_MARKET_DATA_BILLING_561227.md` and
`src/adaptive/billing.py::ibkr_snapshot_policy`. Sanitized: no account id,
username, email, credential or token.

| Fact | Value |
|---|---|
| Per snapshot request | USD 0.01 |
| Threshold | 150 requests / exchange / calendar month |
| Cap | USD 1.50 / exchange / calendar month |
| After the cap | realtime streaming stated free for the rest of the month |
| Counter | updated once per business day at end of day |
| Bundle | described as *replacing* snapshot delivery — an alternative |
| Statement label | "GLOBAL SNAPSHOT" |

### Superseded

> *"~USD 2,500/month at ~250,000 requests"* (Phase 25.9I-A)

That modelled an **uncapped** per-request charge. A per-exchange monthly ceiling
now exists, bounding the variable term at **≈ USD 4.50/month** for this
universe. The original text is retained under a superseded banner in both
locations, because it records why the question was open.

### NOT superseded

That this is a **provider statement**, not a statement of account. Recorded as
`PricingConfidence.PROVIDER_STATED`; `ACCOUNT_VERIFIED` is unreachable from a
support reply by construction.

---

## 5. Repository findings

| Area | Finding | Consequence |
|---|---|---|
| Snapshot | `ClientPortalTransport.market_snapshot(conids, fields)` → `GET /iserver/marketdata/snapshot`, **batched** | reused; not duplicated |
| Streaming | **none.** No WebSocket client, no subscribe/unsubscribe, no persistent runtime | **not built** — see §9/§10 |
| Markers | field **6509**; `src/marketdata/quotes.py::_availability_of` maps R/D/Z/Y | mirrored in entitlement terms, canonical mapping untouched |
| Cadence | 1 batched HTTP call per 60 s for the whole universe (`MarketDataService.capacity` → `requests_per_cycle=1`) | created the counting-basis ambiguity |
| Rate limiting | transport sliding budget, `max_requests_per_minute` default 50, refuses rather than sleeps | adaptive ceiling set **below** it at 40 so this layer is refused first |
| Freshness | `DataFreshness`, `MarketDataAvailability`, `ExecutionFreshnessPolicy` (25.9I-A) | reused; `InstrumentDataStatus` adds the four-way separation |
| Cost governor | 25.9I-A `MarketDataCostGovernor` — fixed + per-request + flat allowance | left intact; capped billing added alongside |
| Quote cache | canonical `market_data_state` already stores quote, timestamp, source, availability, freshness, session | **reused, not duplicated**; freshness is evaluated at consumption |

**Reused:** transport, `MarketDataService`, quote acquisition, 6509 mapping,
calendar, `capture_only()` guard, `BudgetPolicy`, `MarketDataState`,
`MarketDataStateTransition`, the activation/deactivation reason enums, the
25.9I-E recommendation contract.
**Not duplicated:** no second transport, no second quote cache, no second
freshness classifier, no second calendar.

---

## 6. Exchange coverage — recomputed, not copied

Source: `broker_instrument_mapping.broker_payload_json.primary_exchange`.
**Not** `exchange` (reads `SMART` for all 31 — a router, not a billing venue),
and **not** the research `exchanges` table (`US_AND_INTL`, *"US & International
(unspecified)"* — a research bucket).

**Capture universe (31):**

| Exchange | Count |
|---|---|
| NYSE | 17 |
| NASDAQ | 13 |
| ARCA | 1 (SPY benchmark) |
| unknown | **0** |

The prior manual claim was NASDAQ 13 / NYSE 17 / ARCA 1. **The repository
agrees exactly.** Verified twice — once through `ExchangeMap`, once by a direct
independent recount of the mapping table.

**Parent universe (130):**

| Exchange | Count |
|---|---|
| NYSE | 11 |
| NASDAQ | 10 |
| **UNKNOWN** | **109** |

Only the 21 members capture has already resolved carry a contract. A dynamic
session universe drawn from the parent can therefore contain names whose venue —
and price — is unknown; those requests are **refused**, never estimated.

**SPY is not free.** ARCA is priced like any other venue, asserted by test.

---

## 7. What was implemented

- **Exchange-aware capped cost model** — `ExchangeBillingPolicy` (price,
  threshold, ceiling, expected entitlement), `ProviderBillingPolicy` (evidence
  source, date, confidence, counting basis, billing timezone). Provider-agnostic;
  nothing in the domain layer names IBKR.
- **Monthly caps and periods** — `BillingPeriod` (`YYYY-MM`), cost applied to
  the *cost* not the count, per-venue counters, history preserved.
- **Local request ledger** — append-only, content-derived ids, idempotent
  flush, replay-from-rows, per-run cost tracking, portal reconciliation.
- **Budget governor** — ALLOW / WARN / THROTTLE / BLOCK against USD 20,
  incremental cost across the cap boundary, forecasting, exposure projection,
  read-only plan comparison.
- **COLD/WARM/HOT allocator** — dedup, priority classes, HOT ceiling,
  TTL expiry, deactivation with reasons, budget-refusal downgrades, and the
  safety-conflict escalation.
- **Entitlement verification** — 6509 markers → per-exchange entitlement, with
  policy expectation and account observation kept apart.
- **Subscription reconciliation** — restart invalidation, staleness,
  `hot_desired_but_not_execution_grade`.
- **Persistence** — four additive tables.
- **CLI** — `scripts/adaptive_market_data.py` (status, coverage, forecast,
  exposure, `--scenarios`), read-only, 0 billable requests.
- **Validation harness** — `scripts/validate_market_data_billing.py`, plan by
  default, three mandatory ceilings, pre-flight, bounded run, reconcile.
- **Tests** — 152 new.

---

## 8. What worked

- Cap arithmetic exact at every boundary: 0 → 0.00, 149 → 1.49, 150 → 1.50,
  151 → 1.50, 10,000 → 1.50.
- Incremental cost correct **across** the threshold: 149 + 10 requests = USD 0.01.
- Per-exchange isolation: NYSE at 200 requests caps at 1.50 while NASDAQ at 5
  still reads 0.05.
- Exchange resolution reproduced the audited distribution exactly, twice, by two
  independent paths.
- Exposure calculator independently reproduced USD 3.30 / 9.30 / 120.90 and the
  cap timings (NYSE 8.82 min, NASDAQ 11.54 min, ARCA 150 min) from canonical
  data — figures that had previously only been computed by hand.
- Deduplication: 4 requests → 1 decision → **1** charge, all 4 requesters and
  all 4 reasons preserved, order-independent.
- Session universe 31 → **HOT 1**, unused capacity 2.
- CAP_REACHED + delayed marker → `execution_grade=False`, `expectation_unmet=True`.
- Validation harness refused to run: gateway down → pre-flight NOT READY,
  nothing sent, exit 0 in plan mode.
- Harness refuses `--execute` without all three ceilings (argparse error).

---

## 9. What did not work

**Mandatory section. Nothing here is hidden because it was later fixed.**

### 9.1 A bash heredoc destroyed a 500-line module before it was written
**Symptom:** writing `market_data_billing_models.py` via `cat <<'PYEOF'` failed
with `unexpected EOF while looking for matching '`.
**Root cause:** the same Git-Bash-on-Windows quoting limitation this project hit
in Phase 25.9I-D.
**Fix:** verified no partial file was left (`git status` clean), then used the
editor tool for every Python file thereafter.
**Status:** resolved. No file was left half-written.

### 9.2 Baseline was first run with a test runner that does not exist here
**Symptom:** `python -m pytest tests/` → *"No module named pytest"*, exit 0,
which could have been mistaken for a pass.
**Root cause:** assumed pytest; this project uses `unittest discover`.
**Fix:** re-ran with the project's own convention; 4664 tests recorded.
**Status:** resolved. **Worth noting**: the failed command exited **0**. A
baseline taken from that exit code alone would have been fiction.

### 9.3 `DataFreshness.UNKNOWN` does not exist
**Symptom:** `AttributeError` on import of `data_attention_models`.
**Root cause:** assumed a member by analogy with `MarketDataAvailability.UNKNOWN`;
`DataFreshness` is `fresh/aging/stale/invalid/unavailable`.
**Fix:** defaulted to `UNAVAILABLE`, and made `execution_freshness_ready` refuse
`STALE` and `UNAVAILABLE` as well as `INVALID`.
**Status:** resolved — and the fix made the freshness gate stricter than the
original draft.

### 9.4 A test asserted the wrong budget verdict
**Symptom:** `test_the_conservative_fallback_is_the_dearest_and_uncapped`
expected WARN/THROTTLE, got ALLOW.
**Root cause:** **the test was wrong, not the code.** USD 10 of a USD 20 budget
is 50%, below the 70% warn threshold.
**Fix:** rewrote the assertion to test what actually matters — that the
stand-in is ~7× dearer than a capped venue and runs out of budget where a capped
venue never would.
**Status:** resolved.

### 9.5 A thesis fixture violated the 25.9I-E contract
**Symptom:** `ValueError: EXTEND_TARGET must carry the revised target zone`.
**Root cause:** the fixture omitted `revised_target_zone`.
**Fix:** supplied one. **The contract was not relaxed** — 25.9I-E is right to
demand it.
**Status:** resolved.

### 9.6 The provider's answer left the counting basis undefined — UNRESOLVED
**Symptom:** "USD 0.01 per request" does not say whether one batched HTTP call
carrying 31 conids is 1 request or 31.
**Impact:** **31× on the downside** — USD 3.90 vs USD 120.90 for a full session
if the cap fails.
**Fix:** modelled as `RequestCountingBasis.UNKNOWN`; every ceiling enforced on
the conservative reading; both numbers reported everywhere.
**Status:** **OPEN.** Only the October test and next-day reconciliation can
close it. It is a limitation of the evidence, not of the code.

### 9.7 109 of 130 parent-universe members have no billing venue — UNRESOLVED
**Symptom:** only instruments capture has already resolved carry a contract.
**Impact:** a dynamic session universe could select names that cannot be priced.
**Fix:** they are refused, not estimated. An explicit
`conservative_unknown_pricing` fallback exists for an operator who must proceed,
and it prices at the dearest known rate **with no cap**.
**Status:** **OPEN by design.** Resolving it means resolving 109 contracts,
which costs broker requests and belongs to whichever phase actually needs them.

### 9.8 No streaming transport exists — NOT BUILT, DELIBERATELY
**Symptom:** §16/§42 assume a streaming path may exist. It does not.
**Root cause:** the project has no persistent runtime; documented as a decision
in `src/marketdata/service.py`.
**Decision:** **not built.** A second market-data stack with a daemon and a
reconnect state machine is the opposite of "the minimum safe abstraction".
`ChannelAvailability` records the audit and lets HOT say streaming was wanted
and is unavailable.
**Status:** deferred, with the seam in place.

---

## 10. What was deferred

- **Real billing reconciliation** — impossible before the October test; the
  `--reconcile` path and the columns exist and are empty.
- **Subscription purchase** — human governance action. No code path exists.
- **Streaming transport** — §9.8.
- **Streaming-after-cap preference (§42)** — requires policy *and* observation
  to agree; neither half is verified.
- **Production dynamic capture switching** — capture is unchanged.
- **Paper and live execution** — out of scope and structurally blocked.
- **Resolving the 109 unmapped parent members** — §9.7.
- **Billing timezone** — not stated by the provider; UTC is a declared,
  overridable assumption.

---

## 11. Cost model evidence

One exchange (NASDAQ), from `IBKR_SNAPSHOT_POLICY_561227`:

| requests | cost (USD) | cap state |
|---|---|---|
| 0 | 0.00 | below_threshold |
| 1 | 0.01 | below_threshold |
| 149 | 1.49 | below_threshold |
| **150** | **1.50** | **cap_reached** |
| 151 | 1.50 | cap_reached |
| 10,000 | 1.50 | cap_reached |

Two independent exchanges, same period:

```
NYSE   200 requests  ->  1.50  cap_reached
NASDAQ   5 requests  ->  0.05  below_threshold
total 1.55 USD
```

Capping NYSE does not cap NASDAQ.

---

## 12. Monthly budget

From the actual capture universe (3 priced venues):

```
variable ceiling   3 x 1.50 = USD 4.50
fixed subscription none configured (UNKNOWN, not zero)
worst case         USD 4.50
budget             USD 20.00      ->  FITS
```

The subscription price is **not** invented: ticket #561227 quoted none, so
`fixed_monthly_subscription_cost_usd` is `None` and the plan comparison reports
it UNKNOWN. With a hypothetical USD 14.50 supplied by a caller, the forecast
reads USD 19.00 and still fits — but that figure is the caller's, not evidence.

---

## 13. COLD / WARM / HOT examples

`python scripts/adaptive_market_data.py --scenarios` (0 requests):

```
us_and_intl-luv   hot    position_exit     risk, thesis
    why: invalidation breached; exit recommended
    why: periodic position verification
us_and_intl-ual   warm   position_monitor  thesis
    why: price approaching the target zone
benchmark-spy     cold   broad_screening   screener
    why: in the session universe; no decision near

EXECUTION FRESHNESS, same instrument, two worlds
  HOT + delayed   ready=False  entitlement is delayed, not realtime_verified
  HOT + realtime  ready=True   (FIXTURE: never observed on this account)
```

The HOT case shows deduplication in the output: two requesters, two reasons, one
activation, one charge.

---

## 14. Session universe vs HOT set

```
session universe 31  ->  HOT 1   (unused HOT capacity 2 of 3)
```

Also asserted by test: a session of 5 produces 0 HOT; a parent universe of 130
produces 0 HOT; and an exchange **already past its cap** — where another request
costs nothing — still produces 0 HOT with capacity left unused.

---

## 15. Entitlement evidence

| Question | Answer |
|---|---|
| Provider policy says realtime is expected after the cap | **YES** (ticket #561227) |
| Actual account realtime verified | **NO — PENDING** |
| Actual marker observed | **NOT TESTED.** No billable request has been made by this phase |

Never merged. `CapState.CAP_REACHED` has no code path to
`EntitlementStatus.REALTIME_VERIFIED`. A cap reached with a delayed marker is
recorded as `expectation_unmet` — a finding to investigate, not a verdict either
way.

---

## 16. Controlled real-account test plan

**Not run. Not scheduled. Requires a human to invoke it.**

| | |
|---|---|
| Session | first US trading day on/after 2026-10-01, per `USEquityCalendar` |
| Instruments | the 31-member capture universe (NYSE 17, NASDAQ 13, ARCA 1) |
| Cadence | 60 s, one batched call — the existing capture cadence |
| Duration | ~30 min (NYSE caps at 8.8 min, NASDAQ 11.5 min; ARCA needs 150 min and will **not** cap) |
| Expected cost | **USD 3.30 if the cap holds** |
| Exposure if it does not | **USD 9.30** (conservative basis) |
| Ceilings | `--max-requests`, `--max-estimated-cost-usd`, `--timeout-seconds` — all mandatory |
| Stop | whichever ceiling arrives first; stopping is the success condition |

```bash
python scripts/validate_market_data_billing.py --plan
```
```bash
python scripts/validate_market_data_billing.py --execute --max-requests 1000 --max-estimated-cost-usd 4.00 --timeout-seconds 1800
```
```bash
python scripts/validate_market_data_billing.py --reconcile --exchange NYSE --portal-count N --portal-cost-usd X
```

**Next business day:** read the portal counter, reconcile per exchange, compare
against the local ledger. A disagreement identifies the counting basis (§9.6).

---

## 17. Request ledger

Four tables (§7). Per-exchange counters keyed `(provider, billing_period,
exchange)`; months are separate rows, so October starts at zero without
September being cleared. Request ids are content-derived
(`provider|exchange|instrument|timestamp|type|purpose`), written with
`INSERT OR IGNORE`, so a re-flush writes **0** new rows — asserted.
Deduplication merges multi-requester demand before pricing. Reconciliation
writes the portal figure **beside** the local estimate; `NULL` means *not yet
compared*, never *agreed*, and a disagreement is preserved rather than
overwritten — asserted by test.

---

## 18. Rate and request safety

| Guard | Value |
|---|---|
| Adaptive request ceiling | 40/min (below the transport's 50) |
| Minimum refresh per instrument | 60 s |
| Retry budget | 3, then refuse |
| Batching | the transport already batches; not re-implemented |
| Deduplication | one activation per instrument per cycle |
| Storm protection | refuses rather than sleeping or trimming |
| Validation ceilings | requests, cost and time, all mandatory |

---

## 19. Feature flags

Existing 25.9I-A flags reused; **no new flag was added**.

| Flag | Default |
|---|---|
| `adaptive_market_data_enabled` | **False** |
| `market_data_cost_governor_enabled` | **False** |
| `adaptive_execution_freshness_gate_enabled` | **False** |

With every flag forced on, `enables_broker_writes` and `enables_live_trading`
remain False, because nothing in this layer imports a broker path.

---

## 20. Production behaviour

| Path | Changed? |
|---|---|
| capture | **No** |
| Portfolio | **No** |
| Risk | **No** |
| OrderIntent | **No** |
| Execution | **No** |
| IBKR order behaviour | **No** |
| Market-data subscriptions | **No** |

Verified structurally: **no module under `src/` outside `src/adaptive/` and
`src/domain/` imports any Phase 25.9I-C module.** There is no production wiring
at all. The three modified files change documentation, a docstring and a test
list.

---

## 21. Broker safety

| | |
|---|---|
| Broker writes attempted | **0** |
| Broker writes succeeded | **0** |
| Paper orders caused | **0** |
| Live orders | **0** |
| Billable market-data requests made | **0** |
| Subscriptions purchased | **0** |

The only broker contact in the whole phase was the validation harness's
`--plan` pre-flight, which reached a **closed gateway**, reported NOT READY and
sent nothing. Asserted by AST test: no adaptive module calls `market_snapshot`,
`place_order`, `cancel_order`, `subscribe` or `init_brokerage_session`.

---

## 22. Test results

| Suite | Tests | Result | Runtime |
|---|---|---|---|
| Targeted I-C: `test_market_data_billing` | 64 | **OK** | 0.04 s |
| Targeted I-C: `test_data_attention` | 51 | **OK** | 0.02 s |
| Targeted I-C: `test_market_data_boundary` | 37 | **OK** | 13.6 s |
| **I-C total** | **152** | **OK** | — |
| Adaptive suite (`tests/adaptive`) | 472 | **OK** | 29.8 s |
| I-B dynamic universe | 45 | **OK** | 3.13 s |
| I-D capital allocation | 55 | **OK** | 0.06 s |
| I-E position thesis | 48 | **OK** | 0.03 s |
| Market data (`tests/marketdata`) | 57 | **OK** | 0.08 s |
| Capture / session (`tests/capture`) | 126 | **OK** | 528.2 s |
| Portfolio (`tests/portfolio`) | 273 | **OK** | 1.47 s |
| Risk engine | 39 | **OK** | 0.02 s |
| Research integrity (25.9D) | 39 | **OK** | 23.0 s |
| Trading readiness (25.9E) | 44 | **OK** | 5.05 s |
| Live safety (Phase 16) | 57 | **OK** | 0.04 s |
| **FULL SUITE** | **4816** | **OK (skipped=1)** | **962.6 s** |

```
PYTHONPATH=src python -m unittest discover -s tests -t . -b
Ran 4816 tests in 962.612s
OK (skipped=1)
```

**4816 − 4664 = 152**, exactly the new I-C tests. **Zero failures, zero errors,
zero regressions.** The one skip is the same pre-existing skip as the baseline.

---

## 23. Performance

| Operation | Time |
|---|---|
| Exchange map build (31 contracts) | 4.69 ms |
| Cost decision, 1 instrument | 0.025 ms |
| Cost decision, 31 instruments | 0.047 ms |
| Exchange budget calculation (3 venues) | 0.003 ms |
| Monthly forecast | 0.008 ms |
| Exposure projection (390 min) | 0.030 ms |
| Attention plan, 31 instruments | 0.69 ms |
| Attention plan, 130 instruments | 3.08 ms |
| Ledger lookup (1,550 entries) | 0.033 ms |
| 1,000 repeated state records | 2.2 µs total |

---

## 24. D20 status

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
`protected_ledger` or `validate_d20` — asserted by test over every new module
and both new scripts. No threshold in this phase was tuned against any
protected outcome; every policy value is either from ticket #561227 or `None`.

---

## 25. Scope-creep review

| Check | Result |
|---|---|
| Unrelated files touched | none |
| Portfolio / Risk changes | none |
| Execution changes | none |
| Capture changes | none |
| IBKR order behaviour | unchanged |
| Protected research | untouched |
| Automatic subscription behaviour | none — no code path exists |
| New external dependencies | **none** (stdlib only: `calendar`, `hashlib`, `json`, `sqlite3`, `collections`) |

Three files modified, all documentation or test-list; 14 added.

---

## 26. Known limitations

1. **The counting basis is unknown** (§9.6) — 31× uncertainty on the downside.
2. **Cap behaviour is unverified on this account.** Policy only.
3. **Realtime has never been observed on this account.** Zero exchanges are
   `REALTIME_VERIFIED`.
4. **The portal counter is not available intraday**, so the local ledger cannot
   be checked against anything until the next business day.
5. **The billing timezone is unknown**; UTC is a declared assumption.
6. **109 of 130 parent-universe members have no billing venue** (§9.7).
7. **No streaming transport exists** (§9.8); a HOT instrument is served by
   snapshot polling and says so.
8. **No `max_hot_symbols` production value exists** — deliberately, there is no
   evidence for one.
9. **The subscription price is UNKNOWN**, so the A/B comparison has one side
   missing until someone supplies a verified figure.
10. **Nothing is wired into production.** Everything here is shadow.

---

## 27. Readiness

| | |
|---|---|
| **25.9I-C IMPLEMENTATION** | **COMPLETE** |
| **REAL IBKR MARKET-DATA VALIDATION** | **READY** (harness built, plan-mode exercised, never executed) |
| **READINESS FOR 25.9I-F** | **YES** — the activation-request interface exists and is tested |
| **READINESS FOR 25.9I-G** | **PARTIAL** — lineage is carried on every decision, but there are no outcomes yet to attribute |
| **READINESS FOR 25.9I-H** | **NO** — realtime entitlement has never been verified on this account, and 25.9I-H's first gate is execution-grade freshness |

---

## 28. Final verdict

> **PHASE 25.9I-C IMPLEMENTATION = COMPLETE**
>
> 14 new files, 3 documentation-only modifications, 152 new tests, and a full
> suite of **4816 passing** against a **4664-test** baseline — no regressions.
> Capped per-exchange billing, calendar
> periods, a local intraday ledger, budget states, COLD/WARM/HOT attention with
> deduplication and priority classes, entitlement verification, and a controlled
> validation harness with three mandatory ceilings. Exchange coverage recomputed
> from canonical contract metadata and confirmed twice.

> **REAL IBKR MARKET-DATA VALIDATION = PENDING**
>
> Zero billable requests made. Zero caps reached. Zero realtime markers
> observed. The cap, the counting basis and the post-cap entitlement are all
> **provider statements** that this account has not been observed to obey. The
> October test exists to change that, and it has not been run.

---

*No broker order was created, modified or cancelled. No subscription was
purchased. No capture behaviour changed. The protected D20 experiment was not
touched.*
