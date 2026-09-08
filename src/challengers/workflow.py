"""
src/challengers/workflow.py
-------------------------------------
Phase 24 §32-§35, §52-§55, §59 — the queue that runs challengers and the
human review that is the only way out of the system.

WHY QUEUE AND REVIEW LIVE TOGETHER
--------------------------------------
They are the two ends of one path: a challenger enters the queue, is
evaluated, and leaves only through a person. Splitting them across two
modules would hide that the exit is a human act.

THE QUEUE REUSES PHASE 23.5's ATOMIC CLAIM, NOT ITS CODE
------------------------------------------------------------
The Phase 23.5 audit found two live defects in the research queue: two
workers could select the same item, and a crashed worker left an item
RUNNING forever, never retried and never reported. The same two traps
exist here, so the same two answers are applied — a single conditional
UPDATE whose WHERE clause carries the expected state, and a reaper that
returns abandoned items with the reason recorded.

The helpers are re-implemented against the challenger tables rather
than imported, because Phase 23's are bound to `autoresearch_queue`.
The TECHNIQUE is shared; a generic queue abstraction over two tables
with different columns would be more code and less clarity.

THE EXIT IS A PERSON (§34, §35)
-----------------------------------
`ChallengerStatus` has no PRODUCTION or ACTIVE member, and
`ReviewOutcome` has no `approved_for_production`. The furthest this
phase reaches is PAPER_CANDIDATE, and only `review()` can set it —
which requires a named reviewer and a reason, both stored, in an
append-only table. There is no code path from a decision to a
deployment, and `promote_to_production` exists solely to refuse.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

from src.data_access.challenger_schema import initialize_challenger_schema
from src.domain.challenger_models import (
    CHALLENGER_METHOD_VERSION, Actor, ChallengerDecision, ChallengerLimits,
    ChallengerStatus, LimitExceeded, ReviewOutcome, _digest, utcnow,
)

#: A RUNNING item older than this is presumed abandoned. Generous
#: relative to an evaluation's own runtime, so a slow-but-alive run is
#: never reclaimed out from under itself.
STALE_AFTER_SECONDS = 3600


class ReviewRefused(Exception):
    """The review cannot be recorded as asked."""


class PromotionRefused(Exception):
    """Phase 24 cannot promote anything to production."""


# ======================================================================
# Audit
# ======================================================================

def audit(conn: sqlite3.Connection, *, actor: Actor, action: str,
          challenger_id: str = "", challenger_version: Optional[int] = None,
          run_id: Optional[str] = None, decision: str = "",
          reason: str = "") -> str:
    """One auditable challenger action (§69)."""
    initialize_challenger_schema(conn)
    audit_id = "cha-" + _digest({"a": action, "c": challenger_id,
                                 "v": challenger_version, "r": run_id,
                                 "t": utcnow()})[:20]
    conn.execute("""
        INSERT OR REPLACE INTO challenger_audit (
            audit_id, method_version, actor, action, challenger_id,
            challenger_version, run_id, decision, reason, occurred_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (audit_id, CHALLENGER_METHOD_VERSION, actor.value, action,
          challenger_id, challenger_version, run_id, decision,
          (reason or "")[:1000], utcnow()))
    conn.commit()
    return audit_id


def trail(conn: sqlite3.Connection, *, challenger_id: Optional[str] = None,
          limit: int = 100) -> List[Dict[str, Any]]:
    initialize_challenger_schema(conn)
    sql = """
        SELECT audit_id, actor, action, challenger_id, challenger_version,
               run_id, decision, reason, occurred_at
        FROM challenger_audit
    """
    params: tuple = ()
    if challenger_id:
        sql += " WHERE challenger_id = ?"
        params = (challenger_id,)
    sql += " ORDER BY occurred_at DESC LIMIT ?"
    keys = ("audit_id", "actor", "action", "challenger_id",
            "challenger_version", "run_id", "decision", "reason",
            "occurred_at")
    return [dict(zip(keys, row)) for row in conn.execute(sql, params + (limit,))]


