"""
src/domain/signal_availability_models.py
--------------------------------------------------------
Why there is no signal (Phase 25.9I-F.5).

THE DISTINCTION THIS MODULE EXISTS FOR
------------------------------------------
    "I looked carefully and found no sufficient edge."
                    versus
    "I cannot make a scientific decision, because the model, the data
     or the horizon is missing."

Those are completely different states and the dashboard currently
renders both as the same blank. The first is a DECISION and belongs in
a coverage statistic as a considered abstention. The second is a
CAPABILITY GAP and belongs on a to-do list.

    NO SIGNAL IS A DECISION ONLY WHEN THE SYSTEM HAD ENOUGH
    INFORMATION TO MAKE THE DECISION.

    ABSENCE OF A MODEL IS NOT EVIDENCE OF ABSENCE OF EDGE.

WHY A NEW TAXONOMY WHEN `SuppressionReason` EXISTS
------------------------------------------------------
`SuppressionReason` (Phase 11) is reused wherever it applies and is
mapped onto this taxonomy rather than duplicated. But it answers a
narrower question: why did a CANDIDATE fail to become a signal. An
instrument with no model never produces a candidate, so it never
reaches suppression and never acquires a reason at all.

Audited 2026-09-30: the signal engine is candidate-driven, so an
instrument the pipeline never reached is invisible rather than
explained. That is the gap — and it is most of the universe.

THE ORDERING RULE
---------------------
`NoSignalReason` is evaluated in a FIXED order, earliest blocker first,
so a reason is always the FIRST thing that stopped the decision rather
than whichever check happened to run last. An instrument with no data
and no model is `NO_DATA`, not `NO_MODEL_AVAILABLE`: fixing the model
would not have helped.

WHAT THIS MODULE WILL NOT INVENT
------------------------------------
No expected return, no confidence, no uncertainty, no target and no
preferred horizon. Every one of those is Optional and defaults to None,
and a None is reported as UNKNOWN rather than filled with a plausible
number. Measured on this repository: exactly one model exists, it
covers one horizon, it loses to its own baselines and it was never
promoted — so almost every honest answer here is "we cannot say".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Tuple

from src.domain.adaptive_models import (
    _require_fraction, _require_non_negative, require_utc,
)
from src.domain.signal_models import (
    AgreementState, SignalDirection, SuppressionReason,
)

# ======================================================================
# The taxonomy
# ======================================================================


class SignalAvailabilityClass(str, Enum):
    """
    The four states a dashboard cell may be in, and they are not alike.

    SIGNAL and VALID_NO_SIGNAL are both DECISIONS -- the system had what
    it needed and formed a view, or formed the view that there is
    nothing worth doing. CAPABILITY_GAP and PROTECTED are NOT decisions,
    and counting them as "no opportunity found" would be the single most
    misleading thing this layer could do.
    """
    SIGNAL = "signal"
    VALID_NO_SIGNAL = "valid_no_signal"
    CAPABILITY_GAP = "capability_gap"
    PROTECTED = "protected"
    ERROR = "error"

    @property
    def is_decision(self) -> bool:
        """Whether the system actually decided something."""
        return self in (SignalAvailabilityClass.SIGNAL,
                        SignalAvailabilityClass.VALID_NO_SIGNAL)


class NoSignalReason(str, Enum):
    """
    Why no signal exists. Ordered by how early the blocker occurs.

    `NO_SIGNAL_VALID` is the ONLY member that means the system decided.
    Everything else means it could not.
    """
    # --- the system decided ---
    NO_SIGNAL_VALID = "no_signal_valid"
    BELOW_SIGNAL_THRESHOLD = "below_signal_threshold"
    LOW_CONFIDENCE = "low_confidence"
    HIGH_UNCERTAINTY = "high_uncertainty"
    COST_EXCEEDS_EDGE = "cost_exceeds_edge"
    # --- the system could not decide ---
    NO_DATA = "no_data"
    INSUFFICIENT_HISTORY = "insufficient_history"
    MISSING_FEATURES = "missing_features"
    STALE_DATA = "stale_data"
    UNSUPPORTED_HORIZON = "unsupported_horizon"
    NO_MODEL_AVAILABLE = "no_model_available"
    MODEL_NOT_QUALIFIED = "model_not_qualified"
    QUALITY_GATE_FAILED = "quality_gate_failed"
    CALIBRATION_FAILED = "calibration_failed"
    REGIME_UNSUPPORTED = "regime_unsupported"
    HORIZON_DISAGREEMENT = "horizon_disagreement"
    # --- sealed ---
    PROTECTED_HORIZON_UNAVAILABLE = "protected_horizon_unavailable"
    # --- broken ---
    PIPELINE_ERROR = "pipeline_error"
    UNKNOWN = "unknown"

    @property
    def availability_class(self) -> SignalAvailabilityClass:
        return _REASON_CLASS.get(self, SignalAvailabilityClass.CAPABILITY_GAP)

    @property
    def is_decision(self) -> bool:
        """
        Whether this reason represents a considered abstention.

        Only true for the first five. A missing model is not a finding
        about the market.
        """
        return self.availability_class is SignalAvailabilityClass.VALID_NO_SIGNAL

    @property
    def order(self) -> int:
        """Position in the evaluation order; lower blocks earlier."""
        return _REASON_ORDER.get(self, 999)


_REASON_CLASS: Dict[NoSignalReason, SignalAvailabilityClass] = {
    NoSignalReason.NO_SIGNAL_VALID: SignalAvailabilityClass.VALID_NO_SIGNAL,
    NoSignalReason.BELOW_SIGNAL_THRESHOLD: SignalAvailabilityClass.VALID_NO_SIGNAL,
    NoSignalReason.LOW_CONFIDENCE: SignalAvailabilityClass.VALID_NO_SIGNAL,
    NoSignalReason.HIGH_UNCERTAINTY: SignalAvailabilityClass.VALID_NO_SIGNAL,
    NoSignalReason.COST_EXCEEDS_EDGE: SignalAvailabilityClass.VALID_NO_SIGNAL,
    NoSignalReason.PROTECTED_HORIZON_UNAVAILABLE: SignalAvailabilityClass.PROTECTED,
    NoSignalReason.PIPELINE_ERROR: SignalAvailabilityClass.ERROR,
    NoSignalReason.UNKNOWN: SignalAvailabilityClass.ERROR,
}

#: Evaluation order. Data before features, features before horizons,
#: horizons before models, models before thresholds. A reason is always
#: the FIRST blocker, so it names something whose repair would help.
_REASON_ORDER: Dict[NoSignalReason, int] = {
    NoSignalReason.PIPELINE_ERROR: 0,
    NoSignalReason.PROTECTED_HORIZON_UNAVAILABLE: 1,
    NoSignalReason.NO_DATA: 2,
    NoSignalReason.INSUFFICIENT_HISTORY: 3,
    NoSignalReason.MISSING_FEATURES: 4,
    NoSignalReason.UNSUPPORTED_HORIZON: 5,
    NoSignalReason.NO_MODEL_AVAILABLE: 6,
    NoSignalReason.MODEL_NOT_QUALIFIED: 7,
    # Staleness ranks AFTER the model checks, and the order is load-
    # bearing: a prediction can only be stale if a model made one, so
    # an unqualified model is the earlier and more fundamental blocker.
    # Ranking staleness first made four instruments report "stale data"
    # when the real repair was "no model has ever been qualified".
    NoSignalReason.STALE_DATA: 8,
    NoSignalReason.QUALITY_GATE_FAILED: 9,
    NoSignalReason.CALIBRATION_FAILED: 10,
    NoSignalReason.REGIME_UNSUPPORTED: 11,
    NoSignalReason.HORIZON_DISAGREEMENT: 12,
    NoSignalReason.HIGH_UNCERTAINTY: 13,
    NoSignalReason.LOW_CONFIDENCE: 14,
    NoSignalReason.BELOW_SIGNAL_THRESHOLD: 15,
    NoSignalReason.COST_EXCEEDS_EDGE: 16,
    NoSignalReason.NO_SIGNAL_VALID: 17,
    NoSignalReason.UNKNOWN: 999,
}

#: Phase 11's `SuppressionReason` mapped onto this taxonomy. Reused
#: rather than duplicated: where the signal engine already has a reason,
#: it is the authority and this is only a translation.
SUPPRESSION_TO_REASON: Dict[SuppressionReason, NoSignalReason] = {
    SuppressionReason.STALE_PREDICTION: NoSignalReason.STALE_DATA,
    SuppressionReason.LOW_CONFIDENCE: NoSignalReason.LOW_CONFIDENCE,
    SuppressionReason.POOR_DATA_QUALITY: NoSignalReason.QUALITY_GATE_FAILED,
    SuppressionReason.MODEL_CONFLICT: NoSignalReason.HORIZON_DISAGREEMENT,
    SuppressionReason.UNSUPPORTED_INSTRUMENT: NoSignalReason.UNSUPPORTED_HORIZON,
    SuppressionReason.MODEL_NOT_ACTIVE: NoSignalReason.MODEL_NOT_QUALIFIED,
    SuppressionReason.MODEL_ABSTAINED: NoSignalReason.NO_SIGNAL_VALID,
    SuppressionReason.INSUFFICIENT_FEATURES: NoSignalReason.MISSING_FEATURES,
    SuppressionReason.BELOW_STRENGTH_THRESHOLD:
        NoSignalReason.BELOW_SIGNAL_THRESHOLD,
    SuppressionReason.EXPIRED_EVENT: NoSignalReason.STALE_DATA,
    SuppressionReason.SMALL_SAMPLE_EVIDENCE: NoSignalReason.QUALITY_GATE_FAILED,
}


# ======================================================================
# Horizons
# ======================================================================


class HorizonStatus(str, Enum):
    """
    What one horizon can do for one instrument, right now.

    Deliberately NOT collapsible to a company-level boolean: the same
    instrument can be AVAILABLE at d1, NO_QUALIFIED_MODEL at d10 and
    PROTECTED at d20, and a single flag would have to lie about two of
    the three.
    """
    AVAILABLE = "available"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    PROTECTED = "protected"
    INSUFFICIENT_DATA = "insufficient_data"
    NO_QUALIFIED_MODEL = "no_qualified_model"
    STALE = "stale"
    UNSUPPORTED = "unsupported"

    @property
    def is_usable_for_decisions(self) -> bool:
        return self is HorizonStatus.AVAILABLE


@dataclass(frozen=True)
class HorizonDescriptor:
    """
    One canonical horizon, described from repository evidence only.

    `is_protected` is not decoration. A protected horizon may expose
    readiness metadata -- how many labels exist, whether a model exists
    -- and may NEVER expose an outcome statistic. `protected_note`
    carries the reason so a reader is not left guessing why a horizon
    with plenty of history produces nothing.
    """
    horizon_id: str
    label_name: str = ""
    label_count: int = 0
    raw_label_count: int = 0
    is_intraday: bool = False
    is_protected: bool = False
    protected_note: str = ""
    model_count: int = 0
    qualified_model_count: int = 0
    trading_days: Optional[int] = None
    minutes: Optional[int] = None
    limitations: Tuple[str, ...] = ()

    def __post_init__(self):
        if not str(self.horizon_id).strip():
            raise ValueError("horizon_id is required")
        for name in ("label_count", "raw_label_count", "model_count",
                     "qualified_model_count"):
            if int(getattr(self, name)) < 0:
                raise ValueError(f"{name} must be >= 0")
        if self.qualified_model_count > self.model_count:
            raise ValueError(
                "a horizon cannot have more qualified models than models")
        if self.is_protected and not str(self.protected_note).strip():
            raise ValueError(
                "a protected horizon must state why it is sealed")

    @property
    def has_model(self) -> bool:
        return self.model_count > 0

    @property
    def has_qualified_model(self) -> bool:
        return self.qualified_model_count > 0

    @property
    def can_produce_signal(self) -> bool:
        """A protected horizon can never produce a signal, model or not."""
        return self.has_qualified_model and not self.is_protected

    def describe(self) -> Dict[str, object]:
        return {
            "horizon_id": self.horizon_id,
            "label_name": self.label_name,
            "label_count": self.label_count,
            "is_intraday": self.is_intraday,
            "is_protected": self.is_protected,
            "protected_note": self.protected_note,
            "models": self.model_count,
            "qualified_models": self.qualified_model_count,
            "can_produce_signal": self.can_produce_signal,
            "limitations": list(self.limitations),
        }


@dataclass(frozen=True)
class InstrumentHorizonStatus:
    """One instrument at one horizon, with the reason for its status."""
    instrument_id: str
    horizon_id: str
    status: HorizonStatus = HorizonStatus.UNAVAILABLE
    reason: Optional[NoSignalReason] = None
    direction: SignalDirection = SignalDirection.NO_SIGNAL
    confidence: Optional[float] = None
    uncertainty: Optional[str] = None
    expected_return: Optional[float] = None
    model_qualified_id: Optional[str] = None
    model_status: str = ""
    observations: int = 0
    explanation: str = ""

    def __post_init__(self):
        _require_fraction(self.confidence, "confidence")
        if int(self.observations) < 0:
            raise ValueError("observations must be >= 0")
        if (self.status is HorizonStatus.PROTECTED
                and self.expected_return is not None):
            # The structural guard: a protected horizon cannot carry a
            # number derived from its sealed outcome.
            raise ValueError(
                "a PROTECTED horizon may not carry an expected return")

    @property
    def is_usable(self) -> bool:
        return self.status.is_usable_for_decisions

    def describe(self) -> Dict[str, object]:
        return {
            "horizon_id": self.horizon_id,
            "status": self.status.value,
            "reason": self.reason.value if self.reason else None,
            "direction": self.direction.value,
            "confidence": self.confidence,
            "uncertainty": self.uncertainty,
            "expected_return": self.expected_return,
            "model": self.model_qualified_id,
            "model_status": self.model_status,
            "observations": self.observations,
            "explanation": self.explanation,
        }


# ======================================================================
# Horizon intelligence
# ======================================================================


class HorizonAgreement(str, Enum):
    """How the usable horizons relate to one another."""
    AGREEMENT = "agreement"
    PARTIAL_AGREEMENT = "partial_agreement"
    DISAGREEMENT = "disagreement"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


@dataclass(frozen=True)
class HorizonIntelligenceResult:
    """
    Which horizon, if any, is worth deciding on.

    `preferred_horizon = None` is a first-class, correct answer. An
    available horizon is NOT a preferred horizon: choosing "5d because
    5d exists" is how a system acquires a holding period nobody can
    defend.
    """
    instrument_id: str
    as_of: datetime
    preferred_horizon: Optional[str] = None
    alternative_horizons: Tuple[str, ...] = ()
    unsupported_horizons: Tuple[str, ...] = ()
    protected_horizons: Tuple[str, ...] = ()
    agreement: HorizonAgreement = HorizonAgreement.INSUFFICIENT_EVIDENCE
    confidence: Optional[float] = None
    completeness: float = 0.0
    reasons: Tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "as_of", require_utc(self.as_of, "as_of"))
        _require_fraction(self.confidence, "confidence")
        _require_fraction(self.completeness, "completeness", allow_none=False)
        if self.preferred_horizon and self.preferred_horizon in self.protected_horizons:
            raise ValueError(
                "a protected horizon can never be the preferred horizon")
        if (self.preferred_horizon
                and self.agreement is HorizonAgreement.DISAGREEMENT):
            raise ValueError(
                "horizons that disagree cannot yield a preferred horizon; "
                "report the disagreement instead")

    @property
    def has_preference(self) -> bool:
        return self.preferred_horizon is not None

    def describe(self) -> Dict[str, object]:
        return {
            "instrument_id": self.instrument_id,
            "preferred_horizon": self.preferred_horizon,
            "alternative_horizons": list(self.alternative_horizons),
            "unsupported_horizons": list(self.unsupported_horizons),
            "protected_horizons": list(self.protected_horizons),
            "agreement": self.agreement.value,
            "confidence": self.confidence,
            "completeness": self.completeness,
            "reasons": list(self.reasons),
        }


# ======================================================================
# Targets
# ======================================================================


class TargetStatus(str, Enum):
    """
    Whether a defensible target can be produced.

    `UNAVAILABLE` is the expected answer today and is not a failure: it
    means no qualified model produces a calibrated expected return, so
    any number would be decoration.
    """
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    PROTECTED = "protected"


@dataclass(frozen=True)
class TargetEstimate:
    """
    A target zone, or an explicit refusal to produce one.

    `target_low`/`target_high` are absent unless `status` is AVAILABLE,
    and AVAILABLE requires a model-sourced expected return. There is no
    `current_price × multiplier` path: a multiplier is a made-up number
    wearing a decimal point.
    """
    instrument_id: str
    as_of: datetime
    status: TargetStatus = TargetStatus.UNAVAILABLE
    horizon_id: Optional[str] = None
    reference_price: Optional[float] = None
    expected_return: Optional[float] = None
    uncertainty: Optional[float] = None
    target_low: Optional[float] = None
    target_high: Optional[float] = None
    confidence: Optional[float] = None
    model_qualified_id: Optional[str] = None
    method: str = ""
    completeness: float = 0.0
    reasons: Tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "as_of", require_utc(self.as_of, "as_of"))
        for name in ("reference_price", "target_low", "target_high",
                     "uncertainty"):
            _require_non_negative(getattr(self, name), name)
        _require_fraction(self.confidence, "confidence")
        _require_fraction(self.completeness, "completeness", allow_none=False)

        if self.status is TargetStatus.AVAILABLE:
            if self.expected_return is None:
                raise ValueError(
                    "an AVAILABLE target requires a model-sourced expected "
                    "return; there is no other way to produce one")
            if not self.model_qualified_id:
                raise ValueError(
                    "an AVAILABLE target must name the model it came from")
            if self.target_low is None or self.target_high is None:
                raise ValueError("an AVAILABLE target requires a zone")
            if self.target_low > self.target_high:
                raise ValueError("target_low must not exceed target_high")
        else:
            for name in ("target_low", "target_high"):
                if getattr(self, name) is not None:
                    raise ValueError(
                        f"{name} must be absent unless the target is AVAILABLE")

    @property
    def has_zone(self) -> bool:
        return self.target_low is not None and self.target_high is not None

    def describe(self) -> Dict[str, object]:
        return {
            "instrument_id": self.instrument_id,
            "status": self.status.value,
            "horizon_id": self.horizon_id,
            "reference_price": self.reference_price,
            "expected_return": self.expected_return,
            "uncertainty": self.uncertainty,
            "target_low": self.target_low,
            "target_high": self.target_high,
            "confidence": self.confidence,
            "model": self.model_qualified_id,
            "method": self.method,
            "completeness": self.completeness,
            "reasons": list(self.reasons),
        }


# ======================================================================
# The per-instrument verdict
# ======================================================================


@dataclass(frozen=True)
class SignalAvailability:
    """
    One instrument's answer, with the reason it is that answer.

    This is what replaces a blank dashboard cell. Every field that could
    be fabricated is Optional and defaults to None.
    """
    instrument_id: str
    as_of: datetime
    availability: SignalAvailabilityClass = SignalAvailabilityClass.CAPABILITY_GAP
    reason: NoSignalReason = NoSignalReason.UNKNOWN
    explanation: str = ""
    direction: SignalDirection = SignalDirection.NO_SIGNAL
    signal_id: Optional[str] = None
    confidence: Optional[float] = None
    uncertainty: Optional[str] = None
    expected_return: Optional[float] = None
    preferred_horizon: Optional[str] = None
    horizon_statuses: Tuple[InstrumentHorizonStatus, ...] = ()
    horizon_intelligence: Optional[HorizonIntelligenceResult] = None
    target: Optional[TargetEstimate] = None
    missing_inputs: Tuple[str, ...] = ()
    model_qualified_id: Optional[str] = None
    model_status: str = ""
    qualification_failures: Tuple[str, ...] = ()
    data_as_of: Optional[datetime] = None
    sector_id: Optional[str] = None
    lineage: Tuple[str, ...] = ()

    def __post_init__(self):
        if not str(self.instrument_id).strip():
            raise ValueError("instrument_id is required")
        object.__setattr__(self, "as_of", require_utc(self.as_of, "as_of"))
        object.__setattr__(self, "data_as_of",
                           require_utc(self.data_as_of, "data_as_of"))
        _require_fraction(self.confidence, "confidence")
        if not str(self.explanation).strip():
            # The whole point of the phase: no blank answers.
            raise ValueError(
                f"{self.instrument_id}: every availability verdict must carry "
                "a human-readable explanation")
        if self.availability is not self.reason.availability_class:
            raise ValueError(
                f"reason {self.reason.value} belongs to class "
                f"{self.reason.availability_class.value}, not "
                f"{self.availability.value}")
        if (self.availability is SignalAvailabilityClass.PROTECTED
                and self.expected_return is not None):
            raise ValueError(
                "a protected verdict may not carry an expected return")

    @property
    def is_decision(self) -> bool:
        """Whether the system actually decided, or merely could not."""
        return self.availability.is_decision

    @property
    def usable_horizons(self) -> Tuple[str, ...]:
        return tuple(h.horizon_id for h in self.horizon_statuses if h.is_usable)

    def describe(self) -> Dict[str, object]:
        return {
            "instrument_id": self.instrument_id,
            "as_of": self.as_of.isoformat(),
            "availability": self.availability.value,
            "is_decision": self.is_decision,
            "reason": self.reason.value,
            "explanation": self.explanation,
            "direction": self.direction.value,
            "signal_id": self.signal_id,
            "confidence": self.confidence,
            "uncertainty": self.uncertainty,
            "expected_return": self.expected_return,
            "preferred_horizon": self.preferred_horizon,
            "horizons": [h.describe() for h in self.horizon_statuses],
            "horizon_intelligence": (self.horizon_intelligence.describe()
                                     if self.horizon_intelligence else None),
            "target": self.target.describe() if self.target else None,
            "missing_inputs": list(self.missing_inputs),
            "model": self.model_qualified_id,
            "model_status": self.model_status,
            "qualification_failures": list(self.qualification_failures),
            "data_as_of": (self.data_as_of.isoformat()
                           if self.data_as_of else None),
            "sector_id": self.sector_id,
            "lineage": list(self.lineage),
        }


# ======================================================================
# Coverage
# ======================================================================


@dataclass(frozen=True)
class CoverageFunnelStage:
    """One step of the funnel, with what it excluded and why."""
    name: str
    remaining: int
    excluded: int = 0
    reason: Optional[NoSignalReason] = None
    note: str = ""


@dataclass(frozen=True)
class SignalCoverageReport:
    """
    Honest counts. They must reconcile to the universe exactly.

    `reconciles` is checked rather than assumed: a coverage report whose
    parts do not sum to its whole is worse than no report, because it
    invites the reader to trust a number that is wrong.
    """
    as_of: datetime
    universe_size: int
    by_reason: Dict[str, int] = field(default_factory=dict)
    by_class: Dict[str, int] = field(default_factory=dict)
    by_horizon: Dict[str, Dict[str, int]] = field(default_factory=dict)
    by_sector: Dict[str, Dict[str, int]] = field(default_factory=dict)
    funnel: Tuple[CoverageFunnelStage, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "as_of", require_utc(self.as_of, "as_of"))
        if int(self.universe_size) < 0:
            raise ValueError("universe_size must be >= 0")

    @property
    def total_by_reason(self) -> int:
        return sum(self.by_reason.values())

    @property
    def total_by_class(self) -> int:
        return sum(self.by_class.values())

    @property
    def reconciles(self) -> bool:
        return (self.total_by_reason == self.universe_size
                and self.total_by_class == self.universe_size)

    @property
    def decisions(self) -> int:
        """How many instruments the system actually decided about."""
        return (self.by_class.get(SignalAvailabilityClass.SIGNAL.value, 0)
                + self.by_class.get(
                    SignalAvailabilityClass.VALID_NO_SIGNAL.value, 0))

    def describe(self) -> Dict[str, object]:
        return {
            "as_of": self.as_of.isoformat(),
            "universe_size": self.universe_size,
            "by_class": dict(self.by_class),
            "by_reason": dict(self.by_reason),
            "by_horizon": {k: dict(v) for k, v in self.by_horizon.items()},
            "by_sector": {k: dict(v) for k, v in self.by_sector.items()},
            "funnel": [{"name": s.name, "remaining": s.remaining,
                        "excluded": s.excluded,
                        "reason": s.reason.value if s.reason else None,
                        "note": s.note} for s in self.funnel],
            "decisions": self.decisions,
            "reconciles": self.reconciles,
        }
