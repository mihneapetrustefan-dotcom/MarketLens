"""
src/autoresearch/observations.py
------------------------------------------
Phase 23 §4 — what the researcher is able to notice, and what it is not.

Every detector here reads existing tables and returns
`ResearchObservation`s carrying `Evidence` that points back at rows.
Nothing is computed twice: hit rates come from Phase 21's patterns,
error counts from Phase 20's attributions, effects from Phase 22's
results. A second calculation of the same quantity would eventually
disagree with the first, and then neither could be trusted.

THE DECLARED-BUT-BLIND DETECTORS ARE THE HONEST PART
--------------------------------------------------------
§4 lists fifteen things a research engine may observe. This system can
currently see nine of them. The remaining six — execution behaviour,
portfolio behaviour, feature importance, feature instability, regime
dependence, and outcome anomalies beyond what attribution already
records — have no source tables in this database.

They are registered anyway, each returning a stated reason rather than
an empty list. Phase 22 established why: an empty result flows
downstream and is read as "we looked and there was nothing", which is
a different and much more flattering claim than "we cannot look".

A RESEARCH ENGINE THAT REPORTS ONLY WHAT IT CAN SEE, WITHOUT SAYING
WHAT IT CANNOT, IS DESCRIBING ITS INSTRUMENTS AND CALLING IT THE WORLD.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from src.domain.autoresearch_models import (
    MIN_RESEARCH_SAMPLE, Evidence, ObservationKind, QuestionSource,
    ResearchObservation, _digest,
)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _observation_id(kind: str, subject: str) -> str:
    return "obs-" + _digest({"kind": kind, "subject": subject})[:20]


class DetectorUnavailable(Exception):
    """
    The inputs for this detector do not exist.

    Raised rather than returning `[]`, for the reason in the module
    docstring: silence and absence are different findings.
    """


@dataclass
class DetectorSpec:
    name: str
    kind: ObservationKind
    source: QuestionSource
    description: str
    requires: Tuple[str, ...]
    available: bool = True
    unavailable_reason: str = ""


_DETECTORS: Dict[str, Tuple[DetectorSpec, Callable]] = {}


def register(spec: DetectorSpec):
    def decorator(function):
        _DETECTORS[spec.name] = (spec, function)
        return function
    return decorator


def registered() -> List[DetectorSpec]:
    return [spec for spec, _fn in _DETECTORS.values()]


def get(name: str) -> Tuple[DetectorSpec, Callable]:
    if name not in _DETECTORS:
        raise DetectorUnavailable(
            "No detector named %r. Registered: %s"
            % (name, ", ".join(sorted(_DETECTORS))))
    return _DETECTORS[name]


# ======================================================================
# Detectors that can run
# ======================================================================

@register(DetectorSpec(
    name="recurring_error",
    kind=ObservationKind.RECURRING_ERROR,
    source=QuestionSource.ERROR_PATTERN,
    description="Error types Phase 20 attributes repeatedly.",
    requires=("error_attributions",)))
def recurring_error(conn: sqlite3.Connection, *, limit: int = 10
                    ) -> List[ResearchObservation]:
    """
    Which failures keep happening (§4).

    Only PRIMARY, OBSERVED attributions count. Contributing roles would
    double-count the same failure, and unobserved ones are Phase 20's
    way of saying it could not tell -- counting those would turn
    missing evidence into a research subject.
    """
    if not _table_exists(conn, "error_attributions"):
        raise DetectorUnavailable(
            "error_attributions does not exist: attribution has never run "
            "on this database, so no error can be called recurring.")

    rows = conn.execute("""
        SELECT error_type, COUNT(*) AS n,
               AVG(CASE WHEN deviation IS NOT NULL THEN deviation END),
               COUNT(DISTINCT instrument_id)
        FROM error_attributions
        WHERE role = 'primary' AND observability = 'observed'
          AND status != 'insufficient_evidence'
          AND error_type NOT IN ('no_error', 'unknown', 'expected_loss')
        GROUP BY error_type
        HAVING n >= ?
        ORDER BY n DESC LIMIT ?
    """, (MIN_RESEARCH_SAMPLE, limit)).fetchall()

    found: List[ResearchObservation] = []
    for error_type, count, mean_deviation, instruments in rows:
        observation = ResearchObservation(
            observation_id=_observation_id("recurring_error", error_type),
            kind=ObservationKind.RECURRING_ERROR,
            subject=error_type,
            statement=(
                "%s is the attributed primary cause in %d observed cases "
                "across %d instruments." % (error_type, count, instruments or 0)),
            sample_size=int(count),
            measures={"count": int(count),
                      "mean_deviation": mean_deviation,
                      "instrument_count": int(instruments or 0)},
            evidence=[Evidence(
                kind="error_attributions",
                reference="error_type=%s" % error_type,
                detail="%d primary observed attributions" % count)],
            source_kind=QuestionSource.ERROR_PATTERN,
            source_reference=error_type)
        observation.validate()
        found.append(observation)
    return found


@register(DetectorSpec(
    name="recurring_success",
    kind=ObservationKind.RECURRING_SUCCESS,
    source=QuestionSource.MEMORY_PATTERN,
    description="Memory patterns whose hit rate stands above the base rate.",
    requires=("memory_patterns",)))
def recurring_success(conn: sqlite3.Connection, *, limit: int = 10
                      ) -> List[ResearchObservation]:
    """
    Cohorts that have done better than the record as a whole (§4).

    Only CONFIRMED patterns with a real sample. Phase 21 marks 68% of
    its patterns WEAK and quotes no rate for them; building research on
    those would be building on a number Phase 21 explicitly declined to
    state.

    The base rate comes from the same table, so "better" means better
    than this record rather than better than 50%.
    """
    if not _table_exists(conn, "memory_patterns"):
        raise DetectorUnavailable(
            "memory_patterns does not exist: Phase 21 has not run on this "
            "database, so there is no record of what has worked.")

    base = conn.execute("""
        SELECT CAST(SUM(hits) AS REAL) / NULLIF(SUM(hits + misses), 0)
        FROM memory_patterns WHERE hit_rate IS NOT NULL
    """).fetchone()[0]
    if base is None:
        raise DetectorUnavailable(
            "no pattern carries a hit rate, so there is no base rate to "
            "compare a cohort against.")

    rows = conn.execute("""
        SELECT pattern_id, pattern_type, conditions_json, sample_size,
               hit_rate, mean_return, quality, confidence, stability,
               instrument_count
        FROM memory_patterns
        WHERE quality = 'confirmed' AND sample_size >= ?
          AND hit_rate IS NOT NULL AND hit_rate > ?
        ORDER BY sample_size DESC LIMIT ?
    """, (MIN_RESEARCH_SAMPLE, base, limit)).fetchall()

    found: List[ResearchObservation] = []
    for (pattern_id, pattern_type, conditions_json, sample_size, hit_rate,
         mean_return, quality, confidence, stability, instruments) in rows:
        try:
            conditions = json.loads(conditions_json or "{}")
        except (TypeError, ValueError):
            conditions = {}
        label = ", ".join("%s=%s" % (k, conditions[k]) for k in sorted(conditions))
        observation = ResearchObservation(
            observation_id=_observation_id("recurring_success", pattern_id),
            kind=ObservationKind.RECURRING_SUCCESS,
            subject=label or pattern_type,
            statement=(
                "The cohort %s has a %.1f%% hit rate over %d experiences, "
                "against a %.1f%% base rate across the record."
                % (label or pattern_type, 100 * hit_rate, sample_size,
                   100 * base)),
            sample_size=int(sample_size),
            measures={"hit_rate": hit_rate, "base_rate": base,
                      "excess": hit_rate - base, "mean_return": mean_return,
                      "quality": quality, "confidence": confidence,
                      "stability": stability,
                      "instrument_count": int(instruments or 0),
                      "conditions": conditions},
            evidence=[Evidence(kind="memory_patterns", reference=pattern_id,
                               detail="%d experiences, quality %s, %s"
                                      % (sample_size, quality, stability))],
            source_kind=QuestionSource.MEMORY_PATTERN,
            source_reference=pattern_id)
        observation.validate()
        found.append(observation)
    return found


@register(DetectorSpec(
    name="signal_weakness",
    kind=ObservationKind.SIGNAL_WEAKNESS,
    source=QuestionSource.SIGNAL_ANALYSIS,
    description="Cohorts whose hit rate sits below the base rate.",
    requires=("memory_patterns",)))
def signal_weakness(conn: sqlite3.Connection, *, limit: int = 10
                    ) -> List[ResearchObservation]:
    """
    Where the system is worse than itself.

    The mirror of `recurring_success`, and the more useful half: a
    cohort that underperforms is a candidate for exclusion, which is a
    cheaper and more robust change than adding something new.
    """
    if not _table_exists(conn, "memory_patterns"):
        raise DetectorUnavailable(
            "memory_patterns does not exist: Phase 21 has not run here.")

    base = conn.execute("""
        SELECT CAST(SUM(hits) AS REAL) / NULLIF(SUM(hits + misses), 0)
        FROM memory_patterns WHERE hit_rate IS NOT NULL
    """).fetchone()[0]
    if base is None:
        raise DetectorUnavailable("no pattern carries a hit rate")

    rows = conn.execute("""
        SELECT pattern_id, pattern_type, conditions_json, sample_size,
               hit_rate, quality, stability, instrument_count
        FROM memory_patterns
        WHERE quality = 'confirmed' AND sample_size >= ?
          AND hit_rate IS NOT NULL AND hit_rate < ?
        ORDER BY sample_size DESC LIMIT ?
    """, (MIN_RESEARCH_SAMPLE, base, limit)).fetchall()

    found: List[ResearchObservation] = []
    for (pattern_id, pattern_type, conditions_json, sample_size, hit_rate,
         quality, stability, instruments) in rows:
        try:
            conditions = json.loads(conditions_json or "{}")
        except (TypeError, ValueError):
            conditions = {}
        label = ", ".join("%s=%s" % (k, conditions[k]) for k in sorted(conditions))
        observation = ResearchObservation(
            observation_id=_observation_id("signal_weakness", pattern_id),
            kind=ObservationKind.SIGNAL_WEAKNESS,
            subject=label or pattern_type,
            statement=(
                "The cohort %s has a %.1f%% hit rate over %d experiences, "
                "below the %.1f%% base rate."
                % (label or pattern_type, 100 * hit_rate, sample_size,
                   100 * base)),
            sample_size=int(sample_size),
            measures={"hit_rate": hit_rate, "base_rate": base,
                      "shortfall": base - hit_rate, "quality": quality,
                      "stability": stability,
                      "instrument_count": int(instruments or 0),
                      "conditions": conditions},
            evidence=[Evidence(kind="memory_patterns", reference=pattern_id,
                               detail="%d experiences, %.1f%% hit rate"
                                      % (sample_size, 100 * hit_rate))],
            source_kind=QuestionSource.SIGNAL_ANALYSIS,
            source_reference=pattern_id)
        observation.validate()
        found.append(observation)
    return found


@register(DetectorSpec(
    name="experiment_failure",
    kind=ObservationKind.EXPERIMENT_FAILURE,
    source=QuestionSource.EXPERIMENT_RESULT,
    description="Phase 22 experiments that failed, and how they failed.",
    requires=("experiment_results",)))
def experiment_failure(conn: sqlite3.Connection, *, limit: int = 10
                       ) -> List[ResearchObservation]:
    """
    Failed experiments are observations too (§4, §42).

    A candidate whose in-sample effect was large and out-of-sample
    effect negative is not just a dead end; it is evidence about the
    shape of this dataset, and worth a question of its own.
    """
    if not _table_exists(conn, "experiment_results"):
        raise DetectorUnavailable(
            "experiment_results does not exist: Phase 22 has not run here, "
            "so no experiment has succeeded or failed.")

    rows = conn.execute("""
        SELECT r.experiment_id, e.name, r.effect, r.effect_in_sample,
               r.effect_low, r.effect_high, r.decision, e.family_id
        FROM experiment_results r
        JOIN experiments e ON e.experiment_id = r.experiment_id
        WHERE r.decision IN ('fail', 'inconclusive')
        ORDER BY (COALESCE(r.effect_in_sample, 0) - COALESCE(r.effect, 0)) DESC
        LIMIT ?
    """, (limit,)).fetchall()

    found: List[ResearchObservation] = []
    for (experiment_id, name, effect, in_sample, low, high, decision,
         family_id) in rows:
        gap = None
        if effect is not None and in_sample is not None:
            gap = in_sample - effect
        spans_zero = (low is not None and high is not None
                      and low <= 0 <= high)
        observation = ResearchObservation(
            observation_id=_observation_id("experiment_failure", experiment_id),
            kind=ObservationKind.EXPERIMENT_FAILURE,
            subject=name or experiment_id,
            statement=(
                "Experiment %r ended %s with an out-of-sample effect of %s%s."
                % (name or experiment_id, decision,
                   "unmeasured" if effect is None else "%+0.4f" % effect,
                   "" if gap is None else
                   " and an in-sample/out-of-sample gap of %+0.4f" % gap)),
            sample_size=MIN_RESEARCH_SAMPLE,
            measures={"effect": effect, "effect_in_sample": in_sample,
                      "gap": gap, "decision": decision,
                      "interval_spans_zero": spans_zero,
                      "family_id": family_id},
            evidence=[Evidence(kind="experiment_results",
                               reference=experiment_id,
                               detail="decision %s" % decision)],
            source_kind=QuestionSource.EXPERIMENT_RESULT,
            source_reference=experiment_id)
        observation.validate()
        found.append(observation)
    return found


@register(DetectorSpec(
    name="model_degradation",
    kind=ObservationKind.MODEL_DEGRADATION,
    source=QuestionSource.MODEL_ANALYSIS,
    description="Models that do not clear the Phase 18 baseline gate.",
    requires=("model_evaluations",)))
def model_degradation(conn: sqlite3.Connection, *, limit: int = 10
                      ) -> List[ResearchObservation]:
    """
    Models that fail to beat every baseline.

    THE GATE IS NOT RE-IMPLEMENTED HERE. Phase 18 already decided what
    "good enough" means and `model_evaluations.beats_all_baselines`
    records the answer; this detector reads that column. A second
    definition of deployability living in the research layer is exactly
    how two parts of a system start disagreeing about whether a model
    can be used, and the research layer would be the one nobody
    thought to check.

    The accuracy figure is pulled from `metrics_json` for the
    statement only. It is descriptive text, not a second judgement --
    the verdict is the stored gate result.
    """
    if not _table_exists(conn, "model_evaluations"):
        raise DetectorUnavailable(
            "model_evaluations does not exist: no model has been evaluated.")

    rows = conn.execute("""
        SELECT evaluation_id, trained_model_id, model_qualified_id,
               window_label, sample_size, effective_sample_size, small_sample,
               beats_all_baselines, metrics_json
        FROM model_evaluations
        WHERE beats_all_baselines = 0
        ORDER BY effective_sample_size DESC LIMIT ?
    """, (limit,)).fetchall()

    found: List[ResearchObservation] = []
    for (evaluation_id, model_id, qualified, window, sample, effective, small,
         beats, metrics_json) in rows:
        try:
            metrics = json.loads(metrics_json or "{}")
        except (TypeError, ValueError):
            metrics = {}
        accuracy = metrics.get("directional_accuracy")
        detail = ("directional accuracy %.4f" % accuracy
                  if isinstance(accuracy, (int, float))
                  else "no directional accuracy recorded")
        observation = ResearchObservation(
            # Keyed on the EVALUATION, not the model. One model is
            # evaluated many times, and collapsing those into a single
            # observation silently discarded three of the four found
            # here -- while `save` cheerfully reported writing all four.
            observation_id=_observation_id("model_degradation",
                                           str(evaluation_id)),
            kind=ObservationKind.MODEL_DEGRADATION,
            subject=str(qualified or model_id),
            statement=(
                "Model %s does not beat every baseline on window %s "
                "(%s, effective sample %s%s)."
                % (qualified or model_id, window, detail, effective,
                   ", flagged small sample" if small else "")),
            sample_size=int(effective or sample or 0),
            measures={"directional_accuracy": accuracy,
                      "sample_size": sample,
                      "effective_sample_size": effective,
                      "small_sample": bool(small),
                      "beats_all_baselines": bool(beats),
                      "window_label": window},
            evidence=[Evidence(kind="model_evaluations",
                               reference=str(evaluation_id),
                               detail="beats_all_baselines=0, " + detail)],
            source_kind=QuestionSource.MODEL_ANALYSIS,
            source_reference=str(qualified or model_id))
        observation.validate()
        found.append(observation)
    return found


# ======================================================================
# Detectors that are declared and cannot run here
# ======================================================================

def _blind(spec: DetectorSpec):
    def detector(conn, **_kwargs):
        raise DetectorUnavailable(
            "%s cannot observe anything here: %s It is registered so the "
            "gap is visible rather than silent, and it will work when its "
            "inputs exist." % (spec.name, spec.unavailable_reason))
    return detector


for _spec in (
    DetectorSpec(
        name="regime_dependence", kind=ObservationKind.REGIME_DEPENDENCE,
        source=QuestionSource.REGIME_ANALYSIS,
        description="Behaviour that differs by market regime.",
        requires=("trading_experiences.market_regime",), available=False,
        unavailable_reason=(
            "market_regime is NULL on every experience, so no cohort can "
            "be split by regime.")),
    DetectorSpec(
        name="execution_behaviour", kind=ObservationKind.EXECUTION_BEHAVIOUR,
        source=QuestionSource.SIGNAL_ANALYSIS,
        description="Slippage and fill behaviour against expectation.",
        requires=("executions", "orders"), available=False,
        unavailable_reason="no order has ever been placed, so there are no fills."),
    DetectorSpec(
        name="portfolio_behaviour", kind=ObservationKind.PORTFOLIO_BEHAVIOUR,
        source=QuestionSource.SIGNAL_ANALYSIS,
        description="Position sizing and allocation against outcome.",
        requires=("portfolio_positions",), available=False,
        unavailable_reason="no portfolio or position exists."),
    DetectorSpec(
        name="feature_importance", kind=ObservationKind.FEATURE_IMPORTANCE,
        source=QuestionSource.MODEL_ANALYSIS,
        description="Which features carry a model.",
        requires=("model_feature_importance",), available=False,
        unavailable_reason=(
            "no per-feature importance is stored; only aggregate model "
            "evaluations exist.")),
    DetectorSpec(
        name="feature_instability", kind=ObservationKind.FEATURE_INSTABILITY,
        source=QuestionSource.MODEL_ANALYSIS,
        description="Features whose importance moves between refits.",
        requires=("model_feature_importance",), available=False,
        unavailable_reason=(
            "importance is not stored per refit, so it cannot be compared "
            "across them.")),
    DetectorSpec(
        name="event_reaction", kind=ObservationKind.EVENT_REACTION,
        source=QuestionSource.EVENT_ANALYSIS,
        description="How instruments react to an event class over time.",
        requires=("event_studies", "trading_experiences"), available=False,
        unavailable_reason=(
            "event studies exist but are not linked to experience outcomes, "
            "so a reaction cannot be scored against what was expected.")),
):
    _DETECTORS[_spec.name] = (_spec, _blind(_spec))


# ======================================================================
# Running them all
# ======================================================================

def observe_all(conn: sqlite3.Connection, *, limit_each: int = 6
                ) -> Tuple[List[ResearchObservation], List[Dict[str, str]]]:
    """
    Every detector that can run, plus a note for every one that cannot.

    Returns `(observations, blind_spots)`. The second half is not an
    error list: it is the part of §4 this system cannot currently see,
    and it belongs in the same return value as the findings so a caller
    cannot report one without the other.
    """
    observations: List[ResearchObservation] = []
    blind_spots: List[Dict[str, str]] = []
    for name in sorted(_DETECTORS):
        spec, function = _DETECTORS[name]
        try:
            observations.extend(function(conn, limit=limit_each))
        except DetectorUnavailable as exc:
            blind_spots.append({"detector": name, "reason": str(exc)})
        except TypeError:
            # A blind detector takes no `limit`.
            try:
                function(conn)
            except DetectorUnavailable as exc:
                blind_spots.append({"detector": name, "reason": str(exc)})
    return observations, blind_spots


def save(conn: sqlite3.Connection,
         observations: Sequence[ResearchObservation]) -> int:
    """Persist observations. Idempotent on (observation_id, version)."""
    from src.data_access.autoresearch_schema import initialize_autoresearch_schema
    initialize_autoresearch_schema(conn)

    # A batch whose ids collide would write fewer rows than it was
    # handed and report the larger number, which is how 26 observations
    # became 23 rows and nobody was told. That is a bug in whichever
    # detector built the id, so it is raised rather than absorbed.
    seen: Dict[str, int] = {}
    for observation in observations:
        seen[observation.observation_id] = seen.get(observation.observation_id, 0) + 1
    collisions = sorted(key for key, count in seen.items() if count > 1)
    if collisions:
        raise ValueError(
            "%d observation id(s) appear more than once in this batch (%s). "
            "The detector that produced them is keying on something that "
            "does not distinguish its findings, so storing the batch would "
            "silently drop rows."
            % (len(collisions), ", ".join(collisions[:3])))

    written = 0
    for observation in observations:
        conn.execute("""
            INSERT OR REPLACE INTO autoresearch_observations (
                observation_id, method_version, kind, subject, statement,
                sample_size, evidence_json, measures_json, source_kind,
                source_reference, observed_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (observation.observation_id, observation.method_version,
              observation.kind.value, observation.subject,
              observation.statement, observation.sample_size,
              json.dumps([e.as_dict() for e in observation.evidence]),
              json.dumps(observation.measures, default=str),
              observation.source_kind.value, observation.source_reference,
              observation.observed_at))
        written += 1
    conn.commit()
    return written
