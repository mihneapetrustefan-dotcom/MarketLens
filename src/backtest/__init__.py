"""
Phase 12 — Backtesting, market simulation and strategy evaluation.

Reconstructs the full historical decision chain: information at T,
features, prediction, signal, portfolio context, risk decision,
allocation, simulated execution, portfolio state.

Nothing in this package connects to a broker or executes anything. The
only executor implemented is SimulationExecutor, which fills against
cached historical bars.
"""
