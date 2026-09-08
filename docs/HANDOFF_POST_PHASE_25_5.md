# PROJECT HANDOFF — POST PHASE 25.5

**Repository** https://github.com/mihneapetrustefan-dotcom/MarketLens
**Deployed dashboard** https://mihneapetrustefan-dotcom.github.io/MarketLens/
**Local working directory** `C:\Users\GeorgetaS\Documents\marketlens\claude code`
**Handoff written** 2026-09-08, at commit `f07636d`

Everything in this document was checked against the repository at that
commit. Claims are marked **VERIFIED** (measured or read from the code
just now) or **UNVERIFIED** (not established here). Where a thing was
tested only against a double, it says so.

---

## 1. Executive Summary

The project is a financial intelligence, quantitative research,
portfolio/risk and automated-trading platform. Its long-term objective
is a controlled autonomous trading system operating through
**Interactive Brokers**.

Target architecture, with current state per stage:

| Stage | State |
|---|---|
| DATA | **operational in production** |
| INTELLIGENCE (news, entities, events, fusion) | **operational in production** |
| QUANT (impact, event studies, features) | **operational in production** |
| MODELS | **operational**; no model has ever been *promoted* |
| SIGNALS | **operational in production** — 414 rows, 9 active |
| ELIGIBILITY | code-level, exercised only in the loop |
| PORTFOLIO | code-level; **never run on production data** |
| RISK | code-level; **never run on production data** |
| ORDER INTENT | code-level |
| EXECUTION | code-level |
| IBKR | adapter implemented; **real venue never contacted** |
| OUTCOMES (signal) | **operational in production** — 9,233 measurements |
| OUTCOMES (trade) | code-level |
| ERROR ATTRIBUTION | **operational in production** — 10,661 rows |
| MEMORY | code-level; tables absent from production |
| EXPERIMENTS | code-level; tables absent from production |
| AUTONOMOUS RESEARCH | code-level; tables absent from production |
| CHALLENGERS | code-level; tables absent from production |
| PAPER | code-level, verified against the mock |
| SHADOW | **future — Phase 26** |
| CONTROLLED LIVE | **future** |
| CONTINUOUS LEARNING | **future** |

**Just completed:** Phase 25.5 — full paper-trading audit and
pre-shadow gate.

**Verified:** the whole chain signal → eligibility → portfolio → risk
→ order intent → execution → IBKR → fill → position → reconciliation →
P&L → outcome → attribution, end to end, **against the deterministic
mock transport**.

**Not verified:** anything against a real IBKR paper account. No
gateway exists in this environment.

**Next step:** one real IBKR paper validation session, run locally.
See section 23.

---

## 2. Current Status

```
CURRENT PHASE:    Phase 25.5 — COMPLETED
CURRENT VERDICT:  READY FOR PHASE 26 WITH EXPLICIT CONDITIONS
```

The condition, stated exactly as the audit report states it:

> **READY FOR CODE-LEVEL SHADOW PREPARATION, BUT REAL IBKR PAPER
> VALIDATION REMAINS OUTSTANDING.**

**Do not blur these two.** Throughout this project:

- **PASS (mock)** — the code behaved correctly against
  `MockIBKRTransport`, a deterministic double that lives in
  `src/execution/adapters/ibkr/mock_transport.py`.
- **PASS (real IBKR)** — *has never been recorded for anything.*

VERIFIED, by probing this machine on 2026-09-08:

- `IBKR_*` environment variables: **none set**
- TCP listeners on 5000, 5001 (Client Portal), 4001, 4002 (IB Gateway),
  7496, 7497 (TWS): **none**
- HTTPS probe of `localhost:5000/v1/api/iserver/auth/status`: **no
  response**

---

## 3. Phase History

| Phase | Purpose | Key implementation | Status |
|---|---|---|---|
| **0** | Architecture / foundation | repository layout, `src/domain` as the canonical vocabulary | done |
| **1** | Canonical data foundation | `companies`, `securities`, `instruments`, `exchanges`; ticker is not identity | done |
| **2** | News ingestion / historical news | RSS, Finnhub, AlphaVantage collectors; `articles` (47,857) / `news_articles` (49,686) | done; TD-02 split migrated |
| **3** | Entity resolution | `src/entities/`, alias index, `article_entities` (44,500) | done |
| **4** | Event intelligence | `src/events/`, taxonomy, extractor; `events` (1,499) | done |
| **5** | Event fusion / corroboration / event graph | `src/fusion/`; `canonical_events` (1,025), `fusion_decisions` (1,523) | done; `clustering.py` unwired (TD-07) |
| **6** | Market impact intelligence | `src/impact/`, Polygon connector, `price_candle_cache` (129,122) | done |
| **7** | Historical event studies / research dataset | `event_studies` (1,059), `research_observations` (1,059) | done |
| **8** | Quant feature engineering | `src/features/`; `research_features` (34,724), `research_labels` (18,874) | done |
| **9** | Quant modeling / prediction | `src/modeling/`; purged+embargoed walk-forward splitter; `trained_models` (5), `predictions` (947) | done |
| **10** | Signal engine | `src/signals/`; `signals` (414), `signal_suppressions` (493) | done |
| **11** | Portfolio intelligence / risk | `src/portfolio/`; `PortfolioService.evaluate` → `RiskDecision` → `OrderIntent` | code complete, **0 production rows** |
| **12** | Advanced backtesting | `src/backtest/` | code complete, never run on real data |
| **13** | Paper trading / real-time simulation | `src/paper/`, `PaperTradingSession`, `PaperExecutor` (fills against cached bars) | code complete, **0 orders ever**; see TD-04 |
| **14** | Broker / execution foundation | `src/execution/`; orchestrator, validator (23 checks), state machine, `ExecutionSafety` | code complete |
| **15** | Interactive Brokers integration | `src/execution/adapters/ibkr/`; Client Portal Web API transport + mock | adapter complete, **venue never contacted** |
| **16** | IBKR advanced execution / production readiness | `RiskGovernor` (23 limits), `TradingSession`, `trade_outcomes`, four-eyes approval | code complete |
| **17** | Full system audit / reconciliation | found the risk→execution joint broken; added `src/execution/intake.py` | done |
| **17.5** | Post-remediation re-audit | verification gate | done |
| **18** | Model quality gates / signal canonicalization | `src/modeling/selection.py`, `SelectionPolicy.ACTIVE_ONLY`, `promote_model.py` | done |
| **19** | Trade & signal outcome intelligence | `src/outcomes/`; `outcome_measurements` (9,233) | **operational in production** |
| **20** | Error attribution / decision diagnostics | `src/attribution/`; 11 detectors; `error_attributions` (10,661) | **operational in production** |
| **21** | Trading memory / experience intelligence | `src/memory/`; `available_at = outcome.window_end` | code complete, tables absent from production |
| **22** | Experiment engine | `src/experiments/`; hypothesis → experiment → verdict | code complete, tables absent from production |
| **23** | Autonomous research | `src/autoresearch/`; observations → questions → hypotheses → candidates | code complete, tables absent from production |
| **23.5** | Autonomous research audit / remediation | found a stale cache served as current research; a failing live-safety audit | done |
| **24** | Challenger models / strategy variants | `src/challengers/`; six-dimension scorecard, **no total**, `PAPER_CANDIDATE` is the ceiling | code complete, tables absent from production |
| **25** | IBKR paper trading / controlled strategy validation | `src/trading/`; the operating loop; first caller of `intake.from_decision` | code complete, mock-verified |
| **25.5** | Full paper trading audit / pre-shadow gate | 10 findings, 5 HIGH, all remediated | **COMPLETED — current** |

