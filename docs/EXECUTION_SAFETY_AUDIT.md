# Execution safety audit — Phase 17

Spec §25, §26, §27, §28, §29, §65. Every claim here was established by
executing code or tracing call graphs, not by reading comments.

---

## 1. The execution graph

Search across `src/`, `scripts/`, `run_daily.py` for `submit_order`,
`place_order`, `send_order`, `execute_order`.

**Definitions (10):**

```
src/execution/gateway.py:187              BrokerGateway.submit_order      (abstract)
src/execution/adapters/paper_gateway.py   PaperGateway.submit_order
src/execution/adapters/ibkr/gateway.py    IBKRGateway.submit_order
src/execution/adapters/disabled_gateway.py DisabledBrokerGateway.submit_order (refuses)
src/execution/adapters/ibkr/transport.py  place_order  (HTTP, x2)
src/execution/adapters/ibkr/mock_transport.py place_order (test double)
src/execution/service.py:266              ExecutionService.submit  (facade)
src/paper/executor.py:162, :327           PaperExecutor.place_order (simulation)
```

**Call sites that can reach a venue (1):**

```
src/execution/orchestrator.py:549
    ack = entry.gateway.submit_order(order, request.now)
```

One choke point. `_submit` is documented in the source as *"The only
method in this file that can reach a venue."* — and the graph confirms
it.

## 2. Path A — the broker path

```
IntentRequest
  └─ ExecutionService.submit(caller, request)     ← permission check
      └─ ExecutionOrchestrator.execute(request)
          ├─ _prepare()
          │   ├─ registry.get(broker_id)          → UNKNOWN_BROKER if absent
          │   ├─ safety.assert_not_real_money()   ← HARD STOP, raises
          │   ├─ policy.decide()
          │   └─ validator.validate()             ← 23 checks incl. risk
          ├─ idempotency key / duplicate check
          ├─ state machine: VALIDATING → APPROVED
          └─ _submit() → gateway.submit_order()   ← THE LINE
```

Gate order verified by reading `_prepare`: the real-money assertion is
the **first** thing after broker lookup, before policy, before
validation, before anything constructs an order.

## 3. Path B — the paper path

```
PaperSession.tick()
  ├─ freshness.evaluate()
  ├─ PortfolioService.evaluate()    ← the REAL Phase 11 risk engine
  ├─ (refuses if decision not approving)
  └─ PaperExecutor.place_order()    ← simulation only
```

**This is a second execution path and it does not pass through the
Phase 14 orchestrator, validator or safety layer.**

Assessed as **acceptable, not a bypass**, on the following grounds:

- `PaperExecutor` cannot reach any broker. It fills against stored
  candles.
- `PaperAccount.is_paper` cannot be set to `False` — construction fails.
- `ExecutionVenue` has exactly one member, `PAPER`.
- It **does** consult the real risk engine, which until this phase was
  more than the broker path did.

It is nonetheless recorded as debt (TD-04): two order lifecycles exist,
and a rule fixed in one will not appear in the other.

## 4. Risk enforcement (spec §20)

`ValidationRequest.risk_approved: Optional[bool] = None`, and in
`validation.py:294`:

```python
if request.risk_approved is None:
    result.fail(ExecutionRejectCode.RISK_UNAVAILABLE,
                "The risk engine was not consulted for this order.")
elif not request.risk_approved:
    result.fail(ExecutionRejectCode.RISK_REJECTED, request.risk_detail)
```

Fails closed. Correct.

### The finding

Before this phase, the **only** producers of `risk_approved=True` in
the entire repository were:

```
scripts/run_execution.py:320   risk_approved=True if args.assume_risk_approved else None
scripts/run_ibkr.py:324        risk_approved=True if args.assume_risk_approved else None
```

`RiskDecision` objects — produced by `src/portfolio/risk_engine.py:252`
— reached `portfolio_repository` and the dashboard and **nothing
else**. `IntentRequest` was constructed in exactly two places, both CLI
scripts, both by hand.

So the chain the project exists to produce was broken at one joint:

```
signal → portfolio decision → risk decision → order intent
                                              ╳
                                        IntentRequest → order → IBKR
```

Every control was present. The wire between two of them was not.

### The fix

`src/execution/intake.py`. `from_decision(decision, intents, …)`:

