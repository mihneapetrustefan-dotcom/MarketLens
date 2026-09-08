"""
src/trading/targets.py
----------------------------
CURRENT PORTFOLIO -> TARGET PORTFOLIO -> ORDER INTENT (§5, §7, §16).

THE THREE NUMBERS
---------------------
    target    what the portfolio layer decided we should hold
    actual    what the broker says we hold, after reconciliation
    pending   what we have already asked for and not yet received

    outstanding = target - actual - pending

Spec §16's worked example is the reason this module exists as its own
file: TARGET +100, BROKER +40, and the system must recognise the
remaining +60 rather than reporting +100 as a holding. `PositionDelta`
carries all four numbers so that a reader never has to reconstruct one
from the others.

WHY `pending` IS SUBTRACTED
-------------------------------
Without it, a loop that ticks every fifteen minutes while an order is
working re-proposes the same trade on every tick, and by the time the
first fills the account holds four times the target. §7 lists
"already-pending orders" among the cases that must be handled, and this
subtraction is that handling.

WHY THE BROKER'S POSITIONS ARE FED BACK INTO PHASE 11
---------------------------------------------------------
`PortfolioService.evaluate(positions=..., cash=...)` accepts a book it
does not store — the seam Phase 12 uses to run the real risk engine
over a simulated portfolio. Phase 25 uses the same seam with the
RECONCILED BROKER BOOK, which means:

  * Phase 11's `current_weight` is what we really hold, not what we
    think we hold, so `weight_delta` and therefore the intent's SIDE
    are computed against reality;
  * a position that appeared at the broker without a local order
    (§12's "unknown broker orders") immediately constrains the next
    decision instead of being invisible until reconciliation runs.

There is no second sizing implementation here. Sizing belongs to
Phase 11 and this module only differences the result.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from src.domain.broker_models import ExecutionOrder, PositionSnapshot
from src.domain.portfolio_models import (
    OrderIntent, Position, PositionSource, PositionStatus, RiskDecision,
)
from src.domain.trading_loop_models import (
    ActualPosition, PositionDelta, PositionOrigin, TargetPosition, require_utc,
)

#: Quantities smaller than this are treated as zero. Shares are whole
#: numbers at IBKR for the instruments here, but the arithmetic runs in
#: floats and a target of 25 minus an actual of 25 must not leave a
#: 3.55e-15 order behind.
QUANTITY_TOLERANCE = 1e-6


def positions_from_broker(snapshots: Sequence[PositionSnapshot],
                          portfolio_id: str,
                          as_of: datetime) -> List[Position]:
    """
    Translate reconciled broker positions into the Phase 11 shape.

    `source=PositionSource.BROKER` is the important field, and Phase 25
    is what made it truthful: the member did not exist before this
    phase because nothing could produce one. Losing the distinction
    would let a broker-derived book be read as a locally-asserted one,
    which is the specific confusion §4 forbids.
    """
    require_utc(as_of, "as_of")
    out: List[Position] = []
    for snapshot in snapshots:
        if abs(snapshot.quantity) <= QUANTITY_TOLERANCE:
            continue
        out.append(Position(
            position_id=f"pos-{portfolio_id}-{snapshot.instrument_id}",
            portfolio_id=portfolio_id,
            instrument_id=snapshot.instrument_id,
            quantity=float(snapshot.quantity),
            average_entry_price=snapshot.average_price or None,
            currency=snapshot.currency or "USD",
            status=PositionStatus.OPEN,
            source=PositionSource.BROKER,
            opened_at=None,
            realized_pnl=snapshot.realized_pnl,
            metadata={"broker_id": snapshot.broker_id,
                      "account_id": snapshot.account_id,
                      "observed_at": (snapshot.at.isoformat()
                                      if snapshot.at else None)},
        ))
    return out


def actuals_from_broker(snapshots: Sequence[PositionSnapshot],
                        cycle_id: str, at: datetime,
                        reconciled: bool = True) -> List[ActualPosition]:
    """
    The broker's positions, marked with where they came from.

    `reconciled=False` produces rows with origin UNKNOWN — used when
    the gateway answered but reconciliation did not run or did not
    agree. Those rows must never be shown as holdings, and
    `ActualPosition.is_authoritative` is the predicate that says so.
    """
    require_utc(at, "at")
    origin = (PositionOrigin.BROKER_RECONCILED if reconciled
              else PositionOrigin.UNKNOWN)
    return [ActualPosition(
        cycle_id=cycle_id, instrument_id=s.instrument_id,
        quantity=float(s.quantity), average_price=s.average_price,
        market_price=s.market_price, unrealized_pnl=s.unrealized_pnl,
        realized_pnl=s.realized_pnl, origin=origin,
        account_id=s.account_id, broker_id=s.broker_id,
        observed_at=s.at or at) for s in snapshots]


def targets_from_intents(intents: Sequence[OrderIntent], cycle_id: str,
                         decision: Optional[RiskDecision],
                         prices: Dict[str, float],
                         at: datetime) -> List[TargetPosition]:
    """
    Record what the portfolio layer decided, before anything is ordered.

    Written even when execution later refuses every one of them. A
    target that produced no order is evidence about the risk and
    validation layers; discarding it would make the loop's own
    conversion rate unmeasurable.
    """
    require_utc(at, "at")
    out: List[TargetPosition] = []
    for intent in intents:
        out.append(TargetPosition(
            cycle_id=cycle_id,
            instrument_id=intent.instrument_id,
            target_quantity=intent.target_quantity,
            target_weight=intent.target_weight,
            reference_price=prices.get(intent.instrument_id),
            signal_id=intent.source_signal_id,
            decision_id=intent.decision_id or (
                decision.decision_id if decision else None),
            portfolio_id=intent.portfolio_id,
            reason=intent.reason,
            decided_at=at))
    return out


def pending_quantities(orders: Iterable[ExecutionOrder]) -> Dict[str, float]:
    """
    Signed quantity already asked for and not yet received.

    Counts only orders in a state that can still fill. A cancelled or
    rejected order contributes nothing, and an order counted after it
    terminated would suppress the replacement trade forever.
    """
    pending: Dict[str, float] = {}
    for order in orders:
        state = getattr(order, "state", None)
        if state is not None and getattr(state, "is_terminal", False):
            continue
        requested = float(getattr(order, "quantity", 0.0) or 0.0)
        filled = float(getattr(order, "filled_quantity", 0.0) or 0.0)
        remaining = requested - filled
        if abs(remaining) <= QUANTITY_TOLERANCE:
            continue
        side = getattr(order, "side", None)
        signed = remaining if str(getattr(side, "value", side)) == "buy" else -remaining
        key = order.instrument_id
        pending[key] = pending.get(key, 0.0) + signed
    return pending


def combined_pending(broker_orders: Iterable[Any],
                     our_orders: Iterable[Any]) -> Dict[str, float]:
    """
    Pending quantity from BOTH sides, taking the larger magnitude.

    WHY NOT JUST ASK THE BROKER
    -------------------------------
    Because a broker that answers "no open orders" — a fresh session, a
    gateway that lost its view, an API hiccup, a reconnect — would make
    the loop re-propose a trade it has already placed, and the account
    would end up holding twice the target. That is not hypothetical:
    the Phase 25 test suite reproduced it the first time a second
    process picked up an existing order, and the broker double returned
    an empty book while our own record held the order.

    So the BROKER is authoritative for what exists, and OUR RECORD is
    the conservative floor for what we must not re-order. For
    SUPPRESSING new exposure the conservative side has to win, because
    the two failure modes are not symmetric: over-counting pending
    delays a trade by one cycle, under-counting doubles a position.

    Same-instrument entries with opposite signs keep the larger
    magnitude rather than netting — netting a +100 we hold against a
    -100 the broker forgot would produce zero, which is the answer with
    the worst consequences.
    """
    from_broker = pending_quantities(broker_orders)
    from_us = pending_quantities(our_orders)
    merged: Dict[str, float] = {}
    for instrument in set(from_broker) | set(from_us):
        theirs = from_broker.get(instrument, 0.0)
        ours = from_us.get(instrument, 0.0)
        merged[instrument] = theirs if abs(theirs) >= abs(ours) else ours
    return merged


@dataclass
class DeltaRejection:
    """One target that will not become an order, and why."""
    instrument_id: str
    reason: str
    detail: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"instrument_id": self.instrument_id, "reason": self.reason,
                "detail": self.detail}


@dataclass
class DeltaSet:
    """
    Deltas, the trade quantities they imply, and what was dropped.

    `quantities` is keyed by instrument and holds POSITIVE magnitudes,
    which is the shape `intake.from_decision` expects: it takes the
    side from the intent and the size from here. Handing it a signed
    number would make a sell of 40 into a buy of -40 somewhere down the
    chain, and the failure would be silent.
    """
    deltas: List[PositionDelta]
    quantities: Dict[str, float]
    rejected: List[DeltaRejection]

    @property
    def actionable(self) -> List[PositionDelta]:
        return [d for d in self.deltas if not d.is_noop]

    def as_dict(self) -> Dict[str, Any]:
        return {"deltas": [d.as_dict() for d in self.deltas],
                "quantities": dict(self.quantities),
                "rejected": [r.as_dict() for r in self.rejected]}


def compute_deltas(targets: Sequence[TargetPosition],
                   actuals: Sequence[ActualPosition],
                   pending: Optional[Dict[str, float]] = None,
                   intents: Optional[Sequence[OrderIntent]] = None,
                   mappings: Optional[Dict[str, Any]] = None,
                   tolerance: float = QUANTITY_TOLERANCE) -> DeltaSet:
    """
    Difference target against actual, and produce trade quantities.

    Every case §7 names is handled here and named in `PositionDelta.action`:
    no-op, open, increase, reduce, close, reverse, no target.

    A target with no quantity is REJECTED rather than assumed. Phase 11
    may return a weight without a quantity when it has no price, and
    turning a weight into a share count is sizing — which belongs to
    Phase 11, not here. Inventing one would be a second sizing
    implementation, quietly disagreeing with the first.

    A delta whose direction contradicts its intent's side is also
    rejected, with both named. That happens when the broker book moved
    between the decision and this computation, and the honest answer is
    to let the next cycle re-decide against the new book rather than to
    flip a side the risk engine never approved.

    `mappings` supplies the venue's own quantity rules per instrument,
    and `BrokerInstrumentMapping.normalize_quantity` does the rounding
    -- Phase 14's function, not a second copy. It floors, never rounds
    up: rounding up would trade MORE than the risk engine approved, and
    the difference would be invisible in every record afterwards.

    Without this the loop produced a SELL of 0.1255 shares on its
    second cycle -- a 10% weight target against an already-held
    position -- which the validator refused for QUANTITY_INCREMENT
    three layers from the arithmetic that made it. A gap the venue is
    too coarse to close is NO trade, and `PositionDelta.below_minimum`
    now says so where the number is produced.
    """
    pending = dict(pending or {})
    mappings = dict(mappings or {})
    actual_by_instrument = {a.instrument_id: a for a in actuals}
    side_by_instrument = {i.instrument_id: str(i.side).lower()
                          for i in (intents or [])}

    deltas: List[PositionDelta] = []
    quantities: Dict[str, float] = {}
    rejected: List[DeltaRejection] = []

    for target in targets:
        instrument = target.instrument_id
        actual = actual_by_instrument.get(instrument)
        held = actual.quantity if actual else 0.0

        if target.target_quantity is None:
            rejected.append(DeltaRejection(
                instrument_id=instrument, reason="no_target_quantity",
                detail="the portfolio layer produced a weight but no share "
                       "count; sizing belongs to Phase 11 and this layer "
                       "will not invent one"))
            continue

        if actual is not None and not actual.is_authoritative:
            rejected.append(DeltaRejection(
                instrument_id=instrument, reason="position_not_reconciled",
                detail=f"the held quantity came from "
                       f"{actual.origin.value}, which is not authoritative; "
                       f"§25 says an unknown portfolio state does not trade"))
            continue

        mapping = mappings.get(instrument)
        minimum = 0.0
        if mapping is not None:
            minimum = max(float(getattr(mapping, "minimum_quantity", 0.0) or 0.0),
                          float(getattr(mapping, "quantity_increment", 0.0) or 0.0))

        delta = PositionDelta(
            instrument_id=instrument,
            target_quantity=target.target_quantity,
            actual_quantity=held,
            pending_quantity=pending.get(instrument, 0.0),
            tolerance=tolerance, min_quantity=minimum)
        deltas.append(delta)

        if delta.below_minimum:
            rejected.append(DeltaRejection(
                instrument_id=instrument, reason="below_broker_minimum",
                detail=f"the gap of {delta.outstanding:+.4g} is smaller than "
                       f"the venue's minimum tradeable quantity of {minimum:g}"))
            continue

        if delta.is_noop:
            continue

        implied = delta.side
        declared = side_by_instrument.get(instrument)
        if declared and implied and declared != implied:
            rejected.append(DeltaRejection(
                instrument_id=instrument, reason="side_disagreement",
                detail=f"the approved intent says {declared} but the book "
                       f"now implies {implied}; the next cycle will re-decide "
                       f"against the current positions rather than flipping a "
                       f"side risk did not approve"))
            continue

        wanted = abs(delta.outstanding or 0.0)
        if mapping is not None:
            normalized, code = mapping.normalize_quantity(wanted)
            if code is not None:
                rejected.append(DeltaRejection(
                    instrument_id=instrument, reason=code.value,
                    detail=f"{wanted:.4g} does not satisfy the venue's "
                           f"quantity rules"))
                continue
            wanted = normalized
        quantities[instrument] = wanted

    return DeltaSet(deltas=deltas, quantities=quantities, rejected=rejected)
