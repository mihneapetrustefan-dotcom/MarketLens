# Phase 25.9E — Automated Trading & IBKR Execution Readiness Report

**Written** 2026-09-18 · **Base commit** `bb8af28` · **Real IBKR order** NOT ATTEMPTED · **Live** DISABLED

---

## A. Executive Summary

**The machinery can now safely reach the broker-submission boundary. It still does not have a reason to cross it.**

Six real defects were found in the path between a market session and a broker order, each reproduced against the unfixed code before it was fixed:

| # | Defect | Evidence |
|---|---|---|
| F1 | **CRITICAL** — every price on the order path came from the research cache (`price_candle_cache`), accepted up to 5 days old, regardless of transport | production's newest daily close was 11 days old on 2026-09-16; an order built under the old code would have been sized and risk-checked on it |
| F2 | **HIGH** — reconciliation compared the broker's own positions with themselves | a broker holding +10 shares nobody ordered produced 0 discrepancies |
| F3 | **HIGH** — no durable record existed before a venue call; a crash between "sent" and "recorded" could not be told apart from "never sent" | reproduced: place_calls=1, `execution_orders` rows=0 |
| F4 | **HIGH** — nothing enforced one runner per broker account | two `SessionRunner`s both started successfully against the same account |
| F5 | **HIGH** — no exchange calendar; the venue and the Phase 12 research calendar both call after-hours and holidays "open" | reproduced: 2026-11-26 (Thanksgiving) read OPEN |
| F6 | **MEDIUM** — the session runner's own `deployable_models()` call did not exist; the resulting `AttributeError` was swallowed and every tick silently read "no deployable model" whatever the real gate said | confirmed by grep: the method was called, never defined |

**What this did not require:** no model was qualified, no confidence floor was touched, no risk gate was loosened, and no real IBKR order was placed or attempted. Live remains structurally impossible.

---

## B. Repository / Database Baseline

| | |
|---|---|
| branch / base commit | `ibkr-paper-validation-fixes` / `bb8af28`, clean |
| Python | 3.12.10 |
| production snapshot | pulled via public release download, 291,880,960 bytes, `Last-Modified: Wed, 16 Sep 2026 17:36:29 GMT`, sha256 recorded and re-verified after every read/rehearsal |
| operational market-data tables (`market_data_state`, `market_data_bars`, `market_data_cycles`) | **ABSENT** — the Phase 25.7 layer has never run against production |
| trading-loop tables (`trading_cycles`, `paper_loop_sessions`, `position_targets`, `position_actuals`, `loop_account_states`, `trade_lineage`) | **ABSENT** — 0 rows, 0 sessions, the loop has never executed against production |
| `broker_instrument_mapping` | ABSENT |
| `execution_orders` / `execution_fills` | ABSENT |
| `reconciliation_baselines` / `session_runner_leases` (new in this phase) | ABSENT before this phase's schema addition |
| `trading_mode` | ABSENT — no mode has ever been recorded; the durable default is OFF |
| model governance | 8 trained models, 0 ACTIVE |
| signals | 440 rows, max confidence 0.30 against a floor of 0.40 (unchanged since Phase 25.9) |
| full test count (start of phase) | 4,109 OK, 1 skipped |
| live-safety audit (start of phase) | 16/16 PASS |

Every "ABSENT" above was distinguished from "present but empty" by table existence, not just row count — `scripts/audit_trading_readiness.py` reports both states separately.

---

## C. Actual Trading Architecture

Traced from real callers, not intended design:

```
SessionRunner.run_until_close (WallClock only, RunMode.requires_wall_clock enforced)
  -> run_tick (every schedule.tick_seconds)
       -> MarketDataService.run_cycle          [cadence: market_data, 60s]
       -> features stage (bar-completeness only, no computation)   [cadence: features, 300s]
       -> signals stage -> TradingLoop.deployable_models()          [cadence: signals, 300s]
       -> portfolio / risk observational stages [cadence: 300s]
       -> TradingLoop.run_cycle()               [cadence: reconciliation, 900s]
            -> mode (TradingModeStore.resolve, durable, env can only restrict)
            -> health (heartbeat + LIVE connection_state re-read every cycle)
            -> _observe: broker_poll -> fills -> positions -> reconciliation
                 (now compares OUR book: last agreed baseline + our fills,
                  never the broker's own positions against themselves)
                 -> P&L -> outcomes
            -> _decide_and_submit: market_data(operational) -> signals -> eligibility
                 -> portfolio -> risk -> targets -> intents(Phase17 intake)
                 -> SUBMISSION (structural stop or dry-run or real submit)
            -> _persist: order book + lineage
            -> _assess_readiness (new): ExecutionReadiness verdict, always recorded
```

Every arrow's caller, callee, trigger, DB writes, failure behaviour, idempotency key and safety gate is exercised by `tests/trading/test_execution_readiness_25_9e.py` (44 tests) plus the pre-existing suites (224 trading, 460 execution — all still passing).

---

## D. Current Automatic Stop Point (offline reading, production snapshot)

```
SESSION RUNNER     CODE EXISTS; NOT SCHEDULED   no workflow invokes scripts/run_session.py
MARKET DATA        ABSENT                       market_data_state ABSENT; broker_instrument_mapping ABSENT
PRICE FRESHNESS    NONE USABLE
BAR STATE          ABSENT
MODEL GATE         NO DEPLOYABLE MODEL          0 active of 8 trained
SIGNAL GATE        NOT READY                    440 signal(s), max confidence 0.3, floor 0.4
TRADING MODE       OFF                          no trading mode has ever been recorded
RUNNER OWNERSHIP   FREE

CURRENT STOP POINT: MARKET DATA — the operational layer has never run against
this database (no contract mappings, no quotes).
```

