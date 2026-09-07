"""
src/autoresearch/queue.py
-----------------------------------
Phase 23 §24, §25, §26, §52, §70 — the research queue, its scheduler,
and the budget that stops it running away.

THE BUDGET IS THE FEATURE
-----------------------------
An autonomous researcher without limits does not produce more research;
it produces more chances to be fooled. Every experiment is another
draw, and with enough draws something clears any threshold. §24 asks
for configurable limits and §52 for a termination condition; both are
here, and both REFUSE rather than truncate.

That distinction matters. A budget that silently trimmed a sweep from
twelve variants to five would answer a different question than the one
asked, and the reader could not tell which. Phase 22 made the same
choice for `max_rows`.

SCHEDULING IS ORDERING, NOT AUTOMATION
------------------------------------------
`next_batch` sorts and slices a queue. It does not start anything on a
timer, and there is no loop in this module that calls itself. §26 asks
for controlled scheduling and explicitly forbids infinite autonomous
loops, so the trigger stays outside: a human, or a pipeline stage with
a fixed budget.

DEPLETED FAMILIES ARE SKIPPED HERE, NOT DELETED
---------------------------------------------------
§65's dead ends and §66's reactivation meet in `next_batch`: an item
whose family is RESEARCH_DEPLETED is passed over with a reason and
stays in the queue. It is a scheduling decision, not a verdict that
the idea is false, and a later cycle with new evidence can pick it up.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.data_access.autoresearch_schema import initialize_autoresearch_schema
from src.domain.autoresearch_models import (
    RESEARCH_METHOD_VERSION, BudgetExceeded, FamilyStatus, QueueState,
    ResearchBudget, ResearchHypothesis, _digest, utcnow,
)
from src.autoresearch import prioritization


def _queue_id(hypothesis_id: str) -> str:
    return "qi-" + _digest({"hypothesis": hypothesis_id})[:20]


# ======================================================================
# Enqueue
# ======================================================================

def enqueue(conn: sqlite3.Connection, hypothesis: ResearchHypothesis, *,
            priority: float, cycle_id: Optional[str] = None,
            state: QueueState = QueueState.QUEUED,
            reason: str = "") -> str:
    """
    Put a hypothesis in the queue, or record why it was refused.

    A REJECTED item is still written. A queue that only holds work it
    intends to do cannot answer "why was this never tested", which is
    the question a research record most often needs to answer.
    """
    initialize_autoresearch_schema(conn)
    queue_id = _queue_id(hypothesis.hypothesis_id)
    conn.execute("""
        INSERT OR REPLACE INTO autoresearch_queue (
            queue_id, method_version, hypothesis_id, question_id, cycle_id,
            state, priority, reason, experiment_id, queued_at,
            started_at, finished_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,
                  (SELECT started_at FROM autoresearch_queue WHERE queue_id = ?),
                  (SELECT finished_at FROM autoresearch_queue WHERE queue_id = ?))
    """, (queue_id, RESEARCH_METHOD_VERSION, hypothesis.hypothesis_id,
          hypothesis.question_id, cycle_id, state.value, priority, reason,
          None, utcnow(), queue_id, queue_id))
    conn.commit()
    return queue_id


def set_state(conn: sqlite3.Connection, queue_id: str, state: QueueState, *,
              reason: str = "", experiment_id: Optional[str] = None) -> None:
    initialize_autoresearch_schema(conn)
    stamp = utcnow()
    if state is QueueState.RUNNING:
        conn.execute("""
            UPDATE autoresearch_queue SET state=?, reason=?, started_at=?
            WHERE queue_id=?
        """, (state.value, reason, stamp, queue_id))
    elif state in (QueueState.COMPLETED, QueueState.REJECTED,
                   QueueState.CANCELLED):
        conn.execute("""
            UPDATE autoresearch_queue
            SET state=?, reason=?, finished_at=?,
                experiment_id=COALESCE(?, experiment_id)
            WHERE queue_id=?
        """, (state.value, reason, stamp, experiment_id, queue_id))
    else:
        conn.execute("""
            UPDATE autoresearch_queue SET state=?, reason=? WHERE queue_id=?
        """, (state.value, reason, queue_id))
    conn.commit()


def cancel(conn: sqlite3.Connection, queue_id: str,
           reason: str = "cancelled by request") -> None:
    """
    §25: cancellation is explicit and keeps whatever was learned.

    Nothing is deleted. A cancelled item records that it was cancelled
    and why, because "we stopped this halfway" is information about how
    long the work takes.
    """
    set_state(conn, queue_id, QueueState.CANCELLED, reason=reason)


# ======================================================================
# Claiming and recovery
# ======================================================================

#: A RUNNING item older than this is presumed abandoned. Generous
#: relative to a cycle's own runtime budget, so a slow-but-alive run is
#: never reclaimed out from under itself.
STALE_AFTER_SECONDS = 3600


def claim(conn: sqlite3.Connection, queue_id: str, *,
          cycle_id: Optional[str] = None, reason: str = "") -> bool:
    """
    Atomically take ownership of a queued item. True if we got it.

    WHY THIS EXISTS
    -------------------
    `next_batch` only READS. Two workers calling it before either
    marked anything RUNNING both selected the same item -- verified
    directly, and §10 requires that no experiment execute twice.

    The transition is a single conditional UPDATE whose WHERE clause
    includes the expected state, so SQLite decides the winner: exactly
    one caller sees `rowcount == 1`. Nothing here depends on the
    caller checking first, which is the pattern that produced the race.

    Today `max_concurrent_jobs` is 1 and nothing runs cycles in
    parallel, so this changes no behaviour. It is the difference
    between "safe" and "safe until somebody adds a second worker".
    """
    initialize_autoresearch_schema(conn)
    cursor = conn.execute("""
        UPDATE autoresearch_queue
        SET state = ?, started_at = ?, reason = ?,
            cycle_id = COALESCE(?, cycle_id)
        WHERE queue_id = ? AND state IN ('queued', 'prioritized')
    """, (QueueState.RUNNING.value, utcnow(),
          reason or "claimed", cycle_id, queue_id))
    conn.commit()
    return cursor.rowcount == 1


def reclaim_stale(conn: sqlite3.Connection, *,
                  older_than_seconds: int = STALE_AFTER_SECONDS
                  ) -> List[Dict[str, Any]]:
    """
    Return abandoned RUNNING items to the queue (§59).

    A worker that dies mid-run leaves its item RUNNING forever:
    `next_batch` only considers queued and prioritized, so the item is
    never retried and never reported -- it simply stops existing as far
    as the research programme is concerned. Verified directly.

    Reclaiming records WHY, so a reader can tell a reclaimed item from
    one that was never started. Nothing is deleted and no result is
    invented; the item goes back to QUEUED and will be re-run, which is
    safe because every id downstream is derived from the hypothesis
    rather than from the attempt.
    """
    initialize_autoresearch_schema(conn)
    cutoff = (datetime.now(timezone.utc)
              - timedelta(seconds=older_than_seconds)).isoformat()
    stale = [{"queue_id": row[0], "hypothesis_id": row[1],
              "started_at": row[2]}
             for row in conn.execute("""
        SELECT queue_id, hypothesis_id, started_at
        FROM autoresearch_queue
        WHERE state = 'running'
          AND (started_at IS NULL OR started_at < ?)
    """, (cutoff,))]

    for item in stale:
        conn.execute("""
            UPDATE autoresearch_queue
            SET state = ?, reason = ?, started_at = NULL
            WHERE queue_id = ?
        """, (QueueState.QUEUED.value,
              "reclaimed: still marked running since %s, which is longer "
              "than a cycle can take. The worker that held it did not "
              "finish, so the item returns to the queue rather than "
              "disappearing." % (item["started_at"] or "an unknown time"),
              item["queue_id"]))
    if stale:
        conn.commit()
    return stale


# ======================================================================
# Scheduling
# ======================================================================

def next_batch(conn: sqlite3.Connection, *, budget: ResearchBudget
               ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    The highest-priority items this cycle may run (§26).

    Returns `(selected, skipped)`. Both halves are returned because a
    scheduler that silently drops work is indistinguishable from one
    that has none, and the skip reasons are where §65's dead ends and
    §24's per-family cap become visible.
    """
    initialize_autoresearch_schema(conn)
    rows = conn.execute("""
        SELECT q.queue_id, q.hypothesis_id, q.priority, h.family_id,
               h.statement, h.claim_fingerprint
        FROM autoresearch_queue q
        JOIN autoresearch_hypotheses h
          ON h.hypothesis_id = q.hypothesis_id
        WHERE q.state IN ('queued', 'prioritized')
        ORDER BY q.priority DESC, q.queued_at ASC
    """).fetchall()

    selected: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    family_counts: Dict[str, int] = {}
    seen_claims: set = set()

    for queue_id, hypothesis_id, priority, family_id, statement, claim in rows:
        item = {"queue_id": queue_id, "hypothesis_id": hypothesis_id,
                "priority": priority, "family_id": family_id,
                "statement": statement}

        if claim in seen_claims:
            item["reason"] = (
                "another item in this same batch makes an identical claim; "
                "running both would count one piece of evidence twice")
            skipped.append(item)
            continue

        if family_id:
            stats = prioritization.family_statistics(conn, family_id)
            status, why = prioritization.assess_family(stats)
            if status is FamilyStatus.RESEARCH_DEPLETED:
                item["reason"] = "family is research-depleted: " + why
                skipped.append(item)
                continue
            already = conn.execute("""
                SELECT COUNT(*) FROM autoresearch_hypotheses h
                JOIN autoresearch_conclusions c
                  ON c.hypothesis_id = h.hypothesis_id
                WHERE h.family_id = ?
            """, (family_id,)).fetchone()[0]
            planned = family_counts.get(family_id, 0)
            if already + planned >= budget.max_repeats_per_family:
                item["reason"] = (
                    "family has already been tested %d times, at the "
                    "%d-per-family cap. Testing the same idea repeatedly is "
                    "how a research record manufactures a false positive."
                    % (already + planned, budget.max_repeats_per_family))
                skipped.append(item)
                continue
            family_counts[family_id] = planned + 1

        if len(selected) >= budget.max_experiments_per_cycle:
            item["reason"] = (
                "the cycle budget of %d experiments is already committed; "
                "this stays queued for the next cycle"
                % budget.max_experiments_per_cycle)
            skipped.append(item)
            continue

        seen_claims.add(claim)
        selected.append(item)

    return selected, skipped