# ======================================================================
# Queue
# ======================================================================

def _queue_id(challenger_id: str, version: int) -> str:
    return "chq-" + _digest({"c": challenger_id, "v": version})[:20]


def enqueue(conn: sqlite3.Connection, challenger_id: str, version: int, *,
            priority: float = 0.5, reason: str = "") -> str:
    """Queue a challenger for evaluation."""
    initialize_challenger_schema(conn)
    queue_id = _queue_id(challenger_id, version)
    conn.execute("""
        INSERT OR REPLACE INTO challenger_queue (
            queue_id, challenger_id, challenger_version, method_version,
            state, priority, reason, run_id, queued_at, started_at,
            finished_at
        ) VALUES (?,?,?,?,?,?,?,NULL,?,
                  (SELECT started_at FROM challenger_queue WHERE queue_id = ?),
                  (SELECT finished_at FROM challenger_queue WHERE queue_id = ?))
    """, (queue_id, challenger_id, version, CHALLENGER_METHOD_VERSION,
          "queued", priority, reason, utcnow(), queue_id, queue_id))
    conn.commit()
    return queue_id


def claim(conn: sqlite3.Connection, queue_id: str, *, reason: str = "") -> bool:
    """
    Atomically take ownership. True only if we got it (§53).

    A single conditional UPDATE, so SQLite decides the winner and
    exactly one caller sees `rowcount == 1`. Nothing depends on the
    caller checking first, which is the read-then-write pattern that
    let two Phase 23 workers select the same item.
    """
    initialize_challenger_schema(conn)
    cursor = conn.execute("""
        UPDATE challenger_queue
        SET state = 'running', started_at = ?, reason = ?
        WHERE queue_id = ? AND state = 'queued'
    """, (utcnow(), reason or "claimed", queue_id))
    conn.commit()
    return cursor.rowcount == 1


def reclaim_stale(conn: sqlite3.Connection, *,
                  older_than_seconds: int = STALE_AFTER_SECONDS
                  ) -> List[Dict[str, Any]]:
    """
    Return abandoned RUNNING items to the queue (§55).

    A worker that dies mid-evaluation would otherwise leave its item
    RUNNING forever: never retried, never reported, simply absent from
    the record. Reclaiming states why, so a reclaimed item is
    distinguishable from one that never started.
    """
    initialize_challenger_schema(conn)
    cutoff = (datetime.now(timezone.utc)
              - timedelta(seconds=older_than_seconds)).isoformat()
    stale = [{"queue_id": row[0], "challenger_id": row[1],
              "started_at": row[2]}
             for row in conn.execute("""
        SELECT queue_id, challenger_id, started_at FROM challenger_queue
        WHERE state = 'running'
          AND (started_at IS NULL OR started_at < ?)
    """, (cutoff,))]
    for item in stale:
        conn.execute("""
            UPDATE challenger_queue
            SET state = 'queued', started_at = NULL, reason = ?
            WHERE queue_id = ?
        """, ("reclaimed: still marked running since %s, longer than an "
              "evaluation can take. The worker that held it did not finish, "
              "so it returns to the queue rather than disappearing."
              % (item["started_at"] or "an unknown time"), item["queue_id"]))
    if stale:
        conn.commit()
    return stale


def finish(conn: sqlite3.Connection, queue_id: str, state: str, *,
           reason: str = "", run_id: Optional[str] = None) -> None:
    initialize_challenger_schema(conn)
    conn.execute("""
        UPDATE challenger_queue
        SET state = ?, reason = ?, finished_at = ?,
            run_id = COALESCE(?, run_id)
        WHERE queue_id = ?
    """, (state, reason, utcnow(), run_id, queue_id))
    conn.commit()


