"""
src/execution/events.py
----------------------------
Broker event processing (Phase 14, spec §21, §22, §11).

THE THREE WAYS BROKER EVENTS ARRIVE WRONG
---------------------------------------------
Duplicated, late, and out of order. All three are ordinary, none is an
error, and each corrupts state differently if handled naively:

  DUPLICATE  the same fill applied twice doubles a position
  LATE       a stale status winds a filled order back to working
  REORDERED  FILLED arriving before PARTIALLY_FILLED loses the partial
             or, worse, applies it after the order is already closed

`EventProcessor` defends against all three, and the defences are
independent — a duplicate that is also late must be caught by whichever
check sees it first.

DEDUPLICATION IS BY KEY, NOT BY EQUALITY
--------------------------------------------
Two genuinely different fills can be identical in every visible field:
same order, same instrument, same quantity, same price, same second. A
venue that fills 100 shares as two 50s at one price produces exactly
that. So duplicates are recognised by the venue's own execution id
where it provides one, and by a deterministic key over the identifying
fields where it does not — never by comparing payloads.

ORDERING USES THE STATE MACHINE, NOT TIMESTAMPS
---------------------------------------------------
Timestamps would be the obvious ordering key and they are the wrong
one: brokers stamp events with their own clock, which drifts, and
some stamp all events in a batch identically. The lifecycle position
is authoritative instead — `OrderStateMachine.is_regression` decides
whether an event describes an earlier moment than what is already
recorded, and the answer does not depend on any clock.

WHAT THIS MODULE REFUSES TO DO
----------------------------------
It never invents a fill to explain a position, and never adjusts a
position to match a status. When an event implies something the fill
history does not support, that is a reconciliation finding, not a
correction to apply here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from src.domain.broker_models import (
    ExecutionEvent, ExecutionEventType, ExecutionFill, ExecutionOrder,
    ExecutionOrderState, MismatchKind,
)
from src.execution.states import OrderStateMachine, apply_fill_to_order

#: Which canonical state an event implies. Events that carry no state
#: implication are absent, and processing them moves nothing.
EVENT_TO_STATE: Dict[ExecutionEventType, ExecutionOrderState] = {
    ExecutionEventType.ORDER_SUBMITTED: ExecutionOrderState.SUBMITTED,
    ExecutionEventType.ORDER_ACKNOWLEDGED: ExecutionOrderState.ACKNOWLEDGED,
    ExecutionEventType.ORDER_UPDATED: ExecutionOrderState.WORKING,
    ExecutionEventType.ORDER_PARTIALLY_FILLED: ExecutionOrderState.PARTIALLY_FILLED,
    ExecutionEventType.ORDER_FILLED: ExecutionOrderState.FILLED,
    ExecutionEventType.ORDER_CANCELLED: ExecutionOrderState.CANCELLED,
    ExecutionEventType.ORDER_REJECTED: ExecutionOrderState.REJECTED,
    ExecutionEventType.ORDER_EXPIRED: ExecutionOrderState.EXPIRED,
    ExecutionEventType.ORDER_UNKNOWN: ExecutionOrderState.UNKNOWN,
}


@dataclass
class EventOutcome:
    """What happened to one event, and why."""
    event: ExecutionEvent
    applied: bool
    reason: str = ""
    new_state: Optional[ExecutionOrderState] = None
    fill_applied: Optional[ExecutionFill] = None

    @property
    def ignored(self) -> bool:
        return not self.applied


@dataclass
class ProcessingReport:
    """The result of draining a batch of events."""
    processed: int = 0
    applied: int = 0
    duplicates: int = 0
    late: int = 0
    illegal: int = 0
    unmatched: int = 0
    outcomes: List[EventOutcome] = field(default_factory=list)
    fills: List[ExecutionFill] = field(default_factory=list)
    #: Conditions that need reconciliation rather than local handling.
    findings: List[Tuple[MismatchKind, str]] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.findings


class EventProcessor:
    """
    Folds broker events into order state, safely.

    Owns the seen-key sets, so a restart that reloads them from the
    database gets the same protection a long-running process has. Those
    sets are the only mutable state, and `seed` is how recovery
    restores them.
    """

    def __init__(self, machine: Optional[OrderStateMachine] = None):
        self.machine = machine or OrderStateMachine()
        self._seen_events: Set[str] = set()
        self._seen_fills: Set[str] = set()

    # ---------------- recovery ----------------

    def seed(self, event_keys: Iterable[str] = (),
             fill_keys: Iterable[str] = ()) -> None:
        """
        Restore the deduplication sets after a restart.

        Without this, the first redelivery after a restart is treated
        as new — which is precisely when redeliveries happen, because
        reconnecting is what makes a venue replay its recent events.
        """
        self._seen_events.update(k for k in event_keys if k)
        self._seen_fills.update(k for k in fill_keys if k)

    @property
    def seen_event_keys(self) -> Set[str]:
        return set(self._seen_events)

    @property
    def seen_fill_keys(self) -> Set[str]:
        return set(self._seen_fills)

    # ---------------- processing ----------------

    def process(self, events: Sequence[ExecutionEvent],
                orders: Dict[str, ExecutionOrder],
                fills_by_event: Optional[Dict[str, ExecutionFill]] = None
                ) -> ProcessingReport:
        """
        Apply a batch of events to the orders they refer to.

        `orders` is keyed by our own order id. `fills_by_event` carries
        the fill an event delivered, when it delivered one — kept
        separate so an event can be recognised as a duplicate without
        the fill ever being constructed.
        """
        report = ProcessingReport()
        fills_by_event = fills_by_event or {}

        for event in events:
            report.processed += 1
            outcome = self._process_one(event, orders, fills_by_event, report)
            report.outcomes.append(outcome)
            if outcome.applied:
                report.applied += 1
            if outcome.fill_applied is not None:
                report.fills.append(outcome.fill_applied)

        return report

    def _process_one(self, event: ExecutionEvent,
                     orders: Dict[str, ExecutionOrder],
                     fills_by_event: Dict[str, ExecutionFill],
                     report: ProcessingReport) -> EventOutcome:

        # --- duplicate event -----------------------------------------
        key = event.idempotency_key
        if key and key in self._seen_events:
            report.duplicates += 1
            return EventOutcome(event, False, "duplicate event, already applied")
        if key:
            self._seen_events.add(key)

        # --- events that are not about an order ----------------------
        if event.event_type not in EVENT_TO_STATE and event.order_id is None:
            return EventOutcome(event, True, "informational event")

        order = orders.get(event.order_id) if event.order_id else None
        if order is None:
            # A broker event naming an order we have no record of. Not
            # something to fix here: it may be another session's order,
            # or ours with a lost write. Reconciliation decides.
            report.unmatched += 1
            report.findings.append((
                MismatchKind.MISSING_INTERNAL_ORDER,
                f"event {event.event_id} ({event.event_type.value}) references "
                f"order {event.order_id or event.broker_order_id!r}, which is "
                f"not in our book"))
            return EventOutcome(event, False, "no matching internal order")

        # --- the fill, if this event carries one ---------------------
        fill = fills_by_event.get(event.event_id)
        applied_fill: Optional[ExecutionFill] = None
        if fill is not None:
            fill_key = fill.idempotency_key or fill.execution_id or fill.fill_id
            if fill_key in self._seen_fills:
                report.duplicates += 1
                report.findings.append((
                    MismatchKind.DUPLICATE_FILL,
                    f"fill {fill.fill_id} was delivered again and ignored"))
                return EventOutcome(event, False, "duplicate fill, already applied")

            if not apply_fill_to_order(order, fill.quantity, fill.price,
                                       fill.commission, fill.fees):
                # Over-fill: the invariant of spec §64. Never applied,
                # always recorded — a venue reporting more than we
                # ordered is a discrepancy for a human.
                report.findings.append((
                    MismatchKind.QUANTITY_MISMATCH,
                    f"fill {fill.fill_id} of {fill.quantity:g} would take order "
                    f"{order.order_id} past its quantity "
                    f"({order.filled_quantity:g}/{order.quantity:g})"))
                return EventOutcome(event, False, "fill would overfill the order")

            self._seen_fills.add(fill_key)
            applied_fill = fill

        # --- the state move ------------------------------------------
        to_state = EVENT_TO_STATE.get(event.event_type)
        if to_state is None:
            return EventOutcome(event, True, "no state change implied",
                                fill_applied=applied_fill)

        # A FILLED event on an order that is not fully filled is a
        # status the fills do not support. The status is not applied;
        # reconciliation is told.
        if (to_state is ExecutionOrderState.FILLED
                and order.filled_quantity + 1e-9 < order.quantity):
            report.findings.append((
                MismatchKind.STATUS_MISMATCH,
                f"broker reports order {order.order_id} filled, but only "
                f"{order.filled_quantity:g} of {order.quantity:g} is explained "
                f"by fills we hold"))
            outcome = self.machine.apply(
                order, ExecutionOrderState.RECONCILIATION_REQUIRED,
                at=event.at, reason="filled status unsupported by fills",
                event_id=event.event_id, correlation_id=event.correlation_id,
                strict=False)
            return EventOutcome(event, outcome.applied,
                                outcome.ignored_reason or "routed to reconciliation",
                                new_state=order.state, fill_applied=applied_fill)

        transition = self.machine.apply(
            order, to_state, at=event.at,
            reason=f"broker event {event.event_type.value}",
            event_id=event.event_id, correlation_id=event.correlation_id,
            strict=False)

        if not transition.applied:
            if "late event" in transition.ignored_reason:
                report.late += 1
            elif "illegal transition" in transition.ignored_reason:
                report.illegal += 1
            return EventOutcome(event, applied_fill is not None,
                                transition.ignored_reason,
                                new_state=order.state,
                                fill_applied=applied_fill)

        return EventOutcome(event, True, "", new_state=order.state,
                            fill_applied=applied_fill)