---

## 4. Current Architecture

### Package map (VERIFIED against `src/`)

| Layer | Package | Primary types / services | Tables written |
|---|---|---|---|
| DATA | `src/data_access/`, `src/news/` | repositories, `initialize_*_schema` | all |
| INTELLIGENCE | `src/entities/`, `src/events/`, `src/fusion/` | `EntityResolver`, `EventExtractor`, `FusionEngine` | `events`, `canonical_events`, `fusion_*` |
| QUANT | `src/impact/`, `src/research/`, `src/features/` | `ImpactEngine`, `FeatureEngine` | `event_studies`, `research_features` |
| MODELS | `src/modeling/` | `ModelingEngine`, `selection.select`, `promotion.promote` | `trained_models`, `model_evaluations`, `predictions` |
| SIGNALS | `src/signals/` | `SignalEngine`, `SignalValidator` | `signals`, `signal_suppressions`, `signal_contributions` |
| PORTFOLIO / RISK | `src/portfolio/` | `PortfolioService`, `RiskEngine`, `FixedFractionSizing` | `risk_decisions`, `order_intents`, `allocation_*`, `portfolio_state_snapshots` |
| EXECUTION | `src/execution/` | `ExecutionService`, `ExecutionOrchestrator`, `PreTradeValidator`, `OrderStateMachine`, `BrokerReconciler`, `ExecutionSafety`, `intake.from_decision` | `execution_orders`, `execution_fills`, `order_state_history`, `execution_events`, `reconciliation_records`, `execution_audit` |
| IBKR | `src/execution/adapters/ibkr/` | `IBKRGateway`, `ClientPortalTransport`, `MockIBKRTransport`, `IBKRConfig` | `broker_*` |
| **TRADING LOOP** | **`src/trading/`** | `TradingLoop`, `TradingModeStore`, `EligibilityGate`, `PaperValidator`, `TradingLoopAPI`, `TradingLoopRepository` | the 14 tables in `TRADING_LOOP_TABLES` |
| OUTCOMES | `src/outcomes/`, `src/execution/outcomes.py` | `measurement`, `TradeOutcome`, `classify_errors` | `outcome_measurements`, `trade_outcomes` |
| ATTRIBUTION | `src/attribution/` | 11 detectors, `attribute()`, `pipeline.run()` | `error_attributions`, `attribution_evidence` |
| MEMORY | `src/memory/` | `TradingExperience`, `memory_as_of` | `trading_experiences`, `memory_patterns` |
| EXPERIMENTS | `src/experiments/` | `ExperimentEngine`, evaluators | `experiments`, `experiment_*` |
| AUTORESEARCH | `src/autoresearch/` | `cycle`, `governance` (leakage guard), `queue` | `autoresearch_*` |
| CHALLENGERS | `src/challengers/` | `registry`, `evaluation`, `workflow` | `challengers`, `challenger_*` |
| UI | `src/dashboard.py` (6,900+ lines) | `DashboardGenerator` → `docs/index.html` | none (read-only) |

### Sizes (VERIFIED, `wc -l`)

```
src/domain/trading_loop_models.py     1,433
src/trading/loop.py                   1,283
src/trading/repository.py               701
src/trading/validation.py               452
src/data_access/trading_loop_schema.py  446
scripts/run_trading_loop.py             411
src/trading/outcomes.py                 375
src/trading/targets.py                  370
src/trading/api.py                      363
src/trading/eligibility.py              356
src/trading/accounts.py                 321
src/trading/mode.py                     315
src/trading/stack.py                    213
                            src/trading total: 4,769
tests/trading/ + dashboard test:          2,492
```

### There is no HTTP API, by design

Recorded in `docs/API_AUDIT.md`. No Flask/FastAPI/Django/aiohttp, no
auth, no websockets, no queues, no workers, no migration tool.
Everything is a batch job under GitHub Actions cron; the "API" is a
typed Python facade (`ExecutionService` with a `Caller` permission
object, `TradingLoopAPI` for reads). **Do not add a web framework.**

---

## 5. Trading Loop

`src/trading/loop.py` · `TradingLoop.run_cycle(now, worker)` advances
it exactly once. It is **not a daemon** — the state lives in the
database and a scheduled invocation is a running paper account.

### Cycle order (VERIFIED — `LoopStage` has 17 members)

```
       ┌─────────── OBSERVE (always runs) ──────────┐
mode → health → broker_poll → fills → positions
       → reconciliation → pnl → outcomes
       └────────────────────────────────────────────┘
                            │
       ┌─────────── DECIDE (may stop early) ────────┐
       market_data → signals → eligibility
       → portfolio → risk → targets → intents
       → submission
       └────────────────────────────────────────────┘
                            │
                         persist
```

**Observe runs first, and it matters.** The fills a cycle is about were
created by a *previous* one. Deciding first double-counts: the position
is already held at the venue *and* the order that established it still
reads as pending. Phase 25 shipped it the wrong way round and the
second cycle of the end-to-end test proposed a SELL of 500 against a
target of 499.87 with a holding of 500.

**Observe runs regardless of whether anything is traded.** Folding it
into the decision half made a filled order invisible until the loop
happened to want another trade in the same instrument.

### The chain, with the table each link lands in

```
signal            signals / signal_contributions
  → eligibility   signal_eligibility        (one row per signal, always)
  → portfolio     position_targets, allocation_proposals
  → risk          risk_decisions, risk_violations
  → order intent  order_intents
  → execution     execution_orders, order_state_history
  → IBKR          broker_order_id on the order
  → broker result execution_events
  → fill          execution_fills
  → position      position_actuals          (origin = broker_reconciled)
  → reconciliation reconciliation_records
  → P&L           computed, two sources, never merged
  → outcome       trade_outcomes
  → attribution   error_attributions
  → memory        trading_experiences
```

### Idempotency (VERIFIED)

```
cycle_anchor(now, 900s) → decision_id → intent_id → idempotency_key
     Phase 25              Phase 11      Phase 11      Phase 14
```

Each arrow is a hash of the one before. Phase 11 derives `decision_id`
from `as_of`; a wall-clock anchor would mint a new decision — and a new
order — on every retry. Nothing in the loop compares timestamps or
counts attempts.

`cycle_id_for(session, anchor)` is deterministic, and the cycle row is
taken with an **atomic conditional UPDATE** (`claimed_by = ''`), so two
workers cannot advance the same anchor. `reclaim_stale` releases a
cycle whose worker never returned (default 3600 s).

### Constants (VERIFIED)

| Constant | Value | Where |
|---|---|---|
| `LOOP_METHOD_VERSION` | `phase25-v1` | `trading_loop_models.py:68` |
| `DEFAULT_CYCLE_SECONDS` | 900 | `trading_loop_models.py:75` |
| `MAX_ANCHOR_DRIFT_SECONDS` | 14400 (4 h) | `loop.py:110` |
| `DEFAULT_MAX_SIGNAL_AGE_HOURS` | 48.0 | `eligibility.py:55` |
| `DEFAULT_MAX_PRICE_AGE_DAYS` | 5.0 | `eligibility.py:61` |
| `MIN_PAPER_TRADES` | 30 | `validation.py:62` |
| `LOOP_PORTFOLIO_ID` | `__paper_loop__` | `loop.py` |

`__paper_loop__` is never written to the live `portfolios` table.

---

## 6. Portfolio

### The four numbers

```
outstanding = target − actual − pending
```

