"""
src/autoresearch/hypotheses.py
----------------------------------------
Phase 23 §7-§10, §13-§15 — turning a testable question into a claim that
can be shown to be wrong.

WHAT MAKES A HYPOTHESIS HERE DIFFERENT FROM A GUESS
-------------------------------------------------------
Four things, all required before it can be queued:

1. a **mechanism** — why the effect would exist, not just that it might
2. a **population** and a **condition** — about what, under what
3. a **direction** — increase, decrease, or change
4. **falsifiability** — the minimum effect, the minimum sample, the
   acceptable degradation, and whether the interval must exclude zero,
   all fixed BEFORE the test

§9's bad example, "Maybe momentum is bad", fails on every one. The
quality gate rejects it on the first: `_VAGUE_TERMS` catches "maybe",
and no amount of the rest would save it.

DEDUPLICATION IS AGAINST TWO RECORDS, NOT ONE
-------------------------------------------------
§13 says to search prior hypotheses, experiments, runs and results
before creating anything. So `find_duplicate` checks this phase's
hypotheses AND Phase 22's experiments, because the interesting
duplicate is the one already answered by an experiment nobody
remembered running.

Both checks key on the CLAIM, never the wording. Phase 22 established
why the hard way: its generator produced three differently-worded
proposals that were a single comparison, and the per-family correction
missed them because they sat in three different families.

MEMORY IS CONSULTED BEFORE ANYTHING IS PROPOSED (§15)
---------------------------------------------------------
`research_context` answers, for a claim about to be made: has this been
tested, what happened, and is its family already depleted. A researcher
that cannot recall its own failures will repeat them at full cost.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.data_access.autoresearch_schema import initialize_autoresearch_schema
from src.domain.autoresearch_models import (
    MIN_RESEARCH_SAMPLE, RESEARCH_METHOD_VERSION, Actor, Evidence,
    FalsifiabilityCriteria, FamilyStatus, ObservationKind, QuestionSource,
    ResearchHypothesis, ResearchObservation, ResearchQuestion, TriageState,
    _digest,
)
from src.autoresearch import governance, prioritization


class HypothesisRefused(Exception):
    """The claim cannot be formed, and the reason is always given."""


def _hypothesis_id(claim: str) -> str:
    return "h-" + claim[:20]


#: How a memory-pattern condition key maps onto a parameter the Phase
#: 22 `signal_composite` evaluator actually accepts.
#:
#: Anything absent here CANNOT be expressed as a cohort filter by the
#: current evaluators. That is not a detail: a hypothesis carrying an
#: unmappable key produces an experiment Phase 22 rightly refuses to
#: run, and a refused run has no result -- which the research layer
#: then has to interpret. The first version of this code interpreted it
#: as INSUFFICIENT_DATA, i.e. "we tested and the sample was too small",
#: when the truth was "this was never tested at all". Those are
#: different findings and the flattering one was being reported.
#:
#: So unmappable conditions are refused at the point the claim is made.
_CONDITION_TO_PARAMETER = {
    "expected_direction": "direction",
    "direction": "direction",
    "horizon": "horizon",
    "event_type": "event_type",
    "signal_strength": "threshold",
    "signal_confidence": "confidence_min",
    "subject_kind": "subject_kind",
}


def cohort_parameters(condition: Dict[str, Any]) -> Dict[str, Any]:
    """
    Translate a cohort definition into evaluator parameters.

    Raises `HypothesisRefused` naming the keys that cannot be
    expressed, so triage records a real reason rather than the system
    generating a test it cannot run.
    """
    unmappable = sorted(key for key in condition
                        if key not in _CONDITION_TO_PARAMETER)
    if unmappable:
        raise HypothesisRefused(
            "the cohort is keyed on %s, which no registered evaluator can "
            "filter on. The pattern is real and this system cannot express "
            "a test of it, which is different from the test having been run "
            "and found nothing." % ", ".join(unmappable))
    return {_CONDITION_TO_PARAMETER[key]: value
            for key, value in condition.items()}


def _condition_label(condition: Dict[str, Any]) -> str:
    return ", ".join("%s=%s" % (k, condition[k]) for k in sorted(condition))


# ======================================================================
# Generation
# ======================================================================

def from_question(question: ResearchQuestion,
                  observation: ResearchObservation, *,
                  family_id: str = "", family_name: str = "",
                  author: Actor = Actor.SYSTEM) -> ResearchHypothesis:
    """
    Build the falsifiable claim a testable question implies.

    The mechanism is generated from the observation's own numbers, so
    it cannot assert anything the record does not contain. Where the
    hypothesis was MINED from the same record it will be tested
    against, the mechanism says so — Phase 22 established that a
    pattern which suggested a hypothesis is not evidence for it, and
    the caveat belongs in the claim rather than in a footnote.
    """
    if not question.is_testable:
        raise HypothesisRefused(
            "question %s is %s, not testable: %s"
            % (question.question_id, question.triage.value,
               question.triage_reason))

    condition = dict(observation.measures.get("conditions") or {})
    governance.assert_decision_time(condition)

    measures = observation.measures
    base_rate = measures.get("base_rate")
    hit_rate = measures.get("hit_rate")

    if observation.kind is ObservationKind.SIGNAL_WEAKNESS:
        direction = "decrease"
        label = _condition_label(condition) or observation.subject
        statement = (
            "The cohort %s continues to underperform the base rate on "
            "evidence that arrives after it was identified, which is what "
            "would justify excluding it." % label)
        mechanism = (
            "The cohort has a %.1f%% hit rate over %d experiences against a "
            "%.1f%% base rate. If the shortfall reflects something stable "
            "about these signals rather than the particular period, "
            "removing them should raise the accuracy of what remains. "
            "This cohort was found in the same record the test will use, "
            "so the shortfall is already inside the data; the honest test "
            "is whether it persists on the held-out half."
            % (100 * (hit_rate or 0), observation.sample_size,
               100 * (base_rate or 0)))
        # NOTE: the current evaluators can RESTRICT to a cohort but
        # cannot EXCLUDE one. Expressing "exclude X" as "restrict to X"
        # would silently test the opposite claim, so the exclusion is
        # stated in the hypothesis and tested as its complement only
        # when an evaluator supports it. Until then this branch tests
        # the cohort itself and the statement says which.
        parameters = cohort_parameters(condition)
        evaluator = "signal_composite"

    elif observation.kind is ObservationKind.RECURRING_SUCCESS:
        direction = "increase"
        label = _condition_label(condition) or observation.subject
        statement = (
            "Restricting to the cohort %s increases directional accuracy "
            "on evidence that arrives after this cohort was identified."
            % label)
        mechanism = (
            "The cohort has a %.1f%% hit rate over %d experiences against a "
            "%.1f%% base rate. If that excess reflects something stable, it "
            "should survive a chronological split. The cohort was mined "
            "from the record it will be tested against, so a positive "
            "in-sample result is expected and carries no information; only "
            "the out-of-sample half does."
            % (100 * (hit_rate or 0), observation.sample_size,
               100 * (base_rate or 0)))
        parameters = cohort_parameters(condition)
        evaluator = "signal_composite"

    elif observation.kind is ObservationKind.RECURRING_ERROR:
        direction = "increase"
        statement = (
            "Requiring a signal strength of at least 0.5 increases "
            "directional accuracy, because %s concentrates in weaker "
            "signals." % observation.subject)
        mechanism = (
            "%s is the attributed primary cause in %d observed cases. If "
            "weak signals carry a disproportionate share of them, a "
            "strength floor removes the cases without needing to identify "
            "them individually. If the errors are spread evenly across "
            "strength, the floor will remove good signals with the bad and "
            "the effect will be flat or negative."
            % (observation.subject, observation.sample_size))
        parameters = {"threshold": 0.5}
        evaluator = "signal_strength_threshold"

    else:
        raise HypothesisRefused(
            "no hypothesis template exists for observation kind %r. It is "
            "better to have no hypothesis than a generic one that cannot "
            "be tested honestly." % observation.kind.value)

    falsifiability = FalsifiabilityCriteria(
        expected_result=(
            "directional accuracy on the held-out half is at least 2 "
            "percentage points above the baseline, with a bootstrap "
            "interval that excludes zero"),
        minimum_effect=0.02,
        acceptable_degradation=0.0,
        minimum_sample=MIN_RESEARCH_SAMPLE,
        require_interval_excludes_zero=True,
        evaluation_metric="directional_accuracy")

    # A family groups hypotheses that touch the same idea (§14), and it
    # is what the multiple-testing correction and the dead-end detector
    # both count over. Deriving it from the SHAPE of the claim -- what
    # kind of change, keyed on which fields -- rather than from the
    # wording means three differently-phrased tests of one idea land in
    # one family instead of three, which is precisely the miscount that
    # let Phase 22 report three identical results as three findings.
    if not family_id:
        shape = ("exclusion" if observation.kind is ObservationKind.SIGNAL_WEAKNESS
                 else "restriction" if observation.kind is ObservationKind.RECURRING_SUCCESS
                 else "strength floor")
        keys = ", ".join(sorted(condition)) or "signal strength"
        family_name = family_name or "%s keyed on %s" % (shape, keys)
        family_id = "fam-" + _digest({"shape": shape, "keys": sorted(condition)})[:16]

    hypothesis = ResearchHypothesis(
        hypothesis_id="",  # filled below, from the claim itself
        question_id=question.question_id,
        statement=statement,
        mechanism=mechanism,
        population="all signals",
        condition=condition or {"threshold": 0.5},
        expected_direction=direction,
        falsifiability=falsifiability,
        family_id=family_id,
        family_name=family_name,
        source=question.source_type,
        source_reference=question.source_id,
        evidence=list(question.evidence),
        sample_size=observation.sample_size,
        evaluator=evaluator,
        parameters=parameters,
        baseline="all_signals",
        author=author)
    hypothesis.hypothesis_id = _hypothesis_id(hypothesis.claim_fingerprint)
    hypothesis.validate()
    return hypothesis


# ======================================================================
# Deduplication (§13) and research memory (§15)
# ======================================================================

def find_duplicate(conn: sqlite3.Connection, hypothesis: ResearchHypothesis
                   ) -> Optional[Dict[str, Any]]:
    """
    Whether this exact claim has been made before, here or in Phase 22.

    Returns the prior record, so a caller can point at the ANSWER
    rather than merely refusing. "We tested this and it failed" is far
    more useful than "duplicate".
    """
    initialize_autoresearch_schema(conn)

    row = conn.execute("""
        SELECT h.hypothesis_id, h.statement, h.experiment_id,
               c.conclusion, c.effect
        FROM autoresearch_hypotheses h
        LEFT JOIN autoresearch_conclusions c
               ON c.hypothesis_id = h.hypothesis_id
        WHERE h.claim_fingerprint = ? AND h.hypothesis_id != ?
        LIMIT 1
    """, (hypothesis.claim_fingerprint, hypothesis.hypothesis_id)).fetchone()
    if row:
        return {"where": "autoresearch_hypotheses", "hypothesis_id": row[0],
                "statement": row[1], "experiment_id": row[2],
                "conclusion": row[3], "effect": row[4]}

    # Phase 22's own record. An experiment making this comparison may
    # exist even when no Phase 23 hypothesis does -- it could have been
    # proposed by Phase 22's generator or written by hand.
    if _table_exists(conn, "experiments"):
        candidate = conn.execute("""
            SELECT e.experiment_id, e.name, r.decision, r.effect
            FROM experiments e
            LEFT JOIN experiment_results r
                   ON r.experiment_id = e.experiment_id
            WHERE e.candidate_evaluator = ?
              AND e.candidate_params_json = ?
            LIMIT 1
        """, (hypothesis.evaluator,
              json.dumps(hypothesis.parameters, sort_keys=True,
                         default=str))).fetchone()
        if candidate:
            return {"where": "experiments", "experiment_id": candidate[0],
                    "statement": candidate[1], "conclusion": candidate[2],
                    "effect": candidate[3]}
    return None


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def research_context(conn: sqlite3.Connection,
                     hypothesis: ResearchHypothesis) -> Dict[str, Any]:
    """
    What the record already knows about this claim (§15).

    Consulted BEFORE anything is queued: what has been tested, what
    failed, what is unresolved, and whether the family is depleted. A
    researcher that cannot recall its own failures repeats them at full
    price.
    """
    duplicate = find_duplicate(conn, hypothesis)
    stats = prioritization.family_statistics(conn, hypothesis.family_id) \
        if hypothesis.family_id else {"experiments": 0}
    status, reason = prioritization.assess_family(stats)
    testing = governance.multiple_testing_state(conn, hypothesis.family_id)
    return {
        "duplicate": duplicate,
        "family": stats,
        "family_status": status.value,
        "family_reason": reason,
        "multiple_testing": testing,
    }


# ======================================================================
# Persistence
# ======================================================================

def save(conn: sqlite3.Connection,
         hypotheses: Sequence[ResearchHypothesis]) -> int:
    initialize_autoresearch_schema(conn)
    for hypothesis in hypotheses:
        conn.execute("""
            INSERT OR REPLACE INTO autoresearch_hypotheses (
                hypothesis_id, method_version, question_id, statement,
                mechanism, population, condition_json, expected_direction,
                falsifiability_json, family_id, family_name, source,
                source_reference, evidence_json, sample_size, evaluator,
                parameters_json, baseline, author, claim_fingerprint,
                experiment_id, quality_problems_json, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (hypothesis.hypothesis_id, hypothesis.method_version,
              hypothesis.question_id, hypothesis.statement,
              hypothesis.mechanism, hypothesis.population,
              json.dumps(hypothesis.condition, sort_keys=True, default=str),
              hypothesis.expected_direction,
              json.dumps(hypothesis.falsifiability.as_dict()),
              hypothesis.family_id, hypothesis.family_name,
              hypothesis.source.value, hypothesis.source_reference,
              json.dumps([e.as_dict() for e in hypothesis.evidence]),
              hypothesis.sample_size, hypothesis.evaluator,
              json.dumps(hypothesis.parameters, sort_keys=True, default=str),
              hypothesis.baseline, hypothesis.author.value,
              hypothesis.claim_fingerprint, None,
              json.dumps(hypothesis.quality_problems()),
              hypothesis.created_at))
    conn.commit()
    return len(hypotheses)


