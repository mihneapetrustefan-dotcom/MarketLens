"""
src/adaptive/signal_availability.py
-------------------------------------------------
Why every instrument has the answer it has (Phase 25.9I-F.5).

THE STRUCTURAL PROBLEM THIS SOLVES
--------------------------------------
Audited 2026-09-30: the signal pipeline is CANDIDATE-DRIVEN. A strategy
turns a prediction into a candidate and the engine accepts, suppresses
or rejects it. Every stage has good reasons attached -- Phase 11's
`SuppressionReason` is thorough.

But an instrument with no prediction never becomes a candidate, so it
never reaches the engine and never acquires a reason. It is not
suppressed; it is ABSENT. The dashboard renders absence and decision
identically, and absence is most of the universe.

This module inverts the traversal: it starts from the INSTRUMENT and
walks forward, so every instrument gets a verdict whether or not the
pipeline ever touched it.

THE FUNNEL, AND WHY ORDER MATTERS
-------------------------------------
    universe
      -> has observations              NO_DATA
      -> has enough history            INSUFFICIENT_HISTORY
      -> has features                  MISSING_FEATURES
      -> horizon exists and is open    UNSUPPORTED_HORIZON / PROTECTED
      -> a model exists                NO_MODEL_AVAILABLE
      -> the model is qualified        MODEL_NOT_QUALIFIED
      -> a prediction exists           NO_MODEL_AVAILABLE
      -> it is fresh                   STALE_DATA
      -> it clears the thresholds      LOW_CONFIDENCE / BELOW_THRESHOLD
      -> SIGNAL

The FIRST blocker is reported. An instrument with neither data nor a
model is NO_DATA, not NO_MODEL_AVAILABLE: fixing the model would not
have helped it, and a reason that names the wrong repair is worse than
no reason.

WHAT IT REFUSES TO DO
-------------------------
It changes no threshold, promotes no model, weakens no gate and
manufactures no signal. Coverage is a DIAGNOSTIC: if the number is
small because the models are weak, the number stays small and the
report says the models are weak.

Measured on this corpus, the answer for essentially every instrument is
`NO_MODEL_AVAILABLE` or `MODEL_NOT_QUALIFIED`, because one model exists
for one horizon and it failed its gates. That is the truth the phase
was asked to surface.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple

from src.adaptive.horizon_intelligence import HorizonIntelligence, HorizonPolicy
from src.adaptive.horizon_inventory import (
    HorizonInventory, build_horizon_inventory, is_protected,
)
from src.adaptive.model_inventory import (
    ModelInventory, build_model_inventory,
)
from src.domain.signal_availability_models import (
    CoverageFunnelStage, HorizonStatus, InstrumentHorizonStatus,
    NoSignalReason, SUPPRESSION_TO_REASON, SignalAvailability,
    SignalAvailabilityClass, SignalCoverageReport, TargetEstimate,
)
from src.domain.signal_models import SignalDirection, SuppressionReason


@dataclass(frozen=True)
class AvailabilityPolicy:
    """
    Every threshold, and none of them invented here.

    `min_confidence` mirrors the value the signal engine already
    applies rather than introducing a second, different floor. It is
    Optional so that a caller reading the engine's own configuration can
    pass it in; None means "do not apply a second threshold", because
    the engine already applied its own.

    `min_observations` is the only number with a defensible source: it
    is the canonical `ModelEvaluation.MIN_EFFECTIVE_SAMPLE`, reused
    rather than re-chosen.
    """
    min_confidence: Optional[float] = None
    min_observations: int = 0
    stale_after_days: Optional[float] = None
    #: Whether an instrument with no observations at all is NO_DATA
    #: (True) or INSUFFICIENT_HISTORY. They are different repairs.
    absent_is_no_data: bool = True


@dataclass(frozen=True)
class InstrumentInputs:
    """
    What is known about one instrument, gathered before any judgement.

    A plain record so the funnel logic is pure: the database reads all
    happen in `collect_inputs`, and `assess` is a deterministic function
    of this object.
    """
    instrument_id: str
    sector_id: Optional[str] = None
    observation_count: int = 0
    feature_count: int = 0
    latest_observation_at: Optional[datetime] = None
    labels_by_horizon: Mapping[str, int] = field(default_factory=dict)
    predictions: Tuple[Mapping[str, object], ...] = ()
    signals: Tuple[Mapping[str, object], ...] = ()
    suppressions: Tuple[SuppressionReason, ...] = ()


class SignalAvailabilityEngine:
    """
    Produces one explained verdict per instrument. Deterministic.

    Holds the two inventories and a horizon-intelligence instance. Takes
    `as_of` as an argument everywhere, so a replay of the same corpus at
    the same moment yields the same verdicts.
    """

    def __init__(self, horizons: HorizonInventory, models: ModelInventory,
                 policy: Optional[AvailabilityPolicy] = None,
                 horizon_intelligence: Optional[HorizonIntelligence] = None):
        self.horizons = horizons
        self.models = models
        self.policy = policy or AvailabilityPolicy()
        self.horizon_intelligence = horizon_intelligence or HorizonIntelligence(
            HorizonPolicy())

    # ---------------- per horizon ----------------

    def horizon_status(self, inputs: InstrumentInputs, horizon_id: str, *,
                       as_of: datetime) -> InstrumentHorizonStatus:
        """
        What one horizon can do for one instrument, and why not.

        A protected horizon short-circuits before anything else is
        consulted, so no sealed value can influence the answer.
        """
        descriptor = self.horizons.get(horizon_id)
        if descriptor is None:
            return InstrumentHorizonStatus(
                instrument_id=inputs.instrument_id, horizon_id=horizon_id,
                status=HorizonStatus.UNSUPPORTED,
                reason=NoSignalReason.UNSUPPORTED_HORIZON,
                explanation=f"{horizon_id} is not a horizon this corpus knows")

        if descriptor.is_protected or is_protected(horizon_id):
            return InstrumentHorizonStatus(
                instrument_id=inputs.instrument_id, horizon_id=horizon_id,
                status=HorizonStatus.PROTECTED,
                reason=NoSignalReason.PROTECTED_HORIZON_UNAVAILABLE,
                explanation=(f"{horizon_id} is sealed: "
                             f"{descriptor.protected_note}"))

        if inputs.observation_count <= 0:
            return InstrumentHorizonStatus(
                instrument_id=inputs.instrument_id, horizon_id=horizon_id,
                status=HorizonStatus.INSUFFICIENT_DATA,
                reason=NoSignalReason.NO_DATA,
                explanation="no research observations exist for this instrument")

        models = self.models.for_horizon(horizon_id)
        if not models:
            return InstrumentHorizonStatus(
                instrument_id=inputs.instrument_id, horizon_id=horizon_id,
                status=HorizonStatus.NO_QUALIFIED_MODEL,
                reason=NoSignalReason.NO_MODEL_AVAILABLE,
                observations=inputs.observation_count,
                explanation=(f"no model has ever been trained for "
                             f"{horizon_id}"))

        qualified = [m for m in models if m.is_qualified]
        if not qualified:
            worst = models[0]
            return InstrumentHorizonStatus(
                instrument_id=inputs.instrument_id, horizon_id=horizon_id,
                status=HorizonStatus.NO_QUALIFIED_MODEL,
                reason=NoSignalReason.MODEL_NOT_QUALIFIED,
                model_qualified_id=worst.model_qualified_id,
                model_status=worst.status,
                observations=inputs.observation_count,
                explanation=(
                    f"{worst.model_qualified_id} exists for {horizon_id} but "
                    f"failed: {', '.join(worst.gate_failures) or 'unknown gate'}"))

        model = qualified[0]
        prediction = self._prediction_for(inputs, horizon_id)
        if prediction is None:
            return InstrumentHorizonStatus(
                instrument_id=inputs.instrument_id, horizon_id=horizon_id,
                status=HorizonStatus.UNAVAILABLE,
                reason=NoSignalReason.NO_MODEL_AVAILABLE,
                model_qualified_id=model.model_qualified_id,
                model_status=model.status,
                observations=inputs.observation_count,
                explanation=(f"{model.model_qualified_id} is qualified but has "
                             "produced no prediction for this instrument"))

        if self._is_stale(prediction, as_of):
            return InstrumentHorizonStatus(
                instrument_id=inputs.instrument_id, horizon_id=horizon_id,
                status=HorizonStatus.STALE, reason=NoSignalReason.STALE_DATA,
                model_qualified_id=model.model_qualified_id,
                model_status=model.status,
                observations=inputs.observation_count,
                explanation="the most recent prediction is older than the "
                            "configured freshness window")

        return InstrumentHorizonStatus(
            instrument_id=inputs.instrument_id, horizon_id=horizon_id,
            status=HorizonStatus.AVAILABLE,
            direction=self._direction_of(prediction),
            confidence=prediction.get("confidence"),
            expected_return=prediction.get("predicted_value"),
            model_qualified_id=model.model_qualified_id,
            model_status=model.status,
            observations=inputs.observation_count,
            explanation=f"{model.model_qualified_id} produced a usable "
                        f"prediction for {horizon_id}")

    # ---------------- per instrument ----------------

    def assess(self, inputs: InstrumentInputs, *, as_of: datetime
               ) -> SignalAvailability:
        """
        One instrument's verdict. The FIRST blocker is the reason.

        `PIPELINE_ERROR` is deliberately reachable only from an actual
        exception, never from a missing input: a broken pipeline must
        not look like a considered abstention.
        """
        lineage = [f"instrument:{inputs.instrument_id}"]
        statuses = tuple(
            self.horizon_status(inputs, descriptor.horizon_id, as_of=as_of)
            for descriptor in self.horizons)
        intelligence = self.horizon_intelligence.assess(
            inputs.instrument_id, statuses, as_of=as_of)

        # --- an active signal wins outright ---
        active = [s for s in inputs.signals
                  if str(s.get("status", "")).lower() == "active"]
        if active:
            signal = active[0]
            return self._verdict(
                inputs, as_of, SignalAvailabilityClass.SIGNAL,
                NoSignalReason.NO_SIGNAL_VALID,
                explanation=(f"active signal: {signal.get('direction')} at "
                             f"{signal.get('expected_return_horizon_days')}d"),
                statuses=statuses, intelligence=intelligence,
                direction=self._signal_direction(signal),
                signal_id=str(signal.get("signal_id") or "") or None,
                confidence=signal.get("confidence"),
                expected_return=signal.get("expected_return"),
                lineage=lineage + [f"signal:{signal.get('signal_id')}"])

        # --- the funnel, earliest blocker first ---
        blocker = self._first_blocker(inputs, statuses)
        reason, explanation, missing = blocker

        model_id = next((s.model_qualified_id for s in statuses
                         if s.model_qualified_id), None)
        model_status = next((s.model_status for s in statuses
                             if s.model_status), "")
        failures: Tuple[str, ...] = ()
        if model_id:
            records = [m for m in self.models if m.model_qualified_id == model_id]
            if records:
                failures = records[0].gate_failures
                lineage.append(f"model:{model_id}")

        return self._verdict(
            inputs, as_of, reason.availability_class, reason,
            explanation=explanation, statuses=statuses,
            intelligence=intelligence, missing_inputs=missing,
            model_qualified_id=model_id, model_status=model_status,
            qualification_failures=failures, lineage=lineage)

    # ---------------- funnel ----------------

    def _first_blocker(self, inputs: InstrumentInputs,
                       statuses: Sequence[InstrumentHorizonStatus]
                       ) -> Tuple[NoSignalReason, str, Tuple[str, ...]]:
        """
        The earliest thing that stopped a decision, and what is missing.

        Returns the reason whose repair would actually move this
        instrument forward.
        """
        if inputs.observation_count <= 0:
            return (NoSignalReason.NO_DATA,
                    "no research observations exist for this instrument, so "
                    "nothing could be computed",
                    ("research_observations",))

        if (self.policy.min_observations
                and inputs.observation_count < self.policy.min_observations):
            return (NoSignalReason.INSUFFICIENT_HISTORY,
                    f"{inputs.observation_count} observations, below the "
                    f"{self.policy.min_observations} needed for evidence",
                    ("observations",))

        if inputs.feature_count <= 0:
            return (NoSignalReason.MISSING_FEATURES,
                    "no features have been computed for this instrument",
                    ("research_features",))

        open_horizons = [s for s in statuses
                         if s.status is not HorizonStatus.PROTECTED]
        if not open_horizons:
            return (NoSignalReason.PROTECTED_HORIZON_UNAVAILABLE,
                    "every horizon available for this instrument is sealed",
                    ())

        # The signal engine's own suppression reasons, translated. They
        # are evidence, but NOT automatically the answer: the engine
        # judged a candidate that a model produced, and if that model
        # was never qualified then "stale prediction" is a symptom of a
        # deeper blocker. So suppressions are weighed against the
        # horizon-level blockers and the EARLIEST wins.
        suppression_reasons = [
            SUPPRESSION_TO_REASON.get(s, NoSignalReason.UNKNOWN)
            for s in inputs.suppressions]

        available = [s for s in open_horizons
                     if s.status is HorizonStatus.AVAILABLE]
        horizon_reasons = [s.reason for s in open_horizons
                           if s.reason is not None]

        if available and not suppression_reasons:
            return (NoSignalReason.NO_SIGNAL_VALID,
                    "a qualified model produced a usable prediction and the "
                    "evidence did not clear the signal threshold",
                    ())

        # Across horizons, the LEAST severe blocker is the one closest
        # to being fixed, so it names the repair that would first
        # produce a signal. Across suppressions, the earliest applies.
        candidates: List[NoSignalReason] = []
        if horizon_reasons and not available:
            candidates.append(max(horizon_reasons, key=lambda r: r.order))
        if suppression_reasons:
            candidates.append(min(suppression_reasons, key=lambda r: r.order))
        if not candidates:
            return (NoSignalReason.UNKNOWN,
                    "no horizon reported a usable state and none gave a reason",
                    ())

        best = min(candidates, key=lambda r: r.order)
        if best in suppression_reasons and best not in horizon_reasons:
            return (best,
                    "the signal engine suppressed this candidate: "
                    + ", ".join(sorted(s.value for s in inputs.suppressions)),
                    ())
        explanation = next(
            (s.explanation for s in open_horizons if s.reason is best
             and s.explanation), best.value)
        return best, explanation, ()

    # ---------------- helpers ----------------

    def _verdict(self, inputs, as_of, availability, reason, *, explanation,
                 statuses, intelligence, direction=SignalDirection.NO_SIGNAL,
                 signal_id=None, confidence=None, expected_return=None,
                 missing_inputs=(), model_qualified_id=None, model_status="",
                 qualification_failures=(), target=None,
                 lineage=()) -> SignalAvailability:
        return SignalAvailability(
            instrument_id=inputs.instrument_id, as_of=as_of,
            availability=availability, reason=reason, explanation=explanation,
            direction=direction, signal_id=signal_id, confidence=confidence,
            expected_return=expected_return,
            preferred_horizon=intelligence.preferred_horizon,
            horizon_statuses=statuses, horizon_intelligence=intelligence,
            target=target, missing_inputs=tuple(missing_inputs),
            model_qualified_id=model_qualified_id, model_status=model_status,
            qualification_failures=tuple(qualification_failures),
            data_as_of=inputs.latest_observation_at,
            sector_id=inputs.sector_id, lineage=tuple(lineage))

    def _prediction_for(self, inputs: InstrumentInputs, horizon_id: str
                        ) -> Optional[Mapping[str, object]]:
        for prediction in inputs.predictions:
            if str(prediction.get("horizon_id") or "") == horizon_id:
                return prediction
        return None

    def _is_stale(self, prediction: Mapping[str, object],
                  as_of: datetime) -> bool:
        if self.policy.stale_after_days is None:
            return False
        predicted_at = prediction.get("predicted_at")
        if not isinstance(predicted_at, datetime):
            return False
        return (as_of - predicted_at) > timedelta(
            days=self.policy.stale_after_days)

    @staticmethod
    def _direction_of(prediction: Mapping[str, object]) -> SignalDirection:
        value = prediction.get("predicted_value")
        if value is None:
            return SignalDirection.NO_SIGNAL
        if value > 0:
            return SignalDirection.LONG
        if value < 0:
            return SignalDirection.SHORT
        return SignalDirection.NEUTRAL

    @staticmethod
    def _signal_direction(signal: Mapping[str, object]) -> SignalDirection:
        try:
            return SignalDirection(str(signal.get("direction", "")).lower())
        except ValueError:
            return SignalDirection.NO_SIGNAL


# ======================================================================
# Coverage
# ======================================================================


def build_coverage(verdicts: Sequence[SignalAvailability], *,
                   as_of: datetime,
                   horizons: Optional[HorizonInventory] = None
                   ) -> SignalCoverageReport:
    """
    Counts that reconcile to the universe, by construction.

    Every instrument contributes exactly one class and exactly one
    reason, so `reconciles` is True unless something genuinely went
    wrong -- which is the only reason to report it at all.
    """
    by_reason: Dict[str, int] = {}
    by_class: Dict[str, int] = {}
    by_sector: Dict[str, Dict[str, int]] = {}
    by_horizon: Dict[str, Dict[str, int]] = {}

    for verdict in verdicts:
        by_reason[verdict.reason.value] = by_reason.get(
            verdict.reason.value, 0) + 1
        by_class[verdict.availability.value] = by_class.get(
            verdict.availability.value, 0) + 1
        sector = verdict.sector_id or "unknown"
        by_sector.setdefault(sector, {})
        by_sector[sector][verdict.availability.value] = by_sector[sector].get(
            verdict.availability.value, 0) + 1
        for status in verdict.horizon_statuses:
            bucket = by_horizon.setdefault(status.horizon_id, {})
            bucket[status.status.value] = bucket.get(status.status.value, 0) + 1

    funnel = _funnel(verdicts)
    return SignalCoverageReport(
        as_of=as_of, universe_size=len(verdicts),
        by_reason=dict(sorted(by_reason.items())),
        by_class=dict(sorted(by_class.items())),
        by_horizon={k: dict(sorted(v.items()))
                    for k, v in sorted(by_horizon.items())},
        by_sector={k: dict(sorted(v.items()))
                   for k, v in sorted(by_sector.items())},
        funnel=funnel)


def _funnel(verdicts: Sequence[SignalAvailability]
            ) -> Tuple[CoverageFunnelStage, ...]:
    """
    The real funnel, from the verdicts themselves.

    Each stage subtracts the instruments blocked AT that stage, so the
    remaining count is what survived to the next question. Numbers come
    from the data; none is written down anywhere.
    """
    total = len(verdicts)
    stages: List[CoverageFunnelStage] = [
        CoverageFunnelStage("universe", total, 0, None,
                            "instruments considered")]
    ladder = [
        (NoSignalReason.NO_DATA, "has research observations"),
        (NoSignalReason.INSUFFICIENT_HISTORY, "has enough history"),
        (NoSignalReason.MISSING_FEATURES, "has computed features"),
        (NoSignalReason.PROTECTED_HORIZON_UNAVAILABLE,
         "has at least one unsealed horizon"),
        (NoSignalReason.UNSUPPORTED_HORIZON, "has a supported horizon"),
        (NoSignalReason.NO_MODEL_AVAILABLE, "has a model for some horizon"),
        (NoSignalReason.MODEL_NOT_QUALIFIED, "that model is qualified"),
        (NoSignalReason.STALE_DATA, "its prediction is fresh"),
        (NoSignalReason.HORIZON_DISAGREEMENT, "its horizons do not conflict"),
        (NoSignalReason.HIGH_UNCERTAINTY, "uncertainty is acceptable"),
        (NoSignalReason.QUALITY_GATE_FAILED, "quality gates pass"),
        (NoSignalReason.LOW_CONFIDENCE, "confidence is adequate"),
        (NoSignalReason.BELOW_SIGNAL_THRESHOLD, "edge clears the threshold"),
        (NoSignalReason.COST_EXCEEDS_EDGE, "edge survives costs"),
    ]
    remaining = total
    for reason, label in ladder:
        blocked = sum(1 for v in verdicts if v.reason is reason)
        remaining -= blocked
        stages.append(CoverageFunnelStage(
            name=label, remaining=remaining, excluded=blocked, reason=reason))
    signals = sum(1 for v in verdicts
                  if v.availability is SignalAvailabilityClass.SIGNAL)
    stages.append(CoverageFunnelStage(
        "actual signals", signals, max(0, remaining - signals), None,
        "instruments with an active signal"))
    return tuple(stages)


# ======================================================================
# Reading the corpus
# ======================================================================


def collect_inputs(conn: sqlite3.Connection, instrument_ids: Sequence[str], *,
                   as_of: datetime) -> Dict[str, InstrumentInputs]:
    """
    Gather everything the funnel needs, bounded by `as_of`.

    One pass per table rather than per instrument: 389 instruments times
    six queries is 2,334 round trips, and the whole point of the
    diagnostic is that it can be run often.
    """
    wanted = {str(i) for i in instrument_ids}
    cutoff = as_of.isoformat()

    observations: Dict[str, int] = {}
    latest: Dict[str, datetime] = {}
    sectors: Dict[str, str] = {}
    observation_ids: Dict[str, Set[str]] = {}
    try:
        for instrument_id, sector_id, observation_id, information_time in \
                conn.execute(
                    "SELECT instrument_id, sector_id, observation_id, "
                    "information_time FROM research_observations "
                    "WHERE information_time IS NULL OR information_time <= ?",
                    (cutoff,)).fetchall():
            key = str(instrument_id)
            if key not in wanted:
                continue
            observations[key] = observations.get(key, 0) + 1
            observation_ids.setdefault(key, set()).add(str(observation_id))
            if sector_id:
                sectors[key] = str(sector_id)
            if information_time:
                try:
                    moment = datetime.fromisoformat(str(information_time))
                except (TypeError, ValueError):
                    moment = None
                if moment and (key not in latest or moment > latest[key]):
                    latest[key] = moment
    except sqlite3.DatabaseError:
        pass

    features: Dict[str, int] = {}
    all_observations = {o for ids in observation_ids.values() for o in ids}
    if all_observations:
        try:
            rows = conn.execute(
                "SELECT observation_id, COUNT(*) FROM research_features "
                "WHERE as_of IS NULL OR as_of <= ? GROUP BY observation_id",
                (cutoff,)).fetchall()
        except sqlite3.DatabaseError:
            rows = []
        per_observation = {str(o): int(n) for o, n in rows}
        for instrument_id, ids in observation_ids.items():
            features[instrument_id] = sum(
                per_observation.get(o, 0) for o in ids)

    signals: Dict[str, List[Dict[str, object]]] = {}
    try:
        for row in conn.execute(
                "SELECT signal_id, instrument_id, direction, status, "
                "confidence, expected_return, expected_return_horizon_days "
                "FROM signals").fetchall():
            key = str(row[1])
            if key not in wanted:
                continue
            signals.setdefault(key, []).append({
                "signal_id": row[0], "direction": row[2], "status": row[3],
                "confidence": row[4], "expected_return": row[5],
                "expected_return_horizon_days": row[6]})
    except sqlite3.DatabaseError:
        pass

    suppressions: Dict[str, Set[SuppressionReason]] = {}
    try:
        for signal_id, reason in conn.execute(
                "SELECT signal_id, reason FROM signal_suppressions").fetchall():
            for instrument_id, rows in signals.items():
                if any(str(r["signal_id"]) == str(signal_id) for r in rows):
                    try:
                        suppressions.setdefault(instrument_id, set()).add(
                            SuppressionReason(str(reason)))
                    except ValueError:
                        pass
    except sqlite3.DatabaseError:
        pass

    predictions: Dict[str, List[Dict[str, object]]] = {}
    try:
        for row in conn.execute(
                "SELECT p.prediction_id, p.observation_id, p.predicted_value, "
                "p.confidence, p.predicted_at, t.label_name "
                "FROM predictions p LEFT JOIN trained_models t "
                "ON t.trained_model_id = p.trained_model_id "
                "WHERE p.predicted_at IS NULL OR p.predicted_at <= ?",
                (cutoff,)).fetchall():
            observation = str(row[1])
            owner = next((i for i, ids in observation_ids.items()
                          if observation in ids), None)
            if owner is None:
                continue
            moment = None
            if row[4]:
                try:
                    moment = datetime.fromisoformat(str(row[4]))
                except (TypeError, ValueError):
                    moment = None
            predictions.setdefault(owner, []).append({
                "prediction_id": row[0], "predicted_value": row[2],
                "confidence": row[3], "predicted_at": moment,
                "horizon_id": str(row[5] or "").split(".")[0]})
    except sqlite3.DatabaseError:
        pass

    labels_by_horizon: Dict[str, Dict[str, int]] = {}
    if all_observations:
        try:
            # COUNT and names only. The value column is never named in
            # any query here, for any horizon, sealed or not.
            for observation_id, window_name, count in conn.execute(
                    "SELECT observation_id, window_name, COUNT(*) "
                    "FROM research_labels GROUP BY observation_id, window_name"
            ).fetchall():
                owner = next((i for i, ids in observation_ids.items()
                              if str(observation_id) in ids), None)
                if owner is None:
                    continue
                bucket = labels_by_horizon.setdefault(owner, {})
                window = str(window_name)
                bucket[window] = bucket.get(window, 0) + int(count)
        except sqlite3.DatabaseError:
            pass

    return {
        instrument_id: InstrumentInputs(
            instrument_id=instrument_id,
            sector_id=sectors.get(instrument_id),
            observation_count=observations.get(instrument_id, 0),
            feature_count=features.get(instrument_id, 0),
            latest_observation_at=latest.get(instrument_id),
            labels_by_horizon=labels_by_horizon.get(instrument_id, {}),
            predictions=tuple(predictions.get(instrument_id, ())),
            signals=tuple(signals.get(instrument_id, ())),
            suppressions=tuple(sorted(
                suppressions.get(instrument_id, set()),
                key=lambda r: r.value)))
        for instrument_id in sorted(wanted)}


def build_engine(conn: sqlite3.Connection, *, as_of: datetime,
                 policy: Optional[AvailabilityPolicy] = None
                 ) -> SignalAvailabilityEngine:
    """Both inventories plus the engine, from one read-only connection."""
    models = build_model_inventory(conn, as_of=as_of)
    horizons = build_horizon_inventory(
        conn, as_of=as_of, qualified_model_ids=models.qualified_ids)
    return SignalAvailabilityEngine(horizons, models, policy)
