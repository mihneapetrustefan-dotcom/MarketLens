"""
src/adaptive/target_estimation.py
-----------------------------------------------
Turning a model's expected return into a target zone, or refusing to
(Phase 25.9I-F.5).

THE ONLY PATH TO A TARGET
-----------------------------
    a QUALIFIED model
      -> a calibrated expected return for a named horizon
        -> a deterministic, uncertainty-aware transformation
          -> a target zone WITH LINEAGE

There is no other path. In particular there is no

    target = current_price * some_multiplier

because a multiplier is a made-up number wearing a decimal point, and
the resulting zone would look exactly as authoritative as a real one.

WHAT THIS RETURNS TODAY
---------------------------
`TargetStatus.UNAVAILABLE`, for every instrument. Measured 2026-09-30:
zero models are qualified, so no calibrated expected return exists
anywhere in the repository. That is not a failure of this module --
it is the honest answer, and the point of the phase is that the answer
now SAYS why instead of leaving a blank.

25.9I-F asks `target_reestimation_required`. This module is what
answers it, and "UNAVAILABLE, because no qualified model produces an
expected return" is a complete answer.

WHY THE ZONE IS A ZONE
--------------------------
25.9I-E takes a `TargetZone`, not a price, and for a good reason: a
point estimate invites false precision. The half-width comes from the
model's own uncertainty -- a prediction interval when one exists,
otherwise a stated multiple of the estimate's standard error. When
NEITHER exists, the target is refused rather than given an invented
width, because a zone whose width was guessed is a point estimate with
decoration.

THE PROTECTED HORIZON CANNOT SUPPLY ONE
-------------------------------------------
A target derived from d20 would be a target derived from the sealed
experiment's outcome. `estimate()` refuses a protected horizon before
it reads anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

from src.adaptive.horizon_inventory import PROTECTED_HORIZONS, is_protected
from src.domain.signal_availability_models import (
    TargetEstimate, TargetStatus,
)


@dataclass(frozen=True)
class ExpectedReturnEstimate:
    """
    A model's expected return, with the provenance that makes it usable.

    Every field except the value itself is required for a reason: an
    expected return whose model, horizon and qualification are unknown
    cannot be audited, and an unauditable number must not move a
    position.

    `is_calibrated` is separate from `confidence`. A model can be
    confident and badly calibrated; calibration is the question of
    whether its stated confidence means anything.
    """
    instrument_id: str
    horizon_id: str
    expected_return: float
    model_qualified_id: str
    as_of: datetime
    model_is_qualified: bool = False
    is_calibrated: bool = False
    confidence: Optional[float] = None
    standard_error: Optional[float] = None
    interval_low: Optional[float] = None
    interval_high: Optional[float] = None
    uncertainty_basis: str = ""

    def __post_init__(self):
        if not str(self.model_qualified_id).strip():
            raise ValueError(
                "an expected return must name the model that produced it")
        if is_protected(self.horizon_id):
            raise ValueError(
                f"{self.horizon_id} is sealed; no expected return may be "
                "constructed from it")

    @property
    def has_uncertainty(self) -> bool:
        return (self.standard_error is not None
                or (self.interval_low is not None
                    and self.interval_high is not None))


@dataclass(frozen=True)
class TargetPolicy:
    """
    How an expected return becomes a zone. Stated, not hidden.

    `standard_error_multiple` widens a zone from a standard error when
    no interval was supplied. 1.0 is deliberately modest and is a
    STATED CONVENTION, not a confidence level -- calling it "one sigma"
    would imply a distributional claim nobody has earned.
    """
    standard_error_multiple: float = 1.0
    #: Whether an uncalibrated model may still produce a target. It may
    #: not: an uncalibrated expected return has no defensible scale, and
    #: scaling a price by it produces a confident-looking wrong number.
    require_calibration: bool = True
    #: Whether an unqualified model may produce a target. It may not.
    require_qualified_model: bool = True
    #: Minimum zone half-width as a fraction of the reference price, so
    #: a near-zero uncertainty cannot collapse a zone into a point.
    minimum_half_width_fraction: float = 0.001

    def __post_init__(self):
        if self.standard_error_multiple <= 0:
            raise ValueError("standard_error_multiple must be > 0")
        if self.minimum_half_width_fraction < 0:
            raise ValueError("minimum_half_width_fraction must be >= 0")


class TargetEstimator:
    """
    Produces a target zone only when the evidence supports one.

    Deterministic: same inputs, same zone. No clock, no database, no
    model inference -- the expected return arrives as an argument,
    because this module's job is the transformation and the refusal,
    not the prediction.
    """

    def __init__(self, policy: Optional[TargetPolicy] = None):
        self.policy = policy or TargetPolicy()

    def estimate(self, instrument_id: str, *, as_of: datetime,
                 reference_price: Optional[float] = None,
                 horizon_id: Optional[str] = None,
                 expected_return: Optional[ExpectedReturnEstimate] = None,
                 ) -> TargetEstimate:
        """
        A target zone, or an explicit, reasoned UNAVAILABLE.

        Checks in order: sealed horizon, then missing estimate, then
        qualification, then calibration, then price, then uncertainty.
        The first failure is reported, so the reason names something
        whose repair would actually help.
        """
        reasons: List[str] = []
        horizon = horizon_id or (expected_return.horizon_id
                                 if expected_return else None)

        if horizon and is_protected(horizon):
            return TargetEstimate(
                instrument_id=instrument_id, as_of=as_of,
                status=TargetStatus.PROTECTED, horizon_id=horizon,
                reasons=(f"{horizon} is sealed: "
                         f"{PROTECTED_HORIZONS[horizon.lower()]}",))

        if expected_return is None:
            return TargetEstimate(
                instrument_id=instrument_id, as_of=as_of,
                status=TargetStatus.UNAVAILABLE, horizon_id=horizon,
                reference_price=reference_price,
                reasons=("no qualified model produces a calibrated expected "
                         "return for this instrument, so no defensible target "
                         "exists",))

        if self.policy.require_qualified_model and not expected_return.model_is_qualified:
            reasons.append(
                f"model {expected_return.model_qualified_id} is not qualified; "
                "an unqualified estimate cannot set a price target")
        if self.policy.require_calibration and not expected_return.is_calibrated:
            reasons.append(
                f"model {expected_return.model_qualified_id} is not calibrated; "
                "an uncalibrated expected return has no defensible scale")
        if reference_price is None or reference_price <= 0:
            reasons.append(
                "no usable reference price, so an expected return cannot be "
                "turned into a level")
        if not expected_return.has_uncertainty:
            reasons.append(
                "the estimate carries no uncertainty; a zone whose width was "
                "guessed is a point estimate with decoration")

        if reasons:
            return TargetEstimate(
                instrument_id=instrument_id, as_of=as_of,
                status=TargetStatus.UNAVAILABLE, horizon_id=horizon,
                reference_price=reference_price,
                expected_return=expected_return.expected_return,
                confidence=expected_return.confidence,
                model_qualified_id=expected_return.model_qualified_id,
                completeness=self._completeness(expected_return,
                                                reference_price),
                reasons=tuple(reasons))

        centre = reference_price * (1.0 + expected_return.expected_return)
        half_width = self._half_width(expected_return, reference_price)
        low = max(0.0, round(centre - half_width, 6))
        high = round(centre + half_width, 6)

        return TargetEstimate(
            instrument_id=instrument_id, as_of=as_of,
            status=TargetStatus.AVAILABLE, horizon_id=horizon,
            reference_price=reference_price,
            expected_return=expected_return.expected_return,
            uncertainty=round(half_width / reference_price, 6),
            target_low=low, target_high=high,
            confidence=expected_return.confidence,
            model_qualified_id=expected_return.model_qualified_id,
            method=(f"reference_price x (1 + expected_return), widened by "
                    f"{expected_return.uncertainty_basis or 'model uncertainty'}"),
            completeness=self._completeness(expected_return, reference_price),
            reasons=(
                f"derived from {expected_return.model_qualified_id} at "
                f"{expected_return.horizon_id}: expected return "
                f"{expected_return.expected_return:+.4f} on a reference price "
                f"of {reference_price}",))

    # ---------------- helpers ----------------

    def _half_width(self, estimate: ExpectedReturnEstimate,
                    reference_price: float) -> float:
        """
        Zone half-width, from the model's own uncertainty.

        Prefers a supplied prediction interval over a standard error:
        an interval is what the model said, a multiple of a standard
        error is what this module inferred from it.
        """
        if estimate.interval_low is not None and estimate.interval_high is not None:
            span = abs(estimate.interval_high - estimate.interval_low) / 2.0
            width = reference_price * span
        else:
            width = (reference_price * abs(estimate.standard_error or 0.0)
                     * self.policy.standard_error_multiple)
        floor = reference_price * self.policy.minimum_half_width_fraction
        return max(width, floor)

    @staticmethod
    def _completeness(estimate: Optional[ExpectedReturnEstimate],
                      reference_price: Optional[float]) -> float:
        present = 0
        total = 5
        if estimate is not None:
            present += 1
            if estimate.model_is_qualified:
                present += 1
            if estimate.is_calibrated:
                present += 1
            if estimate.has_uncertainty:
                present += 1
        if reference_price:
            present += 1
        return round(present / total, 6)


def target_answer_for_request(target: TargetEstimate) -> Dict[str, object]:
    """
    The answer to 25.9I-F's `target_reestimation_required`.

    "UNAVAILABLE, and here is why" is a complete answer. An event may
    ASK for a target to be re-estimated; it can never supply one, and
    neither can this module without a qualified, calibrated model.
    """
    return {
        "answered": target.status is TargetStatus.AVAILABLE,
        "target_status": target.status.value,
        "target_zone": ([target.target_low, target.target_high]
                        if target.has_zone else None),
        "model": target.model_qualified_id,
        "horizon": target.horizon_id,
        "completeness": target.completeness,
        "reasons": list(target.reasons),
        "note": ("a target may only come from a qualified, calibrated model; "
                 "event materiality can request one but never determine it"),
    }
