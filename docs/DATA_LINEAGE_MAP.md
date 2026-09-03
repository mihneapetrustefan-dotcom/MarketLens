# Data lineage map — Phase 17

Spec §51: every completed trade must be traceable end to end. This
document records where the chain is whole, where it is broken, and what
each break means.

---

## 1. The required chain

```
Market State → Features → Model → Prediction → Signal
  → Portfolio Decision → Risk Decision → OrderIntent
  → IBKR Order → Execution → Fill → Position → P&L → Trade Outcome
```

## 2. Link-by-link status

| # | Link | Carrier | Status |
|---|---|---|---|
| 1 | Market state → Feature | `research_features.observation_id`, `feature_version` | **WHOLE** |
| 2 | Feature → Model | `trained_models.feature_version`, dataset version | **WHOLE** |
| 3 | Model → Prediction | `predictions.model_id` | **WHOLE** |
| 4 | Prediction → Signal | `signals.prediction_id`, `model_version` | **WHOLE** |
| 5 | Signal → Portfolio decision | `allocation_proposals.source_signal_id` | **WHOLE** (schema empty) |
| 6 | Portfolio → Risk decision | `risk_decisions.proposal_id` | **WHOLE** (schema empty) |
| 7 | Risk decision → OrderIntent | `order_intents.decision_id`, guarded by `OrderIntent.require_approval` | **WHOLE** (schema empty) |
| 8 | **OrderIntent → IntentRequest** | — | **WAS BROKEN · FIXED PHASE 17** |
| 9 | IntentRequest → ExecutionOrder | `intent_id`, `idempotency_key`, `client_order_id` | **WHOLE** |
| 10 | ExecutionOrder → IBKR order | `broker_order_id`, `conid` | **WHOLE** |
| 11 | IBKR order → Execution | IBKR execution id | **WHOLE** |
| 12 | Execution → Fill | `execution_fills.execution_id` (dedupe key) | **WHOLE** |
| 13 | Fill → Position | `apply_fill_to_order` + position accounting | **WHOLE** |
| 14 | Position → P&L | `trade_outcomes.gross_pnl`, `fees`, `net_pnl` | **WHOLE** |
| 15 | → Trade Outcome | `TradeLineage`, 21 flat id fields | **WHOLE** |

**14 of 15 links whole; the 15th was link 8 and is now closed.**

## 3. Link 8 — what was broken

`IntentRequest` is the only entry into the execution stack. Before this
phase it was constructed in two places, both CLI scripts, both by hand,
and `risk_approved` came from `--assume-risk-approved`.

The consequence for lineage: an order carried `decision_id` only if a
human typed one. A `RiskDecision` in the database and an
`ExecutionOrder` in the database had **no enforced relationship**.

`src/execution/intake.py` closes it and refuses to produce a request
whose `signal_id`, `decision_id` or `portfolio_id` is missing —
`LineageIncomplete`. A trade whose provenance is already broken at
submission cannot be repaired afterwards, so it is refused at the
boundary rather than recorded incomplete.

`require_lineage=False` exists for an operator smoke test, is named
explicitly, and is off by default.

## 4. Lineage storage

`trade_outcomes` holds all 21 identifiers as **columns on one wide
row**, indexed six ways (`strategy_id`, `model_version`, `signal_id`,
`instrument_id`, `market_regime`, `session_id`).

Flat rather than joined, deliberately: a chain that needs six tables to
reconstruct is a chain that breaks the first time one is pruned — and
pruning happens years later, by someone who does not know what the join
was for.

```
correlation_id · model_id · model_version · prediction_id
feature_version · signal_id · signal_version · strategy_id
strategy_version · portfolio_id · decision_id · risk_config_version
intent_id · order_id · client_order_id · broker_order_id
execution_ids[] · fill_ids[] · session_id
execution_config_version · code_version
```

`TradeLineage.is_complete` checks the chain can actually be walked;
`missing_links` names what is absent.

## 5. The other half — trades that did not happen

`missed_trades` records every signal that failed to become a trade,
with `prevented_by_system` distinguishing a system refusal from a
market outcome.

This matters for spec §52: a system recording only what it did cannot
tell a bad signal from a good one that risk stopped. A learning system
fed only completed trades would learn from a sample selected by the
risk engine.

## 6. Point-in-time guarantee

Feature timestamp ≤ information cutoff < label start, enforced
structurally by `PointInTimeView`, which raises `LookAheadViolation`
rather than returning filtered data. Leakage crashes tests rather than
producing plausible wrong numbers.

**Caveat, stated rather than buried:** the barrier protects the
research path (Phases 6–10). The legacy Phase 1–9 scoring engines
predate it and read full current history. Harmless — they feed a
dashboard, not a model — but they are not point-in-time safe and must
not become a research input without being brought behind the barrier.

## 7. Readiness for autonomous learning (spec §52)

The question is not whether the system learns — §52 forbids
implementing that — but whether the data would let it.

| A future learner must distinguish | Available? | From |
|---|---|---|
| prediction error | ✅ | `predictions` vs realised return |
| signal error | ✅ | `signal_outcomes`, `signal_evaluations` |
| timing error | ✅ | `decision_price` vs `submitted_price` |
| sizing error | ✅ | intent quantity vs risk-approved quantity |
| risk error | ✅ | `risk_decisions` + `missed_trades.prevented_by_system` |
| execution error | ✅ | `slippage_bps`, latencies, `execution_error` |
| slippage | ✅ | three prices kept apart |
| regime mismatch | ✅ | `market_regime`, `event_context` on outcomes |

All eight are representable. **None is populated**, because no trade
has completed. The architecture supports the learning phase; the data
does not exist yet, and cannot until signals stop being suppressed.
