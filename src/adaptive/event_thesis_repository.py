"""
src/adaptive/event_thesis_repository.py
-----------------------------------------------------
Persisting event assessments and the consumption ledger
(Phase 25.9I-F, spec sections 12, 34).

NAMED FOR WHAT IT HOLDS, NOT FOR WHAT IT IS
-----------------------------------------------
`src/data_access/event_repository.py` already exists and reads the
pre-fusion `events` table. This one stores the relationship between an
EVENT and a THESIS, which is a different thing, so it carries a
different name. Two repositories called EventRepository in one codebase
is a bug waiting for a busy afternoon.

THE LEDGER IS THE POINT
---------------------------
Everything except consumption can be recomputed. Consumption is
history: if it is not written down, the system reacts to the same
acquisition every time the crawler re-runs, and a position gets revised
three times for one piece of news.

`load_history` rebuilds the ledger for a thesis so the engine can
answer NEW / ALREADY_ASSESSED / UPDATED / MATERIAL_REVISION without
holding state between runs.

APPEND-ONLY, IDEMPOTENT
---------------------------
Content-derived ids and `INSERT OR IGNORE`. Re-running the same
assessment writes zero new rows. Nothing is updated after writing, so
the 09:20 assessment stays readable after the 14:05 revision replaced
it -- which is what makes a rumour-to-filing sequence auditable.

READS NOTHING IT SHOULD NOT
-------------------------------
No order, intent, fill, position or execution table is touched. No
credential is stored. The module imports no execution, trading or
portfolio package.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

from src.adaptive.event_reassessment import (
    ConsumptionRecord, EventReassessmentOutcome,
)
from src.data_access.adaptive_event_schema import (
    initialize_adaptive_event_schema,
)
from src.domain.event_position_models import PositionEventImpactAssessment


def _iso(moment: Optional[datetime]) -> Optional[str]:
    return moment.isoformat() if moment else None


class EventThesisRepository:
    """Append-only storage for event/thesis assessments and their ledger."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        initialize_adaptive_event_schema(conn)

    # ---------------- writing ----------------

    def save_outcome(self, outcome: EventReassessmentOutcome) -> Dict[str, int]:
        """
        Persist one outcome. Idempotent; returns what was actually new.

        The assessment, the consumption link and the reassessment
        request are written in one transaction, because a link written
        without its assessment would claim a thesis had reacted to
        something no row explains.
        """
        assessment = outcome.assessment
        written = {"assessments": 0, "links": 0, "requests": 0}

        confidence = assessment.confidence
        recommendation = outcome.recommendation
        cursor = self.conn.execute(
            "INSERT OR IGNORE INTO adaptive_position_event_assessments ("
            "assessment_id, canonical_event_id, event_revision_key, thesis_id, "
            "instrument_id, as_of, relation, participation_role, "
            "thesis_relevance, assumption_impacts, direction, "
            "evidence_direction, materiality, materiality_score, "
            "conflict_state, urgency, consumption_status, "
            "confidence_entity_match, confidence_extraction, confidence_fact, "
            "confidence_materiality, confidence_thesis_impact, "
            "evidence_completeness, corroboration_state, "
            "source_independence_known, strengthens_thesis, weakens_thesis, "
            "invalidates_thesis, target_reestimation_required, "
            "horizon_reestimation_required, partial_exit_review_required, "
            "data_integrity_review_required, integrity_concerns, "
            "requested_market_data_state, is_stale, is_executable, reasons, "
            "previous_assessment_id, evidence_id, recommendation_id, "
            "recommendation, lineage_id, session_snapshot_id, "
            "allocation_plan_id) VALUES (" + ",".join("?" * 44) + ")",
            (assessment.assessment_id, assessment.canonical_event_id,
             assessment.event_revision_key, assessment.thesis_id,
             assessment.instrument_id, _iso(assessment.as_of),
             assessment.relevance.relation.value,
             (assessment.relevance.participation_role.value
              if assessment.relevance.participation_role else ""),
             assessment.relevance.thesis_relevance.value,
             json.dumps([[name, impact.value] for name, impact
                         in assessment.relevance.assumption_impacts]),
             assessment.direction.value,
             assessment.evidence_direction().value,
             assessment.materiality.value, assessment.materiality_score,
             assessment.conflict_state.value, assessment.urgency.value,
             assessment.consumption_status.value,
             confidence.entity_match, confidence.event_extraction,
             confidence.fact, confidence.materiality, confidence.thesis_impact,
             assessment.evidence_completeness,
             assessment.corroboration_state.value,
             int(assessment.source_independence_known),
             int(assessment.strengthens_thesis), int(assessment.weakens_thesis),
             int(assessment.invalidates_thesis),
             int(assessment.target_reestimation_required),
             int(assessment.horizon_reestimation_required),
             int(assessment.partial_exit_review_required),
             int(assessment.data_integrity_review_required),
             json.dumps([c.value for c in assessment.integrity_concerns]),
             assessment.requested_market_data_state.value,
             int(assessment.is_stale),
             0,                      # is_executable: a stored constant
             json.dumps(list(assessment.reasons)),
             assessment.previous_assessment_id,
             outcome.evidence.evidence_id if outcome.evidence else None,
             getattr(recommendation, "recommendation_id", None),
             outcome.action.value if outcome.action else "",
             assessment.lineage_id, assessment.session_snapshot_id,
             assessment.allocation_plan_id))
        written["assessments"] += cursor.rowcount or 0

        request = outcome.reassessment_request
        if request is not None:
            cursor = self.conn.execute(
                "INSERT OR IGNORE INTO adaptive_event_reassessment_requests ("
                "request_id, canonical_event_id, thesis_id, instrument_id, "
                "as_of, urgency, requested_market_data_state, assessment_id, "
                "is_executable, reasons, lineage_id) "
                "VALUES (?,?,?,?,?,?,?,?,0,?,?)",
                (request.request_id, request.canonical_event_id,
                 request.thesis_id, request.instrument_id, _iso(request.as_of),
                 request.urgency.value,
                 request.requested_market_data_state.value,
                 request.assessment_id, json.dumps(list(request.reasons)),
                 request.lineage_id))
            written["requests"] += cursor.rowcount or 0

        self.conn.commit()
        return written

    def record_consumption(self, assessment: PositionEventImpactAssessment, *,
                           lifecycle_state: str = "",
                           corroboration_state: str = "",
                           total_report_count: int = 0,
                           has_contradictions: bool = False) -> int:
        """
        Mark this revision as seen by this thesis.

        Stores the MATERIAL fields alongside the revision key so the
        next run can tell a material revision from another syndicated
        copy without rehydrating the whole assessment.
        """
        cursor = self.conn.execute(
            "INSERT OR IGNORE INTO adaptive_event_thesis_links ("
            "canonical_event_id, thesis_id, event_revision_key, assessment_id, "
            "assessed_at, consumption_status, lifecycle_state, "
            "corroboration_state, total_report_count, has_contradictions) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (assessment.canonical_event_id, assessment.thesis_id,
             assessment.event_revision_key, assessment.assessment_id,
             _iso(assessment.as_of), assessment.consumption_status.value,
             lifecycle_state, corroboration_state or
             assessment.corroboration_state.value,
             int(total_report_count), int(has_contradictions)))
        self.conn.commit()
        return cursor.rowcount or 0

    # ---------------- reading ----------------

    def load_history(self, thesis_id: Optional[str] = None,
                     canonical_event_id: Optional[str] = None
                     ) -> Tuple[ConsumptionRecord, ...]:
        """
        The consumption ledger, for the engine's novelty check.

        Ordered deterministically by (assessed_at, assessment_id) so a
        replay picks the same "latest" record every time.
        """
        clauses: List[str] = []
        params: List[object] = []
        if thesis_id:
            clauses.append("thesis_id = ?")
            params.append(thesis_id)
        if canonical_event_id:
            clauses.append("canonical_event_id = ?")
            params.append(canonical_event_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        try:
            rows = self.conn.execute(
                "SELECT canonical_event_id, thesis_id, event_revision_key, "
                "assessment_id, assessed_at, lifecycle_state, "
                "corroboration_state, total_report_count, has_contradictions "
                "FROM adaptive_event_thesis_links" + where
                + " ORDER BY assessed_at, assessment_id", params).fetchall()
        except sqlite3.DatabaseError:
            return ()
        records: List[ConsumptionRecord] = []
        for row in rows:
            try:
                assessed_at = datetime.fromisoformat(str(row[4]))
            except (TypeError, ValueError):
                continue
            records.append(ConsumptionRecord(
                canonical_event_id=str(row[0]), thesis_id=str(row[1]),
                revision_key=str(row[2]), assessment_id=str(row[3]),
                assessed_at=assessed_at, lifecycle_state=str(row[5] or ""),
                corroboration_state=str(row[6] or ""),
                total_report_count=int(row[7] or 0),
                has_contradictions=bool(row[8])))
        return tuple(records)

    def assessment_chain(self, canonical_event_id: str, thesis_id: str
                         ) -> Tuple[Dict[str, object], ...]:
        """
        Every assessment for one event/thesis pair, oldest first.

        This is the revision lineage a reader walks to answer "what did
        we believe about this event, and when did that change".
        """
        rows = self.conn.execute(
            "SELECT assessment_id, previous_assessment_id, event_revision_key, "
            "as_of, consumption_status, direction, materiality, urgency, "
            "invalidates_thesis, recommendation "
            "FROM adaptive_position_event_assessments "
            "WHERE canonical_event_id = ? AND thesis_id = ? "
            "ORDER BY as_of, assessment_id",
            (canonical_event_id, thesis_id)).fetchall()
        return tuple({
            "assessment_id": row[0], "previous_assessment_id": row[1],
            "event_revision_key": row[2], "as_of": row[3],
            "consumption_status": row[4], "direction": row[5],
            "materiality": row[6], "urgency": row[7],
            "invalidates_thesis": bool(row[8]), "recommendation": row[9],
        } for row in rows)

    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for table in ("adaptive_position_event_assessments",
                      "adaptive_event_thesis_links",
                      "adaptive_event_reassessment_requests"):
            try:
                out[table] = self.conn.execute(
                    f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.DatabaseError:
                out[table] = 0
        return out

    def executable_rows(self) -> int:
        """
        How many stored rows claim to be executable. Must always be 0.

        A query rather than an assertion in code: the guarantee is about
        what is in the database, so the database is what gets asked.
        """
        total = 0
        for table in ("adaptive_position_event_assessments",
                      "adaptive_event_reassessment_requests"):
            total += self.conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE is_executable != 0"
            ).fetchone()[0]
        return total
