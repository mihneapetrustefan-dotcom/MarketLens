"""
src/backtest/accounting.py
-------------------------------
Deterministic position and cash accounting for the simulated portfolio
(Phase 12, spec §18, §19, §20, §24).

AVERAGE-COST METHOD, STATED EXPLICITLY
------------------------------------------
Position cost basis uses the weighted average method: adding to a
position blends the new fill into the average, reducing it realises
P&L against that average and leaves it unchanged. FIFO and LIFO would
produce different realised P&L for identical trades, so the choice is
recorded here rather than left for a reader to infer from the numbers.

THREE FILL CASES, AND THE ONE THAT IS EASY TO GET WRONG
-----------------------------------------------------------
  INCREASE   same direction        -> blend the average, no P&L
  REDUCE     opposite, smaller     -> realise against the average
  FLIP       opposite, larger      -> close the whole position, realise
                                      it, then OPEN a new one at the
                                      fill price with the remainder

The flip is the case naive ledgers break on: they blend a sell of 150
into a long of 100 and end up with a negative quantity carrying a
nonsensical average cost. Here it closes and reopens, which is what
actually happened.

CASH IS UPDATED ON EVERY FILL, INCLUDING COSTS
--------------------------------------------------
Spec §20 forbids silently ignoring transaction costs. Commission and
slippage are charged to cash at fill time, not netted out of a return
figure at the end, so the equity curve reflects them continuously and
a strategy that trades itself to death shows it.

SHORT PROCEEDS ARE CREDITED, BORROW COSTS ARE NOT MODELLED
--------------------------------------------------------------
Selling short credits cash with the proceeds. Real short selling also
incurs borrow fees and margin requirements, and this project has no
borrow-rate data of any kind. Spec §23 says not to pretend otherwise:
shorting is disabled by default in the execution assumptions, and a run
that enables it carries the limitation in its warnings.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from src.domain.backtest_models import (
    OrderSide, SimulatedFill, Trade, finite_or_none, safe_ratio,
)
from src.domain.portfolio_models import Position, PositionSource, PositionStatus


@dataclass
class LedgerPosition:
    """One open holding inside the simulation. Signed: negative is short."""
    instrument_id: str
    quantity: float = 0.0
    average_cost: float = 0.0
    opened_at: Optional[datetime] = None
    entry_signal_id: Optional[str] = None
    entry_decision_id: Optional[str] = None
    #: Running extremes of unrealized P&L while open, sampled at each
    #: mark-to-market. An approximation of MFE/MAE from closing marks
    #: rather than intrabar extremes — stated, not implied.
    max_favourable: Optional[float] = None
    max_adverse: Optional[float] = None

    @property
    def is_open(self) -> bool:
        return abs(self.quantity) > 1e-12

    @property
    def is_short(self) -> bool:
        return self.quantity < 0

    def market_value(self, price: float) -> float:
        return self.quantity * price

    def unrealized(self, price: float) -> float:
        return (price - self.average_cost) * self.quantity


@dataclass
class LedgerSnapshot:
    """The ledger's state at one instant, for the equity curve."""
    timestamp: datetime
    cash: float
    positions_value: float
    gross_exposure: float
    long_exposure: float
    short_exposure: float
    open_positions: int
    unpriced_positions: int = 0

    @property
    def equity(self) -> float:
        return self.cash + self.positions_value

    @property
    def net_exposure(self) -> float:
        return self.long_exposure - self.short_exposure