| Term | Meaning | Source | Table |
|---|---|---|---|
| **TARGET** | what the portfolio layer decided we should hold. An intention. | Phase 11 `AllocationChange.target_quantity` | `position_targets` |
| **ACTUAL** | what the broker says we hold, after reconciliation. | `IBKRGateway.get_positions` | `position_actuals` |
| **PENDING** | what we have asked for and not yet received | broker open orders **∪** our own non-terminal orders, larger magnitude wins | derived |
| **OUTSTANDING** | what is still to be traded | computed | `position_deltas` |

**TARGET POSITION ≠ ACTUAL BROKER POSITION.** They are two tables, not
one with a `kind` column, because a query that forgets the filter would
report intentions as holdings. `position_actuals.origin` must be
`broker_reconciled` for a row to be shown as a holding; the dashboard
filters on it **inside the SQL** so a reader cannot forget.

### Why PENDING uses both sources

A broker that answers "no open orders" after a reconnect would make the
loop re-place a trade it has already placed. The failure modes are not
symmetric: over-counting delays a trade by one cycle, under-counting
doubles a position. `combined_pending()` in `src/trading/targets.py`.

### Cases, all VERIFIED by probe

| Case | `PositionDelta.action` |
|---|---|
| target +100, held +40 | `increase`, outstanding +60 |
| target met | `noop`, side None |
| target 25, held 0 | `open` |
| target 60, held 100 | `reduce` |
| target 0, held 100 | `close` |
| target −60, held +100 | `reverse` |
| no target at all | `no_target` (**not** zero — different states) |
| gap below venue minimum | `below_minimum`, no order |
| target with no quantity | rejected `no_target_quantity` — sizing belongs to Phase 11 |
| position not reconciled | rejected `position_not_reconciled` |

Quantities are normalised through Phase 14's own
`BrokerInstrumentMapping.normalize_quantity`, which **floors, never
rounds up** — rounding up would trade more than risk approved. A
fractional target of 10.4 against a 1-share increment yields 10.

---

## 7. Risk

### The authoritative boundary

`src/execution/intake.py` · `from_decision(decision, intents, …)` is
the only path from a decision to an execution request. It raises
`RiskNotApproved` **before reading any other argument** and has **no
override parameter**. `LineageIncomplete` refuses a trade whose
provenance is already broken at submission.

**`--assume-risk-approved` was REMOVED in Phase 25.5.** It existed in
`scripts/run_execution.py` and `scripts/run_ibkr.py`, set
`risk_approved=True` with no `RiskDecision` behind it, had no test, and
survived the Phase 17 fix written to eliminate exactly that pattern.
It is replaced by `--decision-id`, which loads a real decision through
`PortfolioRepository.get_decision`, verifies it approves and covers the
instrument, and refuses otherwise.

### Blocking conditions (`BlockReason`, 19 members, VERIFIED)

`mode_not_permitted` · `mode_unknown` · `kill_switch` ·
`no_market_data` · `stale_market_data` · `stale_signal` ·
`model_not_deployable` · `portfolio_state_unknown` ·
`risk_state_unknown` · `broker_unhealthy` · `broker_disconnected` ·
`reconciliation_failed` · `reconciliation_unresolved` ·
`duplicate_order` · `instrument_unresolved` · `account_state_unknown` ·
`session_not_open` · `cycle_already_running` · `configuration_changed`

**A block now actually stops trading.** Until Phase 25.5 `result.block()`
recorded a reason and stopped nothing — only `health is BLOCKED`
prevented submission, so any block raised after the health verdict was
written to the record and ignored. The submission stage now refuses
when any block stands and names which.

Health (`LoopHealth`) is `HEALTHY` / `DEGRADED` / `BLOCKED`, and
`overall` is the **worst** reading, never an average. A component
nobody measured appears in `unmeasured()` rather than counting as
healthy.

### Kill switch

Durable since Phase 25, in `trading_mode` (single row, `CHECK
(singleton = 1)`), with append-only `trading_mode_history`. Phase 14
still *enforces*; `TradingModeStore.apply_to_safety()` loads the stored
value into `ExecutionSafety` at the start of every cycle. There is no
second enforcement path.

---

## 8. Execution

`OrderIntent` (Phase 11, inert, `is_executable=False`) →
`IntentRequest` (Phase 14, via `intake`) → `ExecutionService.submit` →
`ExecutionOrchestrator.execute` → `BrokerGateway.submit_order`.

`submit_order` has exactly **one** caller: `orchestrator._submit`.
`orchestrator.execute` has exactly one caller:
`ExecutionService.submit`. VERIFIED by caller trace.

### Lifecycle (VERIFIED from `order_state_history`)

```
created → validating → approved → submitting → submitted
        → acknowledged → partially_filled → filled
```

Other `ExecutionOrderState` members: `cancel_requested`, `cancelled`,
`rejected`, `expired`, `failed`, `unknown`, `reconciliation_required`.
`is_in_flight` means SUBMITTING or SUBMITTED only — the crash-recovery
window. Do not confuse it with "still working"; the loop uses
`not state.is_terminal` for that.

### Fills

`ExecutionOrchestrator.poll_broker(broker_id, now)` — added in Phase 25
— collects executions, polls events, and pairs them via
`events.pair_fills`. A venue reports **status** and **executions**
through two different calls; an ORDER_FILLED event whose execution has
not been collected is a filled status the fills do not support, and the
processor correctly sends the order to `RECONCILIATION_REQUIRED`.

An execution with no event (**the partial-fill case**) goes through
`_record_unpaired`, which folds the quantity via `apply_fill_to_order`
and then drives the state machine to `PARTIALLY_FILLED` or `FILLED`.
Over-fills are recorded as a `QUANTITY_MISMATCH` finding and **never
applied**.

`drain_events` is unchanged and remains for callers that want status
only.

---

## 9. IBKR

```
SUPPORTED BROKER:  Interactive Brokers — ONLY
NOT IMPLEMENTED:   Trading 212, MetaTrader 5, any second broker
```

There is no multi-broker roadmap and `scripts/audit_live_safety.py`
Q10/Q11 assert that none is planned or stubbed.

### API approach

**IBKR Client Portal Web API** — REST over a locally running Client
Portal Gateway. The gateway holds the session; a human logs into it in
a browser. **No credential passes through this codebase** — there is no
`IBKR_USERNAME` or `IBKR_PASSWORD` variable anywhere, and `.env.example`
says so explicitly.

`src/execution/adapters/ibkr/transport.py` draws a second boundary
inside the adapter: `IBKRTransport` is the interface,
`ClientPortalTransport` the real one, `MockIBKRTransport` the double.
If the TWS socket API is ever needed it implements `IBKRTransport` and
nothing above changes.

### Configuration (VERIFIED — `config.py` lines 49–73, `.env.example`)

| Variable | Default | Notes |
|---|---|---|
| `IBKR_ENABLED` | `false` | first gate |
| `IBKR_ENVIRONMENT` | `paper` | **only** `paper` is accepted; anything else raises `IBKRConfigurationError` at construction |
| `IBKR_ACCOUNT_ID` | — | paper accounts begin `DU`; live begin `U` |
| `IBKR_HOST` | `localhost` | |
| `IBKR_PORT` | `5000` | |
| `IBKR_BASE_PATH` | `/v1/api` | |
| `IBKR_VERIFY_TLS` | `false` | the gateway uses a self-signed certificate |
| `IBKR_TIMEOUT_SECONDS` | `15` | |
| `IBKR_RECONNECT_ENABLED` | `true` | |
| `IBKR_MAX_RETRIES` | `5` | |
| `IBKR_MAX_REQUESTS_PER_MINUTE` | `50` | |
| `IBKR_PAPER_ORDERING_ENABLED` | `false` | **second gate** — connecting is not permission to trade |

