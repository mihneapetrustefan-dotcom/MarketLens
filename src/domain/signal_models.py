"""
src/domain/signal_models.py
--------------------------------
Signal domain models (Phase 10).

WHAT A SIGNAL IS, AND WHAT IT DELIBERATELY IS NOT
-----------------------------------------------------
A Signal is a structured, explainable, versioned STATEMENT about an
instrument — a direction, a strength, a confidence, and the evidence
behind them. It is not a position, not an order, not an allocation.

Nothing in this file carries a quantity, a position size, a stop, a
broker, or an account. That absence is the same structural commitment
Phase 9 made about predictions, extended one layer: Phase 11 (risk and
portfolio) must introduce its own types rather than quietly widening
these. A Signal that already knew "how much" would make the risk layer
optional, and an optional risk layer is one that eventually gets
skipped.

CANDIDATE AND SIGNAL ARE DIFFERENT THINGS (spec §20)
--------------------------------------------------------
A SignalCandidate is what a strategy proposes. A Signal is what
survived validation. Keeping them as separate types means a candidate
cannot be mistaken for a validated signal by an accident of typing —
and, just as importantly, a REJECTED candidate is still a first-class
object with a recorded reason, not a silent discard (spec §23).

CONFLICT IS A STATE, NOT AN ERROR (spec §10)
------------------------------------------------
When two models disagree, the system records DISAGREEMENT. It does not
pick a winner and move on. AgreementState is carried on the signal
itself so a downstream consumer sees the disagreement rather than
inheriting a confident-looking number that concealed it.

CONFIDENCE AND STRENGTH ARE DIFFERENT (spec §6, §7)
-------------------------------------------------------
Strength is how large the expected move is. Confidence is how much the
system trusts that estimate. A large expected move that the system
barely believes in, and a small move it is sure of, are different
situations and must not collapse into one number.

EVERYTHING IS VERSIONED (spec §16, §32)
-------------------------------------------
Strategy version, configuration version, model version, feature set
version, dataset version. A signal whose generating logic cannot be
reconstructed is not auditable, and an unauditable signal is not
usable as evidence for anything.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


def _require_utc(value: Optional[datetime], name: str) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware (got a naive datetime)")
    if value.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError(f"{name} must be in UTC (got offset {value.utcoffset()})")
    return value


# ============================================================
# Taxonomy (spec §4, §5)
# ============================================================

class SignalType(str, Enum):
    """
    What KIND of claim the signal makes.

    Only the types this project can currently generate evidence for are
    listed. Spec §4 warns against inventing strategies for the sake of
    a long enum: an unimplemented type is a promise the engine cannot
    keep, and a consumer filtering on it would silently get nothing.
    Adding a type later is a one-line change; removing one that
    downstream code already branches on is not.
    """
    DIRECTIONAL = "directional"          # from a model's return/direction prediction
    EVENT_DRIVEN = "event_driven"        # anchored on a Phase 5 canonical event
    VOLATILITY = "volatility"            # about expected dispersion, not direction
    RISK_WARNING = "risk_warning"        # flags elevated risk; never an entry signal


class SignalDirection(str, Enum):
    """
    NEUTRAL and NO_SIGNAL are NOT the same (spec §5).

    NEUTRAL is an active claim: the evidence points to no meaningful
    move. NO_SIGNAL is the absence of a claim: the system did not form
    a view. Collapsing them would let "we don't know" be read as "we
    predict nothing will happen", which is a much stronger statement.
    """
    LONG = "long"
    SHORT = "short"
    NEUTRAL = "neutral"
    NO_SIGNAL = "no_signal"


class SignalStatus(str, Enum):
    """Lifecycle state (spec §15). A signal is never deleted; it expires or is superseded."""
    PENDING = "pending"          # created, not yet validated
    ACTIVE = "active"            # validated and within its validity window
    EXPIRED = "expired"          # past valid_until
    SUPERSEDED = "superseded"    # a newer signal for the same identity replaced it
    REJECTED = "rejected"        # failed validation; kept with its reason
    SUPPRESSED = "suppressed"    # deliberately withheld; kept with its reason


class AgreementState(str, Enum):
    """
    How the contributing evidence relates (spec §10).

    INSUFFICIENT_EVIDENCE is distinct from CONFLICT: one source saying
    nothing is not the same as two sources saying opposite things, and
    the appropriate response differs.
    """
    AGREEMENT = "agreement"
    PARTIAL_AGREEMENT = "partial_agreement"
    CONFLICT = "conflict"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class SuppressionReason(str, Enum):
    """
    Why a candidate did not become a signal (spec §23).

    Enumerated rather than free text so suppressions are countable: "how
    often do we suppress for stale data?" must be a query, not a log
    grep. Free-text reasons go in the accompanying note.
    """
    STALE_PREDICTION = "stale_prediction"
    LOW_CONFIDENCE = "low_confidence"
    POOR_DATA_QUALITY = "poor_data_quality"
    MODEL_CONFLICT = "model_conflict"
    UNSUPPORTED_INSTRUMENT = "unsupported_instrument"
    MODEL_NOT_ACTIVE = "model_not_active"
    MODEL_ABSTAINED = "model_abstained"
    INSUFFICIENT_FEATURES = "insufficient_features"
    BELOW_STRENGTH_THRESHOLD = "below_strength_threshold"
    EXPIRED_EVENT = "expired_event"
    SMALL_SAMPLE_EVIDENCE = "small_sample_evidence"


# ============================================================
# Contributions and provenance (spec §9, §17)
# ============================================================

@dataclass
class ModelContribution:
    """
    One model's input to a signal (spec §9).

    Weight and reliability are stored SEPARATELY from the predicted
    value. Spec §9 explicitly forbids blind averaging; keeping the
    inputs to the aggregation visible is what makes the aggregation
    explainable after the fact rather than a number nobody can defend.
    """
    prediction_id: str
    trained_model_id: str
    model_qualified_id: str
    predicted_value: Optional[float] = None
    probability_up: Optional[float] = None
    confidence: Optional[float] = None
    #: How much this model counted toward the final signal, 0..1.
    weight: float = 1.0
    #: Evidence of past performance, if known. None means UNKNOWN —
    #: never silently treated as good.
    reliability: Optional[float] = None
    is_abstention: bool = False
    note: str = ""


@dataclass
class SignalProvenance:
    """
    The full chain from data to signal (spec §17).

    Every version reference needed to reconstruct why this signal
    exists. If any of these is unknown, the signal is still recorded —
    but the gap is visible, not papered over with a default.
    """
    observation_id: Optional[str] = None
    event_id: Optional[str] = None
    instrument_id: Optional[str] = None
    feature_set_version: Optional[str] = None
    dataset_version: Optional[str] = None
    strategy_id: Optional[str] = None
    strategy_version: Optional[str] = None
    configuration_version: Optional[str] = None
    #: The cutoff of the information this signal was built from. This
    #: is the anchor for point-in-time correctness: nothing dated after
    #: it may have influenced the signal.
    source_information_cutoff: Optional[datetime] = None
    inputs: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        _require_utc(self.source_information_cutoff, "source_information_cutoff")

    @property
    def is_complete(self) -> bool:
        """True when the chain can be walked back to its data. Used for reporting, not gating."""
        return all([self.strategy_id, self.strategy_version,
                    self.source_information_cutoff is not None])


@dataclass
class SignalExplanation:
    """
    Why this signal says what it says (spec §18).

    Structured, not a prose blob: `factors` holds named contributions
    so an explanation can be checked against the numbers, and
    `caveats` holds what weakens it. A explanation listing only
    supporting factors is advocacy, not explanation — caveats are a
    first-class field for that reason.
    """
    summary: str = ""
    factors: List[str] = field(default_factory=list)
    caveats: List[str] = field(default_factory=list)

    def add_factor(self, text: str) -> None:
        if text and text not in self.factors:
            self.factors.append(text)

    def add_caveat(self, text: str) -> None:
        if text and text not in self.caveats:
            self.caveats.append(text)


# ============================================================
# Context (spec §13, §14, §11)
# ============================================================

@dataclass
class SignalContext:
    """
    Market conditions surrounding the signal (spec §13, §14).

    All fields optional and defaulting to None, meaning UNKNOWN. A
    missing regime must never be silently read as "normal" — that
    would be the system inventing a fact it does not have.
    """
    market_regime: Optional[str] = None
    volatility_percentile: Optional[float] = None
    relative_volume: Optional[float] = None
    liquidity_note: Optional[str] = None
    event_type: Optional[str] = None
    event_corroboration_state: Optional[str] = None
    independent_source_count: Optional[int] = None
    data_quality_level: Optional[str] = None


# ============================================================
# Candidate and Signal (spec §3, §20)
# ============================================================

@dataclass
class SignalCandidate:
    """
    What a strategy proposes, BEFORE validation (spec §20).

    A candidate carries everything a signal would, minus the claim to
    have been validated. Keeping it a separate type means no code path
    can treat an unvalidated proposal as a signal by accident.
    """
    candidate_id: str
    instrument_id: str
    signal_type: SignalType
    direction: SignalDirection
    strategy_id: str
    strategy_version: str
    #: Raw, unbounded strength as the strategy computed it. Normalized
    #: into 0..1 only when the candidate becomes a Signal, so the
    #: original scale stays inspectable.
    raw_strength: Optional[float] = None
    confidence: Optional[float] = None
    expected_return: Optional[float] = None
    expected_return_horizon_days: Optional[float] = None
    probability_up: Optional[float] = None
    contributions: List[ModelContribution] = field(default_factory=list)
    context: SignalContext = field(default_factory=SignalContext)
    provenance: SignalProvenance = field(default_factory=SignalProvenance)
    explanation: SignalExplanation = field(default_factory=SignalExplanation)
    created_at: Optional[datetime] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        _require_utc(self.created_at, "created_at")

    @property
    def probability_down(self) -> Optional[float]:
        """Derived, never stored — two fields that must sum to 1 will eventually disagree."""
        return None if self.probability_up is None else round(1.0 - self.probability_up, 6)


@dataclass
class Signal:
    """
    A validated, versioned, explainable claim about an instrument
    (spec §3).

    Carries no quantity and no action. See the module docstring for
    why that absence is structural rather than an oversight.
    """
    signal_id: str
    instrument_id: str
    signal_type: SignalType
    direction: SignalDirection
    status: SignalStatus

    #: Normalized 0..1. Separate from confidence — see module docstring.
    strength: Optional[float] = None
    confidence: Optional[float] = None
    expected_return: Optional[float] = None
    expected_return_horizon_days: Optional[float] = None
    probability_up: Optional[float] = None

    security_id: Optional[str] = None
    company_id: Optional[str] = None

    agreement_state: AgreementState = AgreementState.INSUFFICIENT_EVIDENCE
    contributions: List[ModelContribution] = field(default_factory=list)
    context: SignalContext = field(default_factory=SignalContext)
    provenance: SignalProvenance = field(default_factory=SignalProvenance)
    explanation: SignalExplanation = field(default_factory=SignalExplanation)

    #: Why this signal is suppressed or rejected, when it is. Empty for
    #: an active signal.
    suppression_reasons: List[SuppressionReason] = field(default_factory=list)
    suppression_note: str = ""

    created_at: Optional[datetime] = None
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None
    superseded_by: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        for name in ("created_at", "valid_from", "valid_until"):
            _require_utc(getattr(self, name), name)
        if self.strength is not None and not 0.0 <= self.strength <= 1.0:
            raise ValueError(f"strength must be within 0..1 (got {self.strength})")
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be within 0..1 (got {self.confidence})")
        if self.probability_up is not None and not 0.0 <= self.probability_up <= 1.0:
            raise ValueError(f"probability_up must be within 0..1 (got {self.probability_up})")

    @property
    def probability_down(self) -> Optional[float]:
        return None if self.probability_up is None else round(1.0 - self.probability_up, 6)

    @property
    def is_actionable(self) -> bool:
        """
        ACTIVE, directional, and unexpired.

        Named `is_actionable` rather than `should_trade` on purpose:
        it says this signal is fit to be CONSIDERED by the risk layer,
        not that a trade should happen. Phase 10 never decides that.
        """
        return (self.status == SignalStatus.ACTIVE
                and self.direction in (SignalDirection.LONG, SignalDirection.SHORT)
                and not self.suppression_reasons)

    def is_expired_at(self, moment: datetime) -> bool:
        _require_utc(moment, "moment")
        return self.valid_until is not None and moment > self.valid_until

    def suppress(self, reason: SuppressionReason, note: str = "") -> None:
        """
        Withhold the signal, keeping it and its reason (spec §23).

        Never deletes. A suppressed signal is evidence about the
        system's behaviour and stays queryable.
        """
        if reason not in self.suppression_reasons:
            self.suppression_reasons.append(reason)
        self.status = SignalStatus.SUPPRESSED
        if note:
            self.suppression_note = f"{self.suppression_note}; {note}".strip("; ")
        self.explanation.add_caveat(f"suppressed: {reason.value}")


# ============================================================
# Strategy and configuration versioning (spec §19, §32)
# ============================================================

@dataclass
class SignalStrategyDefinition:
    """
    A registered, versioned strategy (spec §19).

    `is_active` gates generation: a retired strategy stops producing
    new signals but its historical signals remain valid records of what
    was believed at the time.
    """
    strategy_id: str
    name: str
    version: str
    signal_type: SignalType
    description: str = ""
    is_active: bool = True
    configuration_version: str = "v1"
    parameters: Dict[str, Any] = field(default_factory=dict)
    created_at: Optional[datetime] = None

    def __post_init__(self):
        _require_utc(self.created_at, "created_at")

    @property
    def qualified_id(self) -> str:
        return f"{self.strategy_id}:{self.version}"