def cancel(conn: sqlite3.Connection, queue_id: str,
           reason: str = "cancelled by request") -> None:
    """
    §54: a cancelled evaluation is never successful.

    The item keeps its reason and is excluded from every completed
    count. Nothing is deleted — "we stopped this halfway" is
    information about how long the work takes.
    """
    finish(conn, queue_id, "cancelled", reason=reason)


def next_batch(conn: sqlite3.Connection, *,
               limits: Optional[ChallengerLimits] = None
               ) -> List[Dict[str, Any]]:
    """The highest-priority queued items this pass may run."""
    initialize_challenger_schema(conn)
    limits = limits or ChallengerLimits()
    keys = ("queue_id", "challenger_id", "challenger_version", "priority")
    return [dict(zip(keys, row)) for row in conn.execute("""
        SELECT queue_id, challenger_id, challenger_version, priority
        FROM challenger_queue WHERE state = 'queued'
        ORDER BY priority DESC, queued_at ASC LIMIT ?
    """, (limits.max_concurrent_jobs,))]


def depth(conn: sqlite3.Connection) -> Dict[str, int]:
    initialize_challenger_schema(conn)
    counts = {state: 0 for state in
              ("queued", "running", "completed", "failed", "cancelled",
               "requires_review")}
    for state, total in conn.execute(
            "SELECT state, COUNT(*) FROM challenger_queue GROUP BY 1"):
        counts[state] = total
    return counts


def queue_listing(conn: sqlite3.Connection, *, limit: int = 100
                  ) -> List[Dict[str, Any]]:
    initialize_challenger_schema(conn)
    keys = ("queue_id", "challenger_id", "challenger_version", "state",
            "priority", "reason", "run_id", "queued_at", "started_at",
            "finished_at", "name")
    return [dict(zip(keys, row)) for row in conn.execute("""
        SELECT q.queue_id, q.challenger_id, q.challenger_version, q.state,
               q.priority, q.reason, q.run_id, q.queued_at, q.started_at,
               q.finished_at, c.name
        FROM challenger_queue q
        LEFT JOIN challengers c ON c.challenger_id = q.challenger_id
                               AND c.version = q.challenger_version
        ORDER BY q.priority DESC, q.queued_at ASC LIMIT ?
    """, (limit,))]


# ======================================================================
# Runs a challenger through the queue
# ======================================================================