### What the adapter implements

authentication status, connection lifecycle with bounded retries,
heartbeat/keepalive, account discovery, account summary, cash and
buying power, positions, contract search and resolution, market
snapshots, order submission, confirmation replies, order status, live
orders, executions/fills, cancellation, reconciliation views, and
restore of known orders and seen executions.

`gateway.heartbeat()` had **no caller** until Phase 25.5; the loop now
beats the session at the start of every cycle and records the result.

### What remains UNKNOWN because the venue was never contacted

1. **Duplicate `cOID` rejection.** After a crash between submission and
   local persistence the last defence is IBKR rejecting a repeated
   client order id. The mock does not model this. *(A-13)*
2. **Portfolio endpoint lag.** The mock updates positions
   synchronously on fill; IBKR's endpoint lags. A lagging endpoint
   would show a stale position for one cycle and reconciliation would
   flag it — the safe direction, unverified.
3. **Session expiry.** The mock never expires.
4. **Status vocabulary.** The mock emits `Submitted`/`Filled`; IBKR
   also emits `PreSubmitted`, `PendingSubmit`, `Inactive`. Unmapped
   values become `UNKNOWN`, which the validator treats as not
   tradeable.
5. **Confirmation prompts.** IBKR replies with a message id requiring
   confirmation for some orders. Handled by `_answer_confirmations`;
   exercised in the mock only when the flag is set.

---

## 10. Paper Trading

```
PAPER = ENABLED
LIVE  = DISABLED
```

- **Paper code-level path: VERIFIED** — 18 ordered assertions in
  `tests/trading/test_end_to_end_paper.py` walk one trade from the
  signal table to `trade_lineage.complete = TRUE`.
- **Real IBKR paper venue: UNVERIFIED.**

### What a real session must still prove

Per §8 of the Phase 25.5 spec: gateway startup, authentication, account
identification, account summary, buying power/cash, market data,
contract resolution, order creation, broker submission, broker
acknowledgement, order status, fill/execution, position update,
reconciliation, cancellation, reconnect, restart recovery, duplicate
protection.

### Note on Phase 13

`src/paper/` (Phase 13) is a **separate, simulated** paper path whose
fills come from cached bars. It cannot reach a broker. Phase 25
deliberately did **not** extend it — that would have been the
"parallel paper-only shortcut" the spec forbids. See TD-04.

---

## 11. Model / Signal Governance

### The gate

Phase 18 owns deployability: `src/modeling/selection.py`,
`SelectionPolicy.ACTIVE_ONLY` (the default everywhere), `select()`
raises `NoValidatedModel` rather than falling back. Promotion requires
`scripts/promote_model.py --approved-by …`.

VERIFIED: **no model has ever been promoted.** `model_promotions` does
not exist in the production database.

### How the loop handles that (§37 of the Phase 25 spec)

A signal from an unpromoted model is marked `experimental = True` on
its `signal_eligibility` row. A session that has **not** declared
itself experimental refuses it with code `model_not_deployable`.
`--experimental` on the CLI is what declares it, and the label travels
onto every row — so the question "was this trade governed?" is
answerable per signal, not per session.

Since Phase 25.5, a model gate that **cannot be read** (an exception
from `candidates()`) is reported and blocks, instead of silently
returning an empty dict indistinguishable from "nothing promoted".

### `EligibilityCode` — 18 members (VERIFIED)

`eligible` is the only one that permits a decision. The others:
`not_active`, `no_direction`, `expired`, `not_yet_valid`, `stale`,
`suppressed`, `instrument_unsupported`, `instrument_unresolved`,
`no_price`, `stale_price`, `model_not_deployable`, `strategy_disabled`,
`below_confidence_policy`, `below_strength_policy`,
`conflicting_open_order`, `duplicate_intent`, `instrument_paused`.

**Every signal the loop sees gets exactly one row**, including the
eligible ones — which is what makes the signal-to-trade conversion
computable.

### The confidence floor — IMPORTANT, and NOT a bug

VERIFIED at `src/portfolio/constraints.py:150`:

```
constraint_id = "min_signal_confidence"
severity      = HARD
min_value     = 0.40
description   = "…Deliberately stricter than Phase 10's 0.25
                 generation floor: being worth recording and being
                 worth money are different bars."
```

VERIFIED on the production record: all nine active signals carry
confidence **0.30**. Phase 11's sizing layer therefore drops every one
of them, the risk decision reads "no changes proposed", and the loop
places nothing.

**This is a governance decision working, not a defect.** Neither the
Phase 25 report nor the Phase 25.5 audit classifies it as a bug.
Whether 0.40 is the right number is a risk decision for a named person
— it is **not** something to lower in order to produce trades.

---

## 12. Pipeline / Scheduling

### Actual cron, VERIFIED from the workflow files

```
Sun 02:00 UTC ─┐
Wed 02:00 UTC ─┴─ pipeline.yml — ONE job, 16 stages back to back:
                  entities → events → fusion → prices → studies →
                  observations → features → train → predict →
                  SIGNALS → outcomes → attribution → memory →
                  experiments → research → dashboard

13:00 / 16:30 / 21:15 UTC daily ─ daily.yml + archive_articles.yml
                  news ingestion, archiving, size reduction.
                  NO signal generation.

(no active schedule) ─ run_trading_loop.yml  — manual dispatch only
(no active schedule) ─ run_paper_session.yml — manual dispatch only
```

`pipeline.yml` crons are at lines 54–55. The `schedule:` blocks in both
trading workflows are commented out (lines 79 and 87 respectively) —
deliberately, because a scheduled run must write its state back to the
release asset or every invocation restarts from the downloaded
snapshot.

### The freshness incompatibility — OPEN, an operational decision

VERIFIED measurement on the production record: the median gap between a
signal's `source_information_cutoff` and its `created_at` is **39.0
hours** (n = 9 active signals, range 20.5–64.9 h).

Against the default 48-hour information-age policy the tradeable window
is therefore roughly **9 hours wide, twice a week** — about 18 of every
168 hours — and the loop is not scheduled inside it.

**Audit verdict: OPERATIONALLY INEFFICIENT, not broken.** The
suppression is correct; a signal built on four-day-old information
*should* be refused. **The threshold was deliberately not changed to
produce trades.** It is now an explicit, versioned field
(`EligibilityPolicy.max_signal_age_hours`, CLI
`--max-signal-age-hours`) recorded in the session fingerprint.

Two defensible resolutions, neither taken:

1. run the signal pipeline more often, or
2. set the age policy knowing the ~39 h feature lag.

**Do not silently pick one.** It needs an operator decision.

### Can GitHub Actions reach a real IBKR gateway?

**No.** VERIFIED: `run_trading_loop.yml` line 129 hard-codes `--mock`
into the command (it is not an input), line 95 pins
`IBKR_ENVIRONMENT: paper`, and the runner has no Client Portal Gateway.
If a future developer deleted `--mock`, the loop would attempt
`localhost:5000`, fail to connect, and **block** — it fails closed even
with the guard removed.

---

## 13. Outcome / Error / Memory

```
execution_fills → TradeOutcome → error_attributions → trading_experiences
```

### TradeOutcome

Built by `src/trading/outcomes.py` using **Phase 16's own**
constructors: `lineage_from_order`, `quality_from_order`,
`classify_errors`. Phase 25 added the producer, not a second
definition. `outcome_id_for(order)` is deterministic on
`order_id|intent_id`, so re-deriving replaces rather than duplicates.

`is_open` is set from whether the instrument's net position is still
non-zero. An open outcome carries an entry and no exit — a
closed-looking row with a None exit price would become a zero-return
trade in every later aggregate.

