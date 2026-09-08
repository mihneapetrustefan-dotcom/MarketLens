"""
src/trading — the Phase 25 paper-trading operating loop.

The package that turns a set of working subsystems into a system: one
bounded, restart-safe cycle that carries a signal from the signal table
to a reconciled broker position and back into memory.

Nothing here reimplements a layer. Phase 11 decides, Phase 14 validates
and submits, Phase 15 talks to IBKR, Phases 19-21 measure and remember.
This package calls them in order, records what each one did, and
refuses to continue when any of them cannot answer.
"""
