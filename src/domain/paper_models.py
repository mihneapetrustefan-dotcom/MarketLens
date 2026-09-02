"""
src/domain/paper_models.py
-------------------------------
Paper trading domain models (Phase 13).

WHAT PAPER TRADING IS IN THIS SYSTEM, STATED HONESTLY
---------------------------------------------------------
This repository has no persistent runtime. Every phase runs as a batch
job under GitHub Actions cron — there is no server, no event loop, no
websocket, and no streaming market feed. `yfinance` snapshot calls and
cached Polygon bars are the only market inputs that exist.

So paper trading here is NOT a streaming daemon, and pretending
otherwise would be the central lie of this phase. It is a DURABLE,
RESUMABLE SESSION that advances when it is invoked: each tick reads the
market data available at that moment, runs the real pipeline, executes
through the paper executor, and persists its state and a checkpoint.

That is genuine paper trading under a batch scheduler. What makes it
honest rather than a pretence is `DataFreshness`: every tick records how
old its inputs actually were, so a session running on four-day-old bars
reports exactly that instead of displaying them as live.

WHAT IS REUSED RATHER THAN REDEFINED
----------------------------------------
Signal, Position, PortfolioSnapshot, RiskDecision and OrderIntent come
from Phases 10 and 11 unchanged. Cost and slippage models, the market
calendar, and the position/cash ledger come from Phase 12 unchanged —
spec §21, §22 and §25 all require reuse, and a second set of accounting
formulas would let paper and backtest results diverge for reasons
nobody could trace.

PaperOrder and PaperFill ARE new types, deliberately: an order needs an
idempotency key, an order type, limit and stop prices, and a fuller
state machine than the backtester's `SimulatedOrder` carries. A fill
needs a venue and an execution-model reference. Both convert into the
Phase 12 shapes the ledger already consumes, so the accounting stays
single-sourced.

NOTHING HERE REACHES A BROKER
---------------------------------
No account credential, no venue routing, no broker order id, no
connection. `ExecutionVenue.PAPER` is the only venue that exists, and
the executor that produces these records prices them against cached
bars.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from math import isfinite
from typing import Any, Dict, List, Optional, Tuple


# ============================================================
# Guards (same contract as Phases 9-12)
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

class PaperAccountStatus(str, Enum):
    """
    Account lifecycle.

    EMERGENCY_STOP is separate from PAUSED because they mean different
    things operationally: paused is a deliberate, routine hold, while
    emergency stop records that something went wrong. Collapsing them
    would lose the distinction exactly when someone needs it.
    """
    ACTIVE = "active"
    PAUSED = "paused"
    REDUCE_ONLY = "reduce_only"
    EMERGENCY_STOP = "emergency_stop"
    CLOSED = "closed"


class PaperSessionStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PaperOrderState(str, Enum):
    """
    Order lifecycle (spec §17).

    Richer than the backtester's `OrderState` because a paper order can
    be validated, accepted and then cancelled before ever reaching a
    fillable bar — states a backtest fill loop has no use for but a
    live-parity path does.
    """
    CREATED = "created"
    VALIDATING = "validating"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    EXPIRED = "expired"

    @property
    def is_terminal(self) -> bool:
        return self in (PaperOrderState.FILLED, PaperOrderState.REJECTED,
                        PaperOrderState.CANCELLED, PaperOrderState.EXPIRED)

    @property
    def is_working(self) -> bool:
        """Still eligible to receive fills on a later tick."""
        return self in (PaperOrderState.ACCEPTED, PaperOrderState.SUBMITTED,
                        PaperOrderState.PARTIALLY_FILLED)


class PaperOrderType(str, Enum):
    """
    Order types this system can honestly simulate (spec §16).

    MARKET and LIMIT are modelled properly against OHLC bars. STOP is
    modelled with a documented caveat: a bar records only its open,
    high, low and close, so when a bar's range spans both a stop
    trigger and a limit price there is no way to know which came first
    intrabar. STOP_LIMIT therefore carries an explicit
    `intrabar_ambiguous` flag on its fills rather than pretending to a
    sequencing the data cannot support.
    """
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class TimeInForce(str, Enum):
    DAY = "day"
    GTC = "gtc"                     # good till cancelled
    IOC = "immediate_or_cancel"


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class ExecutionVenue(str, Enum):
    """
    The only venue that exists in this phase.

    A single-member enum is deliberate: it makes every fill record
    unambiguously paper, and it is the field a future broker phase would
    extend rather than a boolean somebody could forget to set.
    """
    PAPER = "paper"


class PaperRejectReason(str, Enum):
    """Why a paper order was refused (spec §20). Enumerated so rejections are countable."""
    MARKET_CLOSED = "market_closed"
    STALE_DATA = "stale_data"
    NO_PRICE = "no_price"
    INSUFFICIENT_CASH = "insufficient_cash"
    INVALID_QUANTITY = "invalid_quantity"
    INVALID_PRICE = "invalid_price"
    UNKNOWN_INSTRUMENT = "unknown_instrument"
    RISK_REJECTED = "risk_rejected"
    SHORTING_DISABLED = "shorting_disabled"
    RATE_LIMITED = "rate_limited"
    CIRCUIT_BREAKER = "circuit_breaker"
    ACCOUNT_NOT_ACTIVE = "account_not_active"
    LIQUIDITY_CAP = "liquidity_cap"
    DUPLICATE = "duplicate"
    SAFE_MODE = "safe_mode"


class DataFreshness(str, Enum):
    """
    How old an input actually is (spec §8, §9).

    The distinction this phase most depends on. A paper system running
    on cached daily bars is not receiving live data, and saying so is
    the difference between an honest simulation and a misleading one.
    """
    FRESH = "fresh"
    AGING = "aging"
    STALE = "stale"
    INVALID = "invalid"
    UNAVAILABLE = "unavailable"

    @property
    def is_tradeable(self) -> bool:
        """Only FRESH and AGING data may back a new order."""
        return self in (DataFreshness.FRESH, DataFreshness.AGING)


class HealthState(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    STALE = "stale"
    FAILED = "failed"
    PAUSED = "paused"

    @property
    def allows_new_orders(self) -> bool:
        """Spec §61: on failure the default is to stop creating orders."""
        return self in (HealthState.HEALTHY, HealthState.DEGRADED)


class AlertSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class PaperEventKind(str, Enum):
    """The chronological log's vocabulary (spec §33)."""
    SESSION_STARTED = "session_started"
    SESSION_PAUSED = "session_paused"
    SESSION_RESUMED = "session_resumed"
    SESSION_STOPPED = "session_stopped"
    TICK = "tick"
    MARKET_DATA = "market_data"
    DATA_STALE = "data_stale"
    SIGNAL_OBSERVED = "signal_observed"
    SIGNAL_EXPIRED = "signal_expired"
    RISK_EVALUATED = "risk_evaluated"
    RISK_REJECTED = "risk_rejected"
    ORDER_INTENT = "order_intent"
    ORDER_CREATED = "order_created"
    ORDER_ACCEPTED = "order_accepted"
    ORDER_REJECTED = "order_rejected"
    ORDER_CANCELLED = "order_cancelled"
    ORDER_EXPIRED = "order_expired"
    FILL = "fill"
    POSITION_UPDATED = "position_updated"
    SNAPSHOT = "snapshot"
    RECONCILIATION = "reconciliation"
    HEALTH_CHANGED = "health_changed"
    ALERT = "alert"
    CONTROL = "control"
    CHECKPOINT = "checkpoint"
    RECOVERY = "recovery"