### A loss is not an error

`classify_errors` sets exactly three fields that can be established
mechanically: `direction_correct`, `execution_correct`, `data_error`.
Nothing in `src/trading/` adds to it. There is **no code path** in
which negative P&L becomes an error classification.

`ErrorType` has three members that are deliberately not errors:
`NO_ERROR`, `EXPECTED_LOSS`, `UNKNOWN`.

### Missing evidence never becomes PASS

`detectors._missing()` returns `fired=False` **with**
`confidence=INSUFFICIENT_EVIDENCE` and the name of the absent table.
The engine handles the aggregate too: when nothing could be judged it
returns UNKNOWN/INSUFFICIENT_EVIDENCE naming every absent input; when
some layers were judged it says how many were assessed.

### Detector evidence (Phase 25.5 wired four of them)

`attribution/pipeline.run()` used to pass `position=None,
risk_decision=None, fill=None, portfolio=None` as **literals**. Three
loaders now join through `trade_outcomes` on `signal_id`:

| Detector | State |
|---|---|
| `detect_execution_error` | **sees fills** — slippage vs decision price |
| `detect_sizing_error` | **sees positions** — quantity vs risk budget |
| `detect_risk_error` | **sees decisions** — approval vs violated limits |
| `detect_portfolio_error` | **STILL BLIND** — needs a concentration figure against a limit; Phase 25 records exposure without one. Reports `INSUFFICIENT_EVIDENCE`, never NO_ERROR. Classified **UNMEASURED**. *(A-12)* |

The risk budget is now the account equity of **the cycle that placed
the trade**, joined through `trade_lineage`. It used to be the *latest*
equity applied to every trade including old ones — future information
in a diagnosis. *(A-09)*

### Memory

Reads `outcome_measurements` and `error_attributions`, dated by
`available_at = outcome.window_end` — when the experience became
*knowable*, not when it was computed. Execution evidence enters
attribution from fills that necessarily precede the window's close, so
no future information reaches an experience. Memory writes only its own
four tables and can never change what it remembers.

---

## 14. Challenger / Experiment / Autoresearch

### Governance ladder (`PaperStrategyState`, VERIFIED — 7 members)

```
research_candidate → backtest_validated → paper_eligible
  → paper_running → paper_evaluated → human_review → live_eligible
```

Only three transitions are automatic (`AUTOMATIC_TRANSITIONS`):
`research_candidate → backtest_validated`, `paper_eligible →
paper_running`, `paper_running → paper_evaluated`. Every other step
needs a person.

**`live_eligible` is absent from every entry**, refused by
`assert_transition`, refused again by `PaperValidator.review`, and
asserted absent by the integrity check `nothing_reached_live_eligible`.

### Entry into paper

A challenger may enter paper **only** from Phase 24's
`PAPER_CANDIDATE`, which itself required a named reviewer and a
recorded reason. Phase 25 reads that status and refuses everything
else (`NotEligibleForPaper`); it never writes the `challengers` table.

### Paper validation

`PaperValidation` carries **seven named quality dimensions**
(`model`, `signal`, `portfolio`, `risk`, `execution`, `strategy`,
`operational`) and **no total, no `overall`, no ordering** —
`sorted([card, card])` raises `TypeError`. A `DimensionReading` marked
measured with no value is refused at construction. `compare()` reports
differences and declares no winner. `is_conclusive()` needs 30
completed trades **and** every dimension measured — it returns False on
everything this project can currently produce.

### Confirmed absent

**No automatic promotion to LIVE. No autonomous capital increase. No
automatic risk relaxation. No automatic production strategy
replacement.** `workflow.promote_to_production()` exists in Phase 24
and always refuses — it is there so somebody searching finds an
explicit refusal rather than nothing.

### Contamination (VERIFIED)

`trade_outcomes` is read only by `attribution/pipeline.py`,
`dashboard.py`, `governance_repository.py`, `trading/api.py` and
`trading/loop.py`. **`src/experiments/`, `src/autoresearch/`,
`src/challengers/` and `src/memory/` never read it.** Paper trading
cannot contaminate experiment evaluation, training data, model
promotion or benchmarks.

---

## 15. Dashboard

Static single-page app generated by `src/dashboard.py` into
`docs/index.html`, published on GitHub Pages. No server, no fetch, no
XHR, no WebSocket.

**Route:** `#/tradingloop` · sidebar "Bucla de tranzactionare" under
"Portofoliu". Payload key `D.tradingloop`, collector
`DashboardGenerator._collect_trading_loop`.

Shows: trading mode and its reason, kill switch, method version,
conversion counters (signals seen / eligible / orders / fills /
outcomes / blocked cycles), broker account **with the source of every
figure**, holdings, target-vs-actual, why signals did not trade
(every eligibility code including refusals), orders with their
lifecycle state, the lineage chain, cycle history with block reasons,
and paper validations with their unmeasured dimensions.

**Deliberately absent:** fake data, fake positions, a trade button,
any client-side execution, any live-execution UI. The page is a reader.
It carries a permanent banner stating everything on it is PAPER and
that live trading is blocked.

Three properties are tested rather than described: positions are
filtered on `origin = 'broker_reconciled'` **in the SQL**; the mode is
resolved through `TradingModeStore.resolve` (so a stored `"live"`
displays as OFF with its reason, and the page cannot drift from the
rule the loop obeys); every eligibility code is collected.

On the production record the page correctly reports **absence** — the
loop has never run there.

---

## 16. Database

SQLite. Production lives in a **GitHub Release asset** (`db-latest`),
not in git. `docs/index.html` is a **committed build artifact**.

### Production snapshot (VERIFIED — asset updated 2026-09-08T17:13:12Z)

```
size    239,157,248 bytes
tables  52
        47 non-empty, 5 empty
empty:  event_corrections, event_instruments, event_sectors,
        ingestion_checkpoints, raw_articles
```

Largest populated tables: `price_candle_cache` 129,122 ·
`news_articles` 49,686 · `articles` 47,857 · `article_entities` 44,500 ·
`recommendations` 39,797 · `research_features` 34,724 ·
`research_labels` 18,874 · `event_study_returns` 11,564 ·
`error_attributions` 10,661 · `outcome_measurements` 9,233 ·
`outcome_aggregates` 3,591 · `fusion_timeline` 2,018 ·
`events`/`canonical_event_reports` 1,499 · `signals` 414 ·
`signal_contributions` 414 · `predictions` 947 · `trained_models` 5.

### CRITICAL DISTINCTION — ABSENT, not empty

VERIFIED: in the production database **all of these tables do not
exist at all**:

```
trading_mode, trading_mode_history, paper_loop_sessions,
trading_cycles, trading_cycle_stages, signal_eligibility,
position_targets, position_actuals, position_deltas,
loop_account_states, trade_lineage, paper_validations,
paper_validation_reviews, trading_loop_audit,
execution_orders, execution_fills, risk_decisions, order_intents,
portfolios, positions, trade_outcomes, trading_experiences,
challengers, experiments
```

**"Portfolio = 0" does NOT mean the portfolio code is broken.** All
schemas are `CREATE TABLE IF NOT EXISTS` created by the script that
owns them on first run. These scripts have never run against the
production database, so the tables were never created. There are three
separate reasons nothing exists there, and they must not be conflated:

1. **table absent** — the owning script never ran on production
   (all of the above);
2. **table present but empty** — the script ran and correctly produced
   nothing;
3. **risk declined** — Phase 11's `min_signal_confidence = 0.40`
   against signals at 0.30 (see section 11).

### Phase 25 tables (14, `TRADING_LOOP_TABLES`)

