# Execution architecture (Phase 14)

The broker abstraction. This document covers what the layer is, where
its boundary sits, and what it deliberately refuses to do.

**No real-money execution exists.** Interactive Brokers is the only
broker of this project and is connected in its PAPER environment only
(Phase 15). No adapter accepts a real-money environment, no broker
credential lives in the application, and no live order path exists.
This is not a disabled path — it is an absent one, and §11 below
explains how that is enforced rather than merely stated.

---

## 1. The boundary

```
        MARKET DATA
             |
        FEATURE ENGINE
             |
        MODELS  ->  PREDICTIONS
             |
        SIGNALS                    (Phase 10)
             |
        PORTFOLIO                  (Phase 11)
             |
        RISK                       (Phase 11)
             |
        ORDER INTENT               <-- strategy code ends HERE
             |
  ===========|============================ the broker-neutral boundary
             |
        EXECUTION ORCHESTRATOR     (Phase 14)
             |
        BROKER GATEWAY  (abstract) (Phase 14)
             |
        BROKER ADAPTER  (concrete) (paper, and IBKR from Phase 15)
             |
        EXTERNAL BROKER
             |
        BROKER EVENTS
             |
        EXECUTION STATE  ->  FILLS  ->  PORTFOLIO ACCOUNTING
             |
        RECONCILIATION  ->  MONITORING
```

Nothing above the boundary may know which broker it is talking to. No
SDK type, no broker symbol, no broker status string and no broker order
id crosses it. What crosses is the canonical types in
`src/domain/broker_models.py`.

The rule is what made Phase 15 an *addition* rather than a rewrite:
the IBKR adapter implements `BrokerGateway`, and strategy, signal,
portfolio and risk code stayed untouched.

## 2. Execution environments

Four environments, and the safety layer is separate from all of them —
an environment says what KIND of execution is described; the safety
layer says whether it is permitted.

| Environment | Adapter | Status |
|---|---|---|
| `SIMULATION` | Phase 12 `SimulationExecutor` | implemented, historical bars |
| `PAPER` | Phase 13 `PaperExecutor` via `PaperBrokerGateway` | implemented, simulated fills |
| `DEMO` | none | named, not implemented |
| `LIVE` | none | **refused structurally** |

## 3. The pipeline, in order

The order of operations *is* the safety model:

```
safety          can we execute at all
routing         which broker, which account
idempotency     have we already done this
mapping         does this instrument exist there
capability      can that venue do this
market session  is it open
risk            did Phase 11 approve
validation      everything else, all findings collected
---- the line ----
submission      the only step that can reach a venue
```

Every check sits above the line. Once a submission is issued the
outcome may be unknown, so nothing may reach that state for a reason
that could have been caught first. `dry_run()` runs the identical
pipeline and returns at the line.

## 4. The order state machine

```
CREATED -> VALIDATING -> APPROVED -> SUBMITTING -> SUBMITTED
        -> ACKNOWLEDGED -> WORKING -> PARTIALLY_FILLED -> FILLED

  any of:  REJECTED  CANCELLED  EXPIRED  FAILED     (terminal)
  any of:  UNKNOWN   RECONCILIATION_REQUIRED        (open questions)
```

`UNKNOWN` and `RECONCILIATION_REQUIRED` are deliberately **not**
terminal: they are open questions, and treating an open question as
settled is how a real position goes unrecorded.

Transitions are enforced by `ORDER_TRANSITIONS`; anything absent is
refused. `RECONCILIATION_REQUIRED` is reachable from every state,
because a disagreement with the broker can be discovered at any point.

Broker status vocabularies are translated by their adapter. A raw
broker status string never reaches the orchestrator, the database or
the UI.

## 5. Order identifiers

Six, and they are genuinely six different things:

| Identifier | Meaning |
|---|---|
| `intent_id` | the portfolio decision this came from |
| `order_id` | our record, stable across everything below |
| `client_order_id` | what we told the broker to call it |
| `broker_order_id` | what the broker calls it, learned on acceptance |
| `execution_id` | per broker execution report |
| `fill_id` | per individual fill |

Collapsing any pair breaks a real case. A submission that times out has
an `order_id` and a `client_order_id` but no `broker_order_id`, and
that gap is exactly what reconciliation searches on.

## 6. Idempotency

The key is **derived**, not assigned, so a retry after a crash — where
nothing local survived — computes the same key and recognises its own
earlier work:

```
sha1(account | instrument | side | quantity | order_type
     | time_in_force | limit | stop | intent_id | intent_version)
```

Deliberately **excluded: the signal id.** Phase 13 learned this: several
live signals for one instrument frequently ask for the same target at
the same moment, so keying on the signal produced one order per signal
and protected nothing. The signal is still carried for provenance — it
just does not participate in identity.

