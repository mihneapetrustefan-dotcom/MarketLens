"""
src/trading/outcomes.py
-----------------------------
Paper P&L, and turning fills into trade outcomes with their lineage
(§17, §18, §23).

NOTHING NEW IS INVENTED HERE
--------------------------------
Phase 16 already defined `TradeOutcome`, `TradeLineage`,
`ExecutionQuality`, `lineage_from_order`, `quality_from_order` and
`classify_errors`, and `GovernanceRepository.save_outcome` already
writes them. All of it has sat unexercised since Phase 16 because no
order had ever been placed. This module is the producer that was
missing, not a second definition.

TWO KINDS OF P&L, NEVER MIXED (§17)
---------------------------------------
    BROKER-REPORTED     what IBKR says the account is worth
    LOCALLY-CALCULATED  what our own fills add up to

`PaperPnL` keeps both, with the source on each. They are expected to
agree and the interesting case is when they do not, which is a
reconciliation finding rather than a number to average away.

A ROUND TRIP IS A ROUND TRIP (§18)
--------------------------------------
An outcome is created OPEN when a position is established and closed
when the signed quantity returns to zero. An open outcome carries an
entry and no exit, and `is_open` says so — a closed-looking row with a
None exit price would silently become a zero-return trade in every
later aggregate.

WHY A LOSING TRADE IS STILL NOT AN ERROR (§19)
--------------------------------------------------
`classify_errors` is Phase 16's and it sets exactly three fields that
can be established mechanically. This module calls it and adds
nothing. The temptation to write `if net_pnl < 0: error` is the one
thing §19 names explicitly, and the way to not do it is to have no
code here that could.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from src.data_access.governance_repository import GovernanceRepository
from src.data_access.governance_schema import initialize_governance_schema
from src.domain.broker_models import (
    CanonicalOrderSide, ExecutionFill, ExecutionOrder,
)
from src.execution.outcomes import (
    ExitReason, TradeOutcome, classify_errors, lineage_from_order,
    quality_from_order,
)
from src.domain.trading_loop_models import finite_or_none, require_utc

#: Below this a position counts as flat. Same tolerance the delta
#: arithmetic uses, for the same reason.
QUANTITY_TOLERANCE = 1e-6



@dataclass
class PnLComponent:
    """
    One P&L figure with the source that produced it.

    `source` is required. Spec §17 says do not mix estimated and
    authoritative numbers without labelling, and a default would be a
    label nobody chose.
    """
    value: Optional[float]
    source: str
    detail: str = ""

    def __post_init__(self):
        self.value = finite_or_none(self.value)
        if not self.source:
            raise ValueError("a P&L figure must say where it came from")


@dataclass
class PaperPnL:
    """
    The account's P&L, from both sides, kept apart (§17).

    `agrees_within` is the comparison that matters. It returns None
    when either side is missing, because "we could not compare" and
    "they agree" must not read the same.
    """
    at: Optional[datetime] = None
    broker_realized: Optional[PnLComponent] = None
    broker_unrealized: Optional[PnLComponent] = None
    local_realized: Optional[PnLComponent] = None
    local_unrealized: Optional[PnLComponent] = None
    equity: Optional[PnLComponent] = None
    cash: Optional[PnLComponent] = None
    gross_exposure: Optional[float] = None
    turnover: Optional[float] = None
    fees: float = 0.0

    def __post_init__(self):
        require_utc(self.at, "at")

    def agrees_within(self, tolerance: float = 0.01) -> Optional[bool]:
        pairs = [(self.broker_realized, self.local_realized),
                 (self.broker_unrealized, self.local_unrealized)]
        compared = False
        for broker, local in pairs:
            if broker is None or local is None:
                continue
            if broker.value is None or local.value is None:
                continue
            compared = True
            if abs(broker.value - local.value) > tolerance:
                return False
        return True if compared else None

    def as_dict(self) -> Dict[str, Any]:
        def one(component: Optional[PnLComponent]) -> Optional[Dict[str, Any]]:
            if component is None:
                return None
            return {"value": component.value, "source": component.source,
                    "detail": component.detail}
        return {"at": self.at.isoformat() if self.at else None,
                "broker_realized": one(self.broker_realized),
                "broker_unrealized": one(self.broker_unrealized),
                "local_realized": one(self.local_realized),
                "local_unrealized": one(self.local_unrealized),
                "equity": one(self.equity), "cash": one(self.cash),
                "gross_exposure": self.gross_exposure,
                "turnover": self.turnover, "fees": self.fees,
                "sources_agree": self.agrees_within()}


def compute_pnl(account: Optional[Any], fills: Sequence[ExecutionFill],
                positions: Sequence[Any], at: datetime,
                marks: Optional[Dict[str, float]] = None) -> PaperPnL:
    """
    Both sides of the P&L, from the account snapshot and from our fills.

    The local side is deliberately naive average-cost accounting over
    our own fills. It is not meant to beat the broker's number; it is
    meant to DISAGREE with it when something is wrong, which a number
    copied from the broker never could.
    """
    require_utc(at, "at")
    marks = marks or {}

    book = _replay_fills(fills)
    local_realized = sum(entry.realized for entry in book.values())
    local_fees = sum(f.total_cost for f in fills)
    local_unrealized: Optional[float] = 0.0
    gross = 0.0
    for instrument_id, entry in book.items():
        if abs(entry.quantity) <= QUANTITY_TOLERANCE:
            continue
        mark = marks.get(instrument_id)
        if mark is None:
            local_unrealized = None
            continue
        gross += abs(entry.quantity) * mark
        if local_unrealized is not None:
            local_unrealized += (mark - entry.average_price) * entry.quantity

    result = PaperPnL(
        at=at,
        local_realized=PnLComponent(local_realized, "locally-calculated",
                                    "average cost over our own fills"),
        local_unrealized=(
            PnLComponent(local_unrealized, "locally-calculated",
                         "marked at the supplied prices")
            if local_unrealized is not None else
            PnLComponent(None, "locally-calculated",
                         "no mark for at least one open position")),
        gross_exposure=gross or None,
        turnover=sum(f.notional for f in fills) or None,
        fees=local_fees)

    if account is not None:
        result.broker_realized = PnLComponent(
            getattr(account, "realized_pnl", None), "broker-reported",
            "from the account snapshot")
        result.broker_unrealized = PnLComponent(
            getattr(account, "unrealized_pnl", None), "broker-reported",
            "from the account snapshot")
        result.equity = PnLComponent(getattr(account, "equity", None),
                                     "broker-reported", "")
        result.cash = PnLComponent(getattr(account, "cash", None),
                                   "broker-reported", "")
    return result


@dataclass
class _BookEntry:
    quantity: float = 0.0
    average_price: float = 0.0
    realized: float = 0.0
    opened_at: Optional[datetime] = None
    entry_price: Optional[float] = None


def _replay_fills(fills: Sequence[ExecutionFill]) -> Dict[str, _BookEntry]:
    """
    Average-cost position accounting over our own fills, in time order.

    Ordered by `filled_at` and then by `fill_id` so a replay is
    deterministic. Two fills at the same microsecond in a different
    order would otherwise produce a different average price, and the
    P&L would depend on iteration order.
    """
    book: Dict[str, _BookEntry] = {}
    for fill in sorted(fills, key=lambda f: (f.filled_at or datetime.min.replace(
            tzinfo=timezone.utc), f.fill_id)):
        entry = book.setdefault(fill.instrument_id, _BookEntry())
        signed = fill.signed_quantity
        if abs(entry.quantity) <= QUANTITY_TOLERANCE:
            entry.quantity = signed
            entry.average_price = fill.price
            entry.opened_at = fill.filled_at
            entry.entry_price = fill.price
        elif (entry.quantity > 0) == (signed > 0):
            total = entry.quantity + signed
            entry.average_price = (
                (entry.average_price * entry.quantity + fill.price * signed)
                / total) if total else fill.price
            entry.quantity = total
        else:
            closing = min(abs(signed), abs(entry.quantity))
            direction = 1.0 if entry.quantity > 0 else -1.0
            entry.realized += (fill.price - entry.average_price) * closing * direction
            entry.quantity += signed
            if abs(entry.quantity) <= QUANTITY_TOLERANCE:
                entry.quantity = 0.0
            elif (entry.quantity > 0) != (direction > 0):
                # Reversed through zero: what is left is a new position
                # at the closing fill's price.
                entry.average_price = fill.price
                entry.opened_at = fill.filled_at
                entry.entry_price = fill.price
    return book


# ======================================================================
# Trade outcomes (§18)
# ======================================================================

def outcome_id_for(order: ExecutionOrder) -> str:
    """
    Deterministic outcome identity, keyed on the order.

    Not a uuid: a cycle that re-derives outcomes must REPLACE the row
    for a trade rather than adding a second one. Phase 23 made the uuid
    mistake with observation ids and it cost a whole debugging pass.
    """
    raw = f"{order.order_id}|{order.intent_id}"
    return "to-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def build_outcomes(orders: Sequence[ExecutionOrder],
                   fills: Sequence[ExecutionFill],
                   *, session_id: Optional[str] = None,
                   marks: Optional[Dict[str, float]] = None,
                   code_version: str = "phase25-v1",
                   strategy_version: Optional[str] = None,
                   ) -> List[TradeOutcome]:
    """
    One outcome per FILLED order, open or closed.

    An order with no fill produces nothing — there is no trade to have
    an outcome about, and a zero-quantity outcome would dilute every
    later average. An order that was rejected is a MissedTrade in
    Phase 16's vocabulary, which the caller records separately.

    `is_open` is set from whether the instrument's net position is
    still non-zero after this fill, so a buy that has not been sold
    reads OPEN with an entry and no exit — the honest shape.
    """
    marks = marks or {}
    by_order: Dict[str, List[ExecutionFill]] = {}
    for fill in fills:
        by_order.setdefault(fill.order_id, []).append(fill)

    book = _replay_fills(fills)
    outcomes: List[TradeOutcome] = []

    for order in orders:
        order_fills = by_order.get(order.order_id) or []
        if not order_fills:
            continue

        filled_quantity = sum(f.quantity for f in order_fills)
        entry_price = (sum(f.price * f.quantity for f in order_fills)
                       / filled_quantity) if filled_quantity else None
        entry_at = min((f.filled_at for f in order_fills if f.filled_at),
                       default=None)

        entry = book.get(order.instrument_id)
        still_open = bool(entry and abs(entry.quantity) > QUANTITY_TOLERANCE)
        mark = marks.get(order.instrument_id)

        exit_price = None if still_open else mark
        gross = None
        if entry is not None and not still_open:
            gross = entry.realized
        elif mark is not None and entry_price is not None:
            gross = ((mark - entry_price) * filled_quantity
                     * (1.0 if order.side is CanonicalOrderSide.BUY else -1.0))

        outcome = TradeOutcome(
            outcome_id=outcome_id_for(order),
            instrument_id=order.instrument_id,
            side=order.side,
            quantity=filled_quantity,
            lineage=lineage_from_order(
                order, session_id=session_id, fills=order_fills,
                code_version=code_version,
                strategy_version=strategy_version),
            quality=quality_from_order(order, order_fills),
            entry_at=entry_at,
            exit_at=None if still_open else max(
                (f.filled_at for f in fills
                 if f.instrument_id == order.instrument_id and f.filled_at),
                default=None),
            entry_price=entry_price,
            exit_price=exit_price,
            gross_pnl=finite_or_none(gross),
            fees=sum(f.total_cost for f in order_fills),
            # UNKNOWN while open, and SIGNAL_EXIT when closed: the
            # loop only ever closes a position because the portfolio
            # layer set a smaller target. A stop or a take-profit would
            # need an exit policy this phase does not have, and naming
            # one here would claim a mechanism that does not exist.
            exit_reason=(ExitReason.UNKNOWN if still_open
                         else ExitReason.SIGNAL_EXIT),
            environment=getattr(order.environment, "value", "paper"),
            is_open=still_open)
        # Phase 16's classifier, unchanged. Three mechanical fields and
        # no inference from P&L.
        classify_errors(outcome)
        outcomes.append(outcome)
    return outcomes


def persist_outcomes(conn: sqlite3.Connection,
                     outcomes: Sequence[TradeOutcome],
                     session_id: Optional[str] = None) -> int:
    """
    Write outcomes through Phase 16's repository.

    Through its repository rather than with SQL here, so the wide
    lineage columns are filled by the code that owns them and cannot
    drift from the schema.
    """
    if not outcomes:
        return 0
    initialize_governance_schema(conn)
    repository = GovernanceRepository(conn)
    for outcome in outcomes:
        repository.save_outcome(outcome, session_id=session_id)
    return len(outcomes)
