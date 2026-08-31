"""
src/domain/backtest_models.py
----------------------------------
Backtesting domain models (Phase 12).

WHAT A BACKTEST IS HERE
---------------------------
A reconstruction of the whole decision chain as it would have run at
each historical moment: information at T, features, prediction, signal,
portfolio context, risk decision, allocation, simulated execution,
portfolio state. Not a return series multiplied by a weight vector.

That distinction drives the type list below. An order is not a fill, a
fill is not a trade, and none of them is a signal. Each is separately
recorded because the questions this phase exists to answer — "why did
this trade happen?", "what did risk refuse?", "how much did costs
take?" — are unanswerable once those are collapsed into one row.

WHAT IS DELIBERATELY REUSED, NOT REDEFINED
----------------------------------------------
Signal, Position, PortfolioSnapshot, AllocationProposal, RiskDecision
and OrderIntent all come from Phases 10 and 11 unchanged. Phase 12
adds only what simulation genuinely needs: orders, fills, trades,
costs, the run's identity, and its results. A parallel Signal or
Position type would let the backtest drift away from the logic it is
supposed to be testing, which would make every result meaningless.

ORDER STATE IS NOT SIGNAL STATE
-----------------------------------
`OrderState.FILLED` and `SignalStatus.ACTIVE` describe different
objects at different layers. They are separate enums on purpose; spec
§17 calls this out because merging them is a common and quietly
destructive shortcut.

NOTHING HERE EXECUTES
-------------------------
`SimulatedOrder` and `SimulatedFill` are records produced by a
simulator against cached historical bars. There is no venue, no
account, no broker, and no code path that sends anything anywhere.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from math import isfinite
from typing import Any, Dict, List, Optional, Tuple


# ============================================================
# Guards (same contract as Phases 9-11)
# ============================================================

def _require_utc(value: Optional[datetime], name: str) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware (got a naive datetime)")
    if value.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError(f"{name} must be in UTC (got offset {value.utcoffset()})")
    return value


def finite_or_none(value: Optional[float]) -> Optional[float]:
    """NaN and Infinity collapse to None — the Phase 11 rule, applied here too."""
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if isfinite(numeric) else None


def safe_ratio(numerator: Optional[float], denominator: Optional[float]) -> Optional[float]:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return finite_or_none(numerator / denominator)


# ============================================================
# Taxonomy
# ============================================================

class BacktestStatus(str, Enum):
    """
    Lifecycle of a run (spec §75).

    COMPLETED_WITH_WARNINGS exists so a run that finished but hit
    missing prices or skipped bars cannot be read as a clean result.
    Without it, every degraded run looks identical to a healthy one.
    """
    CREATED = "created"
    VALIDATING = "validating"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OrderState(str, Enum):
    """Simulated order lifecycle (spec §17). Distinct from SignalStatus."""
    CREATED = "created"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class RejectReason(str, Enum):
    """
    Why a simulated order did not fill.

    Enumerated rather than free text so rejection rates are queryable —
    "how often did we fail to fill for want of a price?" is a
    data-quality question about the backtest itself, and it must not
    require grepping logs. Same reasoning as Phase 10's
    SuppressionReason.
    """
    NO_PRICE = "no_price"
    MARKET_CLOSED = "market_closed"
    INSUFFICIENT_CASH = "insufficient_cash"
    ZERO_QUANTITY = "zero_quantity"
    BEYOND_HORIZON = "beyond_horizon"
    LIQUIDITY_CAP = "liquidity_cap"
    SHORTING_DISABLED = "shorting_disabled"


class ExecutionTiming(str, Enum):
    """
    When a simulated order is allowed to fill relative to the bar that
    produced the decision.

    SAME_BAR_CLOSE is available but flagged: deciding on information
    that includes a bar's close and then filling at that same close is
    the single most common way a backtest quietly reports impossible
    performance. It is offered for deliberate comparison, and any run
    using it is marked with a research-quality warning.
    """
    NEXT_BAR_OPEN = "next_bar_open"
    NEXT_BAR_CLOSE = "next_bar_close"
    SAME_BAR_CLOSE = "same_bar_close"


class ReplayTrigger(str, Enum):
    """What caused an evaluation to happen at this moment (spec §8, §9)."""
    SCHEDULED = "scheduled"
    EVENT = "event"


class WarningCode(str, Enum):
    """
    Research-quality warnings (spec §99).

    These describe the trustworthiness of a run, never its
    profitability. A run can be highly profitable and carry every
    warning below — that combination is exactly what they exist to make
    visible.
    """
    SMALL_SAMPLE = "small_sample"
    NO_SIGNALS = "no_signals"
    SHORT_HISTORY = "short_history"
    MISSING_PRICES = "missing_prices"
    SURVIVORSHIP_RISK = "survivorship_risk"
    SAME_BAR_EXECUTION = "same_bar_execution"
    ZERO_COSTS = "zero_costs"
    HIGH_TURNOVER = "high_turnover"
    NO_BENCHMARK = "no_benchmark"
    RETROACTIVE_ADJUSTMENT = "retroactive_adjustment"
    IN_SAMPLE_MODEL = "in_sample_model"
    NO_REGIME_DATA = "no_regime_data"
    LOW_LIQUIDITY = "low_liquidity"


# ============================================================
# Execution, cost and slippage configuration
# ============================================================

@dataclass(frozen=True)
class ExecutionAssumptions:
    """
    How information becomes a fill (spec §11, §12, §16).

    Latency is expressed in seconds between stages so that the ordering
    the guards assert — information <= signal <= order <= fill — is a
    property of the configuration rather than a hope about the code.
    """
    version: str = "exec-v1"
    timing: ExecutionTiming = ExecutionTiming.NEXT_BAR_OPEN
    #: Seconds between the information cutoff and the order being cut.
    signal_to_order_seconds: float = 60.0
    #: Maximum bars to wait for a fillable price before giving up.
    max_bars_to_fill: int = 3
    #: Cap on participation in a bar's volume. None disables the check.
    max_participation: Optional[float] = 0.10
    #: Whether partial fills are produced when participation binds.
    allow_partial_fills: bool = True
    allow_shorting: bool = False

    def describe(self) -> str:
        return (f"{self.timing.value} + {self.signal_to_order_seconds:.0f}s latency, "
                f"participation cap "
                f"{'none' if self.max_participation is None else f'{self.max_participation:.0%}'}")


@dataclass(frozen=True)
class CostModel:
    """
    Explicit, versioned transaction costs (spec §13).

    Every component is separate rather than folded into one "cost bps"
    number, because they behave differently: commission has a floor,
    fees scale with notional, and a per-share charge dominates for
    cheap instruments. Collapsing them hides which one is actually
    eating the returns.
    """
    version: str = "cost-v1"
    commission_bps: float = 0.0
    commission_per_share: float = 0.0
    minimum_commission: float = 0.0
    fee_bps: float = 0.0

    def charge(self, quantity: float, price: float) -> float:
        """Total cost for one fill. Always non-negative."""
        notional = abs(quantity) * price
        commission = max(
            notional * self.commission_bps / 10_000.0
            + abs(quantity) * self.commission_per_share,
            self.minimum_commission if notional > 0 else 0.0,
        )
        fees = notional * self.fee_bps / 10_000.0
        return finite_or_none(commission + fees) or 0.0

    @property
    def is_zero(self) -> bool:
        return not any((self.commission_bps, self.commission_per_share,
                        self.minimum_commission, self.fee_bps))


class SlippageMethod(str, Enum):
    NONE = "none"
    FIXED_BPS = "fixed_bps"
    VOLATILITY_SCALED = "volatility_scaled"
    PARTICIPATION_SCALED = "participation_scaled"


@dataclass(frozen=True)
class SlippageModel:
    """
    Versioned slippage (spec §14, §15).

    PARTICIPATION_SCALED is a deliberately SIMPLIFIED market-impact
    proxy: impact grows with the square root of participation, a
    conventional shape, but this project has no order-book data to
    calibrate it against. Spec §15 requires that to be labelled rather
    than presented as a realistic impact model — `is_simplified_impact`
    is what carries that label into the run's warnings.
    """
    version: str = "slip-v1"
    method: SlippageMethod = SlippageMethod.FIXED_BPS
    base_bps: float = 5.0
    #: Multiplier applied to recent volatility under VOLATILITY_SCALED.
    volatility_multiple: float = 0.10
    #: Coefficient on sqrt(participation) under PARTICIPATION_SCALED.
    impact_coefficient: float = 10.0

    @property
    def is_simplified_impact(self) -> bool:
        return self.method == SlippageMethod.PARTICIPATION_SCALED

    def slippage_bps(self, volatility: Optional[float] = None,
                     participation: Optional[float] = None) -> float:
        """
        Basis points of adverse price movement. Never negative: slippage
        that helped you is not slippage.
        """
        if self.method == SlippageMethod.NONE:
            return 0.0
        if self.method == SlippageMethod.FIXED_BPS:
            return max(0.0, self.base_bps)
        if self.method == SlippageMethod.VOLATILITY_SCALED:
            if volatility is None:
                # Unmeasurable volatility falls back to the base rate
                # rather than to zero — assuming no slippage because a
                # number was missing is the optimistic failure.
                return max(0.0, self.base_bps)
            daily = volatility / (252 ** 0.5)
            return max(0.0, self.base_bps + daily * self.volatility_multiple * 10_000.0)
        if self.method == SlippageMethod.PARTICIPATION_SCALED:
            if not participation or participation <= 0:
                return max(0.0, self.base_bps)
            return max(0.0, self.base_bps
                       + self.impact_coefficient * (participation ** 0.5) * 100.0)
        return max(0.0, self.base_bps)

    def apply(self, price: float, side: OrderSide, volatility: Optional[float] = None,
              participation: Optional[float] = None) -> float:
        """Move the price against the trader, in the direction of the trade."""
        bps = self.slippage_bps(volatility, participation)
        factor = 1.0 + (bps / 10_000.0) * (1.0 if side == OrderSide.BUY else -1.0)
        return finite_or_none(price * factor) or price


# ============================================================
# Configuration and identity
# ============================================================

@dataclass
class BacktestConfiguration:
    """
    Everything that determines a run's result (spec §5).

    Stored in full with the run. A configuration that lived only in the
    code that launched it would make the run unreproducible the moment
    that code changed — which is the ordinary case, not the exception.
    """
    name: str
    start: datetime
    end: datetime
    initial_capital: float = 100_000.0
    base_currency: str = "USD"

    #: Explicit instrument list. Empty means "whatever the signals
    #: reference", which is recorded as such rather than silently
    #: becoming today's full registry.
    universe: List[str] = field(default_factory=list)
    benchmark_instrument_id: Optional[str] = None

    #: Which stored strategy's signals to replay. None replays all.
    strategy_id: Optional[str] = None
    strategy_version: Optional[str] = None

    constraint_set_version: str = "v1"
    sizing_strategy_id: str = "fixed_fraction"
    sizing_target_weight: float = 0.05

    execution: ExecutionAssumptions = field(default_factory=ExecutionAssumptions)
    costs: CostModel = field(default_factory=CostModel)
    slippage: SlippageModel = field(default_factory=SlippageModel)

    #: Calendar days between scheduled evaluations. 1 = daily.
    rebalance_days: int = 1
    #: Also evaluate whenever a new signal's information arrives.
    event_driven: bool = True
    #: Close a holding once no live signal supports it.
    #:
    #: A rebalancing rule (spec §30), not sizing logic — sizing only
    #: proposes targets for instruments that HAVE a signal, so without
    #: this a position taken on a 5-day signal would ride untouched
    #: until the run ended. That is not the strategy being tested, and
    #: it silently converts a short-horizon signal strategy into
    #: buy-and-hold.
    exit_when_signal_expires: bool = True

    #: Risk-free rate as an annual fraction, for Sharpe/Sortino.
    #: Explicit rather than assumed zero (spec §36).
    risk_free_rate: float = 0.0
    risk_free_source: str = "assumed zero — no risk-free series in this database"

    random_seed: int = 0
    notes: str = ""

    def __post_init__(self):
        self.start = _require_utc(self.start, "start")
        self.end = _require_utc(self.end, "end")
        if self.start >= self.end:
            raise ValueError("start must be before end")
        if self.initial_capital <= 0:
            raise ValueError("initial_capital must be positive")
        if self.rebalance_days < 1:
            raise ValueError("rebalance_days must be at least 1")

    def fingerprint(self) -> str:
        """
        Stable hash of every field that can change the result.

        Two runs sharing a fingerprint should produce identical output;
        two that differ anywhere produce different ids, so an
        accidentally-changed assumption cannot masquerade as a rerun.
        """
        import hashlib
        parts = [
            self.name, self.start.isoformat(), self.end.isoformat(),
            f"{self.initial_capital:.6f}", self.base_currency,
            ",".join(sorted(self.universe)), str(self.benchmark_instrument_id),
            str(self.strategy_id), str(self.strategy_version),
            self.constraint_set_version, self.sizing_strategy_id,
            f"{self.sizing_target_weight:.6f}",
            self.execution.version, self.execution.timing.value,
            f"{self.execution.signal_to_order_seconds:.3f}",
            str(self.execution.max_participation), str(self.execution.allow_shorting),
            self.costs.version, f"{self.costs.commission_bps:.6f}",
            f"{self.costs.commission_per_share:.6f}",
            f"{self.costs.minimum_commission:.6f}", f"{self.costs.fee_bps:.6f}",
            self.slippage.version, self.slippage.method.value,
            f"{self.slippage.base_bps:.6f}",
            str(self.rebalance_days), str(self.event_driven),
            str(self.exit_when_signal_expires),
            f"{self.risk_free_rate:.6f}", str(self.random_seed),
        ]
        return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:20]


@dataclass
class RunIdentity:
    """
    Every version needed to reproduce a run (spec §6, §54).

    A result whose generating versions are unknown is an anecdote. The
    fields are all optional because some genuinely may not apply, but
    an absent one is visible rather than defaulted to something
    plausible.
    """
    backtest_id: str
    run_id: str
    config_fingerprint: str
    risk_engine_version: str = "v1"
    constraint_set_version: str = "v1"
    sizing_version: str = "v1"
    execution_model_version: str = "exec-v1"
    cost_model_version: str = "cost-v1"
    slippage_model_version: str = "slip-v1"
    calendar_version: str = "cal-derived-v1"
    strategy_version: Optional[str] = None
    model_version: Optional[str] = None
    feature_set_version: Optional[str] = None
    dataset_version: Optional[str] = None
    code_version: Optional[str] = None
    created_at: Optional[datetime] = None

    def __post_init__(self):
        _require_utc(self.created_at, "created_at")


# ============================================================
# Orders, fills, trades
# ============================================================

@dataclass
class SimulatedOrder:
    """One intent turned into an instruction the simulator can act on."""
    order_id: str
    run_id: str
    instrument_id: str
    side: OrderSide
    quantity: float
    state: OrderState = OrderState.CREATED
    #: The information state the deciding signal was built from.
    information_cutoff: Optional[datetime] = None
    decision_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    filled_quantity: float = 0.0
    reject_reason: Optional[RejectReason] = None
    signal_id: Optional[str] = None
    decision_id: Optional[str] = None
    intent_id: Optional[str] = None
    target_weight: Optional[float] = None
    note: str = ""

    def __post_init__(self):
        for name in ("information_cutoff", "decision_at", "created_at"):
            setattr(self, name, _require_utc(getattr(self, name), name))
        if self.quantity <= 0:
            raise ValueError("order quantity must be positive; direction lives on `side`")

    @property
    def remaining(self) -> float:
        return max(0.0, self.quantity - self.filled_quantity)

    @property
    def signed_filled(self) -> float:
        return self.filled_quantity * (1.0 if self.side == OrderSide.BUY else -1.0)


@dataclass
class SimulatedFill:
    """
    One execution against a cached historical bar.

    `reference_price` is the untouched bar price and `price` is what the
    simulator charged after slippage. Both are kept so the slippage
    actually applied is auditable rather than inferred.
    """
    fill_id: str
    run_id: str
    order_id: str
    instrument_id: str
    side: OrderSide
    quantity: float
    price: float
    reference_price: float
    filled_at: datetime
    commission: float = 0.0
    slippage_cost: float = 0.0
    bar_timestamp: Optional[datetime] = None
    participation: Optional[float] = None
    is_partial: bool = False

    def __post_init__(self):
        self.filled_at = _require_utc(self.filled_at, "filled_at")
        _require_utc(self.bar_timestamp, "bar_timestamp")

    @property
    def notional(self) -> float:
        return abs(self.quantity) * self.price

    @property
    def total_cost(self) -> float:
        return self.commission + self.slippage_cost

    @property
    def signed_quantity(self) -> float:
        return self.quantity * (1.0 if self.side == OrderSide.BUY else -1.0)


@dataclass
class Trade:
    """
    A completed round trip: an entry and the exit that closed it
    (spec §18).

    Recorded only when a position is reduced or closed, because P&L is
    not defined until then. Open exposure is visible on the portfolio
    snapshot instead — mixing the two would double-count.
    """
    trade_id: str
    run_id: str
    instrument_id: str
    side: OrderSide                 # side of the ENTRY
    quantity: float
    entry_price: float
    exit_price: float
    entry_at: datetime
    exit_at: datetime
    gross_pnl: float = 0.0
    costs: float = 0.0
    entry_signal_id: Optional[str] = None
    entry_decision_id: Optional[str] = None
    exit_reason: str = ""
    sector_id: Optional[str] = None
    strategy_id: Optional[str] = None
    #: Peak favourable / adverse excursion while the trade was open.
    mfe: Optional[float] = None
    mae: Optional[float] = None

    def __post_init__(self):
        self.entry_at = _require_utc(self.entry_at, "entry_at")
        self.exit_at = _require_utc(self.exit_at, "exit_at")

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.costs

    @property
    def is_win(self) -> bool:
        return self.net_pnl > 0

    @property
    def holding_days(self) -> float:
        return (self.exit_at - self.entry_at).total_seconds() / 86400.0

    @property
    def return_pct(self) -> Optional[float]:
        """Net return on the capital the entry committed."""
        basis = abs(self.quantity * self.entry_price)
        return safe_ratio(self.net_pnl, basis)


# ============================================================
# Time series and results
# ============================================================

@dataclass
class EquityPoint:
    """One observation of the simulated portfolio's value."""
    timestamp: datetime
    equity: float
    cash: float
    positions_value: float
    gross_exposure: float = 0.0
    net_exposure: float = 0.0
    benchmark_value: Optional[float] = None
    drawdown: Optional[float] = None
    open_positions: int = 0

    def __post_init__(self):
        self.timestamp = _require_utc(self.timestamp, "timestamp")