Behind that, in order: no trading mode has ever been recorded (durable default OFF); even were mode PAPER, no deployable model exists (0 of 8 ACTIVE); even with a model, no signal clears the 0.40 confidence floor (max 0.30). **Any one of these alone stops trading today; all three currently hold.**

A live pre-submission cycle run against the production snapshot with a mocked venue (§AE) confirms this chain exactly, stage by stage.

---

## E. Deployment / Scheduler Reality

| | |
|---|---|
| `scripts/run_session.py` invoked by any GitHub Actions workflow | **no** — no `run_session.yml` exists at all |
| `scripts/run_trading_loop.py` / `run_paper_session.yml` | exist, `workflow_dispatch` only; cron lines present but **commented out** |
| any workflow scheduling the market-data service alone | none |
| currently scheduled workflows (active cron) | `pipeline.yml` (2 entries), `daily.yml` (3 entries) — neither touches the trading loop |
| host requirement for the session runner | a **local, supervised, always-on process** with the Client Portal Gateway already logged in via browser; GitHub Actions structurally cannot hold that session (documented since Phase 25.8, unchanged) |
| local scheduled task / persistent process found on this machine | none (checked via `Get-ScheduledTask` and running-process scan) |

**Classification: CODE EXISTS. SCHEDULE DOES NOT EXIST. NO PERSISTENT RUNTIME EXISTS. REAL SESSION NOT VERIFIED.**

---

## F. Session Runner

Re-audited against Phase 25.8's own report plus new adversarial tests:

- **Real wall clock, enforced structurally.** `RunMode.requires_wall_clock` is checked at `SessionRunner.__init__`; `ClockModeViolation` raises before any tick. A `ReplayClock` cannot reach `PAPER_SESSION` or `REAL_SESSION` mode by flag, config, or accident — unchanged, still tested (`TestClockModes`, 6 tests).
- **Grid boundaries, not sleeps.** Work duration is absorbed by the wait to the next boundary; overruns skip forward and are counted, never backlogged — unchanged, still tested.
- **Restart mid-session, startup after open** — covered by existing `test_session_runner.py` suite (unchanged, 22 tests, all passing).
- **`deployable_models()` fixed (F6).** The method the runner's `_stage_signals` called did not exist on `TradingLoop`; the bare `except Exception` swallowed the resulting `AttributeError` and every tick reported "no deployable model" regardless of the real gate. Added as a thin, honest wrapper around the same `_model_governance()` the cycle itself uses — one interface, asked once. An unreadable gate now fails the stage with its reason instead of reading as an empty gate.

---

## G. Market Calendar

`src/marketdata/calendar.py` (new). Rule-based, computed for any year — not a lookup table that goes stale on 1 January.