def run_queued(conn: sqlite3.Connection, *,
               limits: Optional[ChallengerLimits] = None,
               actor: Actor = Actor.SYSTEM) -> Dict[str, Any]:
    """
    Evaluate what is queued, within the limits, and stop.

    Reclaims abandoned items first (§55), claims atomically (§53), and
    always records why it stopped. There is no loop that calls itself
    and nothing scheduled on a timer.
    """
    from src.challengers import evaluation, registry

    initialize_challenger_schema(conn)
    limits = limits or ChallengerLimits()
    report: Dict[str, Any] = {"reclaimed": [], "evaluated": [],
                              "not_claimed": [], "failed": [],
                              "termination_reason": ""}

    for item in reclaim_stale(conn):
        report["reclaimed"].append(item["queue_id"])
        audit(conn, actor=actor, action="reclaim_stale",
              challenger_id=item["challenger_id"], decision="requeued",
              reason="left running since %s"
                     % (item["started_at"] or "an unknown time"))

    selected = next_batch(conn, limits=limits)
    if not selected:
        report["termination_reason"] = "nothing is queued"
        return report

    for item in selected:
        if not claim(conn, item["queue_id"], reason="claimed by run_queued"):
            report["not_claimed"].append(item["queue_id"])
            continue

        challenger = registry.load(conn, item["challenger_id"],
                                   item["challenger_version"])
        if challenger is None:
            finish(conn, item["queue_id"], "failed",
                   reason="the challenger definition no longer exists")
            report["failed"].append(item["queue_id"])
            continue

        completed = conn.execute("""
            SELECT COUNT(*) FROM challenger_runs
            WHERE challenger_id = ? AND status = 'completed'
        """, (challenger.challenger_id,)).fetchone()[0]
        if completed >= limits.max_runs_per_challenger:
            finish(conn, item["queue_id"], "cancelled",
                   reason="%d runs already completed, at the %d-run limit. "
                          "Re-running one challenger repeatedly is how a "
                          "comparison record manufactures a winner."
                          % (completed, limits.max_runs_per_challenger))
            continue

        registry.set_status(conn, challenger.challenger_id,
                            challenger.version, ChallengerStatus.VALIDATING)
        try:
            run, result = evaluation.evaluate(conn, challenger, limits=limits)
        except Exception as exc:
            finish(conn, item["queue_id"], "failed", reason=str(exc)[:400])
            audit(conn, actor=actor, action="evaluation_failed",
                  challenger_id=challenger.challenger_id,
                  challenger_version=challenger.version,
                  decision="failed", reason=str(exc)[:400])
            report["failed"].append(item["queue_id"])
            continue

        if result is None:
            # A run that produced nothing is not a finding. The lesson
            # Phase 23 paid for: an absent result interpreted as a
            # measurement reads as "we tested and could not tell".
            finish(conn, item["queue_id"], "failed",
                   reason="the evaluation produced no result (%s); no verdict "
                          "is drawn, because a comparison that did not happen "
                          "is not a comparison" % (run.get("error") or "unknown"),
                   run_id=run["run_id"])
            report["failed"].append(item["queue_id"])
            continue

        status = _status_for(result.decision)
        registry.set_status(conn, challenger.challenger_id,
                            challenger.version, status)
        finish(conn, item["queue_id"], "completed",
               reason="decided %s" % result.decision.value,
               run_id=run["run_id"])
        audit(conn, actor=actor, action="evaluate",
              challenger_id=challenger.challenger_id,
              challenger_version=challenger.version, run_id=run["run_id"],
              decision=result.decision.value,
              reason=result.reasons[0] if result.reasons else "")
        report["evaluated"].append({
            "challenger_id": challenger.challenger_id,
            "run_id": run["run_id"], "decision": result.decision.value,
            "effect": result.effect, "status": status.value})

    report["termination_reason"] = (
        "every claimed item finished; the %d-job concurrency limit was not "
        "exceeded" % limits.max_concurrent_jobs)
    return report


def _status_for(decision: ChallengerDecision) -> ChallengerStatus:
    """
    The status a verdict implies.

    SUPERIOR becomes PROMISING, never PAPER_CANDIDATE. Reaching paper
    requires a person (§34) — a system that promoted its own winners
    would be marking its own homework.
    """
    return {
        ChallengerDecision.SUPERIOR: ChallengerStatus.PROMISING,
        ChallengerDecision.INFERIOR: ChallengerStatus.REJECTED,
        ChallengerDecision.INCONCLUSIVE: ChallengerStatus.TESTING,
        ChallengerDecision.CONTEXT_DEPENDENT: ChallengerStatus.REQUIRES_REVIEW,
        ChallengerDecision.REQUIRES_REVIEW: ChallengerStatus.REQUIRES_REVIEW,
    }.get(decision, ChallengerStatus.REQUIRES_REVIEW)


# ======================================================================
# Human review — the only exit
# ======================================================================