@dataclass
class DrawdownEpisode:
    """A peak-to-trough-to-recovery cycle (spec §37)."""
    peak_at: datetime
    peak_equity: float
    trough_at: datetime
    trough_equity: float
    depth: float                     # negative fraction
    recovered_at: Optional[datetime] = None

    @property
    def duration_days(self) -> float:
        return (self.trough_at - self.peak_at).total_seconds() / 86400.0

    @property
    def recovery_days(self) -> Optional[float]:
        if self.recovered_at is None:
            return None
        return (self.recovered_at - self.trough_at).total_seconds() / 86400.0

    @property
    def is_recovered(self) -> bool:
        return self.recovered_at is not None


@dataclass
class PerformanceMetrics:
    """
    Computed results (spec §34).

    Every field is Optional and defaults to None. A metric that the
    sample cannot support is absent, not zero — spec §34 says not to
    calculate metrics when insufficient data exists, and None is how
    that absence travels.
    """
    observations: int = 0
    trading_days: int = 0

    initial_capital: Optional[float] = None
    final_capital: Optional[float] = None
    total_return: Optional[float] = None
    cagr: Optional[float] = None
    annualized_return: Optional[float] = None
    volatility: Optional[float] = None
    downside_volatility: Optional[float] = None

    sharpe: Optional[float] = None
    sortino: Optional[float] = None
    calmar: Optional[float] = None

    max_drawdown: Optional[float] = None
    average_drawdown: Optional[float] = None
    max_drawdown_duration_days: Optional[float] = None

    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: Optional[float] = None
    average_win: Optional[float] = None
    average_loss: Optional[float] = None
    largest_win: Optional[float] = None
    largest_loss: Optional[float] = None
    profit_factor: Optional[float] = None
    expectancy: Optional[float] = None
    average_holding_days: Optional[float] = None

    turnover: Optional[float] = None
    annualized_turnover: Optional[float] = None
    average_exposure: Optional[float] = None
    average_cash: Optional[float] = None

    total_costs: float = 0.0
    total_slippage: float = 0.0

    benchmark_return: Optional[float] = None
    excess_return: Optional[float] = None

    #: Metrics that could not be computed, and why.
    unavailable: Dict[str, str] = field(default_factory=dict)

    def mark_unavailable(self, metric: str, reason: str) -> None:
        self.unavailable[metric] = reason