- Regular session 09:30–16:00 America/New_York, correctly converted through both DST regimes (tested at the March and November boundaries).
- The ten NYSE full-day holidays (with Rule 7.2's New Year's Day exception and Saturday/Sunday observance shifting).
- The three 13:00 early closes (3 July eve, day after Thanksgiving, Christmas Eve).
- Verified against the **published 2026 and 2027 NYSE calendars** exactly (10 holidays, 2 early closes each year, dates match).
- Wired into `IBKRGateway.market_status` **ahead of** both the venue's own live-quote inference and the Phase 12 research calendar: an exchange holiday or after-hours moment is CLOSED / HOLIDAY / AFTER_HOURS however fresh an IBKR snapshot looks, because IBKR happily serves available data outside regular hours.
- **Reproduced before the fix:** 2026-11-26 (Thanksgiving), 23:00 UTC, on a date the daily research cache holds a bar for, read `MarketStatus.OPEN`.
- Wired into every gateway builder: `src/trading/stack.py` (the loop's `build_stack`), `scripts/run_session.py`, `scripts/run_ibkr.py`, `scripts/run_market_data.py`, `scripts/run_operations.py`.

---

## H. Operational Market Data

Re-audited against Phase 25.7:

- The operational/research boundary (`market_data_state` vs `price_candle_cache`) is structurally unchanged and still correct in isolation — `operational_price()` cannot reach the research cache.
- **The boundary was never connected to the order path (F1).** `PortfolioService.prices` was always a research-cache `PriceRepository`, whatever the transport. Fixed with `src/trading/pricing.py`: `OperationalPriceRepository` reads only `market_data_state`, refuses a quote stamped after the decision moment, and is swapped into the loop's `PortfolioService` whenever `resolve_price_source()` says OPERATIONAL — which it always does against any transport whose `name != "mock"`. History (volatility, return series) is legitimately research data and stays delegated unchanged.
- One cheap, unambiguous fix to the mock venue: `MockIBKRTransport`'s seeded/added/set quotes never carried IBKR's own realtime marker (field `6509`), so the real acquisition path (`src/marketdata/quotes.py`) always classified them `UNKNOWN`/not-tradeable. The end-to-end market-data service could only be exercised by writing operational state directly, never through a real snapshot. Fixed (§AE shows the corrected pipeline actually producing a tradeable quote).

---

## I. Real IBKR Read-Only Validation

**No real IBKR paper session was safely available in this environment** — `curl` to `https://localhost:5000/v1/api/iserver/auth/status` returned `HTTP 000` (no gateway listening), and no local Client Portal Gateway process or scheduled task was found.

**REAL IBKR MARKET DATA = UNVERIFIED.** Not faked with the mock. Every claim in this report that needed a venue used `MockIBKRTransport` and is labelled MOCK VERIFIED, never merged with VERIFIED REAL IBKR (§AF).

---

## J. Price Freshness / Disconnect / Reconnect

`OPERATIONAL_FRESHNESS` (Phase 25.7, unchanged): fresh ≤120s, aging ≤300s, stale ≤900s.

| Test | Result |
|---|---|
| order priced from the current operational quote, not the cache | PASS — order's `reference_price` matches the injected quote exactly |
| no operational quote, cached close exists | PASS — 0 orders, 0 venue submissions |
| stale quote (16 min old) | PASS — blocked, 0 submissions |
| delayed availability | PASS — blocked |
| quote stamped after the decision moment | PASS — refused as future information |
| good quote, then 16 minutes of silence | PASS — ages from usable to `None` (disconnect) |
| a fresh quote after the gap | PASS — freshness restored, no duplicate bar state |

---

## K. One-Minute Bars

Unchanged from Phase 25.7 (`src/marketdata/bars.py`): correct minute boundaries, gaps recorded (never interpolated), out-of-order/duplicate quotes rejected with a reason, a bar only emitted once the clock has moved past its end. `_seal`/`flush` logic re-verified by the existing suite (unchanged, all passing). The rehearsal (§AE) exercised the real `MinuteBarBuilder` against a live mock snapshot cycle.

---

## L. Intraday Features

**Unchanged limitation, honestly reported, not overstated.** The session runner's `features` stage counts instruments with completed intraday bars; it does not compute an intraday feature. This is Phase 25.8's own documented scope, and this report does not claim otherwise (`FEATURE REFRESH: PARTIAL` in the audit tool's own output).

---

## M. Model Gate

- The canonical Phase 18 `modeling.selection.candidates()` interface is asked exactly once per cycle inside `_model_governance()`, and the session runner's own `deployable_models()` (F6, §F) now asks the **same** interface rather than silently returning nothing.
- No duplicate interpretation of ACTIVE / DEPLOYABLE / EVALUATED / CANDIDATE anywhere in the changed code.
- **The gate was not touched in either direction.** Production: 0 ACTIVE of 8 trained, unchanged before and after this phase.

---

## N. Signal Eligibility

**DEPLOYABLE MODEL: NO** on the production snapshot (0 of 8 ACTIVE). **SIGNAL ELIGIBILITY: NOT READY** (max confidence 0.30 against a floor of 0.40, unchanged since Phase 25.9). This is the correct, expected state and is reported as `NORMAL_NO_TRADE`, not as a fault, by the new `src/trading/readiness.py` classifier.

---

## O. Portfolio

`PortfolioService.evaluate` is unchanged in its own logic. What changed is its **price source**: when the transport is not the mock, `service.prices` and `service.valuator.prices` are swapped for `OperationalPriceRepository` before evaluation, so sizing, valuation and the risk inputs all read the same current quotes the loop itself uses (§H). A zero-change portfolio (target == reconciled actual) still produces an explicit no-op with a recorded reason (`"no change required: targets already met..."`), never a silent skip.

---

## P. Risk

- **No caller can manufacture `risk_approved=True`.** Repo-wide search for bypass-style parameters (`--assume-risk-approved`, `force_approve`, similar) found none reachable from the loop or the two IBKR CLIs; `src/execution/intake.py` is the only converter of an approved `RiskDecision` into execution requests, and it raises `RiskNotApproved` rather than defaulting to approved.
- **Periodic risk** (Phase 25.8): confirmed it only observes/reports on its cadence; no code path invents a discretionary trade from the periodic stage.
- **Price-dependent risk, classified honestly:**

  | Control | Status |
  |---|---|
  | position value / exposure / concentration | **OPERATIONAL** (now on live operational prices, §H) |
  | drawdown, intraday P&L | **OPERATIONAL**, but naive average-cost accounting is deliberately disagreement-seeking (§Y), not a precision claim |
  | stop logic | **NOT IMPLEMENTED** (unchanged; no stop-order policy exists in this architecture) |
  | margin | **PARTIAL** — read from the broker account snapshot only, never independently computed |

---

## Q. Account State

- Every field on `CanonicalAccountState` carries a mandatory `source`; only `BROKER` may be reported as fact (unchanged, Phase 25.5).
- **Fixed:** the broker connection was read once, at process start (`stack.connected`), and never re-checked. A session that lapsed mid-day still reported a usable broker to the health verdict and to submission eligibility. Now re-read every cycle via `gateway.connection_state().can_submit`, combined with the original build-time connectivity — both must hold.
- **Account freshness bound:** 900 seconds (`DEFAULT_MAX_ACCOUNT_AGE_SECONDS`, unchanged, Phase 25.5). Verified: a fresh quote plus a 2-hour-old account snapshot correctly blocks (`account_state_unknown`).
- No real IBKR account was read in this phase (no venue available); the mock account path is exercised end to end.

---

## R. Order Intent

`ExecutionOrder`/`OrderIntent` (Phase 14/17, unchanged schema) already carries: decision_id, intent_id, order_id, instrument_id, side, quantity, strategy_id, signal_id, model_version, prediction_id, environment, `decision_price`/`reference_price`, `client_order_id`, `idempotency_key`, and full timestamps. **No duplicate schema was created.**

---

## S. Idempotency

`session/cycle anchor -> decision_id -> intent_id -> idempotency key -> client_order_id` (Phase 11/14/17, unchanged derivation chain). Re-verified adversarially:

| Case | Result |
|---|---|
| same cycle rerun (repeated anchor) | PASS — `CYCLE_ALREADY_RUNNING`, 0 additional submissions |
| process restart with an order already accepted at the venue | PASS — `stack.repository.restore()` reloads the in-flight order; the venue was not asked to place a second one |
| pre-submission rerun at the same anchor | PASS — refused, 0 venue calls |
| `client_order_id` determinism | PASS — deterministic per idempotency key, 23 chars, `ml-` prefixed, bounded well under IBKR's limit, traceable back to the local order |

---

## T. Execution

- **Write-ahead persistence (F3, new).** `ExecutionOrchestrator.before_submit` is called — and its result required — **before** `gateway.submit_order()`. If the write fails, the order is rejected locally and the venue is never contacted. Wired by `ExecutionService.__init__` to `repository.save_execution` whenever a repository is present.
- **Reproduced before the fix:** a crash immediately after the mock venue accepted (simulated via `_persist` raising) left `place_calls=1` but `execution_orders` rows=0 — a blind spot a restart could not see without asking the broker first.
- **After the fix:** the order reaches `execution_orders` in state `submitting` *before* the venue is called (asserted directly against the order's stored state at the moment of the spied `submit_order` call).

---

## U. Broker Boundary — Phase 25.9E's Structural Stop

`src/execution/adapters/submission_guard.py` (new): `PreSubmissionGateway` wraps the real gateway. Every read (session, heartbeat, account, positions, open orders, contracts, quotes, reconciliation) passes through to the real object unchanged. `submit_order` / `cancel_order` / `modify_order` raise `BrokerSubmissionForbidden` carrying the distinctive code `PHASE_25_9E_BROKER_SUBMISSION_FORBIDDEN`, and every attempt is recorded on `attempted_writes`.

- `build_stack(..., pre_submission_only=True)` installs it and registers **the wrapped object** with the orchestrator — there is no code path that holds the unguarded gateway once this flag is set.
- `ExecutionStack.may_submit` returns `False` whenever `gateway.submission_forbidden` is true, independent of every other condition.
- **Negative control (§AD):** the trap is deliberately triggered by calling `submit_order` directly on the guarded gateway; it raises with the correct code and `place_calls` stays 0.
- **44 real-loop tests exercise the guarded path; the trap never fires in any legitimate 25.9E test.** It fires only in the one test written to trigger it on purpose.

`--pre-submission-only` is a new CLI flag on `scripts/run_trading_loop.py` and `scripts/run_session.py`.

---

## V. Reconciliation (F2)

**Before the fix**, `internal_positions` passed to the reconciler was built from `fresh` — the gateway's own just-polled positions — compared against `broker_positions`, which is also read from the gateway. A position the broker reports that we never traded could not be a mismatch, because both sides of the comparison came from the same source.

**Reproduced:** a broker holding +10 shares of an instrument the loop never ordered produced `discrepancies=0`.

**Fixed:** `TradingLoop.expected_positions()` now derives OUR book from the last agreed `reconciliation_baselines` row plus every one of our own fills since — the actual "what do we think we hold" a reconciler is supposed to check against. A clean reconciliation writes a new baseline; a dirty one blocks new execution and leaves the baseline untouched. `loop.accept_broker_positions(actor=..., reason=..., now=...)` is the explicit, audited operator path to adopt the broker's book (new CLI flag `--accept-broker-positions`, requires `--reason`).

Also fixed as a byproduct: the mock venue's `unrealizedpnl` field was a constant `0.0`, disagreeing with any position marked away from cost and making the P&L cross-check correctly-but-confusingly fire on ordinary two-fill scenarios; the mock's account summary and average-cost bookkeeping now compute it the way a venue would.

---

## W. Restart Recovery

| Scenario | Result |
|---|---|
| crash between claim and the cycle running | reclaimed after `DEFAULT_STALE_CLAIM_SECONDS` (3600s, unchanged) |
| crash after the venue accepted, before persistence | write-ahead record survives (§T); on restart `ExecutionRepository.restore()` marks it in-flight, `orders_in_flight()` reflects it, and no second submission occurs |
| crashed session runner (lease holder dies) | `session_runner_leases` expires after 3× the tick interval (minimum 300s); a new runner **takes over**, the takeover is recorded in `SessionState.blocks`, and the old runner — once it wakes and fails to renew — **stops acting on the account** on its very next tick |
| two runners racing to start | exactly one wins (`leases.acquire`'s single conditional `INSERT ... ON CONFLICT` UPDATE, same SQLite-decides pattern as the Phase 23 queue claim); the loser gets `SessionRefused` |

---

## X. Partial / Duplicate / Out-of-Order Fill Tests

All against `MockIBKRTransport`, through the real loop, with reconciliation now comparing our own book (§V):

- Two partial fills at different prices, with duplicate-execution injection turned on and a process restart between them: no overfill, correct final position and average price, **no reconciliation mismatch**, the remainder is never re-ordered (`place_calls` stays at 1 for the whole sequence), exactly 2 `execution_fills` rows recorded.
- Existing Phase 25.5 partial-fill, overfill, and duplicate-execution tests (`test_audit_25_5.py`) — unchanged, still pass.
- Out-of-order broker events (a late `WORKING` status after `FILLED`) — pre-existing `EventProcessor` behaviour, unchanged, still tested.

---

## Y. P&L / Outcomes

`compute_pnl` (unchanged design) deliberately runs naive average-cost accounting specifically so it can **disagree** with the broker's number when something is wrong — it is not meant to be a precision P&L engine. The mock venue's own numbers were fixed (§V) so that a normal partial-fill sequence at different prices no longer manufactures a spurious disagreement. `TradeOutcome` lineage is produced only through the mock/fixture path; no real broker order exists to produce one from.

---

## Z. Execution Readiness State (§76, §77)

`src/trading/readiness.py` (new): `ExecutionReadiness` — `verdict` (`READY_TO_SUBMIT` / `NOT_READY`), `classification` (`NORMAL_NO_TRADE` / `GOVERNANCE_HOLD` / `TEMPORARY_BLOCK` / `SYSTEM_ERROR` / `READY`), **and separately** `system_ready: bool` / `order_authorized: bool` / `authorization_missing: List[str]`.

**The critical acceptance scenario (§75), reproduced on a real (mocked-venue) cycle:**

```
verdict            = READY_TO_SUBMIT
classification     = READY
system_ready        = True
order_authorized    = False
requests_ready      = 1
authorization_missing = ["Phase 25.9E pre-submission mode: submission is
                          structurally disabled",
                          "IBKR paper ordering not enabled or broker not
                          connected"]
orders_submitted    = 0
venue place_calls   = 0
guard attempted_writes = []
```

Every cycle records a readiness verdict, whatever it did — including a crashed one (assessed in `run_cycle`'s `finally`-equivalent path, from whatever context survived).

---

## AA. Human / Governance Actions Remaining

Unchanged from Phase 25.8, restated precisely:

| Action | Classification |
|---|---|
| IBKR Client Portal browser login | **required governance / IBKR platform constraint** |
| model promotion to ACTIVE | **required governance** |
| `--allow-paper-orders` | **required governance** — running the loop is never permission |
| `--pre-submission-only` removal (new) | **required governance** — this phase's own stop is not lifted automatically by any flag combination; `may_submit` also independently requires the config toggle to be absent |
| accepting a broker-position discrepancy | **required governance**, now with an explicit, audited path (`--accept-broker-positions --reason ...`) instead of no path at all |
| keeping a host process awake with the gateway running | **deployment dependency**, not automated by this or any prior phase |

---

## AB. Scheduler / Workflow Audit

| Workflow | Schedule | Manual | Touches trading | Notes |
|---|---|---|---|---|
| `run_trading_loop.yml` | commented out | `workflow_dispatch` | yes | `--mock` unless overridden; `concurrency: marketlens-data-write` |
| `run_paper_session.yml` | commented out | `workflow_dispatch` | yes | same concurrency group |
| (no `run_session.yml`) | — | — | — | the session runner has **no** workflow at all |
| (no `run_market_data.yml`) | — | — | — | the market-data service has **no** workflow at all |
| `pipeline.yml` | **active**, 2 cron entries | yes | no (research only) | own dedicated concurrency group |
| `daily.yml` | **active**, 3 cron entries | yes | no | own dedicated concurrency group |

**No workflow may accidentally reach LIVE:** `TradingModeStore.set_mode` refuses `TradingMode.LIVE` structurally (`TradingModeRefused`), and no CLI or workflow argument can request it. Verified as a negative control (§AD).

**Concurrency:** all trading-touching workflows share the `marketlens-data-write` group with `cancel-in-progress: false`, so a manual dispatch and a (currently nonexistent) scheduled run could not execute concurrently at the GitHub Actions level. This does not by itself prevent two runner *processes* started outside Actions — that protection is the new lease (§F, §W).

---

## AC. Observability

Every field §80 asks for is now either already exposed (`SessionRunner.describe()`, unchanged) or newly recorded per cycle: `CycleResult.readiness` (new), the lease's `session_runner_leases` row (new, `RUNNER OWNERSHIP`), the reconciliation baseline (new), and the audit trail entry `cycle_readiness` written on every cycle regardless of outcome. `scripts/audit_trading_readiness.py` surfaces all of it read-only in one place.

---

## AD. Failure Injection — Negative Controls

`scripts/audit_trading_readiness.py --negative-controls`: **13/13 passed**, run against the real loop, the real mock venue, and the real `PreSubmissionGateway`:

```
PASS clean fixture reaches READY_TO_SUBMIT and sends nothing
PASS stale price blocks
PASS future price blocks
PASS missing account blocks
PASS stale account blocks
PASS risk rejection sends nothing
PASS reconciliation mismatch blocks
PASS session loss blocks
PASS no deployable model is a normal no-trade
PASS duplicate cycle is refused
PASS second runner is refused
PASS forbidden LIVE mode is refused
PASS broker-submit trap fires
```

Every scenario asserts `place_calls == 0` in addition to the expected classification — a control that merely checked the readiness verdict without checking the venue was never asked would not have caught F1–F3.

---

## AE. Real-Data Working-Copy Rehearsal

Working copy: fresh copy of the pulled production snapshot, in the scratchpad only. Orders structurally disabled throughout (`pre_submission_only=True`). Path guard refuses anything under the repo's `data/` directory or outside the scratchpad. Production snapshot sha256 re-verified **unchanged** after the full rehearsal.

**Step B — SessionRunner.start() against production data exactly as it stands (no market-data run yet):**
```
REFUSED: no instrument is in session; there is nothing to run
```
(No resolved IBKR contract for any instrument yet — the correct, honest refusal.)

**Step C — one real loop cycle, production data, no trading mode recorded:**
```
readiness.verdict         = NOT_READY
readiness.classification  = TEMPORARY_BLOCK
readiness.reasons         = ["stale_market_data: ... no market data is
                              available at the anchor"]
blocks                    = [mode_not_permitted (x2), stale_market_data]
orders_submitted           = 0
venue place_calls          = 0
```

**Step D — one MOCK contract resolved for AAPL, one real market-data acquisition cycle through the real `MarketDataService`+`MinuteBarBuilder`, PAPER mode explicitly recorded with actor and reason:**

```
market-data cycle: requested=1, tradeable=1, health=HEALTHY
runner tick:  market_data "1/1 tradeable, 0 bar(s)"; health DEGRADED
              (blocks: "no instrument has a fresh operational price" —
               correct: the tick's own clock had already moved past the
               quote's evaluation instant by the time it re-checked)
loop cycle:   market_data "operational: freshest tradeable quote 4s old"
              signals "440 live at the anchor"
              eligibility "0 of 440 eligible" (no deployable model)
readiness.verdict         = NOT_READY
readiness.classification  = NORMAL_NO_TRADE
readiness.reasons         = ["no deployable model; signals are not eligible"]
orders_submitted           = 0
venue place_calls          = 0
guard.attempted_writes     = []
```

This is the full chain working correctly end to end: a real (mocked) tradeable quote reaches the loop through the real acquisition path, 440 real production signals are evaluated, and the **sole** remaining blocker is the real production fact that no model is deployable — exactly matching the production `MODEL GATE` reading, and correctly classified as `NORMAL_NO_TRADE`, not an error.

**Table diff:** every table with pre-existing content is byte-identical before and after (`E_existing_tables_changed = []`). Only new, empty-before, trading/market-data-scope tables gained rows: `broker_accounts`, `broker_capability`, `broker_instrument_mapping`, `brokers`, `execution_audit`, `loop_account_states`, `market_data_bars`, `market_data_cycles`, `market_data_state`, `paper_loop_sessions`, `reconciliation_baselines`, `reconciliation_records`, `session_runner_leases`, `signal_eligibility`, `trading_cycle_stages`, `trading_cycles`, `trading_loop_audit`, `trading_mode`, `trading_mode_history`. No research table, no model table, no signal table was written.

---

## AF. Real IBKR vs Mock Matrix

| Item | Status |
|---|---|
| session auth | CODE ONLY (no gateway reachable this session) |
| heartbeat | MOCK VERIFIED |
| reconnect | MOCK VERIFIED |
| account | MOCK VERIFIED |
| balances | MOCK VERIFIED |
| positions | MOCK VERIFIED |
| contract resolution | MOCK VERIFIED |
| quote | MOCK VERIFIED |
| market-data freshness | MOCK VERIFIED |
| open-order read | MOCK VERIFIED |
| reconciliation | MOCK VERIFIED |
| restart | MOCK VERIFIED |
| client-order-id mapping | MOCK VERIFIED (deterministic derivation is code, not venue-dependent) |
| **submit order** | **NOT REAL-VERIFIED** — structurally prevented in this phase |
| **broker ACK** | **NOT REAL-VERIFIED** |
| **partial fill** | MOCK VERIFIED only |
| **full fill** | MOCK VERIFIED only |
| **cancel** | CODE ONLY (mock path exists, not exercised this phase) |
| **position resulting from a fill** | MOCK VERIFIED only |
| **trade outcome** | MOCK VERIFIED only |

No category above is overstated. This phase's earlier automation audits' `VERIFIED LIVE` claims (real IBKR session auth, account, positions, reconciliation, from prior sessions) are **not** re-asserted here — this environment had no reachable gateway, so today's evidence is CODE ONLY / MOCK VERIFIED throughout.

---

## AG. Security / Credentials

- No credential, password, session cookie, token, or account identifier appears in any new or changed file (`git diff` scanned for `password|secret|api_key|token=|bearer`, and for the production account id — none found).
- No gateway logs were captured or committed.
- Nothing in this report or its test fixtures requests, prints, or stores a real credential.

---

## AH. D20 Isolation

| | |
|---|---|
| anchor-v2 / D20 hypothesis / protected window / readiness checker code | `git diff bb8af28 -- src/research src/impact src/modeling src/experiments src/autoresearch src/challengers` | **empty** |
| `research/protected_tests/ledger.jsonl` | byte-identical (unchanged since Phase 25.9D) |
| `scripts/validate_d20_reversal.py` executed | **no** |
| research integrity command | `scripts/audit_research_integrity.py --negative-controls` → **10/10 passed** |

```
D20 RESULT      = UNSEEN
D20 TEST        = NOT EXECUTED
D20 CONSUMPTION = UNCONSUMED
```

---

## AI. Tests

New: `tests/trading/test_execution_readiness_25_9e.py` — **44 tests**, covering real-clock session boundary, exchange calendar (holidays/DST/early close), duplicate runner ownership, runner crash/takeover, market-data freshness aging, operational-vs-research pricing, disconnect/reconnect, reconciliation-against-our-own-book, write-ahead + restart, order-intent idempotency, duplicate/partial/restarted fills, the structural submission trap (both legitimate-never-fires and deliberate-fires), and the full readiness classification matrix (no-model / no-signal / risk-declined / stale-data / reconciliation-blocked / governance-hold / system-error / healthy-ready-to-submit).

No existing test was weakened. Two pre-existing tests were confirmed to fail against the unfixed code during development (`test_a_disconnected_gateway_places_nothing`, `test_the_stage_record_survives_a_failure`) and both now pass alongside the fix.

Suites, all currently passing: trading 224, execution 460, marketdata 57, portfolio 273, backtest 300, dashboard-trading-loop 16, and the new 44.

**Full repository suite: 4,153 tests OK, 1 skipped** (4,109 OK at the start of this phase — the difference is exactly the 44 new tests; nothing else moved).

---

## AJ. Live Safety

```
scripts/audit_live_safety.py --untracked
ALL 16 AUDIT QUESTIONS PASS
INTERACTIVE BROKERS = ONLY BROKER
MT5 = NOT IMPLEMENTED
REAL MONEY EXECUTION = BLOCKED BY DEFAULT
```

**Negative control:** a throwaway `src/_phase_25_9e_nc_probe.py` naming `MetaTrader5`/`mt5.initialize` was added; Q11 correctly **FAILED**. Removed; 16/16 restored. The audit's own broker-scope check is proven capable of catching a real violation.

---

## AK. Findings / Remediations

| ID | Severity | Finding | Reproduced? | Fixed? | Tests |
|---|---|---|---|---|---|
| F1 | **CRITICAL** | Order-path decisions priced from the research cache regardless of transport | yes — order reference_price=100.0 off an 11-day-old cache on a "real" transport with zero operational quotes | yes — `src/trading/pricing.py`, transport-conditioned, mock-only opt-out | 7 |
| F2 | HIGH | Reconciliation compared the broker's positions with themselves | yes — +10 ghost position, 0 discrepancies | yes — baseline-derived `expected_positions()`, operator-acceptance path | 4 |
| F3 | HIGH | No durable record before a venue write; crash-then-restart could double-submit | yes — place_calls=1, 0 local rows after simulated crash | yes — `before_submit` write-ahead hook | 3 |
| F4 | HIGH | No lease; two runners could both operate one account | yes — 2 runners started, 2 place_calls possible | yes — `src/trading/leases.py`, atomic acquire/renew/takeover | 6 |
| F5 | HIGH | No exchange calendar; holiday/after-hours read OPEN | yes — Thanksgiving 23:00 UTC → OPEN | yes — `src/marketdata/calendar.py`, rule-based, wired ahead of venue inference | 6 |
| F6 | MEDIUM | `SessionRunner._stage_signals` called a nonexistent method, swallowed by a bare except | yes — grep confirms no such method existed | yes — `TradingLoop.deployable_models()` added, exception no longer swallowed | covered by session-runner suite |
| F7 | LOW | Mock venue's `unrealizedpnl` constant zero produced spurious P&L disagreements on ordinary two-fill sequences | yes | yes — mock account summary now sums real position P&L | covered by §X test |
| F8 | LOW | Mock venue quotes never set IBKR's realtime marker; market-data service untestable end to end via real acquisition | yes | yes — one-line addition, no behavioural change to any existing test | verified in rehearsal |
| F9 | INFORMATIONAL | Intraday features remain a cadence/boundary, not a computation | n/a (documented limitation, unchanged) | documented, not fixed (out of Phase 25.8/25.9E scope) | — |

No CRITICAL findings remain open. No HIGH findings remain open.

---

## AL. Remaining Risks

1. **The session runner has no scheduled or persistent deployment.** Running it requires a human-started, always-on local process with the gateway already authenticated. This is a deployment dependency, not a code defect, and is unchanged by this phase.
2. **No real IBKR session was available to verify against.** Every claim in this report about live IBKR behaviour is either CODE ONLY or carried forward from a prior phase's separate session; nothing here overstates it.
3. **Margin is read from the broker snapshot only**, never independently modelled — a stale-but-plausible margin figure is only caught by the 900-second account-age bound, not by any margin-specific check.
4. **Stop-loss logic is not implemented** anywhere in this architecture; this was true before this phase and remains true.
5. **These fixes reach any scheduled pipeline only once this branch is merged.** No workflow currently runs the trading loop or the session runner on any schedule.

---

## AM. Phase 25.95 Readiness

Explicit prerequisites for the first controlled real IBKR **paper** order, none of which this phase satisfies or attempts:

| Prerequisite | Current state |
|---|---|
| authenticated real paper session | not established this phase (no gateway reachable) |
| correct account/environment | `IBKRConfig.can_submit_orders` already refuses real money structurally (unchanged) |
| current market price | **now reachable** — operational pricing exists and is wired (F1 fixed) |
| qualified model | **NO** — 0 of 8 ACTIVE; unchanged, a model-quality question outside this phase |
| eligible signal | **NO** — max confidence 0.30 vs floor 0.40; unchanged |
| portfolio decision | mechanism ready (now priced correctly) |
| approved risk decision | mechanism ready, cannot be bypassed (§P) |
| order intent | mechanism ready, full lineage (§R) |
| clean reconciliation | mechanism ready, now genuinely checks against our own book (§V) |
| idempotency identity | verified end to end (§S) |
| **explicit human paper authorization** | **not granted** — `--allow-paper-orders` plus the absence of `--pre-submission-only` is required and was not used to submit anything |
| small controlled quantity | a Phase 25.95 decision, not made here |
| venue acknowledgement / broker order state / fill / reconciliation / outcome | **NOT REAL-VERIFIED**, by design (§AF) |

**Phase 25.95 remains blocked on the same thing Phase 25.8's report named: model quality.** This phase closes every infrastructure gap it was scoped to close; it manufactures no model, no signal, and no authorization.

---

## AN. Verification

- **Full suite: 4,153 tests OK, 1 skipped** (526.1s), rerun clean after every fix in this phase landed, including the final CLI-wiring and mock-transport edits.
- Live safety: 16/16 PASS, with a proven negative control (a planted MT5 reference correctly failed Q11, then passed again once removed).
- Research integrity: 10/10 negative controls.
- Trading-readiness negative controls: 13/13.
- Production snapshot sha256 (`7eeb61e7...`) unchanged through every read and both rehearsal runs.
- SQLite lock contention measured under 4-way concurrent load (market data, loop audit writes, reconciliation baseline writes, dashboard reads) for 20s per worker: 0 lock errors, worst single-operation wait 4.05s (a market-data upsert batch), well inside any cadence in `DEFAULT_CADENCES`.

---

## Required Automation Matrix (§97)

| Component | Implemented | Scheduled | Real-data verified | Real IBKR verified | Mock verified | Current blocker |
|---|---|---|---|---|---|---|
| market session | yes | no | yes (rehearsal, §AE) | no | yes | no scheduled runtime |
| market data | yes | no | yes (rehearsal) | no | yes | no scheduled runtime; no contract mappings in production |
| 1m bars | yes | no | yes (rehearsal) | no | yes | same |
| feature refresh | partial (cadence only) | no | n/a | no | n/a | intraday features not computed (documented, unchanged) |
| model inference | yes (batch, 2×/week) | yes (pipeline.yml) | yes | n/a | n/a | 0 deployable models |
| signal refresh | yes (batch) | yes (pipeline.yml) | yes | n/a | n/a | all below confidence floor |
| portfolio | yes | no | yes (rehearsal) | no | yes | no eligible signal reaches it |
| risk | yes | no | yes (rehearsal) | no | yes | same |
| order intent | yes | no | mechanism only | no | yes | same |
| execution | yes | no | mechanism only | no | yes | structurally stopped this phase |
| IBKR session | yes | no | no | **no gateway reachable this session** | yes | deployment dependency |
| IBKR market data | yes | no | no | **no gateway reachable this session** | yes | same |
| reconciliation | yes | no | yes (rehearsal) | no | yes | no gateway |
| order submission | yes (structurally disabled in 25.9E) | no | no | no | yes (trap-tested) | **intentional Phase 25.9E stop** |
| fills | yes | no | mechanism only | no | yes | no submitted orders |
| positions | yes | no | mechanism only | no | yes | same |
| P&L | yes | no | mechanism only | no | yes | same |
| trade outcome | yes | no | mechanism only | no | yes | same |

## Required Failure Matrix (§98)

| Failure | Expected behavior | Verified? |
|---|---|---|
| market closed | session refuses to start / no new decision | yes |
| stale quote | no submission, blocked | yes |
| missing quote | no submission, blocked | yes |
| IBKR disconnect | ages to stale; broker_usable=False; blocked | yes |
| session expiry | re-read every cycle; blocks on lapse | yes |
| duplicate runner | second refused (lease) | yes |
| DB lock | measured under 4-way contention, worst single-op wait 4.05s, zero lock errors over 20s | yes |
| no model | NORMAL_NO_TRADE, clean | yes |
| no signal | NORMAL_NO_TRADE, clean | yes |
| risk reject | no order intent reaches submission, reason recorded | yes |
| stale account | blocked (900s bound) | yes |
| reconciliation mismatch | blocked, baseline untouched | yes |
| duplicate intent | refused, 0 additional submissions | yes |
| crash before submission | write-ahead absent → nothing sent | yes |
| crash after hypothetical broker accept | write-ahead present → restart does not resubmit | yes |
| duplicate fill | no double position/P&L | yes |
| partial fill | correct running average, no overfill | yes |
| forbidden LIVE | `TradingModeRefused`, cannot be recorded | yes |
| submit during 25.9E | `BrokerSubmissionForbidden`, trap fires, 0 place_calls | yes |

## Required Real-IBKR Matrix (§99)

See §AF verbatim (identical content, restated here per the required section list): session auth CODE ONLY; heartbeat/reconnect/account/balances/positions/contract resolution/quote/market-data freshness/open-order read/reconciliation/restart/client-order-id mapping all MOCK VERIFIED; submit order/broker ACK/cancel NOT REAL-VERIFIED (submit and ACK structurally prevented, cancel not exercised); partial fill/full fill/position-from-fill/trade outcome MOCK VERIFIED only.

---

## Final Status

```
PHASE 25.9E STATUS:                COMPLETE

SESSION RUNNER:                    PASS
REAL WALL CLOCK:                   PASS
MARKET CALENDAR:                   PASS
SCHEDULING:                        NOT VERIFIED
DUPLICATE RUNNER PROTECTION:       PASS
CRASH RECOVERY:                    PASS

OPERATIONAL MARKET DATA:           PASS
REAL IBKR MARKET DATA:             UNVERIFIED
PRICE FRESHNESS:                   PASS
1-MINUTE BARS:                     PASS
INTRADAY FEATURES:                 PARTIAL

MODEL GATE:                        PASS
DEPLOYABLE MODEL:                  NO
SIGNAL ELIGIBILITY:                NOT READY

PORTFOLIO:                         PASS
RISK:                              PASS
ORDER INTENT:                      PASS
IDEMPOTENCY:                       PASS
RECONCILIATION:                    PASS

EXECUTION PRE-SUBMISSION:          READY
HEALTHY FIXTURE RESULT:            READY_TO_SUBMIT

REAL IBKR SESSION:                 UNVERIFIED
REAL IBKR ORDER:                   NOT ATTEMPTED
BROKER SUBMISSION DURING 25.9E:    BLOCKED

PAPER TRADING:                     NOT ATTEMPTED
LIVE TRADING:                      DISABLED

FULL TEST SUITE:                   PASS (4,153 OK, 1 skipped)
LIVE SAFETY:                       PASS (16/16, negative control verified)

D20 RESULT:                        UNSEEN
D20 TEST:                          NOT EXECUTED
D20 CONSUMPTION:                   UNCONSUMED

CRITICAL FINDINGS OPEN:            0
HIGH FINDINGS OPEN:                0

PHASE 25.95:                       NOT READY
PHASE 26:                          NOT READY

NEXT REQUIRED STEP:                Model quality — qualify at least one
                                    model to ACTIVE with a signal that
                                    clears the 0.40 confidence floor.
                                    Separately and only afterward:
                                    establish a real, authenticated IBKR
                                    paper session for Phase 25.95's
                                    read-only rehearsal.
```