def review(conn: sqlite3.Connection, challenger_id: str, version: int, *,
           outcome: ReviewOutcome, reviewer: str, reason: str,
           run_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Record a human decision (§34).

    `reviewer` and `reason` are required with no defaults, exactly as
    Phase 18's `promote()` requires an approver — an approval nobody
    signed is not an approval.

    APPROVED_FOR_PAPER is the furthest this can reach. It marks the
    challenger PAPER_CANDIDATE, which is a label meaning "a person
    agreed this is worth paper-trading". It executes nothing, sends
    nothing to IBKR, and changes no production setting.
    """
    from src.challengers import evaluation, registry

    initialize_challenger_schema(conn)
    if not reviewer.strip():
        raise ReviewRefused(
            "a review must name its reviewer. An approval nobody signed is "
            "not an approval.")
    if not reason.strip():
        raise ReviewRefused("a review must give a reason")

    challenger = registry.load(conn, challenger_id, version)
    if challenger is None:
        raise ReviewRefused("no challenger %s v%s exists"
                            % (challenger_id, version))

    if outcome is ReviewOutcome.APPROVED_FOR_PAPER:
        latest = conn.execute("""
            SELECT run_id, decision FROM challenger_results
            WHERE challenger_id = ? AND challenger_version = ?
            ORDER BY computed_at DESC LIMIT 1
        """, (challenger_id, version)).fetchone()
        if latest is None:
            raise ReviewRefused(
                "this challenger has never been evaluated, so there is "
                "nothing to approve. A paper candidate with no comparison "
                "behind it is a guess with a status.")
        if latest[1] == ChallengerDecision.INFERIOR.value:
            raise ReviewRefused(
                "the latest comparison found this challenger INFERIOR to its "
                "baseline. Approving it for paper would be overriding the "
                "evidence; if that is intended, record a new evaluation "
                "rather than a review that contradicts the last one.")

    review_id = "chv-" + _digest({"c": challenger_id, "v": version,
                                  "o": outcome.value, "t": utcnow()})[:20]
    conn.execute("""
        INSERT INTO challenger_reviews (
            review_id, challenger_id, challenger_version, run_id, outcome,
            reviewer, reason, evidence_json, reviewed_at
        ) VALUES (?,?,?,?,?,?,?,?,?)
    """, (review_id, challenger_id, version, run_id, outcome.value,
          reviewer, reason, json.dumps([]), utcnow()))

    status = {
        ReviewOutcome.APPROVED_FOR_PAPER: ChallengerStatus.PAPER_CANDIDATE,
        ReviewOutcome.REJECTED: ChallengerStatus.REJECTED,
        ReviewOutcome.DEFERRED: ChallengerStatus.REQUIRES_REVIEW,
    }[outcome]
    registry.set_status(conn, challenger_id, version, status)
    conn.commit()

    audit(conn, actor=Actor.HUMAN, action="review",
          challenger_id=challenger_id, challenger_version=version,
          run_id=run_id, decision=outcome.value,
          reason="%s: %s" % (reviewer, reason))

    return {"review_id": review_id, "outcome": outcome.value,
            "status": status.value, "reviewer": reviewer,
            "note": ("PAPER_CANDIDATE means a person agreed this is worth "
                     "paper-trading. Nothing has been executed and no "
                     "production setting has changed.")}


def reviews(conn: sqlite3.Connection, challenger_id: str
            ) -> List[Dict[str, Any]]:
    """
    Every review of this challenger, oldest first.

    Append-only: changing a decision means a second row, so the history
    of what was decided and when survives (§40, §69).
    """
    initialize_challenger_schema(conn)
    keys = ("review_id", "challenger_version", "run_id", "outcome",
            "reviewer", "reason", "reviewed_at")
    return [dict(zip(keys, row)) for row in conn.execute("""
        SELECT review_id, challenger_version, run_id, outcome, reviewer,
               reason, reviewed_at
        FROM challenger_reviews WHERE challenger_id = ?
        ORDER BY reviewed_at ASC
    """, (challenger_id,))]


def promote_to_production(conn: sqlite3.Connection, challenger_id: str
                          ) -> None:
    """
    Always refuses. Present so the refusal is findable (§35).

    Somebody looking for how a challenger reaches production will
    search for this name. Finding an explicit refusal with the reason
    is far better than finding nothing and assuming the mechanism lives
    somewhere they have not looked.
    """
    raise PromotionRefused(
        "Phase 24 cannot promote challenger %s, or any challenger, to "
        "production. The furthest this phase reaches is PAPER_CANDIDATE, "
        "which is a label a person applies and which executes nothing. "
        "Model promotion remains Phase 18's human-gated "
        "`scripts/promote_model.py`, requiring an approver and a reason."
        % challenger_id)