# ============================================================
# Time and freshness
# ============================================================

@dataclass(frozen=True)
class FreshnessPolicy:
    """
    Per-asset-class staleness thresholds (spec §9).

    Explicitly NOT one universal threshold. A crypto quote four hours
    old is stale; a daily equity bar four hours after the close is
    perfectly normal. Applying one number to both would either spam
    false staleness or hide real staleness, depending which way it was
    set.
    """
    asset_class: str = "default"
    fresh_seconds: float = 900.0            # 15 minutes
    aging_seconds: float = 86_400.0         # 1 day
    stale_seconds: float = 604_800.0        # 7 days

    def classify(self, age_seconds: Optional[float]) -> DataFreshness:
        if age_seconds is None:
            return DataFreshness.UNAVAILABLE
        if age_seconds < 0:
            # Data stamped in the future is a clock or feed fault, not
            # freshness. Treated as invalid rather than "very fresh".
            return DataFreshness.INVALID
        if age_seconds <= self.fresh_seconds:
            return DataFreshness.FRESH
        if age_seconds <= self.aging_seconds:
            return DataFreshness.AGING
        if age_seconds <= self.stale_seconds:
            return DataFreshness.STALE
        return DataFreshness.INVALID


#: Thresholds tuned to what this project's data actually looks like.
#: Equities produce one daily bar; crypto trades continuously; the
#: benchmark follows equities.
DEFAULT_FRESHNESS_POLICIES: Dict[str, FreshnessPolicy] = {
    "stock": FreshnessPolicy("stock", fresh_seconds=6 * 3600,
                             aging_seconds=4 * 86_400, stale_seconds=14 * 86_400),
    "crypto": FreshnessPolicy("crypto", fresh_seconds=1800,
                              aging_seconds=6 * 3600, stale_seconds=2 * 86_400),
    "bvb": FreshnessPolicy("bvb", fresh_seconds=12 * 3600,
                           aging_seconds=5 * 86_400, stale_seconds=21 * 86_400),
    "default": FreshnessPolicy(),
}


