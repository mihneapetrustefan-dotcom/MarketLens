"""
src/paper/executor.py
--------------------------
The paper executor and the broker-shaped interface it satisfies
(Phase 13, spec §14, §15, §16, §17, §18, §19, §20, §86, §87).

TWO INTERFACES, ON PURPOSE
------------------------------
`PaperExecutor` satisfies both:

  1. `ExecutionEngine.execute()` — Phase 12's contract, so the same
     pipeline position works for backtest and paper (spec §44, §87).

  2. `BrokerLikeInterface` — connect / get_account / get_positions /
     place_order / cancel_order / health, which is the shape a real
     broker adapter will need (spec §86).

The second is why paper trading is worth building before live trading:
Phase 15 implemented the successor interface against Interactive
Brokers, and nothing upstream changed. What must NOT happen — and what this file is arranged
to prevent — is a broker being reached from anywhere except through
this interface.

WHY PLACING AND FILLING ARE SEPARATE CALLS
----------------------------------------------
Phase 12's executor fills or rejects immediately, sweeping forward over
bars, because a backtest knows the whole future. A live path cannot: a
limit order rests until the market comes to it, and may never fill.

So `place_order` validates and accepts, and `try_fill` is called on each
subsequent tick against that tick's bar. A market order typically
completes on the first attempt; a limit order may rest for days or
expire unfilled. `execute()` composes the two for callers that want the
Phase 12 shape.

EVERY MODEL IS REUSED, NOT REIMPLEMENTED
--------------------------------------------
Costs, slippage and the market calendar come from Phase 12 unchanged —
spec §21, §22 and §24 require it, and the deeper reason is spec §66:
paper results are only comparable to backtest results if both were
produced under the same assumptions. A second slippage formula here
would make every divergence uninterpretable.

WHAT THE DATA CANNOT SUPPORT, STATED
----------------------------------------
There is no bid, ask or spread anywhere in this system — the price
cache holds OHLCV only. So execution prices come from bar prices with
slippage applied, and `SIMPLIFIED_MICROSTRUCTURE` records that
assumption on the executor rather than leaving it implied (spec §23).

Intrabar ordering is likewise unknowable: a bar that spans both a stop
trigger and a limit price cannot tell you which came first. Fills in
that situation carry `intrabar_ambiguous=True` instead of a fabricated
sequence.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from src.backtest.accounting import PortfolioLedger
from src.backtest.calendar import Bar, MarketCalendar
from src.backtest.execution import ExecutionContext, ExecutionEngine
from src.domain.backtest_models import (
    CostModel, OrderSide as BacktestOrderSide, OrderState as BacktestOrderState,
    SimulatedFill, SimulatedOrder, SlippageModel,
)
from src.domain.paper_models import (
    DataFreshness, ExecutionVenue, HealthState, OrderSide, PaperFill, PaperOrder,
    PaperOrderState, PaperOrderType, PaperRejectReason, TimeInForce, finite_or_none,
    safe_ratio,
)
from src.paper.clock import Clock, require_utc

PAPER_EXECUTOR_VERSION = "paper-exec-v1"

#: The microstructure this system can actually model (spec §23).
SIMPLIFIED_MICROSTRUCTURE = (
    "no bid/ask/spread data exists in this system — the price cache holds "
    "OHLCV only. Execution prices are bar prices with the Phase 12 slippage "
    "model applied, which approximates crossing a spread but does not model one.")

#: A cash-constrained fill smaller than this fraction of the order is
#: refused rather than filled, matching Phase 12's dust guard.
MIN_FILL_FRACTION = 0.01


# ============================================================
# The interface a future broker adapter will implement
# ============================================================

@dataclass
class AccountView:
    """A read-only account summary, in the shape a broker would return."""
    account_id: str
    base_currency: str
    cash: float
    equity: float
    positions_value: float
    gross_exposure: float
    net_exposure: float
    buying_power: float
    is_paper: bool = True


@dataclass
class PositionView:
    """A read-only position, in the shape a broker would return."""
    instrument_id: str
    quantity: float
    average_price: float
    market_value: Optional[float] = None
    unrealized_pnl: Optional[float] = None


class BrokerLikeInterface(ABC):
    """
    The contract Phase 14's broker adapters will implement (spec §86).

    Declared here, in the phase that has no broker, precisely so the
    shape is fixed by a working implementation before any real venue is
    involved. The Phase 15 IBKR adapter satisfies the successor
    interface (`BrokerGateway`) and everything upstream — strategy,
    signal, risk, order intent — stayed untouched, which is what this
    shape existed to make possible.

    Deliberately NOT included: any authentication method, credential
    field, or account-funding call. Those belong to the phase that
    connects to a venue, and putting a `login()` here would invite
    someone to fill it in.
    """

    @abstractmethod
    def connect(self) -> bool:
        """Establish the executor. Paper: always succeeds, touches nothing."""

    @abstractmethod
    def disconnect(self) -> None:
        """Release the executor."""

    @abstractmethod
    def is_connected(self) -> bool:
        ...

    @abstractmethod
    def get_account(self) -> AccountView:
        ...

    @abstractmethod
    def get_positions(self) -> List[PositionView]:
        ...

    @abstractmethod
    def get_orders(self, open_only: bool = False) -> List[PaperOrder]:
        ...

    @abstractmethod
    def place_order(self, order: PaperOrder, now: datetime) -> PaperOrder:
        """Validate and accept (or reject) an order. Does not fill it."""

    @abstractmethod
    def cancel_order(self, order_id: str, now: datetime) -> Optional[PaperOrder]:
        ...

    @abstractmethod
    def health(self, now: datetime) -> HealthState:
        ...


# ============================================================
# The paper executor
# ============================================================

class PaperExecutor(ExecutionEngine, BrokerLikeInterface):
    """
    Simulates execution against cached bars. Contacts nothing.

    Holds the working-order book in memory and the position/cash state
    in a Phase 12 `PortfolioLedger`, so paper accounting and backtest
    accounting are literally the same code (spec §25).
    """

    version = PAPER_EXECUTOR_VERSION

    def __init__(self, calendar: MarketCalendar, ledger: PortfolioLedger,
                 costs: CostModel, slippage: SlippageModel,
                 account_id: str = "paper", session_id: str = "",
                 max_participation: Optional[float] = 0.10,
                 allow_shorting: bool = False,
                 allow_partial_fills: bool = True):
        self.calendar = calendar
        self.ledger = ledger
        self.costs = costs
        self.slippage = slippage
        self.account_id = account_id
        self.session_id = session_id
        self.max_participation = max_participation
        self.allow_shorting = allow_shorting
        self.allow_partial_fills = allow_partial_fills

        self._orders: Dict[str, PaperOrder] = {}
        self._by_idempotency: Dict[str, str] = {}
        self._fill_keys: set = set()
        self._connected = False
        self._fill_seq = 0

    # ---------------- broker-like surface ----------------

    def connect(self) -> bool:
        """
        Paper connect is a no-op that always succeeds.

        It exists so the lifecycle a broker adapter needs is exercised
        by a real implementation rather than designed in the abstract.
        """
        self._connected = True
        return True

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def get_account(self, prices: Optional[Dict[str, Optional[float]]] = None
                    ) -> AccountView:
        marks = prices or {}
        equity = self.ledger.equity(marks)
        gross = 0.0
        net = 0.0
        for position in self.ledger.open_positions():
            price = marks.get(position.instrument_id)
            if price is None or price <= 0:
                continue
            value = position.market_value(price)
            gross += abs(value)
            net += value
        return AccountView(
            account_id=self.account_id,
            base_currency=self.ledger.base_currency,
            cash=self.ledger.cash, equity=equity,
            positions_value=net, gross_exposure=gross, net_exposure=net,
            buying_power=max(0.0, self.ledger.cash), is_paper=True)

    def get_positions(self, prices: Optional[Dict[str, Optional[float]]] = None
                      ) -> List[PositionView]:
        marks = prices or {}
        views: List[PositionView] = []
        for position in self.ledger.open_positions():
            price = marks.get(position.instrument_id)
            views.append(PositionView(
                instrument_id=position.instrument_id,
                quantity=position.quantity,
                average_price=position.average_cost,
                market_value=(position.market_value(price)
                              if price is not None and price > 0 else None),
                unrealized_pnl=(position.unrealized(price)
                                if price is not None and price > 0 else None)))
        return views

    def get_orders(self, open_only: bool = False) -> List[PaperOrder]:
        orders = list(self._orders.values())
        if open_only:
            orders = [o for o in orders if o.state.is_working]
        return sorted(orders, key=lambda o: (o.created_at or datetime.min.replace(
            tzinfo=timezone.utc)))

    def health(self, now: datetime) -> HealthState:
        if not self._connected:
            return HealthState.FAILED
        return HealthState.HEALTHY

    # ---------------- idempotency ----------------

    @staticmethod
    def idempotency_key(session_id: str, instrument_id: str,
                        decided_at: datetime, signal_id: Optional[str],
                        target_weight: Optional[float]) -> str:
        """
        A stable key for one DECISION (spec §12).

        Derived from the deciding inputs, not from wall time, so the
        same decision reprocessed after a restart produces the same key
        and is recognised rather than doubling the position.

        WHY signal_id IS ACCEPTED BUT NOT HASHED
        --------------------------------------------
        The decision this key identifies is "at moment M, move
        instrument X to target weight W". The signal that motivated it
        is PROVENANCE, not identity.

        Including it looked right and was wrong: several signals can be
        live for one instrument at the same moment, and the sizing
        strategy proposes a change for each. They all ask for the same
        target, so they are one order — but with the signal in the key
        they produced several, each adding a little more exposure. That
        is precisely the duplicate this key exists to prevent, arriving
        through the front door.

        The parameter stays in the signature because callers naturally
        have it and passing it documents intent; it simply does not
        affect the hash.
        """
        raw = (f"{session_id}|{instrument_id}|{decided_at.isoformat()}|"
               f"{'' if target_weight is None else f'{target_weight:.8f}'}")
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]

    def find_by_idempotency(self, key: str) -> Optional[PaperOrder]:
        order_id = self._by_idempotency.get(key)
        return self._orders.get(order_id) if order_id else None

    # ---------------- placing ----------------

    def _reject(self, order: PaperOrder, reason: PaperRejectReason,
                detail: str, now: datetime) -> PaperOrder:
        order.state = PaperOrderState.REJECTED
        order.reject_reason = reason
        order.reject_detail = detail
        order.terminal_at = now
        self._orders[order.order_id] = order
        return order

    def place_order(self, order: PaperOrder, now: datetime,
                    freshness: Optional[DataFreshness] = None,
                    available_cash: Optional[float] = None) -> PaperOrder:
        """
        Validate and accept an order, or reject it with a reason.

        Never fills. A market order placed here still needs `try_fill`
        against a bar, which keeps the paper path honest about the fact
        that placing and executing are different moments.
        """
        require_utc(now, "now")
        order.state = PaperOrderState.VALIDATING

        # --- duplicate delivery (spec §12) ---
        if order.idempotency_key:
            existing = self.find_by_idempotency(order.idempotency_key)
            if existing is not None and existing.order_id != order.order_id:
                return self._reject(
                    order, PaperRejectReason.DUPLICATE,
                    f"an order with this idempotency key already exists "
                    f"({existing.order_id})", now)

        if order.quantity <= 0:
            return self._reject(order, PaperRejectReason.INVALID_QUANTITY,
                                "quantity must be positive", now)

        if not self.calendar.has_data(order.instrument_id):
            return self._reject(order, PaperRejectReason.UNKNOWN_INSTRUMENT,
                                f"no cached history for {order.instrument_id}", now)

        # --- market session (spec §24) ---
        if self.calendar.bar_at_or_before(order.instrument_id, now) is None:
            return self._reject(
                order, PaperRejectReason.MARKET_CLOSED,
                "no session has occurred for this instrument at or before now", now)

        # --- data freshness (spec §9, §31) ---
        if freshness is not None and not freshness.is_tradeable:
            return self._reject(
                order, PaperRejectReason.STALE_DATA,
                f"market data is {freshness.value}; new orders are blocked", now)

        # --- shorting ---
        current = self.ledger.positions.get(order.instrument_id)
        held = current.quantity if current else 0.0
        would_short = (order.side == OrderSide.SELL
                       and held - order.quantity < -1e-12)
        if would_short and not self.allow_shorting:
            return self._reject(
                order, PaperRejectReason.SHORTING_DISABLED,
                "shorting is disabled; no borrow-cost data exists to model it", now)

        # --- price sanity for resting types ---
        if order.order_type in (PaperOrderType.LIMIT, PaperOrderType.STOP_LIMIT) \
                and (order.limit_price is None or order.limit_price <= 0):
            return self._reject(order, PaperRejectReason.INVALID_PRICE,
                                "limit price must be positive", now)
        if order.order_type in (PaperOrderType.STOP, PaperOrderType.STOP_LIMIT) \
                and (order.stop_price is None or order.stop_price <= 0):
            return self._reject(order, PaperRejectReason.INVALID_PRICE,
                                "stop price must be positive", now)

        order.state = PaperOrderState.ACCEPTED
        order.accepted_at = now
        if order.expires_at is None:
            order.expires_at = self._default_expiry(order, now)

        self._orders[order.order_id] = order
        if order.idempotency_key:
            self._by_idempotency[order.idempotency_key] = order.order_id
        return order

    @staticmethod
    def _default_expiry(order: PaperOrder, now: datetime) -> Optional[datetime]:
        if order.time_in_force == TimeInForce.GTC:
            return None
        if order.time_in_force == TimeInForce.IOC:
            return now
        return now + timedelta(days=1)      # DAY

    def cancel_order(self, order_id: str, now: datetime) -> Optional[PaperOrder]:
        order = self._orders.get(order_id)
        if order is None:
            return None
        if order.state.is_terminal:
            return order
        order.state = PaperOrderState.CANCELLED
        order.terminal_at = require_utc(now, "now")
        return order

    def expire_stale_orders(self, now: datetime) -> List[PaperOrder]:
        """Move working orders past their validity into EXPIRED."""
        require_utc(now, "now")
        expired: List[PaperOrder] = []
        for order in self._orders.values():
            if order.state.is_working and order.is_expired_at(now):
                order.state = PaperOrderState.EXPIRED
                order.terminal_at = now
                expired.append(order)
        return expired

    # ---------------- filling ----------------

    def _next_fill_id(self, order: PaperOrder, bar: Bar) -> str:
        self._fill_seq += 1
        raw = f"{order.order_id}|{bar.timestamp.isoformat()}|{self._fill_seq}"
        return f"pf-{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]}"

    def _triggers(self, order: PaperOrder, bar: Bar) -> Tuple[bool, Optional[float], bool]:
        """
        Decide whether this bar fills the order, and at what reference
        price.

        Returns (fills, reference_price, intrabar_ambiguous).

        MARKET  — fills at the bar's open (the first price available
                  after the decision), falling back to close.
        LIMIT   — fills only if the bar's range reached the limit, at
                  the better of the limit and the open. A buy limit
                  above the open fills at the open, because a real book
                  would not charge more than the resting price.
        STOP    — triggers when the range crosses the stop, then behaves
                  as market at the stop price.
        STOP_LIMIT — both conditions; flagged ambiguous when one bar
                  spans both, since OHLC cannot order them.
        """
        high = finite_or_none(bar.high)
        low = finite_or_none(bar.low)
        open_price = finite_or_none(bar.open) or finite_or_none(bar.close)
        close_price = finite_or_none(bar.close) or open_price
        if open_price is None:
            return False, None, False

        if order.order_type == PaperOrderType.MARKET:
            return True, open_price, False

        if order.order_type == PaperOrderType.LIMIT:
            limit = order.limit_price
            if high is None or low is None:
                # Without a range, only the open can be compared.
                reached = (open_price <= limit if order.side == OrderSide.BUY
                           else open_price >= limit)
                return (reached, open_price if reached else None, False)
            if order.side == OrderSide.BUY:
                if low <= limit:
                    return True, min(open_price, limit), False
                return False, None, False
            if high >= limit:
                return True, max(open_price, limit), False
            return False, None, False

        if order.order_type == PaperOrderType.STOP:
            stop = order.stop_price
            if high is None or low is None:
                triggered = (open_price >= stop if order.side == OrderSide.BUY
                             else open_price <= stop)
                return (triggered, open_price if triggered else None, False)
            if order.side == OrderSide.BUY:
                if high >= stop:
                    return True, max(open_price, stop), False
                return False, None, False
            if low <= stop:
                return True, min(open_price, stop), False
            return False, None, False

        # STOP_LIMIT
        stop, limit = order.stop_price, order.limit_price
        if high is None or low is None:
            return False, None, False
        if order.side == OrderSide.BUY:
            triggered = high >= stop
            fillable = low <= limit
            ambiguous = triggered and fillable
            if triggered and fillable:
                return True, min(max(open_price, stop), limit), ambiguous
            return False, None, False
        triggered = low <= stop
        fillable = high >= limit
        ambiguous = triggered and fillable
        if triggered and fillable:
            return True, max(min(open_price, stop), limit), ambiguous
        return False, None, False

    def try_fill(self, order: PaperOrder, now: datetime,
                 available_cash: Optional[float] = None,
                 volatility: Optional[float] = None) -> List[PaperFill]:
        """
        Attempt to fill a working order against the bar at `now`.

        The bar must be STRICTLY after the order was created — the same
        inequality Phase 12 enforces, and for the same reason: filling
        on the bar that produced the decision is executing on
        information that arrived simultaneously.
        """
        require_utc(now, "now")
        if not order.state.is_working:
            return []
        if order.is_expired_at(now):
            order.state = PaperOrderState.EXPIRED
            order.terminal_at = now
            return []

        bar = self.calendar.bar_at_or_before(order.instrument_id, now)
        if bar is None:
            return []
        if order.created_at is not None and bar.timestamp <= order.created_at:
            # The only bar available is the one the decision used.
            return []

        fills, reference, ambiguous = self._triggers(order, bar)
        if not fills or reference is None or reference <= 0:
            return []

        remaining = order.remaining
        if remaining <= 1e-12:
            return []

        # --- participation cap ---
        fillable = remaining
        participation: Optional[float] = None
        capped = False
        if self.max_participation is not None and bar.volume and bar.volume > 0:
            allowed = bar.volume * self.max_participation
            participation = safe_ratio(remaining, bar.volume)
            if remaining > allowed:
                if not self.allow_partial_fills:
                    order.state = PaperOrderState.REJECTED
                    order.reject_reason = PaperRejectReason.LIQUIDITY_CAP
                    order.reject_detail = (
                        f"order is {participation:.1%} of the bar's volume, above "
                        f"the {self.max_participation:.0%} cap")
                    order.terminal_at = now
                    return []
                fillable = allowed
                participation = self.max_participation
                capped = True

        # --- pricing ---
        price = self.slippage.apply(
            reference,
            BacktestOrderSide.BUY if order.side == OrderSide.BUY
            else BacktestOrderSide.SELL,
            volatility=volatility, participation=participation)
        if price is None or price <= 0:
            return []

        # --- cash ---
        cash = self.ledger.cash if available_cash is None else available_cash
        if order.side == OrderSide.BUY:
            probe = self.costs.charge(fillable, price)
            if fillable * price + probe > cash + 1e-9:
                affordable = max(0.0, cash / price) if price > 0 else 0.0
                if affordable < remaining * MIN_FILL_FRACTION:
                    order.state = PaperOrderState.REJECTED
                    order.reject_reason = PaperRejectReason.INSUFFICIENT_CASH
                    order.reject_detail = (
                        f"needed {fillable * price + probe:,.2f} but only "
                        f"{cash:,.2f} was available")
                    order.terminal_at = now
                    return []
                fillable = affordable
                capped = True

        commission = self.costs.charge(fillable, price)
        slippage_cost = abs(price - reference) * fillable

        fill_id = self._next_fill_id(order, bar)
        fill = PaperFill(
            fill_id=fill_id, session_id=self.session_id, order_id=order.order_id,
            account_id=self.account_id, instrument_id=order.instrument_id,
            side=order.side, quantity=fillable, price=price,
            reference_price=reference, filled_at=bar.timestamp,
            commission=commission, slippage_cost=slippage_cost,
            venue=ExecutionVenue.PAPER,
            execution_model_version=self.version,
            slippage_model_version=self.slippage.version,
            cost_model_version=self.costs.version,
            bar_timestamp=bar.timestamp, participation=participation,
            is_partial=capped or fillable < order.quantity - 1e-12,
            intrabar_ambiguous=ambiguous,
            idempotency_key=f"{order.idempotency_key}:{fill_id}")

        self.apply_fill(order, fill)
        return [fill]

    def apply_fill(self, order: PaperOrder, fill: PaperFill) -> bool:
        """
        Record a fill against its order and the ledger.

        Idempotent by fill key (spec §12, adversarial case 8): a
        repeated fill message is recognised and ignored rather than
        doubling the position. Returns True when the fill was newly
        applied.
        """
        if fill.idempotency_key and fill.idempotency_key in self._fill_keys:
            return False
        if fill.idempotency_key:
            self._fill_keys.add(fill.idempotency_key)

        previous_quantity = order.filled_quantity
        previous_average = order.average_fill_price
        order.filled_quantity += fill.quantity
        if previous_average is None or previous_quantity <= 0:
            order.average_fill_price = fill.price
        else:
            order.average_fill_price = finite_or_none(
                (previous_average * previous_quantity + fill.price * fill.quantity)
                / order.filled_quantity) or fill.price

        order.state = (PaperOrderState.FILLED if order.is_complete
                       else PaperOrderState.PARTIALLY_FILLED)
        if order.state == PaperOrderState.FILLED:
            order.terminal_at = fill.filled_at

        self.ledger.apply_fill(
            fill_to_simulated(fill),
            strategy_id=order.strategy_id, signal_id=order.signal_id,
            decision_id=order.decision_id)
        return True

    # ---------------- Phase 12 interface parity ----------------

    def execute(self, order: SimulatedOrder,
                context: ExecutionContext) -> List[SimulatedFill]:
        """
        Phase 12's `ExecutionEngine` contract (spec §44, §87).

        Place-then-fill composed into one call, so a caller written
        against the backtest interface can drive the paper executor
        unchanged. The richer `place_order` / `try_fill` pair is what a
        live-parity path uses, because a resting order cannot be
        expressed in a single fill-or-reject call.
        """
        now = order.created_at or datetime.now(timezone.utc)
        paper = PaperOrder(
            order_id=order.order_id, session_id=self.session_id,
            account_id=self.account_id, instrument_id=order.instrument_id,
            side=OrderSide.BUY if order.side == BacktestOrderSide.BUY
            else OrderSide.SELL,
            quantity=order.quantity, order_type=PaperOrderType.MARKET,
            information_cutoff=order.information_cutoff,
            decided_at=order.decision_at, created_at=now,
            signal_id=order.signal_id, decision_id=order.decision_id,
            target_weight=order.target_weight)

        placed = self.place_order(paper, now, available_cash=context.available_cash)
        if placed.state == PaperOrderState.REJECTED:
            order.state = BacktestOrderState.REJECTED
            return []

        # Fill against the first session strictly after the order.
        following = self.calendar.next_bar_after(order.instrument_id, now)
        if following is None:
            return []
        fills = self.try_fill(placed, following.timestamp,
                              available_cash=context.available_cash,
                              volatility=context.volatility)
        order.filled_quantity = placed.filled_quantity
        return [fill_to_simulated(f) for f in fills]

    def describe(self) -> Dict[str, object]:
        return {
            "executor": self.version,
            "venue": ExecutionVenue.PAPER.value,
            "cost_model": self.costs.version,
            "slippage_model": f"{self.slippage.version}:{self.slippage.method.value}",
            "max_participation": self.max_participation,
            "allow_shorting": self.allow_shorting,
            "microstructure": SIMPLIFIED_MICROSTRUCTURE,
            "connects_to_broker": False,
        }


def fill_to_simulated(fill: PaperFill) -> SimulatedFill:
    """
    Convert a paper fill into the Phase 12 shape the ledger consumes.

    This adapter is what lets paper trading reuse Phase 12's accounting
    verbatim (spec §25) while keeping a richer paper fill record. The
    alternative — a second set of accounting formulas — would let paper
    and backtest P&L diverge for reasons nobody could trace.
    """
    return SimulatedFill(
        fill_id=fill.fill_id, run_id=fill.session_id, order_id=fill.order_id,
        instrument_id=fill.instrument_id,
        side=(BacktestOrderSide.BUY if fill.side == OrderSide.BUY
              else BacktestOrderSide.SELL),
        quantity=fill.quantity, price=fill.price,
        reference_price=fill.reference_price, filled_at=fill.filled_at,
        commission=fill.commission, slippage_cost=fill.slippage_cost,
        bar_timestamp=fill.bar_timestamp, participation=fill.participation,
        is_partial=fill.is_partial)