def experiments_today(conn: sqlite3.Connection, day: str) -> int:
    """How many experiments this researcher has already run today."""
    initialize_autoresearch_schema(conn)
    return conn.execute("""
        SELECT COUNT(*) FROM autoresearch_queue
        WHERE state = 'completed' AND finished_at LIKE ?
    """, (day[:10] + "%",)).fetchone()[0]


def assert_daily_budget(conn: sqlite3.Connection, *, budget: ResearchBudget,
                        day: str) -> None:
    """§24: refuse rather than quietly exceed the daily allowance."""
    used = experiments_today(conn, day)
    if used >= budget.max_experiments_per_day:
        raise BudgetExceeded(
            "%d experiments have already run on %s, at the daily limit of "
            "%d. The limit exists because every additional test is another "
            "chance for noise to clear the bar."
            % (used, day[:10], budget.max_experiments_per_day))


def depth(conn: sqlite3.Connection) -> Dict[str, int]:
    """Queue depth by state — one of §76's observability numbers."""
    initialize_autoresearch_schema(conn)
    counts = {state.value: 0 for state in QueueState}
    for state, n in conn.execute(
            "SELECT state, COUNT(*) FROM autoresearch_queue GROUP BY 1"):
        counts[state] = n
    return counts


def listing(conn: sqlite3.Connection, *, state: Optional[str] = None,
            limit: int = 100) -> List[Dict[str, Any]]:
    initialize_autoresearch_schema(conn)
    sql = """
        SELECT q.queue_id, q.hypothesis_id, q.question_id, q.state,
               q.priority, q.reason, q.experiment_id, q.queued_at,
               q.started_at, q.finished_at, h.statement, h.family_name
        FROM autoresearch_queue q
        LEFT JOIN autoresearch_hypotheses h
               ON h.hypothesis_id = q.hypothesis_id
    """
    params: Tuple[Any, ...] = ()
    if state:
        sql += " WHERE q.state = ?"
        params = (state,)
    sql += " ORDER BY q.priority DESC, q.queued_at ASC LIMIT ?"
    params = params + (limit,)
    keys = ("queue_id", "hypothesis_id", "question_id", "state", "priority",
            "reason", "experiment_id", "queued_at", "started_at",
            "finished_at", "statement", "family_name")
    return [dict(zip(keys, row)) for row in conn.execute(sql, params)]