@dataclass
class MarketDataStatus:
    """One instrument's data state at one moment."""
    instrument_id: str
    asset_class: Optional[str] = None
    price: Optional[float] = None
    observed_at: Optional[datetime] = None      # when the market produced it
    received_at: Optional[datetime] = None      # when this system saw it
    evaluated_at: Optional[datetime] = None     # when freshness was judged
    freshness: DataFreshness = DataFreshness.UNAVAILABLE
    source: str = ""
    #: True when the price came from a stored bar rather than a live
    #: quote. Every price in this system currently does.
    is_cached: bool = True

    def __post_init__(self):
        for name in ("observed_at", "received_at", "evaluated_at"):
            setattr(self, name, _require_utc(getattr(self, name), name))

    @property
    def age_seconds(self) -> Optional[float]:
        if self.observed_at is None or self.evaluated_at is None:
            return None
        return (self.evaluated_at - self.observed_at).total_seconds()

    @property
    def is_tradeable(self) -> bool:
        return self.freshness.is_tradeable and self.price is not None


# ============================================================
# Account
# ============================================================

@dataclass
class PaperAccount:
    """
    A simulated account (spec §6).

    Carries no credential, no broker reference and no external id,
    because there is nothing external to reference. `is_paper` is a
    permanent True rather than a configurable flag: an account type
    that could be switched to live by setting a boolean is exactly the
    footgun spec §56 and §85 exist to prevent.
    """
    account_id: str
    name: str
    base_currency: str = "USD"
    initial_capital: float = 100_000.0
    status: PaperAccountStatus = PaperAccountStatus.ACTIVE
    #: 'long_only' | 'long_short'. Only what the risk layer supports.
    account_type: str = "long_only"
    created_at: Optional[datetime] = None
    #: Increments on reset; old generations keep their history (spec §63).
    generation: int = 1
    metadata: Dict[str, Any] = field(default_factory=dict)

    #: Structural, not configurable.
    is_paper: bool = True

    def __post_init__(self):
        _require_utc(self.created_at, "created_at")
        if self.initial_capital <= 0:
            raise ValueError("initial_capital must be positive")
        if not self.is_paper:
            raise ValueError(
                "PaperAccount.is_paper cannot be False — this phase has no "
                "live execution path of any kind")

    @property
    def allows_new_exposure(self) -> bool:
        """Only an ACTIVE account may increase exposure."""
        return self.status == PaperAccountStatus.ACTIVE

    @property
    def allows_reductions(self) -> bool:
        return self.status in (PaperAccountStatus.ACTIVE,
                               PaperAccountStatus.REDUCE_ONLY)

    @property
    def allows_shorting(self) -> bool:
        return self.account_type == "long_short"