`intent_version` is the escape hatch: two genuinely different orders for
the same instrument, side and size in the same second are rare but real
(a scale-in, a correction after a partial cancel), and the caller states
that rather than the system guessing from timing.

Two layers enforce it: an in-memory index (fast path) and a unique
database index (survives the process). Recovery rebuilds the first from
the orders themselves, so it cannot drift out of step with the book it
protects.

## 7. The timeout, and why nothing is ever resubmitted

The single most dangerous moment in live execution:

```
submit -> network timeout -> the broker may or may not have accepted
```

Guessing either way is how one intended position becomes two real ones.

```
UNKNOWN -> QUERY BROKER -> RECONCILE -> RESOLVE
```

`resolve_unknown_orders` asks the venue and applies what it says. Three
outcomes:

- **the broker knows it** — adopt the broker's state
- **the broker never saw it** — `FAILED`, which is safe: a venue with
  no record will not fill it
- **we cannot ask** — stays `UNKNOWN` and is reported, because the
  honest answer is still that we do not know

It never resubmits, under any outcome.

## 8. Event ordering

Broker events arrive duplicated, late, and out of order. All three are
ordinary; each corrupts state differently if handled naively.

- **Duplicates** are recognised by the venue's execution id, or a
  deterministic key — never by comparing payloads. Two genuinely
  different fills can be identical in every visible field.
- **Late events** are detected by lifecycle position, not timestamps.
  Brokers stamp with their own clock, and some stamp a whole batch
  identically.
- **Reordering** — `FILLED` arriving before `PARTIALLY_FILLED` — is
  handled by refusing to wind an order backwards.

A `FILLED` status our fills cannot account for is **not** applied: it
becomes a `STATUS_MISMATCH` finding and the order moves to
`RECONCILIATION_REQUIRED`. Adopting it would create a position no
execution explains.

## 9. Reconciliation

Internal state is a *belief* about the broker. The broker is the fact.

Compared in both directions — orders, fills, positions, cash. Findings
are recorded, never silently repaired: a position mismatch is
uncomfortable, and adjusting the local book makes it disappear along
with the only evidence of whatever caused it.

Order status is compared by **stage**, not spelling. Venues disagree
about what to call an accepted-but-unfilled order, and comparing exact
states would report a mismatch on every healthy book.

Mismatch kinds: `MISSING_INTERNAL_ORDER`, `UNKNOWN_BROKER_ORDER`,
`MISSING_FILL`, `DUPLICATE_FILL`, `POSITION_MISMATCH`, `CASH_MISMATCH`,
`QUANTITY_MISMATCH`, `PRICE_MISMATCH`, `STATUS_MISMATCH`,
`UNKNOWN_STATE`.

## 10. Instrument mapping

```
Canonical Instrument  ->  Broker Mapping  ->  Broker Symbol / Contract
```

The core knows `instrument_id`. A venue knows `AAPL`, `AAPL.US`,
`EURUSD.a`, or a contract object. Venue-specific facts — tick size, lot
size, quantity increment, contract multiplier, tradability — belong to
the *(instrument, broker)* pair, because the same security is
fractional at one venue and whole-lot at another.

There is **no fallback** that turns an unmapped instrument into a symbol
by string manipulation. A guess that is usually right is the worst kind:
it works until it silently trades the wrong contract.

Quantities round **down**, never up — rounding up would submit more
exposure than the risk engine sized, and the risk engine is the
authority on size. A negative quantity is *refused*, not made positive:
direction lives in `side`.

## 11. The live-execution boundary, enforced five times

1. `ExecutionEnvironment.LIVE` cannot be attached to a `Broker` that
   claims to be implemented, or to a `BrokerAccount` at all — the
   domain types refuse construction.
2. `ExecutionSafety.allow_real_orders` is a property with **no setter**.
3. `ExecutionSafety.check` refuses `LIVE` before it looks at anything
   else, so the reported reason is the one that will not change
   tomorrow.
4. No permission grants live execution — `LIVE_EXECUTION_ADMIN` is
   refused even when held, because holding a permission for a
   capability that does not exist must not be mistaken for the
   capability existing.
5. No adapter capable of a live order exists, and
   `DisabledBrokerGateway` refuses construction for a real-money
   environment.

The environment variable `MARKETLENS_ALLOW_REAL_ORDERS` is *read* — but
only so the system can report that someone set it. It changes nothing.
The worst outcome would be an operator setting it, seeing nothing
happen, and assuming it worked.

The repository also refuses to store a `live_execution_enabled` control:
real-money execution is not a flag in this phase, so there is nothing to
set.

## 12. Safety controls and the kill switch

Layered, checked outermost-first so the reported reason is the broadest
one that applies: `execution_enabled` → environment → broker → account →
strategy → portfolio.

