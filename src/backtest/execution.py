"""
src/backtest/execution.py
------------------------------
The execution boundary and its only Phase 12 implementation
(spec §16, §17, §94, §95, §96).

THE ABSTRACTION IS THE POINT
--------------------------------
    ExecutionEngine  (interface)
        |- SimulationExecutor    <- Phase 12, fills against cached bars
        |- PaperExecutor         <- Phase 13, NOT implemented here
        |- BrokerExecutor        <- later, NOT implemented here

Strategy and risk code never learns which executor it is talking to.
That is what allows the same strategy to be backtested, then paper
traded, then eventually traded live without the logic being rewritten
for each — and rewriting it for each is how backtest and live behaviour
silently diverge.

Phase 13 implements PaperExecutor against this interface. This file
must not grow one.

WHAT THE SIMULATOR REFUSES TO DO
------------------------------------
It will not fill against a bar that does not exist, will not
interpolate a price, and will not fill at or before the moment its
order was cut. Each refusal is recorded with a reason rather than
silently skipped, because a run that quietly dropped a third of its
orders looks identical to one that traded cleanly.

FILL PRICING
----------------
The reference price comes from the chosen bar and timing. Slippage then
moves it AGAINST the trade, and commission is charged on top. Both are
recorded separately from the reference price, so what the model charged
is auditable rather than inferred from a net number.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from src.backtest.calendar import Bar, MarketCalendar
from src.backtest.guards import TemporalGuard
from src.domain.backtest_models import (
    CostModel, ExecutionAssumptions, ExecutionTiming, OrderSide, OrderState,
    RejectReason, SimulatedFill, SimulatedOrder, SlippageModel, finite_or_none,
    safe_ratio,
)

EXECUTION_ENGINE_VERSION = "exec-sim-v1"

#: A cash-constrained fill smaller than this fraction of the order is
#: treated as unaffordable rather than filled. Prevents dust trades
#: from a nearly-empty account.
MIN_FILL_FRACTION = 0.01


@dataclass
class ExecutionContext:
    """
    What the executor may know when filling one order.

    Deliberately small. An executor that could see the whole portfolio
    could size against it, which is the risk layer's job, and an
    executor that could see the future could fill favourably.
    """
    available_cash: float
    #: Annualized volatility for the instrument, when measurable — the
    #: input to volatility-scaled slippage. None means unmeasurable.
    volatility: Optional[float] = None
    #: Hard horizon; nothing may fill past the backtest's end date.
    horizon_end: Optional[datetime] = None
    allow_shorting: bool = False
    #: Current signed position, so a sell can be recognised as a close
    #: rather than a new short.
    current_quantity: float = 0.0


class ExecutionEngine(ABC):
    """The interface every executor implements — simulated, paper, or live."""

    version: str = "abstract"

    @abstractmethod
    def execute(self, order: SimulatedOrder,
                context: ExecutionContext) -> List[SimulatedFill]:
        """
        Attempt to fill an order.

        Returns the fills produced — possibly none. The order's `state`
        and `reject_reason` are updated in place, so a caller that
        ignores the return value still sees what happened.
        """


class SimulationExecutor(ExecutionEngine):
    """Fills orders against cached historical bars. No network, no broker."""

    version = EXECUTION_ENGINE_VERSION

    def __init__(self, calendar: MarketCalendar, costs: CostModel,
                 slippage: SlippageModel, assumptions: ExecutionAssumptions,
                 guard: Optional[TemporalGuard] = None):
        self.calendar = calendar
        self.costs = costs
        self.slippage = slippage
        self.assumptions = assumptions
        self.guard = guard or TemporalGuard()
        self._fill_seq = 0

    # ---------------- bar selection ----------------

    def _candidate_bars(self, order: SimulatedOrder) -> List[Bar]:
        """
        Bars this order is allowed to fill against, in order of
        preference.

        For the realistic timings this is the next N sessions STRICTLY
        after the order — the strict inequality being the difference
        between a plausible fill and look-ahead.

        SAME_BAR_CLOSE deliberately returns the bar at or before the
        order instead. It exists so a researcher can measure how much
        of a result depends on that unrealistic assumption; the
        configuration flags it and the run carries a warning.
        """
        moment = order.created_at
        if moment is None:
            return []
        if self.assumptions.timing == ExecutionTiming.SAME_BAR_CLOSE:
            bar = self.calendar.bar_at_or_before(order.instrument_id, moment)
            return [bar] if bar is not None else []
        return self.calendar.next_bars_after(
            order.instrument_id, moment, max(1, self.assumptions.max_bars_to_fill))

    def _reference_price(self, bar: Bar) -> Optional[float]:
        """
        The untouched bar price for this timing.

        NEXT_BAR_OPEN prefers the open and falls back to the close when
        the bar has no open, because a bar with only a close is still a
        real observation — but the fallback is what `note` on the fill
        records, so the substitution is never invisible.
        """
        if self.assumptions.timing == ExecutionTiming.NEXT_BAR_OPEN:
            return finite_or_none(bar.open) or finite_or_none(bar.close)
        return finite_or_none(bar.close) or finite_or_none(bar.open)

    # ---------------- execution ----------------

    def _next_fill_id(self, order: SimulatedOrder, bar: Bar) -> str:
        self._fill_seq += 1
        raw = f"{order.order_id}|{bar.timestamp.isoformat()}|{self._fill_seq}"
        return f"fl-{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]}"

    def _reject(self, order: SimulatedOrder, reason: RejectReason,
                note: str = "") -> List[SimulatedFill]:
        order.state = OrderState.REJECTED
        order.reject_reason = reason
        if note:
            order.note = note
        return []

    def execute(self, order: SimulatedOrder,
                context: ExecutionContext) -> List[SimulatedFill]:
        order.state = OrderState.SUBMITTED

        if order.quantity <= 0:
            return self._reject(order, RejectReason.ZERO_QUANTITY,
                                "order carried no quantity")

        # Opening or extending a short requires shorting to be enabled.
        would_be_short = (order.side == OrderSide.SELL
                          and context.current_quantity - order.quantity < -1e-12)
        if would_be_short and not context.allow_shorting:
            return self._reject(
                order, RejectReason.SHORTING_DISABLED,
                "shorting is disabled; no borrow-cost data exists to model it")

        bars = self._candidate_bars(order)
        if not bars:
            return self._reject(
                order, RejectReason.NO_PRICE,
                f"no session within {self.assumptions.max_bars_to_fill} bar(s) "
                f"after {order.created_at.isoformat() if order.created_at else 'n/a'}")

        allow_same = self.assumptions.timing == ExecutionTiming.SAME_BAR_CLOSE
        fills: List[SimulatedFill] = []
        remaining = order.quantity

        for bar in bars:
            if remaining <= 1e-12:
                break

            if context.horizon_end is not None and bar.timestamp > context.horizon_end:
                if not fills:
                    return self._reject(
                        order, RejectReason.BEYOND_HORIZON,
                        "next fillable session is past the backtest end date")
                break

            reference = self._reference_price(bar)
            if reference is None or reference <= 0:
                continue

            # The load-bearing check: a fill must follow its order.
            self.guard.check_fill_after_order(
                bar.timestamp, order.created_at, allow_same_moment=allow_same)

            # --- participation / liquidity ---
            fillable = remaining
            participation: Optional[float] = None
            capped = False
            cap = self.assumptions.max_participation
            if cap is not None and bar.volume and bar.volume > 0:
                allowed = bar.volume * cap
                participation = safe_ratio(remaining, bar.volume)
                if remaining > allowed:
                    if not self.assumptions.allow_partial_fills:
                        return self._reject(
                            order, RejectReason.LIQUIDITY_CAP,
                            f"order is {participation:.1%} of the bar's volume, "
                            f"above the {cap:.0%} participation cap")
                    fillable = allowed
                    participation = cap
                    capped = True

            if fillable <= 1e-12:
                continue

            # --- pricing ---
            price = self.slippage.apply(
                reference, order.side, volatility=context.volatility,
                participation=participation)
            if price is None or price <= 0:
                continue

            # --- cash ---
            if order.side == OrderSide.BUY:
                commission_probe = self.costs.charge(fillable, price)
                needed = fillable * price + commission_probe
                if needed > context.available_cash + 1e-9:
                    affordable = self._affordable_quantity(
                        context.available_cash, price)
                    # Dust guard: a sliver of cash should refuse the
                    # order, not buy a hundredth of a share. Filling
                    # trivial remainders would pack the trade ledger
                    # with meaningless micro-positions and inflate the
                    # trade count that every per-trade metric divides by.
                    if affordable < remaining * MIN_FILL_FRACTION:
                        if not fills:
                            return self._reject(
                                order, RejectReason.INSUFFICIENT_CASH,
                                f"needed {needed:,.2f} but only "
                                f"{context.available_cash:,.2f} was available "
                                f"({affordable:.6f} of {remaining:.6f} affordable)")
                        break
                    fillable = affordable
                    capped = True

            commission = self.costs.charge(fillable, price)
            slippage_cost = abs(price - reference) * fillable

            fill = SimulatedFill(
                fill_id=self._next_fill_id(order, bar),
                run_id=order.run_id, order_id=order.order_id,
                instrument_id=order.instrument_id, side=order.side,
                quantity=fillable, price=price, reference_price=reference,
                filled_at=bar.timestamp, commission=commission,
                slippage_cost=slippage_cost, bar_timestamp=bar.timestamp,
                participation=participation,
                is_partial=capped or fillable < order.quantity - 1e-12)

            fills.append(fill)
            remaining -= fillable
            order.filled_quantity += fillable
            context.available_cash -= (
                fillable * price + commission if order.side == OrderSide.BUY
                else -(fillable * price - commission))

        if not fills:
            return self._reject(order, RejectReason.NO_PRICE,
                                "no usable price on any candidate session")

        order.state = (OrderState.FILLED if remaining <= 1e-9
                       else OrderState.PARTIALLY_FILLED)
        return fills

    @staticmethod
    def _affordable_quantity(cash: float, price: float) -> float:
        """
        The largest quantity this cash can buy, ignoring commission.

        Commission is re-derived on the reduced quantity afterwards, so
        the result may be marginally optimistic by one commission's
        worth. Documented rather than solved with a fixed-point loop —
        the error is bounded by a single commission charge and never
        compounds.
        """
        if price <= 0:
            return 0.0
        return max(0.0, cash / price)

    def describe(self) -> Dict[str, object]:
        return {
            "engine": self.version,
            "timing": self.assumptions.timing.value,
            "assumptions": self.assumptions.describe(),
            "cost_model": self.costs.version,
            "slippage_model": f"{self.slippage.version}:{self.slippage.method.value}",
            "simplified_market_impact": self.slippage.is_simplified_impact,
        }
