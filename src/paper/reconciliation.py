"""
src/paper/reconciliation.py
--------------------------------
Do the orders, fills, positions and cash still agree?
(Phase 13, spec §32, §62, §78)

WHAT THIS CATCHES THAT TESTS CANNOT
---------------------------------------
Unit tests prove the ledger applies one fill correctly. Reconciliation
proves that after four hundred fills across a restarted process, the
positions still equal the sum of what was filled and the cash still
equals what was spent.

Those are different claims. The second can fail from a duplicate
message, a dropped write, a crash between the fill and the ledger
update, or a restart that replayed a tick — none of which a unit test
sees, and all of which are ordinary in a scheduled system that stops
and starts.

DISCREPANCIES ARE REPORTED, NEVER SILENTLY REPAIRED
-------------------------------------------------------
Spec §32 is explicit. A system that quietly corrects its own books
destroys the evidence of what went wrong, and the corruption usually
recurs. So this module computes, compares and records — and the caller
decides what to do, which for a paper session is to raise an alert and
enter safe mode rather than to trade on numbers that do not add up.

A CLEAN RESULT IS RECORDED AS LOUDLY AS A DIRTY ONE
-------------------------------------------------------
`checks_performed` is on the result for the same reason Phase 12's
temporal guard counts its checks: "we verified and it balanced" and "we
never verified" must not look identical in the record.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence

from src.backtest.accounting import PortfolioLedger
from src.domain.paper_models import (
    OrderSide, PaperFill, PaperOrder, PaperOrderState, ReconciliationResult,
    finite_or_none,
)
from src.paper.clock import require_utc

#: Tolerance for float comparison. Quantities and cash accumulate over
#: many operations, so exact equality would report noise as corruption.
QUANTITY_TOLERANCE = 1e-6
CASH_TOLERANCE = 1e-4


class Reconciler:
    """Checks the ledger's views against each other after execution events."""

    def __init__(self, initial_capital: float):
        self.initial_capital = float(initial_capital)

    # ---------------- the check ----------------

    def reconcile(self, session_id: str, at: datetime,
                  orders: Sequence[PaperOrder], fills: Sequence[PaperFill],
                  ledger: PortfolioLedger) -> ReconciliationResult:
        """
        Verify orders ↔ fills ↔ positions ↔ cash.

        Five independent checks, each able to fail on its own, because a
        single "does it balance" boolean would tell you something is
        wrong without telling you where.
        """
        require_utc(at, "at")
        result = ReconciliationResult(
            at=at, session_id=session_id,
            orders_examined=len(orders), fills_examined=len(fills),
            positions_examined=len(ledger.open_positions()))

        self._check_fills_belong_to_orders(result, orders, fills)
        self._check_filled_quantity_matches(result, orders, fills)
        self._check_positions_match_fills(result, fills, ledger)
        self._check_cash_matches_fills(result, fills, ledger)
        self._check_no_impossible_state(result, ledger)

        return result

    # ---------------- individual checks ----------------

    def _check_fills_belong_to_orders(self, result: ReconciliationResult,
                                      orders: Sequence[PaperOrder],
                                      fills: Sequence[PaperFill]) -> None:
        result.checks_performed += 1
        known = {o.order_id for o in orders}
        for fill in fills:
            if fill.order_id not in known:
                result.add("orphan_fill",
                           f"fill {fill.fill_id} references unknown order "
                           f"{fill.order_id}",
                           instrument_id=fill.instrument_id)

    def _check_filled_quantity_matches(self, result: ReconciliationResult,
                                       orders: Sequence[PaperOrder],
                                       fills: Sequence[PaperFill]) -> None:
        result.checks_performed += 1
        by_order: Dict[str, float] = defaultdict(float)
        for fill in fills:
            by_order[fill.order_id] += fill.quantity

        for order in orders:
            recorded = order.filled_quantity
            from_fills = by_order.get(order.order_id, 0.0)
            if abs(recorded - from_fills) > QUANTITY_TOLERANCE:
                result.add("order_fill_mismatch",
                           f"order {order.order_id} records {recorded:.8f} filled "
                           f"but its fills sum to {from_fills:.8f}",
                           instrument_id=order.instrument_id,
                           expected=from_fills, actual=recorded)
            if from_fills - order.quantity > QUANTITY_TOLERANCE:
                result.add("overfill",
                           f"order {order.order_id} filled {from_fills:.8f} of "
                           f"{order.quantity:.8f} requested",
                           instrument_id=order.instrument_id,
                           expected=order.quantity, actual=from_fills)

    def _check_positions_match_fills(self, result: ReconciliationResult,
                                     fills: Sequence[PaperFill],
                                     ledger: PortfolioLedger) -> None:
        """
        Net signed fills per instrument must equal the held quantity.

        This is the check that catches a duplicate fill: applying one
        twice leaves the position larger than the fills justify.
        """
        result.checks_performed += 1
        expected: Dict[str, float] = defaultdict(float)
        for fill in fills:
            expected[fill.instrument_id] += fill.signed_quantity

        held = {p.instrument_id: p.quantity for p in ledger.open_positions()}
        for instrument_id in set(expected) | set(held):
            want = expected.get(instrument_id, 0.0)
            have = held.get(instrument_id, 0.0)
            if abs(want - have) > QUANTITY_TOLERANCE:
                result.add("position_mismatch",
                           f"{instrument_id}: fills net to {want:.8f} but the "
                           f"ledger holds {have:.8f}",
                           instrument_id=instrument_id,
                           expected=want, actual=have)

    def _check_cash_matches_fills(self, result: ReconciliationResult,
                                  fills: Sequence[PaperFill],
                                  ledger: PortfolioLedger) -> None:
        """
        Cash must equal starting capital minus everything spent.

        Derived independently from the fills rather than trusting the
        ledger's running balance — the point is to catch the running
        balance being wrong.
        """
        result.checks_performed += 1
        expected = self.initial_capital
        for fill in fills:
            expected -= fill.signed_quantity * fill.price
            expected -= fill.total_cost

        if abs(expected - ledger.cash) > CASH_TOLERANCE:
            result.add("cash_mismatch",
                       f"fills imply {expected:,.6f} cash but the ledger holds "
                       f"{ledger.cash:,.6f}",
                       expected=expected, actual=ledger.cash)

    def _check_no_impossible_state(self, result: ReconciliationResult,
                                   ledger: PortfolioLedger) -> None:
        """
        States that should be unreachable (spec §76, case 10).

        Negative cash is possible with margin, but this system has no
        margin model and refuses buys it cannot fund — so negative cash
        here means the accounting is wrong, not that leverage was used.
        """
        result.checks_performed += 1
        if ledger.cash < -CASH_TOLERANCE:
            result.add("negative_cash",
                       f"cash is {ledger.cash:,.6f}; this system funds every buy "
                       f"before filling it, so a negative balance is an "
                       f"accounting fault rather than borrowing",
                       expected=0.0, actual=ledger.cash)

        for position in ledger.open_positions():
            if position.average_cost is not None and position.average_cost < 0:
                result.add("negative_average_cost",
                           f"{position.instrument_id} has a negative average cost "
                           f"of {position.average_cost:.6f}",
                           instrument_id=position.instrument_id,
                           actual=position.average_cost)

    # ---------------- reporting ----------------

    def describe(self, result: ReconciliationResult) -> Dict[str, object]:
        by_kind: Dict[str, int] = {}
        for discrepancy in result.discrepancies:
            by_kind[discrepancy.kind] = by_kind.get(discrepancy.kind, 0) + 1
        return {
            "at": result.at.isoformat(),
            "clean": result.is_clean,
            "checks_performed": result.checks_performed,
            "orders_examined": result.orders_examined,
            "fills_examined": result.fills_examined,
            "positions_examined": result.positions_examined,
            "discrepancies": len(result.discrepancies),
            "by_kind": by_kind,
        }
