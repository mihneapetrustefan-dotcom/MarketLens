"""
src/portfolio/sizing.py
----------------------------
Turning signals into proposed allocations (Phase 11, spec §16, §17).

CONFIDENCE IS A GATE, NOT A MULTIPLIER
------------------------------------------
Spec §16 is explicit: "Do NOT simply convert confidence directly into
position size." The temptation is obvious — confidence is already a
0..1 number and multiplying by it looks principled. It is not.

A model's confidence is a statement about how well it expects to
predict DIRECTION. Position size is a statement about how much money
should be exposed to being wrong. Those are different questions, and
the mapping between them depends on volatility, liquidity, correlation
with what is already held, and how much the portfolio can afford to
lose — none of which confidence knows anything about.

Worse, the multiplication is self-reinforcing in exactly the wrong
direction: a miscalibrated model that reports high confidence would
automatically receive more capital, and this project's own calibration
data shows its legacy confidence scores are badly calibrated (the
0.8–0.9 bucket does no better than 0.7–0.8). So confidence is used
here only to DECIDE WHETHER a signal is eligible. Size comes from the
sizing strategy and the risk limits.

STRATEGIES PROPOSE; THEY DO NOT APPROVE
-------------------------------------------
Every strategy here returns an AllocationProposal, which is a request.
None of them checks a sector cap, gross exposure or correlation — that
is the risk engine's job, and duplicating it here would create two
places that can disagree about whether something is allowed. A
strategy may cap a single position at the configured maximum simply to
avoid proposing something obviously impossible, but that is
convenience, not authority: the engine still checks.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence

from src.domain.portfolio_models import (
    AllocationChange, AllocationProposal, ConstraintScope, ConstraintSet,
    PortfolioSnapshot, TradingState, finite_or_none, safe_ratio,
)
from src.domain.signal_models import Signal, SignalDirection

#: Default target weight per new position when no other information
#: differentiates them. 5% means a 20-position book at full investment,
#: which sits comfortably inside the 20% single-position cap.
DEFAULT_TARGET_WEIGHT = 0.05

#: Annualized volatility a volatility-targeted position aims to
#: contribute. 15% is a conventional moderate target.
DEFAULT_VOLATILITY_TARGET = 0.15


@dataclass
class SizingContext:
    """
    Everything a sizing strategy may read.

    Passed as one object so adding an input later does not change every
    strategy's signature, and so a strategy physically cannot reach for
    something that was not offered — notably a database connection,
    which would let it query whatever it liked and quietly break both
    point-in-time correctness and testability.
    """
    as_of: datetime
    snapshot: PortfolioSnapshot
    signals: Sequence[Signal] = field(default_factory=list)
    constraint_set: Optional[ConstraintSet] = None
    #: instrument_id -> annualized volatility, where measurable.
    volatility_by_instrument: Dict[str, Optional[float]] = field(default_factory=dict)
    #: instrument_id -> latest price at the anchor, for quantity conversion.
    price_by_instrument: Dict[str, float] = field(default_factory=dict)

    def max_position_weight(self) -> Optional[float]:
        if self.constraint_set is None:
            return None
        constraint = self.constraint_set.first(ConstraintScope.POSITION_WEIGHT)
        return constraint.max_value if constraint else None

    def min_signal_confidence(self) -> Optional[float]:
        if self.constraint_set is None:
            return None
        constraint = self.constraint_set.first(ConstraintScope.MIN_SIGNAL_CONFIDENCE)
        return constraint.min_value if constraint else None


class PositionSizingStrategy(ABC):
    """
    Base class for sizing methods.

    Versioned like Phase 10's strategies: the version lands on the
    proposal and then on the decision, so a change in sizing logic is
    visible in the audit trail instead of silently reinterpreting past
    proposals.
    """

    strategy_id: str = "base"
    version: str = "v1"

    def _proposal_id(self, portfolio_id: str, as_of: datetime) -> str:
        """
        Deterministic id from (strategy, portfolio, anchor).

        Deterministic so re-running the same evaluation over the same
        information does not accumulate near-duplicate proposals — the
        same idempotency-by-identity argument Phase 10 applied to
        signals.
        """
        raw = f"{self.strategy_id}|{self.version}|{portfolio_id}|{as_of.isoformat()}"
        return f"prop-{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]}"

    def eligible_signals(self, context: SizingContext) -> List[Signal]:
        """
        Signals allowed to be sized at all.

        Four gates, each of which is a reason a signal must not become
        exposure: it must be actionable (Phase 10's own definition —
        active, directional, unsuppressed), it must not be expired at
        the anchor, it must clear the confidence floor, and it must be
        priceable so a weight can become a quantity.
        """
        floor = self.min_confidence(context)
        eligible: List[Signal] = []
        for signal in context.signals:
            if not signal.is_actionable:
                continue
            if signal.is_expired_at(context.as_of):
                continue
            if floor is not None and (signal.confidence is None or signal.confidence < floor):
                continue
            if signal.instrument_id not in context.price_by_instrument:
                continue
            eligible.append(signal)
        return eligible

    def min_confidence(self, context: SizingContext) -> Optional[float]:
        return context.min_signal_confidence()

    def _target_quantity(self, context: SizingContext, instrument_id: str,
                         target_weight: float, direction: SignalDirection
                         ) -> Optional[float]:
        """Convert a target weight into a signed share count at the anchor price."""
        price = context.price_by_instrument.get(instrument_id)
        equity = context.snapshot.equity
        if not price or price <= 0 or equity <= 0:
            return None
        quantity = finite_or_none((target_weight * equity) / price)
        if quantity is None:
            return None
        return -quantity if direction == SignalDirection.SHORT else quantity

    @abstractmethod
    def propose(self, context: SizingContext) -> AllocationProposal:
        """Produce a request to change exposure. Never approves anything."""


class FixedFractionSizing(PositionSizingStrategy):
    """
    Every eligible signal targets the same weight.

    The honest default when there is no validated basis for treating
    one signal as deserving more capital than another. It is not
    sophisticated, and that is the point: an equal-weight rule makes no
    claim it cannot support, whereas anything cleverer here would be
    asserting a relationship between signal properties and correct
    position size that this project has not demonstrated.
    """

    strategy_id = "fixed_fraction"
    version = "v1"

    def __init__(self, target_weight: float = DEFAULT_TARGET_WEIGHT):
        if not 0 < target_weight <= 1:
            raise ValueError("target_weight must be within (0, 1]")
        self.target_weight = target_weight

    def propose(self, context: SizingContext) -> AllocationProposal:
        proposal = AllocationProposal(
            proposal_id=self._proposal_id(context.snapshot.portfolio_id, context.as_of),
            portfolio_id=context.snapshot.portfolio_id,
            as_of=context.as_of,
            sizing_strategy_id=self.strategy_id,
            sizing_version=self.version,
        )

        cap = context.max_position_weight()
        target = self.target_weight if cap is None else min(self.target_weight, cap)

        for signal in self.eligible_signals(context):
            current = context.snapshot.weight_of(signal.instrument_id) or 0.0
            proposal.changes.append(AllocationChange(
                instrument_id=signal.instrument_id,
                current_weight=current,
                target_weight=target,
                current_quantity=None,
                target_quantity=self._target_quantity(
                    context, signal.instrument_id, target, signal.direction),
                signal_id=signal.signal_id,
                reason=(f"fixed fraction {target:.2%} on a {signal.direction.value} "
                        f"signal (confidence {signal.confidence:.2f})"
                        if signal.confidence is not None
                        else f"fixed fraction {target:.2%} on a {signal.direction.value} signal"),
            ))
            proposal.source_signal_ids.append(signal.signal_id)

        return proposal


class VolatilityTargetSizing(PositionSizingStrategy):
    """
    Size inversely to the instrument's own volatility, so each position
    contributes a comparable amount of risk rather than a comparable
    amount of money.

    weight = volatility_target / instrument_volatility, capped.

    A 60%-volatility coin and a 15%-volatility utility are not the same
    bet at the same dollar size; equal-weighting them means the coin
    dominates the portfolio's variance. This is the standard correction.

    An instrument whose volatility could not be measured is SKIPPED, not
    given a default. Substituting an assumed volatility would size a
    position on a number nobody measured — precisely the failure mode
    spec §41 warns about.
    """

    strategy_id = "volatility_target"
    version = "v1"

    def __init__(self, volatility_target: float = DEFAULT_VOLATILITY_TARGET,
                 max_weight: float = DEFAULT_TARGET_WEIGHT * 2):
        if volatility_target <= 0:
            raise ValueError("volatility_target must be positive")
        self.volatility_target = volatility_target
        self.max_weight = max_weight

    def propose(self, context: SizingContext) -> AllocationProposal:
        proposal = AllocationProposal(
            proposal_id=self._proposal_id(context.snapshot.portfolio_id, context.as_of),
            portfolio_id=context.snapshot.portfolio_id,
            as_of=context.as_of,
            sizing_strategy_id=self.strategy_id,
            sizing_version=self.version,
        )

        cap = context.max_position_weight()
        ceiling = self.max_weight if cap is None else min(self.max_weight, cap)

        for signal in self.eligible_signals(context):
            volatility = context.volatility_by_instrument.get(signal.instrument_id)
            if volatility is None or volatility <= 0:
                proposal.note = (proposal.note + "; " if proposal.note else "") + (
                    f"{signal.instrument_id} skipped: volatility not measurable")
                continue

            raw = safe_ratio(self.volatility_target, volatility)
            if raw is None:
                continue
            target = min(raw, ceiling)
            current = context.snapshot.weight_of(signal.instrument_id) or 0.0

            proposal.changes.append(AllocationChange(
                instrument_id=signal.instrument_id,
                current_weight=current,
                target_weight=target,
                target_quantity=self._target_quantity(
                    context, signal.instrument_id, target, signal.direction),
                signal_id=signal.signal_id,
                reason=(f"volatility target {self.volatility_target:.0%} / measured "
                        f"{volatility:.0%} -> {target:.2%}"),
            ))
            proposal.source_signal_ids.append(signal.signal_id)

        return proposal
