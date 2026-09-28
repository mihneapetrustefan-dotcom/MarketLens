"""
src/adaptive/freshness.py
--------------------------------------
Which decisions delayed data may support (Phase 25.9I-A). Inactive.

THE DISTINCTION THIS MODULE EXISTS FOR
------------------------------------------
Delayed data is not useless data. It is perfectly good for screening a
universe, ranking candidates, noticing that something deserves a look,
and watching a long-horizon position at low frequency. What it must
never do is price an action: a fill at a price fifteen minutes old is a
decision made about a market that has already moved.

Phase 25.9H measured what this costs in practice: four real sessions,
every quote marked delayed by IBKR, and therefore zero sessions that
could support an execution decision. The policy below is the rule that
was being applied by hand.

NOT WIRED IN, DELIBERATELY
------------------------------
No production path calls this in 25.9I-A. Wiring it now would change
current behaviour, and §4.D says to create the contract layer only. The
`adaptive_execution_freshness_gate_enabled` flag is where 25.9I-H will
switch it on, after the paths it would gate actually exist.

WHAT IS REUSED
------------------
`DataFreshness` and `FreshnessPolicy` (paper_models) already classify
age, and `MarketDataAvailability` (market_data_models) already records
whether the venue served the quote live or delayed. This module adds
only the mapping from a decision to the quality its inputs must have.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

from src.domain.adaptive_models import MarketDataRequirement, MarketDataState
from src.domain.market_data_models import MarketDataAvailability
from src.domain.paper_models import DataFreshness


class DecisionKind(str, Enum):
    """
    What a decision is about to do, which sets what its data must be.

    The split is between decisions that only produce an opinion and
    decisions that produce, move or cancel an exposure.
    """
    # Analysis: delayed data is acceptable.
    SCREEN = "screen"
    RANK = "rank"
    DETECT_OPPORTUNITY = "detect_opportunity"
    INVESTIGATE = "investigate"
    MONITOR_LONG_HORIZON = "monitor_long_horizon"

    # Execution-sensitive: delayed data is not acceptable.
    BUY = "buy"
    SELL = "sell"
    INCREASE_POSITION = "increase_position"
    DECREASE_POSITION = "decrease_position"
    PARTIAL_EXIT = "partial_exit"
    FULL_EXIT = "full_exit"
    MODIFY_STOP = "modify_stop"
    CANCEL_REPLACE_PRICE_SENSITIVE = "cancel_replace_price_sensitive"


EXECUTION_SENSITIVE_DECISIONS = frozenset({
    DecisionKind.BUY, DecisionKind.SELL,
    DecisionKind.INCREASE_POSITION, DecisionKind.DECREASE_POSITION,
    DecisionKind.PARTIAL_EXIT, DecisionKind.FULL_EXIT,
    DecisionKind.MODIFY_STOP, DecisionKind.CANCEL_REPLACE_PRICE_SENSITIVE,
})

ANALYSIS_DECISIONS = frozenset(DecisionKind) - EXECUTION_SENSITIVE_DECISIONS

#: Freshness values that can never support an execution-sensitive
#: decision, whatever the age threshold says.
NON_EXECUTABLE_FRESHNESS = frozenset({
    DataFreshness.STALE, DataFreshness.INVALID, DataFreshness.UNAVAILABLE,
    # AGING is where a delayed quote lands: recent arrival, old content.
    DataFreshness.AGING,
})


@dataclass(frozen=True)
class FreshnessVerdict:
    permitted: bool
    reason: str = ""
    required_state: Optional[MarketDataState] = None

    def __bool__(self) -> bool:
        return self.permitted


@dataclass(frozen=True)
class ExecutionFreshnessPolicy:
    """
    Whether a given decision may proceed on the data it actually has.

    Fail-closed in three ways, each learned from something that happened:
    absent data is refused, a missing realtime marker is refused for
    execution, and an age that was never measured is refused rather than
    assumed recent.
    """
    max_execution_age_seconds: float = 60.0
    require_realtime_marker: bool = True

    def __post_init__(self):
        if self.max_execution_age_seconds <= 0:
            raise ValueError("max_execution_age_seconds must be > 0")

    def requirement_for(self, decision: DecisionKind) -> MarketDataRequirement:
        """The data quality a decision needs, before looking at any quote."""
        if decision in EXECUTION_SENSITIVE_DECISIONS:
            return MarketDataRequirement(
                state=MarketDataState.HOT,
                max_age_seconds=self.max_execution_age_seconds,
                requires_realtime_marker=self.require_realtime_marker,
                permits_delayed=False,
                purpose=decision.value)
        return MarketDataRequirement(
            state=MarketDataState.COLD, max_age_seconds=None,
            requires_realtime_marker=False, permits_delayed=True,
            purpose=decision.value)

    def permits(self, decision: DecisionKind, *,
                freshness: Optional[DataFreshness] = None,
                availability: Optional[MarketDataAvailability] = None,
                age_seconds: Optional[float] = None) -> FreshnessVerdict:
        """
        The verdict for one decision against one observation.

        Analysis passes on anything that exists at all: delayed quotes
        are what screening runs on. Execution requires a live marker and
        a measured, recent age.
        """
        requirement = self.requirement_for(decision)

        if freshness is DataFreshness.UNAVAILABLE or (
                freshness is None and availability is None and age_seconds is None):
            return FreshnessVerdict(False, "no market data", requirement.state)

        if decision in ANALYSIS_DECISIONS:
            if freshness is DataFreshness.INVALID:
                return FreshnessVerdict(
                    False, "invalid quote: a venue timestamp ahead of receipt, or "
                           "no price", requirement.state)
            return FreshnessVerdict(True, "", requirement.state)

        # Execution-sensitive from here down.
        if availability is not None and availability is not MarketDataAvailability.AVAILABLE:
            return FreshnessVerdict(
                False,
                f"{availability.value} data cannot support {decision.value}",
                requirement.state)
        if availability is None and self.require_realtime_marker:
            return FreshnessVerdict(
                False, "the venue's realtime marker was not recorded",
                requirement.state)
        if freshness is not None and freshness in NON_EXECUTABLE_FRESHNESS:
            return FreshnessVerdict(
                False, f"{freshness.value} data cannot support {decision.value}",
                requirement.state)
        if age_seconds is None:
            return FreshnessVerdict(
                False, "the age of this quote was never measured",
                requirement.state)
        if age_seconds < 0:
            return FreshnessVerdict(
                False, "the quote is stamped in the future: a clock or feed fault",
                requirement.state)
        if age_seconds > self.max_execution_age_seconds:
            return FreshnessVerdict(
                False, f"quote is {age_seconds:.0f}s old, above the "
                       f"{self.max_execution_age_seconds:.0f}s execution limit",
                requirement.state)
        return FreshnessVerdict(True, "", requirement.state)

    def describe(self) -> dict:
        return {
            "max_execution_age_seconds": self.max_execution_age_seconds,
            "require_realtime_marker": self.require_realtime_marker,
            "execution_sensitive_decisions": sorted(
                d.value for d in EXECUTION_SENSITIVE_DECISIONS),
            "active": False,       # 25.9I-A: contract only, gates nothing
        }
