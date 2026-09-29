"""
src/adaptive/thesis_repository.py
----------------------------------------------
Storing theses, revisions and exit recommendations (Phase 25.9I-E).

Three properties, the same ones 25.9I-B and 25.9I-D hold to: writing the
same record twice writes it once, a revision never erases its
predecessor, and nothing outside `adaptive_*` is touched.

`is_executable` is stored as a literal 0 on every recommendation row.
That is not decoration: a future reader of this table must be able to
see, in the data itself, that these rows were never orders.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.adaptive.thesis import ThesisAssessment
from src.data_access.adaptive_thesis_schema import (
    initialize_adaptive_thesis_schema,
)
from src.domain.thesis_models import PositionThesis, PositionThesisRevision


def _stamp(moment: Optional[datetime]) -> str:
    return (moment or datetime.now(timezone.utc)).astimezone(
        timezone.utc).isoformat()


class AdaptiveThesisRepository:
    """Append-only store for position theses and their history."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        initialize_adaptive_thesis_schema(conn)

    # ---------------- writing ----------------

    def save_thesis(self, thesis: PositionThesis,
                    written_at: Optional[datetime] = None) -> bool:
        """Persist one revision of a thesis. True when it was new."""
        horizon = thesis.horizon
        cursor = self.conn.execute(
            "INSERT OR IGNORE INTO adaptive_position_theses (thesis_id, "
            "instrument_id, direction, status, revision, previous_thesis_id, "
            "created_at, as_of, updated_at, entry_price, entry_notional, "
            "target_low, target_high, invalidation_low, invalidation_high, "
            "horizon_calendar_days, horizon_expires_at, confidence, "
            "initial_confidence, remaining_fraction, realized_fraction, "
            "initial_summary, current_summary, risk_assumptions, "
            "event_assumptions, regime_assumptions, evidence_references, "
            "session_snapshot_id, admission_decision_id, allocation_plan_id, "
            "lineage_id, position_id, written_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (thesis.thesis_id, thesis.instrument_id, thesis.direction.value,
             thesis.status.value, thesis.revision, thesis.previous_thesis_id,
             thesis.created_at.isoformat(),
             thesis.as_of.isoformat() if thesis.as_of else None,
             thesis.updated_at.isoformat() if thesis.updated_at else None,
             thesis.entry_price, thesis.entry_notional,
             thesis.target_zone.low, thesis.target_zone.high,
             thesis.invalidation_zone.low, thesis.invalidation_zone.high,
             horizon.calendar_days,
             horizon.expires_at.isoformat() if horizon.expires_at else None,
             thesis.confidence, thesis.initial_confidence,
             thesis.remaining_fraction, thesis.realized_fraction,
             thesis.initial_summary, thesis.current_summary,
             json.dumps(list(thesis.risk_assumptions)),
             json.dumps(list(thesis.event_assumptions)),
             json.dumps(list(thesis.regime_assumptions)),
             json.dumps(list(thesis.evidence_references)),
             thesis.session_snapshot_id, thesis.admission_decision_id,
             thesis.allocation_plan_id, thesis.lineage_id, thesis.position_id,
             _stamp(written_at)))
        self.conn.commit()
        return cursor.rowcount > 0

    def save_revision(self, revision: PositionThesisRevision,
                      written_at: Optional[datetime] = None) -> bool:
        """Persist a transition. Both theses are stored too, if new."""
        self.save_thesis(revision.previous, written_at)
        self.save_thesis(revision.revised, written_at)
        cursor = self.conn.execute(
            "INSERT OR IGNORE INTO adaptive_thesis_revisions (revision_id, "
            "previous_thesis_id, revised_thesis_id, instrument_id, reason, at, "
            "extends_target, tightens_protection, previous_target_low, "
            "previous_target_high, revised_target_low, revised_target_high, "
            "previous_invalidation_low, previous_invalidation_high, "
            "revised_invalidation_low, revised_invalidation_high, "
            "previous_remaining_fraction, revised_remaining_fraction, evidence, "
            "note, written_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (revision.revision_id, revision.previous.thesis_id,
             revision.revised.thesis_id, revision.previous.instrument_id,
             revision.reason.value, revision.at.isoformat(),
             1 if revision.extends_target else 0,
             1 if revision.tightens_protection else 0,
             revision.previous.target_zone.low, revision.previous.target_zone.high,
             revision.revised.target_zone.low, revision.revised.target_zone.high,
             revision.previous.invalidation_zone.low,
             revision.previous.invalidation_zone.high,
             revision.revised.invalidation_zone.low,
             revision.revised.invalidation_zone.high,
             revision.previous.remaining_fraction,
             revision.revised.remaining_fraction,
             json.dumps(list(revision.evidence)), revision.note,
             _stamp(written_at)))
        self.conn.commit()
        return cursor.rowcount > 0

    def save_assessment(self, assessment: ThesisAssessment,
                        written_at: Optional[datetime] = None) -> bool:
        """Persist a recommendation, always marked non-executable."""
        if assessment.revision is not None:
            self.save_revision(assessment.revision, written_at)
        recommendation = assessment.recommendation
        release = assessment.capital_release
        cursor = self.conn.execute(
            "INSERT OR IGNORE INTO adaptive_exit_recommendations "
            "(recommendation_id, thesis_id, instrument_id, action, at, "
            "exit_fraction, required_market_data_state, activation_reason, "
            "freshness_max_age_seconds, requires_realtime_marker, "
            "released_fraction, released_notional, reasons, blocked_by_policy, "
            "revision_id, is_executable, written_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (recommendation.recommendation_id, assessment.thesis_id,
             recommendation.instrument_id, recommendation.action.value,
             recommendation.at.isoformat(), recommendation.exit_fraction,
             assessment.required_state.value,
             assessment.activation_reason.value
             if assessment.activation_reason else None,
             assessment.freshness.max_age_seconds,
             1 if assessment.freshness.requires_realtime_marker else 0,
             release.released_fraction if release else None,
             release.released_notional if release else None,
             json.dumps(list(assessment.reasons)),
             json.dumps(list(assessment.blocked_by_policy)),
             assessment.revision.revision_id if assessment.revision else None,
             0,                                  # never executable, by record
             _stamp(written_at)))
        self.conn.commit()
        return cursor.rowcount > 0

    # ---------------- reading ----------------

    def thesis(self, thesis_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT thesis_id, instrument_id, status, revision, "
            "previous_thesis_id, target_low, target_high, invalidation_low, "
            "invalidation_high, remaining_fraction, realized_fraction, as_of "
            "FROM adaptive_position_theses WHERE thesis_id = ?",
            (thesis_id,)).fetchone()
        if row is None:
            return None
        return {"thesis_id": row[0], "instrument_id": row[1], "status": row[2],
                "revision": row[3], "previous_thesis_id": row[4],
                "target": (row[5], row[6]), "invalidation": (row[7], row[8]),
                "remaining_fraction": row[9], "realized_fraction": row[10],
                "as_of": row[11]}

    def history(self, instrument_id: str) -> List[Dict[str, Any]]:
        """Every revision for one instrument, oldest first."""
        return [{"thesis_id": r[0], "revision": r[1], "status": r[2],
                 "remaining_fraction": r[3], "previous_thesis_id": r[4]}
                for r in self.conn.execute(
                    "SELECT thesis_id, revision, status, remaining_fraction, "
                    "previous_thesis_id FROM adaptive_position_theses "
                    "WHERE instrument_id = ? ORDER BY revision, thesis_id",
                    (instrument_id,))]

    def revisions(self, instrument_id: str) -> List[Dict[str, Any]]:
        return [{"revision_id": r[0], "reason": r[1], "at": r[2],
                 "extends_target": bool(r[3]),
                 "previous_thesis_id": r[4], "revised_thesis_id": r[5]}
                for r in self.conn.execute(
                    "SELECT revision_id, reason, at, extends_target, "
                    "previous_thesis_id, revised_thesis_id "
                    "FROM adaptive_thesis_revisions WHERE instrument_id = ? "
                    "ORDER BY at, revision_id", (instrument_id,))]

    def recommendations(self, thesis_id: str) -> List[Dict[str, Any]]:
        return [{"recommendation_id": r[0], "action": r[1],
                 "exit_fraction": r[2], "required_state": r[3],
                 "is_executable": bool(r[4]), "reasons": json.loads(r[5] or "[]")}
                for r in self.conn.execute(
                    "SELECT recommendation_id, action, exit_fraction, "
                    "required_market_data_state, is_executable, reasons "
                    "FROM adaptive_exit_recommendations WHERE thesis_id = ? "
                    "ORDER BY at, recommendation_id", (thesis_id,))]