class PortfolioLedger:
    """
    The simulated book: cash, open positions, realised trades.

    Lives in memory for the duration of a run. It is deliberately NOT
    the Phase 11 `positions` table — a backtest must not write into the
    live portfolio it is testing against, and two runs in parallel must
    not see each other's fills (spec §81).
    """

    def __init__(self, initial_capital: float, run_id: str = "",
                 base_currency: str = "USD"):
        if initial_capital <= 0:
            raise ValueError("initial_capital must be positive")
        self.initial_capital = float(initial_capital)
        self.cash = float(initial_capital)
        self.run_id = run_id
        self.base_currency = base_currency
        self.positions: Dict[str, LedgerPosition] = {}
        self.trades: List[Trade] = []
        self.realized_pnl: float = 0.0
        self.total_costs: float = 0.0
        self.total_slippage: float = 0.0
        #: Absolute notional traded, the numerator of turnover.
        self.traded_notional: float = 0.0
        self._trade_seq = 0

    # ---------------- ids ----------------

    def _next_trade_id(self, instrument_id: str, at: datetime) -> str:
        self._trade_seq += 1
        raw = f"{self.run_id}|{instrument_id}|{at.isoformat()}|{self._trade_seq}"
        return f"tr-{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]}"

    # ---------------- fills ----------------

    def apply_fill(self, fill: SimulatedFill,
                   sector_id: Optional[str] = None,
                   strategy_id: Optional[str] = None,
                   signal_id: Optional[str] = None,
                   decision_id: Optional[str] = None,
                   exit_reason: str = "") -> Optional[Trade]:
        """
        Apply one fill to the book.

        Returns a Trade when the fill closed or reduced a position, or
        None when it only opened or increased one. P&L is undefined
        until something is closed, so returning a Trade for an opening
        fill would mean inventing one.
        """
        signed = fill.signed_quantity
        if abs(signed) <= 1e-12:
            return None

        position = self.positions.get(fill.instrument_id)
        if position is None:
            position = LedgerPosition(instrument_id=fill.instrument_id)
            self.positions[fill.instrument_id] = position

        costs = fill.total_cost
        self.total_costs += fill.commission
        self.total_slippage += fill.slippage_cost
        self.traded_notional += fill.notional

        # Cash: buying spends, selling receives; costs always subtract.
        self.cash -= signed * fill.price
        self.cash -= costs

        produced: Optional[Trade] = None

        if not position.is_open:
            # --- opening ---
            position.quantity = signed
            position.average_cost = fill.price
            position.opened_at = fill.filled_at
            position.entry_signal_id = signal_id
            position.entry_decision_id = decision_id
            position.max_favourable = 0.0
            position.max_adverse = 0.0

        elif (position.quantity > 0) == (signed > 0):
            # --- increasing in the same direction: blend the average ---
            total = position.quantity + signed
            position.average_cost = finite_or_none(
                (position.quantity * position.average_cost + signed * fill.price) / total
            ) or fill.price
            position.quantity = total

        else:
            # --- reducing, closing, or flipping ---
            closing = min(abs(signed), abs(position.quantity))
            direction = 1.0 if position.quantity > 0 else -1.0
            gross = (fill.price - position.average_cost) * closing * direction
            self.realized_pnl += gross

            produced = Trade(
                trade_id=self._next_trade_id(fill.instrument_id, fill.filled_at),
                run_id=self.run_id,
                instrument_id=fill.instrument_id,
                side=OrderSide.BUY if direction > 0 else OrderSide.SELL,
                quantity=closing,
                entry_price=position.average_cost,
                exit_price=fill.price,
                entry_at=position.opened_at or fill.filled_at,
                exit_at=fill.filled_at,
                gross_pnl=finite_or_none(gross) or 0.0,
                costs=costs,
                entry_signal_id=position.entry_signal_id,
                entry_decision_id=position.entry_decision_id,
                exit_reason=exit_reason or "reduced",
                sector_id=sector_id,
                strategy_id=strategy_id,
                mfe=position.max_favourable,
                mae=position.max_adverse,
            )
            self.trades.append(produced)

            remaining = position.quantity + signed
            if abs(remaining) <= 1e-12:
                # Fully closed.
                position.quantity = 0.0
                position.average_cost = 0.0
                position.opened_at = None
                position.entry_signal_id = None
                position.entry_decision_id = None
                position.max_favourable = None
                position.max_adverse = None
            elif (remaining > 0) == (position.quantity > 0):
                # Partially reduced; average cost is unchanged.
                position.quantity = remaining
            else:
                # Flipped through zero: the old position is closed above,
                # and what is left opens a NEW position at the fill price.
                position.quantity = remaining
                position.average_cost = fill.price
                position.opened_at = fill.filled_at
                position.entry_signal_id = signal_id
                position.entry_decision_id = decision_id
                position.max_favourable = 0.0
                position.max_adverse = 0.0

        return produced

    # ---------------- valuation ----------------

    def mark_to_market(self, timestamp: datetime,
                       prices: Dict[str, Optional[float]]) -> LedgerSnapshot:
        """
        Value the book at one instant.

        A position whose price is missing contributes NOTHING to
        positions_value and is counted in `unpriced_positions`. It is
        not valued at cost, because carrying an unpriceable holding at
        its purchase price is a silent assumption that it has not
        moved — and it is not dropped either, because then the caller
        would never learn the equity figure is incomplete.
        """
        positions_value = 0.0
        gross = 0.0
        long_exposure = 0.0
        short_exposure = 0.0
        open_count = 0
        unpriced = 0

        for position in self.positions.values():
            if not position.is_open:
                continue
            open_count += 1
            price = prices.get(position.instrument_id)
            if price is None or price <= 0:
                unpriced += 1
                continue

            value = position.market_value(price)
            positions_value += value
            exposure = abs(value)
            gross += exposure
            if position.quantity < 0:
                short_exposure += exposure
            else:
                long_exposure += exposure

            unrealized = position.unrealized(price)
            if position.max_favourable is None or unrealized > position.max_favourable:
                position.max_favourable = unrealized
            if position.max_adverse is None or unrealized < position.max_adverse:
                position.max_adverse = unrealized

        return LedgerSnapshot(
            timestamp=timestamp, cash=self.cash, positions_value=positions_value,
            gross_exposure=gross, long_exposure=long_exposure,
            short_exposure=short_exposure, open_positions=open_count,
            unpriced_positions=unpriced)

    def equity(self, prices: Dict[str, Optional[float]]) -> float:
        total = self.cash
        for position in self.positions.values():
            if not position.is_open:
                continue
            price = prices.get(position.instrument_id)
            if price is not None and price > 0:
                total += position.market_value(price)
        return total

    # ---------------- bridging to Phase 11 ----------------

    def to_positions(self, portfolio_id: str,
                     as_of: Optional[datetime] = None) -> List[Position]:
        """
        Express the simulated book as Phase 11 `Position` objects.

        This is what lets the REAL risk engine evaluate the simulated
        portfolio. Spec §25 forbids a simplified "fake risk" layer for
        the backtest, and the cheapest way to guarantee the real one is
        used is to hand it the exact type it already consumes.
        """
        out: List[Position] = []
        for position in self.positions.values():
            if not position.is_open:
                continue
            out.append(Position(
                position_id=f"sim-{portfolio_id}-{position.instrument_id}",
                portfolio_id=portfolio_id,
                instrument_id=position.instrument_id,
                quantity=position.quantity,
                average_entry_price=position.average_cost,
                currency=self.base_currency,
                status=PositionStatus.OPEN,
                source=PositionSource.SIMULATED,
                opened_at=position.opened_at or as_of,
            ))
        return out

    # ---------------- reporting ----------------

    def open_positions(self) -> List[LedgerPosition]:
        return [p for p in self.positions.values() if p.is_open]

    def close_all(self, timestamp: datetime,
                  prices: Dict[str, Optional[float]],
                  reason: str = "end of backtest") -> List[Trade]:
        """
        Liquidate every open position at the last known price.

        Called at the end of a run so final performance reflects
        realised results rather than leaving open exposure whose value
        depends on a mark. Positions with no price stay open and are
        reported — force-closing them at an invented price would
        fabricate the very P&L this exists to measure honestly.

        Charges NO commission or slippage: this is an accounting
        liquidation for reporting, not a simulated trade, and pretending
        it cost something would be as wrong as pretending it was free
        if it had really happened.
        """
        closed: List[Trade] = []
        for position in list(self.positions.values()):
            if not position.is_open:
                continue
            price = prices.get(position.instrument_id)
            if price is None or price <= 0:
                continue
            quantity = abs(position.quantity)
            side = OrderSide.SELL if position.quantity > 0 else OrderSide.BUY
            synthetic = SimulatedFill(
                fill_id=f"liq-{position.instrument_id}",
                run_id=self.run_id, order_id="liquidation",
                instrument_id=position.instrument_id, side=side,
                quantity=quantity, price=price, reference_price=price,
                filled_at=timestamp, commission=0.0, slippage_cost=0.0)
            trade = self.apply_fill(synthetic, exit_reason=reason)
            if trade is not None:
                closed.append(trade)
        return closed

    def describe(self) -> Dict[str, object]:
        return {
            "initial_capital": self.initial_capital,
            "cash": self.cash,
            "open_positions": len(self.open_positions()),
            "realized_pnl": self.realized_pnl,
            "total_costs": self.total_costs,
            "total_slippage": self.total_slippage,
            "trades": len(self.trades),
            "cost_basis_method": "weighted average",
        }