The kill switch stops **new orders**. It deliberately does not cancel
working orders or delete anything: history stays intact and
reconciliation keeps running, because the moment you most need to know
what you hold is the moment you hit the switch. Stopping requires a
lower permission than trading — stopping is always safer than
continuing.

## 13. Security

No method anywhere accepts a credential, and no database column exists
to hold one. `BrokerGateway.connect()` takes **no arguments** by design:
a future adapter reads its own secrets from the environment at connect
time (the pattern the existing collectors already use), so they never
appear in a call site, a log line, or a serialised dashboard payload.

The interface has no `login` and no `authenticate`. A stub with an empty
`api_key` field would be an invitation.

## 14. Operations

There is no HTTP API in this repository, and none was added — every
phase runs as a batch job and publishes a static page, so adding a
server for one phase would be a parallel architecture. The operations an
API would expose live in `src/execution/service.py`, and the CLI and
dashboard consume them. The mapping, if a server is ever added:

| Route | Service method |
|---|---|
| `GET /brokers` | `list_brokers()` |
| `GET /brokers/{id}/health` | `broker_health()` |
| `GET /brokers/{id}/capabilities` | `capabilities()` |
| `GET /accounts/{id}/positions` | `positions()` |
| `GET /accounts/{id}/orders` | `orders()` |
| `POST /execution/validate` | `validate()` |
| `POST /execution/dry-run` | `dry_run()` |
| `POST /execution/order` | `submit()` |
| `POST /orders/{id}/cancel` | `cancel()` |
| `GET /execution/reconciliation` | `reconcile()` |

Permissions are presented explicitly by the caller rather than derived
from an authenticated session, because there are no sessions here. That
is weaker than real auth and is stated as such. What it does preserve is
the distinction that matters: reading execution state and causing
execution are different permissions.

## 15. Persistence

Additive `CREATE TABLE IF NOT EXISTS`, matching every earlier phase. No
existing table is touched; nothing is dropped, renamed or rewritten.

Fourteen tables: `brokers`, `broker_accounts`, `broker_capability`,
`broker_connection`, `broker_health`, `broker_instrument_mapping`,
`execution_orders`, `order_state_history`, `execution_fills`,
`execution_events`, `reconciliation_records`, `execution_errors`,
`execution_audit`, `execution_controls`.

State history is its own table because current state cannot answer the
questions it exists for: how long an order worked, whether it passed
through `UNKNOWN`, which event caused each move.

Orders upsert on their **primary key only** — never `INSERT OR REPLACE`,
which resolves a conflict on any unique index by deleting the row it
collided with, and would therefore let a second order carrying an
existing idempotency key silently erase the first.

## 16. Recovery

`ExecutionRepository.restore` rebuilds the book, the transition history,
the fills and the deduplication sets. Restoring the event keys matters
because reconnecting is exactly what makes a venue replay its recent
events.

Orders that were **in flight** — `SUBMITTING` or `SUBMITTED` with no
outcome recorded — become `UNKNOWN` rather than being assumed either
way. The venue may hold an order nothing local knows the fate of.

The paper gateway also restores its own venue book, because the paper
"venue" is an in-process executor that forgets on restart while a real
broker keeps its state across ours. Without it, every recovered order
would look like one the venue had never heard of.

## 17. What Phase 15 and 16 must do

Implement `BrokerGateway`. That is the whole contract.

Specifically: translate the venue's symbols through
`BrokerInstrumentMapping`, its statuses into `ExecutionOrderState`, its
rejections into `ExecutionRejectCode`, and its execution reports into
`ExecutionEvent` plus `ExecutionFill`. Declare only the capabilities the
adapter actually implements. Read credentials from the environment
inside `connect()`.

Nothing in strategy, signals, portfolio or risk changes.

IBKR characteristics the canonical model accommodates: contracts and
exchanges via `broker_payload`, its own order ids kept separate from
ours, multiple asset classes via `BrokerCapability.asset_classes`,
account updates through the same event vocabulary, netting **and**
hedging accounting (`PositionAccounting`), account currency, and margin
fields that are `Optional` because a cash account has none.

## 18. What is deliberately absent

- Smart order routing. `brokers_for_instrument` returns *candidates* and
  does not choose; preferred venue, fallback and asset-class routing are
  extension points, not implementations.
- TWAP, VWAP and participation policies. Named in `PLANNED_POLICIES` so
  the UI can say they are planned; `get_policy` raises for them. A class
  named after an algorithm it does not implement reads as available.
- Transaction-cost analysis. The data architecture is prepared
  (`decision_price`, `reference_price`, `slippage_bps`, per-fill costs);
  the analysis is not built.
- Bracket, OCO, trailing-stop execution. Declared in
  `CanonicalOrderType` so capability checks can refuse them; no adapter
  supports them.
- Multi-process concurrency. This is a single-process batch system on
  SQLite. Idempotency and transactional writes are in place; a
  distributed lock is not, and would be untested here.