def attach_experiment(conn: sqlite3.Connection, hypothesis_id: str,
                      experiment_id: str) -> None:
    """Record which Phase 22 experiment a hypothesis became."""
    initialize_autoresearch_schema(conn)
    conn.execute("""
        UPDATE autoresearch_hypotheses SET experiment_id = ?
        WHERE hypothesis_id = ?
    """, (experiment_id, hypothesis_id))
    conn.commit()


def load(conn: sqlite3.Connection, hypothesis_id: str
         ) -> Optional[Dict[str, Any]]:
    initialize_autoresearch_schema(conn)
    keys = ("hypothesis_id", "question_id", "statement", "mechanism",
            "population", "condition_json", "expected_direction",
            "falsifiability_json", "family_id", "family_name", "source",
            "source_reference", "evidence_json", "sample_size", "evaluator",
            "parameters_json", "baseline", "author", "claim_fingerprint",
            "experiment_id", "quality_problems_json", "created_at")
    row = conn.execute(
        "SELECT %s FROM autoresearch_hypotheses WHERE hypothesis_id = ?"
        % ", ".join(keys), (hypothesis_id,)).fetchone()
    if row is None:
        return None
    record = dict(zip(keys, row))
    for key in list(record):
        if key.endswith("_json"):
            try:
                record[key[:-5]] = json.loads(record.pop(key) or "null")
            except (TypeError, ValueError):
                record[key[:-5]] = None
    return record
