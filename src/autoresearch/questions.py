"""
src/autoresearch/questions.py
---------------------------------------
Phase 23 §5, §6 — turning an observation into a question, and deciding
which questions are worth answering.

THE TRIAGE IS THE VALUABLE PART, NOT THE GENERATION
-------------------------------------------------------
Generating questions from a database is easy and almost worthless: a
system that turns every regularity into a research question produces a
backlog nobody can act on and a multiple-testing problem nobody can
correct for. §6 is explicit — not every observation deserves an
experiment.

So every question carries a triage state AND the reason for it. Seven
of the eight states are ways of saying no:

    INSUFFICIENT_DATA   too few rows to learn anything
    UNTESTABLE          nothing in the system can express the test
    DUPLICATE           already asked; go read the answer
    LOW_PRIORITY        real, but not worth the budget now
    IGNORED             deliberately set aside
    QUEUED / RESEARCHING  in flight
    TESTABLE            the only state that may become a hypothesis

Recording the refusal with its reason is what separates a research
programme from a backlog. "We looked at this and decided not to test
it, because the cohort is 14 rows" is a finding; silence is not.

QUESTIONS ARE PHRASED AS QUESTIONS
--------------------------------------
`ResearchQuestion.validate()` requires a trailing question mark. That
looks like pedantry and is not: a statement dressed as a question is
usually a conclusion somebody already reached, and the wording is the
cheapest available check on whether the research is open.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.data_access.autoresearch_schema import initialize_autoresearch_schema
from src.domain.autoresearch_models import (
    MIN_RESEARCH_SAMPLE, RESEARCH_METHOD_VERSION, Evidence, ObservationKind,
    PriorityScore, QuestionSource, ResearchCost, ResearchObservation,
    ResearchQuestion, TriageState, _digest, novelty_against,
)
from src.autoresearch import governance, prioritization


def _question_id(observation_id: str) -> str:
    return "q-" + _digest({"observation": observation_id})[:20]


# ======================================================================
# Generation
# ======================================================================

#: How each observation kind becomes a question. The template supplies
#: the SHAPE; the observation supplies every number in it, so nothing
#: here can assert something the record does not.
_TEMPLATES = {
    ObservationKind.RECURRING_ERROR: (
        "Recurring {subject}",
        "Can {subject} be reduced by excluding an identifiable cohort of "
        "signals before they are issued?",
        "{statement} If the cases share a characteristic visible at "
        "decision time, a filter could remove them in advance. If they do "
        "not, the error is not addressable this way and the question "
        "closes."),
    ObservationKind.RECURRING_SUCCESS: (
        "Cohort above the base rate: {subject}",
        "Does the cohort {subject} still beat the base rate on evidence "
        "that arrives after the pattern was found?",
        "{statement} The pattern was found in this record, so the excess "
        "is already inside the data. The question is whether it survives "
        "a chronological split."),
    ObservationKind.SIGNAL_WEAKNESS: (
        "Cohort below the base rate: {subject}",
        "Does excluding the cohort {subject} improve directional accuracy "
        "out of sample?",
        "{statement} Removing a losing cohort is a cheaper and usually "
        "more robust change than adding a new rule, because it reduces "
        "rather than increases the number of moving parts."),
    ObservationKind.MODEL_DEGRADATION: (
        "Model below the baseline gate: {subject}",
        "Is the underperformance of {subject} concentrated in an "
        "identifiable cohort rather than spread across all of its "
        "predictions?",
        "{statement} A model that is uniformly weak needs replacing; one "
        "that is weak in a nameable subset can be restricted. The two "
        "call for different work, and the record can tell them apart."),
    ObservationKind.EXPERIMENT_FAILURE: (
        "Failed experiment: {subject}",
        "Did {subject} fail because the effect is absent, or because the "
        "held-out half was too short to measure it?",
        "{statement} An interval that includes zero is consistent with "
        "both, and distinguishing them decides whether the idea is dead "
        "or merely untested."),
}


def from_observation(observation: ResearchObservation) -> Optional[ResearchQuestion]:
    """
    One question per observation, or None where no template applies.

    Returning None rather than inventing a generic question is
    deliberate: a question the system does not know how to ask is one
    it cannot triage honestly either.
    """
    template = _TEMPLATES.get(observation.kind)
    if template is None:
        return None
    title_form, question_form, description_form = template
    fields = {"subject": observation.subject,
              "statement": observation.statement}
    question = ResearchQuestion(
        question_id=_question_id(observation.observation_id),
        title=title_form.format(**fields),
        question=question_form.format(**fields),
        description=description_form.format(**fields),
        source_type=observation.source_kind,
        source_id=observation.source_reference,
        observation_id=observation.observation_id,
        sample_size=observation.sample_size,
        evidence=list(observation.evidence))
    question.validate()
    return question


# ======================================================================
# Triage
# ======================================================================

#: An observation can only become a testable question if the system has
#: a registered Phase 22 evaluator able to express it. This maps the
#: observation kind onto the evaluator that would run it -- and the
#: kinds with no entry are exactly the ones that must be marked
#: UNTESTABLE rather than queued and quietly never run.
_TESTABLE_KINDS = {
    ObservationKind.RECURRING_SUCCESS: "signal_composite",
    ObservationKind.SIGNAL_WEAKNESS: "signal_composite",
    ObservationKind.RECURRING_ERROR: "signal_strength_threshold",
}


def triage(conn: sqlite3.Connection, question: ResearchQuestion,
           observation: ResearchObservation, *,
           existing_claims: Sequence[str] = (),
           weaknesses: Sequence[str] = ()) -> Tuple[ResearchQuestion,
                                                    PriorityScore,
                                                    ResearchCost]:
    """
    Decide what to do with a question, and record why (§6).

    The order of the checks matters. Sample size is tested first
    because a cohort too small to learn from is not worth deduplicating
    or costing; duplication is tested before priority because a
    question already answered should point at its answer rather than
    compete for the budget.
    """
    cost = prioritization.estimate_cost(observation)
    claim = _claim_key(observation)
    novelty = novelty_against(claim, existing_claims)
    score = prioritization.score_observation(
        observation, novelty=novelty, cost=cost, known_weaknesses=weaknesses)
    question.priority = score.total

    if observation.sample_size < MIN_RESEARCH_SAMPLE:
        question.triage = TriageState.INSUFFICIENT_DATA
        question.triage_reason = (
            "the cohort holds %d observations, below the %d this project "
            "requires everywhere else. A result from it would be a number, "
            "not evidence."
            % (observation.sample_size, MIN_RESEARCH_SAMPLE))
        return question, score, cost

    if claim in set(existing_claims):
        question.triage = TriageState.DUPLICATE
        question.triage_reason = (
            "this claim has already been tested. Testing it again without "
            "reading the previous answer is how a research record starts "
            "double-counting its own evidence (§13).")
        return question, score, cost

    conditions = observation.measures.get("conditions") or {}
    leaking = governance.leaking_fields(conditions)
    if leaking:
        question.triage = TriageState.UNTESTABLE
        question.triage_reason = (
            "the cohort is defined on %s, which %s only knowable after the "
            "outcome. A filter cannot consult it at decision time, so an "
            "improvement measured this way would be hindsight rather than a "
            "result. This is not a gap in the evaluators: the question "
            "cannot be asked of the future in any implementation."
            % (", ".join(leaking), "are" if len(leaking) > 1 else "is"))
        return question, score, cost

    if observation.kind not in _TESTABLE_KINDS:
        question.triage = TriageState.UNTESTABLE
        question.triage_reason = (
            "no registered evaluator can express a test of this "
            "observation. It is a real question and this system cannot "
            "currently answer it, which is different from it being "
            "uninteresting.")
        return question, score, cost

    if score.total < prioritization.PRIORITY_FLOOR:
        question.triage = TriageState.LOW_PRIORITY
        question.triage_reason = (
            "priority %.3f is below the %.2f floor. Components: %s"
            % (score.total, prioritization.PRIORITY_FLOOR,
               "; ".join(score.explain())))
        return question, score, cost

    question.triage = TriageState.TESTABLE
    question.triage_reason = (
        "priority %.3f over %d observations; a registered evaluator can "
        "express the test." % (score.total, observation.sample_size))
    return question, score, cost


def _claim_key(observation: ResearchObservation) -> str:
    """
    What an observation would CLAIM if tested (§13).

    Keyed on the cohort and the direction, never on the wording. Phase
    22 learned this expensively: its generator produced three
    differently-worded proposals that were one comparison, and the
    per-family correction missed them because they sat in three
    families.
    """
    conditions = observation.measures.get("conditions") or {}
    direction = ("exclude"
                 if observation.kind is ObservationKind.SIGNAL_WEAKNESS
                 else "include")
    return _digest({"conditions": conditions, "subject": observation.subject,
                    "direction": direction})


def existing_claims(conn: sqlite3.Connection) -> List[str]:
    """Claims already asked about, so a repeat can be recognised."""
    initialize_autoresearch_schema(conn)
    return [row[0] for row in conn.execute(
        "SELECT DISTINCT source_id FROM autoresearch_questions "
        "WHERE source_id != ''") if row[0]]


# ======================================================================
# Batch
# ======================================================================

def raise_questions(conn: sqlite3.Connection,
                    observations: Sequence[ResearchObservation]
                    ) -> List[Tuple[ResearchQuestion, PriorityScore, ResearchCost]]:
    """Every observation triaged, including the ones refused."""
    initialize_autoresearch_schema(conn)
    weaknesses = prioritization.known_weaknesses(conn)
    claims: List[str] = []
    results = []
    for observation in observations:
        question = from_observation(observation)
        if question is None:
            continue
        question, score, cost = triage(
            conn, question, observation,
            existing_claims=claims, weaknesses=weaknesses)
        claims.append(_claim_key(observation))
        results.append((question, score, cost))
    return results


def save(conn: sqlite3.Connection,
         triaged: Sequence[Tuple[ResearchQuestion, PriorityScore, ResearchCost]]
         ) -> int:
    """Persist questions with their priority breakdown and cost."""
    initialize_autoresearch_schema(conn)
    seen = set()
    for question, _s, _c in triaged:
        if question.question_id in seen:
            raise ValueError(
                "question id %s appears twice in one batch; the id is "
                "keyed on something that does not distinguish these "
                "questions and storing the batch would drop rows."
                % question.question_id)
        seen.add(question.question_id)

    for question, score, cost in triaged:
        conn.execute("""
            INSERT OR REPLACE INTO autoresearch_questions (
                question_id, method_version, title, question, description,
                source_type, source_id, observation_id, triage,
                triage_reason, priority, priority_json, cost_json,
                sample_size, evidence_json, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (question.question_id, question.method_version, question.title,
              question.question, question.description,
              question.source_type.value, question.source_id,
              question.observation_id, question.triage.value,
              question.triage_reason, question.priority,
              json.dumps(score.as_dict()), json.dumps(cost.as_dict()),
              question.sample_size,
              json.dumps([e.as_dict() for e in question.evidence]),
              question.created_at))
    conn.commit()
    return len(triaged)


def load(conn: sqlite3.Connection, question_id: str
         ) -> Optional[Dict[str, Any]]:
    initialize_autoresearch_schema(conn)
    row = conn.execute("""
        SELECT question_id, title, question, description, source_type,
               source_id, observation_id, triage, triage_reason, priority,
               priority_json, cost_json, sample_size, evidence_json, created_at
        FROM autoresearch_questions WHERE question_id = ?
    """, (question_id,)).fetchone()
    if row is None:
        return None
    keys = ("question_id", "title", "question", "description", "source_type",
            "source_id", "observation_id", "triage", "triage_reason",
            "priority", "priority_json", "cost_json", "sample_size",
            "evidence_json", "created_at")
    record = dict(zip(keys, row))
    for key in ("priority_json", "cost_json", "evidence_json"):
        try:
            record[key[:-5]] = json.loads(record.pop(key) or "null")
        except (TypeError, ValueError):
            record[key[:-5]] = None
    return record