- raises `RiskNotApproved` unless `decision.is_approved` — checked
  first, before any other argument matters
- sets `risk_approved=decision.is_approved` — a fact about an object
- raises `LineageIncomplete` when signal / decision / portfolio ids are
  missing
- rejects (rather than guesses) intents with no price or no quantity —
  sizing belongs to the portfolio layer
- **has no override parameter**, asserted by a test that inspects the
  signature

`--assume-risk-approved` remains on both CLIs, now labelled `OPERATOR
OVERRIDE` in its help text and pointing at the intake as the real path.
Removing it would break hand-typed smoke tests; leaving it undocumented
alongside a proper path would be worse.

## 5. Real-money execution (spec §24)

Six independent enforcement points, all verified by execution via
`scripts/audit_live_safety.py`:

| # | Location | Refuses |
|---|---|---|
| 1 | `Broker` / `BrokerAccount` constructors | a real-money environment, at all |
| 2 | `IBKRConfig.__post_init__` | `IBKR_ENVIRONMENT=live`, whatever route the value took |
| 3 | `SessionConfiguration` | a session configured for real money |
| 4 | `ExecutionSafety.allow_real_orders` | read-only property, no setter, no env var read |
| 5 | every `BrokerGateway` implementation | none of the three accepts real money |
| 6 | `ExecutionLevel.is_implemented` | `False` for levels 5–7 |

The first five would each stop a live order. The sixth is why none is
reached: **there is no real-money execution path to block.**

Audit result: **16 / 16 questions pass.**

## 6. Idempotency (spec §28)

- Order keys derive from the intent, not from a counter — the same
  intent after a restart resolves to the same order rather than a
  second one. Tested (`test_a_restart_does_not_resubmit_a_live_order`).
- `ON CONFLICT(order_id) DO UPDATE` on persistence, not
  `INSERT OR REPLACE` — the latter would delete an order colliding on
  its idempotency key.
- Fills deduplicate on the **IBKR execution id**, never on visible
  fields: two genuinely different executions can be identical in
  quantity, price and timestamp.
- `apply_fill_to_order` refuses a fill that would exceed the order's
  own quantity — the shape a duplicate takes when it slips past.

## 7. State machine (spec §27)

16 states, explicit `ORDER_TRANSITIONS` table, illegal transitions
raise. `RECONCILIATION_REQUIRED` is reachable from every working state
(added programmatically after a Phase 14 audit found it unreachable).
`UNKNOWN` is terminal-but-resolvable: `resolve_unknown_orders` asks the
broker and **never resubmits**.

## 8. Reconciliation (spec §26)

Mismatches are graded since Phase 16. `POSITION_MISMATCH`,
`CASH_MISMATCH`, `DUPLICATE_FILL`, `UNKNOWN_BROKER_ORDER` are
`CRITICAL` and block execution. Price and status differences are `INFO`
and do not.

**No silent overwrite exists.** `ReconciliationMismatch.resolve()`
raises when the actor is `system` and the severity is above `INFO`:
automatically correcting an unexplained capital or position discrepancy
destroys the evidence of its cause.

## 9. Failure recovery (spec §29)

All injected against the mock venue and passing:

| Failure | Behaviour |
|---|---|
| broker disconnect | caught before an order is built; venue never contacted |
| authentication loss | `REJECTED` + `BROKER_DISCONNECTED`; `place_calls` unchanged |
| competing session | surfaced as unhealthy (IBKR allows one login; a second displaces the first) |
| post-submission timeout | order → `UNKNOWN`, **not** `FAILED` — claiming failure invites a duplicate |
| duplicate execution | applied once; second returns 0 |
| rate limit | `RATE_LIMITED`, distinguishable from a venue rejection |
| venue rejection | recorded, never retried |
| application restart | idempotency index restored from the database first |
| market data loss | quote returns with `RESTRICTED` availability and no prices |

## 10. Residual risks

1. **Two order lifecycles** (TD-04). Paper and broker paths diverge.
2. **Nothing calls the intake in production yet** — `portfolios` has
   zero rows, so there is no evaluation to convert. The join exists and
   is tested; it is not yet exercised end to end by a scheduled job.
3. **Never run against a real IBKR account.** Unchanged since Phase 15.
   Every test uses `MockIBKRTransport`; what a mock cannot prove is
   that IBKR behaves the way the mock does.
