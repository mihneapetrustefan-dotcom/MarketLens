"""
Phase 13 — Paper trading and real-time simulation.

Runs the real pipeline (features, models, signals, portfolio, risk)
against market data as it becomes available, and executes the resulting
order intents through a paper executor that prices against cached bars.

Nothing in this package connects to a broker, holds a credential, or
can place a real order. `PaperExecutor` is the only executor here, and
`ExecutionVenue.PAPER` is the only venue that exists.
"""
