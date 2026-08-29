"""
src/signals/engine.py
--------------------------
Orchestration for Phase 10: strategies produce candidates, the
validator turns them into signals, and this engine handles identity,
deduplication, superseding and persistence.

WHY AN ENGINE RATHER THAN CALLING THE PIECES DIRECTLY
---------------------------------------------------------
The strategy does not know about the database. The validator does not
know about previously stored signals. Neither should: a strategy that
could query history could accidentally condition on it, and a
validator that could read past signals would make validation depend on
call order.

The engine is the only place that sees both, which keeps the
duplicate/supersede decision in exactly one auditable location.

DEDUPLICATION AND SUPERSEDING ARE DIFFERENT THINGS (spec §22, §15)
----------------------------------------------------------------------
DUPLICATE: the same strategy, same instrument, same information state.
Nothing new was learned, so nothing new is stored. The existing signal
is returned unchanged. This is what makes the generator safe to call
repeatedly — an endpoint hit a hundred times produces one signal.

SUPERSEDED: the same strategy and instrument, but a NEWER information
state. Something genuinely new was learned. A new signal is stored and
the previous one is marked superseded, pointing forward. The old row's
claim is never altered — only its lifecycle pointer.

Confusing the two would either flood the table with identical rows or
silently erase the history of what was believed and when.

SUPPRESSED SIGNALS ARE STORED TOO (spec §23)
------------------------------------------------
A suppressed signal is a record of the system declining to act, and
that is data. Storing only successes would make the engine's own
selectivity invisible.

NOTHING HERE DECIDES ANYTHING TRADEABLE
-------------------------------------------
The engine emits signals. It does not size, allocate, route or
execute. Phase 11 consumes what this produces.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

from src.data_access.signal_repository import SignalRepository, compute_identity_hash
from src.domain.signal_models import Signal, SignalCandidate, SignalStatus
from src.signals.strategy import GenerationContext, SignalStrategy
from src.signals.validator import SignalValidator

logger = logging.getLogger("marketlens.signals.engine")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


@dataclass
class GenerationReport:
    """
    What one generation run did. Every candidate is accounted for —
    the counts must add up, which is how a silent drop would be caught.
    """
    candidates_generated: int = 0
    signals_created: int = 0
    signals_suppressed: int = 0
    duplicates_skipped: int = 0
    signals_superseded: int = 0
    strategies_run: int = 0
    suppression_reasons: Dict[str, int] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    @property
    def accounted_for(self) -> int:
        """Created + suppressed + duplicates should equal candidates."""
        return self.signals_created + self.signals_suppressed + self.duplicates_skipped

    @property
    def is_balanced(self) -> bool:
        return self.accounted_for == self.candidates_generated


class SignalEngine:
    """Runs strategies over contexts and persists the resulting signals."""

    def __init__(self, repository: SignalRepository,
                 validator: Optional[SignalValidator] = None,
                 persist_suppressed: bool = True):
        self.repository = repository
        self.validator = validator or SignalValidator()
        #: Suppressed signals are stored by default. Turning this off
        #: is supported for dry-run inspection, never as a way to hide
        #: them in production.
        self.persist_suppressed = persist_suppressed

    def _identity_for(self, signal: Signal) -> str:
        p = signal.provenance
        return compute_identity_hash(
            p.strategy_id or "", p.strategy_version or "",
            p.configuration_version or "v1", signal.instrument_id,
            p.source_information_cutoff)

    def _find_superseded(self, signal: Signal) -> Optional[Signal]:
        """
        The most recent ACTIVE signal from the same strategy for the
        same instrument, built from OLDER information.

        Older information only: a signal built from newer information
        must never be superseded by one built from older, which could
        otherwise happen when backfilling out of chronological order.
        """
        cutoff = signal.provenance.source_information_cutoff
        if cutoff is None:
            return None
        rows = self.repository.conn.execute("""
            SELECT signal_id FROM signals
            WHERE instrument_id = ? AND strategy_id = ? AND status = ?
              AND source_information_cutoff IS NOT NULL
              AND source_information_cutoff < ?
            ORDER BY source_information_cutoff DESC LIMIT 1
        """, (signal.instrument_id, signal.provenance.strategy_id,
              SignalStatus.ACTIVE.value, cutoff.isoformat())).fetchall()
        return self.repository.get(rows[0][0]) if rows else None

    def process_candidate(self, candidate: SignalCandidate,
                          report: GenerationReport,
                          now: Optional[datetime] = None,
                          apply: bool = True) -> Optional[Signal]:
        """
        Validate one candidate and store the result, unless it is a
        duplicate of an already-recorded claim.

        Returns the resulting signal, or the existing one when this was
        a duplicate.
        """
        signal = self.validator.validate(candidate, now=now)
        identity = self._identity_for(signal)

        existing = self.repository.find_by_identity(identity)
        if existing is not None:
            report.duplicates_skipped += 1
            return existing

        is_suppressed = signal.status == SignalStatus.SUPPRESSED
        if is_suppressed:
            report.signals_suppressed += 1
            for reason in signal.suppression_reasons:
                report.suppression_reasons[reason.value] = (
                    report.suppression_reasons.get(reason.value, 0) + 1)
        else:
            report.signals_created += 1

        if not apply:
            return signal
        if is_suppressed and not self.persist_suppressed:
            return signal

        # Supersede only when this signal is itself actionable: a
        # suppressed signal represents the system declining to speak,
        # which must not silence a previous, valid view.
        superseded = None if is_suppressed else self._find_superseded(signal)

        self.repository.save(signal)
        if superseded is not None:
            self.repository.supersede(superseded.signal_id, signal.signal_id)
            report.signals_superseded += 1

        return signal

    def run(self, strategies: Sequence[SignalStrategy],
            contexts: Sequence[GenerationContext],
            now: Optional[datetime] = None,
            apply: bool = True) -> GenerationReport:
        """
        Run every strategy over every context.

        A strategy that raises is logged and skipped for that context
        rather than aborting the batch — one broken strategy must not
        prevent the others from producing signals (spec §40). The
        failure is recorded in the report, not swallowed.
        """
        report = GenerationReport(strategies_run=len(strategies))

        for strategy in strategies:
            if not strategy.definition.is_active:
                report.notes.append(
                    f"strategy {strategy.definition.qualified_id} is inactive — skipped")
                continue

            for context in contexts:
                try:
                    candidates = strategy.generate(context)
                except Exception as exc:  # noqa: BLE001 — isolate one failure
                    logger.error("Strategy %s failed on %s: %s",
                                 strategy.definition.qualified_id,
                                 context.instrument_id, exc)
                    report.notes.append(
                        f"{strategy.definition.qualified_id} failed on "
                        f"{context.instrument_id}: {exc}")
                    continue

                for candidate in candidates:
                    report.candidates_generated += 1
                    self.process_candidate(candidate, report, now=now, apply=apply)

        if not report.is_balanced:
            report.notes.append(
                f"ACCOUNTING MISMATCH: {report.candidates_generated} candidates but "
                f"{report.accounted_for} accounted for — a candidate was lost")

        return report

    def expire_stale(self, moment: Optional[datetime] = None) -> int:
        """Move ACTIVE signals past their validity into EXPIRED."""
        return self.repository.expire_before(moment or datetime.now(timezone.utc))