# ============================================================
# Session
# ============================================================

@dataclass
class PaperSessionConfig:
    """
    Everything that determines what a session does (spec §50).

    Versioned and stored whole. A session whose configuration lived only
    in the invoking command could not be reproduced or explained after
    the fact, which spec §51 requires it to be.
    """
    universe: List[str] = field(default_factory=list)
    strategy_id: Optional[str] = None
    strategy_version: Optional[str] = None
    constraint_set_version: str = "v1"
    sizing_strategy_id: str = "fixed_fraction"
    sizing_target_weight: float = 0.05

    #: Reused verbatim from Phase 12 — spec §21, §22.
    cost_model_version: str = "cost-v1"
    slippage_model_version: str = "slip-v1"
    execution_model_version: str = "paper-exec-v1"
    commission_bps: float = 2.0
    slippage_bps: float = 5.0

    #: Seconds between a decision and the order being cut.
    signal_to_order_seconds: float = 60.0
    max_participation: Optional[float] = 0.10
    default_order_type: PaperOrderType = PaperOrderType.MARKET
    default_time_in_force: TimeInForce = TimeInForce.DAY

    #: Safety (spec §39, §40).
    max_orders_per_tick: int = 25
    max_orders_per_day: int = 200
    daily_loss_limit_pct: Optional[float] = 0.05
    max_drawdown_pct: Optional[float] = 0.20

    #: Refuse to trade on data older than the policy allows.
    require_fresh_data: bool = True
    #: Expected seconds between ticks. Drives heartbeat staleness — a
    #: session ticking once per trading day must not declare itself
    #: stale between ticks, and a fixed timeout cannot serve both a
    #: daily and a minute cadence.
    tick_interval_seconds: float = 86_400.0
    config_version: str = "paper-cfg-v1"

    def fingerprint(self) -> str:
        """Stable hash of every field that changes behaviour."""
        import hashlib
        parts = [
            ",".join(sorted(self.universe)), str(self.strategy_id),
            str(self.strategy_version), self.constraint_set_version,
            self.sizing_strategy_id, f"{self.sizing_target_weight:.6f}",
            self.cost_model_version, self.slippage_model_version,
            self.execution_model_version, f"{self.commission_bps:.6f}",
            f"{self.slippage_bps:.6f}", f"{self.signal_to_order_seconds:.3f}",
            str(self.max_participation), self.default_order_type.value,
            self.default_time_in_force.value, str(self.max_orders_per_tick),
            str(self.max_orders_per_day), str(self.daily_loss_limit_pct),
            str(self.max_drawdown_pct), str(self.require_fresh_data),
            f"{self.tick_interval_seconds:.3f}", self.config_version,
        ]
        return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:20]


@dataclass
class PaperSession:
    """One run of the paper pipeline against an account (spec §49)."""
    session_id: str
    account_id: str
    name: str
    config: PaperSessionConfig
    status: PaperSessionStatus = PaperSessionStatus.CREATED
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    #: The clock position of the most recent completed tick. Recovery
    #: resumes from here rather than from the session start.
    last_tick_at: Optional[datetime] = None
    ticks_processed: int = 0
    #: Version identity, mirroring Phase 12's RunIdentity.
    risk_engine_version: str = "v1"
    code_version: str = "phase13-v1"
    notes: str = ""

    def __post_init__(self):
        for name in ("started_at", "ended_at", "last_tick_at"):
            setattr(self, name, _require_utc(getattr(self, name), name))

    @property
    def is_running(self) -> bool:
        return self.status == PaperSessionStatus.RUNNING

    @property
    def accepts_ticks(self) -> bool:
        return self.status in (PaperSessionStatus.CREATED,
                               PaperSessionStatus.RUNNING)


