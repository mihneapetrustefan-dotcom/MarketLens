# Phase 25 — Final report

**IBKR paper trading and controlled strategy validation.**

Date: 2026-09-08 · Method version `phase25-v1` · Tests **3,825 passing**
(from 3,689) · Live-safety audit **16/16** · Loop integrity **12/12**

---

## A. Executive summary

The project can now take a signal, construct a portfolio decision, pass
it through the real risk gate, create an order intent, send it to IBKR
paper, observe the broker's result, reconcile the resulting position,
measure P&L from two independent sources, produce a trade outcome with
full lineage, and hand that outcome to the error-attribution layer.

The end-to-end test walks that chain link by link and its final
assertion reads `trade_lineage.complete`. Nothing stops at "order
submitted".

**On the production record the loop places no order, and the reason is
recorded rather than worked around.** Two facts, both measured today:

1. Phase 11's `min_signal_confidence` is **0.40** — a hard constraint,
   deliberately stricter than Phase 10's 0.25 generation floor. Every
   one of the nine active production signals carries **0.30**. The
   sizing layer drops all of them. That is a governance decision
   working, not a defect.
2. A signal's information cutoff is already a median **39 hours** old
   when the signal is created. Against the default 48-hour
   information-age policy the tradeable window is about **nine hours**
   wide, so a pipeline running three times a day catches it and one
   running every two days never will. The pipeline last generated
   signals two days ago; all 414 are expired, stale, suppressed or
   superseded.

Neither was tuned away. The confidence floor is a risk constraint and
Phase 25 does not change risk constraints; the age policy is now an
explicit, versioned, fingerprinted operator setting rather than a
constant nobody could see.

**Live trading remains blocked by four independent refusals.**

---

## B. What already existed

Everything the loop drives, and none of it was rebuilt:

| Concern | Owner | State on arrival |
|---|---|---|
| sizing, risk verdict, order intents | Phase 11 | correct, 0 rows ever written |
| backtester | Phase 12 | tested, never run on real data |
| simulated paper session | Phase 13 | tables absent from production; 3 sessions and **0 orders** in an older local snapshot |
| order lifecycle, validation, limits | Phase 14 | tested, **0 orders** |
| IBKR adapter + transport + mock | Phase 15 | complete, never connected |
| governance, `trade_outcomes` | Phase 16 | tables absent from production |
| **risk → execution join** | Phase 17 | **zero callers** |
| model deployability gate | Phase 18 | working; nothing promoted |
| outcomes, attribution, memory | Phases 19–21 | 9,233 / 10,661 rows / absent |
| challenger approval for paper | Phase 24 | `PAPER_CANDIDATE` reachable |

The production database held **52 tables, 47 non-empty** — and no
portfolio, execution, paper, memory, experiment, research or challenger
table at all. Pipeline stages 13–16 had never run there.

---

## C. What Phase 25 implemented

**New (`src/trading/`, 4,494 lines + 1,402 of domain vocabulary + 446 of schema):**

- `mode.py` — the durable trading mode and durable kill switch
- `eligibility.py` — a recorded verdict for **every** signal the loop sees
- `targets.py` — current → target → delta → trade quantity
- `accounts.py` — canonical broker account state and the health verdict
- `outcomes.py` — two-sided P&L, and fills → `TradeOutcome`
- `loop.py` — the bounded, restart-safe cycle
- `validation.py` — paper validation records and the §38 ladder
- `repository.py` — persistence and the atomic cycle claim
- `stack.py` — one assembly of the execution stack (was hand-rolled twice)
- `api.py` — read facade and a 12-check integrity report

**Fixed in earlier code — three defects that only a running loop exposes:**

1. **`EventProcessor.process` accepted `fills_by_event` and nothing
   could build one.** A gateway reports order *status* and *executions*
   through two different calls, and pairing them needs both at once —
   which no component held. So every real poll delivered an
   ORDER_FILLED event with no fill attached, the processor correctly
   refused a filled status the fills did not support, and the order
   landed in `RECONCILIATION_REQUIRED`. Added `events.pair_fills()` and
   `ExecutionOrchestrator.poll_broker()`; `drain_events` is unchanged.

2. **`attribution/pipeline.run()` passed `position=None,
   risk_decision=None, fill=None, portfolio=None` as literals.** True
   when written — no order had ever been placed — and false the moment
   the loop produced a fill. Four detectors would have gone on
   reporting "no fill exists" over a database full of fills. Three
   loaders now join through `trade_outcomes` on `signal_id`.

3. **`PositionSource` had no `BROKER` member**, with a note saying an
   enum value nothing can generate is a promise that cannot be kept.
   Phase 25 can generate one, so it exists — used only for holdings a
   broker reported *and* reconciliation agreed with.

---

## D. Database changes

