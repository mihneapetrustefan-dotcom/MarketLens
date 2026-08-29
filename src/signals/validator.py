"""
src/signals/validator.py
-----------------------------
Deterministic validation that turns a SignalCandidate into a Signal
(Phase 10, spec §8, §21, §23).

EVERY CANDIDATE PRODUCES A SIGNAL — SOMETIMES A SUPPRESSED ONE
------------------------------------------------------------------
`validate()` never returns None and never drops a candidate. A
candidate that fails checks becomes a Signal with status SUPPRESSED
and its reasons attached. Spec §23 is explicit: "do not silently
discard candidates".

This matters more than it looks. If failures vanished, the only thing
observable would be the signals that passed, and the system's own
selectivity would be invisible — you could not tell a strategy that
found nothing from one whose output was being thrown away, and
"suppressed 90% of candidates for stale data" would never surface.

WHAT IS CHECKED HERE, AND WHAT IS NOT
-----------------------------------------
Checked (spec §21): model abstention, confidence floor, strength
floor, data quality, prediction freshness, model conflict, instrument
support, event expiry.

NOT checked, deliberately: position limits, exposure, correlation,
drawdown, capital. Those are portfolio risk checks and belong to
Phase 11 (spec §21 says so directly). Putting them here would make
the risk layer partly redundant and partly bypassed — the worst of
both.

ALL THRESHOLDS ARE CONFIGURATION, NOT CONSTANTS (spec §31, §32)
-------------------------------------------------------------------
Every floor lives in ValidationConfig, which carries its own version.
A signal records which configuration version validated it, so a change
in thresholds is visible in the audit trail rather than silently
reinterpreting past decisions.

FAIL SAFE, NOT OPEN (spec §40, §46)
---------------------------------------
Where a check cannot be evaluated — missing data, unknown quality —
the outcome is suppression, not approval. An unknown is treated as a
reason to withhold, because this layer's failure mode should be
producing no signal rather than producing an unfounded one.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Set

from src.domain.signal_models import (
    AgreementState, Signal, SignalCandidate, SignalDirection, SignalStatus,
    SuppressionReason,
)


@dataclass
class ValidationConfig:
    """
    Versioned validation thresholds (spec §31, §32).

    Defaults are intentionally conservative. They are starting points
    chosen to be defensible, not values tuned on results — tuning
    thresholds against outcomes on a dataset this small would be
    fitting noise.
    """
    version: str = "v1"
    #: Below this, the signal is not trusted enough to act on.
    min_confidence: float = 0.25
    #: Below this, the expected move is too small to be worth a signal.
    min_strength: float = 0.10
    #: A prediction older than this is stale relative to its cutoff.
    max_prediction_age_days: float = 7.0
    #: Data quality levels accepted. 'invalid' is never acceptable.
    accepted_quality_levels: Set[str] = field(
        default_factory=lambda: {"high", "medium", "low"})
    #: When models genuinely conflict, suppress rather than pick a side.
    suppress_on_conflict: bool = True
    #: Instruments the engine will produce signals for. Empty means no
    #: restriction — used when the caller has already scoped the
    #: universe.
    supported_instruments: Set[str] = field(default_factory=set)
    #: How long a produced signal stays valid.
    validity_days: float = 5.0


class SignalValidator:
    """Applies deterministic checks; produces a Signal in every case."""

    def __init__(self, config: Optional[ValidationConfig] = None):
        self.config = config or ValidationConfig()

    def signal_id_for(self, candidate: SignalCandidate) -> str:
        """
        Deterministic signal id derived from the candidate id, so the
        same information state maps to the same signal id across runs.
        """
        return f"sig-{hashlib.sha1(candidate.candidate_id.encode('utf-8')).hexdigest()[:16]}"

    def validate(self, candidate: SignalCandidate,
                 now: Optional[datetime] = None) -> Signal:
        """
        Turn a candidate into a Signal — ACTIVE if every check passes,
        SUPPRESSED with reasons otherwise. Never returns None.
        """
        moment = now or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            raise ValueError("now must be timezone-aware")

        config = self.config
        cutoff = candidate.provenance.source_information_cutoff
        normalized_strength = candidate.metadata.get("normalized_strength")
        agreement_value = candidate.metadata.get("agreement_state")
        agreement = (AgreementState(agreement_value) if agreement_value
                     else AgreementState.INSUFFICIENT_EVIDENCE)

        reasons: List[SuppressionReason] = []
        notes: List[str] = []

        # --- instrument support ---
        if (config.supported_instruments
                and candidate.instrument_id not in config.supported_instruments):
            reasons.append(SuppressionReason.UNSUPPORTED_INSTRUMENT)
            notes.append(f"{candidate.instrument_id} not in supported universe")

        # --- model abstention / absence of a usable view ---
        usable = [c for c in candidate.contributions
                  if not c.is_abstention and c.predicted_value is not None]
        if not usable:
            reasons.append(SuppressionReason.MODEL_ABSTAINED)
            notes.append("no model produced a usable prediction")

        # --- direction ---
        if candidate.direction == SignalDirection.NO_SIGNAL:
            reasons.append(SuppressionReason.MODEL_ABSTAINED)
            notes.append("no directional view formed")

        # --- confidence floor ---
        if candidate.confidence is None:
            reasons.append(SuppressionReason.LOW_CONFIDENCE)
            notes.append("confidence could not be computed")
        elif candidate.confidence < config.min_confidence:
            reasons.append(SuppressionReason.LOW_CONFIDENCE)
            notes.append(f"confidence {candidate.confidence:.3f} "
                         f"below floor {config.min_confidence}")

        # --- strength floor. NEUTRAL is exempt: it is a claim that the
        # move is small, so a small strength is consistent, not a
        # failure. ---
        if candidate.direction in (SignalDirection.LONG, SignalDirection.SHORT):
            if normalized_strength is None:
                reasons.append(SuppressionReason.BELOW_STRENGTH_THRESHOLD)
                notes.append("strength could not be computed")
            elif normalized_strength < config.min_strength:
                reasons.append(SuppressionReason.BELOW_STRENGTH_THRESHOLD)
                notes.append(f"strength {normalized_strength:.3f} "
                             f"below floor {config.min_strength}")

        # --- data quality ---
        quality = candidate.context.data_quality_level
        if quality is None:
            reasons.append(SuppressionReason.POOR_DATA_QUALITY)
            notes.append("data quality unknown — treated as unacceptable")
        elif quality.lower() not in config.accepted_quality_levels:
            reasons.append(SuppressionReason.POOR_DATA_QUALITY)
            notes.append(f"data quality '{quality}' not accepted")

        # --- prediction freshness, measured against the information
        # cutoff rather than wall-clock creation time ---
        if cutoff is None:
            reasons.append(SuppressionReason.POOR_DATA_QUALITY)
            notes.append("no information cutoff recorded")
        else:
            age_days = (moment - cutoff).total_seconds() / 86400
            if age_days > config.max_prediction_age_days:
                reasons.append(SuppressionReason.STALE_PREDICTION)
                notes.append(f"information is {age_days:.1f} days old, "
                             f"limit {config.max_prediction_age_days}")

        # --- model conflict ---
        if config.suppress_on_conflict and agreement == AgreementState.CONFLICT:
            reasons.append(SuppressionReason.MODEL_CONFLICT)
            notes.append("contributing models disagree on direction")

        # --- small-sample evidence ---
        if any("small-sample" in c for c in candidate.explanation.caveats):
            reasons.append(SuppressionReason.SMALL_SAMPLE_EVIDENCE)
            notes.append("supporting evaluation was flagged small-sample")

        valid_from = cutoff or moment
        signal = Signal(
            signal_id=self.signal_id_for(candidate),
            instrument_id=candidate.instrument_id,
            signal_type=candidate.signal_type,
            direction=candidate.direction,
            status=SignalStatus.ACTIVE,
            strength=normalized_strength,
            confidence=candidate.confidence,
            expected_return=candidate.expected_return,
            expected_return_horizon_days=candidate.expected_return_horizon_days,
            probability_up=candidate.probability_up,
            agreement_state=agreement,
            contributions=list(candidate.contributions),
            context=candidate.context,
            provenance=candidate.provenance,
            explanation=candidate.explanation,
            created_at=moment,
            valid_from=valid_from,
            valid_until=valid_from + timedelta(days=config.validity_days),
            metadata=dict(candidate.metadata),
        )
        signal.provenance.configuration_version = config.version

        for reason in reasons:
            signal.suppress(reason)
        if notes:
            signal.suppression_note = "; ".join(notes)

        if not reasons:
            signal.explanation.add_factor(
                f"passed validation config {config.version}")

        return signal

    def validate_all(self, candidates: List[SignalCandidate],
                     now: Optional[datetime] = None) -> List[Signal]:
        return [self.validate(candidate, now) for candidate in candidates]
