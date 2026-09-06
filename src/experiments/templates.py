"""
src/experiments/templates.py
------------------------------------
Safe templates, and the pathways from memory and error to hypothesis.

    memory pattern  ->  candidate hypothesis  ->  experiment   (§21)
    recurring error ->  candidate hypothesis  ->  experiment   (§22)

PROPOSE, NEVER EXECUTE (§69, §70, §76)
------------------------------------------
Every function here returns a DRAFT experiment. Nothing in this module
runs anything, and nothing schedules anything. §76 is explicit that
autonomous experiment generation is not this phase, and the line that
keeps it honest is that a proposal and an execution are different verbs
with different callers.

A proposal also carries its own provenance: which pattern or which
error prompted it, and — importantly — the fact that a hypothesis mined
from the same record it will be tested against is more exposed to data
snooping than one a person brought from outside. That caveat is written
into the hypothesis mechanism, so it survives into the stored
experiment rather than living in a docstring.

WHY TEMPLATES AT ALL (§55)
------------------------------
Because the failure mode of a research system is not a wrong answer, it
is a badly posed question: no baseline, no criteria, or a candidate
that changes four things at once. A template fixes the shape — a
registered baseline, a stated mechanism, criteria set before the run —
and leaves the researcher to supply the idea.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from src.data_access.experiment_schema import initialize_experiment_schema
from src.domain.experiment_models import (
    AcceptanceCriteria, ArmSpec, DatasetSnapshot, EvaluationProtocol,
    Experiment, ExperimentStatus, ExperimentType, Hypothesis,
    HypothesisSource, ResourceLimits,
)
from src.experiments import evaluators

#: The caveat attached to any hypothesis mined from the record it will
#: be tested against. Stated once so every generated hypothesis carries
#: the same words and a reader learns to recognise them.
MINED_CAVEAT = (
    "This hypothesis was derived from the same historical record it will be "
    "tested against, so the pattern that suggested it is already inside the "
    "data. Read a positive result with that in mind: the honest test is "
    "whether it survives on evidence that arrives afterwards."
)


def _experiment_id(name: str, conditions: Dict[str, Any]) -> str:
    payload = json.dumps({"n": name, "c": conditions}, sort_keys=True,
                         default=str)
    return f"exp-{hashlib.sha256(payload.encode()).hexdigest()[:20]}"


def _family_id(core: str) -> str:
    return f"fam-{hashlib.sha256(core.encode()).hexdigest()[:16]}"


def ensure_family(conn: sqlite3.Connection, family_id: str, name: str,
                  core_statement: str, *, created_by: str = "") -> str:
    """
    Register a hypothesis family, so variations can be counted (§43).

    Counting is the whole point: a family of fifty produces a winner by
    chance, and the count travels with every decision made inside it.
    """
    initialize_experiment_schema(conn)
    conn.execute("""
        INSERT OR IGNORE INTO hypothesis_families
        (family_id, name, description, core_statement, created_by, created_at)
        VALUES (?,?,?,?,?,?)
    """, (family_id, name, "", core_statement, created_by,
          datetime.now(timezone.utc).isoformat()))
    conn.commit()
    return family_id


# ======================================================================
# Templates (§55)
# ======================================================================

def threshold_experiment(*, name: str, evaluator: str, parameter: str,
                         value: Any, baseline_name: str = "all_signals",
                         metric: str = "directional_accuracy",
                         mechanism: str = "",
                         population: str = "all signal experiences",
                         family_id: Optional[str] = None,
                         criteria: Optional[AcceptanceCriteria] = None,
                         dataset: Optional[DatasetSnapshot] = None,
                         created_by: str = "") -> Experiment:
    """
    "Does filtering on this threshold help?" — the one-variable case.

    The candidate changes exactly one parameter against a registered
    baseline, which is §8's preferred shape and the only one where a
    difference can be attributed without argument.
    """
    conditions = {parameter: value}
    return Experiment(
        experiment_id=_experiment_id(name, conditions),
        name=name,
        experiment_type=ExperimentType.SIGNAL,
        hypothesis=Hypothesis(
            statement=f"Filtering on {parameter} >= {value} improves {metric}.",
            mechanism=mechanism or (
                f"A higher {parameter} should carry more information than a "
                f"marginal one, so removing the marginal cases should raise "
                f"{metric}."),
            expected_effect=f"higher {metric} out of sample",
            population=population,
            conditions=conditions, metric=metric,
            source=HypothesisSource.SIGNAL_ANALYSIS,
            family_id=family_id),
        baseline=evaluators.baseline(baseline_name),
        candidate=ArmSpec(
            name=f"{parameter} >= {value}", evaluator=evaluator,
            parameters={parameter: value, "subject_kind": "signal"},
            description=f"one variable changed: {parameter}",
            complexity=2),
        dataset=dataset or DatasetSnapshot(universe="all signals"),
        criteria=criteria or AcceptanceCriteria(),
        created_by=created_by,
        description=("Template: threshold test. One parameter changed against "
                     "a registered baseline."))


def filter_experiment(*, name: str, evaluator: str,
                      parameters: Dict[str, Any],
                      baseline_name: str = "all_signals",
                      metric: str = "directional_accuracy",
                      statement: str = "", mechanism: str = "",
                      population: str = "all signal experiences",
                      family_id: Optional[str] = None,
                      source: HypothesisSource = HypothesisSource.RESEARCHER,
                      source_reference: Optional[str] = None,
                      criteria: Optional[AcceptanceCriteria] = None,
                      dataset: Optional[DatasetSnapshot] = None,
                      complexity: int = 2,
                      created_by: str = "") -> Experiment:
    """
    "Does this cohort behave differently?" — a filter against a control.

    When `parameters` changes more than one thing, `changed_variables`
    on the experiment records all of them, so §8's requirement to
    document multi-variable changes is satisfied by computation rather
    than by a promise in a description.
    """
    return Experiment(
        experiment_id=_experiment_id(name, parameters),
        name=name,
        experiment_type=ExperimentType.SIGNAL,
        hypothesis=Hypothesis(
            statement=statement or f"The cohort {parameters} differs on {metric}.",
            mechanism=mechanism or (
                "A stated mechanism is required; this template's default is a "
                "placeholder and should be replaced with the actual reason "
                "the cohort is expected to differ."),
            expected_effect=f"a different {metric} from the control",
            population=population, conditions=dict(parameters), metric=metric,
            source=source, source_reference=source_reference,
            family_id=family_id),
        baseline=evaluators.baseline(baseline_name),
        candidate=ArmSpec(name=name, evaluator=evaluator,
                          parameters=dict(parameters), complexity=complexity),
        dataset=dataset or DatasetSnapshot(universe="all signals"),
        criteria=criteria or AcceptanceCriteria(),
        created_by=created_by,
        description="Template: cohort filter against a registered baseline.")


def horizon_experiment(*, horizon: str, baseline_name: str = "all_signals",
                       metric: str = "directional_accuracy",
                       family_id: Optional[str] = None,
                       created_by: str = "") -> Experiment:
    """"Is this horizon better than the whole ladder?" """
    return filter_experiment(
        name=f"horizon {horizon} versus all horizons",
        evaluator="signal_horizon", parameters={"horizon": horizon,
                                                "subject_kind": "signal"},
        baseline_name=baseline_name, metric=metric,
        statement=f"Signals measured at {horizon} are more accurate than the "
                  f"ladder as a whole.",
        mechanism=("An edge with a natural duration should be strongest at the "
                   "horizon matching it and diluted at others."),
        family_id=family_id, source=HypothesisSource.SIGNAL_ANALYSIS,
        created_by=created_by)


def event_experiment(*, event_type: str, baseline_name: str = "all_signals",
                     metric: str = "directional_accuracy",
                     family_id: Optional[str] = None,
                     created_by: str = "") -> Experiment:
    """"Does this event class behave differently?" """
    return filter_experiment(
        name=f"{event_type} events versus all signals",
        evaluator="signal_event_filter",
        parameters={"event_type": event_type, "subject_kind": "signal"},
        baseline_name=baseline_name, metric=metric,
        statement=f"Signals arising from {event_type} events differ on {metric}.",
        mechanism=(f"{event_type} events may resolve on a different timescale "
                   f"or with different information content from the average "
                   f"event, which would show as a different accuracy."),
        family_id=family_id, source=HypothesisSource.EVENT_ANALYSIS,
        created_by=created_by)


def direction_experiment(*, direction: str,
                         metric: str = "directional_accuracy",
                         family_id: Optional[str] = None,
                         created_by: str = "") -> Experiment:
    """"Is one side of the book better than the other?" """
    control = "short_only" if direction == "long" else "long_only"
    return filter_experiment(
        name=f"{direction} signals versus {control.replace('_only','')}",
        evaluator="signal_direction",
        parameters={"direction": direction, "subject_kind": "signal"},
        baseline_name=control, metric=metric,
        statement=f"{direction.title()} signals are more accurate than the "
                  f"other side.",
        mechanism=("Asymmetry between sides can come from borrow costs, from "
                   "the drift of the underlying, or from the event mix that "
                   "produces each side."),
        family_id=family_id, source=HypothesisSource.SIGNAL_ANALYSIS,
        created_by=created_by)


#: The templates offered by `GET /experiments/templates` (§55).
TEMPLATES: Dict[str, Dict[str, Any]] = {
    "threshold": {
        "function": "threshold_experiment",
        "description": "One parameter threshold against a registered baseline.",
        "changes": 1,
        "runnable": True,
    },
    "filter": {
        "function": "filter_experiment",
        "description": "A cohort filter against a registered baseline.",
        "changes": "1 or more (recorded in changed_variables)",
        "runnable": True,
    },
    "horizon": {
        "function": "horizon_experiment",
        "description": "One horizon against the whole ladder.",
        "changes": 1,
        "runnable": True,
    },
    "event": {
        "function": "event_experiment",
        "description": "One event class against all signals.",
        "changes": 1,
        "runnable": True,
    },
    "direction": {
        "function": "direction_experiment",
        "description": "One side of the book against the other.",
        "changes": 1,
        "runnable": True,
    },
    "model_comparison": {
        "function": "filter_experiment",
        "description": "Two trained models on the same data.",
        "changes": 1,
        "runnable": False,
        "requires": "a second promoted model; only one family exists",
    },
    "regime_filter": {
        "function": "filter_experiment",
        "description": "One market regime against all regimes.",
        "changes": 1,
        "runnable": False,
        "requires": "market_regime is NULL on every experience",
    },
    "execution_policy": {
        "function": "filter_experiment",
        "description": "Two execution policies on realised fills.",
        "changes": 1,
        "runnable": False,
        "requires": "no order has ever been placed",
    },
    "position_sizing": {
        "function": "filter_experiment",
        "description": "Two sizing rules under the same risk budget.",
        "changes": 1,
        "runnable": False,
        "requires": "no portfolio or position exists",
    },
}


# ======================================================================
# Memory -> hypothesis (§21, §69)
# ======================================================================

def propose_from_memory_pattern(conn: sqlite3.Connection, pattern_id: str, *,
                                memory_version: str = "v1",
                                created_by: str = "") -> Optional[Experiment]:
    """
    Turn a memory pattern into a DRAFT experiment (§21, §69).

    Returns None when the pattern is too weak to be worth testing — a
    pattern of eleven observations does not need an experiment, it
    needs more observations, and generating one anyway would inflate
    the family count without adding evidence.

    Proposes. Never runs. The source pattern is preserved on the
    hypothesis, and the mined-hypothesis caveat is written into the
    mechanism so it survives into storage.
    """
    from src.data_access.memory_schema import initialize_memory_schema
    initialize_memory_schema(conn)
    initialize_experiment_schema(conn)

    row = conn.execute("""
        SELECT pattern_type, conditions_json, sample_size, hit_rate,
               mean_return, quality, confidence, stability
        FROM memory_patterns WHERE pattern_id = ? AND memory_version = ?
    """, (pattern_id, memory_version)).fetchone()
    if row is None:
        return None

    pattern_type, conditions_raw, sample, hit_rate, mean_return = row[:5]
    quality, confidence, stability = row[5], row[6], row[7]
    conditions = json.loads(conditions_raw or "{}")

    from src.domain.memory_models import MIN_PATTERN_SAMPLE
    if sample < MIN_PATTERN_SAMPLE or hit_rate is None:
        return None

    evaluator, parameters = _evaluator_for(conditions)
    if evaluator is None:
        return None

    family = _family_id(f"{pattern_type}:{sorted(conditions)}")
    ensure_family(conn, family, f"variations of {pattern_type}",
                  f"cohorts keyed on {', '.join(sorted(conditions))}",
                  created_by=created_by)

    described = ", ".join(f"{k}={v}" for k, v in sorted(conditions.items()))
    return filter_experiment(
        name=f"memory pattern: {described}",
        evaluator=evaluator, parameters=parameters,
        metric="directional_accuracy",
        statement=(f"The cohort {described} is directionally more accurate "
                   f"than all signals."),
        mechanism=(
            f"Memory records a {hit_rate:.0%} hit rate over {sample} "
            f"experiences under these conditions, quality {quality}, "
            f"confidence {confidence}, stability {stability}. "
            + MINED_CAVEAT),
        population="all signal experiences with a measured outcome",
        family_id=family,
        source=HypothesisSource.MEMORY_PATTERN,
        source_reference=pattern_id,
        created_by=created_by)


def _evaluator_for(conditions: Dict[str, Any]):
    """
    Map a pattern's condition keys onto a registered evaluator.

    Returns `(None, {})` when no registered evaluator covers the
    conditions — a pattern keyed on something the laboratory cannot
    filter on cannot become an experiment, and inventing an evaluator
    to fit it would be exactly the arbitrary-code path §80 forbids.
    """
    keys = set(conditions)
    if keys == {"expected_direction", "horizon"}:
        return "signal_composite", {"direction": conditions["expected_direction"],
                                    "horizon": conditions["horizon"],
                                    "subject_kind": "signal"}
    if keys == {"event_type", "expected_direction"}:
        return "signal_composite", {"event_type": conditions["event_type"],
                                    "direction": conditions["expected_direction"],
                                    "subject_kind": "signal"}
    if keys == {"event_type", "horizon"}:
        return "signal_composite", {"event_type": conditions["event_type"],
                                    "horizon": conditions["horizon"],
                                    "subject_kind": "signal"}
    if keys == {"event_type"}:
        return "signal_event_filter", {"event_type": conditions["event_type"],
                                       "subject_kind": "signal"}
    if keys == {"horizon"}:
        return "signal_horizon", {"horizon": conditions["horizon"],
                                  "subject_kind": "signal"}
    if keys == {"expected_direction"}:
        return "signal_direction", {"direction": conditions["expected_direction"],
                                    "subject_kind": "signal"}
    return None, {}


# ======================================================================
# Error -> hypothesis (§22, §70)
# ======================================================================

#: Recurring errors and the experiment each suggests. The mapping is
#: stated rather than inferred, because "a timing error suggests a
#: delayed entry" is a research judgement and belongs where it can be
#: argued with.
_ERROR_HYPOTHESES = {
    "timing_error": {
        "statement": "Signals whose favourable move was not captured share a "
                     "characteristic that a filter can identify in advance.",
        "mechanism": ("Recurring timing errors mean the move existed and the "
                      "close gave it back. If the affected signals share a "
                      "measurable property known at decision time, filtering "
                      "on it should raise realised return without touching "
                      "the prediction."),
        "suggested": "test a strength floor, then a horizon change",
    },
    "magnitude_error": {
        "statement": "The expected-return scale is miscalibrated for a "
                     "identifiable subset of signals.",
        "mechanism": ("Recurring magnitude errors with correct direction point "
                      "at the scale rather than the model: the sign is right "
                      "and the size is not."),
        "suggested": "test a horizon change before touching the model",
    },
    "horizon_mismatch": {
        "statement": "The stated horizon is wrong for an identifiable subset.",
        "mechanism": ("Recurring horizon mismatches mean the direction was "
                      "eventually right. If they cluster on one event type or "
                      "one asset class, that subset wants a different window."),
        "suggested": "test each horizon against the ladder",
    },
    "prediction_error": {
        "statement": "Directional errors cluster in an identifiable cohort.",
        "mechanism": ("If wrong calls concentrate somewhere measurable at "
                      "decision time, excluding that cohort should raise "
                      "accuracy. If they do not, the problem is the model and "
                      "no filter will fix it."),
        "suggested": "test cohort filters; expect most to fail",
    },
    "signal_error": {
        "statement": "The suppression rule is withholding correct calls.",
        "mechanism": ("Recurring signal errors mean suppressed signals turned "
                      "out right. The question is whether the suppressed set "
                      "is distinguishable in advance from the ones "
                      "suppression correctly caught."),
        "suggested": "compare suppressed against active cohorts",
    },
}


def propose_from_recurring_error(conn: sqlite3.Connection, error_type: str, *,
                                 attribution_version: str = "v1",
                                 min_occurrences: int = 30,
                                 created_by: str = "") -> Optional[Experiment]:
    """
    Turn a recurring error into a DRAFT experiment (§22, §70).

    Returns None when the error is not recurrent enough to be worth an
    experiment, or when it is one this laboratory has no evaluator for.

    Proposes. Never runs.
    """
    from src.data_access.attribution_schema import initialize_attribution_schema
    initialize_attribution_schema(conn)
    initialize_experiment_schema(conn)

    if error_type not in _ERROR_HYPOTHESES:
        return None

    count = conn.execute("""
        SELECT COUNT(*) FROM error_attributions
        WHERE method_version = ? AND error_type = ? AND role = 'primary'
          AND observability = 'observed'
    """, (attribution_version, error_type)).fetchone()[0]
    if count < min_occurrences:
        return None

    template = _ERROR_HYPOTHESES[error_type]
    family = _family_id(f"error:{error_type}")
    ensure_family(conn, family, f"responses to recurring {error_type}",
                  template["statement"], created_by=created_by)

    return filter_experiment(
        name=f"response to recurring {error_type}",
        evaluator="signal_strength_threshold",
        parameters={"threshold": 0.5, "subject_kind": "signal"},
        metric=("mean_return" if error_type == "timing_error"
                else "directional_accuracy"),
        statement=template["statement"],
        mechanism=(f"{count} outcomes carry {error_type} as their primary "
                   f"attribution. " + template["mechanism"] + " "
                   + MINED_CAVEAT),
        population="all signal experiences with a measured outcome",
        family_id=family,
        source=HypothesisSource.ERROR_ATTRIBUTION,
        source_reference=f"{error_type}:{count}",
        created_by=created_by)


def propose_all(conn: sqlite3.Connection, *, limit: int = 10,
                created_by: str = "") -> List[Experiment]:
    """
    Every proposal the current record supports, as DRAFTS.

    Bounded, and it does not run anything. §76 allows exposing "create
    experiment from memory" while keeping execution human-controlled,
    and the bound exists so that a record with ten thousand patterns
    cannot generate ten thousand experiments and drown the family
    counts that make selection bias visible.
    """
    from src.data_access.memory_schema import initialize_memory_schema
    initialize_memory_schema(conn)

    proposals: List[Experiment] = []
    for (pattern_id,) in conn.execute("""
        SELECT pattern_id FROM memory_patterns
        WHERE quality IN ('confirmed','requires_review')
        ORDER BY sample_size DESC LIMIT ?
    """, (limit,)):
        proposal = propose_from_memory_pattern(conn, pattern_id,
                                               created_by=created_by)
        if proposal is not None:
            proposals.append(proposal)
        if len(proposals) >= limit:
            break

    for error_type in _ERROR_HYPOTHESES:
        if len(proposals) >= limit:
            break
        proposal = propose_from_recurring_error(conn, error_type,
                                                created_by=created_by)
        if proposal is not None:
            proposals.append(proposal)
    return proposals
