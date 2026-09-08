# Phase 25.5 Audit Report

**Full paper-trading audit, IBKR validation attempt, and pre-shadow gate.**

Date 2026-09-08 · Python 3.12.10 · Windows 11 · SQLite ·
Baseline **3,825 tests** → after **3,851 tests**, both OK, 1 skipped ·
Live-safety audit **16/16** · Loop integrity **12 checks → 14** ·
Adversarial probes **16/16**

---

## A. Executive verdict

**READY FOR PHASE 26 WITH EXPLICIT CONDITIONS.**

Ten defects were found, of which **five were HIGH** and all five were
in code Phase 25 shipped and its own tests certified. Every HIGH and
every MEDIUM has been fixed and covered by a regression test. No
CRITICAL finding survived triage.

The three that matter most were invisible for the same reason: **the
suite only ever exercised the happy path.** Every test filled its order
completely, so the partial-fill path — which raises `TypeError` on its
first line — had never once executed. Every test used a fixture signal
with no model attached, so the fact that no order ever recorded its
model was unobservable. And no test ever made a component fail, so
nobody noticed that `result.block()` recorded a reason and **stopped
nothing**.

Per §36, because the real IBKR paper environment could not be reached:

> **READY FOR CODE-LEVEL SHADOW PREPARATION, BUT REAL IBKR PAPER
> VALIDATION REMAINS OUTSTANDING.**

---

## B. Baseline

| | Before | After |
|---|---|---|
| tests | 3,825 | 3,851 |
| passed | 3,824 | 3,850 |
| failed | 0 | 0 |
| skipped | 1 | 1 |
| xfailed | 0 | 0 |
| runtime | 156 s | 423 s |

Environment: Python 3.12.10, Windows-11-10.0.26200, SQLite via stdlib,
`PYTHONPATH=src python -m unittest discover -s tests -t . -b`.
Database: in-memory fixtures for tests; a 239,157,248-byte copy of the
`db-latest` release asset (updated 2026-09-08T17:13:12Z) for the
production reads. Migrations are `CREATE TABLE IF NOT EXISTS` with
additive column migration; no migration tool exists by design.

No test was modified before the baseline was captured. Two tests were
modified afterwards, both because the *stricter* behaviour made the old
expectation wrong; both changes are named in section S.

---

## C. Architecture findings

Actual callers were traced, not assumed.

| Component | Callers |
|---|---|
| `orchestrator.execute` | `ExecutionService.submit` — **only** |
| `ExecutionService.submit` | `trading/loop.py`, `run_execution.py`, `run_ibkr.py` |
| `gateway.submit_order` | `orchestrator._submit` — **only** |
| `intake.from_decision` | `trading/loop.py`, and (now) both CLIs |
| `gateway.heartbeat` | **nobody** → finding A-10 |

**Three order lifecycles exist and only one can reach a broker:**
Phase 12's backtester (historical bars), Phase 13's `PaperExecutor`
(cached bars), and Phase 25's loop (IBKR paper). The first two are
simulations and neither has a venue. TD-04 is re-scoped rather than
closed and did not grow — the loop drives the Phase 14 orchestrator
rather than extending `PaperExecutor`, which would have been exactly
the "parallel paper-only shortcut" §2 forbids.

No duplicate IBKR adapter, no second risk engine, no second reconciler.
`disabled_gateway.py` and `paper_gateway.py` are alternative
`BrokerGateway` implementations, not duplicates of the IBKR one.

---

## D. Database findings

Production holds **52 tables, 47 non-empty**. The Phase 25 tables do
not exist there because the loop has never run on it — reported as
absence, not as zeros.

Lineage is followable in actual records:
`signals → risk_decisions → order_intents → execution_orders →
execution_fills → position_actuals → trade_outcomes`, joined by
`signal_id`, `decision_id`, `intent_id`, `order_id`. Verified by
walking one real trade in the fixture database, link by link.

