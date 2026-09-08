# Phase 25 — The IBKR paper-trading operating loop

How a signal becomes a reconciled position, and every place the system
refuses to let it.

**PAPER = ENABLED. LIVE = DISABLED.**

---

## The joint that was missing

Every layer this phase drives already existed and was tested:

| Concern | Owner | Since |
|---|---|---|
| sizing and risk | `PortfolioService.evaluate` | Phase 11 |
| backtest | `BacktestEngine` | Phase 12 |
| simulated paper session | `PaperTradingSession` | Phase 13 |
| order lifecycle, validation, limits | `ExecutionOrchestrator` | Phase 14 |
| IBKR | `IBKRGateway` | Phase 15 |
| governance, trade outcomes | `GovernanceRepository` | Phase 16 |
| **risk → execution** | **`intake.from_decision`** | **Phase 17** |
| model deployability | `modeling.selection` | Phase 18 |
| outcome, attribution, memory | Phases 19–21 | |

`src/execution/intake.py` was written in Phase 17 specifically to close
the joint between an approved `RiskDecision` and an `IntentRequest`.
Until this phase it had **zero callers** — both CLIs mentioned it in a
help string and built their requests by hand from a flag literally
named `--assume-risk-approved`.

Phase 25 is its first caller. That single change is most of what turns
a set of subsystems into a system.

---

## One cycle

```
        ┌─────────── OBSERVE (always) ──────────┐
 mode → health/account → poll → fills → positions
        → reconcile → P&L → outcomes
        └───────────────────────────────────────┘
                          │
        ┌─────────── DECIDE (may stop early) ───┐
        market data → signals → eligibility
        → portfolio → risk → targets → intents
        → submission
        └───────────────────────────────────────┘
                          │
                       persist
```

**Observe runs first, and it matters.** The fills a cycle is about were
created by a *previous* one. Deciding against a book that has not
absorbed them double-counts: the position is already held at the venue
*and* the order that established it still reads as working, so the
arithmetic sees the target met twice and proposes a trade to undo the
difference. Deciding first produced, on the second cycle of the
end-to-end test, a **SELL of 500 shares against a target of 499.87 and
a holding of 500**.

**Observe runs regardless.** A cycle that decides nothing still has to
poll, fill, reconcile and record — otherwise a filled order stays
invisible until the loop happens to want another trade in the same
instrument, and a paper account looks flat while holding 500 shares.

---

## Idempotency is structural

```
cycle_anchor(now)  ──►  decision_id  ──►  intent_id  ──►  idempotency_key
   (Phase 25)          (Phase 11)        (Phase 11)        (Phase 14)
```

Every arrow is a hash of the one before it. Phase 11 derives
`decision_id` from `as_of`; feed it `datetime.now()` and a retried
workflow mints a new decision, a new intent and a second order for the
same intention. Feed it a **quantized anchor** and the retry recomputes
identical ids all the way down, and the orchestrator recognises its own
previous work.

`cycle_id_for(session, anchor)` is deterministic for the same reason: a
retried GitHub Actions job computes the same id, finds the row already
terminal, and changes nothing.

Nothing in the loop compares timestamps or counts attempts. §10 lists
seven ways the system must survive being run twice; all seven reduce to
the anchor plus the atomic claim.

---

## The three states that never merge

```
 what we WANT        TargetPosition     position_targets
 what we ASKED FOR   ExecutionOrder     execution_orders
 what we HOLD        ActualPosition     position_actuals   (broker only)
```

Two tables, not one with a `kind` column — a query that forgets the
filter would report intentions as holdings, and that specific mistake
is what §16 exists to prevent. `position_actuals` also carries an
`origin`, and only `broker_reconciled` may be displayed as a holding;
the dashboard filters on it **inside the SQL** so a reader cannot
forget.

`PositionDelta` carries all four numbers:

```
outstanding = target − actual − pending
```