Fourteen new tables, all `CREATE TABLE IF NOT EXISTS` with an additive
column migration:

`trading_mode` · `trading_mode_history` · `paper_loop_sessions` ·
`trading_cycles` · `trading_cycle_stages` · `signal_eligibility` ·
`position_targets` · `position_actuals` · `position_deltas` ·
`loop_account_states` · `trade_lineage` · `paper_validations` ·
`paper_validation_reviews` · `trading_loop_audit`

`trading_mode` holds exactly one row, enforced by a CHECK on a constant
primary key — "the current mode" is never a question about ordering.
`trading_mode_history`, `paper_validation_reviews` and
`trading_loop_audit` are append-only.

**No table was altered.** One enum gained a member
(`PositionSource.BROKER`).

---

## E. Portfolio architecture

`PortfolioService.evaluate(positions=…, cash=…)` — the seam Phase 12
uses for simulated books — is fed the **reconciled broker book**. That
means Phase 11's `current_weight` is what is really held, so
`weight_delta` and therefore the order's side are computed against
reality, and a position that appeared at the broker without a local
order constrains the very next decision.

Three states, three tables, never merged:

```
target − actual − pending = outstanding
```

`pending` comes from the broker's open orders **and** our own working
ones, taking the larger magnitude. A broker that answers "no open
orders" after a reconnect would otherwise make the loop re-place a
trade it has already placed; over-counting delays a trade by one cycle,
under-counting doubles a position.

---

## F. Risk architecture

Risk is a hard gate and Phase 25 adds no second one. The only path from
a decision to an order is `intake.from_decision`, which raises
`RiskNotApproved` before reading any other argument and has no override
parameter. `LineageIncomplete` refuses a trade whose provenance is
already broken at submission.

Signals that pass the loop's own eligibility gate and are then dropped
by Phase 11's *internal* sizing gate used to vanish between "eligible"
and "no changes proposed". The portfolio stage now names them and the
floor they missed — §14's rule applied to the gate after the loop's own.

---

## G. Execution architecture

No order lifecycle exists in `src/trading`. The loop calls
`ExecutionService.submit`, which resolves the environment from the
registered broker (not from the request), asserts it is not real money,
requires the permission for that environment, and hands to the Phase 14
orchestrator. The full state walk is preserved and asserted:

```
validating → approved → submitting → submitted → acknowledged → filled
```

---

## H. IBKR paper integration

Unchanged from Phase 15 except for `poll_broker`. The loop connects
through `IBKRGateway`, and `state.can_submit` — Phase 14's own
predicate — decides whether new exposure may be created, so a DEGRADED
link queries and reconciles but does not trade.

`--mock` runs the entire path against `MockIBKRTransport`: no gateway,
no account, no network. Every other layer is the same object the real
path uses.

**§36, honestly:** no validation against a real IBKR paper account was
performed, because this environment has no Client Portal Gateway and
cannot have one — the IBKR session is opened by a human in a browser.
What the mock proves is that the adapter behaves correctly against
IBKR's shapes, not that IBKR behaves the way the mock does. Closing
that gap needs `scripts/run_trading_loop.py` run locally with the
gateway up, and it is the first item under **S**.

---

## I. Reconciliation

Phase 14's `BrokerReconciler`, called every cycle with our positions and
cash. Mismatches are recorded, never overwritten. Unknown orders are
then *asked about* through `resolve_unknown_orders` (which never
resubmits) before the remainder is reported as unresolved — and an
unresolved discrepancy blocks the next cycle from trading.

An adapter failure inside reconciliation is caught and turned into a
`reconciliation_failed` block. Found by stubbing `get_account` to
raise: the cycle previously died before reaching the health verdict and
recorded **no reason at all**.

---

## J. Order lifecycle and idempotency

```
cycle_anchor(now) → decision_id → intent_id → idempotency_key
   (Phase 25)       (Phase 11)    (Phase 11)     (Phase 14)
```

Every arrow is a hash of the one before. A retried run recomputes
identical ids and the orchestrator recognises its own work. The cycle
row is claimed with a single conditional UPDATE (the Phase 23.5
pattern), and `reclaim_stale` releases a cycle whose worker never
returned — without it a crashed run would block its anchor forever
while every component reported healthy.

`build_stack` always calls `ExecutionRepository.restore()`, because a
fresh process has an empty idempotency index.

---

## K. Outcome / error / memory integration

A filled order produces a `TradeOutcome` through Phase 16's own
constructors (`lineage_from_order`, `quality_from_order`,
`classify_errors`) — all of which had sat unexercised since Phase 16
because no order existed. Phase 25 adds the producer, not a second
definition. `classify_errors` sets exactly three mechanical fields and
Phase 25 adds nothing to it; there is no code here that could turn a
loss into an error.

