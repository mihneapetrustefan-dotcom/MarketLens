"""
src/signals/strategy.py
----------------------------
Signal strategy framework and the deterministic scoring that turns raw
model output into a bounded, explainable strength and confidence
(Phase 10, spec §6, §7, §19, §20).

A STRATEGY PRODUCES A CANDIDATE, NEVER A SIGNAL (spec §20)
--------------------------------------------------------------
`generate()` returns SignalCandidate objects. Only the validator can
turn one into a Signal. That is enforced by return type, not by
convention: a strategy has no way to construct a validated signal even
if its author wanted to.

STRENGTH AND CONFIDENCE ARE COMPUTED SEPARATELY, FROM DIFFERENT INPUTS
-------------------------------------------------------------------------
This is the part spec §6 and §7 care most about, so it is worth being
explicit about the formulas rather than burying them.

STRENGTH answers "how big is the expected move?". It is the predicted
value normalized against a scale parameter:

    strength = min(1.0, |predicted_value| / strength_scale)

The scale is a STRATEGY PARAMETER, versioned with the strategy, not a
constant hidden in code. Changing it changes the strategy version,
which is what makes historical signals comparable to each other. The
mapping is deliberately linear and saturating — a fancier curve would
be harder to defend and no better justified.

Strength is NOT a probability. A strength of 0.8 does not mean 80%
likely; it means the expected move is 80% of the scale at which this
strategy considers a move maximal.

CONFIDENCE answers "how much do we trust that estimate?". Spec §7
explicitly forbids "a meaningless average of unrelated scores", so
confidence is built multiplicatively from independent factors, each in
0..1, each documented:

    confidence = base * quality_factor * agreement_factor * sample_factor

  base            — the model's own confidence, or 0.5 when it reports
                    none. 0.5 rather than 1.0: an unknown confidence is
                    not a confident one.
  quality_factor  — from the observation's data quality level. Poor
                    inputs cap trust regardless of how clean the model
                    output looks.
  agreement_factor— from AgreementState. Conflicting evidence reduces
                    confidence rather than being averaged away.
  sample_factor   — reduced when the supporting evidence came from a
                    small effective sample.

Multiplicative, not additive, because these are all NECESSARY
conditions: a model that is confident about garbage input should not
be rescued by its own confidence. Any single factor near zero should
collapse the result, and multiplication does that where averaging
would not.

NOTHING HERE IS AN LLM CALL (spec §18)
------------------------------------------
Every number and every explanation string is derived deterministically
from structured inputs. Re-running on the same inputs produces
byte-identical output.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.domain.signal_models import (
    AgreementState, ModelContribution, SignalCandidate, SignalContext,
    SignalDirection, SignalExplanation, SignalProvenance,
    SignalStrategyDefinition, SignalType,
)

#: Default scale at which a predicted abnormal return is considered a
#: maximal move. 5% is a large single-event abnormal return for a
#: liquid equity; strategies may override it as a parameter.
DEFAULT_STRENGTH_SCALE = 0.05

#: Data quality level -> confidence multiplier. An unknown level is
#: treated as 'low', not as 'high': absence of a quality signal is not
#: evidence of quality.
QUALITY_FACTORS = {"high": 1.0, "medium": 0.8, "low": 0.5, "invalid": 0.0}

#: Agreement state -> confidence multiplier (spec §10).
AGREEMENT_FACTORS = {
    AgreementState.AGREEMENT: 1.0,
    AgreementState.PARTIAL_AGREEMENT: 0.75,
    AgreementState.CONFLICT: 0.3,
    AgreementState.INSUFFICIENT_EVIDENCE: 0.6,
}


def normalize_strength(predicted_value: Optional[float],
                       scale: float = DEFAULT_STRENGTH_SCALE) -> Optional[float]:
    """
    Map a predicted magnitude onto 0..1. See module docstring for why
    this is linear-and-saturating rather than something cleverer.
    """
    if predicted_value is None or scale <= 0:
        return None
    return round(min(1.0, abs(predicted_value) / scale), 6)


def compute_confidence(model_confidence: Optional[float],
                       data_quality_level: Optional[str],
                       agreement: AgreementState,
                       small_sample: bool = False) -> float:
    """
    Combine independent trust factors multiplicatively.

    Returns 0..1. See the module docstring for each factor's meaning
    and why multiplication is the right combiner here.
    """
    base = 0.5 if model_confidence is None else max(0.0, min(1.0, model_confidence))
    quality = QUALITY_FACTORS.get((data_quality_level or "low").lower(), 0.5)
    agreement_factor = AGREEMENT_FACTORS.get(agreement, 0.6)
    #: Evidence from a small effective sample is halved rather than
    #: discarded — it is weak, not worthless.
    sample_factor = 0.5 if small_sample else 1.0
    return round(base * quality * agreement_factor * sample_factor, 6)


def classify_agreement(contributions: List[ModelContribution]) -> AgreementState:
    """
    Derive the agreement state from contributing model directions
    (spec §10).

    One contribution is INSUFFICIENT_EVIDENCE, not AGREEMENT: a single
    voice agreeing with itself is not corroboration, and calling it
    agreement would inflate confidence on the thinnest possible basis.
    """
    usable = [c for c in contributions
              if not c.is_abstention and c.predicted_value is not None]
    if len(usable) < 2:
        return AgreementState.INSUFFICIENT_EVIDENCE

    positive = sum(1 for c in usable if c.predicted_value > 0)
    negative = sum(1 for c in usable if c.predicted_value < 0)
    if positive == 0 or negative == 0:
        return AgreementState.AGREEMENT
    minority = min(positive, negative)
    #: A lone dissenter among several is partial agreement; an even
    #: split is a genuine conflict.
    return (AgreementState.PARTIAL_AGREEMENT if minority / len(usable) < 0.34
            else AgreementState.CONFLICT)


def direction_from_value(predicted_value: Optional[float],
                         neutral_band: float = 0.0) -> SignalDirection:
    """
    Sign of the prediction, with an optional dead zone.

    A prediction inside the neutral band yields NEUTRAL (an active
    claim of no meaningful move); a missing prediction yields
    NO_SIGNAL (no claim at all). See SignalDirection's docstring for
    why those must not collapse together.
    """
    if predicted_value is None:
        return SignalDirection.NO_SIGNAL
    if abs(predicted_value) <= neutral_band:
        return SignalDirection.NEUTRAL
    return SignalDirection.LONG if predicted_value > 0 else SignalDirection.SHORT


@dataclass
class GenerationContext:
    """
    Everything a strategy is allowed to see for one instrument at one
    information state.

    Mirrors Phase 8's FeatureContext in spirit: a strategy receives
    this object and nothing else, so it cannot reach past the cutoff
    even carelessly. Unlike FeatureContext it does no filtering itself
    — the data handed in was already selected as-of the cutoff by the
    caller, and `information_cutoff` is carried so every produced
    candidate can record it.
    """
    instrument_id: str
    information_cutoff: datetime
    #: Phase 9 predictions for this instrument at this cutoff.
    predictions: List[Any] = field(default_factory=list)
    #: Numeric Phase 7/8 features, keyed by qualified name.
    features: Dict[str, Optional[float]] = field(default_factory=dict)
    context: SignalContext = field(default_factory=SignalContext)
    observation_id: Optional[str] = None
    event_id: Optional[str] = None
    feature_set_version: Optional[str] = None
    dataset_version: Optional[str] = None
    #: True when the evidence behind these predictions came from an
    #: evaluation flagged as small-sample. Feeds the confidence
    #: penalty rather than being silently ignored.
    small_sample_evidence: bool = False

    def __post_init__(self):
        if self.information_cutoff.tzinfo is None:
            raise ValueError("information_cutoff must be timezone-aware")

    def feature(self, name: str) -> Optional[float]:
        return self.features.get(name)


class SignalStrategy(ABC):
    """
    Base class for all signal strategies (spec §19).

    A strategy is defined by its `definition` (which carries its
    version and parameters) and its `generate` method. It never
    persists anything, never validates, and never produces a Signal —
    only candidates.
    """

    def __init__(self, definition: SignalStrategyDefinition):
        self.definition = definition

    @property
    def strategy_id(self) -> str:
        return self.definition.strategy_id

    @property
    def version(self) -> str:
        return self.definition.version

    def parameter(self, name: str, default: Any = None) -> Any:
        return self.definition.parameters.get(name, default)

    def candidate_id_for(self, context: GenerationContext, suffix: str = "") -> str:
        """
        Deterministic candidate id from the same inputs as the signal
        identity, so re-running a strategy over unchanged information
        produces the same candidate id rather than a fresh uuid.
        """
        raw = (f"{self.strategy_id}|{self.version}|{self.definition.configuration_version}"
               f"|{context.instrument_id}|{context.information_cutoff.isoformat()}|{suffix}")
        return f"cand-{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]}"

    @abstractmethod
    def generate(self, context: GenerationContext) -> List[SignalCandidate]:
        """Propose zero or more candidates. Never raises for ordinary missing data."""


class MLDirectionalStrategy(SignalStrategy):
    """
    Turns Phase 9 model predictions into directional candidates
    (spec §19's requirement for at least one real end-to-end strategy).

    Uses actual persisted predictions — predicted_value, confidence,
    abstention state — rather than recomputing anything. If the model
    abstained, the candidate is still produced, carrying NO_SIGNAL and
    the abstention recorded as a contribution: the validator decides
    what to do with it, and the fact that the system looked and found
    nothing stays visible.
    """

    def generate(self, context: GenerationContext) -> List[SignalCandidate]:
        if not context.predictions:
            return []

        scale = float(self.parameter("strength_scale", DEFAULT_STRENGTH_SCALE))
        neutral_band = float(self.parameter("neutral_band", 0.0))
        horizon_days = self.parameter("horizon_days")

        contributions = [
            ModelContribution(
                prediction_id=p.prediction_id,
                trained_model_id=p.trained_model_id,
                model_qualified_id=p.model_qualified_id,
                predicted_value=p.predicted_value,
                confidence=p.confidence,
                probability_up=(p.class_probabilities or {}).get("up") if p.class_probabilities else None,
                weight=1.0,
                is_abstention=p.is_abstention,
                note=p.abstention_reason or "",
            )
            for p in context.predictions
        ]

        usable = [c for c in contributions if not c.is_abstention and c.predicted_value is not None]
        agreement = classify_agreement(contributions)

        if usable:
            #: Equal-weighted mean across contributing models. Spec §9
            #: forbids blind averaging as the FINAL word, which is why
            #: the disagreement is separately recorded in
            #: agreement_state and penalises confidence — the mean is
            #: the point estimate, not the whole story.
            combined = sum(c.predicted_value for c in usable) / len(usable)
            model_confidences = [c.confidence for c in usable if c.confidence is not None]
            mean_confidence = (sum(model_confidences) / len(model_confidences)
                               if model_confidences else None)
        else:
            combined = None
            mean_confidence = None

        direction = direction_from_value(combined, neutral_band)
        strength = normalize_strength(combined, scale)
        confidence = compute_confidence(
            mean_confidence, context.context.data_quality_level,
            agreement, context.small_sample_evidence)

        explanation = SignalExplanation()
        explanation.summary = (
            f"{direction.value} from {len(usable)} model prediction(s) "
            f"with mean expected return {combined:.4f}" if combined is not None
            else "no usable model prediction at this information state")
        for c in usable:
            explanation.add_factor(
                f"{c.model_qualified_id} predicted {c.predicted_value:+.4f}")
        if agreement in (AgreementState.CONFLICT, AgreementState.PARTIAL_AGREEMENT):
            explanation.add_caveat(f"model agreement: {agreement.value}")
        if agreement == AgreementState.INSUFFICIENT_EVIDENCE and usable:
            explanation.add_caveat("single model — no corroboration between models")
        if context.small_sample_evidence:
            explanation.add_caveat("supporting evaluation was flagged small-sample")
        if context.context.data_quality_level and context.context.data_quality_level != "high":
            explanation.add_caveat(f"data quality: {context.context.data_quality_level}")
        for c in contributions:
            if c.is_abstention:
                explanation.add_caveat(f"{c.model_qualified_id} abstained")

        candidate = SignalCandidate(
            candidate_id=self.candidate_id_for(context),
            instrument_id=context.instrument_id,
            signal_type=SignalType.DIRECTIONAL,
            direction=direction,
            strategy_id=self.strategy_id,
            strategy_version=self.version,
            raw_strength=combined,
            confidence=confidence,
            expected_return=combined,
            expected_return_horizon_days=float(horizon_days) if horizon_days else None,
            contributions=contributions,
            context=context.context,
            provenance=SignalProvenance(
                observation_id=context.observation_id,
                event_id=context.event_id,
                instrument_id=context.instrument_id,
                feature_set_version=context.feature_set_version,
                dataset_version=context.dataset_version,
                strategy_id=self.strategy_id,
                strategy_version=self.version,
                configuration_version=self.definition.configuration_version,
                source_information_cutoff=context.information_cutoff,
                inputs={"normalized_strength": strength,
                        "agreement_state": agreement.value},
            ),
            explanation=explanation,
            created_at=datetime.now(timezone.utc),
            metadata={"normalized_strength": strength, "agreement_state": agreement.value},
        )
        return [candidate]
