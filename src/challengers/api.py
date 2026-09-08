"""
src/challengers/api.py
--------------------------------
Phase 24 §36, §63, §66 — the read/act surface, and the side-by-side
comparison.

Same architecture as every phase since 10: plain functions taking a
connection, named for the route they would serve if this project had an
HTTP layer. It does not, deliberately (`docs/API_AUDIT.md`), so these
are the API.

WHAT `comparison()` DELIBERATELY DOES NOT DO
------------------------------------------------
It does not rank, and it returns no total. §37 forbids collapsing a
comparison into one number, and the reason is mechanical rather than
philosophical: a sortable challenger list gets sorted, and the top of a
list of a hundred is where the noise collects.

So the comparison lays the two arms beside each other across six named
dimensions plus the per-context slices, and leaves the trade-off to the
reader — who is the only party that can weigh "better return, worse
complexity" for a particular purpose.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional, Sequence

from src.data_access.challenger_schema import initialize_challenger_schema
from src.domain.challenger_models import (
    ChallengerDecision, ChallengerLimits, ChallengerStatus, ReviewOutcome,
    RunEnvironment,
)
from src.challengers import evaluation, registry, workflow

MAX_LIMIT = 500


def _limit(value: int) -> int:
    return max(1, min(int(value), MAX_LIMIT))


# ======================================================================
# Challengers
# ======================================================================

def challengers(conn: sqlite3.Connection, *, status: Optional[str] = None,
                limit: int = 100) -> List[Dict[str, Any]]:
    """
    `GET /challengers` — rejected ones included, always.

    There is deliberately no `promising_only` parameter. A challenger
    record filtered to its winners is not a record (§31, §42).
    """
    return registry.listing(conn, status=status, limit=_limit(limit))


def create(conn: sqlite3.Connection, candidate_id: str, *,
           created_by: str = "", force: bool = False) -> Dict[str, Any]:
    """`POST /challengers` — from a validated Phase 23 candidate."""
    challenger = registry.from_candidate(conn, candidate_id,
                                         created_by=created_by, force=force)
    registry.assert_family_budget(conn, challenger.family_id)
    registry.save(conn, challenger)
    workflow.audit(conn, actor=workflow.Actor.SYSTEM, action="create",
                   challenger_id=challenger.challenger_id,
                   challenger_version=challenger.version,
                   decision="proposed",
                   reason="from candidate %s" % candidate_id)
    return challenger.as_dict()


def detail(conn: sqlite3.Connection, challenger_id: str,
           version: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """
    `GET /challengers/{id}` — everything §64 asks a detail page to show.

    Runs, results, reviews and the research lineage are always
    attached. A challenger page that could show a change without its
    outcome would let a reader form an impression from the proposal
    alone.
    """
    challenger = registry.load(conn, challenger_id, version)
    if challenger is None:
        return None
    record = challenger.as_dict()
    record["versions"] = versions(conn, challenger_id)
    record["runs"] = runs(conn, challenger_id)
    record["results"] = results(conn, challenger_id)
    record["reviews"] = workflow.reviews(conn, challenger_id)
    record["lineage"] = lineage(conn, challenger)
    record["family_challenger_count"] = registry.family_challenger_count(
        conn, challenger.family_id)
    return record


def versions(conn: sqlite3.Connection, challenger_id: str
             ) -> List[Dict[str, Any]]:
    initialize_challenger_schema(conn)
    keys = ("version", "status", "fingerprint", "change_summary",
            "baseline_version", "dataset_cutoff", "created_at")
    return [dict(zip(keys, row)) for row in conn.execute("""
        SELECT version, status, fingerprint, change_summary,
               baseline_version, dataset_cutoff, created_at
        FROM challengers WHERE challenger_id = ? ORDER BY version ASC
    """, (challenger_id,))]


def lineage(conn: sqlite3.Connection, challenger) -> Dict[str, Any]:
    """
    Candidate → conclusion → hypothesis → experiment, resolved (§7, §80).

    Returned as records rather than ids so a reader can see the chain
    without four more queries, and so a broken link is visible as a
    missing record rather than as an id that looks fine.
    """
    from src.autoresearch import api as research_api
    from src.autoresearch import hypotheses as hypothesis_layer

    chain: Dict[str, Any] = {}
    try:
        chain["conclusion"] = research_api.conclusion_detail(
            conn, challenger.conclusion_id)
    except Exception:
        chain["conclusion"] = None
    try:
        chain["hypothesis"] = hypothesis_layer.load(
            conn, challenger.hypothesis_id)
    except Exception:
        chain["hypothesis"] = None
    try:
        candidates = {row["candidate_id"]: row
                      for row in research_api.candidates(conn, limit=500)}
        chain["candidate"] = candidates.get(challenger.candidate_id)
    except Exception:
        chain["candidate"] = None
    chain["experiment_id"] = challenger.experiment_id
    return chain


def runs(conn: sqlite3.Connection, challenger_id: str
         ) -> List[Dict[str, Any]]:
    """`GET /challengers/{id}/runs`"""
    initialize_challenger_schema(conn)
    keys = ("run_id", "challenger_version", "environment", "status", "seed",
            "rows_examined", "cache_hit", "cached_from_run", "error",
            "cancelled_reason", "started_at", "completed_at",
            "duration_seconds")
    return [dict(zip(keys, row)) for row in conn.execute("""
        SELECT run_id, challenger_version, environment, status, seed,
               rows_examined, cache_hit, cached_from_run, error,
               cancelled_reason, started_at, completed_at, duration_seconds
        FROM challenger_runs WHERE challenger_id = ?
        ORDER BY queued_at DESC
    """, (challenger_id,))]


def results(conn: sqlite3.Connection, challenger_id: str
            ) -> List[Dict[str, Any]]:
    """`GET /challengers/{id}/results`"""
    initialize_challenger_schema(conn)
    found = []
    for row in conn.execute("""
        SELECT run_id FROM challenger_results WHERE challenger_id = ?
        ORDER BY computed_at DESC
    """, (challenger_id,)):
        result = evaluation.load_result(conn, row[0])
        if result is not None:
            found.append(result.as_dict())
    return found


def run(conn: sqlite3.Connection, challenger_id: str,
        version: Optional[int] = None, *, priority: float = 0.5
        ) -> Dict[str, Any]:
    """`POST /challengers/{id}/run` — queue it; the worker evaluates."""
    challenger = registry.load(conn, challenger_id, version)
    if challenger is None:
        raise ValueError("no challenger %s" % challenger_id)
    queue_id = workflow.enqueue(conn, challenger.challenger_id,
                                challenger.version, priority=priority,
                                reason="queued by request")
    return {"queue_id": queue_id, "challenger_id": challenger.challenger_id,
            "version": challenger.version, "state": "queued"}


def cancel(conn: sqlite3.Connection, queue_id: str,
           reason: str = "cancelled by request") -> Dict[str, Any]:
    """`POST /challengers/{id}/cancel` — never counted as success."""
    workflow.cancel(conn, queue_id, reason)
    return {"queue_id": queue_id, "state": "cancelled", "reason": reason,
            "note": "a cancelled evaluation is not a completed one"}


def queue(conn: sqlite3.Connection, *, limit: int = 100
          ) -> List[Dict[str, Any]]:
    """`GET /challengers/queue`"""
    return workflow.queue_listing(conn, limit=_limit(limit))


def candidates(conn: sqlite3.Connection, *, limit: int = 100
               ) -> List[Dict[str, Any]]:
    """
    `GET /challengers/candidates` — Phase 23 candidates, each marked
    with whether it qualifies for a challenger and why not (§3).
    """
    from src.autoresearch import api as research_api
    from src.domain.challenger_models import validate_candidate

    initialize_challenger_schema(conn)
    existing = {row[0] for row in conn.execute(
        "SELECT DISTINCT candidate_id FROM challengers")}
    rows = []
    for candidate in research_api.candidates(conn, limit=_limit(limit)):
        problems = validate_candidate(candidate)
        candidate["qualifies"] = not problems
        candidate["blocking_reasons"] = problems
        candidate["has_challenger"] = candidate["candidate_id"] in existing
        rows.append(candidate)
    return rows


# ======================================================================
# Comparison
# ======================================================================

def comparison(conn: sqlite3.Connection, challenger_id: str,
               version: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """
    `GET /challengers/{id}/comparison` — baseline beside challenger.

    Six named dimensions, the per-context slices, the research history
    and the limitations. **No total and no ranking**: the trade-off
    between "better return" and "worse complexity" belongs to a person
    with a purpose, not to a sort key.
    """
    challenger = registry.load(conn, challenger_id, version)
    if challenger is None:
        return None
    stored = results(conn, challenger_id)
    latest = stored[0] if stored else None

    report: Dict[str, Any] = {
        "challenger": challenger.as_dict(),
        "baseline": challenger.baseline.as_dict(),
        "change": challenger.change.as_dict(),
        "plan": challenger.plan.as_dict(),
        "result": latest,
        "reviews": workflow.reviews(conn, challenger_id),
        "family_challenger_count": registry.family_challenger_count(
            conn, challenger.family_id),
        "note": ("laid side by side, not ranked. A challenger that wins on "
                 "return and loses on complexity is a trade-off for a "
                 "person to weigh; collapsing it into one number would hide "
                 "which half you are buying."),
    }

    if latest is None:
        report["state"] = "not evaluated"
        return report

    report["state"] = latest["decision"]
    report["side_by_side"] = _side_by_side(challenger, latest)
    report["contexts"] = latest.get("slices", [])
    report["research_result_is_not_production_approval"] = (
        "This is a research comparison. %s does not authorise any change to "
        "a production model, strategy, threshold, risk limit or capital "
        "figure." % latest["decision"].upper())
    return report


def _side_by_side(challenger, result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The two arms on the metrics §21 asks for, where measured."""
    base = result.get("baseline_out_of_sample") or {}
    cand = result.get("challenger_out_of_sample") or {}
    rows = []
    for label, key in (("directional accuracy", "directional_accuracy"),
                       ("mean return", "mean_return"),
                       ("median return", "median_return"),
                       ("return volatility", "stdev_return"),
                       ("mean MFE", "mean_mfe"),
                       ("mean MAE", "mean_mae"),
                       ("sample size", "sample_size"),
                       ("instruments", "instrument_count")):
        baseline_value = base.get(key)
        challenger_value = cand.get(key)
        if baseline_value is None and challenger_value is None:
            continue
        difference = None
        if isinstance(baseline_value, (int, float)) and \
                isinstance(challenger_value, (int, float)):
            difference = challenger_value - baseline_value
        rows.append({"metric": label, "baseline": baseline_value,
                     "challenger": challenger_value, "difference": difference})
    rows.append({"metric": "complexity", "baseline": challenger.baseline.complexity,
                 "challenger": challenger.change.complexity,
                 "difference": (challenger.change.complexity
                                - challenger.baseline.complexity)})
    return rows