With `trade_outcomes` populated, the four blind attribution detectors
can see:

| Detector | Was | Now |
|---|---|---|
| `detect_execution_error` | "no fill exists" | slippage vs decision price |
| `detect_sizing_error` | "no position record exists" | quantity vs risk budget |
| `detect_risk_error` | "no risk decision record exists" | approval vs violated limits |
| `detect_portfolio_error` | "no portfolio record exists" | **still blind** — no per-cycle concentration measure exists, and the detector says so |

Memory (Phase 21) reads outcomes and attributions and therefore
inherits the execution evidence. Nothing in Phase 25 writes a memory
table.

---

## L. Challenger paper validation

Phase 24's `PAPER_CANDIDATE` — which already required a named reviewer
and a recorded reason — is the only status from which a challenger may
enter paper. Phase 25 reads it and refuses everything else; it adds no
challenger state and never writes the `challengers` table.

`PaperValidation` has **seven named quality dimensions and no total, no
`overall`, no ordering** — `sorted([card, card])` raises `TypeError`.
An unmeasured dimension defaults to `measured=False` and a reading
marked measured with no value is refused at construction, which is
exactly the combination that turned an unmeasured dimension into a
passing one in Phase 24.

`compare()` reports differences and declares no winner.

`LIVE_ELIGIBLE` is absent from every entry in `AUTOMATIC_TRANSITIONS`,
refused by `assert_transition`, refused again by `review()`, and
asserted absent by the integrity check.

---

## M. Dashboard

New workspace **Bucla de tranzactionare** (`#/tradingloop`), plus the
payload key the sidebar dereferences. It shows the mode and kill
switch, the broker account with the *source* of every figure, holdings,
target-versus-actual, why signals did not trade, orders, the lineage
chain and the cycle history.

Three properties are tested rather than described:

- `positions` is filtered on `origin = 'broker_reconciled'` **inside
  the SQL**, so a reader cannot get intentions back from a positions
  query;
- the mode is resolved through `TradingModeStore.resolve`, so a stored
  `"live"` displays as OFF with its reason — the page cannot
  reimplement the rule and drift from the one the loop obeys;
- every eligibility code is collected, including the refusals.

No fetch, no XHR, no WebSocket, no trade button.

---

## N. Tests

| Suite | Tests |
|---|---|
| `tests/trading/test_loop_lifecycle.py` | 47 |
| `tests/trading/test_fail_safe_and_boundary.py` | 31 |
| `tests/trading/test_paper_validation.py` | 24 |
| `tests/trading/test_end_to_end_paper.py` | 18 |
| `tests/test_dashboard_trading_loop.py` | 16 |
| **New total** | **136** |

**Full suite: 3,825 passing, 1 skipped** (from 3,689). No existing test
was modified or removed.

Five defects were found by these tests during development and fixed:

1. **A SELL of 0.1255 shares.** A 10% weight target against a held
   position produced a delta the venue could not trade, refused three
   layers later for `QUANTITY_INCREMENT`. Deltas are now floored
   through Phase 14's own `normalize_quantity`, and a gap below the
   venue's minimum is `below_minimum`, not a small trade.
2. **A filled order counted as still pending.** Deciding before
   observing double-counted the same 500 shares as `actual` *and*
   `pending`, producing a SELL of 500 against a target of 499.87. The
   cycle now observes first.
3. **A fill invisible until the loop wanted another trade.** The
   decision half returned early on "nothing to trade" and skipped the
   observation half. Observation is now unconditional.
4. **Pending read only from the broker.** A second process saw an empty
   open-order book and re-placed an existing order. Pending is now the
   larger of the broker's view and ours.
5. **A crashed reconciliation recorded no reason.** Now a
   `reconciliation_failed` block.

Two more found in the fixtures, worth recording because both looked
like product bugs: a duplicate mock contract made every order fail
`NO_INSTRUMENT_MAPPING`, and a helper that debited the venue's cash a
second time made the loop correctly sell the difference.

---

## O. Security and live-safety verification

`scripts/audit_live_safety.py --untracked` → **16/16 PASS**.

Additionally, asserted in `test_fail_safe_and_boundary.py`:

- no credential token appears anywhere in `src/trading` (tokenised, so
  the docstrings explaining the rule cannot satisfy it);
- `mode.py` is the **only** file in the package that reads the
  environment, and the variable it reads cannot grant permission the
  database has withheld;
- the CLI declares no `--password`, `--secret`, `--token`, `--api-key`
  or `--username` argument (checked by parsing its `add_argument` calls);
- `--dry-run` is the default;
- four independent live refusals, each tested on its own;
- a source scan for a positively-named live environment, **with a
  negative control** proving the scan can fire.