`trading_mode` is a single row enforced by `CHECK (singleton = 1)`;
`mode` is `NOT NULL` — a probe that tried to write NULL was refused by
the schema itself, not by application code. `trading_mode_history`,
`paper_validation_reviews` and `trading_loop_audit` are append-only
(no UPDATE path exists).

Empty tables were **not** counted as implemented functionality. The
finding that `trade_outcomes.model_id` was always NULL came from
reading rows, not from reading code.

---

## E. Portfolio findings

`target − actual − pending = outstanding` verified with correct signs
across every case in §10:

| Case | Result |
|---|---|
| target +100, broker +40 | outstanding +60, action `increase` |
| target met | `noop`, side None |
| open / close / reduce / reverse | named correctly |
| no target ≠ zero target | `outstanding` None vs 0.0 |
| pending subtracted | re-proposal suppressed |
| broker position, no local order | seen and sized against |
| fractional 10.4, 1-share increment | floored to 10, never rounded up |
| gap below venue minimum | `below_minimum`, no order |
| unreconciled position | refused (`position_not_reconciled`) |

`pending` is the larger magnitude of the broker's open orders and our
own working orders. A probe in which the venue returned an empty
open-order book after a reconnect produced **no duplicate order**.

---

## F. Risk findings

Every execution entry point reaches the same boundary. The one that
did not is finding **A-03**.

Verified backend-enforced (not UI): mode, kill switch, stale market
data, stale signal, unpromoted model, unreadable account, disconnected
gateway, ordering disabled, unresolved reconciliation, duplicate cycle,
configuration change, quantity increment, venue minimum, and the risk
verdict itself.

`intake.from_decision` raises `RiskNotApproved` before reading any
other argument and has no override parameter — asserted by a test that
inspects its signature.

---

## G. Execution findings

The lifecycle is walked, never jumped:
`validating → approved → submitting → submitted → acknowledged →
partially_filled → filled`, asserted from `order_state_history`.

Finding **A-01** is here: the partial-fill branch had never run.

---

## H. IBKR findings

**No real IBKR paper session was contacted. UNVERIFIED —
ENVIRONMENTAL BLOCKER.** Established by evidence, not assumption:

- `IBKR_*` environment variables: none set;
- TCP probe of 5000 and 5001 (Client Portal), 4001/4002 (IB Gateway),
  7496/7497 (TWS): nothing listening;
- HTTPS probe of `localhost:5000/v1/api/iserver/auth/status`: no
  response.

**Where the mock may create false confidence** (§9), named rather than
glossed:

1. **Duplicate client order id.** After a crash between submission and
   local persistence the loop's last defence is IBKR rejecting a
   repeated `cOID`. The mock does not reject one. *Untested against the
   venue.* (Mitigated: reconciliation blocks the next cycle — see I.)
2. **Position endpoint lag.** The mock updates `positions_book`
   synchronously on fill; IBKR's portfolio endpoint lags. A lagging
   endpoint would show a stale position for one cycle and reconciliation
   would flag it — the safe direction, still unverified.
3. **Session expiry.** The mock never expires. The Client Portal
   session lapses when idle — finding **A-10**.
4. **Order status vocabulary.** The mock emits `Submitted`/`Filled`;
   IBKR also emits `PreSubmitted`, `PendingSubmit`, `Inactive`. The
   adapter maps what it knows and returns UNKNOWN otherwise, which the
   validator treats as not tradeable.
5. **Confirmation prompts.** IBKR replies with a message id requiring
   confirmation for some orders. The adapter handles it
   (`_answer_confirmations`); the mock exercises it only when the flag
   is set.

---

## I. Reconciliation findings

Detects unknown broker orders, missing local orders, quantity, status,
fill, position and cash mismatches. **An unresolved discrepancy now
blocks new trading** — and until finding **A-08** it did not.

Crash-consistency probe, §17 boundary D (broker accepted, local write
lost):