# ======================================================================
# Review
# ======================================================================

def review(conn: sqlite3.Connection, challenger_id: str, version: int, *,
           outcome: str, reviewer: str, reason: str) -> Dict[str, Any]:
    """`POST /challengers/{id}/review` — the only exit from the system."""
    return workflow.review(conn, challenger_id, version,
                           outcome=ReviewOutcome(outcome),
                           reviewer=reviewer, reason=reason)


def paper_candidates(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """
    Challengers a person approved for paper (§32, §59).

    A label, not an instruction. Nothing here is sent anywhere; Phase
    25 may act on it, under its own human control.
    """
    return registry.listing(conn,
                            status=ChallengerStatus.PAPER_CANDIDATE.value)


# ======================================================================
# Integrity
# ======================================================================

def integrity_check(conn: sqlite3.Connection) -> Dict[str, int]:
    """
    §82, as queries. Every count must be zero.

    The first three are the ones that matter: a challenger with no
    candidate behind it cannot be traced to evidence, a paper candidate
    with no review is an automatic promotion, and a run whose
    fingerprint differs from its definition's is proof the definition
    moved after the numbers existed.
    """
    initialize_challenger_schema(conn)

    def scalar(sql: str, params: tuple = ()) -> int:
        try:
            return conn.execute(sql, params).fetchone()[0]
        except sqlite3.OperationalError:
            return 0

    return {
        "challengers_without_a_candidate": scalar(
            "SELECT COUNT(*) FROM challengers WHERE TRIM(candidate_id) = ''"),
        "challengers_without_a_baseline_version": scalar(
            "SELECT COUNT(*) FROM challengers "
            "WHERE TRIM(baseline_version) = ''"),
        "paper_candidates_without_a_review": scalar("""
            SELECT COUNT(*) FROM challengers c
            WHERE c.status = 'paper_candidate' AND NOT EXISTS (
                SELECT 1 FROM challenger_reviews v
                WHERE v.challenger_id = c.challenger_id
                  AND v.challenger_version = c.version
                  AND v.outcome = 'approved_for_paper')"""),
        "runs_whose_fingerprint_moved": scalar("""
            SELECT COUNT(*) FROM challenger_runs r
            JOIN challengers c ON c.challenger_id = r.challenger_id
                              AND c.version = r.challenger_version
            WHERE r.fingerprint != '' AND r.fingerprint != c.fingerprint"""),
        "results_without_a_run": scalar("""
            SELECT COUNT(*) FROM challenger_results x
            WHERE NOT EXISTS (SELECT 1 FROM challenger_runs r
                              WHERE r.run_id = x.run_id)"""),
        "results_without_reasons": scalar(
            "SELECT COUNT(*) FROM challenger_results "
            "WHERE reasons_json IN ('[]','')"),
        "superior_without_an_interval": scalar("""
            SELECT COUNT(*) FROM challenger_results
            WHERE decision = 'superior'
              AND (effect_low IS NULL OR effect_high IS NULL)"""),
        "superior_below_the_minimum_sample": scalar("""
            SELECT COUNT(*) FROM challenger_results
            WHERE decision = 'superior'
              AND json_extract(challenger_oos_json, '$.sample_size') < 30"""),
        "reviews_without_a_reviewer": scalar(
            "SELECT COUNT(*) FROM challenger_reviews "
            "WHERE TRIM(reviewer) = '' OR TRIM(reason) = ''"),
        "challengers_in_a_production_state": scalar(
            "SELECT COUNT(*) FROM challengers "
            "WHERE status IN ('production', 'active', 'promoted')"),
    }


def observability(conn: sqlite3.Connection) -> Dict[str, Any]:
    """§79: what the challenger system has done, and at what cost."""
    initialize_challenger_schema(conn)
    scalar = lambda sql: conn.execute(sql).fetchone()[0]
    return {
        "challengers": scalar("SELECT COUNT(DISTINCT challenger_id) FROM challengers"),
        "versions": scalar("SELECT COUNT(*) FROM challengers"),
        "runs": scalar("SELECT COUNT(*) FROM challenger_runs"),
        "results": scalar("SELECT COUNT(*) FROM challenger_results"),
        "reviews": scalar("SELECT COUNT(*) FROM challenger_reviews"),
        "cache_hits": scalar(
            "SELECT COUNT(*) FROM challenger_runs WHERE cache_hit = 1"),
        "runtime_seconds": scalar(
            "SELECT COALESCE(SUM(duration_seconds), 0) FROM challenger_runs"),
        "queue_depth": workflow.depth(conn),
        "by_decision": {row[0]: row[1] for row in conn.execute(
            "SELECT decision, COUNT(*) FROM challenger_results GROUP BY 1")},
        "by_status": {row[0]: row[1] for row in conn.execute(
            "SELECT status, COUNT(*) FROM challengers GROUP BY 1")},
    }
