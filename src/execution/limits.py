"""
src/execution/limits.py
----------------------------
Capital, loss, position and margin limits, and the pre-trade gates
that enforce them (Phase 16, spec §12, §13, §14, §15, §16, §25, §26,
§27, §29, §63, §64, §65).

WHY THESE ARE SEPARATE FROM PHASE 11 RISK
---------------------------------------------
Phase 11 answers "is this a sound portfolio decision" — exposure,
correlation, volatility, sector concentration. This module answers a
narrower and blunter question: "have we lost too much today, is this
order larger than we permit, is there enough margin".

They are different in kind. Phase 11's limits are about portfolio
construction and are argued about by a researcher. These are
operational circuit breakers argued about by whoever is responsible
for the account, and they must hold even when the portfolio logic
believes the trade is excellent. So they are a separate gate, checked
after risk approval rather than inside it.

Nothing here re-implements Phase 11. `RiskGovernor` consumes the Phase
11 verdict and adds the operational limits on top.

EVERY GATE FAILS CLOSED
---------------------------
An unmeasurable limit blocks. Not knowing the day's P&L is not the
same as knowing it is fine, and a gate that passed on missing data
would be most permissive exactly when instrumentation had broken.

THE STALENESS GATES ARE THE ONES PEOPLE FORGET
--------------------------------------------------
Spec §15 asks for quote, instrument, account AND risk staleness. The
first is obvious and the last three are the ones that bite: an account
snapshot from an hour ago will happily authorise an order against
buying power that has since been spent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.domain.broker_models import (
    AccountSnapshot, CanonicalOrderSide, ExecutionRejectCode, PositionSnapshot,
    finite_or_none,
)


def _require_utc(value: Optional[datetime], name: str) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    if value.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError(f"{name} must be in UTC")
    return value


class LimitBreach(str, Enum):
    """
    Which limit stopped an order (spec §10).

    Enumerated so breaches are countable, alertable, and distinguishable
    in a post-mortem from ordinary risk rejections.
    """
    DAILY_REALIZED_LOSS = "daily_realized_loss"
    DAILY_TOTAL_LOSS = "daily_total_loss"
    PORTFOLIO_DRAWDOWN = "portfolio_drawdown"
    STRATEGY_DRAWDOWN = "strategy_drawdown"
    ACCOUNT_DRAWDOWN = "account_drawdown"
    MAX_ORDER_NOTIONAL = "max_order_notional"
    MAX_POSITION_SIZE = "max_position_size"
    MAX_POSITION_NOTIONAL = "max_position_notional"
    MAX_PORTFOLIO_EXPOSURE = "max_portfolio_exposure"
    MAX_STRATEGY_EXPOSURE = "max_strategy_exposure"
    MAX_INSTRUMENT_EXPOSURE = "max_instrument_exposure"
    MAX_OPEN_POSITIONS = "max_open_positions"
    MAX_LIVE_CAPITAL = "max_live_capital"
    CAPITAL_ALLOCATION = "capital_allocation"
    MAX_LEVERAGE = "max_leverage"
    INSUFFICIENT_MARGIN = "insufficient_margin"
    MAINTENANCE_MARGIN = "maintenance_margin"
    LIQUIDITY = "liquidity"
    STALE_QUOTE = "stale_quote"
    STALE_ACCOUNT = "stale_account"
    STALE_RISK = "stale_risk"
    STALE_POSITION = "stale_position"
    DELAYED_QUOTE = "delayed_quote"
    MAX_DAILY_ORDERS = "max_daily_orders"
    BROKER_HEALTH = "broker_health"
    RECONCILIATION = "reconciliation"
    CLOCK_DRIFT = "clock_drift"
    NOT_MEASURED = "not_measured"

    @property
    def requires_reactivation(self) -> bool:
        """
        Breaches that must not clear themselves.

        A daily loss limit that resumed trading the moment the market
        ticked back up would defeat its own purpose — the point is to
        stop for the day and make a human decide. Staleness and health,
        by contrast, legitimately recover.
        """
        return self in (LimitBreach.DAILY_REALIZED_LOSS,
                        LimitBreach.DAILY_TOTAL_LOSS,
                        LimitBreach.PORTFOLIO_DRAWDOWN,
                        LimitBreach.STRATEGY_DRAWDOWN,
                        LimitBreach.ACCOUNT_DRAWDOWN,
                        LimitBreach.MAX_LIVE_CAPITAL)


#: Which canonical reject code each breach maps to, so the Phase 14
#: validation result stays the single vocabulary downstream.
BREACH_TO_REJECT: Dict[LimitBreach, ExecutionRejectCode] = {
    LimitBreach.INSUFFICIENT_MARGIN: ExecutionRejectCode.INSUFFICIENT_MARGIN,
    LimitBreach.MAINTENANCE_MARGIN: ExecutionRejectCode.INSUFFICIENT_MARGIN,
    LimitBreach.MAX_LEVERAGE: ExecutionRejectCode.INSUFFICIENT_MARGIN,
    LimitBreach.STALE_QUOTE: ExecutionRejectCode.STALE_DATA,
    LimitBreach.STALE_ACCOUNT: ExecutionRejectCode.STALE_DATA,
    LimitBreach.STALE_RISK: ExecutionRejectCode.STALE_DATA,
    LimitBreach.STALE_POSITION: ExecutionRejectCode.STALE_DATA,
    LimitBreach.DELAYED_QUOTE: ExecutionRejectCode.STALE_DATA,
    LimitBreach.CLOCK_DRIFT: ExecutionRejectCode.STALE_DATA,
    LimitBreach.BROKER_HEALTH: ExecutionRejectCode.BROKER_DISCONNECTED,
    LimitBreach.LIQUIDITY: ExecutionRejectCode.INVALID_QUANTITY,
    LimitBreach.NOT_MEASURED: ExecutionRejectCode.RISK_UNAVAILABLE,
}


def reject_code_for(breach: LimitBreach) -> ExecutionRejectCode:
    return BREACH_TO_REJECT.get(breach, ExecutionRejectCode.POSITION_LIMIT)


@dataclass
class CapitalLimits:
    """
    Hard operational caps (spec §25, §26, §64).

    Every field is Optional and every default is None — meaning "not
    configured", which the governor treats as **not permitted** for a
    real-money level and permitted for paper. Shipping a number as a
    default would make it a production default by inattention, which
    spec §25 explicitly warns against.
    """
    max_live_capital: Optional[float] = None
    max_order_notional: Optional[float] = None
    max_position_notional: Optional[float] = None
    max_position_quantity: Optional[float] = None
    max_open_positions: Optional[int] = None
    max_portfolio_exposure: Optional[float] = None
    max_strategy_exposure: Optional[float] = None
    max_instrument_exposure: Optional[float] = None
    max_leverage: Optional[float] = None
    #: Fraction of equity that may be deployed at all.
    capital_fraction: Optional[float] = None
    reserve_cash: Optional[float] = None
    #: Orders per day. A runaway loop is a capital risk before it is
    #: an engineering one, so the ceiling lives with the other caps.
    max_daily_orders: Optional[int] = None
    #: Per-strategy budgets. Absent means unbudgeted, which is refused
    #: at real-money levels.
    strategy_capital: Dict[str, float] = field(default_factory=dict)

    @property
    def configured_for_real_money(self) -> bool:
        """
        Whether enough is set to consider real money at all.

        No strategy may acquire unlimited capital automatically (§26),
        so the caps that bound total exposure must exist before any
        real-money level is even evaluated.
        """
        return all(v is not None for v in (
            self.max_live_capital, self.max_order_notional,
            self.max_position_notional, self.max_leverage))


@dataclass
class LossLimits:
    """Daily and drawdown circuit breakers (spec §12)."""
    daily_realized_loss: Optional[float] = None
    daily_total_loss: Optional[float] = None
    daily_loss_pct: Optional[float] = None
    portfolio_drawdown_pct: Optional[float] = None
    strategy_drawdown_pct: Optional[float] = None
    account_drawdown_pct: Optional[float] = None


@dataclass
class FreshnessLimits:
    """
    How old each input may be (spec §15).

    Four separate budgets because they age differently. A quote is
    stale in a minute; an account snapshot is usually fine for several;
    a risk evaluation may legitimately stand for a whole session.
    """
    quote_max_age_seconds: float = 60.0
    account_max_age_seconds: float = 300.0
    position_max_age_seconds: float = 300.0
    risk_max_age_seconds: float = 900.0
    max_clock_drift_seconds: float = 5.0


@dataclass
class ExecutionQualityLimits:
    """Thresholds that pause execution when quality degrades (§63)."""
    max_slippage_bps: Optional[float] = 50.0
    max_submit_latency_ms: Optional[float] = 5_000.0
    max_rejection_rate: Optional[float] = 0.25
    max_unknown_state_rate: Optional[float] = 0.05
    max_reconciliation_mismatch_rate: Optional[float] = 0.05


@dataclass
class LimitDecision:
    """
    The verdict of the operational gate.

    Collects ALL breaches rather than stopping at the first: one tells
    an operator what to change, the whole list tells them whether
    changing it will help.
    """
    permitted: bool = True
    breaches: List[Tuple[LimitBreach, str]] = field(default_factory=list)
    checks_performed: int = 0
    #: Breaches that will not clear on their own.
    latching: List[LimitBreach] = field(default_factory=list)

    def deny(self, breach: LimitBreach, detail: str) -> "LimitDecision":
        self.permitted = False
        self.breaches.append((breach, detail))
        if breach.requires_reactivation and breach not in self.latching:
            self.latching.append(breach)
        return self

    @property
    def codes(self) -> List[ExecutionRejectCode]:
        return [reject_code_for(b) for b, _ in self.breaches]

    @property
    def first_breach(self) -> Optional[LimitBreach]:
        return self.breaches[0][0] if self.breaches else None

    def explain(self) -> str:
        if self.permitted:
            return "All operational limits satisfied."
        return " | ".join(f"{b.value}: {d}" for b, d in self.breaches)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "permitted": self.permitted,
            "checks_performed": self.checks_performed,
            "breaches": [{"limit": b.value, "detail": d}
                         for b, d in self.breaches],
            "latching": [b.value for b in self.latching],
        }


@dataclass
class DayState:
    """
    What has happened today, for the loss limits to measure against.

    `day` is a date so a session spanning midnight resets correctly
    rather than carrying yesterday's losses into a new limit window.
    """
    day: Optional[str] = None
    realized_pnl: float = 0.0
    unrealized_pnl: Optional[float] = None
    starting_equity: Optional[float] = None
    current_equity: Optional[float] = None
    peak_equity: Optional[float] = None
    orders_submitted: int = 0
    orders_rejected: int = 0

    @property
    def total_pnl(self) -> Optional[float]:
        if self.unrealized_pnl is None:
            return None
        return self.realized_pnl + self.unrealized_pnl

    @property
    def loss_fraction(self) -> Optional[float]:
        if not self.starting_equity or self.current_equity is None:
            return None
        return (self.current_equity - self.starting_equity) / self.starting_equity

    @property
    def drawdown(self) -> Optional[float]:
        if not self.peak_equity or self.current_equity is None:
            return None
        return (self.current_equity - self.peak_equity) / self.peak_equity


@dataclass
class MarketContext:
    """Freshness and liquidity inputs for one prospective order."""
    quote_at: Optional[datetime] = None
    account_at: Optional[datetime] = None
    position_at: Optional[datetime] = None
    risk_at: Optional[datetime] = None
    broker_time: Optional[datetime] = None
    reference_price: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    average_volume: Optional[float] = None
    #: True only when the venue reports genuinely live data.
    quote_is_live: bool = False


class RiskGovernor:
    """
    The operational gate that sits after Phase 11 risk.

    Consumes the Phase 11 verdict; never re-derives it. Everything this
    adds is an operational circuit breaker that must hold even when the
    portfolio logic believes the trade is excellent.
    """

    def __init__(self, capital: Optional[CapitalLimits] = None,
                 loss: Optional[LossLimits] = None,
                 freshness: Optional[FreshnessLimits] = None,
                 quality: Optional[ExecutionQualityLimits] = None):
        self.capital = capital or CapitalLimits()
        self.loss = loss or LossLimits()
        self.freshness = freshness or FreshnessLimits()
        self.quality = quality or ExecutionQualityLimits()
        #: Breaches that latched and need explicit reactivation.
        self._latched: Dict[LimitBreach, str] = {}

    # ---------------- latching ----------------

    @property
    def latched(self) -> Dict[str, str]:
        return {b.value: reason for b, reason in self._latched.items()}

    def reactivate(self, breach: LimitBreach, actor: str, reason: str) -> bool:
        """
        Clear a latched breach. Requires an actor and a reason (§12).

        A limit that could clear itself would not be a limit.
        """
        if not actor or not reason:
            raise ValueError("reactivation requires an actor and a reason")
        return self._latched.pop(breach, None) is not None

    def reactivate_all(self, actor: str, reason: str) -> int:
        if not actor or not reason:
            raise ValueError("reactivation requires an actor and a reason")
        count = len(self._latched)
        self._latched.clear()
        return count

    # ---------------- the gate ----------------

    def check(self, now: datetime, *,
              side: CanonicalOrderSide,
              quantity: float,
              price: Optional[float],
              instrument_id: str,
              strategy_id: Optional[str] = None,
              account: Optional[AccountSnapshot] = None,
              positions: Sequence[PositionSnapshot] = (),
              day: Optional[DayState] = None,
              market: Optional[MarketContext] = None,
              broker_healthy: Optional[bool] = None,
              reconciliation_clean: Optional[bool] = None,
              quality_metrics: Optional[Dict[str, float]] = None,
              require_real_money_config: bool = False) -> LimitDecision:
        """
        Every operational limit, in one pass.

        Ordered cheapest-and-broadest first so the first breach is the
        most actionable one.
        """
        decision = LimitDecision()
        market = market or MarketContext()

        # --- 0. anything already latched stays shut -----------------
        decision.checks_performed += 1
        for breach, reason in self._latched.items():
            decision.deny(breach, f"latched: {reason}")

        # --- 1. staleness (spec §15) --------------------------------
        for label, stamp, budget, breach in (
            ("quote", market.quote_at, self.freshness.quote_max_age_seconds,
             LimitBreach.STALE_QUOTE),
            ("account", market.account_at, self.freshness.account_max_age_seconds,
             LimitBreach.STALE_ACCOUNT),
            ("position", market.position_at, self.freshness.position_max_age_seconds,
             LimitBreach.STALE_POSITION),
            ("risk", market.risk_at, self.freshness.risk_max_age_seconds,
             LimitBreach.STALE_RISK),
        ):
            decision.checks_performed += 1
            if stamp is None:
                decision.deny(breach, f"{label} state has no timestamp, so its "
                                      f"age cannot be established")
                continue
            age = (now - stamp).total_seconds()
            if age < 0:
                decision.deny(breach, f"{label} state is stamped in the future "
                                      f"by {abs(age):.1f}s")
            elif age > budget:
                decision.deny(breach, f"{label} state is {age:.1f}s old, "
                                      f"budget {budget:.0f}s")

        # A quote can be recent and still not be a live quote. IBKR
        # serves delayed and frozen data on the same endpoint, and a
        # frozen quote carries the timestamp of the moment it froze,
        # so age alone would call it fresh.
        decision.checks_performed += 1
        if not market.quote_is_live:
            decision.deny(
                LimitBreach.DELAYED_QUOTE,
                "the quote is delayed, frozen or of unconfirmed provenance; "
                "its age says nothing about whether it is current")

        # --- 2. clock drift (spec §17) ------------------------------
        decision.checks_performed += 1
        if market.broker_time is not None:
            drift = abs((now - market.broker_time).total_seconds())
            if drift > self.freshness.max_clock_drift_seconds:
                decision.deny(
                    LimitBreach.CLOCK_DRIFT,
                    f"our clock and the broker's differ by {drift:.1f}s "
                    f"(budget {self.freshness.max_clock_drift_seconds:.0f}s)")

        # --- 3. broker health and reconciliation (spec §16, §32) ----
        decision.checks_performed += 1
        if broker_healthy is None:
            decision.deny(LimitBreach.BROKER_HEALTH,
                          "broker health was not measured")
        elif not broker_healthy:
            decision.deny(LimitBreach.BROKER_HEALTH,
                          "broker health is not satisfactory for new orders")

        decision.checks_performed += 1
        if reconciliation_clean is None:
            decision.deny(LimitBreach.RECONCILIATION,
                          "reconciliation state was not measured")
        elif not reconciliation_clean:
            decision.deny(LimitBreach.RECONCILIATION,
                          "an unresolved reconciliation mismatch is outstanding")

        # --- 4. daily loss and drawdown (spec §12) ------------------
        decision.checks_performed += 1
        if day is not None:
            self._check_losses(decision, day)

        # --- 5. capital and notional (spec §25, §26, §64) -----------
        decision.checks_performed += 1
        if require_real_money_config and not self.capital.configured_for_real_money:
            decision.deny(
                LimitBreach.MAX_LIVE_CAPITAL,
                "real-money capital caps are not configured; no strategy may "
                "acquire unlimited capital automatically")

        notional = None
        effective_price = finite_or_none(price if price is not None
                                         else market.reference_price)
        if effective_price is not None and quantity:
            notional = abs(quantity) * effective_price

        decision.checks_performed += 1
        if self.capital.max_order_notional is not None:
            if notional is None:
                decision.deny(LimitBreach.MAX_ORDER_NOTIONAL,
                              "order notional could not be computed, so the "
                              "cap cannot be enforced")
            elif notional > self.capital.max_order_notional:
                decision.deny(
                    LimitBreach.MAX_ORDER_NOTIONAL,
                    f"order notional {notional:,.2f} exceeds "
                    f"{self.capital.max_order_notional:,.2f}")

        decision.checks_performed += 1
        if (self.capital.max_daily_orders is not None and day is not None
                and day.orders_submitted >= self.capital.max_daily_orders):
            decision.deny(
                LimitBreach.MAX_DAILY_ORDERS,
                f"{day.orders_submitted} orders already submitted today; "
                f"the ceiling is {self.capital.max_daily_orders}")

        # --- 6. position limits (spec §13) --------------------------
        self._check_positions(decision, side, quantity, effective_price,
                              instrument_id, positions, account)

        # --- 7. margin and leverage (spec §14) ----------------------
        self._check_margin(decision, account, notional, side)

        # --- 8. liquidity (spec §65) --------------------------------
        decision.checks_performed += 1
        if market.average_volume is not None and quantity:
            share = abs(quantity) / market.average_volume
            if share > 0.10:
                decision.deny(
                    LimitBreach.LIQUIDITY,
                    f"order is {share:.1%} of average volume; above the 10% "
                    f"participation ceiling")

        # --- 9. execution quality (spec §63) ------------------------
        decision.checks_performed += 1
        if quality_metrics:
            self._check_quality(decision, quality_metrics)

        # Latch anything that must not clear itself.
        for breach, detail in decision.breaches:
            if breach.requires_reactivation:
                self._latched.setdefault(breach, detail)

        return decision

    # ---------------- component checks ----------------

    def _check_losses(self, decision: LimitDecision, day: DayState) -> None:
        limits = self.loss

        if limits.daily_realized_loss is not None:
            if day.realized_pnl < -abs(limits.daily_realized_loss):
                decision.deny(
                    LimitBreach.DAILY_REALIZED_LOSS,
                    f"realized {day.realized_pnl:,.2f} today, limit "
                    f"{-abs(limits.daily_realized_loss):,.2f}")

        if limits.daily_total_loss is not None:
            total = day.total_pnl
            if total is None:
                decision.deny(LimitBreach.DAILY_TOTAL_LOSS,
                              "total P&L is unknown because positions could "
                              "not be priced")
            elif total < -abs(limits.daily_total_loss):
                decision.deny(
                    LimitBreach.DAILY_TOTAL_LOSS,
                    f"total {total:,.2f} today, limit "
                    f"{-abs(limits.daily_total_loss):,.2f}")

        if limits.daily_loss_pct is not None:
            fraction = day.loss_fraction
            if fraction is not None and fraction < -abs(limits.daily_loss_pct):
                decision.deny(
                    LimitBreach.DAILY_TOTAL_LOSS,
                    f"down {fraction:.2%} today, limit "
                    f"{-abs(limits.daily_loss_pct):.2%}")

        for value, breach, label in (
            (limits.portfolio_drawdown_pct, LimitBreach.PORTFOLIO_DRAWDOWN,
             "portfolio"),
            (limits.account_drawdown_pct, LimitBreach.ACCOUNT_DRAWDOWN,
             "account"),
        ):
            if value is None:
                continue
            drawdown = day.drawdown
            if drawdown is not None and drawdown < -abs(value):
                decision.deny(breach,
                              f"{label} drawdown {drawdown:.2%}, limit "
                              f"{-abs(value):.2%}")

    def _check_positions(self, decision: LimitDecision,
                         side: CanonicalOrderSide, quantity: float,
                         price: Optional[float], instrument_id: str,
                         positions: Sequence[PositionSnapshot],
                         account: Optional[AccountSnapshot]) -> None:
        held = 0.0
        for position in positions:
            if position.instrument_id == instrument_id:
                held = position.quantity
                break
        projected = held + abs(quantity) * side.sign

        decision.checks_performed += 1
        if self.capital.max_position_quantity is not None:
            if abs(projected) > self.capital.max_position_quantity + 1e-9:
                decision.deny(
                    LimitBreach.MAX_POSITION_SIZE,
                    f"projected {projected:g} units exceeds "
                    f"{self.capital.max_position_quantity:g}")

        decision.checks_performed += 1
        if self.capital.max_position_notional is not None and price is not None:
            projected_notional = abs(projected) * price
            if projected_notional > self.capital.max_position_notional:
                decision.deny(
                    LimitBreach.MAX_POSITION_NOTIONAL,
                    f"projected position {projected_notional:,.2f} exceeds "
                    f"{self.capital.max_position_notional:,.2f}")

        decision.checks_performed += 1
        if self.capital.max_open_positions is not None:
            open_now = sum(1 for p in positions if abs(p.quantity) > 1e-9)
            opening_new = abs(held) <= 1e-9 and abs(projected) > 1e-9
            if opening_new and open_now >= self.capital.max_open_positions:
                decision.deny(
                    LimitBreach.MAX_OPEN_POSITIONS,
                    f"{open_now} positions already open, limit "
                    f"{self.capital.max_open_positions}")

        decision.checks_performed += 1
        if (self.capital.max_instrument_exposure is not None
                and account is not None and account.equity and price is not None):
            weight = abs(projected) * price / account.equity
            if weight > self.capital.max_instrument_exposure:
                decision.deny(
                    LimitBreach.MAX_INSTRUMENT_EXPOSURE,
                    f"{instrument_id} would be {weight:.1%} of equity, limit "
                    f"{self.capital.max_instrument_exposure:.1%}")

        decision.checks_performed += 1
        if (self.capital.max_portfolio_exposure is not None
                and account is not None and account.equity):
            gross = sum(abs(p.quantity) * (p.market_price or p.average_price)
                        for p in positions)
            if price is not None:
                gross += abs(quantity) * price
            exposure = gross / account.equity
            if exposure > self.capital.max_portfolio_exposure:
                decision.deny(
                    LimitBreach.MAX_PORTFOLIO_EXPOSURE,
                    f"gross exposure would be {exposure:.1%} of equity, limit "
                    f"{self.capital.max_portfolio_exposure:.1%}")

    def _check_margin(self, decision: LimitDecision,
                      account: Optional[AccountSnapshot],
                      notional: Optional[float],
                      side: CanonicalOrderSide) -> None:
        decision.checks_performed += 1
        if account is None:
            if self.capital.max_leverage is not None:
                decision.deny(LimitBreach.INSUFFICIENT_MARGIN,
                              "no account snapshot, so margin cannot be checked")
            return

        if (notional is not None and side is CanonicalOrderSide.BUY
                and account.spendable is not None):
            available = account.spendable
            if self.capital.reserve_cash is not None:
                available -= self.capital.reserve_cash
            if notional > available + 1e-6:
                decision.deny(
                    LimitBreach.INSUFFICIENT_MARGIN,
                    f"needs {notional:,.2f}, available "
                    f"{available:,.2f} after reserve")

        decision.checks_performed += 1
        maintenance = account.margin.maintenance_margin
        if maintenance is not None and account.equity:
            if account.equity < maintenance:
                decision.deny(
                    LimitBreach.MAINTENANCE_MARGIN,
                    f"equity {account.equity:,.2f} is below maintenance "
                    f"margin {maintenance:,.2f}")

        decision.checks_performed += 1
        if self.capital.max_leverage is not None and account.equity:
            gross = (notional or 0.0)
            leverage = gross / account.equity
            if leverage > self.capital.max_leverage:
                decision.deny(
                    LimitBreach.MAX_LEVERAGE,
                    f"this order alone is {leverage:.2f}x equity, limit "
                    f"{self.capital.max_leverage:.2f}x")

    def _check_quality(self, decision: LimitDecision,
                       metrics: Dict[str, float]) -> None:
        for key, limit, breach, label in (
            ("median_slippage_bps", self.quality.max_slippage_bps,
             LimitBreach.LIQUIDITY, "median slippage"),
            ("submit_latency_ms", self.quality.max_submit_latency_ms,
             LimitBreach.BROKER_HEALTH, "submission latency"),
            ("rejection_rate", self.quality.max_rejection_rate,
             LimitBreach.BROKER_HEALTH, "rejection rate"),
            ("unknown_state_rate", self.quality.max_unknown_state_rate,
             LimitBreach.BROKER_HEALTH, "unknown-state rate"),
            ("reconciliation_mismatch_rate",
             self.quality.max_reconciliation_mismatch_rate,
             LimitBreach.RECONCILIATION, "reconciliation mismatch rate"),
        ):
            if limit is None:
                continue
            observed = finite_or_none(metrics.get(key))
            if observed is not None and observed > limit:
                decision.deny(breach,
                              f"{label} {observed:g} exceeds threshold {limit:g}")

    # ---------------- reporting ----------------

    def state(self) -> Dict[str, Any]:
        return {
            "latched": self.latched,
            "capital": {
                "max_live_capital": self.capital.max_live_capital,
                "max_order_notional": self.capital.max_order_notional,
                "max_position_notional": self.capital.max_position_notional,
                "max_open_positions": self.capital.max_open_positions,
                "max_leverage": self.capital.max_leverage,
                "configured_for_real_money": self.capital.configured_for_real_money,
            },
            "loss": {
                "daily_realized_loss": self.loss.daily_realized_loss,
                "daily_total_loss": self.loss.daily_total_loss,
                "daily_loss_pct": self.loss.daily_loss_pct,
                "portfolio_drawdown_pct": self.loss.portfolio_drawdown_pct,
            },
            "freshness": {
                "quote_seconds": self.freshness.quote_max_age_seconds,
                "account_seconds": self.freshness.account_max_age_seconds,
                "risk_seconds": self.freshness.risk_max_age_seconds,
                "max_clock_drift_seconds": self.freshness.max_clock_drift_seconds,
            },
        }


def paper_limits() -> RiskGovernor:
    """
    A governor sized for paper trading.

    Deliberately not offered as a real-money preset — those caps are an
    operator's decision about their own capital, and a default here
    would become a production default by inattention.
    """
    return RiskGovernor(
        capital=CapitalLimits(
            max_order_notional=25_000.0,
            max_position_notional=50_000.0,
            max_open_positions=20,
            max_portfolio_exposure=1.0,
            max_instrument_exposure=0.25,
            max_leverage=1.0),
        loss=LossLimits(daily_loss_pct=0.05, portfolio_drawdown_pct=0.20),
        freshness=FreshnessLimits(),
        quality=ExecutionQualityLimits())