`trading_mode` (single row, `CHECK (singleton = 1)`, `mode NOT NULL`) ·
`trading_mode_history` (append-only) · `paper_loop_sessions` ·
`trading_cycles` (atomic claim) · `trading_cycle_stages` ·
`signal_eligibility` · `position_targets` · `position_actuals` ·
`position_deltas` · `loop_account_states` · `trade_lineage` ·
`paper_validations` · `paper_validation_reviews` (append-only) ·
`trading_loop_audit` (append-only).

Schema creation is idempotent and additive (VERIFIED: re-running
`initialize_trading_loop_schema` twice is a no-op; all 14 tables
present).

### Lineage joins that work on real records

```
signals.signal_id
  → risk_decisions.decision_id      (via execution_orders.decision_id)
  → order_intents.intent_id
  → execution_orders.order_id
  → execution_fills.order_id
  → position_actuals.instrument_id
  → trade_outcomes.order_id / signal_id
  → trade_lineage.*  (cycle_id, and the model/strategy provenance)
```

---

## 17. Tests

Run exactly as CI does — the `-t .` matters, it puts the repo root on
the path for `scripts.*` imports:

```bash
PYTHONPATH=src python -m unittest discover -s tests -t . -b
```

`unittest` only. **There is no pytest in this project.**

### Counts (VERIFIED by running, 2026-09-08)

| | |
|---|---|
| **Full suite** | **3,851 tests, OK, 1 skipped** (~370–440 s) |
| Phase 25.5 baseline (before remediation) | 3,825, OK, 1 skipped, 156 s |

Per suite:

```
tests/execution      449      tests/paper          314
tests/portfolio      273      tests/trading        146
tests/attribution    134      tests/outcomes       134
tests/autoresearch   132      tests/signals        127
tests/modeling       111      tests/memory          92
tests/challengers     90      tests/experiments     83
tests/pointintime     34
```

`tests/trading/` (146) breaks down as: `test_loop_lifecycle` 47 ·
`test_fail_safe_and_boundary` 31 · `test_audit_25_5` 26 ·
`test_paper_validation` 24 · `test_end_to_end_paper` 18.
Plus `tests/test_dashboard_trading_loop.py` 16.

### Which tests touch a broker

| Suite | Transport |
|---|---|
| `tests/trading/*` | **`MockIBKRTransport`** |
| `tests/execution/ibkr/*` | **`MockIBKRTransport`** |
| `tests/paper/*` | no broker — Phase 13 fills against cached bars |
| everything else | no broker |

**There are NO tests against a real IBKR paper account. None exist and
none should be claimed.** Every layer above the gateway is real code in
these tests; only the venue is a double.

### Other verification tools

```bash
python scripts/audit_live_safety.py --untracked   # 16/16 PASS
python scripts/verify_phases.py                   # 23/23 PASS
python scripts/run_trading_loop.py --integrity    # 14 checks
```

The integrity report distinguishes **passed / failed / could-not-run**
and is `conclusive` only when nothing could-not-run — "a check that
could not run is not a check that passed."

---

## 18. Security / Live Safety

`scripts/audit_live_safety.py --untracked` → **16/16 PASS** (VERIFIED
after the Phase 25.5 changes).

### Four independent live refusals, each separately tested

1. `TradingModeStore.set_mode(LIVE)` raises `TradingModeRefused` — no
   live row can be written.
2. `TradingMode.resolve("live")` returns OFF **by name**, with the
   reason "live trading is blocked in this phase" — so a value from a
   restored backup or a manual UPDATE is refused at read too.
3. `IBKRConfig.__post_init__` raises `IBKRConfigurationError` on any
   non-paper environment.
4. `ExecutionSafety.assert_not_real_money` raises
   `RealMoneyExecutionDisabled` before anything runs.

`ExecutionSafety.allow_real_orders` is a property with no setter and
returns False permanently. Setting `MARKETLENS_ALLOW_REAL_ORDERS=1` is
detected, reported, and never honoured.

### Trading mode semantics (tightened in Phase 25.5)

| Stored | Environment | Result |
|---|---|---|
| paper | (unset) | **PAPER** |
| paper | `paper` | **PAPER** |
| paper | `live` / `off` / anything else | **OFF** — the environment may restrict |
| (absent) | `paper` | **OFF** — the environment cannot *grant* |
| (absent) | (unset) | **OFF** |
| `live` (arrived some other way) | any | **OFF**, named refusal |
| misspelled / blank | any | **OFF**, named refusal |
| `NULL` | any | refused by the schema itself |

Trading requires a **stored** row written by `set_mode`, which demands
an actor and a reason and appends to `trading_mode_history`.

### Credentials

No credential literals anywhere. Every hit in a repository-wide scan is
a parameter named `api_key`/`password` with an `Optional[str] = None`
default read from the environment. The only IBKR account ids in the
tree are `DU0000000` (placeholder) and `DU1234567` (mock) — both `DU`,
IBKR's paper prefix, which cannot be a live account.

`src/trading/mode.py` is the **only** file in the trading package that
reads the environment (asserted by a tokenised scan with a negative
control). The CLI declares no secret argument (asserted by parsing its
`add_argument` calls). `.env` is gitignored; `.env.example` is not.

---

## 19. Remaining Findings

### Fixed in Phase 25.5 — kept here because they explain the code

| # | Sev | Bug | Why tests missed it | Fix | Regression test | Caveat |
|---|---|---|---|---|---|---|
| **A-01** | HIGH | **Partial fills never applied.** `_record_unpaired` called `apply_fill_to_order(order, fill, self.machine, at=now)` — not that function's signature. TypeError inside a generator inside the broker-poll stage; the only path that can apply a partial fill had **never once completed**. | Every test filled its order **completely** and took the paired path. | Correct signature + the state move to `PARTIALLY_FILLED`/`FILLED` that `apply_fill_to_order` does not do. Over-fill recorded, never applied. | `TestPartialFills` (5) | none |
| **A-02** | HIGH | **Model lineage never populated.** `model_version`, `prediction_id`, `trained_model_id`, `strategy_id` were None on every order, lineage row and trade outcome. Phase 16 said `lineage_complete = 0`; Phase 25 said `complete = 1`. | The fixture built signals with **no `ModelContribution`** — no model to lose. Phase 25's own chain did not include the model, so its check could not see the gap. | Per-instrument provenance from Phase 10's records; `intake.from_decision` gained `model_versions`/`strategy_ids`; outcomes key the model on the **signal** (the outcome builder runs before the decision half). Two new integrity checks compare the two lineage models. | `TestProvenanceReachesTheRecord` (7) | a rule-based signal genuinely has no model — reported via `missing_provenance()`, not forced |
| **A-03** | HIGH | **`--assume-risk-approved` bypassed the risk gate** in both CLIs, with no `RiskDecision`. TD-01 recorded the defect FIXED while the flag survived. | **The flag had no test at all.** | Deleted. Replaced by `--decision-id`, which loads a real decision and refuses one that does not approve or does not cover the instrument. | manual probe, both directions | operator must now produce a decision first |
| **A-04** | HIGH | **An environment variable alone enabled PAPER** on a database with no recorded mode — no actor, no reason, no history row. | Never probed. | The environment may only *restrict*. | `TestTheEnvironmentMayOnlyRestrict` (4) | none |
| **A-08** | HIGH | **`result.block()` stopped nothing.** Only `health is BLOCKED` prevented trading, so any block raised after the health verdict was recorded and ignored — the cycle submitted anyway. | No test ever made a component **fail**. | The submission stage refuses when any block stands and names which. Everything before it still runs. | `TestABlockActuallyStopsTrading` (4) | none |
| **A-05** | MED | **No anchor-drift guard.** `run_cycle(now=…)` takes its moment as an argument; a replay would decide on month-old signals and trade at today's venue. | No replay was ever attempted. | Anchors > 4 h behind wall clock block trading and fall back to dry run; the cycle still observes. Limit is configuration, in the session fingerprint. | `TestTheAnchorMustDescribeNow` (3) | the test fixture widens the limit explicitly |
| **A-06** | MED | **Model-gate failures were silent** — two helpers each swallowed every exception and returned `{}`, identical to "nothing promoted"; each called Phase 18 separately. | No test broke the gate. | One method, asked once, returning `(deployable, statuses, detail)`. A missing table is an answer; anything else is a reported failure that blocks. | `TestABlockActuallyStopsTrading` (4) | none |
| **A-09** | MED | **Future information in attribution** — the sizing detector's risk budget was the *latest* equity applied to every trade. | Static mock account made it invisible. | Joined through `trade_lineage` to the account state of the cycle that placed the trade. | `tests/attribution` (134) | none |
| **A-10** | MED | **`gateway.heartbeat()` had no caller**, though it exists because the Client Portal session "lapses when idle". | Never audited for callers. | Beat at the start of every cycle; result on the health stage. | covered by loop tests | effectiveness unverified against the real gateway |
| **A-07** | LOW | `poll_broker` extended `report.fills` from a generator that read `report.fills`. | — | Built into a list first. | — | none |

