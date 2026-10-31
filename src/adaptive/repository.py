"""
src/adaptive/repository.py
---------------------------------------
Reading and writing session-universe decisions (Phase 25.9I-B).

IDEMPOTENT BY CONSTRUCTION
------------------------------
Every id is derived from the decision's own content, so writing the same
selection twice writes the same rows twice and the second write changes
nothing. That is not a convenience: a selector re-run after a crash must
not double-count a decision, and a comparison between two runs must be
able to assume that identical inputs produced identical records.

HISTORY IS NEVER OVERWRITTEN
--------------------------------
A revision is a new snapshot row with its own id and a
`previous_snapshot_id`. Nothing updates a prior revision's members or
decisions, so the 09:20 universe stays readable after 14:05 replaced it.

WHAT IT REFUSES TO TOUCH
----------------------------
Only the five `adaptive_*` tables. No order, intent, fill, position or
research table is read or written here, and the module imports nothing
from the execution or trading packages.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.adaptive.admission import SessionSelection
from src.adaptive.universe import ParentUniverse
from src.data_access.adaptive_universe_schema import (
    initialize_adaptive_universe_schema,
)
from src.domain.adaptive_models import AdmissionVerdict


class AdaptiveUniverseRepository:
    """Durable store for parent versions, snapshots and decisions."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        initialize_adaptive_universe_schema(conn)

    # ---------------- parent universe ----------------

    def register_parent_universe(self, parent: ParentUniverse,
                                 registered_at: Optional[datetime] = None) -> bool:
        """
        Record a parent-universe version. True when it was new.

        A version already present is left exactly as it was: the artifact
        is immutable, so a second registration of the same version is a
        no-op rather than an update.
        """
        stamp = (registered_at or datetime.now(timezone.utc)).astimezone(
            timezone.utc).isoformat()
        cursor = self.conn.execute(
            "INSERT OR IGNORE INTO adaptive_parent_universe_versions "
            "(version, as_of, members_sha256, member_count, sector_count, rule, "
            "source, registered_at) VALUES (?,?,?,?,?,?,?,?)",
            (parent.version, parent.as_of.isoformat(), parent.members_sha256,
             len(parent.members), len(parent.sectors), parent.rule,
             parent.source, stamp))
        self.conn.commit()
        return cursor.rowcount > 0

    def parent_versions(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT version, as_of, members_sha256, member_count, sector_count "
            "FROM adaptive_parent_universe_versions ORDER BY version").fetchall()
        return [{"version": r[0], "as_of": r[1], "members_sha256": r[2],
                 "member_count": r[3], "sector_count": r[4]} for r in rows]

    # ---------------- snapshots ----------------

    def save_selection(self, selection: SessionSelection,
                       created_at: Optional[datetime] = None) -> bool:
        """
        Persist one revision. True when it was new, False when already stored.

        Written in one transaction: a snapshot without its members would
        be a record nobody could interpret.
        """
        stamp = (created_at or datetime.now(timezone.utc)).astimezone(
            timezone.utc).isoformat()
        cursor = self.conn.execute(
            "INSERT OR IGNORE INTO adaptive_session_snapshots (snapshot_id, "
            "session_date, as_of, revision, max_capacity, selected_count, "
            "unused_capacity, waitlist_count, content_hash, "
            "parent_universe_version, parent_universe_sha256, "
            "eligibility_config_version, scoring_config_version, "
            "admission_config_version, lineage_id, previous_snapshot_id, "
            "benchmarks, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (selection.snapshot_id, selection.session_date,
             selection.as_of.isoformat(), selection.revision,
             selection.universe.max_capacity, selection.universe.occupied,
             selection.unused_capacity, len(selection.waitlist),
             selection.content_hash(), selection.parent_universe_version,
             selection.parent_universe_sha256,
             selection.eligibility_config_version,
             selection.scoring_config_version,
             selection.admission_config_version,
             selection.lineage.lineage_id if selection.lineage else None,
             selection.previous_snapshot_id,
             json.dumps(list(selection.benchmarks)), stamp))
        is_new = cursor.rowcount > 0

        selected = set(selection.universe.members)
        for assessment in selection.assessments:
            if assessment.instrument_id in selected:
                self.conn.execute(
                    "INSERT OR IGNORE INTO adaptive_session_members (snapshot_id, "
                    "instrument_id, sector_id, score_overall, score_completeness, "
                    "requires_market_data_activation, admitted_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (selection.snapshot_id, assessment.instrument_id,
                     assessment.sector_id,
                     assessment.score.overall if assessment.score else None,
                     assessment.score.completeness if assessment.score else None,
                     1 if assessment.requires_market_data_activation else 0,
                     assessment.admitted_at.isoformat()
                     if assessment.admitted_at else None))

        outcomes = {d.instrument_id: d for d in selection.admissions}
        for assessment in selection.assessments:
            decision = outcomes.get(assessment.instrument_id)
            decision_id = (decision.decision_id if decision else
                           "asm-%s@%s" % (assessment.instrument_id,
                                          selection.snapshot_id))
            score = assessment.score
            self.conn.execute(
                "INSERT OR IGNORE INTO adaptive_admission_decisions (decision_id, "
                "snapshot_id, instrument_id, as_of, verdict, outcome, "
                "eligibility_status, eligibility_reasons, score_overall, "
                "score_completeness, score_components, unavailable_components, "
                "reasons, capacity_at_decision, occupied_at_decision) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (decision_id, selection.snapshot_id, assessment.instrument_id,
                 assessment.as_of.isoformat(), assessment.verdict.value,
                 decision.outcome.value if decision else None,
                 assessment.eligibility.status.value,
                 json.dumps(list(assessment.eligibility.reasons)),
                 score.overall if score else None,
                 score.completeness if score else None,
                 json.dumps(score.components if score else {}, sort_keys=True),
                 json.dumps(list(score.unavailable) if score else []),
                 json.dumps(list(assessment.reasons)),
                 decision.capacity_at_decision if decision else None,
                 decision.occupied_at_decision if decision else None))
        self.conn.commit()
        return is_new

    def record_expiries(self, snapshot_id: str,
                        expiries: Sequence[Tuple[str, str]],
                        at: datetime) -> int:
        """Record why members stopped being valid. Idempotent per (id, at)."""
        stamp = at.astimezone(timezone.utc).isoformat()
        written = 0
        for instrument_id, reason in expiries:
            cursor = self.conn.execute(
                "INSERT OR IGNORE INTO adaptive_expiry_events (snapshot_id, "
                "instrument_id, at, reason) VALUES (?,?,?,?)",
                (snapshot_id, instrument_id, stamp, reason))
            written += cursor.rowcount
        self.conn.commit()
        return written

    # ---------------- reading ----------------

    def snapshot(self, snapshot_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT snapshot_id, session_date, as_of, revision, max_capacity, "
            "selected_count, unused_capacity, waitlist_count, content_hash, "
            "previous_snapshot_id FROM adaptive_session_snapshots "
            "WHERE snapshot_id = ?", (snapshot_id,)).fetchone()
        if row is None:
            return None
        members = [r[0] for r in self.conn.execute(
            "SELECT instrument_id FROM adaptive_session_members "
            "WHERE snapshot_id = ? ORDER BY instrument_id", (snapshot_id,))]
        return {"snapshot_id": row[0], "session_date": row[1], "as_of": row[2],
                "revision": row[3], "max_capacity": row[4],
                "selected_count": row[5], "unused_capacity": row[6],
                "waitlist_count": row[7], "content_hash": row[8],
                "previous_snapshot_id": row[9], "members": members}

    def revisions(self, session_date: str) -> List[Dict[str, Any]]:
        """Every revision of one session, oldest first. History intact."""
        rows = self.conn.execute(
            "SELECT snapshot_id, revision, as_of, selected_count, "
            "unused_capacity, previous_snapshot_id FROM adaptive_session_snapshots "
            "WHERE session_date = ? ORDER BY revision", (session_date,)).fetchall()
        return [{"snapshot_id": r[0], "revision": r[1], "as_of": r[2],
                 "selected_count": r[3], "unused_capacity": r[4],
                 "previous_snapshot_id": r[5]} for r in rows]

    def decisions(self, snapshot_id: str,
                  verdict: Optional[AdmissionVerdict] = None
                  ) -> List[Dict[str, Any]]:
        sql = ("SELECT instrument_id, verdict, outcome, eligibility_status, "
               "score_overall, reasons FROM adaptive_admission_decisions "
               "WHERE snapshot_id = ?")
        params: List[Any] = [snapshot_id]
        if verdict is not None:
            sql += " AND verdict = ?"
            params.append(verdict.value)
        sql += " ORDER BY instrument_id"
        return [{"instrument_id": r[0], "verdict": r[1], "outcome": r[2],
                 "eligibility_status": r[3], "score_overall": r[4],
                 "reasons": json.loads(r[5] or "[]")}
                for r in self.conn.execute(sql, params)]