The boundary is **measured, not asserted**: a cycle's row counts are
compared across every table in the database, and only the fourteen
Phase 25 tables plus those Phase 11/14/16 legitimately write may move.
`portfolios`, `positions`, `signals`, `predictions` and
`trained_models` are asserted unchanged, and a negative control proves
the check can fail.

---

## P. End-to-end paper test result

Fifteen ordered assertions over one trade:

```
signal sig-live-1 → eligible (experimental, 13 checks)
  → target 500 @ 100.00, decision risk-82f5edfe…
  → risk_decisions row: approved
  → order_intents row: intent-81e0d7e4…
  → order eo-…, broker ib-000001, environment paper
  → validating → approved → submitting → submitted → acknowledged → filled
  → fill ex-000001, 500 @ 100.50, execution id persisted
  → position 500 @ 100.50, origin broker_reconciled
  → reconciliation clean
  → P&L: local realized labelled; broker side absent → "not comparable"
  → trade outcome to-…, is_open=1, environment paper
  → trade_lineage.complete = TRUE, broken = FALSE
  → integrity 12/12, conclusive
  → exactly one order across both cycles
```

A separate case then confirms the four attribution loaders see that
fill, and that `detect_execution_error` stops reporting a missing input
and starts reporting slippage.

---

## Q. Known limitations

1. **No real IBKR paper session was contacted.** No Client Portal
   Gateway exists in this environment and one cannot be started
   headlessly. The mock proves the adapter, not the venue.
2. **No production order.** The confidence floor (0.40 vs 0.30) blocks
   every current signal. Correct behaviour, and it means the loop's
   production path has never carried a real trade.
3. **`detect_portfolio_error` is still blind.** It needs a
   concentration figure against a limit and Phase 25 records exposure
   without one. Reported as missing, not as passing.
4. **No round trip has been closed on real data.** Every closed-trade
   number in `PaperValidation` therefore reads unmeasured, and
   `is_conclusive()` returns False on everything this project can
   currently produce.
5. **`stability`, `model` and `strategy` quality remain unmeasured** for
   the same reason Phase 24 found: the record is too short.
6. **The Phase 13 simulated paper path still exists** (TD-04, re-scoped
   rather than closed). It cannot reach a broker, and Phase 25
   deliberately did not extend it.
7. **Local P&L is naive average-cost accounting.** It is meant to
   *disagree* with the broker when something is wrong, not to beat it.
8. **No static analysis or coverage tooling is configured** in this
   repository. Stated, not invented — the same note Phase 24 left.

---

## R. Remaining risks

- **The mock could be wrong about IBKR.** Named in Phase 15, unchanged,
  and now load-bearing for more of the system.
- **The 15-minute cycle anchor is a policy choice.** A signal arriving
  at 15:01 waits until 15:15. Shorter anchors mean more cycles and more
  broker calls; the value is configuration and is fingerprinted into
  the session.
- **The information-age policy interacts with pipeline cadence.** At
  48 hours and a ~39-hour feature lag the window is nine hours wide.
  Nobody chose that interaction; it is now visible.
- **A GitHub Actions cycle cannot reach a real gateway**, so scheduled
  runs are exercise, not trading. The workflow says so and defaults to
  `--mock` and `--dry-run`.
- **`persist_all` writes the order book at the end of the cycle.** A
  process killed between submission and persist leaves an order at the
  venue that the next run recovers as UNKNOWN — correct, and it needs a
  human to resolve.

---

## S. Exact next-step recommendation

**Run the loop against a real IBKR paper account, locally, once.**

```bash
# 1. start the Client Portal Gateway and log in (browser)
# 2. then, with IBKR_ENVIRONMENT=paper and IBKR_ACCOUNT_ID set:
python scripts/run_trading_loop.py --status
python scripts/run_trading_loop.py --cycles 1 --experimental --verbose
python scripts/run_trading_loop.py --cycles 1 --experimental \
    --allow-paper-orders --no-dry-run --verbose
python scripts/run_trading_loop.py --integrity
```

That is the one claim in this report that rests on a double rather than
on the venue, and it is a single afternoon with the gateway running.

Two things should follow it, in this order and not before:

1. **Decide the confidence floor deliberately.** 0.40 against a model
   producing 0.30 is either the right refusal or a threshold set before
   anyone knew what the model would produce. That is a risk decision,
   with a named person and a recorded reason — not something Phase 25
   may change.
2. **Run the pipeline often enough that signals are fresh when the loop
   sees them**, or set the information-age policy knowing the ~39-hour
   feature lag. Either is defensible; the current combination is
   nobody's decision.

---

## Verdict

**PAPER = ENABLED. LIVE = DISABLED.**

The loop is complete, restart-safe, auditable and honest about what it
has not done. The system has stopped being an engine that produces
signals and become a controlled system that can carry one to a
reconciled position — and today it correctly carries none, for a reason
it can name.
