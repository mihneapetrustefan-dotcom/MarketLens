"""
src/domain/thesis_models.py
----------------------------------------
Position thesis and dynamic-exit contracts (Phase 25.9I-A).

NOTHING HERE TOUCHES A POSITION
-----------------------------------
Types and validation only. No thesis is written for a real position, no
exit is recommended, and no order is modified in this phase. What this
module fixes is the vocabulary a later phase will need, and the three
rules that are easy to state and easy to violate once code is under
pressure.

RULE 1: A THESIS MAY CHANGE; PRICE ALONE MAY NOT CHANGE IT
--------------------------------------------------------------
New information can lengthen a horizon, raise a target or invalidate an
idea. The move that must never be automatic is "the price went up, so
the target goes up" — that is not a reassessment, it is a position
following its own P&L. `PositionThesisRevision` therefore refuses to
extend a target on `TARGET_APPROACH` alone: extension requires a reason
that carries evidence from outside the price path.

RULE 2: TARGET AND STOP ARE SEPARATE
----------------------------------------
Raising the expected target must not loosen downside protection. A
revision that does both at once is refused by construction, because
that pairing is how a position quietly becomes a bet with no floor.

RULE 3: EVERY REVISION KEEPS ITS PREDECESSOR
------------------------------------------------
Revisions are immutable records carrying `revision` numbers and the
previous thesis's id. The history is the point: without it, "we changed
our mind" and "we were wrong" are indistinguishable afterwards.

WHAT IS REUSED RATHER THAN REDEFINED
----------------------------------------
Exit sizing, order construction and risk approval remain
`portfolio_models` and the execution path (Phase 11 / 14). A
`PositionActionRecommendation` here is a RECOMMENDATION: it carries no
venue, no account, no order id and no executable flag, and a later
phase must still take it through Portfolio, Risk and Order Intent. The
flow INTELLIGENCE -> PORTFOLIO -> RISK -> INTENT -> EXECUTION is not
short-circuited by anything in this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, Optional, Tuple

from src.domain.adaptive_models import require_utc

# ======================================================================
# Shared validation
# ======================================================================


def _positive(value: float, name: str) -> float:
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError(f"{name} must be finite (got {value!r})")
    if number <= 0:
        raise ValueError(f"{name} must be > 0 (got {number})")
    return number


class PositionDirection(str, Enum):
    LONG = "long"
    SHORT = "short"


# ======================================================================
# Zones and horizon
# ======================================================================


@dataclass(frozen=True)
class TargetZone:
    """
    Where the idea is expected to work out: a range, not a single price.

    A single exact target implies a precision no model here has. A zone
    also makes partial exits expressible without inventing a second
    number at the moment of selling.
    """
    low: float
    high: float
    confidence: Optional[float] = None
    rationale: str = ""

    def __post_init__(self):
        _positive(self.low, "low")
        _positive(self.high, "high")
        if self.high < self.low:
            raise ValueError(f"target zone high ({self.high}) is below low ({self.low})")
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")

    @property
    def midpoint(self) -> float:
        return round((self.low + self.high) / 2.0, 10)

    def contains(self, price: float) -> bool:
        return self.low <= price <= self.high

    def is_more_ambitious_than(self, other: "TargetZone",
                               direction: PositionDirection) -> bool:
        """
        True when this zone asks the market for more than `other` did.

        For a long that means a higher midpoint; for a short, a lower
        one. Used to decide whether a revision counts as an extension,
        which is the case that needs evidence.
        """
        if direction is PositionDirection.LONG:
            return self.midpoint > other.midpoint
        return self.midpoint < other.midpoint


@dataclass(frozen=True)
class InvalidationZone:
    """
    Where the idea is wrong. Not a stop order: a statement about the thesis.

    Kept separate from the target so that the two can never be edited as
    one gesture, which is what Rule 2 exists to prevent.
    """
    low: float
    high: float
    rationale: str = ""

    def __post_init__(self):
        _positive(self.low, "low")
        _positive(self.high, "high")
        if self.high < self.low:
            raise ValueError(
                f"invalidation zone high ({self.high}) is below low ({self.low})")

    @property
    def midpoint(self) -> float:
        return round((self.low + self.high) / 2.0, 10)

    def is_looser_than(self, other: "InvalidationZone",
                       direction: PositionDirection) -> bool:
        """
        True when this zone tolerates more adverse movement than `other`.

        Long positions are invalidated by falling prices, so a lower
        zone is looser; shorts are the mirror image.
        """
        if direction is PositionDirection.LONG:
            return self.midpoint < other.midpoint
        return self.midpoint > other.midpoint


@dataclass(frozen=True)
class ExpectedHorizon:
    """
    How long the idea needs, and when that expectation was formed.

    An unreached target does not extend a horizon by itself (§10); a
    later phase must justify an extension with a revision reason that
    carries evidence.
    """
    trading_days: Optional[int] = None
    calendar_days: Optional[int] = None
    expires_at: Optional[datetime] = None
    set_at: Optional[datetime] = None

    def __post_init__(self):
        for name in ("trading_days", "calendar_days"):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ValueError(f"{name} must be > 0")
        object.__setattr__(self, "expires_at", require_utc(self.expires_at, "expires_at"))
        object.__setattr__(self, "set_at", require_utc(self.set_at, "set_at"))
        if self.expires_at and self.set_at and self.expires_at < self.set_at:
            raise ValueError("expires_at must not precede set_at")

    def is_expired_at(self, moment: datetime) -> bool:
        moment = require_utc(moment, "moment")
        if self.expires_at is not None:
            return moment > self.expires_at
        if self.set_at is not None and self.calendar_days is not None:
            return moment > self.set_at + timedelta(days=self.calendar_days)
        return False


# ======================================================================
# The thesis and its revisions
# ======================================================================


class ThesisRevisionReason(str, Enum):
    MATERIAL_POSITIVE_EVENT = "material_positive_event"
    MATERIAL_NEGATIVE_EVENT = "material_negative_event"
    MODEL_REASSESSMENT = "model_reassessment"
    REGIME_CHANGE = "regime_change"
    VOLATILITY_CHANGE = "volatility_change"
    TIME_DECAY = "time_decay"
    TARGET_APPROACH = "target_approach"
    RISK_CHANGE = "risk_change"


#: Reasons that carry evidence from outside the position's own price
#: path. Only these may justify asking the market for more (Rule 1).
EVIDENCE_BEARING_REASONS = frozenset({
    ThesisRevisionReason.MATERIAL_POSITIVE_EVENT,
    ThesisRevisionReason.MATERIAL_NEGATIVE_EVENT,
    ThesisRevisionReason.MODEL_REASSESSMENT,
    ThesisRevisionReason.REGIME_CHANGE,
    ThesisRevisionReason.VOLATILITY_CHANGE,
    ThesisRevisionReason.RISK_CHANGE,
})


class ThesisStatus(str, Enum):
    """
    Where a thesis is in its life (25.9I-E).

    PARTIALLY_EXITED is still an ACTIVE thesis: the remainder is held
    for the same stated reason, and treating it as finished is how a
    system forgets why it is still holding 60% of something.

    INVALIDATED is not a synonym for "lost money". It means the reason
    for holding stopped being true, which can happen at a profit.
    EXPIRED means the time-based thesis ran out without new evidence,
    which is also not a loss.
    """
    DRAFT = "draft"
    ACTIVE = "active"
    PARTIALLY_EXITED = "partially_exited"
    EXIT_RECOMMENDED = "exit_recommended"
    INVALIDATED = "invalidated"
    EXPIRED = "expired"
    CLOSED = "closed"


#: Legal moves. CLOSED is terminal: re-entering the same instrument is a
#: NEW thesis with a new id, because a reopened thesis would make
#: "what did we believe, and when" unanswerable.
LEGAL_THESIS_TRANSITIONS: Dict[ThesisStatus, Tuple[ThesisStatus, ...]] = {
    ThesisStatus.DRAFT: (ThesisStatus.ACTIVE, ThesisStatus.CLOSED),
    ThesisStatus.ACTIVE: (ThesisStatus.PARTIALLY_EXITED,
                          ThesisStatus.EXIT_RECOMMENDED,
                          ThesisStatus.INVALIDATED, ThesisStatus.EXPIRED,
                          ThesisStatus.CLOSED),
    ThesisStatus.PARTIALLY_EXITED: (ThesisStatus.PARTIALLY_EXITED,
                                    ThesisStatus.EXIT_RECOMMENDED,
                                    ThesisStatus.INVALIDATED,
                                    ThesisStatus.EXPIRED, ThesisStatus.CLOSED),
    ThesisStatus.EXIT_RECOMMENDED: (ThesisStatus.ACTIVE,
                                    ThesisStatus.PARTIALLY_EXITED,
                                    ThesisStatus.INVALIDATED,
                                    ThesisStatus.EXPIRED, ThesisStatus.CLOSED),
    ThesisStatus.INVALIDATED: (ThesisStatus.CLOSED,),
    ThesisStatus.EXPIRED: (ThesisStatus.CLOSED, ThesisStatus.ACTIVE),
    ThesisStatus.CLOSED: (),
}


def transition_allowed(current: ThesisStatus, new: ThesisStatus) -> bool:
    """Whether one lifecycle move is legal. Unknown states are not."""
    return new in LEGAL_THESIS_TRANSITIONS[current]


class EvidenceDirection(str, Enum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"


@dataclass(frozen=True)
class ThesisEvidence:
    """
    New information offered to a thesis, with its strength stated.

    `materiality` separates news from noise, and the caller supplies it
    rather than this module inferring it: 25.9I-F owns event
    materiality, and inventing a number now would bake a guess into the
    contract. Evidence whose materiality was never measured cannot clear
    a materiality threshold, because unmeasured is not strong.
    """
    evidence_id: str
    direction: EvidenceDirection
    as_of: datetime
    summary: str = ""
    materiality: Optional[float] = None
    confidence: Optional[float] = None
    source: str = ""
    invalidates_thesis: bool = False
    references: Tuple[str, ...] = ()

    def __post_init__(self):
        if not self.evidence_id:
            raise ValueError("evidence_id is required")
        object.__setattr__(self, "as_of", require_utc(self.as_of, "as_of"))
        for name in ("materiality", "confidence"):
            value = getattr(self, name)
            if value is not None and not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")

    @property
    def is_material_information(self) -> bool:
        """True only when materiality was actually measured and is above zero."""
        return self.materiality is not None and self.materiality > 0


@dataclass(frozen=True)
class PositionThesis:
    """
    Why a position is held, in a form that can be checked later.

    `revision` starts at 1 and increases; each revision is a new record
    rather than an edit, so the history of what was believed, and when,
    survives the position.

    FRACTION CONVENTION (25.9I-E), stated once and applied everywhere:
    `remaining_fraction` and `realized_fraction` are fractions OF THE
    ORIGINAL position and always sum to 1.0. An exit of "40%" means 40%
    of the original, never 40% of what is left. Two conventions in one
    codebase eventually meet in one expression and quietly produce the
    wrong size.
    """
    thesis_id: str
    instrument_id: str
    direction: PositionDirection
    created_at: datetime
    target_zone: TargetZone
    invalidation_zone: InvalidationZone
    horizon: ExpectedHorizon
    position_id: Optional[str] = None
    trade_id: Optional[str] = None
    entry_price: Optional[float] = None
    confidence: Optional[float] = None
    expected_return: Optional[float] = None
    expected_downside: Optional[float] = None
    risk_assumptions: Tuple[str, ...] = ()
    event_assumptions: Tuple[str, ...] = ()
    revision: int = 1
    previous_thesis_id: Optional[str] = None
    updated_at: Optional[datetime] = None
    lineage_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    #: 25.9I-E additions. Every one has a default, so constructions
    #: written against the 25.9I-A shape stay valid.
    status: ThesisStatus = ThesisStatus.ACTIVE
    remaining_fraction: float = 1.0
    realized_fraction: float = 0.0
    entry_notional: Optional[float] = None
    initial_summary: str = ""
    current_summary: str = ""
    regime_assumptions: Tuple[str, ...] = ()
    evidence_references: Tuple[str, ...] = ()
    initial_horizon: Optional[ExpectedHorizon] = None
    initial_confidence: Optional[float] = None
    #: Lineage back through 25.9I-D and 25.9I-B.
    session_snapshot_id: Optional[str] = None
    admission_decision_id: Optional[str] = None
    allocation_plan_id: Optional[str] = None
    as_of: Optional[datetime] = None

    def __post_init__(self):
        if not self.thesis_id:
            raise ValueError("thesis_id is required")
        if not self.instrument_id:
            raise ValueError("instrument_id is required")
        object.__setattr__(self, "created_at", require_utc(self.created_at, "created_at"))
        object.__setattr__(self, "updated_at", require_utc(self.updated_at, "updated_at"))
        if self.entry_price is not None:
            _positive(self.entry_price, "entry_price")
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        if self.expected_downside is not None and self.expected_downside < 0:
            raise ValueError("expected_downside is a magnitude and must be >= 0")
        if self.revision < 1:
            raise ValueError("revision starts at 1")
        if self.revision > 1 and not self.previous_thesis_id:
            raise ValueError("a revised thesis must name the thesis it replaced")
        object.__setattr__(self, "as_of", require_utc(self.as_of, "as_of"))
        for name in ("remaining_fraction", "realized_fraction"):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1 (got {value})")
        if abs(self.remaining_fraction + self.realized_fraction - 1.0) > 1e-9:
            raise ValueError(
                f"remaining ({self.remaining_fraction}) and realized "
                f"({self.realized_fraction}) fractions must sum to 1.0")
        if self.entry_notional is not None and self.entry_notional < 0:
            raise ValueError("entry_notional must be >= 0")
        if (self.initial_confidence is not None
                and not 0.0 <= self.initial_confidence <= 1.0):
            raise ValueError("initial_confidence must be between 0 and 1")
        if self.status is ThesisStatus.CLOSED and self.remaining_fraction > 0:
            raise ValueError("a CLOSED thesis cannot still hold exposure")
        if (self.status is ThesisStatus.PARTIALLY_EXITED
                and self.realized_fraction <= 0):
            raise ValueError(
                "a PARTIALLY_EXITED thesis must have realized something")
        self._check_zones()

    @property
    def is_open(self) -> bool:
        return self.status in (ThesisStatus.DRAFT, ThesisStatus.ACTIVE,
                               ThesisStatus.PARTIALLY_EXITED,
                               ThesisStatus.EXIT_RECOMMENDED)

    @property
    def remaining_notional(self) -> Optional[float]:
        """None when no entry notional was recorded: unknown, not zero."""
        if self.entry_notional is None:
            return None
        return round(self.entry_notional * self.remaining_fraction, 6)

    @property
    def realized_notional(self) -> Optional[float]:
        if self.entry_notional is None:
            return None
        return round(self.entry_notional * self.realized_fraction, 6)

    def _check_zones(self) -> None:
        """
        The target and the invalidation must sit on opposite sides.

        The entry-price check applies to the INITIAL thesis only. A
        position entered at or beyond its own invalidation is invalid
        the moment it is opened. A later revision is a different case:
        protection trailed above a long's entry price is a stop moved
        into profit, which is exactly what §3.I wants to be possible.
        """
        if self.direction is PositionDirection.LONG:
            if self.invalidation_zone.high >= self.target_zone.low:
                raise ValueError(
                    "for a long thesis the invalidation zone must sit below the "
                    "target zone")
            if (self.revision == 1 and self.entry_price is not None
                    and self.entry_price <= self.invalidation_zone.high):
                raise ValueError(
                    "a long thesis entered at or below its invalidation zone is "
                    "already invalid")
        else:
            if self.invalidation_zone.low <= self.target_zone.high:
                raise ValueError(
                    "for a short thesis the invalidation zone must sit above the "
                    "target zone")
            if (self.revision == 1 and self.entry_price is not None
                    and self.entry_price >= self.invalidation_zone.low):
                raise ValueError(
                    "a short thesis entered at or above its invalidation zone is "
                    "already invalid")


@dataclass(frozen=True)
class PositionThesisRevision:
    """
    One change of mind, with the previous state and the new one.

    Both theses are carried, not just the new one: "old -> new" is the
    unit that can be reviewed, and storing only the result is how a
    system ends up unable to say what it used to believe.
    """
    revision_id: str
    reason: ThesisRevisionReason
    at: datetime
    previous: PositionThesis
    revised: PositionThesis
    evidence: Tuple[str, ...] = ()
    note: str = ""
    lineage_id: Optional[str] = None

    def __post_init__(self):
        if not self.revision_id:
            raise ValueError("revision_id is required")
        object.__setattr__(self, "at", require_utc(self.at, "at"))
        if self.previous.instrument_id != self.revised.instrument_id:
            raise ValueError("a revision may not change the instrument")
        if self.previous.direction is not self.revised.direction:
            raise ValueError("a revision may not flip the position's direction")
        if self.revised.revision <= self.previous.revision:
            raise ValueError("a revision must increase the revision number")
        if self.revised.previous_thesis_id != self.previous.thesis_id:
            raise ValueError("the revised thesis must name its predecessor")
        self._check_rules()

    def _check_rules(self) -> None:
        direction = self.previous.direction
        raised = self.revised.target_zone.is_more_ambitious_than(
            self.previous.target_zone, direction)
        loosened = self.revised.invalidation_zone.is_looser_than(
            self.previous.invalidation_zone, direction)

        # Rule 1: price movement alone cannot ask the market for more.
        if raised and self.reason not in EVIDENCE_BEARING_REASONS:
            raise ValueError(
                f"extending the target on {self.reason.value} alone is not "
                f"evidence; an extension requires one of: "
                f"{sorted(r.value for r in EVIDENCE_BEARING_REASONS)}")

        # Rule 2: a better target may not buy itself a worse floor.
        if raised and loosened:
            raise ValueError(
                "a revision may not raise the target and loosen the invalidation "
                "zone at the same time: target and protection are separate")

    @property
    def extends_target(self) -> bool:
        return self.revised.target_zone.is_more_ambitious_than(
            self.previous.target_zone, self.previous.direction)

    @property
    def tightens_protection(self) -> bool:
        return self.previous.invalidation_zone.is_looser_than(
            self.revised.invalidation_zone, self.previous.direction)


# ======================================================================
# Dynamic exit recommendations
# ======================================================================


class PositionAction(str, Enum):
    """
    What a later phase might RECOMMEND for an open position.

    A recommendation is not an order. Portfolio and Risk still decide,
    and Execution still acts; see the module docstring.
    """
    HOLD = "hold"
    PARTIAL_EXIT = "partial_exit"
    FULL_EXIT = "full_exit"
    EXTEND_TARGET = "extend_target"
    REDUCE_TARGET = "reduce_target"
    TIGHTEN_STOP = "tighten_stop"


#: Actions that change how much of a position is held.
SIZE_CHANGING_ACTIONS = frozenset({
    PositionAction.PARTIAL_EXIT, PositionAction.FULL_EXIT,
})


@dataclass(frozen=True)
class PositionActionRecommendation:
    """
    A proposed action with its size, its reason and its lineage.

    `is_executable` is absent on purpose, unlike `OrderIntent` which
    carries it as False. A recommendation is one step further from the
    venue than an intent, and giving it the same field would invite a
    future caller to flip it.
    """
    recommendation_id: str
    instrument_id: str
    action: PositionAction
    at: datetime
    thesis_id: Optional[str] = None
    exit_fraction: Optional[float] = None
    exit_quantity: Optional[float] = None
    reasons: Tuple[str, ...] = ()
    revised_target_zone: Optional[TargetZone] = None
    revised_invalidation_zone: Optional[InvalidationZone] = None
    lineage_id: Optional[str] = None

    def __post_init__(self):
        if not self.recommendation_id:
            raise ValueError("recommendation_id is required")
        object.__setattr__(self, "at", require_utc(self.at, "at"))

        if self.exit_fraction is not None:
            fraction = float(self.exit_fraction)
            if not 0.0 < fraction <= 1.0:
                raise ValueError(
                    f"exit_fraction must be within (0, 1] (got {fraction})")
        if self.exit_quantity is not None and self.exit_quantity <= 0:
            raise ValueError("exit_quantity must be > 0 when present")

        if self.action is PositionAction.PARTIAL_EXIT:
            if self.exit_fraction is None and self.exit_quantity is None:
                raise ValueError(
                    "a partial exit must say how much: exit_fraction or "
                    "exit_quantity")
            if self.exit_fraction is not None and self.exit_fraction == 1.0:
                raise ValueError(
                    "a partial exit of the whole position is a FULL_EXIT")
        if self.action is PositionAction.FULL_EXIT and self.exit_fraction not in (None, 1.0):
            raise ValueError("a full exit cannot carry a partial fraction")
        if self.action is PositionAction.HOLD and (
                self.exit_fraction is not None or self.exit_quantity is not None):
            raise ValueError("a HOLD recommendation cannot carry an exit size")
        if self.action is PositionAction.EXTEND_TARGET and self.revised_target_zone is None:
            raise ValueError("EXTEND_TARGET must carry the revised target zone")
        if self.action is PositionAction.REDUCE_TARGET and self.revised_target_zone is None:
            raise ValueError("REDUCE_TARGET must carry the revised target zone")
        if self.action is PositionAction.TIGHTEN_STOP and self.revised_invalidation_zone is None:
            raise ValueError("TIGHTEN_STOP must carry the revised invalidation zone")

    @property
    def changes_size(self) -> bool:
        return self.action in SIZE_CHANGING_ACTIONS


# ======================================================================
# Anti-overreaction (§11) -- thresholds configurable, inactive by default
# ======================================================================


@dataclass(frozen=True)
class RevisionPolicy:
    """
    Limits on how often and how easily a thesis may be revised.

    The failure this prevents: target 110 -> 120 -> 106 -> 126 inside a
    week, each step defensible on its own and the sequence indefensible.
    No thresholds are invented here. The defaults are deliberately
    permissive and the policy is INACTIVE in 25.9I-A; calibration
    belongs to 25.9I-E, against evidence.
    """
    min_materiality: float = 0.0
    min_confidence: float = 0.0
    cooldown: timedelta = timedelta(0)
    min_revision_interval: timedelta = timedelta(0)
    max_revisions_per_day: Optional[int] = None
    require_explicit_reason: bool = True

    def __post_init__(self):
        if self.min_materiality < 0:
            raise ValueError("min_materiality must be >= 0")
        if not 0.0 <= self.min_confidence <= 1.0:
            raise ValueError("min_confidence must be between 0 and 1")
        for name in ("cooldown", "min_revision_interval"):
            if getattr(self, name).total_seconds() < 0:
                raise ValueError(f"{name} must not be negative")
        if self.max_revisions_per_day is not None and self.max_revisions_per_day < 0:
            raise ValueError("max_revisions_per_day must be >= 0")

    def permits(self, *, since_last_revision: Optional[timedelta] = None,
                materiality: Optional[float] = None,
                confidence: Optional[float] = None,
                revisions_today: int = 0,
                has_reason: bool = True) -> Tuple[bool, str]:
        """
        Whether a revision clears the policy, and why not when it does not.

        Fail-closed on absent evidence: a materiality or confidence that
        was never measured cannot satisfy a threshold that requires one.
        """
        if self.require_explicit_reason and not has_reason:
            return False, "a revision requires an explicit reason"
        if self.min_revision_interval.total_seconds() > 0:
            if since_last_revision is None:
                return False, "time since the last revision is unknown"
            if since_last_revision < self.min_revision_interval:
                return False, "minimum revision interval has not elapsed"
        if self.cooldown.total_seconds() > 0 and since_last_revision is not None:
            if since_last_revision < self.cooldown:
                return False, "within the cooldown window"
        if self.min_materiality > 0:
            if materiality is None:
                return False, "materiality was not measured"
            if materiality < self.min_materiality:
                return False, "below minimum materiality"
        if self.min_confidence > 0:
            if confidence is None:
                return False, "confidence was not measured"
            if confidence < self.min_confidence:
                return False, "below minimum confidence"
        if (self.max_revisions_per_day is not None
                and revisions_today >= self.max_revisions_per_day):
            return False, "maximum revisions for the day reached"
        return True, ""


# ======================================================================
# Event and overnight risk (§12) -- hooks only
# ======================================================================


class EventRiskKind(str, Enum):
    EARNINGS = "earnings"
    SCHEDULED_MACRO = "scheduled_macro"
    CORPORATE_ACTION = "corporate_action"
    OVERNIGHT_GAP = "overnight_gap"
    UNSCHEDULED = "unscheduled"


@dataclass(frozen=True)
class EventRiskExposure:
    """
    A known event a position will be carried into.

    Sizing and pre-event reduction are 25.9I-F's business. This type
    exists so that when they arrive, "we did not know it was earnings"
    is not an available excuse.
    """
    instrument_id: str
    kind: EventRiskKind
    expected_at: Optional[datetime] = None
    confidence: Optional[float] = None
    expected_move: Optional[float] = None
    source: str = ""

    def __post_init__(self):
        object.__setattr__(self, "expected_at",
                           require_utc(self.expected_at, "expected_at"))
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        if self.expected_move is not None and self.expected_move < 0:
            raise ValueError("expected_move is a magnitude and must be >= 0")
