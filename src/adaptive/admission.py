"""
src/adaptive/admission.py
--------------------------------------
Who gets a session slot, and who is merely interesting (25.9I-B).

ADMISSION IS NOT SCORING
----------------------------
A score says how much attention an instrument might deserve. Admission
decides whether it gets any, and the two are separated because the
question "why is this in today's session" has to have an answer that is
not "it had a big number". Thresholds, data completeness, capacity and
concentration all sit in between, and each of them records its reason.

CAPACITY IS A CEILING
-------------------------
31 is the maximum, not the target. If eight candidates clear the bar,
eight are selected and twenty-three slots stay empty. There is no
fallback that lowers a threshold to fill space, and a test asserts that
lowering the bar is the only way to get more names -- which is a
configuration change an operator makes deliberately, not something the
selector does on its own.

The one thing that is NOT allowed is exceeding the ceiling. When more
candidates pass than there is room for, the strongest are selected and
the rest are recorded as a WAITLIST with the reason "passed quality,
lost the capacity competition" -- distinct from rejection, because they
were not rejected.

CONCENTRATION LIMITS ONLY EVER SUBTRACT
-------------------------------------------
A sector cap can stop technology taking twenty slots. Nothing here can
add a healthcare name to balance the picture: if healthcare has no
candidate above the threshold, healthcare contributes nothing and the
slots stay empty. Forcing inclusion for visual balance is how a weak
instrument ends up in a portfolio wearing a diversification excuse.

INTRADAY ADMISSION TOUCHES NO SUBSCRIPTION
----------------------------------------------
A candidate admitted at 14:05 is marked
`requires_market_data_activation`. This phase does not open, change or
price a market-data subscription; 25.9I-C owns that. Until then the
mark is the honest statement that the system has decided it wants data
it does not yet have.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.adaptive.universe import ParentUniverseMember
from src.domain.adaptive_models import (
    AdmissionOutcome, AdmissionVerdict, DecisionLineage, EligibilityDecision,
    EligibilityStatus, OpportunityAdmissionDecision, OpportunityScore,
    OpportunityStatus, OpportunityValidity, SessionUniverse, require_utc,
)

#: The capacity the capture pilot validated. A default, not a law: the
#: whole point of this phase is that the number is a ceiling.
DEFAULT_MAX_CAPACITY = 31


@dataclass(frozen=True)
class AdmissionConfig:
    """Thresholds and limits, versioned so a decision can be replayed."""
    version: str = "admission-v1"
    max_capacity: int = DEFAULT_MAX_CAPACITY
    pass_threshold: float = 0.55
    watch_threshold: float = 0.40
    min_completeness: float = 0.40
    max_per_sector: Optional[int] = 6
    max_sector_fraction: Optional[float] = None
    #: TTL for an admitted opportunity. None means "until session end".
    validity: Optional[timedelta] = None

    def __post_init__(self):
        if self.max_capacity < 0:
            raise ValueError("max_capacity must be >= 0")
        if not 0.0 <= self.watch_threshold <= self.pass_threshold <= 1.0:
            raise ValueError(
                "require 0 <= watch_threshold <= pass_threshold <= 1")
        if not 0.0 <= self.min_completeness <= 1.0:
            raise ValueError("min_completeness must be between 0 and 1")
        if self.max_per_sector is not None and self.max_per_sector < 0:
            raise ValueError("max_per_sector must be >= 0")
        if self.max_sector_fraction is not None and not (
                0.0 < self.max_sector_fraction <= 1.0):
            raise ValueError("max_sector_fraction must be within (0, 1]")

    def sector_limit(self) -> Optional[int]:
        """The tighter of the two sector limits, in slots."""
        limits = []
        if self.max_per_sector is not None:
            limits.append(self.max_per_sector)
        if self.max_sector_fraction is not None:
            limits.append(max(1, int(self.max_capacity * self.max_sector_fraction)))
        return min(limits) if limits else None

    def describe(self) -> Dict[str, Any]:
        return {"version": self.version, "max_capacity": self.max_capacity,
                "pass_threshold": self.pass_threshold,
                "watch_threshold": self.watch_threshold,
                "min_completeness": self.min_completeness,
                "max_per_sector": self.max_per_sector,
                "max_sector_fraction": self.max_sector_fraction,
                "sector_limit_slots": self.sector_limit()}


@dataclass(frozen=True)
class ChurnPolicy:
    """
    Limits on swapping a held slot for a marginally better candidate.

    Inactive by default: every threshold is zero, so 25.9I-B changes
    nothing about churn until an operator sets values. The defaults are
    not calibrated, because there is no session history of dynamic
    selection to calibrate them against yet.
    """
    replacement_margin: float = 0.0
    min_residency: timedelta = timedelta(0)
    cooldown: timedelta = timedelta(0)

    def __post_init__(self):
        if self.replacement_margin < 0:
            raise ValueError("replacement_margin must be >= 0")
        for name in ("min_residency", "cooldown"):
            if getattr(self, name).total_seconds() < 0:
                raise ValueError(f"{name} must not be negative")

    def permits_replacement(self, *, incumbent_score: float,
                            challenger_score: float,
                            incumbent_held_for: Optional[timedelta] = None,
                            evicted_recently: bool = False) -> Tuple[bool, str]:
        if challenger_score <= incumbent_score + self.replacement_margin:
            return False, (f"challenger does not beat the incumbent by the "
                           f"required margin of {self.replacement_margin}")
        if self.min_residency.total_seconds() > 0:
            if incumbent_held_for is None:
                return False, "how long the incumbent has been held is unknown"
            if incumbent_held_for < self.min_residency:
                return False, "incumbent has not served its minimum residency"
        if evicted_recently and self.cooldown.total_seconds() > 0:
            return False, "candidate was evicted within the cooldown window"
        return True, ""


@dataclass(frozen=True)
class CandidateAssessment:
    """
    One instrument, judged end to end, with everything behind it kept.

    This is the record a later phase reads to answer "why". It carries
    the eligibility decision, the score with its components, the verdict
    and the reasons -- not a summary of them.
    """
    instrument_id: str
    ticker: str
    sector_id: str
    as_of: datetime
    eligibility: EligibilityDecision
    verdict: AdmissionVerdict
    score: Optional[OpportunityScore] = None
    reasons: Tuple[str, ...] = ()
    validity: Optional[OpportunityValidity] = None
    requires_market_data_activation: bool = False
    admitted_at: Optional[datetime] = None

    def __post_init__(self):
        object.__setattr__(self, "as_of", require_utc(self.as_of, "as_of"))
        object.__setattr__(self, "admitted_at",
                           require_utc(self.admitted_at, "admitted_at"))
        if self.verdict is not AdmissionVerdict.PASS and not self.reasons:
            raise ValueError(
                f"a {self.verdict.value} assessment must carry reasons")

    @property
    def overall(self) -> float:
        return self.score.overall if self.score else 0.0

    @property
    def status(self) -> OpportunityStatus:
        """The I-A lifecycle status this verdict corresponds to."""
        if self.verdict is AdmissionVerdict.PASS:
            return OpportunityStatus.QUALIFIED
        if self.verdict is AdmissionVerdict.REJECT:
            return OpportunityStatus.REJECTED
        return OpportunityStatus.PROPOSED

    def is_expired_at(self, moment: datetime) -> bool:
        return bool(self.validity and self.validity.is_expired_at(moment))


@dataclass(frozen=True)
class SessionSelection:
    """
    One revision of a session universe, with everything it decided.

    Revisions are append-only: `revise()` returns a NEW selection whose
    `previous_snapshot_id` names the one it replaced. Nothing overwrites
    a prior revision, so "what did we think at 09:20" stays answerable
    after 14:05.
    """
    snapshot_id: str
    session_date: str
    as_of: datetime
    revision: int
    universe: SessionUniverse
    assessments: Tuple[CandidateAssessment, ...]
    admissions: Tuple[OpportunityAdmissionDecision, ...]
    waitlist: Tuple[str, ...] = ()
    benchmarks: Tuple[str, ...] = ()
    parent_universe_version: str = ""
    parent_universe_sha256: str = ""
    eligibility_config_version: str = ""
    scoring_config_version: str = ""
    admission_config_version: str = ""
    lineage: Optional[DecisionLineage] = None
    previous_snapshot_id: Optional[str] = None

    def __post_init__(self):
        object.__setattr__(self, "as_of", require_utc(self.as_of, "as_of"))
        if self.revision < 0:
            raise ValueError("revision must be >= 0")
        if self.revision > 0 and not self.previous_snapshot_id:
            raise ValueError("a revision must name the snapshot it followed")

    # ---------------- reading ----------------

    @property
    def selected(self) -> Tuple[str, ...]:
        return self.universe.members

    @property
    def unused_capacity(self) -> int:
        return self.universe.unused_capacity

    def verdict_counts(self) -> Dict[str, int]:
        counts = {verdict.value: 0 for verdict in AdmissionVerdict}
        for assessment in self.assessments:
            counts[assessment.verdict.value] += 1
        return counts

    def eligibility_counts(self) -> Dict[str, int]:
        counts = {status.value: 0 for status in EligibilityStatus}
        for assessment in self.assessments:
            counts[assessment.eligibility.status.value] += 1
        return counts

    def sector_distribution(self) -> Dict[str, int]:
        selected = set(self.universe.members)
        distribution: Dict[str, int] = {}
        for assessment in self.assessments:
            if assessment.instrument_id in selected:
                distribution[assessment.sector_id] = (
                    distribution.get(assessment.sector_id, 0) + 1)
        return dict(sorted(distribution.items()))

    def assessment(self, instrument_id: str) -> Optional[CandidateAssessment]:
        for assessment in self.assessments:
            if assessment.instrument_id == instrument_id:
                return assessment
        return None

    def content_hash(self) -> str:
        """
        Deterministic over what was decided, not when it was computed.

        `built_at` is deliberately absent: two runs of the same inputs
        must hash identically, which is what makes the reproducibility
        test meaningful.
        """
        payload = "|".join([
            self.session_date, self.as_of.isoformat(), str(self.revision),
            str(self.universe.max_capacity), ",".join(self.universe.members),
            self.parent_universe_sha256, self.eligibility_config_version,
            self.scoring_config_version, self.admission_config_version])
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def describe(self) -> Dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id, "session_date": self.session_date,
            "as_of": self.as_of.isoformat(), "revision": self.revision,
            "max_capacity": self.universe.max_capacity,
            "selected": list(self.universe.members),
            "selected_count": self.universe.occupied,
            "unused_capacity": self.unused_capacity,
            "waitlist": list(self.waitlist),
            "benchmarks": list(self.benchmarks),
            "verdicts": self.verdict_counts(),
            "eligibility": self.eligibility_counts(),
            "sector_distribution": self.sector_distribution(),
            "content_hash": self.content_hash()[:16],
            "previous_snapshot_id": self.previous_snapshot_id,
        }


class OpportunityAdmitter:
    """
    Turns eligibility plus score into a verdict, then fills capacity.

    Deterministic and side-effect free: no clock, no database, no
    network. Everything it needs is passed in, and the same inputs give
    the same selection.
    """

    def __init__(self, config: Optional[AdmissionConfig] = None,
                 churn: Optional[ChurnPolicy] = None):
        self.config = config or AdmissionConfig()
        self.churn = churn or ChurnPolicy()

    # ---------------- per-candidate verdict ----------------

    def judge(self, member: ParentUniverseMember, eligibility: EligibilityDecision,
              score: Optional[OpportunityScore], as_of: datetime, *,
              session_close: Optional[datetime] = None) -> CandidateAssessment:
        as_of = require_utc(as_of, "as_of")

        if eligibility.status is EligibilityStatus.INSUFFICIENT_DATA:
            return CandidateAssessment(
                instrument_id=member.instrument_id, ticker=member.ticker,
                sector_id=member.sector_id, as_of=as_of, eligibility=eligibility,
                verdict=AdmissionVerdict.INSUFFICIENT_DATA, score=score,
                reasons=eligibility.reasons)
        if eligibility.status is EligibilityStatus.INELIGIBLE:
            return CandidateAssessment(
                instrument_id=member.instrument_id, ticker=member.ticker,
                sector_id=member.sector_id, as_of=as_of, eligibility=eligibility,
                verdict=AdmissionVerdict.REJECT, score=score,
                reasons=eligibility.reasons)
        if score is None:
            return CandidateAssessment(
                instrument_id=member.instrument_id, ticker=member.ticker,
                sector_id=member.sector_id, as_of=as_of, eligibility=eligibility,
                verdict=AdmissionVerdict.INSUFFICIENT_DATA,
                reasons=("no score could be computed",))

        if score.completeness < self.config.min_completeness:
            return CandidateAssessment(
                instrument_id=member.instrument_id, ticker=member.ticker,
                sector_id=member.sector_id, as_of=as_of, eligibility=eligibility,
                verdict=AdmissionVerdict.INSUFFICIENT_DATA, score=score,
                reasons=(f"score completeness {score.completeness:.2f} is below "
                         f"{self.config.min_completeness:.2f}; missing: "
                         + ", ".join(score.unavailable),))

        if score.overall >= self.config.pass_threshold:
            validity = OpportunityValidity(
                as_of=as_of,
                expires_at=session_close,
                max_age=self.config.validity)
            return CandidateAssessment(
                instrument_id=member.instrument_id, ticker=member.ticker,
                sector_id=member.sector_id, as_of=as_of, eligibility=eligibility,
                verdict=AdmissionVerdict.PASS, score=score, validity=validity)
        if score.overall >= self.config.watch_threshold:
            return CandidateAssessment(
                instrument_id=member.instrument_id, ticker=member.ticker,
                sector_id=member.sector_id, as_of=as_of, eligibility=eligibility,
                verdict=AdmissionVerdict.WATCH, score=score,
                reasons=(f"score {score.overall:.3f} is below the pass threshold "
                         f"of {self.config.pass_threshold}",))
        return CandidateAssessment(
            instrument_id=member.instrument_id, ticker=member.ticker,
            sector_id=member.sector_id, as_of=as_of, eligibility=eligibility,
            verdict=AdmissionVerdict.REJECT, score=score,
            reasons=(f"score {score.overall:.3f} is below the watch threshold "
                     f"of {self.config.watch_threshold}",))

    # ---------------- capacity ----------------

    def fill_capacity(self, assessments: Sequence[CandidateAssessment],
                      as_of: datetime, *, capacity: Optional[int] = None,
                      already_held: Sequence[str] = ()
                      ) -> Tuple[List[str], List[str],
                                 List[OpportunityAdmissionDecision]]:
        """
        Select from PASS candidates only. Returns (selected, waitlist, decisions).

        Ranking is by score descending, then instrument id, so ties break
        deterministically rather than by dictionary order.
        """
        as_of = require_utc(as_of, "as_of")
        limit = self.config.max_capacity if capacity is None else capacity
        sector_limit = self.config.sector_limit()

        passed = sorted((a for a in assessments
                         if a.verdict is AdmissionVerdict.PASS),
                        key=lambda a: (-a.overall, a.instrument_id))
        selected: List[str] = list(already_held)
        per_sector: Dict[str, int] = {}
        for instrument_id in already_held:
            held = next((a for a in assessments
                         if a.instrument_id == instrument_id), None)
            if held:
                per_sector[held.sector_id] = per_sector.get(held.sector_id, 0) + 1

        waitlist: List[str] = []
        decisions: List[OpportunityAdmissionDecision] = []
        for assessment in passed:
            if assessment.instrument_id in selected:
                continue
            occupied = len(selected)
            if occupied >= limit:
                waitlist.append(assessment.instrument_id)
                decisions.append(self._decision(
                    assessment, AdmissionOutcome.CAPACITY_REACHED, as_of, limit,
                    occupied,
                    "passed quality, lost the capacity competition"))
                continue
            if (sector_limit is not None
                    and per_sector.get(assessment.sector_id, 0) >= sector_limit):
                waitlist.append(assessment.instrument_id)
                decisions.append(self._decision(
                    assessment, AdmissionOutcome.DECLINED, as_of, limit, occupied,
                    f"sector {assessment.sector_id} already holds its limit of "
                    f"{sector_limit}"))
                continue
            selected.append(assessment.instrument_id)
            per_sector[assessment.sector_id] = (
                per_sector.get(assessment.sector_id, 0) + 1)
            decisions.append(self._decision(
                assessment, AdmissionOutcome.ADMITTED, as_of, limit, occupied,
                "passed admission with capacity available"))

        # Deterministic membership order, independent of arrival order.
        return sorted(selected), waitlist, decisions

    def _decision(self, assessment: CandidateAssessment,
                  outcome: AdmissionOutcome, as_of: datetime, capacity: int,
                  occupied: int, reason: str) -> OpportunityAdmissionDecision:
        decision_id = "adm-" + hashlib.sha256(
            f"{assessment.instrument_id}|{as_of.isoformat()}|{outcome.value}"
            .encode("utf-8")).hexdigest()[:16]
        return OpportunityAdmissionDecision(
            decision_id=decision_id,
            assessment_id=assessment.instrument_id + "@" + as_of.isoformat(),
            instrument_id=assessment.instrument_id, outcome=outcome,
            decided_at=as_of, reasons=(reason,),
            capacity_at_decision=capacity, occupied_at_decision=occupied)


# ======================================================================
# Expiry
# ======================================================================


class ExpiryReason(str):
    """Free-form but recorded; every expiry says which rule ended it."""


def expired_assessments(selection: SessionSelection, moment: datetime
                        ) -> List[Tuple[str, str]]:
    """
    Which selected instruments are no longer valid at `moment`, and why.

    Only evidence-supported rules: the TTL and the session-end expiry
    that were recorded when the opportunity was admitted. Price-based and
    thesis-based invalidation belong to 25.9I-E, where a thesis exists.
    """
    moment = require_utc(moment, "moment")
    expired = []
    for instrument_id in selection.universe.members:
        assessment = selection.assessment(instrument_id)
        if assessment is None or assessment.validity is None:
            continue
        if assessment.validity.is_expired_at(moment):
            deadline = assessment.validity.deadline()
            expired.append((instrument_id,
                            f"validity ended at {deadline.isoformat()}"
                            if deadline else "validity ended"))
    return expired
