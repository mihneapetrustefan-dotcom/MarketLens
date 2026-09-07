"""
src/autoresearch/api.py
---------------------------------
Phase 23 §82 — the read/act surface for the research layer.

Same architecture as every phase since 10: plain functions taking a
connection, named for the route they would serve if this project had
an HTTP layer. It does not, deliberately — `docs/API_AUDIT.md` records
that as an architectural choice rather than an omission — so these are
the API.

WHAT IS NOT HERE
--------------------
No promotion, no production mutation, no delete. `conclusions()`
returns the negative results alongside the positive ones and there is
no parameter that filters the failures out, because a "successes only"
view is the single most misleading thing this module could offer.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional, Sequence

from src.data_access.autoresearch_schema import initialize_autoresearch_schema
from src.domain.autoresearch_models import RESEARCH_METHOD_VERSION
from src.autoresearch import (
    audit, candidates as candidate_registry, cycle as cycle_layer, governance,
    hypotheses as hypothesis_layer, prioritization, questions as question_layer,
    queue as queue_layer,
)

MAX_LIMIT = 500


def _limit(value: int) -> int:
    return max(1, min(int(value), MAX_LIMIT))


def _decode(record: Dict[str, Any]) -> Dict[str, Any]:
    for key in list(record):
        if key.endswith("_json"):
            try:
                record[key[:-5]] = json.loads(record.pop(key) or "null")
            except (TypeError, ValueError):
                record[key[:-5]] = None
    return record


# ======================================================================
# Questions
# ======================================================================

def questions(conn: sqlite3.Connection, *, triage: Optional[str] = None,
              limit: int = 100) -> List[Dict[str, Any]]:
    """`GET /research/questions` — including the ones refused."""
    initialize_autoresearch_schema(conn)
    sql = """
        SELECT question_id, title, question, source_type, source_id,
               observation_id, triage, triage_reason, priority, sample_size,
               created_at
        FROM autoresearch_questions
    """
    params: tuple = ()
    if triage:
        sql += " WHERE triage = ?"
        params = (triage,)
    sql += " ORDER BY priority DESC, created_at DESC LIMIT ?"
    keys = ("question_id", "title", "question", "source_type", "source_id",
            "observation_id", "triage", "triage_reason", "priority",
            "sample_size", "created_at")
    return [dict(zip(keys, row))
            for row in conn.execute(sql, params + (_limit(limit),))]


def question_detail(conn: sqlite3.Connection, question_id: str
                    ) -> Optional[Dict[str, Any]]:
    """`GET /research/questions/{id}`"""
    return question_layer.load(conn, question_id)


def create_question(conn: sqlite3.Connection, *, title: str, question: str,
                    description: str = "", evidence: Sequence[Dict[str, Any]] = ()
                    ) -> Dict[str, Any]:
    """
    `POST /research/questions` — a human asking something.

    Human questions enter as QUEUED with the author recorded as HUMAN.
    They are not auto-triaged: a person asking a question has already
    decided it is worth asking, and silently downgrading it to
    LOW_PRIORITY would be the system overruling its user.
    """
    from src.domain.autoresearch_models import (
        Actor, Evidence, QuestionSource, ResearchQuestion, TriageState, _digest,
    )
    initialize_autoresearch_schema(conn)
    record = ResearchQuestion(
        question_id="q-" + _digest({"title": title, "q": question})[:20],
        title=title, question=question, description=description,
        source_type=QuestionSource.HUMAN_INPUT,
        triage=TriageState.QUEUED,
        triage_reason="asked by a person; not auto-triaged",
        evidence=[Evidence(**e) for e in evidence] or
                 [Evidence(kind="human", reference="researcher",
                           detail="asked directly")])
    record.validate()
    question_layer.save(conn, [(record, _zero_score(), _zero_cost())])
    audit.record(conn, actor=Actor.HUMAN, action="create_question",
                 question_id=record.question_id, decision="queued",
                 reason=question[:400])
    return record.as_dict()


def _zero_score():
    from src.domain.autoresearch_models import PriorityScore
    return PriorityScore()


def _zero_cost():
    from src.domain.autoresearch_models import ResearchCost
    return ResearchCost()


# ======================================================================
# Hypotheses
# ======================================================================

def hypotheses(conn: sqlite3.Connection, *, family_id: Optional[str] = None,
               limit: int = 100) -> List[Dict[str, Any]]:
    """`GET /research/hypotheses`"""
    initialize_autoresearch_schema(conn)
    sql = """
        SELECT hypothesis_id, question_id, statement, mechanism, population,
               expected_direction, family_id, family_name, source,
               source_reference, sample_size, evaluator, baseline,
               claim_fingerprint, experiment_id, created_at
        FROM autoresearch_hypotheses
    """
    params: tuple = ()
    if family_id:
        sql += " WHERE family_id = ?"
        params = (family_id,)
    sql += " ORDER BY created_at DESC LIMIT ?"
    keys = ("hypothesis_id", "question_id", "statement", "mechanism",
            "population", "expected_direction", "family_id", "family_name",
            "source", "source_reference", "sample_size", "evaluator",
            "baseline", "claim_fingerprint", "experiment_id", "created_at")
    return [dict(zip(keys, row))
            for row in conn.execute(sql, params + (_limit(limit),))]


def hypothesis_detail(conn: sqlite3.Connection, hypothesis_id: str
                      ) -> Optional[Dict[str, Any]]:
    """`GET /research/hypotheses/{id}` — with its full trace (§37)."""
    record = hypothesis_layer.load(conn, hypothesis_id)
    if record is None:
        return None
    record["question"] = question_layer.load(conn, record.get("question_id") or "")
    record["conclusions"] = [
        conclusion_detail(conn, row["conclusion_id"])
        for row in conclusions(conn, hypothesis_id=hypothesis_id)]
    record["family"] = prioritization.family_statistics(
        conn, record.get("family_id") or "")
    record["multiple_testing"] = governance.multiple_testing_state(
        conn, record.get("family_id") or "")
    return record


# ======================================================================
# Queue and cycles
# ======================================================================

def research_queue(conn: sqlite3.Connection, *, state: Optional[str] = None,
                   limit: int = 100) -> List[Dict[str, Any]]:
    """`GET /research/queue`"""
    return queue_layer.listing(conn, state=state, limit=_limit(limit))


def queue_depth(conn: sqlite3.Connection) -> Dict[str, int]:
    return queue_layer.depth(conn)


def cycles(conn: sqlite3.Connection, *, limit: int = 25) -> List[Dict[str, Any]]:
    """`GET /research/cycles`"""
    return cycle_layer.cycles(conn, limit=_limit(limit))


def run_cycle(conn: sqlite3.Connection, **kwargs) -> Dict[str, Any]:
    """`POST /research/cycles` — always bounded by a budget."""
    return cycle_layer.run_cycle(conn, **kwargs)


# ======================================================================
# Conclusions
# ======================================================================

def conclusions(conn: sqlite3.Connection, *,
                hypothesis_id: Optional[str] = None,
                conclusion: Optional[str] = None,
                limit: int = 100) -> List[Dict[str, Any]]:
    """
    `GET /research/conclusions` — failures included, always.

    There is deliberately no `successes_only` parameter. A research
    record filtered to its successes is not a research record.
    """
    initialize_autoresearch_schema(conn)
    clauses: List[str] = []
    params: List[Any] = []
    if hypothesis_id:
        clauses.append("hypothesis_id = ?")
        params.append(hypothesis_id)
    if conclusion:
        clauses.append("conclusion = ?")
        params.append(conclusion)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(_limit(limit))
    keys = ("conclusion_id", "hypothesis_id", "question_id", "experiment_id",
            "conclusion", "confidence", "effect", "effect_in_sample",
            "effect_low", "effect_high", "sample_size",
            "family_experiment_count", "promising", "concluded_at")
    return [dict(zip(keys, row)) for row in conn.execute("""
        SELECT conclusion_id, hypothesis_id, question_id, experiment_id,
               conclusion, confidence, effect, effect_in_sample, effect_low,
               effect_high, sample_size, family_experiment_count, promising,
               concluded_at
        FROM autoresearch_conclusions %s
        ORDER BY concluded_at DESC LIMIT ?
    """ % where, tuple(params))]


def conclusion_detail(conn: sqlite3.Connection, conclusion_id: str
                      ) -> Optional[Dict[str, Any]]:
    initialize_autoresearch_schema(conn)
    keys = ("conclusion_id", "hypothesis_id", "question_id", "experiment_id",
            "conclusion", "confidence", "effect", "effect_in_sample",
            "effect_low", "effect_high", "sample_size", "reasons_json",
            "limitations_json", "warnings_json", "evidence_json",
            "family_experiment_count", "promising", "concluded_at")
    row = conn.execute(
        "SELECT %s FROM autoresearch_conclusions WHERE conclusion_id = ?"
        % ", ".join(keys), (conclusion_id,)).fetchone()
    if row is None:
        return None
    return _decode(dict(zip(keys, row)))


# ======================================================================
# Candidates and families
# ======================================================================

def candidates(conn: sqlite3.Connection, *, status: Optional[str] = None,
               limit: int = 100) -> List[Dict[str, Any]]:
    """`GET /research/candidates` — every one requires review."""
    return candidate_registry.listing(conn, status=status, limit=_limit(limit))


def families(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """
    `GET /research/families` — with best AND median (§20, §80).

    Reporting only the best result of a family is how a weak line of
    research reads as a promising one.
    """
    initialize_autoresearch_schema(conn)
    keys = ("family_id", "status", "experiments", "supported", "rejected",
            "inconclusive", "best_effect", "median_effect", "reason",
            "reactivation_reason", "updated_at")
    rows = [dict(zip(keys, row)) for row in conn.execute("""
        SELECT family_id, status, experiments, supported, rejected,
               inconclusive, best_effect, median_effect, reason,
               reactivation_reason, updated_at
        FROM autoresearch_family_state ORDER BY experiments DESC
    """)]
    for row in rows:
        name = conn.execute(
            "SELECT family_name FROM autoresearch_hypotheses "
            "WHERE family_id = ? LIMIT 1", (row["family_id"],)).fetchone()
        row["family_name"] = name[0] if name else row["family_id"]
    return rows


# ======================================================================
# Governance and observability
# ======================================================================

def governance_report(conn: sqlite3.Connection) -> Dict[str, Any]:
    """§21-§25 in one place, for the dashboard and the report."""
    return {
        "protected_windows": governance.protected_windows(conn),
        "snooping": governance.snooping_report(conn),
        "multiple_testing": governance.multiple_testing_state(conn),
        "diversity": prioritization.research_diversity(conn),
        "exploration": prioritization.exploration_balance(conn),
    }


def observability(conn: sqlite3.Connection) -> Dict[str, Any]:
    """§76: what the researcher has done, and at what cost."""
    initialize_autoresearch_schema(conn)
    scalar = lambda sql: conn.execute(sql).fetchone()[0]
    return {
        "cycles": scalar("SELECT COUNT(*) FROM autoresearch_cycles"),
        "observations": scalar("SELECT COUNT(*) FROM autoresearch_observations"),
        "questions": scalar("SELECT COUNT(*) FROM autoresearch_questions"),
        "hypotheses": scalar("SELECT COUNT(*) FROM autoresearch_hypotheses"),
        "conclusions": scalar("SELECT COUNT(*) FROM autoresearch_conclusions"),
        "candidates": scalar("SELECT COUNT(*) FROM autoresearch_candidates"),
        "runtime_seconds": scalar(
            "SELECT COALESCE(SUM(runtime_seconds), 0) FROM autoresearch_cycles"),
        "queue_depth": queue_layer.depth(conn),
        "agent_activity": audit.activity_summary(conn),
    }


def integrity_check(conn: sqlite3.Connection) -> Dict[str, int]:
    """
    §88, as a query rather than a promise. Every count must be zero.

    The first two are the ones that matter: a conclusion with no
    reasons is an assertion, and a candidate that does not require
    review is a production change waiting to happen.
    """
    initialize_autoresearch_schema(conn)

    def scalar(sql: str, params: tuple = ()) -> int:
        try:
            return conn.execute(sql, params).fetchone()[0]
        except sqlite3.OperationalError:
            return 0

    return {
        "conclusions_without_reasons": scalar(
            "SELECT COUNT(*) FROM autoresearch_conclusions "
            "WHERE reasons_json IN ('[]','')"),
        "candidates_not_requiring_review": scalar(
            "SELECT COUNT(*) FROM autoresearch_candidates "
            "WHERE requires_review = 0"),
        "promoted_candidates": scalar(
            "SELECT COUNT(*) FROM autoresearch_candidates "
            "WHERE status = 'promoted'"),
        "candidates_without_a_base_version": scalar(
            "SELECT COUNT(*) FROM autoresearch_candidates "
            "WHERE TRIM(base_version) = ''"),
        "promising_conclusions_that_are_not_supported": scalar(
            "SELECT COUNT(*) FROM autoresearch_conclusions "
            "WHERE promising = 1 AND conclusion != 'supported'"),
        "hypotheses_without_a_mechanism": scalar(
            "SELECT COUNT(*) FROM autoresearch_hypotheses "
            "WHERE TRIM(mechanism) = ''"),
        "observations_without_evidence": scalar(
            "SELECT COUNT(*) FROM autoresearch_observations "
            "WHERE evidence_json IN ('[]','')"),
        "questions_without_a_triage_reason": scalar(
            "SELECT COUNT(*) FROM autoresearch_questions "
            "WHERE TRIM(triage_reason) = ''"),
        "cycles_without_a_termination_reason": scalar(
            "SELECT COUNT(*) FROM autoresearch_cycles "
            "WHERE TRIM(termination_reason) = ''"),

        # Lineage (§41, §61). The research trail is only a trail if
        # every link resolves; an orphan means a conclusion whose
        # hypothesis, question or experiment cannot be reached, which
        # is a finding nobody can check. Audited once by hand during
        # Phase 23.5 and found clean -- enforced here so it stays that
        # way.
        "conclusions_without_a_hypothesis": scalar("""
            SELECT COUNT(*) FROM autoresearch_conclusions c
            WHERE NOT EXISTS (SELECT 1 FROM autoresearch_hypotheses h
                              WHERE h.hypothesis_id = c.hypothesis_id)"""),
        "hypotheses_without_a_question": scalar("""
            SELECT COUNT(*) FROM autoresearch_hypotheses h
            WHERE h.question_id != '' AND NOT EXISTS (
                SELECT 1 FROM autoresearch_questions q
                WHERE q.question_id = h.question_id)"""),
        "questions_without_an_observation": scalar("""
            SELECT COUNT(*) FROM autoresearch_questions q
            WHERE q.observation_id != '' AND NOT EXISTS (
                SELECT 1 FROM autoresearch_observations o
                WHERE o.observation_id = q.observation_id)"""),
        "queue_items_without_a_hypothesis": scalar("""
            SELECT COUNT(*) FROM autoresearch_queue x
            WHERE NOT EXISTS (SELECT 1 FROM autoresearch_hypotheses h
                              WHERE h.hypothesis_id = x.hypothesis_id)"""),
        "candidates_without_a_conclusion": scalar("""
            SELECT COUNT(*) FROM autoresearch_candidates n
            WHERE NOT EXISTS (SELECT 1 FROM autoresearch_conclusions c
                              WHERE c.conclusion_id = n.conclusion_id)"""),
    }
