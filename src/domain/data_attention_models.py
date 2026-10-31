"""
src/domain/data_attention_models.py
-------------------------------------------------
Where market-data attention goes, and what it actually got
(Phase 25.9I-C, spec sections 5, 6, 13, 30, 36).

ATTENTION IS NOT CAPITAL
----------------------------
25.9I-D allocates money. This allocates *looking*. They are separate
scarce resources with separate constraints, and conflating them
produces the mistake where the biggest position is assumed to be the
one most in need of a fresh price. A small position one tick from its
invalidation needs realtime far more than a large quiet one.

THE SEPARATION THAT MATTERS MOST
------------------------------------
Four different things were being conflated by the word "realtime":

    desired_state          what the system decided is worth paying for
    availability           what the feed actually served
    entitlement            what the account is allowed to receive
    freshness              how old the quote in hand is

`InstrumentDataStatus` carries all four and derives
`execution_freshness_ready` from the conjunction, so no caller can read
one of them and believe it has the others. Phase 25.9H's four real
sessions were all "HOT desired, delayed served" -- a state that must be
expressible, and must not evaluate as ready.

HOT CAPACITY IS A CEILING, NOT A TARGET (section 14)
--------------------------------------------------------
`AttentionPlan.unused_hot_capacity` exists to be reported as a normal,
healthy outcome. The 25.9I-B decision that "31 is a maximum capacity,
not a required count" applies here with more force, because unlike a
universe slot, an unused HOT slot costs nothing and a used one does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, Optional, Tuple

from src.domain.adaptive_models import (
    MarketDataState, RealtimeActivationReason, RealtimeDeactivationReason,
    _require_non_negative, require_utc,
)
from src.domain.market_data_billing_models import EntitlementStatus
from src.domain.market_data_models import MarketDataAvailability
from src.domain.paper_models import DataFreshness


class AttentionPriority(str, Enum):
    """
    Who gets data first when there is not enough to go round (section 30).

    Explicitly ordered and explicitly named, rather than a score. A
    hidden numeric priority is unarguable after the fact: nobody can say
    whether 0.62 should have beaten 0.58. A class can be checked against
    the rule that produced it.

    The order encodes one judgement: an open position that may need to
    LEAVE outranks any candidate that might be worth entering. A missed
    entry costs an opportunity; a missed exit costs money that exists.
    """
    POSITION_EXIT = "position_exit"
    POSITION_MONITOR = "position_monitor"
    ENTRY_CANDIDATE = "entry_candidate"
    BROAD_SCREENING = "broad_screening"

    @property
    def rank(self) -> int:
        return _PRIORITY_RANK[self]

    @property
    def is_position_safety(self) -> bool:
        """Whether a budget refusal here must be escalated, not absorbed."""
        return self in (AttentionPriority.POSITION_EXIT,
                        AttentionPriority.POSITION_MONITOR)


_PRIORITY_RANK: Dict[AttentionPriority, int] = {
    AttentionPriority.POSITION_EXIT: 0,
    AttentionPriority.POSITION_MONITOR: 1,
    AttentionPriority.ENTRY_CANDIDATE: 2,
    AttentionPriority.BROAD_SCREENING: 3,
}


@dataclass(frozen=True)
class DataAttentionRequest:
    """
    One subsystem asking for one instrument to be watched more closely.

    Several of these may name the same instrument in the same cycle --
    a thesis near its target, an event, a risk review -- and section 36
    requires them to become ONE activation. The requester is recorded so
    the merge can name everyone who asked.
    """
    instrument_id: str
    desired_state: MarketDataState
    requester: str
    reason: str = ""
    priority: AttentionPriority = AttentionPriority.BROAD_SCREENING
    activation_reason: Optional[RealtimeActivationReason] = None
    as_of: Optional[datetime] = None
    lineage_id: Optional[str] = None
    thesis_id: Optional[str] = None
    admission_decision_id: Optional[str] = None
    session_snapshot_id: Optional[str] = None
    expires_at: Optional[datetime] = None

    def __post_init__(self):
        if not str(self.instrument_id).strip():
            raise ValueError("instrument_id is required")
        if not str(self.requester).strip():
            # Deduplication has to be able to say who asked. An
            # anonymous request cannot be attributed or revoked.
            raise ValueError("requester is required")
        object.__setattr__(self, "as_of", require_utc(self.as_of, "as_of"))
        object.__setattr__(self, "expires_at",
                           require_utc(self.expires_at, "expires_at"))
        if (self.desired_state is MarketDataState.HOT
                and self.activation_reason is None):
            raise ValueError(
                "a HOT request must name a RealtimeActivationReason; "
                "no resource is consumed merely because it exists")


@dataclass(frozen=True)
class InstrumentDataStatus:
    """
    Desired, available, entitled, fresh -- four answers, never one.

    `execution_freshness_ready` is the only property an execution-
    sensitive caller may consult, and it is a conjunction of all four
    plus a measured age. It is False by default and every path to True
    requires positive evidence.
    """
    instrument_id: str
    exchange: str = ""
    desired_state: MarketDataState = MarketDataState.COLD
    availability: MarketDataAvailability = MarketDataAvailability.UNKNOWN
    entitlement: EntitlementStatus = EntitlementStatus.UNKNOWN
    freshness: DataFreshness = DataFreshness.UNAVAILABLE
    age_seconds: Optional[float] = None
    quote_at: Optional[datetime] = None
    stream_active: bool = False
    stream_stale: bool = False
    note: str = ""

    def __post_init__(self):
        if not str(self.instrument_id).strip():
            raise ValueError("instrument_id is required")
        object.__setattr__(self, "exchange", str(self.exchange).strip().upper())
        object.__setattr__(self, "quote_at", require_utc(self.quote_at, "quote_at"))
        # age may legitimately be negative (a host clock behind the
        # venue, as on 2026-09-21), so it is NOT forced non-negative
        # here -- it is rejected at the freshness check instead, where
        # the reason can be reported.

    def execution_freshness_ready(self, max_age_seconds: float = 60.0
                                  ) -> Tuple[bool, str]:
        """
        Whether this instrument could price an action right now, and why not.

        Fails closed at every step. The reason string is returned rather
        than logged because the caller is usually about to refuse
        something and needs to say what is missing.
        """
        if self.desired_state is not MarketDataState.HOT:
            return False, (f"{self.instrument_id} is {self.desired_state.value}; "
                           "execution requires a HOT desired state")
        if self.entitlement is not EntitlementStatus.REALTIME_VERIFIED:
            return False, (f"{self.instrument_id}: entitlement is "
                           f"{self.entitlement.value}, not realtime_verified")
        if self.availability is not MarketDataAvailability.AVAILABLE:
            return False, (f"{self.instrument_id}: the feed served "
                           f"{self.availability.value} data")
        if self.stream_stale:
            return False, f"{self.instrument_id}: the stream is stale"
        if self.freshness in (DataFreshness.INVALID, DataFreshness.UNAVAILABLE,
                              DataFreshness.STALE):
            return False, (f"{self.instrument_id}: the quote is "
                           f"{self.freshness.value}")
        if self.age_seconds is None:
            return False, f"{self.instrument_id}: quote age was never measured"
        if self.age_seconds < 0:
            return False, (f"{self.instrument_id}: quote is {-self.age_seconds:.0f}s "
                           "in the future; the local clock disagrees with the venue")
        if self.age_seconds > max_age_seconds:
            return False, (f"{self.instrument_id}: quote is {self.age_seconds:.0f}s "
                           f"old, limit {max_age_seconds:.0f}s")
        return True, ""

    def describe(self, max_age_seconds: float = 60.0) -> Dict[str, object]:
        ready, reason = self.execution_freshness_ready(max_age_seconds)
        return {
            "instrument_id": self.instrument_id,
            "exchange": self.exchange,
            "desired_state": self.desired_state.value,
            "availability": self.availability.value,
            "entitlement": self.entitlement.value,
            "freshness": self.freshness.value,
            "age_seconds": self.age_seconds,
            "stream_active": self.stream_active,
            "stream_stale": self.stream_stale,
            "execution_freshness_ready": ready,
            "not_ready_because": reason,
        }


@dataclass(frozen=True)
class AttentionDecision:
    """
    One instrument's resolved state, with everyone who asked for it.

    `requesters` and `reasons` are tuples, not single values, because
    four subsystems wanting the same instrument HOT is ONE activation
    (section 36) and losing three of the four reasons would make the
    later question "why was this hot" unanswerable.
    """
    instrument_id: str
    exchange: str
    granted_state: MarketDataState
    previous_state: MarketDataState = MarketDataState.COLD
    priority: AttentionPriority = AttentionPriority.BROAD_SCREENING
    requesters: Tuple[str, ...] = ()
    reasons: Tuple[str, ...] = ()
    activation_reasons: Tuple[RealtimeActivationReason, ...] = ()
    deactivation_reason: Optional[RealtimeDeactivationReason] = None
    downgraded_from: Optional[MarketDataState] = None
    downgrade_reason: str = ""
    estimated_cost_usd: Optional[float] = None
    as_of: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    lineage_id: Optional[str] = None

    def __post_init__(self):
        object.__setattr__(self, "exchange", str(self.exchange).strip().upper())
        object.__setattr__(self, "as_of", require_utc(self.as_of, "as_of"))
        object.__setattr__(self, "expires_at",
                           require_utc(self.expires_at, "expires_at"))
        _require_non_negative(self.estimated_cost_usd, "estimated_cost_usd")

    @property
    def was_downgraded(self) -> bool:
        return self.downgraded_from is not None

    @property
    def is_hot(self) -> bool:
        return self.granted_state is MarketDataState.HOT

    def describe(self) -> Dict[str, object]:
        return {
            "instrument_id": self.instrument_id,
            "exchange": self.exchange,
            "granted_state": self.granted_state.value,
            "previous_state": self.previous_state.value,
            "priority": self.priority.value,
            "requesters": list(self.requesters),
            "reasons": list(self.reasons),
            "downgraded_from": (self.downgraded_from.value
                                if self.downgraded_from else None),
            "downgrade_reason": self.downgrade_reason,
            "estimated_cost_usd": self.estimated_cost_usd,
        }


@dataclass(frozen=True)
class AttentionPlan:
    """
    The whole cycle's answer: who is COLD, WARM, HOT, and what is unused.

    Reports `unused_hot_capacity` prominently because an unused slot is
    a correct outcome, not a shortfall to be filled.
    """
    decisions: Tuple[AttentionDecision, ...] = ()
    max_hot_symbols: Optional[int] = None
    candidates_considered: int = 0
    requests_received: int = 0
    escalations_refused: Tuple[Tuple[str, str], ...] = ()
    as_of: Optional[datetime] = None
    session_snapshot_id: Optional[str] = None
    notes: Tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "as_of", require_utc(self.as_of, "as_of"))
        if self.max_hot_symbols is not None and int(self.max_hot_symbols) < 0:
            raise ValueError("max_hot_symbols must be >= 0")

    def in_state(self, state: MarketDataState) -> Tuple[AttentionDecision, ...]:
        return tuple(d for d in self.decisions if d.granted_state is state)

    @property
    def hot(self) -> Tuple[AttentionDecision, ...]:
        return self.in_state(MarketDataState.HOT)

    @property
    def warm(self) -> Tuple[AttentionDecision, ...]:
        return self.in_state(MarketDataState.WARM)

    @property
    def cold(self) -> Tuple[AttentionDecision, ...]:
        return self.in_state(MarketDataState.COLD)

    @property
    def unused_hot_capacity(self) -> Optional[int]:
        """None when uncapped. Zero is not a goal and neither is filling it."""
        if self.max_hot_symbols is None:
            return None
        return max(0, int(self.max_hot_symbols) - len(self.hot))

    @property
    def exchanges_touched(self) -> Tuple[str, ...]:
        return tuple(sorted({d.exchange for d in self.decisions
                             if d.granted_state is not MarketDataState.COLD
                             and d.exchange}))

    def describe(self) -> Dict[str, object]:
        return {
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "candidates_considered": self.candidates_considered,
            "requests_received": self.requests_received,
            "decisions": len(self.decisions),
            "hot": len(self.hot),
            "warm": len(self.warm),
            "cold": len(self.cold),
            "max_hot_symbols": self.max_hot_symbols,
            "unused_hot_capacity": self.unused_hot_capacity,
            "exchanges_touched": list(self.exchanges_touched),
            "escalations_refused": [list(pair) for pair in self.escalations_refused],
            "notes": list(self.notes),
        }
