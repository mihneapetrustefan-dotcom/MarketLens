# Phase 16 — production readiness and the controlled-live foundation

What Phase 16 built, what it deliberately did not build, and how the
second follows from the first.

**Interactive Brokers is the only broker of this project.** There is no
MetaTrader 5 adapter, no MT5 compatibility layer, no multi-broker
routing and no placeholder for a second venue. `planned_gateways()`
returns nothing, and no module under `src`, `tests` or `scripts`
mentions MT5.

**Real-money execution is blocked, and the block is an absence.** Every
execution level from 4 upward is specified, gated and tested — and none
is implemented. No adapter accepts a real-money environment, so there
is nothing for the gates to stop. This is stated plainly rather than
implied, and §11 explains how it is enforced rather than promised.

---

## 1. What this phase is

Phases 11–15 built a chain: risk decision → order intent → validated
execution order → IBKR paper order → fill → position. It works, and
that is exactly the moment at which the interesting question stops
being "does it execute" and becomes "under what circumstances should
it be allowed to".

Phase 16 answers that. It adds the layer between *capable* and
*permitted*:

- **Execution levels** — a ladder from research to production, with
  each rung naming what it requires
- **Promotion gates** — measurable criteria, where an absent
  measurement blocks
- **Human approval** — nothing promotes itself, and no one approves
  their own request
- **Trading sessions** — execution happens inside an explicitly opened
  session with a frozen configuration
- **Operational limits** — capital, loss, freshness and quality
  ceilings, with the ones that must not clear themselves latching
- **Health and monitoring** — capability-level readings where the
  aggregate is the worst, never an average
- **Trade lineage and outcomes** — the full causal chain per trade,
  preserved as ids so a later analysis can walk it

## 2. The layers

```
      SIGNAL  ->  RISK DECISION  ->  ORDER INTENT      (Phases 11-12)
                                          |
  ================ the governance boundary (Phase 16) ================
                                          |
     EXECUTION GOVERNOR   level, promotion gates, human approval
                                          |
     TRADING SESSION      frozen config, preflight, may_submit()
                                          |
     RISK GOVERNOR        capital, loss, freshness, quality limits
                                          |
     EXECUTION SAFETY     kill switch, real-money refusal   (Phase 14)
                                          |
  ================ the broker-neutral boundary (Phase 14) ============
                                          |
     EXECUTION ORCHESTRATOR  ->  BROKER GATEWAY (abstract)
                                          |
                            IBKR GATEWAY (paper only)
                                          |
                            IBKR TRANSPORT  <- the second boundary
                                          |
                            Client Portal Web API
```

An order must pass **every** layer. Each refuses independently, and
each refusal is recorded with the reason — an order that is stopped
does not vanish, it becomes a `REJECTED` order with a canonical reject
code, because a missing trade with no record is a missing trade nobody
can explain.

## 3. Execution levels

| Level | Name | Implemented | Real money |
|---|---|---|---|
| 0 | Research | yes | no |
| 1 | Backtest | yes | no |
| 2 | Paper | yes | no |
| 3 | IBKR paper, end to end | yes | no |
| 4 | Controlled-live preparation | **no** | no |
| 5 | Micro-capital live | **no** | **yes** |
| 6 | Restricted live | **no** | **yes** |
| 7 | Production live | **no** | **yes** |

`ExecutionGovernor.effective_level()` returns the highest level that is
both approved **and implemented**. Approving level 7 records the
approval and leaves the effective level at 3. That is not a
configuration quirk; it is the design. Implementation is a fact about
the code, and no amount of approval changes it.

## 4. Promotion gates

Fourteen gates, each with an explicit direction so a `max_drawdown`
gate cannot be compared the wrong way round:

paper days, paper trades, max drawdown, worst daily loss, max position
weight, max leverage, execution error rate, median slippage, rejection
rate, reconciliation mismatch rate, unknown-state rate, broker uptime,
signal stability, model stability.

**Profitability is deliberately not a gate.** A strategy can be
profitable over a short period by luck, and promoting on that basis
commits capital to noise. The gates measure whether the *machinery*
behaves, which is a question a small sample can actually answer.

**An unmeasured gate blocks.** Not measuring something is not evidence
that it is fine, and a gate that passed on absent data would be most
permissive exactly when instrumentation had failed.

## 5. Human approval

`PromotionRequest.approve()` refuses when the approver is the
requester. It is the cheapest possible four-eyes control and it is
worth having even with a single operator, because it forces approval to
be a deliberate second act rather than a continuation of the first.

Approvals expire. A standing permission nobody renews is how a
temporary decision becomes permanent by inattention.

## 6. Trading sessions

Execution happens inside a session, never outside one. A session:

- runs a **preflight** of eight checks, where an unmeasured check
  blocks as firmly as a failed one
- freezes its **configuration** at start, fingerprinted across every
  version field (model, strategy, feature, signal, risk config,
  execution config, capital limit)
- **detects drift** by recomputing the fingerprint rather than trusting
  that nothing mutated it — a change that bypassed `amend()` still
  reaches a report
- keeps **history**; nothing is deleted at close
- distinguishes a routine **pause** (resumable) from an **emergency
  stop** (terminal — continuing requires a new session)

## 7. Operational limits

Twenty-three checks in one pass, all failing closed. Capital and
notional caps, position and exposure ceilings, margin and leverage,
daily loss and drawdown, data freshness per input (quote, account,
position, risk — separately), clock drift against the venue, delayed or
frozen quotes, liquidity participation, broker health, reconciliation
state, and execution-quality thresholds.