@dataclass
class ExecutionStatistics:
    """How the simulated execution behaved (spec §59)."""
    orders_created: int = 0
    orders_filled: int = 0
    orders_partially_filled: int = 0
    orders_rejected: int = 0
    reject_reasons: Dict[str, int] = field(default_factory=dict)
    average_slippage_bps: Optional[float] = None
    total_commission: float = 0.0
    total_slippage_cost: float = 0.0
    average_fill_delay_days: Optional[float] = None

    @property
    def fill_rate(self) -> Optional[float]:
        return safe_ratio(self.orders_filled, self.orders_created)

    @property
    def rejection_rate(self) -> Optional[float]:
        return safe_ratio(self.orders_rejected, self.orders_created)


@dataclass
class AttributionBucket:
    """One slice of performance attribution (spec §42, §108)."""
    dimension: str
    key: str
    label: str = ""
    trades: int = 0
    net_pnl: float = 0.0
    gross_pnl: float = 0.0
    costs: float = 0.0
    wins: int = 0

    @property
    def win_rate(self) -> Optional[float]:
        return safe_ratio(self.wins, self.trades)

    @property
    def average_pnl(self) -> Optional[float]:
        return safe_ratio(self.net_pnl, self.trades)


@dataclass
class BacktestWarning:
    """A research-quality caveat attached to a run."""
    code: WarningCode
    message: str
    detail: str = ""


