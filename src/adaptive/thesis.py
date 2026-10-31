"""
src/adaptive/thesis.py
-----------------------------------
The position thesis engine (Phase 25.9I-E). Shadow only.

THE THESIS ENGINE DOES NOT CONTROL BROKER ORDERS
----------------------------------------------------
Every output is a RECOMMENDATION with `is_executable` permanently False.
It carries no venue, no account, no order id and no share quantity. The
path to a broker is unchanged and unshortened: recommendation ->
canonical Portfolio -> canonical Risk -> OrderIntent -> Execution. This
module imports none of them, and a test asserts the imports.

WHAT A THESIS IS FOR
------------------------
So that "why do we still hold this?" has an answer that is not "we
bought it". Entry reason, expectation, horizon, target zone,
invalidation and accepted risks are recorded at entry and revised only
through immutable revisions, so the position's history of belief
survives the position.

THE THREE RULES THAT DO THE WORK
------------------------------------
1. PRICE ALONE MOVES NOTHING. A price near the target produces
   TARGET_APPROACH and a request to REASSESS. It can never, by itself,
   produce EXTEND_TARGET. Raising a target because the price rose is a
   position following its own P&L, and this engine refuses it (the
   25.9I-A revision rules enforce it a second time at construction).

2. TIME DOES NOT EXTEND ITSELF. A seven-day thesis at day seven is
   expired, not a fourteen-day thesis. Extension needs material new
   evidence, recorded, or the thesis expires and the policy decides
   whether that means exit.

3. COOLDOWN RESTRAINS GREED, NOT SAFETY. The anti-overreaction policy
   blocks repeated upside revisions inside a cooldown window. It never
   blocks an exit, a target reduction or a protection tightening:
   §24 is explicit, and a cooldown that could suppress a risk response
   would be a bug with a schedule.

UNKNOWNS DO NOT BECOME OPINIONS
-----------------------------------
Evidence whose materiality was never measured cannot clear a materiality
threshold. A thesis with no confidence value does not acquire one. When
the engine cannot justify an action it returns NO_RECOMMENDATION or
INSUFFICIENT_DATA rather than inventing a fraction to look decisive.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.adaptive.risk_budget import PositionRiskEstimator, RiskConfig
from src.domain.adaptive_models import (
    DecisionLineage, MarketDataRequirement, MarketDataState,
    RealtimeActivationReason, require_utc,
)
from src.domain.allocation_models import PositionRiskEstimate
from src.domain.thesis_models import (
    EVIDENCE_BEARING_REASONS, EvidenceDirection, ExpectedHorizon,
    InvalidationZone, PositionAction, PositionActionRecommendation,
    PositionDirection, PositionThesis, PositionThesisRevision, RevisionPolicy,
    TargetZone, ThesisEvidence, ThesisRevisionReason, ThesisStatus,
    transition_allowed,
)


class ThesisEngineError(ValueError):
    """The requested change is not legal for this thesis."""


#: Actions that reduce or protect exposure. These are never blocked by an
#: upside cooldown (§24).
SAFETY_ACTIONS = frozenset({
    PositionAction.FULL_EXIT, PositionAction.PARTIAL_EXIT,
    PositionAction.REDUCE_TARGET, PositionAction.TIGHTEN_STOP,
})


@dataclass(frozen=True)
class ThesisPolicy:
    """
    How a thesis may change. No calibrated values are invented here.

    `revision_policy` is the 25.9I-A anti-overreaction contract, inert by
    default. `partial_exit_fraction` is None on purpose: there is no
    evidence for 40%, 25% or any other number, so a caller that wants a
    partial exit must say how much. The engine will not guess (§15).
    """
    version: str = "thesis-v1"
    revision_policy: RevisionPolicy = field(default_factory=RevisionPolicy)
    #: Materiality an item of evidence needs before it can extend a
    #: target or a horizon. 0.0 means "any measured materiality".
    minimum_extension_materiality: float = 0.0
    #: Explicit fraction for a policy-driven partial exit. None = the
    #: engine has no opinion and will say so.
    partial_exit_fraction: Optional[float] = None
    #: What an expired thesis with no new evidence should recommend.
    expiry_action: PositionAction = PositionAction.FULL_EXIT
    #: How close to the target zone counts as "approaching".
    target_approach_fraction: float = 0.9

    def __post_init__(self):
        if not 0.0 <= self.minimum_extension_materiality <= 1.0:
            raise ValueError("minimum_extension_materiality must be within [0, 1]")
        if self.partial_exit_fraction is not None and not (
                0.0 < self.partial_exit_fraction < 1.0):
            raise ValueError("partial_exit_fraction must be within (0, 1)")
        if not 0.0 < self.target_approach_fraction <= 1.0:
            raise ValueError("target_approach_fraction must be within (0, 1]")
        if self.expiry_action not in (PositionAction.FULL_EXIT,
                                      PositionAction.HOLD,
                                      PositionAction.PARTIAL_EXIT):
            raise ValueError("expiry_action must be FULL_EXIT, PARTIAL_EXIT or HOLD")


@dataclass(frozen=True)
class CapitalReleaseRecommendation:
    """
    Capital a proposed exit would free. A statement, not an instruction.

    25.9I-D deferred rebalancing, and this does not undo that: nothing
    here allocates the released capital, and no allocator reads it
    automatically. Portfolio decides later what freed capital is for.
    """
    instrument_id: str
    released_fraction: float
    released_notional: Optional[float] = None
    note: str = ""

    def __post_init__(self):
        if not 0.0 < self.released_fraction <= 1.0:
            raise ValueError("released_fraction must be within (0, 1]")
        if self.released_notional is not None and self.released_notional < 0:
            raise ValueError("released_notional must be >= 0")


@dataclass(frozen=True)
class ThesisAssessment:
    """
    One reassessment: what the engine thinks, and what it needs to act.

    `is_executable` is False and has no setter. `required_state` and
    `freshness` say what market data a later phase would need BEFORE
    acting -- they request a state, they never activate one. 25.9I-C
    owns subscriptions, and it is externally blocked.
    """
    thesis_id: str
    as_of: datetime
    recommendation: PositionActionRecommendation
    required_state: MarketDataState
    freshness: MarketDataRequirement
    revision: Optional[PositionThesisRevision] = None
    revised_thesis: Optional[PositionThesis] = None
    activation_reason: Optional[RealtimeActivationReason] = None
    capital_release: Optional[CapitalReleaseRecommendation] = None
    reasons: Tuple[str, ...] = ()
    blocked_by_policy: Tuple[str, ...] = ()
    lineage: Optional[DecisionLineage] = None

    def __post_init__(self):
        object.__setattr__(self, "as_of", require_utc(self.as_of, "as_of"))

    @property
    def is_executable(self) -> bool:
        """Always False. A recommendation is upstream of every gate."""
        return False

    @property
    def action(self) -> PositionAction:
        return self.recommendation.action

    def describe(self) -> Dict[str, Any]:
        return {
            "thesis_id": self.thesis_id, "as_of": self.as_of.isoformat(),
            "action": self.action.value,
            "exit_fraction": self.recommendation.exit_fraction,
            "required_market_data_state": self.required_state.value,
            "activation_reason": (self.activation_reason.value
                                  if self.activation_reason else None),
            "revision": self.revision.revision_id if self.revision else None,
            "revised_to": (self.revised_thesis.thesis_id
                           if self.revised_thesis else None),
            "capital_release": (self.capital_release.released_fraction
                                if self.capital_release else None),
            "reasons": list(self.reasons),
            "blocked_by_policy": list(self.blocked_by_policy),
            "is_executable": self.is_executable,
        }


def _thesis_id(instrument_id: str, as_of: datetime, revision: int) -> str:
    digest = hashlib.sha256(
        f"{instrument_id}|{as_of.isoformat()}|{revision}".encode("utf-8"))
    return "th-" + digest.hexdigest()[:16]


def _revision_id(thesis_id: str, as_of: datetime, reason: str) -> str:
    digest = hashlib.sha256(
        f"{thesis_id}|{as_of.isoformat()}|{reason}".encode("utf-8"))
    return "rev-" + digest.hexdigest()[:16]


class PositionThesisEngine:
    """
    Creates theses, reassesses them against evidence, revises them.

    Deterministic and free of I/O: no clock, no database, no transport.
    Every input is an argument, which is what makes a revision
    reproducible and the safety properties checkable by reading imports.
    """

    def __init__(self, policy: Optional[ThesisPolicy] = None,
                 risk_config: Optional[RiskConfig] = None):
        self.policy = policy or ThesisPolicy()
        self.risk_estimator = PositionRiskEstimator(risk_config or RiskConfig())

    # ---------------- creation ----------------

    def open_thesis(self, instrument_id: str, *, as_of: datetime,
                    direction: PositionDirection, target_zone: TargetZone,
                    invalidation_zone: InvalidationZone,
                    horizon: ExpectedHorizon,
                    entry_price: Optional[float] = None,
                    entry_notional: Optional[float] = None,
                    confidence: Optional[float] = None,
                    summary: str = "",
                    risk_assumptions: Sequence[str] = (),
                    event_assumptions: Sequence[str] = (),
                    regime_assumptions: Sequence[str] = (),
                    session_snapshot_id: Optional[str] = None,
                    admission_decision_id: Optional[str] = None,
                    allocation_plan_id: Optional[str] = None,
                    lineage: Optional[DecisionLineage] = None) -> PositionThesis:
        """The entry record. Nothing is inferred that was not supplied."""
        as_of = require_utc(as_of, "as_of")
        return PositionThesis(
            thesis_id=_thesis_id(instrument_id, as_of, 1),
            instrument_id=instrument_id, direction=direction, created_at=as_of,
            target_zone=target_zone, invalidation_zone=invalidation_zone,
            horizon=horizon, entry_price=entry_price,
            entry_notional=entry_notional, confidence=confidence,
            initial_confidence=confidence, initial_horizon=horizon,
            initial_summary=summary, current_summary=summary,
            risk_assumptions=tuple(risk_assumptions),
            event_assumptions=tuple(event_assumptions),
            regime_assumptions=tuple(regime_assumptions),
            status=ThesisStatus.ACTIVE, remaining_fraction=1.0,
            realized_fraction=0.0, as_of=as_of,
            session_snapshot_id=session_snapshot_id,
            admission_decision_id=admission_decision_id,
            allocation_plan_id=allocation_plan_id,
            lineage_id=lineage.lineage_id if lineage else None)

    # ---------------- risk ----------------

    def risk_estimate(self, thesis: PositionThesis, *,
                      reference_price: Optional[float] = None,
                      daily_volatility: Optional[float] = None
                      ) -> PositionRiskEstimate:
        """
        The 25.9I-D estimate, upgraded by this thesis where it can be.

        With a price-based invalidation and a reference price the
        estimate becomes MEASURED instead of the PARTIAL that volatility
        alone produces. The dependency runs one way -- thesis reads the
        estimator, the estimator knows nothing about theses -- so there
        is no cycle between allocation and thesis code.
        """
        price = reference_price if reference_price is not None else thesis.entry_price
        invalidation = (thesis.invalidation_zone.high
                        if thesis.direction is PositionDirection.LONG
                        else thesis.invalidation_zone.low)
        return self.risk_estimator.estimate(
            thesis.instrument_id, reference_price=price,
            invalidation_price=invalidation, daily_volatility=daily_volatility,
            horizon_days=thesis.horizon.calendar_days)

    # ---------------- reassessment ----------------

    def reassess(self, thesis: PositionThesis, as_of: datetime, *,
                 price: Optional[float] = None,
                 evidence: Sequence[ThesisEvidence] = (),
                 proposed_target: Optional[TargetZone] = None,
                 proposed_invalidation: Optional[InvalidationZone] = None,
                 proposed_horizon: Optional[ExpectedHorizon] = None,
                 exit_fraction: Optional[float] = None,
                 since_last_revision: Optional[timedelta] = None,
                 revisions_today: int = 0) -> ThesisAssessment:
        """
        Judge a thesis against what is now known. Never executes anything.

        The order of checks is the priority order: invalidation first,
        then expiry, then evidence-driven revision, then price proximity,
        then hold. Safety outranks opportunity by construction rather
        than by the caller remembering to check it first.
        """
        as_of = require_utc(as_of, "as_of")
        if not thesis.is_open:
            raise ThesisEngineError(
                f"thesis {thesis.thesis_id} is {thesis.status.value} and cannot "
                f"be reassessed; a new position needs a new thesis")

        evidence = tuple(sorted(evidence, key=lambda e: e.evidence_id))
        negative = [e for e in evidence
                    if e.direction is EvidenceDirection.NEGATIVE]
        positive = [e for e in evidence
                    if e.direction is EvidenceDirection.POSITIVE]
        invalidating = [e for e in evidence if e.invalidates_thesis]

        # ---- 1. invalidation: outranks every cooldown -----------------
        if invalidating:
            return self._exit_assessment(
                thesis, as_of, evidence=evidence,
                reason=ThesisRevisionReason.MATERIAL_NEGATIVE_EVENT,
                cause="the thesis was invalidated by "
                      + ", ".join(e.evidence_id for e in invalidating),
                activation=RealtimeActivationReason.MATERIAL_NEGATIVE_EVENT,
                status=ThesisStatus.INVALIDATED)

        if price is not None and self._breached_invalidation(thesis, price):
            return self._exit_assessment(
                thesis, as_of, evidence=evidence,
                reason=ThesisRevisionReason.RISK_CHANGE,
                cause=f"price {price} reached the invalidation zone",
                activation=RealtimeActivationReason.INVALIDATION_PROXIMITY,
                status=ThesisStatus.INVALIDATED)

        # ---- 2. time decay: a horizon never extends itself ------------
        if thesis.horizon.is_expired_at(as_of):
            supporting = [e for e in positive if e.is_material_information]
            if not supporting:
                return self._expiry_assessment(thesis, as_of, evidence)
            # Material positive evidence exists: an extension is POSSIBLE,
            # and it still has to pass the policy below.

        # ---- 3. evidence-driven revision ------------------------------
        if evidence:
            assessment = self._evidence_assessment(
                thesis, as_of, evidence=evidence, positive=positive,
                negative=negative, proposed_target=proposed_target,
                proposed_invalidation=proposed_invalidation,
                proposed_horizon=proposed_horizon, exit_fraction=exit_fraction,
                since_last_revision=since_last_revision,
                revisions_today=revisions_today)
            if assessment is not None:
                return assessment

        # ---- 4. price proximity: reassess, never extend ---------------
        if price is not None and self._approaching_target(thesis, price):
            return self._hold_assessment(
                thesis, as_of, evidence,
                reasons=(f"price {price} is approaching the target zone "
                         f"{thesis.target_zone.low}-{thesis.target_zone.high}; "
                         f"reassessment requested. Price movement alone is not "
                         f"evidence and cannot extend a target.",),
                required_state=MarketDataState.WARM,
                activation=RealtimeActivationReason.TARGET_PROXIMITY)

        # ---- 5. nothing has changed -----------------------------------
        return self._hold_assessment(
            thesis, as_of, evidence,
            reasons=("no material change; the thesis stands as recorded",),
            required_state=MarketDataState.COLD)

    # ---------------- partial exit ----------------

    def apply_partial_exit(self, thesis: PositionThesis, fraction: float,
                           as_of: datetime, *,
                           reason: ThesisRevisionReason,
                           evidence: Sequence[ThesisEvidence] = (),
                           note: str = "") -> Tuple[PositionThesis,
                                                    PositionThesisRevision]:
        """
        Realize part of the position. Fractions are OF THE ORIGINAL.

        Returns the new thesis and the revision that produced it. The
        predecessor is untouched, so the history of what was held and
        why survives the exit.
        """
        as_of = require_utc(as_of, "as_of")
        if not 0.0 < fraction < 1.0:
            raise ThesisEngineError(
                f"a partial exit fraction must be within (0, 1); {fraction} is "
                f"not. A full exit is FULL_EXIT, not PARTIAL_EXIT(1.0)")
        if fraction > thesis.remaining_fraction + 1e-9:
            raise ThesisEngineError(
                f"cannot realize {fraction} of the original position when only "
                f"{thesis.remaining_fraction} remains")
        if not thesis.is_open:
            raise ThesisEngineError("a closed thesis cannot be exited again")

        remaining = round(thesis.remaining_fraction - fraction, 10)
        realized = round(thesis.realized_fraction + fraction, 10)
        status = (ThesisStatus.CLOSED if remaining <= 1e-9
                  else ThesisStatus.PARTIALLY_EXITED)
        if not transition_allowed(thesis.status, status):
            raise ThesisEngineError(
                f"{thesis.status.value} -> {status.value} is not a legal "
                f"lifecycle transition")

        revised = replace(
            thesis, thesis_id=_thesis_id(thesis.instrument_id, as_of,
                                         thesis.revision + 1),
            revision=thesis.revision + 1, previous_thesis_id=thesis.thesis_id,
            remaining_fraction=max(0.0, remaining), realized_fraction=realized,
            status=status, updated_at=as_of, as_of=as_of,
            current_summary=note or thesis.current_summary,
            evidence_references=thesis.evidence_references
            + tuple(e.evidence_id for e in evidence))
        revision = PositionThesisRevision(
            revision_id=_revision_id(thesis.thesis_id, as_of, reason.value),
            reason=reason, at=as_of, previous=thesis, revised=revised,
            evidence=tuple(e.evidence_id for e in evidence),
            note=note or f"realized {fraction:.0%} of the original position")
        return revised, revision

    # ---------------- internals ----------------

    def _breached_invalidation(self, thesis: PositionThesis,
                               price: float) -> bool:
        if thesis.direction is PositionDirection.LONG:
            return price <= thesis.invalidation_zone.high
        return price >= thesis.invalidation_zone.low

    def _approaching_target(self, thesis: PositionThesis, price: float) -> bool:
        entry = thesis.entry_price
        target = thesis.target_zone.low
        if entry is None or entry == target:
            return thesis.target_zone.contains(price)
        travelled = (price - entry) / (target - entry)
        return travelled >= self.policy.target_approach_fraction

    def _freshness_for(self, action: PositionAction,
                       state: MarketDataState) -> MarketDataRequirement:
        """
        What data an action would need BEFORE it could be executed.

        Size-changing actions are execution-sensitive and need HOT. A
        HOLD on a multi-day thesis does not: §21, and it is also how the
        market-data budget stays survivable.
        """
        if action in (PositionAction.PARTIAL_EXIT, PositionAction.FULL_EXIT):
            return MarketDataRequirement(
                state=MarketDataState.HOT, max_age_seconds=60.0,
                requires_realtime_marker=True, permits_delayed=False,
                purpose=action.value)
        return MarketDataRequirement(
            state=state, permits_delayed=state is not MarketDataState.HOT,
            purpose=action.value)

    def _hold_assessment(self, thesis: PositionThesis, as_of: datetime,
                         evidence: Sequence[ThesisEvidence], *,
                         reasons: Tuple[str, ...],
                         required_state: MarketDataState,
                         activation: Optional[RealtimeActivationReason] = None,
                         blocked: Tuple[str, ...] = ()) -> ThesisAssessment:
        recommendation = PositionActionRecommendation(
            recommendation_id=_revision_id(thesis.thesis_id, as_of, "hold"),
            instrument_id=thesis.instrument_id, action=PositionAction.HOLD,
            at=as_of, thesis_id=thesis.thesis_id, reasons=reasons)
        return ThesisAssessment(
            thesis_id=thesis.thesis_id, as_of=as_of,
            recommendation=recommendation, required_state=required_state,
            freshness=self._freshness_for(PositionAction.HOLD, required_state),
            activation_reason=activation, reasons=reasons,
            blocked_by_policy=blocked)

    def _exit_assessment(self, thesis: PositionThesis, as_of: datetime, *,
                         evidence: Sequence[ThesisEvidence],
                         reason: ThesisRevisionReason, cause: str,
                         activation: RealtimeActivationReason,
                         status: ThesisStatus) -> ThesisAssessment:
        recommendation = PositionActionRecommendation(
            recommendation_id=_revision_id(thesis.thesis_id, as_of, "full_exit"),
            instrument_id=thesis.instrument_id, action=PositionAction.FULL_EXIT,
            at=as_of, thesis_id=thesis.thesis_id, exit_fraction=1.0,
            reasons=(cause,))
        revised = replace(
            thesis, thesis_id=_thesis_id(thesis.instrument_id, as_of,
                                         thesis.revision + 1),
            revision=thesis.revision + 1, previous_thesis_id=thesis.thesis_id,
            status=status, updated_at=as_of, as_of=as_of,
            current_summary=cause,
            evidence_references=thesis.evidence_references
            + tuple(e.evidence_id for e in evidence))
        revision = PositionThesisRevision(
            revision_id=_revision_id(thesis.thesis_id, as_of, reason.value),
            reason=reason, at=as_of, previous=thesis, revised=revised,
            evidence=tuple(e.evidence_id for e in evidence), note=cause)
        return ThesisAssessment(
            thesis_id=thesis.thesis_id, as_of=as_of,
            recommendation=recommendation, required_state=MarketDataState.HOT,
            freshness=self._freshness_for(PositionAction.FULL_EXIT,
                                          MarketDataState.HOT),
            revision=revision, revised_thesis=revised,
            activation_reason=activation, reasons=(cause,),
            capital_release=CapitalReleaseRecommendation(
                instrument_id=thesis.instrument_id,
                released_fraction=thesis.remaining_fraction,
                released_notional=thesis.remaining_notional,
                note="released capital is not reallocated by this phase"))

    def _expiry_assessment(self, thesis: PositionThesis, as_of: datetime,
                           evidence: Sequence[ThesisEvidence]) -> ThesisAssessment:
        cause = ("the expected horizon ended and no material new evidence "
                 "supports extending it; a horizon does not extend itself")
        if self.policy.expiry_action is PositionAction.HOLD:
            return self._hold_assessment(
                thesis, as_of, evidence, reasons=(cause,),
                required_state=MarketDataState.WARM,
                activation=RealtimeActivationReason.PERIODIC_POSITION_VERIFICATION)
        return self._exit_assessment(
            thesis, as_of, evidence=evidence,
            reason=ThesisRevisionReason.TIME_DECAY, cause=cause,
            activation=RealtimeActivationReason.EXIT_CANDIDATE,
            status=ThesisStatus.EXPIRED)

    def _evidence_assessment(self, thesis: PositionThesis, as_of: datetime, *,
                             evidence, positive, negative, proposed_target,
                             proposed_invalidation, proposed_horizon,
                             exit_fraction, since_last_revision, revisions_today
                             ) -> Optional[ThesisAssessment]:
        """A revision driven by evidence, or None when none is justified."""
        wants_extension = proposed_target is not None and (
            proposed_target.is_more_ambitious_than(thesis.target_zone,
                                                   thesis.direction))
        material_positive = [e for e in positive if e.is_material_information
                             and (e.materiality or 0.0)
                             >= self.policy.minimum_extension_materiality]
        material_negative = [e for e in negative if e.is_material_information]

        # Upside revisions are the ones a cooldown restrains (§24).
        if wants_extension:
            if not material_positive:
                return self._hold_assessment(
                    thesis, as_of, evidence,
                    reasons=("a target extension needs material new evidence; "
                             "none of the supplied evidence carries measured "
                             "materiality above the threshold",),
                    required_state=MarketDataState.COLD,
                    blocked=("extension refused: no material evidence",))
            permitted, why = self.policy.revision_policy.permits(
                since_last_revision=since_last_revision,
                materiality=max(e.materiality for e in material_positive),
                confidence=max((e.confidence for e in material_positive
                                if e.confidence is not None), default=None),
                revisions_today=revisions_today, has_reason=True)
            if not permitted:
                return self._hold_assessment(
                    thesis, as_of, evidence,
                    reasons=(f"target extension withheld: {why}",),
                    required_state=MarketDataState.COLD,
                    blocked=(why,))

        if proposed_target is None and proposed_invalidation is None \
                and proposed_horizon is None and exit_fraction is None:
            if material_negative:
                return self._hold_assessment(
                    thesis, as_of, evidence,
                    reasons=("material negative evidence recorded; no change "
                             "was proposed, so the thesis stands and a "
                             "reassessment is requested",),
                    required_state=MarketDataState.WARM,
                    activation=RealtimeActivationReason.MATERIAL_NEGATIVE_EVENT)
            return None

        reason = (ThesisRevisionReason.MATERIAL_POSITIVE_EVENT
                  if wants_extension or (material_positive and not material_negative)
                  else ThesisRevisionReason.MATERIAL_NEGATIVE_EVENT
                  if material_negative else ThesisRevisionReason.MODEL_REASSESSMENT)

        target = proposed_target or thesis.target_zone
        invalidation = proposed_invalidation or thesis.invalidation_zone
        horizon = proposed_horizon or thesis.horizon
        remaining, realized = thesis.remaining_fraction, thesis.realized_fraction
        status = thesis.status
        action = PositionAction.HOLD
        exit_size: Optional[float] = None
        capital_release = None

        if exit_fraction is not None:
            if not 0.0 < exit_fraction < 1.0:
                raise ThesisEngineError(
                    "an exit fraction must be within (0, 1); a full exit is "
                    "FULL_EXIT")
            exit_size = exit_fraction
            remaining = round(thesis.remaining_fraction - exit_fraction, 10)
            realized = round(thesis.realized_fraction + exit_fraction, 10)
            if remaining < -1e-9:
                raise ThesisEngineError(
                    "the proposed exit exceeds the remaining position")
            remaining = max(0.0, remaining)
            status = (ThesisStatus.CLOSED if remaining <= 1e-9
                      else ThesisStatus.PARTIALLY_EXITED)
            action = PositionAction.PARTIAL_EXIT
            capital_release = CapitalReleaseRecommendation(
                instrument_id=thesis.instrument_id,
                released_fraction=exit_fraction,
                released_notional=(round(thesis.entry_notional * exit_fraction, 6)
                                   if thesis.entry_notional is not None else None),
                note="released capital is not reallocated by this phase")
        elif wants_extension:
            action = PositionAction.EXTEND_TARGET
        elif proposed_target is not None:
            action = PositionAction.REDUCE_TARGET
        elif proposed_invalidation is not None:
            action = PositionAction.TIGHTEN_STOP

        revised = replace(
            thesis, thesis_id=_thesis_id(thesis.instrument_id, as_of,
                                         thesis.revision + 1),
            revision=thesis.revision + 1, previous_thesis_id=thesis.thesis_id,
            target_zone=target, invalidation_zone=invalidation, horizon=horizon,
            remaining_fraction=remaining, realized_fraction=realized,
            status=status, updated_at=as_of, as_of=as_of,
            evidence_references=thesis.evidence_references
            + tuple(e.evidence_id for e in evidence))

        # The 25.9I-A rules run again here at construction: a target
        # raised on a non-evidence reason, or raised while protection is
        # loosened, raises rather than being quietly accepted.
        revision = PositionThesisRevision(
            revision_id=_revision_id(thesis.thesis_id, as_of, reason.value),
            reason=reason, at=as_of, previous=thesis, revised=revised,
            evidence=tuple(e.evidence_id for e in evidence))

        required_state = (MarketDataState.HOT
                          if action in (PositionAction.PARTIAL_EXIT,
                                        PositionAction.FULL_EXIT)
                          else MarketDataState.WARM)
        activation = (RealtimeActivationReason.EXIT_CANDIDATE
                      if action is PositionAction.PARTIAL_EXIT
                      else RealtimeActivationReason.MATERIAL_POSITIVE_EVENT
                      if reason is ThesisRevisionReason.MATERIAL_POSITIVE_EVENT
                      else RealtimeActivationReason.MATERIAL_NEGATIVE_EVENT)

        recommendation = PositionActionRecommendation(
            recommendation_id=_revision_id(thesis.thesis_id, as_of, action.value),
            instrument_id=thesis.instrument_id, action=action, at=as_of,
            thesis_id=thesis.thesis_id, exit_fraction=exit_size,
            revised_target_zone=(target if action in (
                PositionAction.EXTEND_TARGET, PositionAction.REDUCE_TARGET)
                else None),
            revised_invalidation_zone=(invalidation
                                       if action is PositionAction.TIGHTEN_STOP
                                       else None),
            reasons=tuple(e.evidence_id for e in evidence))
        return ThesisAssessment(
            thesis_id=thesis.thesis_id, as_of=as_of,
            recommendation=recommendation, required_state=required_state,
            freshness=self._freshness_for(action, required_state),
            revision=revision, revised_thesis=revised,
            activation_reason=activation, capital_release=capital_release,
            reasons=tuple(f"{e.evidence_id}: {e.summary}" for e in evidence))