**Nothing ships a real-money default.** Every capital cap defaults to
`None`, and `configured_for_real_money` is False until a human sets
them. A number shipped as a default becomes a production default by
inattention.

**Loss limits latch.** A daily loss limit that resumed trading the
moment the market ticked back up would defeat its own purpose. Clearing
one requires an actor and a reason. Staleness and health do not latch —
those legitimately recover.

## 8. Health and monitoring

Nine capabilities are measured separately: connection, authentication,
market data, account, orders, executions, positions, reconciliation,
clock. The aggregate is the **worst** reading, never an average — a
system whose account feed is dead is not "mostly healthy", and
averaging is exactly how that gets hidden.

Only `HEALTHY` permits new orders. `DEGRADED` does not: a degraded order
path can still carry a submission whose acknowledgement never arrives,
which is the route to an `UNKNOWN` order.

Rates return `None` rather than zero when the denominator is zero. A
zero rejection rate over zero orders reads as a perfect record; it is an
absence of evidence.

## 9. Trade lineage and outcomes

Every completed trade carries twenty-one identifier fields — model,
prediction, features, signal, strategy, portfolio, risk decision,
intent, order, client order id, broker order id, executions, fills,
session, and the config versions in force. Stored flat rather than
joined, because a chain that needs six tables is a chain that breaks
when one is pruned.

Execution quality keeps **decision price**, **submitted price** and
**fill price** apart. The first gap is decision latency, the second is
spread and impact; collapsing them makes the causes
indistinguishable.

Signals that did *not* become trades are recorded too, with whether the
**system** prevented it or the **market** did. A system recording only
what it did cannot tell a bad signal from a good one that risk stopped.

`was_profitable` is deliberately **not** one of the post-mortem error
fields. A correct prediction, correctly sized and correctly executed,
can lose money — that is what risk means. `classify_errors()` fills
only three fields that can be established mechanically and leaves every
judgement call `None`.

## 10. Reconciliation severity

Mismatches are graded. Position, cash, duplicate-fill and unknown-order
differences are `CRITICAL` and block execution. Price and status
differences are informational and do not.

**The system cannot resolve a critical finding.** Only a human can,
with a note. Automatically "fixing" an unknown capital or position
discrepancy destroys the evidence of its cause, and the cause is the
thing that matters.

## 11. How the real-money block is enforced

Six independent places, none of which is a flag:

1. `ExecutionEnvironment.LIVE` cannot be attached to a `Broker` that
   declares itself implemented, nor to a `BrokerAccount` at all — the
   constructors refuse it.
2. `IBKRConfig.__post_init__` refuses `IBKR_ENVIRONMENT=live` whatever
   route the value took, including `from_environment()`.
3. `SessionConfiguration` refuses a real-money environment.
4. `ExecutionSafety.allow_real_orders` is a read-only property that
   returns False. There is no setter, so no code path can turn it on,
   and no environment variable is consulted.
5. No `BrokerGateway` implementation accepts a real-money environment.
   There are three: paper, IBKR (paper only), and disabled.
6. `ExecutionLevel.is_implemented` is False for every real-money level,
   and `effective_level()` degrades to the highest implemented one.

The first five would each stop a live order. The sixth is why none of
them is ever reached: **there is no real-money execution path to
block.**

## 12. What Phase 16 did not build, and why

§40 of the specification asks for controlled-live architecture; §101
Q11 requires that live can never activate accidentally. Both are
satisfied by building the complete governance layer — levels, gates,
approval, sessions, limits, readiness — and **keeping the execution
refusal intact**.

The gates are real and tested. The final step of wiring an adapter that
accepts a real-money environment is deliberately absent, because no
instruction in this project authorised building a real-money path, and
creating one would be irreversible in a way the rest of this work is
not. Someone must decide that separately, knowingly, and with the
evidence the gates above are designed to produce.

§78 also forbids self-learning, reinforcement learning, autonomous
strategy modification, autonomous model promotion and live autonomous
capital management. None exists. What exists is the **lineage** a later
learning system would need — recorded now, while the trades are
happening, because it cannot be reconstructed afterwards.

## 13. Where things live

| Concern | Module |
|---|---|
| Levels, gates, approval, readiness | `src/execution/governance.py` |
| Capital, loss, freshness, quality limits | `src/execution/limits.py` |
| Sessions, preflight, configuration freeze | `src/execution/session.py` |
| Capabilities, health, metrics, alerts | `src/execution/monitoring.py` |
| Lineage, quality, outcomes, misses, journal | `src/execution/outcomes.py` |
| Persistence (13 tables) | `src/data_access/governance_{schema,repository}.py` |
| Operator CLI | `scripts/run_operations.py` |
| Operations dashboard | `src/dashboard.py` (`_collect_operations`, `xOperations`) |
| Tests | `tests/execution/test_phase16_{governance,live_safety}.py` |

## 14. Tests

`test_phase16_governance.py` — 63 tests covering levels, approval,
gates, readiness, limits, sessions and reconciliation severity.

`test_phase16_live_safety.py` — 57 tests covering the §78 requirement
by name: live execution blocked by default, and blocked independently
by the kill switch, a risk failure, broker health, stale market data, a
reconciliation failure and a capital limit — each asserted one
condition at a time, because a suite that only tested them together
would pass even if five of the six checks were dead code. Plus the §79
failure injections and the §80 end-to-end paper path.

Every test runs against `MockIBKRTransport`. What the mock cannot prove
is that IBKR behaves the way the mock does; that gap is closed by
`scripts/run_ibkr.py` against an actual paper account, and it is named
here rather than left implicit.