### OPEN — carried into Phase 26

| # | Sev | Finding | Why it remains | Blocks Phase 26? | Recommended action |
|---|---|---|---|---|---|
| **A-11** | INFO | **Real IBKR paper never contacted.** | No gateway exists in this environment and one cannot be started headlessly — the IBKR session is opened by a human in a browser. | **YES — this is the gate.** | Run the session in section 23. |
| **A-12** | INFO | `detect_portfolio_error` **UNMEASURED** — no per-cycle concentration measure exists. | Building one is Phase 26 work; claiming one now would be a fake metric. | No | Add a concentration measure per cycle in Phase 26. Do **not** make the detector claim PASS. |
| **A-13** | LOW | Duplicate-`cOID` defence after a crash rests on IBKR's behaviour; the mock does not model it. | Cannot be tested without the venue. | No — mitigated: reconciliation blocks the next cycle (VERIFIED). | Verify explicitly during the manual session. |
| **A-14** | LOW | Pipeline cadence (twice weekly) incompatible with the 48 h freshness policy given a ~39 h feature lag. | It is an operator decision, and the threshold was deliberately not tuned. | No | Decide: run the pipeline more often, or set the policy knowingly. |
| **A-15** | LOW | TD-04 — Phase 13's simulated `PaperExecutor` remains beside the loop. | Re-scoped, did not grow: the loop drives the Phase 14 orchestrator and has no lifecycle of its own. | No | When Phase 13 is next touched, port it onto the loop with a simulating gateway. |

### Other open technical debt

`TD-09` six unused feature tables · `TD-10` two import conventions
(accepted) · `TD-18` event confidence spends 10 % of its weight on a
dimension it cannot know. See `docs/TECHNICAL_DEBT_REGISTER.md`.

---

## 20. Git / Working Tree

VERIFIED at handoff time:

```
branch:            main
HEAD:              f07636d  Phase 25.5: five HIGH defects in code a
                            passing suite had certified
working tree:      CLEAN — no modified, no untracked files
vs origin/main:    behind 0, ahead 0  (fully pushed)
```

Recent commits:

```
f07636d  Phase 25.5: five HIGH defects in code a passing suite had certified
864bf02  Merge automat (rezolvare conflict pe fisiere regenerate)      [bot]
8fa2a18  Actualizare automata 2026-09-08 19:34 UTC                     [bot]
54cc857  Actualizare automata 2026-09-08 19:32 UTC                     [bot]
32e95f1  Correct the Phase 13 row: those three sessions were in a local snapshot
4beb43c  Rebuild the published dashboard for the trading-loop workspace
df43016  Phase 25: the loop that finally walks the joint Phase 17 built
```

**All Phase 25 and Phase 25.5 work is committed and pushed.**

Bot commits (`Actualizare automata …`) land frequently — they
regenerate `docs/index.html` and `data/archives/*`. **Always rebase
onto `origin/main` before committing.**

Commit convention: `Co-Authored-By: Claude Opus 5
<noreply@anthropic.com>`.

---

## 21. Important Files

Read in this order:

| Purpose | Path |
|---|---|
| **This handoff** | `docs/HANDOFF_POST_PHASE_25_5.md` |
| **Current audit + verdict** | `docs/PHASE_25_5_AUDIT_REPORT.md` |
| Phase 25 report | `docs/PHASE_25_FINAL_REPORT.md` |
| Phase 25 architecture reference | `docs/PHASE_25_PAPER_TRADING.md` |
| Whole-system architecture | `docs/MASTER_ARCHITECTURE.md` |
| Open debt | `docs/TECHNICAL_DEBT_REGISTER.md` |
| IBKR architecture | `docs/PHASE_15_IBKR_ARCHITECTURE.md` |
| **IBKR operator runbook** | `docs/PHASE_15_IBKR_RUNBOOK.md` |
| Execution operations runbook | `docs/PHASE_16_OPERATIONS_RUNBOOK.md` |
| Execution architecture | `docs/execution-architecture.md` |
| Why there is no HTTP API | `docs/API_AUDIT.md` |
| Database audit | `docs/DATABASE_AUDIT.md` |
| Lineage map | `docs/DATA_LINEAGE_MAP.md` |
| Security audit | `docs/SECURITY_AUDIT.md` |
| Earlier phases | `docs/PHASE_{18..24}_*.md` |

Key source files:

```
src/trading/loop.py                  the cycle
src/trading/mode.py                  durable mode + kill switch
src/trading/targets.py               target / actual / pending / outstanding
src/trading/eligibility.py           a verdict for every signal
src/trading/validation.py            paper validation + the §38 ladder
src/trading/api.py                   read facade + 14 integrity checks
src/domain/trading_loop_models.py    the vocabulary
src/execution/intake.py              THE risk → execution joint
src/execution/orchestrator.py        order lifecycle, poll_broker
src/execution/adapters/ibkr/         the adapter, transport, mock
src/portfolio/constraints.py         min_signal_confidence = 0.40
scripts/run_trading_loop.py          the loop CLI
scripts/run_ibkr.py                  the IBKR operator CLI
scripts/audit_live_safety.py         16 live-safety questions
```

---

## 22. Non-Negotiable Rules

> **NON-NEGOTIABLE ARCHITECTURAL RULES**
>
> 1. **Interactive Brokers is the only broker.**
> 2. **Do not implement MetaTrader 5.**
> 3. **Do not implement Trading 212.**
> 4. **Do not create a multi-broker roadmap.**
> 5. **Never bypass Risk.**
> 6. **Never bypass Execution.**
> 7. **Never confuse TARGET with ACTUAL position.**
> 8. **Never treat LOSS = ERROR.**
> 9. **Never treat missing evidence as PASS.**
> 10. **Never fake IBKR validation.**
> 11. **Never enable live trading.**
> 12. **Never automatically promote challengers to live.**
> 13. **Never automatically increase capital.**
> 14. **Never weaken risk constraints merely to generate trades.**
> 15. **Preserve point-in-time integrity.**
> 16. **Preserve lineage.**
> 17. **Prefer NO TRADE over UNSAFE TRADE.**
> 18. **Prefer UNKNOWN over FALSE SUCCESS.**
> 19. **Prefer UNMEASURED over FAKE METRIC.**
> 20. **Prefer BLOCKED over UNCERTAIN EXECUTION.**