# ============================================================
# Orders and fills
# ============================================================

@dataclass
class PaperOrder:
    """
    A simulated instruction (spec §16).

    `idempotency_key` is what makes duplicate delivery safe (spec §12):
    it is derived from the deciding inputs, so the same decision
    reprocessed produces the same key and the second attempt is
    recognised rather than creating a second position.
    """
    order_id: str
    session_id: str
    account_id: str
    instrument_id: str
    side: OrderSide
    quantity: float
    order_type: PaperOrderType = PaperOrderType.MARKET
    time_in_force: TimeInForce = TimeInForce.DAY
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None

    state: PaperOrderState = PaperOrderState.CREATED
    filled_quantity: float = 0.0
    average_fill_price: Optional[float] = None
    reject_reason: Optional[PaperRejectReason] = None
    reject_detail: str = ""

    #: Idempotency and provenance (spec §12, §29).
    idempotency_key: str = ""
    signal_id: Optional[str] = None
    decision_id: Optional[str] = None
    intent_id: Optional[str] = None
    strategy_id: Optional[str] = None
    model_version: Optional[str] = None
    target_weight: Optional[float] = None

    information_cutoff: Optional[datetime] = None
    decided_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    accepted_at: Optional[datetime] = None
    terminal_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None

    execution_model_version: str = "paper-exec-v1"
    note: str = ""

    def __post_init__(self):
        for name in ("information_cutoff", "decided_at", "created_at",
                     "accepted_at", "terminal_at", "expires_at"):
            setattr(self, name, _require_utc(getattr(self, name), name))
        if self.quantity <= 0:
            raise ValueError("order quantity must be positive; direction is `side`")
        if self.order_type in (PaperOrderType.LIMIT, PaperOrderType.STOP_LIMIT) \
                and self.limit_price is None:
            raise ValueError(f"{self.order_type.value} order requires a limit_price")
        if self.order_type in (PaperOrderType.STOP, PaperOrderType.STOP_LIMIT) \
                and self.stop_price is None:
            raise ValueError(f"{self.order_type.value} order requires a stop_price")

    @property
    def remaining(self) -> float:
        return max(0.0, self.quantity - self.filled_quantity)

    @property
    def signed_filled(self) -> float:
        return self.filled_quantity * (1.0 if self.side == OrderSide.BUY else -1.0)

    @property
    def is_complete(self) -> bool:
        return self.remaining <= 1e-9

    def is_expired_at(self, moment: datetime) -> bool:
        _require_utc(moment, "moment")
        return self.expires_at is not None and moment > self.expires_at


@dataclass
class PaperFill:
    """
    One simulated execution (spec §18).

    Records the reference market price alongside the charged price, so
    the slippage actually applied is auditable rather than inferred —
    the same discipline Phase 12's fills follow.
    """
    fill_id: str
    session_id: str
    order_id: str
    account_id: str
    instrument_id: str
    side: OrderSide
    quantity: float
    price: float
    reference_price: float
    filled_at: datetime

    commission: float = 0.0
    slippage_cost: float = 0.0
    venue: ExecutionVenue = ExecutionVenue.PAPER
    execution_model_version: str = "paper-exec-v1"
    slippage_model_version: str = "slip-v1"
    cost_model_version: str = "cost-v1"

    bar_timestamp: Optional[datetime] = None
    participation: Optional[float] = None
    is_partial: bool = False
    #: True when the bar's range spans both a stop trigger and a limit,
    #: so intrabar ordering is unknowable from OHLC data.
    intrabar_ambiguous: bool = False
    #: Idempotency: a repeated fill message with this key is ignored.
    idempotency_key: str = ""

    def __post_init__(self):
        self.filled_at = _require_utc(self.filled_at, "filled_at")
        _require_utc(self.bar_timestamp, "bar_timestamp")
        if self.venue != ExecutionVenue.PAPER:
            raise ValueError("Phase 13 can only produce PAPER fills")

    @property
    def notional(self) -> float:
        return abs(self.quantity) * self.price

    @property
    def total_cost(self) -> float:
        return self.commission + self.slippage_cost

    @property
    def signed_quantity(self) -> float:
        return self.quantity * (1.0 if self.side == OrderSide.BUY else -1.0)


