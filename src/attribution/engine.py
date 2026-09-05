"""
src/attribution/engine.py
---------------------------------
Running the detectors, ranking the findings, and knowing when to stop.

THE PIPELINE (§41)
----------------------
    outcome
      -> run every detector
      -> keep the ones that fired, with their evidence
      -> rank: primary vs contributing
      -> assign status
      -> queue for review where the rules cannot decide
      -> store

WHAT MAKES ONE ERROR PRIMARY (§20)
--------------------------------------
Not detector order, and not severity. Two things, in order:

  1. **Causal depth.** An error early in the chain explains the ones
     after it. If the direction was wrong, the timing of a move that
     was never going to happen is a consequence, not a cause — so
     PREDICTION_ERROR outranks TIMING_ERROR whenever both fire.
  2. **Evidence strength**, as a tie-break within a depth.

That ordering is a claim about causation and it is written down as
`_CAUSAL_DEPTH` rather than left implicit in a sort, so that
disagreeing with it means editing a documented table rather than
reverse-engineering a comparator.

WHEN THE RULES REFUSE TO DECIDE (§47)
-----------------------------------------
Two findings of equal depth and equal confidence do not get an
arbitrary winner. The case goes to REQUIRES_REVIEW with both
candidates named, because picking one by tie-break would manufacture a
certainty the evidence does not contain.

NOTHING HERE CHANGES ANYTHING (§59)
---------------------------------------
No model, strategy, threshold, weight, risk limit or capital figure is
modified by any code path in this package. It reads and it records.
`tests/attribution/test_leakage.py` asserts that structurally.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.attribution import detectors as rules
from src.domain.attribution_models import (
    ATTRIBUTION_METHOD_VERSION, MIN_COHORT_SAMPLE, AttributionConfidence,
    AttributionRole, AttributionStatus, DetectorResult, ErrorAttribution,
    ErrorType, Evidence, Observability, Severity,
)

#: How far upstream each error sits. Lower is earlier, and earlier
#: explains later. This is the causal claim of the whole phase, stated
#: once, in the open.
#:
#: DATA sits first: a decision made on invalid data explains everything
#: downstream of it, including a wrong direction.
#: PREDICTION before SIGNAL before TIMING: a wrong direction makes the
#: signal translation and the entry timing consequences rather than
#: causes.
#: EXECUTION and PORTFOLIO sit last: they can only ever add to a loss
#: whose direction was already decided.
_CAUSAL_DEPTH: Dict[ErrorType, int] = {
    ErrorType.DATA_ERROR: 0,
    ErrorType.REGIME_ERROR: 1,
    ErrorType.PREDICTION_ERROR: 2,
    ErrorType.HORIZON_MISMATCH: 3,
    ErrorType.MAGNITUDE_ERROR: 4,
    ErrorType.SIGNAL_ERROR: 5,
    ErrorType.TIMING_ERROR: 6,
    ErrorType.SIZING_ERROR: 7,
    ErrorType.RISK_ERROR: 8,
    ErrorType.EXECUTION_ERROR: 9,
    ErrorType.PORTFOLIO_ERROR: 10,
}


@dataclass
class AttributionReport:
    """What one pass did."""
    outcomes_seen: int = 0
    attributed: int = 0
    no_error: int = 0
    expected_loss: int = 0
    unknown: int = 0
    insufficient: int = 0
    requires_review: int = 0
    skipped_existing: int = 0
    findings: int = 0
    by_type: Dict[str, int] = field(default_factory=dict)
    unjudgeable_layers: Dict[str, int] = field(default_factory=dict)
    method_version: str = ATTRIBUTION_METHOD_VERSION

    def note_layer(self, result: DetectorResult) -> None:
        if not result.judgeable:
            key = f"{result.error_type.value} ({result.missing})"
            self.unjudgeable_layers[key] = self.unjudgeable_layers.get(key, 0) + 1

    def as_dict(self) -> Dict[str, Any]:
        return {
            "outcomes_seen": self.outcomes_seen,
            "attributed": self.attributed,
            "findings": self.findings,
            "no_error": self.no_error,
            "expected_loss": self.expected_loss,
            "unknown": self.unknown,
            "insufficient_evidence": self.insufficient,
            "requires_review": self.requires_review,
            "skipped_existing": self.skipped_existing,
            "by_type": dict(sorted(self.by_type.items())),
            "unjudgeable_layers": dict(sorted(self.unjudgeable_layers.items())),
            "method_version": self.method_version,
        }


def _expectedness(outcome: Dict[str, Any],
                  cohort: Optional[Dict[str, Any]]) -> Tuple[bool, Optional[Evidence]]:
    """
    Was this result ordinary for its cohort (§26, §27)?

    Uses the cohort's own 10th-90th percentile band from Phase 19's
    aggregates. No new distributional assumption is introduced — the
    percentiles were already computed, and reusing them keeps one
    definition of "unusual" in the repository.

    Returns `(is_unexpected, evidence)`. Below `MIN_COHORT_SAMPLE` it
    returns `(False, None)`: a cohort of eleven cannot say what is
    unusual, and treating it as though it could is how small samples
    become confident claims (§64).
    """
    realized = outcome.get("simple_return")
    if realized is None or cohort is None:
        return False, None
    if (cohort.get("sample_size") or 0) < MIN_COHORT_SAMPLE:
        return False, None
    low, high = cohort.get("p10_return"), cohort.get("p90_return")
    if low is None or high is None:
        return False, None

    if low <= realized <= high:
        return False, Evidence(
            kind="expectedness", source="outcome_aggregates",
            statement=(f"the {realized:+.2%} result sits inside the cohort's "
                       f"usual range ({low:+.2%} to {high:+.2%}, n="
                       f"{cohort.get('sample_size')}) — an ordinary result, "
                       f"not evidence of a mistake"),
            value=realized, comparison=low,
            detail={"p10": low, "p90": high,
                    "sample_size": cohort.get("sample_size")})

    return True, Evidence(
        kind="expectedness", source="outcome_aggregates",
        statement=(f"the {realized:+.2%} result falls outside the cohort's "
                   f"10th-90th percentile band ({low:+.2%} to {high:+.2%}, n="
                   f"{cohort.get('sample_size')}) — unusual for this cohort"),
        value=realized, comparison=high,
        detail={"p10": low, "p90": high,
                "sample_size": cohort.get("sample_size")})


def run_detectors(outcome: Dict[str, Any], *,
                  siblings: Sequence[Dict[str, Any]] = (),
                  signal: Optional[Dict[str, Any]] = None,
                  observation: Optional[Dict[str, Any]] = None,
                  regime_cohort: Optional[Dict[str, Any]] = None,
                  position: Optional[Dict[str, Any]] = None,
                  risk_decision: Optional[Dict[str, Any]] = None,
                  fill: Optional[Dict[str, Any]] = None,
                  portfolio: Optional[Dict[str, Any]] = None
                  ) -> List[DetectorResult]:
    """Every detector, in a fixed order, on one outcome."""
    return [
        rules.detect_prediction_error(outcome),
        rules.detect_magnitude_error(outcome),
        rules.detect_horizon_mismatch(outcome, siblings),
        rules.detect_timing_error(outcome),
        rules.detect_signal_error(outcome, signal),
        rules.detect_data_error(outcome, observation),
        rules.detect_regime_error(outcome, regime_cohort),
        rules.detect_sizing_error(outcome, position),
        rules.detect_risk_error(outcome, risk_decision),
        rules.detect_execution_error(outcome, fill),
        rules.detect_portfolio_error(outcome, portfolio),
    ]


def _rank(fired: Sequence[DetectorResult]) -> Tuple[DetectorResult, bool]:
    """
    Pick the primary finding, and say whether the choice was clear.

    Returns `(winner, ambiguous)`. `ambiguous` is True when a second
    finding has the same causal depth and the same confidence — in
    which case the caller sends it to review rather than pretending the
    tie-break meant something (§47).
    """
    ordered = sorted(
        fired,
        key=lambda r: (_CAUSAL_DEPTH.get(r.error_type, 99), -r.confidence.rank))
    winner = ordered[0]
    if len(ordered) > 1:
        runner_up = ordered[1]
        ambiguous = (
            _CAUSAL_DEPTH.get(runner_up.error_type, 99)
            == _CAUSAL_DEPTH.get(winner.error_type, 99)
            and runner_up.confidence.rank == winner.confidence.rank)
    else:
        ambiguous = False
    return winner, ambiguous


def attribute(outcome: Dict[str, Any], *,
              cohort: Optional[Dict[str, Any]] = None,
              method_version: str = ATTRIBUTION_METHOD_VERSION,
              **evidence_sources: Any
              ) -> Tuple[List[ErrorAttribution], List[DetectorResult], Optional[str]]:
    """
    Diagnose one outcome.

    Returns `(attributions, all_detector_results, review_reason)`.
    `review_reason` is None unless a person is needed.

    An outcome that produced no finding still produces a row — NO_ERROR
    or EXPECTED_LOSS — because "we looked and found nothing wrong" is a
    result worth recording, and a diagnostic layer that only ever
    stores faults gives a systematically pessimistic picture of the
    system it is diagnosing.
    """
    results = run_detectors(outcome, **evidence_sources)
    fired = [r for r in results if r.fired]

    realized = outcome.get("simple_return")
    expected = outcome.get("expected_return")
    deviation = (realized - expected
                 if realized is not None and expected is not None else None)

    def build(error_type: ErrorType, *, role: AttributionRole,
              confidence: AttributionConfidence, severity: Severity,
              summary: str, evidence: Sequence[Evidence],
              status: AttributionStatus) -> ErrorAttribution:
        return ErrorAttribution(
            subject_kind=outcome.get("subject_kind", ""),
            subject_id=outcome.get("subject_id", ""),
            horizon=outcome.get("horizon", ""),
            error_type=error_type, method_version=method_version,
            role=role, confidence=confidence, severity=severity,
            status=status, observability=Observability.OBSERVED,
            summary=summary, evidence=list(evidence),
            expected_direction=outcome.get("expected_direction") or "",
            expected_return=expected, realized_return=realized,
            deviation=deviation,
            instrument_id=outcome.get("instrument_id") or "",
            trained_model_id=outcome.get("trained_model_id"),
            model_status=outcome.get("model_status"),
            strategy_id=outcome.get("strategy_id"),
            market_regime=outcome.get("market_regime"),
            event_type=outcome.get("event_type"),
            confidence_score=outcome.get("confidence"),
            strength=outcome.get("strength"),
            signal_status=outcome.get("signal_status"),
            outcome_method_version=outcome.get("method_version") or "")

    review_reason: Optional[str] = None

    # ---- the outcome itself was never measured ----------------------
    # This gate has to come first. Peripheral detectors — data quality,
    # for one — can still answer on a PENDING outcome, and without this
    # the engine reached "nothing fired" and concluded NO_ERROR for
    # 2,199 measurements whose result is simply not known yet.
    #
    # "Every layer behaved as designed" is a claim about a result. You
    # cannot make it about a result you do not have.
    if outcome.get("status") != "available":
        return ([build(
            ErrorType.UNKNOWN, role=AttributionRole.PRIMARY,
            confidence=AttributionConfidence.INSUFFICIENT_EVIDENCE,
            severity=Severity.INFO,
            status=AttributionStatus.INSUFFICIENT_EVIDENCE,
            summary=(f"the outcome is {outcome.get('status')}; there is no "
                     f"result to diagnose"),
            evidence=[Evidence(
                kind="missing_input", source="outcome_measurements.status",
                statement=(f"Phase 19 recorded this measurement as "
                           f"{outcome.get('status')}, so what happened is not "
                           f"known. No layer can be cleared and none can be "
                           f"blamed."))])],
            results, None)

    # ---- nothing could be judged at all -----------------------------
    if not any(r.judgeable for r in results):
        missing = sorted({r.missing for r in results if r.missing})
        return ([build(
            ErrorType.UNKNOWN, role=AttributionRole.PRIMARY,
            confidence=AttributionConfidence.INSUFFICIENT_EVIDENCE,
            severity=Severity.INFO,
            status=AttributionStatus.INSUFFICIENT_EVIDENCE,
            summary="no layer could be assessed",
            evidence=[Evidence(
                kind="missing_input", source=", ".join(missing),
                statement=("none of the attribution layers had the inputs they "
                           "require. No explanation is offered rather than a "
                           "guess: " + "; ".join(missing)))])],
            results, None)

    # ---- something was judged and nothing fired ---------------------
    if not fired:
        unexpected, evidence = _expectedness(outcome, cohort)
        judged = [r for r in results if r.judgeable]
        base = [Evidence(
            kind="checks", source="attribution.detectors",
            statement=(f"{len(judged)} layer(s) were assessed and none showed "
                       f"a deviation: "
                       + "; ".join(f"{r.error_type.value}: {r.summary}"
                                   for r in judged if r.summary)))]
        if evidence:
            base.append(evidence)

        if realized is not None and realized < 0 and not unexpected and evidence:
            # A loss inside the cohort's usual range. Not a mistake —
            # §18 requires this outcome to be reachable, and forcing it
            # into an error class is the failure mode the whole phase
            # is written against.
            return ([build(
                ErrorType.EXPECTED_LOSS, role=AttributionRole.PRIMARY,
                confidence=AttributionConfidence.MEDIUM,
                severity=Severity.INFO, status=AttributionStatus.ATTRIBUTED,
                summary=("an ordinary adverse move; every assessable layer "
                         "behaved as designed"),
                evidence=base)], results, None)

        if unexpected:
            # Outside the usual range with no layer explaining it. That
            # is worth a person's attention rather than a manufactured
            # cause (§27, §47).
            review_reason = ("the result is outside the cohort's usual range "
                             "and no layer explains it")
            return ([build(
                ErrorType.UNKNOWN, role=AttributionRole.PRIMARY,
                confidence=AttributionConfidence.LOW,
                severity=Severity.MEDIUM,
                status=AttributionStatus.REQUIRES_REVIEW,
                summary="unusual result with no layer showing a deviation",
                evidence=base)], results, review_reason)

        return ([build(
            ErrorType.NO_ERROR, role=AttributionRole.PRIMARY,
            confidence=AttributionConfidence.MEDIUM,
            severity=Severity.INFO, status=AttributionStatus.ATTRIBUTED,
            summary="every assessable layer behaved as designed",
            evidence=base)], results, None)

    # ---- one or more findings ---------------------------------------
    winner, ambiguous = _rank(fired)
    unjudgeable = [r for r in results if not r.judgeable]
    status = (AttributionStatus.PARTIALLY_ATTRIBUTED if unjudgeable
              else AttributionStatus.ATTRIBUTED)
    if ambiguous:
        status = AttributionStatus.REQUIRES_REVIEW
        review_reason = ("two findings share the same causal depth and the "
                         "same confidence; the rules cannot say which came "
                         "first")

    attributions = []
    for result in fired:
        is_primary = result is winner
        evidence = list(result.evidence)
        if is_primary and len(fired) > 1:
            others = [r.error_type.value for r in fired if r is not winner]
            evidence.append(Evidence(
                kind="ranking", source="attribution.engine",
                statement=(f"ranked primary over {', '.join(others)} because it "
                           f"sits earliest in the causal chain (depth "
                           f"{_CAUSAL_DEPTH.get(result.error_type)}); an error "
                           f"upstream explains the ones after it")))
        if is_primary and unjudgeable:
            evidence.append(Evidence(
                kind="coverage", source="attribution.engine",
                statement=(f"{len(unjudgeable)} layer(s) could not be assessed "
                           f"at all: "
                           + ", ".join(sorted({r.missing for r in unjudgeable
                                               if r.missing}))
                           + ". A cause may lie in one of them.")))
        attributions.append(build(
            result.error_type,
            role=AttributionRole.PRIMARY if is_primary else AttributionRole.CONTRIBUTING,
            confidence=result.confidence, severity=result.severity,
            summary=result.summary, evidence=evidence, status=status))

    return attributions, results, review_reason