@dataclass
class BacktestError:
    """A problem serious enough to affect the result's validity."""
    code: str
    message: str
    at: Optional[datetime] = None
    instrument_id: Optional[str] = None
    fatal: bool = False

    def __post_init__(self):
        _require_utc(self.at, "at")


@dataclass
class BacktestResult:
    """
    The full record of one run (spec §55).

    Deliberately not "a number": configuration, identity, metrics,
    execution behaviour, warnings and errors all travel together,
    because a return figure detached from its assumptions is the thing
    this entire phase exists to avoid producing.
    """
    run_id: str
    backtest_id: str
    status: BacktestStatus
    configuration: BacktestConfiguration
    identity: RunIdentity

    metrics: PerformanceMetrics = field(default_factory=PerformanceMetrics)
    execution_stats: ExecutionStatistics = field(default_factory=ExecutionStatistics)
    equity_curve: List[EquityPoint] = field(default_factory=list)
    drawdowns: List[DrawdownEpisode] = field(default_factory=list)
    trades: List[Trade] = field(default_factory=list)
    orders: List[SimulatedOrder] = field(default_factory=list)
    fills: List[SimulatedFill] = field(default_factory=list)
    attribution: List[AttributionBucket] = field(default_factory=list)

    #: Risk decisions the replay produced, by state.
    risk_decision_counts: Dict[str, int] = field(default_factory=dict)
    #: Allocations risk refused or trimmed — kept, not discarded
    #: (spec §27, §28).
    rejected_allocations: List[Dict[str, Any]] = field(default_factory=list)
    modified_allocations: List[Dict[str, Any]] = field(default_factory=list)

    warnings: List[BacktestWarning] = field(default_factory=list)
    errors: List[BacktestError] = field(default_factory=list)
    event_log: List[Dict[str, Any]] = field(default_factory=list)

    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    observations_processed: int = 0

    def __post_init__(self):
        for name in ("started_at", "finished_at"):
            setattr(self, name, _require_utc(getattr(self, name), name))

    def add_warning(self, code: WarningCode, message: str, detail: str = "") -> None:
        if not any(w.code == code for w in self.warnings):
            self.warnings.append(BacktestWarning(code, message, detail))

    def add_error(self, code: str, message: str, at: Optional[datetime] = None,
                  instrument_id: Optional[str] = None, fatal: bool = False) -> None:
        self.errors.append(BacktestError(code, message, at, instrument_id, fatal))

    @property
    def has_fatal_error(self) -> bool:
        return any(e.fatal for e in self.errors)

    @property
    def duration_seconds(self) -> Optional[float]:
        if self.started_at is None or self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()

    def log(self, at: datetime, kind: str, **payload: Any) -> None:
        """Append to the portfolio event log (spec §73)."""
        entry = {"at": at.isoformat(), "kind": kind}
        entry.update(payload)
        self.event_log.append(entry)


# ============================================================
# Research quality
# ============================================================

@dataclass
class QualityAssessment:
    """
    A research-quality classification (spec §100).

    THIS IS NOT A PROFITABILITY SCORE, and the type carries that
    statement because the confusion is easy and expensive. It grades
    how much the RESULT CAN BE TRUSTED — sample size, execution
    realism, point-in-time integrity, cost realism. A run with a
    perfect score can still lose money, and a run with a terrible
    score can look spectacular. That is the whole point.
    """
    score: Optional[float] = None            # 0..1
    factors: Dict[str, float] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    #: Fixed disclaimer carried with the score wherever it is shown.
    MEANING: str = ("measures research quality and trustworthiness, "
                    "NOT profitability or expected future return")

    @property
    def band(self) -> str:
        if self.score is None:
            return "unrated"
        if self.score >= 0.75:
            return "strong"
        if self.score >= 0.5:
            return "moderate"
        if self.score >= 0.25:
            return "weak"
        return "very weak"
