"""
src/autoresearch/audit.py
-----------------------------------
Phase 23 §77, §81 — every research action, by whom, and why.

WHAT AN AUDIT ROW IS FOR
----------------------------
Not blame. The question an audit trail answers here is "how did this
conclusion come to exist", and the useful version of that answer names
the actor, the decision, and the reason — because the actor is
sometimes a human overriding the system, and a record that flattens
that into "the system decided" is worse than no record.

`Actor` has three members. Two are in use: HUMAN and SYSTEM. LLM
exists and is never written by this phase, because no LLM is used
(§47 permits one, does not require one). The member stays so that if
one is added, its actions are distinguishable from the deterministic
ones from the first row rather than retrofitted.

WHAT IS NOT STORED (§81)
----------------------------
No chain-of-thought. If an LLM is ever introduced, §81 is explicit:
store the concise decision rationale and the structured evidence, not
the reasoning transcript. The schema has `reason` and `evidence_json`
and nothing shaped like a transcript, so the storage makes the rule
hard to break by accident.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional, Sequence

from src.data_access.autoresearch_schema import initialize_autoresearch_schema
from src.domain.autoresearch_models import (
    RESEARCH_METHOD_VERSION, Actor, _digest, utcnow,
)

#: A hard ceiling on how much prose one audit row may carry. Long
#: enough for a real reason, short enough that nobody is tempted to
#: paste a reasoning transcript into it (§81).
MAX_REASON = 1000


def record(conn: sqlite3.Connection, *, actor: Actor, action: str,
           question_id: str = "", hypothesis_id: str = "",
           experiment_id: str = "", cycle_id: Optional[str] = None,
           decision: str = "", reason: str = "",
           evidence: Optional[Sequence[Dict[str, Any]]] = None) -> str:
    """One auditable research action."""
    initialize_autoresearch_schema(conn)
    audit_id = "aud-" + _digest({
        "a": action, "h": hypothesis_id, "q": question_id,
        "e": experiment_id, "t": utcnow()})[:20]
    conn.execute("""
        INSERT OR REPLACE INTO autoresearch_audit (
            audit_id, method_version, actor, action, question_id,
            hypothesis_id, experiment_id, cycle_id, decision, reason,
            evidence_json, occurred_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, (audit_id, RESEARCH_METHOD_VERSION, actor.value, action,
          question_id, hypothesis_id, experiment_id, cycle_id, decision,
          (reason or "")[:MAX_REASON],
          json.dumps(list(evidence or [])), utcnow()))
    conn.commit()
    return audit_id


def trail(conn: sqlite3.Connection, *, limit: int = 100,
          actor: Optional[str] = None,
          hypothesis_id: Optional[str] = None) -> List[Dict[str, Any]]:
    initialize_autoresearch_schema(conn)
    clauses: List[str] = []
    params: List[Any] = []
    if actor:
        clauses.append("actor = ?")
        params.append(actor)
    if hypothesis_id:
        clauses.append("hypothesis_id = ?")
        params.append(hypothesis_id)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    keys = ("audit_id", "actor", "action", "question_id", "hypothesis_id",
            "experiment_id", "cycle_id", "decision", "reason", "occurred_at")
    return [dict(zip(keys, row)) for row in conn.execute("""
        SELECT audit_id, actor, action, question_id, hypothesis_id,
               experiment_id, cycle_id, decision, reason, occurred_at
        FROM autoresearch_audit %s ORDER BY occurred_at DESC LIMIT ?
    """ % where, tuple(params))]


def activity_summary(conn: sqlite3.Connection) -> Dict[str, Any]:
    """
    §76, §81: what the researcher has actually been doing.

    Counts by actor and by action. The LLM row will read zero for as
    long as no LLM is used, which is the point: the absence is
    measured rather than asserted.
    """
    initialize_autoresearch_schema(conn)
    by_actor = {row[0]: row[1] for row in conn.execute(
        "SELECT actor, COUNT(*) FROM autoresearch_audit GROUP BY 1")}
    by_action = {row[0]: row[1] for row in conn.execute(
        "SELECT action, COUNT(*) FROM autoresearch_audit GROUP BY 1 "
        "ORDER BY 2 DESC")}
    return {
        "by_actor": {actor.value: by_actor.get(actor.value, 0)
                     for actor in Actor},
        "by_action": by_action,
        "total": sum(by_actor.values()),
    }
