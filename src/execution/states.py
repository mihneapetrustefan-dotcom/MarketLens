"""
src/execution/states.py
----------------------------
The canonical order state machine (Phase 14, spec §9, §22, §54).

WHY A MACHINE RATHER THAN AN ASSIGNMENT
-------------------------------------------
Order state is the thing every other subsystem trusts. Positions move
because an order filled; reconciliation searches on orders that are
working; the audit trail is the transition history. If any code could
write any state, all three of those become guesses.

So state changes go through `OrderStateMachine.apply`, which refuses
transitions that are not in `ORDER_TRANSITIONS` and records every
accepted one. There is no setter that skips it.

OUT-OF-ORDER EVENTS ARE THE NORMAL CASE
-------------------------------------------
Real brokers deliver events late, twice, and in the wrong order. A
FILLED that arrives before the PARTIALLY_FILLED it superseded is not a
corruption to be rejected — it is Tuesday. `is_regression` recognises
that the late event describes an EARLIER moment than the state already
recorded, and drops it rather than winding the order backwards.

The rule: an event may only move an order forward through the
lifecycle, or into a state that represents a new question (UNKNOWN,
RECONCILIATION_REQUIRED). It may never resurrect a terminal order into
a working one, because that is how a closed position gets reopened by
a duplicate message.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

from src.domain.broker_models import (
    ORDER_TRANSITIONS, ExecutionOrder, ExecutionOrderState,
    OrderStateTransition,
)


#: How far through the lifecycle each state sits. Used only to detect
#: regressions — a late event describing an earlier stage than the one
#: already recorded. States that pose a question rather than describe
#: progress share the highest rank so they are never treated as stale.
_PROGRESS: Dict[ExecutionOrderState, int] = {
    ExecutionOrderState.CREATED: 0,
    ExecutionOrderState.VALIDATING: 1,
    ExecutionOrderState.APPROVED: 2,
    ExecutionOrderState.SUBMITTING: 3,
    ExecutionOrderState.SUBMITTED: 4,
    ExecutionOrderState.ACKNOWLEDGED: 5,
    ExecutionOrderState.WORKING: 6,
    ExecutionOrderState.PARTIALLY_FILLED: 7,
    ExecutionOrderState.CANCEL_REQUESTED: 8,
    ExecutionOrderState.FILLED: 9,
    ExecutionOrderState.CANCELLED: 9,
    ExecutionOrderState.REJECTED: 9,
    ExecutionOrderState.EXPIRED: 9,
    ExecutionOrderState.FAILED: 9,
    ExecutionOrderState.UNKNOWN: 10,
    ExecutionOrderState.RECONCILIATION_REQUIRED: 10,
}


class InvalidTransition(Exception):
    """
    Raised when a caller asks for a transition the lifecycle forbids.

    Deliberately an exception rather than a silent no-op: a component
    trying to move an order from CREATED straight to FILLED has a bug,
    and swallowing it would leave the bug in place with the position
    already wrong.
    """

    def __init__(self, order_id: str, current: ExecutionOrderState,
                 requested: ExecutionOrderState):
        super().__init__(
            f"order {order_id}: {current.value} -> {requested.value} is not a "
            f"legal transition")
        self.order_id = order_id
        self.current = current
        self.requested = requested


@dataclass
class TransitionOutcome:
    """What `apply` did, so the caller can tell 'ignored' from 'moved'."""
    applied: bool
    order: ExecutionOrder
    transition: Optional[OrderStateTransition] = None
    ignored_reason: str = ""

    @property
    def state(self) -> ExecutionOrderState:
        return self.order.state


class OrderStateMachine:
    """
    Applies and records state changes for one execution book.

    Holds the transition history in memory for the current process; the
    repository persists it. Kept separate from persistence so the rules
    can be tested without a database, and so a failed database write
    cannot leave the in-memory order in a state nothing recorded.
    """

    def __init__(self):
        self.history: Dict[str, List[OrderStateTransition]] = {}

    # ---------------- queries ----------------

    @staticmethod
    def can_transition(current: ExecutionOrderState,
                       requested: ExecutionOrderState) -> bool:
        return requested in ORDER_TRANSITIONS.get(current, set())

    @staticmethod
    def is_regression(current: ExecutionOrderState,
                      requested: ExecutionOrderState) -> bool:
        """
        True when `requested` describes an earlier moment than `current`.

        This is the out-of-order guard. It is separate from
        `can_transition` because the two answer different questions:
        one asks whether the move is legal at all, the other whether
        this particular message is simply late.
        """
        return _PROGRESS.get(requested, 0) < _PROGRESS.get(current, 0)

    def transitions_for(self, order_id: str) -> List[OrderStateTransition]:
        return list(self.history.get(order_id, ()))

    def seed(self, transitions: Sequence[OrderStateTransition]) -> None:
        """Reload persisted history after a restart."""
        for transition in transitions:
            self.history.setdefault(transition.order_id, []).append(transition)
        for entries in self.history.values():
            entries.sort(key=lambda t: t.sequence)

    # ---------------- mutation ----------------

    def apply(self, order: ExecutionOrder, to_state: ExecutionOrderState,
              at: Optional[datetime] = None, reason: str = "",
              event_id: Optional[str] = None,
              correlation_id: str = "",
              strict: bool = True) -> TransitionOutcome:
        """
        Move an order, or explain why it did not move.

        `strict=True` raises on an illegal transition — the right
        behaviour for our own orchestrator, whose transitions we
        control. Adapters processing broker events pass `strict=False`,
        because a venue sending a nonsensical sequence is a fact to
        record, not an exception to propagate into the tick.
        """
        current = order.state

        if to_state is current:
            return TransitionOutcome(
                False, order, ignored_reason="already in that state")

        if self.is_regression(current, to_state):
            return TransitionOutcome(
                False, order,
                ignored_reason=(
                    f"late event: {to_state.value} describes an earlier stage "
                    f"than the recorded {current.value}"))

        if not self.can_transition(current, to_state):
            if strict:
                raise InvalidTransition(order.order_id, current, to_state)
            return TransitionOutcome(
                False, order,
                ignored_reason=(
                    f"illegal transition {current.value} -> {to_state.value}; "
                    f"order left unchanged"))

        sequence = len(self.history.get(order.order_id, ())) + 1
        transition = OrderStateTransition(
            order_id=order.order_id, sequence=sequence, from_state=current,
            to_state=to_state, at=at, reason=reason, event_id=event_id,
            correlation_id=correlation_id or order.correlation_id)

        order.state = to_state
        if to_state.is_terminal:
            order.terminal_at = at
        self.history.setdefault(order.order_id, []).append(transition)
        return TransitionOutcome(True, order, transition=transition)

    def force(self, order: ExecutionOrder, to_state: ExecutionOrderState,
              at: Optional[datetime], reason: str,
              correlation_id: str = "") -> TransitionOutcome:
        """
        Record a state that reconciliation established against the broker.

        This is the one path that may contradict the lifecycle, and it
        exists because the broker is the authority on what happened at
        the broker. It is never silent: the reason is required, and the
        transition is recorded like any other, so a book that was
        corrected always shows that it was.
        """
        if not reason:
            raise ValueError("a forced transition must state its reason")
        sequence = len(self.history.get(order.order_id, ())) + 1
        transition = OrderStateTransition(
            order_id=order.order_id, sequence=sequence, from_state=order.state,
            to_state=to_state, at=at, reason=f"reconciliation: {reason}",
            correlation_id=correlation_id or order.correlation_id)
        order.state = to_state
        if to_state.is_terminal:
            order.terminal_at = at
        self.history.setdefault(order.order_id, []).append(transition)
        return TransitionOutcome(True, order, transition=transition)


def apply_fill_to_order(order: ExecutionOrder, quantity: float, price: float,
                        commission: float = 0.0, fees: float = 0.0) -> bool:
    """
    Fold one fill into an order's running totals.

    Returns False and changes nothing when the fill would take the
    order past its own quantity — the invariant of spec §64. That is
    the shape a duplicate fill takes when it slips past the
    idempotency check, and letting it through would double-count the
    position.

    The average price is recomputed from the running notional rather
    than averaged with the previous average, which would weight the
    fills wrongly whenever they differ in size.
    """
    if quantity <= 0:
        return False
    if order.filled_quantity + quantity > order.quantity + 1e-9:
        return False

    previous_notional = order.filled_quantity * (order.average_fill_price or 0.0)
    order.filled_quantity += quantity
    order.average_fill_price = (previous_notional + quantity * price) / order.filled_quantity
    order.commission += commission
    order.fees += fees
    return True