# ============================================================
# Snapshots and P&L
# ============================================================

@dataclass
class PaperSnapshot:
    """The account's state at one instant (spec §26)."""
    snapshot_id: str
    session_id: str
    account_id: str
    at: datetime
    equity: float
    cash: float
    positions_value: float
    gross_exposure: float = 0.0
    net_exposure: float = 0.0
    long_exposure: float = 0.0
    short_exposure: float = 0.0
    leverage: Optional[float] = None
    realized_pnl: float = 0.0
    unrealized_pnl: Optional[float] = None
    drawdown: Optional[float] = None
    open_positions: int = 0
    unpriced_positions: int = 0
    #: Worst freshness across the instruments valued here.
    data_freshness: DataFreshness = DataFreshness.UNAVAILABLE
    health: HealthState = HealthState.HEALTHY

    def __post_init__(self):
        self.at = _require_utc(self.at, "at")

    @property
    def total_pnl(self) -> Optional[float]:
        if self.unrealized_pnl is None:
            return None
        return finite_or_none(self.realized_pnl + self.unrealized_pnl)

    @property
    def is_complete(self) -> bool:
        return self.unpriced_positions == 0


# ============================================================
# Health, alerts, reconciliation
# ============================================================

@dataclass
class ComponentHealth:
    """One pipeline component's state and heartbeat (spec §34, §35)."""
    component: str
    state: HealthState = HealthState.HEALTHY
    last_heartbeat_at: Optional[datetime] = None
    detail: str = ""
    latency_ms: Optional[float] = None

    def __post_init__(self):
        _require_utc(self.last_heartbeat_at, "last_heartbeat_at")

    def age_seconds(self, now: datetime) -> Optional[float]:
        if self.last_heartbeat_at is None:
            return None
        return (now - self.last_heartbeat_at).total_seconds()


@dataclass
class SystemHealth:
    """
    Aggregate pipeline health (spec §34).

    `overall` is the WORST component state, not an average. A pipeline
    whose model has failed is not "mostly healthy" — averaging would
    dilute exactly the signal that matters.
    """
    at: Optional[datetime] = None
    components: Dict[str, ComponentHealth] = field(default_factory=dict)
    safe_mode: bool = False
    safe_mode_reason: str = ""

    def __post_init__(self):
        _require_utc(self.at, "at")

    _ORDER = [HealthState.HEALTHY, HealthState.PAUSED, HealthState.DEGRADED,
              HealthState.STALE, HealthState.FAILED]

    @property
    def overall(self) -> HealthState:
        if not self.components:
            return HealthState.FAILED
        worst = HealthState.HEALTHY
        for component in self.components.values():
            if self._ORDER.index(component.state) > self._ORDER.index(worst):
                worst = component.state
        return worst

    @property
    def allows_new_orders(self) -> bool:
        """Spec §61 — fail safe, not open."""
        return (not self.safe_mode) and self.overall.allows_new_orders

    def record(self, component: str, state: HealthState, at: datetime,
               detail: str = "", latency_ms: Optional[float] = None) -> None:
        self.components[component] = ComponentHealth(
            component=component, state=state, last_heartbeat_at=at,
            detail=detail, latency_ms=latency_ms)


@dataclass
class PaperAlert:
    """A raised condition (spec §37)."""
    alert_id: str
    session_id: str
    code: str
    severity: AlertSeverity
    message: str
    at: Optional[datetime] = None
    detail: str = ""
    acknowledged: bool = False

    def __post_init__(self):
        _require_utc(self.at, "at")


