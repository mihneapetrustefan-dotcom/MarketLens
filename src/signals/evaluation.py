"""
src/signals/evaluation.py
------------------------------
Scores signals against what actually happened, and aggregates those
outcomes into evaluations (Phase 10, spec §29, §30).

THE OUTCOME MUST RESOLVE AFTER THE SIGNAL'S INFORMATION CUTOFF
------------------------------------------------------------------
This is the one rule that makes the whole exercise meaningful. A label
measured at or before the cutoff was already knowable when the signal
was made; scoring against it would measure memory, not prediction.
`score_signal` refuses such a pairing outright rather than quietly
producing a flattering number.

A BASELINE IS MANDATORY, EXACTLY AS IN PHASE 9
--------------------------------------------------
A hit rate of 55% means nothing on its own. If the instrument rose on
55% of days in the period, a signal that always said LONG would score
the same. So every evaluation carries the baseline hit rate — the rate
achieved by always predicting the majority direction of the cohort —
and a flag for whether the signals actually beat it.

This mirrors ModelingEngine.MANDATORY_BASELINES deliberately: the same
discipline should not weaken just because we moved one layer up.

SUPPRESSED SIGNALS ARE SCORED TOO, AND COUNTED SEPARATELY
-------------------------------------------------------------
A suppressed signal still made a directional claim before being
withheld. Scoring those answers a question worth asking: is the
suppression throwing away good signals or bad ones? Excluding them
would make the suppression logic unfalsifiable.

They are never mixed into the active-signal cohort, though — that
would inflate or deflate the headline number with signals the system
declined to stand behind.

SMALL SAMPLES ARE FLAGGED, NOT HIDDEN
-----------------------------------------
Same threshold discipline as Phase 9: below MIN_SAMPLE the evaluation
is marked descriptive-only. On the current dataset essentially every
cohort will be flagged. That is the honest reading, and the flag is
persisted rather than reported once and forgotten.
"""

from __future__ import annotations

import hashlib
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

from src.domain.signal_models import Signal, SignalDirection, SignalStatus

#: Below this many outcomes, an evaluation is descriptive only. Same
#: value and same reasoning as Phase 9's MIN_EFFECTIVE_SAMPLE.
MIN_SAMPLE = 30

#: Confidence buckets for slicing performance (spec §29).
CONFIDENCE_BUCKETS = ((0.0, 0.25, "very_low"), (0.25, 0.5, "low"),
                      (0.5, 0.75, "medium"), (0.75, 1.01, "high"))


def confidence_bucket(confidence: Optional[float]) -> str:
    """Bucket a confidence value. None is its own bucket, not merged into a numeric one."""
    if confidence is None:
        return "unknown"
    for low, high, name in CONFIDENCE_BUCKETS:
        if low <= confidence < high:
            return name
    return "unknown"


@dataclass
class SignalOutcome:
    """One signal scored against one realized label."""
    signal_id: str
    horizon: str
    signal_direction: str
    realized_return: Optional[float] = None
    realized_direction: Optional[str] = None
    expected_return: Optional[float] = None
    strength: Optional[float] = None
    confidence: Optional[float] = None
    direction_correct: Optional[bool] = None
    error: Optional[float] = None
    absolute_error: Optional[float] = None
    strategy_id: Optional[str] = None
    strategy_version: Optional[str] = None
    market_regime: Optional[str] = None
    event_type: Optional[str] = None
    confidence_bucket: str = "unknown"
    label_name: Optional[str] = None
    measured_at: Optional[datetime] = None
    scored_at: Optional[datetime] = None


@dataclass
class SignalEvaluation:
    """Aggregate metrics over a cohort of outcomes."""
    evaluation_id: str
    cohort_kind: str
    cohort_value: str
    horizon: str
    sample_size: int = 0
    instrument_count: Optional[int] = None
    hit_rate: Optional[float] = None
    mean_return: Optional[float] = None
    median_return: Optional[float] = None
    mean_absolute_error: Optional[float] = None
    mean_expected_return: Optional[float] = None
    return_stdev: Optional[float] = None
    baseline_hit_rate: Optional[float] = None
    beats_baseline: Optional[bool] = None
    small_sample: bool = True
    notes: List[str] = field(default_factory=list)
    evaluated_at: Optional[datetime] = None


class OutcomeScoringError(ValueError):
    """Raised when a signal and label pairing would be look-ahead."""


