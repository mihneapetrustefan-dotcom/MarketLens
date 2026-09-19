"""
src/capture
-----------------
Phase 25.9G — continuous intraday market-data capture.

OBSERVATION IS NOT DECISION. Nothing in this package imports the trading
loop, portfolio, risk, intake or orchestrator, and the gateway it holds
cannot write to the venue at either the gateway or the transport layer
(`src/execution/adapters/submission_guard.capture_only`). Capture runs with
no model, no signal, no risk approval and trading mode OFF, because
collecting evidence must not wait for permission to trade.
"""