```
cycle 1  submitted 1     execution_orders rows 0     venue holds 1
cycle 2  submitted 0     discrepancies 1             venue holds 1
         blocked: reconciliation_unresolved
```

No duplicate, an explicit UNKNOWN, and trading stopped for a human —
never "assume nothing happened".

`resolve_unknown_orders` queries and never resubmits.

---

## J. Idempotency and restart findings

`cycle_anchor → decision_id → intent_id → idempotency_key`, each a hash
of the one before. Verified:

- same anchor twice → second cycle ABANDONED, 1 order;
- fresh process, order live at venue → 1 order;
- reconnect, venue forgot the order → 1 order;
- crash between submit and persist → 1 order, then blocked;
- duplicate execution id → ignored (dedup on the venue's id);
- over-fill → recorded as a discrepancy, never applied.

---

## K. Signal freshness and pipeline cadence findings

**The actual timing chain, from the workflow files:**

```
Sun 02:00 UTC ─┐
Wed 02:00 UTC ─┴─ pipeline.yml, ONE job, 16 stages back to back:
                  entities → events → fusion → prices → studies →
                  observations → features → train → predict →
                  SIGNALS → outcomes → attribution → memory →
                  experiments → research → dashboard

13:00 / 16:30 / 21:15 UTC daily ─ daily.yml: news ingestion,
                  archiving, size reduction. NO signal generation.

(no schedule) ─ run_trading_loop.yml: manual dispatch only.
```

**Measured on the production record:** the median gap between a
signal's `source_information_cutoff` and its `created_at` is **39.0
hours** (n = 9 active signals; range 20.5–64.9 h).

Against the default 48-hour information-age policy the tradeable
window is therefore about **9 hours wide, twice a week** — roughly 18
of every 168 hours — and the loop is not scheduled inside it.

**Verdict: OPERATIONALLY INEFFICIENT, not broken.** The suppression is
correct: a signal built on four-day-old information *should* be
refused. The incompatibility is between the pipeline's cadence and the
policy, and **the threshold was not changed to produce trades** (§14,
§34). It is now an explicit, versioned field in the session
fingerprint rather than a constant nobody could see.

---

## L. P&L findings

Broker-reported and locally-calculated figures are kept apart, each
carrying a required `source`. `agrees_within()` returns **None** when
either side is missing — "could not compare" never reads as "agree".

Verified on a real fill: 500 @ 100.00, marked at 102.00 → local
unrealized 1,000.00, local realized 0.00, fees 1.00, sources "not
comparable" (broker side absent in the probe). Average cost is
recomputed from running notional, not averaged with the previous
average: two 250-share fills at 100.00 and 100.60 give **100.30**, not
100.30-by-luck — asserted to six places.

Local P&L is deliberately naive average-cost accounting over our own
fills. It exists to *disagree* with the broker, and a disagreement is
recorded as a reconciliation finding rather than averaged away.

---

## M. Outcome, attribution and memory findings

`classify_errors` sets three mechanical fields and Phase 25 adds
nothing to it. There is no code path in which a loss becomes an error.

Every detector was audited against §20. `_missing()` returns
`fired=False` **with** `confidence=INSUFFICIENT_EVIDENCE` and a
`missing` table name — it does not masquerade as a pass. The engine
handles the aggregate correctly too: when nothing could be judged it
returns UNKNOWN / INSUFFICIENT_EVIDENCE naming every absent input, and
when some layers were judged it says how many. **No detector turns
missing evidence into PASS.**

`detect_portfolio_error` remains blind, as Phase 25 reported. Audited
in detail:

- *expects*: `max_concentration` and `concentration_limit`;
- *Phase 25 produces*: exposure, no per-cycle concentration measure;
- *result*: `INSUFFICIENT_EVIDENCE`, summary "no portfolio record
  exists…", never NO_ERROR;
- *classified*: **UNMEASURED**, not fixed, not removed. Building a
  concentration measure is Phase 26 work; claiming one now would be
  the fake metric this audit exists to prevent.

Memory reads `outcome_measurements` and `error_attributions`, both
dated by `available_at = window_end`. Execution evidence enters
attribution from fills that necessarily precede the window's close, so
no future information reaches an experience. One real leak was found
and fixed — finding **A-09**.

---

## N. Challenger and paper-validation findings

Governance holds. `PAPER_CANDIDATE` is the only Phase 24 status from
which a challenger may enter paper; a `promising` challenger raises
`NotEligibleForPaper`. `LIVE_ELIGIBLE` is absent from every entry of
`AUTOMATIC_TRANSITIONS`, refused by `assert_transition`, refused again
by `review()`, and asserted absent by the integrity check.

A review requires a named reviewer and a reason, both without defaults;
reviews are append-only; a still-running validation cannot be reviewed.

Seven quality dimensions, **no total, no ordering** — `sorted([card,
card])` raises `TypeError`. A `DimensionReading` marked measured with
no value is refused at construction. `compare()` reports differences
and declares no winner. `is_conclusive()` requires 30 completed trades
**and** every dimension measured, and returns False on everything this
project can currently produce.

---

## O. Dashboard findings

`positions` is filtered on `origin = 'broker_reconciled'` **inside the
SQL**, so a reader cannot get intentions back from a positions query.
Target and actual are separate blocks with separate headings. The mode
is resolved through `TradingModeStore.resolve`, so a stored `"live"`
displays as OFF with its reason — the page cannot reimplement the rule
and drift from the one the loop obeys. Every eligibility code is
collected, including refusals. No fetch, no XHR, no WebSocket, no trade
button. On the production record the page reports absence.

---

## P. Security and live-safety findings

`audit_live_safety.py --untracked` → **16/16 PASS**, unchanged after
remediation.

Repository-wide scan found no credential literals: every hit is a
parameter named `api_key`/`password` with an `Optional[str] = None`
default read from the environment. The only IBKR account ids in the
tree are `DU0000000` (placeholder in `.env.example` and
`paper_config`) and `DU1234567` (mock) — both `DU`, IBKR's paper
prefix, which cannot be a live account.

`src/trading/mode.py` is the only file in the trading package that
reads the environment, asserted by a tokenised scan with a negative
control. The CLI declares no secret argument, checked by parsing its
`add_argument` calls.

Four independent live refusals, each tested separately: mode store
write, mode resolve, IBKR config construction, Phase 14 safety layer.

---

## Q. Scheduler and workflow findings

Only two workflows can invoke trading code:

| Workflow | Reaches IBKR? | Defaults |
|---|---|---|
| `run_paper_session.yml` | no — Phase 13 has no venue | `--dry-run` |
| `run_trading_loop.yml` | **no** — `--mock` is hard-coded in the command, not an input | `--dry-run`, `IBKR_ENVIRONMENT: paper` pinned |

No GitHub Action can reach a real gateway. If a future developer
removed `--mock`, the loop would attempt `localhost:5000`, fail to
connect and **block** — it fails closed even when the guard is
deleted.

---

## R. Data leakage and point-in-time findings

The loop anchors every read: `signals_as_of(anchor)`,
`prices_as_of(…, anchor)`, `MAX(timestamp) … WHERE timestamp <=
anchor`, `evaluate(as_of=anchor)`. Broker account and positions are
read at wall clock, which is correct — they are facts about the
present, not history.

Two leaks were found and fixed: **A-05** (a replay could decide on
month-old signals and send the orders to today's venue — point-in-time
protected the decision, nothing protected the execution) and **A-09**
(the attribution risk budget used the *latest* account equity for every
trade, including old ones).

Contamination (§32): `trade_outcomes` is read by attribution, the
dashboard, its own repository and the loop. **Experiments, autonomous
research, challengers and memory never read it.** Paper trading cannot
contaminate experiment evaluation, training data, model promotion or
benchmarks.

---

## S. Remediations performed

| # | Severity | Finding | Fix |
|---|---|---|---|
| **A-01** | HIGH | **Partial fills never applied.** `_record_unpaired` called `apply_fill_to_order(order, fill, self.machine, at=now)` — not that function's signature. TypeError inside a generator inside the broker-poll stage, so the only path that can apply a partial fill had **never once completed**. Every test filled completely and took the paired path. | Correct signature, plus the state move to `PARTIALLY_FILLED`/`FILLED` the function does not do. Over-fill recorded as a discrepancy, never applied. |
| **A-02** | HIGH | **Model lineage never populated.** `model_version`, `prediction_id`, `trained_model_id`, `strategy_id` were None on every order, lineage row and trade outcome. Phase 16 recorded `lineage_complete = 0` while Phase 25 recorded `complete = 1` — two lineage models disagreeing, and the certifying one did not look at the model. | Per-instrument provenance read from `ModelContribution`/`SignalProvenance`; `intake.from_decision` gained `model_versions`/`strategy_ids` maps; outcomes key the model on the **signal**, not the instrument (the outcome builder runs before the decision half). Two new integrity checks compare the two lineage models and require every filled trade to name its strategy. |
| **A-03** | HIGH | **`--assume-risk-approved` bypassed the risk gate** in both CLIs, setting `risk_approved=True` with no `RiskDecision`. Untested. Survived the Phase 17 fix written to remove exactly this. | Flag **deleted**. Replaced by `--decision-id`, which loads a real decision, checks it approves and covers the instrument, and refuses otherwise. Verified from both directions. |
| **A-04** | HIGH | **An environment variable alone enabled PAPER** on a database with no recorded mode — no actor, no reason, no history row. | The environment may now only *restrict*. Trading requires a stored row written by `set_mode`, which demands an actor and a reason and appends to history. |
| **A-08** | HIGH | **`result.block()` stopped nothing.** Only `health is BLOCKED` prevented trading, so any block recorded after the health verdict was written to the record and ignored — the cycle submitted anyway. Found by breaking the model gate: block present, order sent. | The submission stage refuses when any block stands, and names which. Everything before it still runs, so a blocked cycle can still explain what it would have done. |
| **A-05** | MEDIUM | **No anchor-drift guard.** `run_cycle(now=…)` takes its moment as an argument; a replay would decide on month-old signals and trade at today's venue. | Anchors more than 4 h behind wall clock block trading and fall back to dry run; the cycle still observes and reconciles. The limit is configuration and is in the session fingerprint. |
| **A-06** | MEDIUM | **Model-gate failures were silent.** Two helpers each swallowed every exception and returned `{}` — identical to "nothing promoted" — and each called Phase 18 separately, so the two answers could disagree. | One method, asked once, returning `(deployable, statuses, detail)`. A missing table is an answer; anything else is a reported failure that blocks. |
| **A-09** | MEDIUM | **Future information in attribution.** The sizing detector's risk budget was the *latest* account equity applied to every trade, including old ones. | Joined through `trade_lineage` to the account state of the cycle that placed the trade. |
| **A-10** | MEDIUM | **`gateway.heartbeat()` had no caller**, though it exists because the Client Portal session "lapses when idle" and the loop is exactly that idle pattern. | Beat at the start of every cycle; the result is recorded on the health stage. |
| **A-07** | LOW | `poll_broker` extended `report.fills` from a generator that read `report.fills`. | Built into a list first. |

**Two tests were changed, both because the stricter behaviour made the
old expectation wrong**: `test_a_clean_run_passes_every_check` became
`test_a_clean_run_fails_no_check` (a cycle with no fill legitimately
cannot run the two new lineage checks, so the report is correctly *not
conclusive*), and the trading fixture now sets a wide anchor-drift
limit explicitly rather than the guard being weakened for everyone.

The test fixture also gained a `ModelContribution`, because its absence
is what hid A-02 for a whole phase.

**26 regression tests** were added in `tests/trading/test_audit_25_5.py`,
one group per finding, including negative controls that prove each new
check can actually fail.

---

## T. Remaining findings

| # | Severity | Finding | Status |
|---|---|---|---|
| **A-11** | INFORMATIONAL | Real IBKR paper never contacted. No gateway exists in this environment and one cannot be started headlessly. | **UNVERIFIED — ENVIRONMENTAL BLOCKER.** Not fixed, not faked. |
| **A-12** | INFORMATIONAL | `detect_portfolio_error` remains blind: no per-cycle concentration measure exists. | **UNMEASURED**, correctly reported as INSUFFICIENT_EVIDENCE. Phase 26 work. |
| **A-13** | LOW | The duplicate-`cOID` defence after a crash rests on IBKR's behaviour, which the mock does not model. | Mitigated by reconciliation blocking the next cycle. Verify during the manual run. |
| **A-14** | LOW | Pipeline cadence (twice weekly) is incompatible with the 48-hour freshness policy given a ~39-hour feature lag. | Documented, not tuned. Operator decision. |
| **A-15** | LOW | TD-04: Phase 13's simulated paper path still exists beside the loop. | Re-scoped, did not grow. Not urgent. |

---

## U. Evidence matrix

| Claim | Evidence | Test | Result | Status |
|---|---|---|---|---|
| A signal reaches an IBKR paper position | `trade_lineage.complete = 1`, all 7 spine links non-null | `test_end_to_end_paper` (18) | order → fill → position → outcome | **PASS (mock)** |
| …against a real IBKR paper account | port + env probe: nothing listening | — | no gateway | **UNVERIFIED** |
| Nothing bypasses Risk | `--assume-risk-approved` deleted; `--decision-id` refuses a bogus id | manual probe, both directions | Risk PASS / Risk FAIL | **PASS** |
| Nothing bypasses Execution | `submit_order` has one caller | caller trace | 1 | **PASS** |
| No duplicate after restart/retry/reconnect | 4 probes | `test_loop_lifecycle`, probe suite | 1 order in every case | **PASS** |
| Divergence blocks trading | crash probe D | `test_audit_25_5` | blocked, 1 discrepancy | **PASS** |
| Target ≠ Actual | two tables; SQL-level origin filter | `test_dashboard_trading_loop` (16) | only reconciled rows listed | **PASS** |
| A loss is not an error | `classify_errors` sets 3 mechanical fields | Phase 20 suite (134) | no P&L→error path | **PASS** |
| Unmeasured ≠ passing | `DimensionReading(measured=True, value=None)` raises | `test_paper_validation` (24) | ValueError | **PASS** |
| No automatic live promotion | `LIVE_ELIGIBLE` absent from all transitions | `test_fail_safe_and_boundary` (31) | refused from 3 sides | **PASS** |
| No automated live orders | `--mock` hard-coded; 4 refusals | live-safety audit | 16/16 | **PASS** |
| Partial fills handled | half fill → `partially_filled`, half position | `test_audit_25_5` | 250/500 | **PASS (fixed)** |
| Every trade names its model | `trade_outcomes.model_id` populated | `test_audit_25_5` | `tm-fixture-1` | **PASS (fixed)** |
| Blocks stop trading | broken gate → 0 orders | `test_audit_25_5` | BLOCKED | **PASS (fixed)** |
| No future data in a decision | anchored reads; budget joined per cycle | `tests/pointintime`, `tests/memory` | no leak | **PASS (fixed)** |
| Paper cannot contaminate research | `trade_outcomes` unread by 4 packages | grep over `src/` | 0 readers | **PASS** |

---

## V. Exact outstanding manual steps

One session, with a Client Portal Gateway running and logged in:

```bash
# 1. start the gateway and log in (browser), then:
export IBKR_ENVIRONMENT=paper
export IBKR_ACCOUNT_ID=DU………

python scripts/run_trading_loop.py --status
python scripts/run_trading_loop.py --cycles 1 --experimental --verbose
python scripts/run_ibkr.py --resolve --symbol AAPL --instrument i-aapl
python scripts/run_trading_loop.py --cycles 1 --experimental \
       --allow-paper-orders --no-dry-run --verbose
python scripts/run_trading_loop.py --integrity
```

Record, against §8's list: session startup, authentication, account
identification, summary, buying power, market data, contract
resolution, order creation, submission, acknowledgement, status, fill,
position update, reconciliation, cancellation, reconnect, restart
recovery, duplicate protection.

Pay particular attention to **A-13**: submit, kill the process between
acknowledgement and persistence, restart, and confirm IBKR rejects the
repeated `cOID` — the one defence the mock cannot model.

---

## W. Phase 26 readiness verdict

**READY FOR PHASE 26 WITH EXPLICIT CONDITIONS.**

Gate check per §36:

| Requirement | State |
|---|---|
| CRITICAL = 0 | ✅ |
| HIGH = 0 (after remediation) | ✅ five found, five fixed |
| live trading blocked | ✅ four independent refusals |
| IBKR paper safety verified | ⚠️ verified in code; **venue unverified** |
| risk is a hard gate | ✅ the one bypass deleted |
| reconciliation blocks discrepancies | ✅ and now actually stops submission |
| target vs actual correct | ✅ |
| idempotency reliable | ✅ five restart/retry probes |
| crash recovery safe | ✅ explicit UNKNOWN, no duplicate |
| lineage complete | ✅ including the model, and cross-checked |
| no fake metrics | ✅ |
| model gate active | ✅ and its failures now visible |
| paper governance intact | ✅ |
| no automatic live promotion | ✅ |

> **READY FOR CODE-LEVEL SHADOW PREPARATION, BUT REAL IBKR PAPER
> VALIDATION REMAINS OUTSTANDING.**

---

## §39 — the fifteen questions

1. **Can the project take a real valid signal through to IBKR PAPER?**
   Yes, in code. On the production record it correctly takes none: the
   Phase 11 constraint `min_signal_confidence` is 0.40 and every
   current signal carries 0.30.
2. **Demonstrated against real IBKR paper, or only the mock?**
   **Only the mock.** No gateway exists here; probed and confirmed.
3. **Can anything bypass Risk?** No. The one path that could —
   `--assume-risk-approved` — was found and deleted.
4. **Can anything bypass Execution?** No. `submit_order` has exactly
   one caller.
5. **Duplicate orders after restart/retry/reconnect?** No, in five
   probes. The one residual depends on IBKR rejecting a repeated
   `cOID` (A-13).
6. **Can broker and local state diverge without blocking?** No — and
   until A-08 they could, because the block did not stop submission.
7. **Can the system mistake TARGET for ACTUAL?** No. Two tables, and
   the position query filters on origin in SQL.
8. **Can a losing trade be classified as an error automatically?** No.
9. **Can an unmeasured metric become a passing one?** No — refused at
   construction, and the integrity report counts "could not run"
   separately from "passed".
10. **Can a challenger automatically become live?** No.
11. **Can any automated process submit LIVE orders?** No.
12. **Are signal freshness and pipeline cadence compatible?**
    **No — operationally inefficient.** Signals regenerate twice a
    week; the tradeable window is ~9 hours wide. Documented, not tuned.
13. **Is Portfolio truly operational?** Operational in code and
    exercised end to end; **it has never run on production data**,
    because risk correctly declines every current signal.
14. **Is Paper Trading operational or only testable in the mock?**
    Operational against a double. The venue is unverified.
15. **Biggest remaining technical risk?** That the mock is wrong about
    IBKR. Everything in section H rests on a double, and the single
    afternoon in section V is what converts it into evidence.

---

*This audit preferred NO TRADE over UNSAFE TRADE, UNKNOWN over FALSE
SUCCESS, UNMEASURED over FAKE METRIC, and BLOCKED over UNCERTAIN
EXECUTION. Five of its ten findings were in code that a passing test
suite had already certified.*
