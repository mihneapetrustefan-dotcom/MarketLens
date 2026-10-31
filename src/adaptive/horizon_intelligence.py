"""
src/adaptive/horizon_intelligence.py
--------------------------------------------------
Which horizon, if any, is worth deciding on (Phase 25.9I-F.5).

THE RULE THIS MODULE EXISTS TO ENFORCE
------------------------------------------
    AVAILABLE HORIZON  !=  PREFERRED HORIZON.

Choosing "5d because 5d exists" gives a position a holding period
nobody can defend, and it is the easiest mistake in the phase to make
because it always produces an answer. `preferred_horizon = None` is a
first-class, correct outcome, and on this repository's current data it
is the ONLY correct outcome: zero horizons have a qualified model.

DISAGREEMENT IS NOT RESOLVED BY VOTING
------------------------------------------
If d1 says down and d5 says up, the honest report is DISAGREEMENT, not
"2 out of 3 say up". The contract refuses a preferred horizon whenever
agreement is DISAGREEMENT, so a caller cannot smooth over a conflict by
picking the side it likes -- enforced in
`HorizonIntelligenceResult.__post_init__`, not by convention here.

A SINGLE HORIZON IS NOT AGREEMENT
-------------------------------------
One usable horizon agreeing with itself is INSUFFICIENT_EVIDENCE, the
same rule Phase 11 applies to model contributions. Calling it agreement
would let the thinnest possible basis produce the highest confidence.

PROTECTED HORIZONS CANNOT BE PREFERRED
------------------------------------------
d20 is sealed. It is reported in `protected_horizons` so a reader can
see it was considered and excluded, and the contract raises if it ever
appears as the preference.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

from src.adaptive.horizon_inventory import is_protected
from src.domain.signal_availability_models import (
    HorizonAgreement, HorizonIntelligenceResult, HorizonStatus,
    InstrumentHorizonStatus,
)
from src.domain.signal_models import SignalDirection

#: Directions that constitute a directional claim. NEUTRAL is a claim
#: too, but it is not a direction to agree or disagree ABOUT.
_DIRECTIONAL = (SignalDirection.LONG, SignalDirection.SHORT)


@dataclass(frozen=True)
class HorizonPolicy:
    """
    Every threshold, inert or explicit. No calibrated production values.

    `min_confidence` defaults to None: no calibrated confidence floor
    exists in this repository for horizon selection, and inventing one
    would decide which horizons are usable by fiat. With None, a
    horizon is usable when the canonical gates already said so.
    """
    min_confidence: Optional[float] = None
    #: Whether a lone usable horizon may become the preferred one. It
    #: may -- a single qualified model is thin evidence, not no
    #: evidence -- but the agreement state stays INSUFFICIENT_EVIDENCE
    #: so the thinness is visible downstream.
    single_horizon_may_be_preferred: bool = True
    #: Prefer the shortest usable horizon when several tie. Stated so
    #: the tie-break is inspectable rather than incidental.
    prefer_shorter_on_tie: bool = True


#: Ordering for tie-breaks and reporting. Shorter first.
_ORDER = {"intraday_5m": 0, "intraday_15m": 1, "intraday_30m": 2,
          "intraday_60m": 3, "d1": 4, "d3": 5, "d5": 6, "d10": 7, "d20": 8}


def _rank(horizon_id: str) -> Tuple[int, str]:
    return (_ORDER.get(horizon_id, 99), horizon_id)


class HorizonIntelligence:
    """
    Reasons over per-horizon statuses. Deterministic; no clock, no I/O.

    Decides nothing about capital, position size or execution. Its only
    output is "which horizon is worth deciding on, and why" -- or None.
    """

    def __init__(self, policy: Optional[HorizonPolicy] = None):
        self.policy = policy or HorizonPolicy()

    def assess(self, instrument_id: str,
               statuses: Sequence[InstrumentHorizonStatus], *,
               as_of: datetime) -> HorizonIntelligenceResult:
        """One instrument's horizon verdict, with the reason for it."""
        reasons: List[str] = []
        protected = tuple(sorted(
            (s.horizon_id for s in statuses
             if s.status is HorizonStatus.PROTECTED or is_protected(s.horizon_id)),
            key=_rank))
        considered = [s for s in statuses if s.horizon_id not in protected]

        usable = [s for s in considered if s.is_usable]
        if self.policy.min_confidence is not None:
            before = len(usable)
            usable = [s for s in usable
                      if s.confidence is not None
                      and s.confidence >= self.policy.min_confidence]
            if before != len(usable):
                reasons.append(
                    f"{before - len(usable)} horizon(s) fell below the "
                    f"configured confidence floor of "
                    f"{self.policy.min_confidence}")

        unsupported = tuple(sorted(
            (s.horizon_id for s in considered if not s.is_usable), key=_rank))

        if protected:
            reasons.append(
                f"excluded sealed horizon(s): {', '.join(protected)}")

        if not usable:
            reasons.append(
                "no horizon has a qualified model and usable inputs, so no "
                "preferred horizon can be justified")
            return HorizonIntelligenceResult(
                instrument_id=instrument_id, as_of=as_of,
                preferred_horizon=None, alternative_horizons=(),
                unsupported_horizons=unsupported, protected_horizons=protected,
                agreement=HorizonAgreement.INSUFFICIENT_EVIDENCE,
                confidence=None,
                completeness=self._completeness(statuses, usable),
                reasons=tuple(reasons))

        agreement = self.classify_agreement(usable)
        if agreement is HorizonAgreement.DISAGREEMENT:
            reasons.append(
                "usable horizons point in opposite directions: "
                + ", ".join(f"{s.horizon_id}={s.direction.value}"
                            for s in sorted(usable, key=lambda x: _rank(x.horizon_id)))
                + " -- this is reported, not voted on")
            return HorizonIntelligenceResult(
                instrument_id=instrument_id, as_of=as_of,
                preferred_horizon=None,
                alternative_horizons=tuple(sorted(
                    (s.horizon_id for s in usable), key=_rank)),
                unsupported_horizons=unsupported, protected_horizons=protected,
                agreement=agreement, confidence=None,
                completeness=self._completeness(statuses, usable),
                reasons=tuple(reasons))

        if len(usable) == 1 and not self.policy.single_horizon_may_be_preferred:
            reasons.append(
                "only one horizon is usable and policy requires corroboration")
            return HorizonIntelligenceResult(
                instrument_id=instrument_id, as_of=as_of,
                preferred_horizon=None,
                alternative_horizons=(usable[0].horizon_id,),
                unsupported_horizons=unsupported, protected_horizons=protected,
                agreement=agreement, confidence=None,
                completeness=self._completeness(statuses, usable),
                reasons=tuple(reasons))

        preferred = self._select(usable)
        alternatives = tuple(sorted(
            (s.horizon_id for s in usable if s.horizon_id != preferred.horizon_id),
            key=_rank))
        reasons.append(self._selection_reason(preferred, usable, agreement))

        return HorizonIntelligenceResult(
            instrument_id=instrument_id, as_of=as_of,
            preferred_horizon=preferred.horizon_id,
            alternative_horizons=alternatives,
            unsupported_horizons=unsupported, protected_horizons=protected,
            agreement=agreement, confidence=preferred.confidence,
            completeness=self._completeness(statuses, usable),
            reasons=tuple(reasons))

    # ---------------- helpers ----------------

    @staticmethod
    def classify_agreement(usable: Sequence[InstrumentHorizonStatus]
                           ) -> HorizonAgreement:
        """
        How the usable horizons relate.

        One horizon is INSUFFICIENT_EVIDENCE, never AGREEMENT: a single
        voice agreeing with itself is not corroboration.
        """
        directions = [s.direction for s in usable if s.direction in _DIRECTIONAL]
        if not directions:
            return HorizonAgreement.INSUFFICIENT_EVIDENCE
        if len(directions) == 1:
            return HorizonAgreement.INSUFFICIENT_EVIDENCE
        distinct = set(directions)
        if len(distinct) > 1:
            return HorizonAgreement.DISAGREEMENT
        neutral = [s for s in usable if s.direction is SignalDirection.NEUTRAL]
        return (HorizonAgreement.PARTIAL_AGREEMENT if neutral
                else HorizonAgreement.AGREEMENT)

    def _select(self, usable: Sequence[InstrumentHorizonStatus]
                ) -> InstrumentHorizonStatus:
        """
        The strongest evidence wins; shorter horizon breaks a tie.

        Confidence is the only ranking input, and an unmeasured
        confidence sorts LAST rather than being treated as zero or as
        average -- an unknown is not a score.
        """
        def key(status: InstrumentHorizonStatus):
            has_confidence = status.confidence is not None
            return (0 if has_confidence else 1,
                    -(status.confidence or 0.0),
                    _rank(status.horizon_id) if self.policy.prefer_shorter_on_tie
                    else (0, status.horizon_id))
        return sorted(usable, key=key)[0]

    @staticmethod
    def _selection_reason(preferred: InstrumentHorizonStatus,
                          usable: Sequence[InstrumentHorizonStatus],
                          agreement: HorizonAgreement) -> str:
        if len(usable) == 1:
            return (f"{preferred.horizon_id} is the only horizon with a "
                    "qualified model and usable inputs; evidence is thin and "
                    "the agreement state says so")
        return (f"{preferred.horizon_id} carries the strongest evidence of "
                f"{len(usable)} usable horizons ({agreement.value})")

    @staticmethod
    def _completeness(all_statuses: Sequence[InstrumentHorizonStatus],
                      usable: Sequence[InstrumentHorizonStatus]) -> float:
        if not all_statuses:
            return 0.0
        return round(len(usable) / len(all_statuses), 6)


def horizon_answer_for_request(result: HorizonIntelligenceResult
                               ) -> Dict[str, object]:
    """
    The answer to 25.9I-F's `horizon_reestimation_required`.

    Returns what can be said, which is frequently "nothing". An event
    may ASK for a horizon to be re-estimated; it can never supply the
    answer, and neither can this module without a qualified model.
    There is deliberately no `extra_days` field: +5 days has to come
    from an estimator or not at all.
    """
    return {
        "answered": result.has_preference,
        "preferred_horizon": result.preferred_horizon,
        "alternative_horizons": list(result.alternative_horizons),
        "agreement": result.agreement.value,
        "confidence": result.confidence,
        "reason": ("; ".join(result.reasons) if not result.has_preference
                   else result.reasons[-1] if result.reasons else ""),
        "note": ("no horizon extension is proposed; a duration must come "
                 "from a qualified estimator or not at all"),
    }