`pending` is subtracted from **both** the broker's open orders and our
own working ones, taking the larger magnitude. A broker that answers
"no open orders" — a fresh session, a reconnect, an API hiccup — would
otherwise make the loop re-place a trade it has already placed. The two
failure modes are not symmetric: over-counting delays a trade by one
cycle, under-counting doubles a position.

---

## Fail closed, and specifically

`CycleResult.mode` starts OFF and `health` starts BLOCKED. Every stage
that cannot establish its precondition records a `Block` with a named
reason and returns.

| Condition | Block reason |
|---|---|
| mode not PAPER | `mode_not_permitted` |
| kill switch | `kill_switch` |
| no / stale market data | `no_market_data`, `stale_market_data` |
| stale signal | `stale_signal` |
| model not promoted, session not experimental | `model_not_deployable` |
| account unreadable | `account_state_unknown` |
| gateway not connected | `broker_disconnected` |
| ordering not enabled | `broker_unhealthy` |
| reconciliation raised | `reconciliation_failed` |
| discrepancies stand | `reconciliation_unresolved` |
| cycle held by another worker | `cycle_already_running` |
| configuration changed mid-session | `configuration_changed` |

`HealthReport.overall` is the **worst** reading, not an average. A
component nobody measured shows in `unmeasured()` rather than counting
as healthy.

---

## LIVE is declared and unreachable

Four independent refusals, each tested separately:

1. `TradingModeStore.set_mode(LIVE)` raises — no live row can be written.
2. `TradingMode.resolve("live")` returns OFF **by name**, with the
   reason "live trading is blocked in this phase" — so a row from a
   restored backup or a hand-edited database is refused at read too.
3. `IBKRConfig.__post_init__` raises on a non-paper environment.
4. `ExecutionSafety.assert_not_real_money` raises before anything runs.

`TradingMode.LIVE` exists as a member on purpose. Deleting it would not
make live trading harder — it would make a stored `"live"` resolve
through the generic *unknown* branch instead of the specific refusal,
and the log would stop saying which boundary was hit.

---

## The kill switch is now durable

Phase 14 built `ExecutionSafety`, which enforces the switch correctly.
Its state lived in `SafetySwitches.emergency_stop` — an in-memory field
on an object every script constructed fresh. `execution_controls` was
created to hold it and **nothing ever wrote to it**; a grep for
`save_control` found the definition and no caller. An operator who
stopped trading stopped it until the next process started.

Phase 25 stores it in `trading_mode` (one row, enforced by a CHECK on a
constant primary key) with an append-only `trading_mode_history`, and
`apply_to_safety()` loads it into the Phase 14 enforcer at the start of
every cycle. **Phase 14 still enforces; Phase 25 only persists.** There
is no second enforcement path.

---

## Model governance is labelled, not bypassed

§37 says paper trading is not a loophole around Phase 18. On this
database **no model has been promoted** — `model_promotions` does not
exist in production — so a strict reading would mean the loop can never
place an order, and a loose one would place orders as if governance had
approved something.

The honest third answer: a signal from an unpromoted model is marked
`experimental=True` on its eligibility row, and a session that has not
declared itself experimental **refuses it** (`model_not_deployable`).
The label travels onto every row, so §37 is answerable per signal
rather than per session.

---

## What one production cycle actually reports

Run on the live database on 2026-09-08:

```
signals              414 seen, 4 eligible
eligibility          398 suppressed · 7 expired · 5 stale · 4 superseded
portfolio            0 of 4 eligible signals sized;
                     4 dropped by the sizing layer
                     (confidence floor 0.40; dropped carry 0.30 ×4)
risk                 approved: no changes proposed
targets              0
orders               0
```

Two findings, both quantified and neither invented:

* **`min_signal_confidence` is 0.40** (a hard Phase 11 constraint,
  deliberately stricter than Phase 10's 0.25 generation floor) and
  every production signal carries **0.30**. No signal the current model
  produces may add exposure. That is a governance decision working.
* **A signal's information is already ~39 h old when the signal is
  created** (median over the nine active signals). With the default 48 h
  information-age policy the tradeable window is about **nine hours**
  wide, so a pipeline running three times daily catches it and one
  running every two days never will.

Signals dropped by Phase 11's *own* sizing gate used to vanish between
"eligible" and "no changes proposed". The portfolio stage now names
them and the floor they missed — §14's rule applied to the gate after
the loop's own.

---

## What is reused, not rebuilt

| Concern | Owner |
|---|---|
| position sizing, risk verdict, intents | Phase 11 |
| approved decision → execution request | Phase 17 |
| pre-trade validation, limits, state machine | Phase 14 |
| broker reconciliation, unknown-order resolution | Phase 14 |
| IBKR transport, contracts, quotes, fills | Phase 15 |
| trade outcome, lineage, execution quality | Phase 16 |
| model deployability | Phase 18 |
| error attribution | Phase 20 |
| challenger approval for paper | Phase 24 |
| atomic queue claim, stale reclaim | Phase 23.5 |

There is no second risk engine, sizing rule, order lifecycle,
reconciler or outcome model in `src/trading`. A boundary test counts
every table in the database before and after a cycle and asserts that
only the fourteen Phase 25 tables plus the ones these owners write have
moved.

---

## Three gaps this phase closed in earlier code

**`EventProcessor.process` accepted `fills_by_event` and nothing could
build one.** A gateway reports order *status* through `poll_events` and
*executions* through `collect_fills`, and pairing them needs both halves
at once — which no component held. Every real poll delivered an
ORDER_FILLED event with no fill attached, the processor correctly
refused to believe a filled status the fills did not support, and the
order landed in `RECONCILIATION_REQUIRED`. Fixed by
`events.pair_fills()` and `ExecutionOrchestrator.poll_broker()`;
`drain_events` is unchanged for callers that only want status.

**`attribution/pipeline.run()` passed `position=None, risk_decision=None,
fill=None, portfolio=None` as literals**, with a comment saying they
were absent by construction. That was true when it was written and
stopped being true the moment the loop produced its first fill. Three
loaders now join through `trade_outcomes` on `signal_id`; a signal that
never traded still yields None and the detector still names the missing
input.

**`PositionSource` had no `BROKER` member**, with a note saying an enum
value nothing can generate is a promise that cannot be kept. Phase 25
can generate them, so the member exists and is used only for holdings a
broker reported and reconciliation agreed with.

---

## Files

| File | Lines |
|---|---|
| `src/domain/trading_loop_models.py` | 1,402 |
| `src/trading/loop.py` | 1,083 |
| `src/trading/repository.py` | 701 |
| `src/trading/validation.py` | 452 |
| `src/data_access/trading_loop_schema.py` | 446 |
| `scripts/run_trading_loop.py` | 411 |
| `src/trading/targets.py` | 370 |
| `src/trading/outcomes.py` | 365 |
| `src/trading/eligibility.py` | 356 |
| `src/trading/accounts.py` | 321 |
| `src/trading/api.py` | 321 |
| `src/trading/mode.py` | 300 |
| `src/trading/stack.py` | 213 |
| tests | 2,008 (136 tests) |

---

## What this phase cannot do

- place a real-money order — four independent refusals, no flag
- promote a model, activate a challenger, raise a risk limit or change capital
- reach `LIVE_ELIGIBLE` — no transition, no function, no configuration
- write `portfolios`, `positions`, `signals`, `predictions` or `trained_models`
- paper-trade a challenger Phase 24 did not mark `PAPER_CANDIDATE`
- trade on an unreconciled position, a stale price or an unknown account
- read a credential — `mode.py` is the only file that touches the
  environment, and it reads one variable that cannot grant permission
  the database has withheld
