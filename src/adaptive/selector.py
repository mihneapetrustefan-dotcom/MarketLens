"""
src/adaptive/selector.py
-------------------------------------
The session universe selector, end to end (Phase 25.9I-B).

WHAT IT DOES
----------------
Parent universe -> as-of history -> eligibility -> score -> verdict ->
capacity -> a `SessionSelection`. One pass, no hidden state, and nothing
it produces reaches capture, Portfolio, Risk or a broker. The output is
a record of a decision, not an instruction.

AS-OF IS THE WHOLE DISCIPLINE
---------------------------------
`select(as_of=...)` bounds every read at that instant through
`MarketHistory`, which opens the database read-only and puts
`timestamp <= as_of` in every query. A pre-open selection therefore
cannot see the session's own close, and a replay of a past date selects
from what was knowable then. The no-lookahead tests exist because this
guarantee is worth exactly as much as its weakest query.

REVISIONS APPEND
--------------------
`revise()` returns a NEW selection carrying `previous_snapshot_id`.
Intraday admission consumes FREE capacity only: this phase never evicts
a held instrument to make room, because eviction without a thesis is
churn with extra steps, and 25.9I-E is where a thesis arrives. Newly
admitted instruments are flagged `requires_market_data_activation` --
the honest statement that the system now wants data it does not have.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

from src.adaptive.admission import (
    AdmissionConfig, CandidateAssessment, ChurnPolicy, OpportunityAdmitter,
    SessionSelection,
)
from src.adaptive.eligibility import EligibilityConfig, EligibilityEvaluator
from src.adaptive.scoring import ScoringConfig, SessionScorer
from src.adaptive.universe import MarketHistory, ParentUniverse
from src.domain.adaptive_models import (
    AdmissionVerdict, DecisionLineage, SessionUniverse, require_utc,
)


def _snapshot_id(session_date: str, as_of: datetime, revision: int) -> str:
    """Deterministic: the same session, instant and revision re-derives it."""
    digest = hashlib.sha256(
        f"{session_date}|{as_of.isoformat()}|{revision}".encode("utf-8"))
    return "su-" + digest.hexdigest()[:16]


class SessionUniverseSelector:
    """Composes the parent universe, the filters, the scorer and admission."""

    def __init__(self, parent: ParentUniverse, *,
                 eligibility: Optional[EligibilityConfig] = None,
                 scoring: Optional[ScoringConfig] = None,
                 admission: Optional[AdmissionConfig] = None,
                 churn: Optional[ChurnPolicy] = None):
        self.parent = parent
        self.eligibility_config = eligibility or EligibilityConfig()
        self.scoring_config = scoring or ScoringConfig()
        self.admission_config = admission or AdmissionConfig()
        self.evaluator = EligibilityEvaluator(self.eligibility_config)
        self.scorer = SessionScorer(self.scoring_config)
        self.admitter = OpportunityAdmitter(self.admission_config, churn)

    # ---------------- assessment ----------------

    def assess(self, history: MarketHistory, as_of: datetime, *,
               session_close: Optional[datetime] = None,
               instruments: Optional[Sequence[str]] = None
               ) -> List[CandidateAssessment]:
        """Judge every parent member (or a named subset) at `as_of`."""
        as_of = require_utc(as_of, "as_of")
        benchmark = None
        if self.parent.benchmarks:
            benchmark = history.history(
                self.parent.benchmarks[0].instrument_id)

        wanted = set(instruments) if instruments is not None else None
        assessments = []
        for member in self.parent.members:
            if wanted is not None and member.instrument_id not in wanted:
                continue
            candles = history.history(member.instrument_id)
            eligibility = self.evaluator.evaluate(member, candles, as_of)
            score = (self.scorer.score(member, candles, as_of, benchmark=benchmark)
                     if eligibility.is_eligible else None)
            assessments.append(self.admitter.judge(
                member, eligibility, score, as_of, session_close=session_close))
        return assessments

    # ---------------- selection ----------------

    def select(self, history: MarketHistory, as_of: datetime, *,
               session_date: Optional[str] = None,
               session_close: Optional[datetime] = None,
               lineage_id: Optional[str] = None) -> SessionSelection:
        as_of = require_utc(as_of, "as_of")
        session_date = session_date or as_of.date().isoformat()

        assessments = self.assess(history, as_of, session_close=session_close)
        selected, waitlist, decisions = self.admitter.fill_capacity(
            assessments, as_of)

        admitted = set(selected)
        assessments = tuple(
            replace(a, admitted_at=as_of) if a.instrument_id in admitted else a
            for a in assessments)

        universe = SessionUniverse(
            session_date=session_date,
            max_capacity=self.admission_config.max_capacity,
            members=tuple(selected), selected_at=as_of,
            parent_universe_size=len(self.parent.members),
            method_version=self.admission_config.version)

        snapshot_id = _snapshot_id(session_date, as_of, 0)
        return SessionSelection(
            snapshot_id=snapshot_id, session_date=session_date, as_of=as_of,
            revision=0, universe=universe, assessments=assessments,
            admissions=tuple(decisions), waitlist=tuple(waitlist),
            benchmarks=tuple(b.instrument_id for b in self.parent.benchmarks),
            parent_universe_version=self.parent.version,
            parent_universe_sha256=self.parent.members_sha256,
            eligibility_config_version=self.eligibility_config.version,
            scoring_config_version=self.scoring_config.version,
            admission_config_version=self.admission_config.version,
            lineage=DecisionLineage(
                lineage_id=lineage_id or snapshot_id,
                session_date=session_date, universe_decision_id=snapshot_id,
                created_at=as_of))

    # ---------------- intraday revision ----------------

    def revise(self, previous: SessionSelection, history: MarketHistory,
               as_of: datetime, *, candidates: Optional[Sequence[str]] = None,
               session_close: Optional[datetime] = None) -> SessionSelection:
        """
        Admit into FREE capacity later in the session. Never evicts.

        Expired members are dropped first -- an opportunity whose
        validity ended does not keep its slot merely because nothing
        replaced it -- and the freed capacity becomes available to the
        new candidates.
        """
        as_of = require_utc(as_of, "as_of")
        from src.adaptive.admission import expired_assessments

        expired = dict(expired_assessments(previous, as_of))
        held = [i for i in previous.universe.members if i not in expired]

        fresh = self.assess(history, as_of, session_close=session_close,
                            instruments=candidates)
        fresh_ids = {a.instrument_id for a in fresh}

        # An expired assessment may not re-admit itself. Carrying it
        # forward as a PASS would let a slot be reclaimed on the strength
        # of a judgement that has already been declared out of date --
        # which would make expiry decorative. Re-entry requires a fresh
        # assessment at this revision's as_of, like any other candidate.
        carried = []
        for assessment in previous.assessments:
            if assessment.instrument_id in fresh_ids:
                continue
            if assessment.is_expired_at(as_of):
                deadline = assessment.validity.deadline()
                carried.append(replace(
                    assessment, verdict=AdmissionVerdict.REJECT,
                    reasons=(f"validity expired at "
                             f"{deadline.isoformat() if deadline else 'session end'}; "
                             f"re-entry needs a fresh assessment",)))
            else:
                carried.append(assessment)
        carried = tuple(carried)
        assessments = tuple(sorted(carried + tuple(fresh),
                                   key=lambda a: a.instrument_id))

        selected, waitlist, decisions = self.admitter.fill_capacity(
            assessments, as_of, already_held=held)

        newly_admitted = set(selected) - set(previous.universe.members)
        assessments = tuple(
            replace(a, requires_market_data_activation=True, admitted_at=as_of)
            if a.instrument_id in newly_admitted else a
            for a in assessments)

        universe = SessionUniverse(
            session_date=previous.session_date,
            max_capacity=self.admission_config.max_capacity,
            members=tuple(selected), selected_at=as_of,
            parent_universe_size=len(self.parent.members),
            method_version=self.admission_config.version)

        revision = previous.revision + 1
        snapshot_id = _snapshot_id(previous.session_date, as_of, revision)
        lineage = (previous.lineage.with_step(universe_decision_id=snapshot_id)
                   if previous.lineage else None)
        return SessionSelection(
            snapshot_id=snapshot_id, session_date=previous.session_date,
            as_of=as_of, revision=revision, universe=universe,
            assessments=assessments, admissions=tuple(decisions),
            waitlist=tuple(waitlist), benchmarks=previous.benchmarks,
            parent_universe_version=self.parent.version,
            parent_universe_sha256=self.parent.members_sha256,
            eligibility_config_version=self.eligibility_config.version,
            scoring_config_version=self.scoring_config.version,
            admission_config_version=self.admission_config.version,
            lineage=lineage, previous_snapshot_id=previous.snapshot_id)


# ======================================================================
# Shadow comparison against the operational capture universe
# ======================================================================


def compare_with_fixed(selection: SessionSelection,
                       fixed_instrument_ids: Sequence[str]) -> Dict[str, object]:
    """
    What the dynamic selector would change, stated without judgement.

    Deliberately reports no performance claim. Whether the difference is
    an improvement is a question about future returns, and this phase
    does not have them -- using them would be the lookahead it forbids.
    """
    fixed = set(fixed_instrument_ids)
    dynamic = set(selection.universe.members)
    reasons = {}
    for instrument_id in sorted(fixed - dynamic):
        assessment = selection.assessment(instrument_id)
        reasons[instrument_id] = (
            "; ".join(assessment.reasons) if assessment and assessment.reasons
            else ("not in the parent universe" if assessment is None
                  else "passed but lost the capacity competition"))
    return {
        "fixed_count": len(fixed),
        "dynamic_count": len(dynamic),
        "overlap": sorted(fixed & dynamic),
        "overlap_count": len(fixed & dynamic),
        "added_by_dynamic": sorted(dynamic - fixed),
        "dropped_by_dynamic": sorted(fixed - dynamic),
        "drop_reasons": reasons,
        "unused_capacity": selection.unused_capacity,
        "sector_distribution": selection.sector_distribution(),
        "note": "architecture and selection integrity only; this comparison "
                "makes no claim about which universe performs better",
    }