@dataclass
class ReconciliationDiscrepancy:
    """One inconsistency found between the ledger's views (spec §32)."""
    kind: str
    instrument_id: Optional[str] = None
    expected: Optional[float] = None
    actual: Optional[float] = None
    detail: str = ""

    @property
    def difference(self) -> Optional[float]:
        if self.expected is None or self.actual is None:
            return None
        return finite_or_none(self.actual - self.expected)


@dataclass
class ReconciliationResult:
    """
    The outcome of checking orders against fills against positions
    against cash (spec §32, §62).

    A clean result is recorded as explicitly as a dirty one. "We
    checked and it balanced" and "we never checked" must not look the
    same in the record.
    """
    at: datetime
    session_id: str
    checks_performed: int = 0
    discrepancies: List[ReconciliationDiscrepancy] = field(default_factory=list)
    orders_examined: int = 0
    fills_examined: int = 0
    positions_examined: int = 0

    def __post_init__(self):
        self.at = _require_utc(self.at, "at")

    @property
    def is_clean(self) -> bool:
        return not self.discrepancies

    def add(self, kind: str, detail: str = "", instrument_id: Optional[str] = None,
            expected: Optional[float] = None, actual: Optional[float] = None) -> None:
        self.discrepancies.append(ReconciliationDiscrepancy(
            kind=kind, instrument_id=instrument_id, expected=expected,
            actual=actual, detail=detail))


# ============================================================
# Control and audit
# ============================================================

@dataclass
class ControlAction:
    """
    An operator intervention (spec §38, §72, §74).

    Records who did what and why. The `previous` / `new` pair exists so
    a configuration change is reversible and explicable, not just
    observable after the fact.
    """
    action_id: str
    session_id: str
    action: str                     # pause | resume | stop | reduce_only | configure
    at: datetime
    actor: str = "system"
    reason: str = ""
    previous_value: Optional[str] = None
    new_value: Optional[str] = None

    def __post_init__(self):
        self.at = _require_utc(self.at, "at")


@dataclass
class PaperEvent:
    """One entry in the chronological log (spec §33)."""
    session_id: str
    at: datetime
    kind: PaperEventKind
    sequence: int = 0
    instrument_id: Optional[str] = None
    order_id: Optional[str] = None
    fill_id: Optional[str] = None
    signal_id: Optional[str] = None
    message: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        self.at = _require_utc(self.at, "at")


# ============================================================
# Latency
# ============================================================

@dataclass
class LatencySample:
    """
    Elapsed time across one pipeline stage (spec §36, §77).

    Measured in milliseconds of WALL CLOCK inside this process. It is
    not a claim about market-data latency — this system has no
    streaming feed to measure that against, and spec §36 warns against
    claiming precision the architecture cannot support.
    """
    stage: str
    milliseconds: float
    at: Optional[datetime] = None

    def __post_init__(self):
        _require_utc(self.at, "at")


@dataclass
class TickResult:
    """
    What one advance of the session produced.

    Returned rather than logged-and-forgotten so a caller — a test, a
    CLI, a future scheduler — can assert on exactly what happened
    during that tick.
    """
    session_id: str
    at: datetime
    signals_observed: int = 0
    orders_created: int = 0
    orders_filled: int = 0
    orders_rejected: int = 0
    fills: int = 0
    risk_state: Optional[str] = None
    health: HealthState = HealthState.HEALTHY
    freshness: DataFreshness = DataFreshness.UNAVAILABLE
    blocked_reason: str = ""
    latencies: List[LatencySample] = field(default_factory=list)
    snapshot: Optional[PaperSnapshot] = None
    reconciliation: Optional[ReconciliationResult] = None

    def __post_init__(self):
        self.at = _require_utc(self.at, "at")

    @property
    def traded(self) -> bool:
        return self.fills > 0

    @property
    def was_blocked(self) -> bool:
        return bool(self.blocked_reason)
