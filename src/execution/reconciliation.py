"""
src/execution/reconciliation.py
-----------------------------------
Broker reconciliation (Phase 14, spec §23, §24, §60).

THE PREMISE
---------------
Internal state is a belief about the broker. The broker is the fact.
Every part of this module follows from that: we compare, we record the
disagreements, and we never overwrite one side with the other to make
the numbers agree.

That last rule is the one that costs discipline. A position mismatch is
uncomfortable, and adjusting the local book makes it disappear. It also
destroys the only evidence of whatever caused it, and the cause is
usually a missing fill that will cause it again tomorrow.

WHAT IS COMPARED
--------------------
Orders, fills, positions and cash — the four things a trading system
can be wrong about in a way that matters. Each comparison runs in both
directions, because the two failures are different: an order we have
that the broker does not is a lost submission, and an order the broker
has that we do not is an untracked position.

THE UNKNOWN-ORDER RESOLUTION (spec §24)
-------------------------------------------
The dangerous case. We submitted, the network timed out, and the venue
may or may not have accepted. `resolve_unknown_orders` asks the broker
about each one and applies what it says.

It never resubmits. Resubmission is how one intended position becomes
two real ones, and the whole reason UNKNOWN exists as a state rather
than being collapsed into "failed, try again".

An order the broker has never heard of resolves to FAILED — safe,
because a venue that has no record of an order will not fill it. An
order the broker cannot be asked about stays UNKNOWN and is reported,
because the honest answer is still that we do not know.

TOLERANCES ARE EXPLICIT
---------------------------
Floating-point equality on prices and quantities produces mismatches
that are pure arithmetic noise. The thresholds are named constants so
they can be argued about, rather than magic numbers buried in
comparisons.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from src.domain.broker_models import (
    AccountSnapshot, ExecutionFill, ExecutionOrder, ExecutionOrderState,
    MismatchKind, PositionSnapshot, ReconciliationMismatch,
    ReconciliationRecord,
)
from src.execution.gateway import BrokerGateway, BrokerOrderView
from src.execution.states import OrderStateMachine

#: Canonical states grouped by the STAGE they describe.
#:
#: Venues disagree about what to call an accepted-but-unfilled order —
#: acknowledged, working, open, submitted are all the same fact. And
#: our own record moves through several of those within one tick. So
#: reconciliation compares stages, not spellings; comparing exact
#: states would report a mismatch on every healthy book, and an alert
#: that always fires is one nobody reads.
#:
#: What it still catches is a real disagreement: working versus filled,
#: working versus cancelled, filled versus rejected.
_STATE_CLASS: Dict[ExecutionOrderState, str] = {
    ExecutionOrderState.CREATED: "pending",
    ExecutionOrderState.VALIDATING: "pending",
    ExecutionOrderState.APPROVED: "pending",
    ExecutionOrderState.SUBMITTING: "pending",
    ExecutionOrderState.SUBMITTED: "working",
    ExecutionOrderState.ACKNOWLEDGED: "working",
    ExecutionOrderState.WORKING: "working",
    ExecutionOrderState.PARTIALLY_FILLED: "working",
    ExecutionOrderState.CANCEL_REQUESTED: "working",
    ExecutionOrderState.FILLED: "filled",
    ExecutionOrderState.CANCELLED: "cancelled",
    ExecutionOrderState.REJECTED: "rejected",
    ExecutionOrderState.EXPIRED: "expired",
    ExecutionOrderState.FAILED: "failed",
    ExecutionOrderState.UNKNOWN: "unknown",
    ExecutionOrderState.RECONCILIATION_REQUIRED: "unknown",
}


def state_class(state: ExecutionOrderState) -> str:
    """The stage a state describes, for comparison across venues."""
    return _STATE_CLASS.get(state, "unknown")


#: Quantities agreeing to within this are the same quantity.
QUANTITY_TOLERANCE = 1e-6
#: Cash agreeing to within this is the same cash — a hundredth of a cent.
CASH_TOLERANCE = 1e-4
#: Prices are compared in relative terms; venues round differently.
PRICE_TOLERANCE_BPS = 1.0


@dataclass
class UnknownResolution:
    """What asking the broker settled about one unknown order."""
    order_id: str
    resolved: bool
    state: ExecutionOrderState
    detail: str
    broker_order_id: Optional[str] = None


class BrokerReconciler:
    """
    Compares our book against a broker's and records the differences.

    Stateless between passes. Every conclusion comes from the
    arguments, so a reconciliation can be re-run over stored inputs and
    reach the same verdict — which is what makes an old record worth
    reading.
    """

    def __init__(self, quantity_tolerance: float = QUANTITY_TOLERANCE,
                 cash_tolerance: float = CASH_TOLERANCE,
                 price_tolerance_bps: float = PRICE_TOLERANCE_BPS):
        self.quantity_tolerance = quantity_tolerance
        self.cash_tolerance = cash_tolerance
        self.price_tolerance_bps = price_tolerance_bps

    # ---------------- the pass ----------------

    def reconcile(self, broker_id: str, account_id: str, at: datetime,
                  internal_orders: Sequence[ExecutionOrder],
                  broker_orders: Sequence[BrokerOrderView],
                  internal_fills: Sequence[ExecutionFill] = (),
                  internal_positions: Optional[Dict[str, float]] = None,
                  broker_positions: Sequence[PositionSnapshot] = (),
                  internal_cash: Optional[float] = None,
                  broker_account: Optional[AccountSnapshot] = None,
                  reconciliation_id: str = "",
                  correlation_id: str = "",
                  scope: str = "all") -> ReconciliationRecord:
        record = ReconciliationRecord(
            reconciliation_id=reconciliation_id or f"rec-{int(at.timestamp())}",
            broker_id=broker_id, account_id=account_id, at=at, scope=scope,
            correlation_id=correlation_id,
            orders_compared=len(internal_orders),
            fills_compared=len(internal_fills),
            positions_compared=len(broker_positions))

        self._check_orders(record, internal_orders, broker_orders)
        self._check_fills(record, internal_fills)
        self._check_positions(record, internal_positions or {}, broker_positions)
        self._check_cash(record, internal_cash, broker_account)
        self._check_unknown(record, internal_orders)
        return record

    # ---------------- order comparison ----------------

    def _check_orders(self, record: ReconciliationRecord,
                      internal: Sequence[ExecutionOrder],
                      broker: Sequence[BrokerOrderView]) -> None:
        record.checks_performed += 1
        by_broker_id = {v.broker_order_id: v for v in broker}
        by_client_id = {v.client_order_id: v for v in broker if v.client_order_id}
        matched: set = set()

        for order in internal:
            if not order.state.is_working:
                continue
            view = None
            if order.broker_order_id:
                view = by_broker_id.get(order.broker_order_id)
            if view is None and order.client_order_id:
                # The fallback that matters after a timeout: we may have
                # no broker id, but we always sent a client id.
                view = by_client_id.get(order.client_order_id)

            if view is None:
                record.mismatches.append(ReconciliationMismatch(
                    kind=MismatchKind.MISSING_INTERNAL_ORDER,
                    detail=(f"order {order.order_id} is {order.state.value} for us "
                            f"but the broker does not report it"),
                    order_id=order.order_id,
                    broker_order_id=order.broker_order_id,
                    internal_value=order.state.value, broker_value=None))
                continue

            matched.add(view.broker_order_id)
            self._compare_one(record, order, view)

        record.checks_performed += 1
        for view in broker:
            if view.broker_order_id in matched:
                continue
            if any(o.broker_order_id == view.broker_order_id for o in internal):
                continue
            record.mismatches.append(ReconciliationMismatch(
                kind=MismatchKind.UNKNOWN_BROKER_ORDER,
                detail=(f"the broker reports order {view.broker_order_id} "
                        f"({view.broker_symbol}, {view.state.value}) that we have "
                        f"no record of"),
                broker_order_id=view.broker_order_id,
                instrument_id=view.instrument_id,
                internal_value=None, broker_value=view.state.value))

    def _compare_one(self, record: ReconciliationRecord,
                     order: ExecutionOrder, view: BrokerOrderView) -> None:
        if state_class(order.state) != state_class(view.state):
            record.mismatches.append(ReconciliationMismatch(
                kind=MismatchKind.STATUS_MISMATCH,
                detail=(f"order {order.order_id}: we say {order.state.value} "
                        f"({state_class(order.state)}), the broker says "
                        f"{view.state.value} ({state_class(view.state)})"),
                order_id=order.order_id, broker_order_id=view.broker_order_id,
                internal_value=order.state.value, broker_value=view.state.value))

        if abs(order.filled_quantity - view.filled_quantity) > self.quantity_tolerance:
            record.mismatches.append(ReconciliationMismatch(
                kind=MismatchKind.QUANTITY_MISMATCH,
                detail=(f"order {order.order_id}: we have "
                        f"{order.filled_quantity:g} filled, the broker has "
                        f"{view.filled_quantity:g}"),
                order_id=order.order_id, broker_order_id=view.broker_order_id,
                internal_value=order.filled_quantity,
                broker_value=view.filled_quantity))

        ours, theirs = order.average_fill_price, view.average_fill_price
        if ours and theirs:
            drift_bps = abs(ours - theirs) / abs(theirs) * 10_000.0
            if drift_bps > self.price_tolerance_bps:
                record.mismatches.append(ReconciliationMismatch(
                    kind=MismatchKind.PRICE_MISMATCH,
                    detail=(f"order {order.order_id}: average fill {ours:.6f} "
                            f"vs broker {theirs:.6f} ({drift_bps:.1f} bps apart)"),
                    order_id=order.order_id,
                    broker_order_id=view.broker_order_id,
                    internal_value=ours, broker_value=theirs))

    # ---------------- fills ----------------

    def _check_fills(self, record: ReconciliationRecord,
                     fills: Sequence[ExecutionFill]) -> None:
        """
        Duplicate detection over our own fill history.

        Runs even though `EventProcessor` deduplicates on the way in,
        because a duplicate that reached storage by some other route —
        a replayed import, a double-write — is exactly the kind of
        thing a periodic check exists to catch.
        """
        record.checks_performed += 1
        seen: Dict[str, ExecutionFill] = {}
        for fill in fills:
            key = fill.idempotency_key or fill.execution_id or fill.fill_id
            if key in seen:
                record.mismatches.append(ReconciliationMismatch(
                    kind=MismatchKind.DUPLICATE_FILL,
                    detail=(f"fills {seen[key].fill_id} and {fill.fill_id} share "
                            f"execution key {key}"),
                    order_id=fill.order_id, instrument_id=fill.instrument_id,
                    internal_value=key))
            else:
                seen[key] = fill

    # ---------------- positions ----------------

    def _check_positions(self, record: ReconciliationRecord,
                         internal: Dict[str, float],
                         broker: Sequence[PositionSnapshot]) -> None:
        record.checks_performed += 1
        broker_by_instrument = {p.instrument_id: p for p in broker}

        for instrument_id in sorted(set(internal) | set(broker_by_instrument)):
            ours = float(internal.get(instrument_id, 0.0))
            position = broker_by_instrument.get(instrument_id)
            theirs = float(position.quantity) if position else 0.0
            if abs(ours - theirs) <= self.quantity_tolerance:
                continue
            record.mismatches.append(ReconciliationMismatch(
                kind=MismatchKind.POSITION_MISMATCH,
                detail=(f"{instrument_id}: we hold {ours:g}, the broker holds "
                        f"{theirs:g}"),
                instrument_id=instrument_id,
                internal_value=ours, broker_value=theirs))

    # ---------------- cash ----------------

    def _check_cash(self, record: ReconciliationRecord,
                    internal_cash: Optional[float],
                    account: Optional[AccountSnapshot]) -> None:
        record.checks_performed += 1
        if internal_cash is None or account is None:
            return
        if abs(internal_cash - account.cash) > self.cash_tolerance:
            record.mismatches.append(ReconciliationMismatch(
                kind=MismatchKind.CASH_MISMATCH,
                detail=(f"cash: we hold {internal_cash:,.4f}, the broker reports "
                        f"{account.cash:,.4f}"),
                internal_value=internal_cash, broker_value=account.cash))

    # ---------------- unknown states ----------------

    def _check_unknown(self, record: ReconciliationRecord,
                       orders: Sequence[ExecutionOrder]) -> None:
        record.checks_performed += 1
        for order in orders:
            if order.state.needs_reconciliation:
                record.mismatches.append(ReconciliationMismatch(
                    kind=MismatchKind.UNKNOWN_STATE,
                    detail=(f"order {order.order_id} is {order.state.value} and "
                            f"needs an answer from the broker"),
                    order_id=order.order_id,
                    broker_order_id=order.broker_order_id,
                    internal_value=order.state.value))

    # ---------------- the timeout resolution ----------------

    def resolve_unknown_orders(self, gateway: BrokerGateway,
                               orders: Sequence[ExecutionOrder],
                               machine: OrderStateMachine,
                               at: datetime) -> List[UnknownResolution]:
        """
        Ask the broker about every unknown order (spec §24).

        Never resubmits, under any outcome. The three cases:

          the broker knows it       -> adopt the broker's state
          the broker never saw it   -> FAILED, which is safe: a venue
                                       with no record will not fill it
          we cannot ask             -> stays UNKNOWN and is reported

        The third is the one worth preserving. A resolution pass that
        could not reach the venue has learned nothing, and recording
        "unresolved" is the only honest outcome.
        """
        resolutions: List[UnknownResolution] = []

        for order in orders:
            if not order.state.needs_reconciliation:
                continue

            lookup_id = order.broker_order_id or order.client_order_id
            if not lookup_id:
                # Submitted, timed out, no id came back. The client id
                # is the only handle, and without it the order can only
                # be found by scanning the venue's open orders.
                resolutions.append(UnknownResolution(
                    order.order_id, False, order.state,
                    "no broker or client order id to query with"))
                continue

            try:
                view = gateway.get_order(lookup_id)
            except Exception as error:                    # noqa: BLE001
                resolutions.append(UnknownResolution(
                    order.order_id, False, order.state,
                    f"broker query failed: {error}"))
                continue

            if view is None:
                machine.force(
                    order, ExecutionOrderState.FAILED, at,
                    reason="the broker has no record of this order",
                    correlation_id=order.correlation_id)
                resolutions.append(UnknownResolution(
                    order.order_id, True, ExecutionOrderState.FAILED,
                    "the broker has no record of it; it cannot fill"))
                continue

            machine.force(
                order, view.state, at,
                reason=f"broker reports {view.state.value}",
                correlation_id=order.correlation_id)
            order.broker_order_id = view.broker_order_id
            if view.filled_quantity > order.filled_quantity:
                # The venue filled more than we recorded. Adopt the
                # quantity, and let the position check report that the
                # fills behind it are missing — the quantity is a fact,
                # the missing fills are the finding.
                order.filled_quantity = view.filled_quantity
                order.average_fill_price = view.average_fill_price
            resolutions.append(UnknownResolution(
                order.order_id, True, view.state,
                f"broker reports {view.state.value}",
                broker_order_id=view.broker_order_id))

        return resolutions