Additional project conventions that are easy to break by accident:

- `unittest` only — **no pytest**.
- No web framework, no server, no auth, no websockets.
- Run tests as `PYTHONPATH=src python -m unittest discover -s tests -t . -b`.
- Bash heredocs break on apostrophes — use the Write tool for patch
  scripts.
- Narrow `except sqlite3.OperationalError` to `"no such table"`; a
  missing *column* must not look like a missing table.
- Scanners must be word-bounded or tokenised — `signals` inside
  `signal_eligibility`, `environ` inside `RunEnvironment`, and `UPDATE`
  inside `updated_at` have all caused false positives here.
- New methodology ⇒ new `method_version`, writing new rows beside the
  old ones. Never destroy historical data.

---

## 23. Exact Next Step

**Run one real IBKR paper validation session, locally.**

### Prerequisites

1. IBKR **Client Portal Gateway** downloaded, running, and logged into
   in a browser (the gateway holds the session; this codebase never
   sees a credential).
2. An IBKR **paper** account — the id begins with `DU`. A live id
   begins with `U` and must never be used.
3. This repository, Python 3.12, `pip install -r requirements.txt`.
4. A local copy of the database at `data/marketlens.db` (download the
   `db-latest` release asset), or pass `--db`.

### Environment

```bash
export IBKR_ENABLED=true
export IBKR_ENVIRONMENT=paper          # the only accepted value
export IBKR_ACCOUNT_ID=DU………           # your paper account
export IBKR_HOST=localhost
export IBKR_PORT=5000
export IBKR_VERIFY_TLS=false
export IBKR_PAPER_ORDERING_ENABLED=true   # the second gate
```

### Commands — quoted from the current scripts, not from memory

```bash
# 0. confirm the gateway answers and the account is PAPER
python scripts/run_ibkr.py --status
python scripts/run_ibkr.py --account-info
```

```bash
# 1. record the durable trading mode (actor and reason are required)
python scripts/run_trading_loop.py --set-mode paper \
    --actor "your.name" --reason "Phase 25.5 real IBKR paper validation"
```

```bash
# 2. read-only state
python scripts/run_trading_loop.py --status
```

```bash
# 3. one safe diagnostic cycle — --dry-run is the DEFAULT
python scripts/run_trading_loop.py --cycles 1 --experimental --verbose
```

```bash
# 4. resolve the instrument at the venue (needed before any order)
python scripts/run_ibkr.py --resolve --symbol AAPL --instrument i-aapl
python scripts/run_ibkr.py --quote --instrument i-aapl
```

```bash
# 5. one cycle that may actually submit to IBKR PAPER
python scripts/run_trading_loop.py --cycles 1 --experimental \
    --allow-paper-orders --no-dry-run --verbose
```

```bash
# 6. integrity, then inspect state
python scripts/run_trading_loop.py --integrity
python scripts/run_ibkr.py --reconcile
python scripts/run_ibkr.py --trace <ORDER_ID>
python scripts/run_trading_loop.py --status
```

**Note on step 5:** on the current production record this will place
**nothing**, because `min_signal_confidence = 0.40` and every signal
carries 0.30 (section 11). That is correct behaviour. To exercise the
venue you must either use `scripts/run_ibkr.py --submit` with a real
`--decision-id` (section 7), or accept that the loop honestly declines.
**Do not lower the constraint.**

Relevant `run_trading_loop.py` flags (VERIFIED, complete list):
`--db --actor --worker --status --integrity --cycles --session --name
--account --cycle-seconds --universe-limit --constraints --strategy
--strategy-version --challenger --mock --allow-paper-orders
--experimental --max-signal-age-hours --min-confidence --min-strength
--dry-run --no-dry-run --verbose --set-mode --kill-switch --reason
--review --review-state --reviewer`

Relevant `run_ibkr.py` flags: `--db --actor --account --mock --status
--account-info --resolve --quote --dry-run-order --submit --reconcile
--resolve-unknown --trace --symbol --instrument --sec-type --currency
--exchange --side --quantity --order-type --time-in-force --limit-price
--stop-price --policy --intent-id --intent-version --strategy
--portfolio --allow-paper-orders --decision-id --as-of --dry-run`

Emergency stop at any time:

```bash
python scripts/run_trading_loop.py --kill-switch on \
    --actor "your.name" --reason "why"
```

---

## 24. Phase 26 Gate

Phase 26 (Shadow Trading) must **not** be treated as fully validated
until all fourteen are recorded against a **real IBKR paper account**:

1. real IBKR paper account contacted
2. account state verified
3. contract resolution verified
4. paper order submitted
5. broker acknowledgement verified
6. fill / order status verified
7. position verified
8. reconciliation verified
9. restart / reconnect behaviour verified
10. duplicate `cOID` behaviour verified or safely mitigated
11. no unexpected risk bypass exists
12. live remains impossible
13. lineage preserved end to end
14. paper outcome recorded

Gate state per the Phase 25.5 audit:

| Requirement | State |
|---|---|
| CRITICAL findings | **0** |
| HIGH findings (after remediation) | **0** — 5 found, 5 fixed |
| live trading blocked | ✅ four independent refusals |
| IBKR paper safety verified | ⚠️ **in code only; venue unverified** |
| risk is a hard gate | ✅ the one bypass deleted |
| reconciliation blocks discrepancies | ✅ |
| target vs actual correct | ✅ |
| idempotency reliable | ✅ five probes |
| crash recovery safe | ✅ explicit UNKNOWN, no duplicate |
| lineage complete | ✅ including the model, cross-checked |
| no fake metrics | ✅ |
| model quality gate active | ✅ |
| paper governance intact | ✅ |
| no automatic live promotion | ✅ |

Until item 1 is done, the standing verdict is:

> **READY FOR CODE-LEVEL SHADOW PREPARATION, BUT REAL IBKR PAPER
> VALIDATION REMAINS OUTSTANDING.**

---

## 25. Bootstrap Instructions for New Claude Chat

On the first message of the new chat:

1. **Read this handoff** (`docs/HANDOFF_POST_PHASE_25_5.md`).
2. **Inspect the actual repository** — do not trust this document over
   the code. It was accurate at `f07636d`; bot commits land often.
3. **Verify git state**: branch, `HEAD`, working tree, distance from
   `origin/main`.
4. **Verify the reported Phase 25.5 status** by running:
   ```bash
   PYTHONPATH=src python -m unittest discover -s tests -t . -b
   python scripts/audit_live_safety.py --untracked
   python scripts/run_trading_loop.py --integrity
   ```
   Expect ~3,851 tests OK with 1 skipped, and 16/16 live safety.
5. **Do not start implementing anything** until the current state is
   confirmed.
6. **Then continue from the exact outstanding step.**

```
NEXT IMMEDIATE TASK:
    REAL IBKR PAPER VALIDATION   (section 23)

AFTER SUCCESSFUL REAL IBKR VALIDATION:
    NEXT DEVELOPMENT PHASE — Phase 26: Shadow Trading
```

**Phase 26 must not begin until the gate in section 24 is satisfied.**
If the real IBKR environment remains unavailable, say so plainly and do
not substitute mock results for venue results.

---

```
HANDOFF STATUS:
    READY FOR TRANSFER

NEXT ACTION:
    REAL IBKR PAPER VALIDATION
```

*Phase 26 is NOT complete. Real IBKR paper validation has NOT
happened. Everything recorded as verified in this document was verified
against the deterministic mock transport unless explicitly stated
otherwise.*