def score_signal(signal: Signal, realized_return: Optional[float],
                 horizon: str, label_name: str,
                 measured_at: Optional[datetime],
                 now: Optional[datetime] = None) -> SignalOutcome:
    """
    Compare one signal against its realized outcome.

    Raises OutcomeScoringError if the label resolved at or before the
    signal's information cutoff — see the module docstring for why that
    pairing is never scored rather than merely warned about.
    """
    cutoff = signal.provenance.source_information_cutoff
    if measured_at is not None and cutoff is not None and measured_at <= cutoff:
        raise OutcomeScoringError(
            f"label for {signal.signal_id} resolved at {measured_at}, at or before "
            f"its information cutoff {cutoff} — scoring this would measure hindsight")

    realized_direction = None
    direction_correct = None
    if realized_return is not None:
        realized_direction = (SignalDirection.LONG.value if realized_return > 0
                              else SignalDirection.SHORT.value if realized_return < 0
                              else SignalDirection.NEUTRAL.value)
        # Only directional claims can be right or wrong about direction.
        # NEUTRAL and NO_SIGNAL are scored for return but not for hits.
        if signal.direction in (SignalDirection.LONG, SignalDirection.SHORT):
            direction_correct = (signal.direction.value == realized_direction)

    error = None
    absolute_error = None
    if realized_return is not None and signal.expected_return is not None:
        error = round(signal.expected_return - realized_return, 8)
        absolute_error = abs(error)

    return SignalOutcome(
        signal_id=signal.signal_id, horizon=horizon,
        signal_direction=signal.direction.value,
        realized_return=realized_return, realized_direction=realized_direction,
        expected_return=signal.expected_return, strength=signal.strength,
        confidence=signal.confidence, direction_correct=direction_correct,
        error=error, absolute_error=absolute_error,
        strategy_id=signal.provenance.strategy_id,
        strategy_version=signal.provenance.strategy_version,
        market_regime=signal.context.market_regime,
        event_type=signal.context.event_type,
        confidence_bucket=confidence_bucket(signal.confidence),
        label_name=label_name, measured_at=measured_at,
        scored_at=now or datetime.now(timezone.utc),
    )


def _baseline_hit_rate(outcomes: Sequence[SignalOutcome]) -> Optional[float]:
    """
    The rate a naive always-majority-direction rule would achieve.

    This is the number a real signal has to beat. Computed from the
    REALIZED directions in the cohort, not from the signals — the
    baseline must not depend on what the signals happened to say.
    """
    realized = [o.realized_direction for o in outcomes if o.realized_direction]
    if not realized:
        return None
    ups = sum(1 for d in realized if d == SignalDirection.LONG.value)
    downs = sum(1 for d in realized if d == SignalDirection.SHORT.value)
    if ups + downs == 0:
        return None
    return round(max(ups, downs) / (ups + downs), 6)


def evaluate_cohort(outcomes: Sequence[SignalOutcome], cohort_kind: str,
                    cohort_value: str, horizon: str,
                    instrument_count: Optional[int] = None,
                    now: Optional[datetime] = None) -> SignalEvaluation:
    """Aggregate a set of outcomes into one evaluation, with its baseline."""
    moment = now or datetime.now(timezone.utc)
    raw = f"{cohort_kind}|{cohort_value}|{horizon}"
    evaluation = SignalEvaluation(
        evaluation_id=f"se-{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]}",
        cohort_kind=cohort_kind, cohort_value=cohort_value, horizon=horizon,
        sample_size=len(outcomes), instrument_count=instrument_count,
        evaluated_at=moment,
    )
    if not outcomes:
        evaluation.notes.append("no outcomes in cohort")
        return evaluation

    scored = [o for o in outcomes if o.direction_correct is not None]
    if scored:
        evaluation.hit_rate = round(
            sum(1 for o in scored if o.direction_correct) / len(scored), 6)

    returns = [o.realized_return for o in outcomes if o.realized_return is not None]
    if returns:
        evaluation.mean_return = round(statistics.fmean(returns), 8)
        evaluation.median_return = round(statistics.median(returns), 8)
        if len(returns) > 1:
            evaluation.return_stdev = round(statistics.stdev(returns), 8)

    expected = [o.expected_return for o in outcomes if o.expected_return is not None]
    if expected:
        evaluation.mean_expected_return = round(statistics.fmean(expected), 8)

    errors = [o.absolute_error for o in outcomes if o.absolute_error is not None]
    if errors:
        evaluation.mean_absolute_error = round(statistics.fmean(errors), 8)

    evaluation.baseline_hit_rate = _baseline_hit_rate(outcomes)
    if evaluation.hit_rate is not None and evaluation.baseline_hit_rate is not None:
        evaluation.beats_baseline = evaluation.hit_rate > evaluation.baseline_hit_rate

    evaluation.small_sample = len(outcomes) < MIN_SAMPLE
    if evaluation.small_sample:
        evaluation.notes.append(
            f"sample {len(outcomes)} below {MIN_SAMPLE} — descriptive only, not conclusive")
    if evaluation.hit_rate is not None and not scored:
        evaluation.notes.append("no directional signals to score")
    if evaluation.baseline_hit_rate is None:
        evaluation.notes.append("baseline could not be computed — hit rate is uninterpretable")

    return evaluation


def evaluate_by(outcomes: Sequence[SignalOutcome], attribute: str,
                cohort_kind: str, horizon: str,
                now: Optional[datetime] = None) -> List[SignalEvaluation]:
    """Slice outcomes by one attribute and evaluate each slice separately."""
    groups: Dict[str, List[SignalOutcome]] = {}
    for outcome in outcomes:
        key = getattr(outcome, attribute, None) or "unknown"
        groups.setdefault(str(key), []).append(outcome)
    return [evaluate_cohort(group, cohort_kind, key, horizon, now=now)
            for key, group in sorted(groups.items())]
