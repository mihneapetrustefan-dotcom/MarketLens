"""
src/execution/monitoring.py
--------------------------------
Execution health, quality analytics, alerting and comparison
(Phase 16, spec §18, §19, §23, §35, §46, §47, §57, §58, §59, §60,
§61, §62, §63, §75).

THREE THINGS, KEPT APART
----------------------------
HEALTH is whether the machinery is working: connected, authenticated,
data arriving, orders acknowledged. It gates execution.

QUALITY is how well orders executed: slippage, latency, fill rates. It
does not gate by itself — a bad fill is not a broken system — but
sustained degradation does, through the Phase 16 limit thresholds.

ALERTS are what a human is told. Separate from both, because the
decision "is this worth waking someone" is not the same as "is this
bad".

PARTIAL OUTAGE IS THE NORMAL FAILURE
----------------------------------------
Spec §35. Venues do not fail all at once — market data goes stale
while orders still work, or account data ages while quotes flow. So
capabilities are tracked individually and the aggregate is the WORST
one, never an average. Averaging is how a dead account feed hides
behind three healthy ones.

COMPARISON REFUSES THE CONCLUSION IT CANNOT SUPPORT
-------------------------------------------------------
Phase 13 established this and it holds here: mechanical divergences
between backtest, paper and live (fill rate, slippage, rejections) are
diagnostic. Return differences are reported and explicitly not treated
as evidence, because a short live period cannot distinguish skill from
noise any better than a short paper period could.
"""

from __future__ import annotations

import statistics
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from src.domain.broker_models import (
    ExecutionFill, ExecutionOrder, ExecutionOrderState, finite_or_none,
)


def _require_utc(value: Optional[datetime], name: str) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    if value.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError(f"{name} must be in UTC")
    return value


def _ratio(numerator: float, denominator: float) -> Optional[float]:
    if not denominator:
        return None
    return finite_or_none(numerator / denominator)


class Capability(str, Enum):
    """The things that can fail separately (spec §35)."""
    CONNECTION = "connection"
    AUTHENTICATION = "authentication"
    MARKET_DATA = "market_data"
    ACCOUNT = "account"
    ORDERS = "orders"
    EXECUTIONS = "executions"
    POSITIONS = "positions"
    RECONCILIATION = "reconciliation"
    CLOCK = "clock"


class CapabilityState(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    STALE = "stale"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"

    @property
    def permits_new_orders(self) -> bool:
        """
        Only HEALTHY permits. DEGRADED does not.

        A degraded order path can still carry a submission whose
        acknowledgement never arrives, which is the route to an
        UNKNOWN order. New exposure waits; reading continues.
        """
        return self is CapabilityState.HEALTHY

    @property
    def rank(self) -> int:
        return {
            CapabilityState.HEALTHY: 0,
            CapabilityState.DEGRADED: 1,
            CapabilityState.STALE: 2,
            CapabilityState.UNAVAILABLE: 3,
            CapabilityState.UNKNOWN: 4,
        }[self]


#: Which capabilities must be healthy before an order may be sent.
#: Positions and reconciliation are read-side and gate through the
#: limit governor instead, so a stale position feed does not silently
#: become an order-blocking condition twice.
ORDER_CRITICAL: Tuple[Capability, ...] = (
    Capability.CONNECTION, Capability.AUTHENTICATION,
    Capability.MARKET_DATA, Capability.ACCOUNT, Capability.ORDERS,
    Capability.CLOCK,
)


class AlertSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return {"info": 0, "warning": 1, "error": 2, "critical": 3}[self.value]

    @property
    def demands_attention(self) -> bool:
        return self in (AlertSeverity.ERROR, AlertSeverity.CRITICAL)


@dataclass
class Alert:
    """One thing a human should know (spec §46, §47)."""
    alert_id: str
    code: str
    severity: AlertSeverity
    message: str
    at: Optional[datetime] = None
    detail: str = ""
    session_id: Optional[str] = None
    order_id: Optional[str] = None
    acknowledged: bool = False
    acknowledged_by: Optional[str] = None

    def __post_init__(self):
        self.at = _require_utc(self.at, "at")

    def acknowledge(self, actor: str) -> None:
        if not actor:
            raise ValueError("acknowledging an alert requires an actor")
        self.acknowledged = True
        self.acknowledged_by = actor

    def as_dict(self) -> Dict[str, Any]:
        return {
            "alert_id": self.alert_id, "code": self.code,
            "severity": self.severity.value, "message": self.message,
            "detail": self.detail,
            "at": self.at.isoformat() if self.at else None,
            "session_id": self.session_id, "order_id": self.order_id,
            "acknowledged": self.acknowledged,
            "acknowledged_by": self.acknowledged_by,
        }


@dataclass
class CapabilityReading:
    name: Capability
    state: CapabilityState = CapabilityState.UNKNOWN
    at: Optional[datetime] = None
    detail: str = ""
    latency_ms: Optional[float] = None
    age_seconds: Optional[float] = None

    def __post_init__(self):
        self.at = _require_utc(self.at, "at")


@dataclass
class SystemHealth:
    """
    Every capability, and the aggregate.

    The aggregate is the WORST reading, never an average. A system
    whose account feed is dead is not "mostly healthy", and averaging
    is exactly how that gets hidden.
    """
    at: Optional[datetime] = None
    readings: Dict[Capability, CapabilityReading] = field(default_factory=dict)

    def __post_init__(self):
        self.at = _require_utc(self.at, "at")
        for capability in Capability:
            self.readings.setdefault(
                capability, CapabilityReading(capability, at=self.at))

    def record(self, capability: Capability, state: CapabilityState,
               detail: str = "", latency_ms: Optional[float] = None,
               age_seconds: Optional[float] = None) -> None:
        self.readings[capability] = CapabilityReading(
            capability, state, self.at, detail, latency_ms, age_seconds)

    @property
    def overall(self) -> CapabilityState:
        return max((r.state for r in self.readings.values()),
                   key=lambda s: s.rank, default=CapabilityState.UNKNOWN)

    @property
    def permits_new_orders(self) -> bool:
        """
        Only the order-critical capabilities gate submission.

        Everything is still reported; a stale position feed is worth
        knowing about and is not, by itself, a reason to refuse a
        trade the account and market data both support.
        """
        return all(self.readings[c].state.permits_new_orders
                   for c in ORDER_CRITICAL)

    @property
    def failing(self) -> List[Capability]:
        return [c for c, r in self.readings.items()
                if not r.state.permits_new_orders]

    def render(self) -> str:
        width = max(len(c.value) for c in Capability)
        lines = []
        for capability in Capability:
            reading = self.readings[capability]
            marker = "" if reading.state.permits_new_orders else "   <-- blocks" \
                if capability in ORDER_CRITICAL else "   (reported only)"
            lines.append(f"{capability.value:<{width}}  "
                         f"{reading.state.value.upper():<12}"
                         f"{reading.detail}{marker}")
        return "\n".join(lines)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "at": self.at.isoformat() if self.at else None,
            "overall": self.overall.value,
            "permits_new_orders": self.permits_new_orders,
            "failing": [c.value for c in self.failing],
            "capabilities": {
                c.value: {"state": r.state.value, "detail": r.detail,
                          "latency_ms": r.latency_ms,
                          "age_seconds": r.age_seconds}
                for c, r in self.readings.items()},
        }


@dataclass
class ExecutionMetrics:
    """
    How execution is performing (spec §18, §19).

    Every rate is Optional and None when the denominator is zero — a
    rejection rate of "0.0" from zero orders is not a fact about the
    system, and reporting it as one would make an untested venue look
    flawless.
    """
    at: Optional[datetime] = None
    orders_submitted: int = 0
    orders_acknowledged: int = 0
    orders_filled: int = 0
    orders_partially_filled: int = 0
    orders_rejected: int = 0
    orders_cancelled: int = 0
    orders_unknown: int = 0
    cancel_requests: int = 0
    cancels_confirmed: int = 0
    reconciliations: int = 0
    reconciliation_mismatches: int = 0
    disconnects: int = 0
    reconnects: int = 0

    submit_latency_ms: List[float] = field(default_factory=list)
    ack_latency_ms: List[float] = field(default_factory=list)
    fill_latency_ms: List[float] = field(default_factory=list)
    slippage_bps: List[float] = field(default_factory=list)
    fees: float = 0.0

    def __post_init__(self):
        self.at = _require_utc(self.at, "at")

    # ---------------- rates ----------------

    @property
    def rejection_rate(self) -> Optional[float]:
        return _ratio(self.orders_rejected, self.orders_submitted)

    @property
    def fill_rate(self) -> Optional[float]:
        return _ratio(self.orders_filled, self.orders_submitted)

    @property
    def partial_fill_rate(self) -> Optional[float]:
        return _ratio(self.orders_partially_filled, self.orders_submitted)

    @property
    def unknown_state_rate(self) -> Optional[float]:
        return _ratio(self.orders_unknown, self.orders_submitted)

    @property
    def cancel_success_rate(self) -> Optional[float]:
        return _ratio(self.cancels_confirmed, self.cancel_requests)

    @property
    def reconciliation_mismatch_rate(self) -> Optional[float]:
        return _ratio(self.reconciliation_mismatches, self.reconciliations)

    @property
    def error_rate(self) -> Optional[float]:
        """Rejections plus unknowns — everything that did not go cleanly."""
        return _ratio(self.orders_rejected + self.orders_unknown,
                      self.orders_submitted)

    # ---------------- distributions ----------------

    @staticmethod
    def _median(values: Sequence[float]) -> Optional[float]:
        return finite_or_none(statistics.median(values)) if values else None

    @staticmethod
    def _percentile(values: Sequence[float], fraction: float) -> Optional[float]:
        if not values:
            return None
        ordered = sorted(values)
        index = min(len(ordered) - 1, int(fraction * len(ordered)))
        return finite_or_none(ordered[index])

    @property
    def median_slippage_bps(self) -> Optional[float]:
        return self._median(self.slippage_bps)

    @property
    def worst_slippage_bps(self) -> Optional[float]:
        return max(self.slippage_bps) if self.slippage_bps else None

    @property
    def median_submit_latency_ms(self) -> Optional[float]:
        return self._median(self.submit_latency_ms)

    @property
    def p95_submit_latency_ms(self) -> Optional[float]:
        return self._percentile(self.submit_latency_ms, 0.95)

    @property
    def median_fill_latency_ms(self) -> Optional[float]:
        return self._median(self.fill_latency_ms)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "at": self.at.isoformat() if self.at else None,
            "orders_submitted": self.orders_submitted,
            "orders_filled": self.orders_filled,
            "orders_rejected": self.orders_rejected,
            "orders_unknown": self.orders_unknown,
            "fill_rate": self.fill_rate,
            "rejection_rate": self.rejection_rate,
            "partial_fill_rate": self.partial_fill_rate,
            "unknown_state_rate": self.unknown_state_rate,
            "cancel_success_rate": self.cancel_success_rate,
            "reconciliation_mismatch_rate": self.reconciliation_mismatch_rate,
            "error_rate": self.error_rate,
            "median_slippage_bps": self.median_slippage_bps,
            "worst_slippage_bps": self.worst_slippage_bps,
            "median_submit_latency_ms": self.median_submit_latency_ms,
            "p95_submit_latency_ms": self.p95_submit_latency_ms,
            "median_fill_latency_ms": self.median_fill_latency_ms,
            "disconnects": self.disconnects,
            "reconnects": self.reconnects,
            "fees": self.fees,
        }


class ExecutionMonitor:
    """
    Accumulates health, metrics and alerts for a session.

    Deliberately does not decide policy. It measures and reports; the
    limit governor decides whether the measurements permit an order.
    """

    def __init__(self, session_id: Optional[str] = None):
        self.session_id = session_id
        self.metrics = ExecutionMetrics()
        self.alerts: List[Alert] = []
        self._health: Optional[SystemHealth] = None

    # ---------------- observation ----------------

    def observe_order(self, order: ExecutionOrder,
                      submit_latency_ms: Optional[float] = None) -> None:
        """Fold one order's outcome into the metrics."""
        self.metrics.orders_submitted += 1
        if submit_latency_ms is not None:
            self.metrics.submit_latency_ms.append(float(submit_latency_ms))

        state = order.state
        if state is ExecutionOrderState.FILLED:
            self.metrics.orders_filled += 1
        elif state is ExecutionOrderState.PARTIALLY_FILLED:
            self.metrics.orders_partially_filled += 1
        elif state is ExecutionOrderState.REJECTED:
            self.metrics.orders_rejected += 1
        elif state is ExecutionOrderState.CANCELLED:
            self.metrics.orders_cancelled += 1
        elif state.needs_reconciliation:
            self.metrics.orders_unknown += 1

        latency = order.execution_latency_seconds
        if latency is not None:
            self.metrics.ack_latency_ms.append(latency * 1000.0)

        slippage = order.slippage_bps
        if slippage is not None:
            self.metrics.slippage_bps.append(slippage)

        self.metrics.fees += order.commission + order.fees

    def observe_fill(self, fill: ExecutionFill,
                     order: Optional[ExecutionOrder] = None) -> None:
        self.metrics.fees += fill.total_cost
        if order is not None and order.submitted_at and fill.filled_at:
            delta = (fill.filled_at - order.submitted_at).total_seconds()
            if delta >= 0:
                self.metrics.fill_latency_ms.append(delta * 1000.0)

    def observe_reconciliation(self, clean: bool, mismatches: int = 0) -> None:
        self.metrics.reconciliations += 1
        if not clean:
            self.metrics.reconciliation_mismatches += max(1, mismatches)

    def observe_connection(self, connected: bool) -> None:
        if connected:
            self.metrics.reconnects += 1
        else:
            self.metrics.disconnects += 1

    # ---------------- health ----------------

    def set_health(self, health: SystemHealth) -> SystemHealth:
        self._health = health
        return health

    @property
    def health(self) -> Optional[SystemHealth]:
        return self._health

    # ---------------- alerts ----------------

    def alert(self, code: str, severity: AlertSeverity, message: str,
              at: Optional[datetime] = None, detail: str = "",
              order_id: Optional[str] = None) -> Alert:
        record = Alert(
            alert_id=f"al-{uuid.uuid4().hex[:16]}", code=code,
            severity=severity, message=message, at=at, detail=detail,
            session_id=self.session_id, order_id=order_id)
        self.alerts.append(record)
        return record

    def raise_health_alerts(self, health: SystemHealth,
                            at: Optional[datetime] = None) -> List[Alert]:
        """
        One alert per unhealthy capability (spec §46).

        Quiet when everything is fine: an alert that fires on every
        cycle trains people to ignore all of them.
        """
        raised: List[Alert] = []
        for capability, reading in sorted(
                health.readings.items(), key=lambda kv: kv[0].value):
            if reading.state.permits_new_orders:
                continue
            severity = (AlertSeverity.CRITICAL
                        if capability in ORDER_CRITICAL
                        else AlertSeverity.WARNING)
            raised.append(self.alert(
                f"capability_{reading.state.value}", severity,
                f"{capability.value} is {reading.state.value}",
                at=at, detail=reading.detail))
        return raised

    @property
    def unacknowledged(self) -> List[Alert]:
        return [a for a in self.alerts if not a.acknowledged]

    @property
    def worst_severity(self) -> Optional[AlertSeverity]:
        pending = self.unacknowledged
        if not pending:
            return None
        return max((a.severity for a in pending), key=lambda s: s.rank)

    def state(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "metrics": self.metrics.as_dict(),
            "health": self._health.as_dict() if self._health else None,
            "alerts": {
                "total": len(self.alerts),
                "unacknowledged": len(self.unacknowledged),
                "worst": (self.worst_severity.value
                          if self.worst_severity else None),
                "recent": [a.as_dict() for a in self.alerts[-20:]],
            },
        }


# ============================================================
# Backtest / paper / live comparison (spec §23, §61, §62)
# ============================================================

#: Metrics whose divergence points at a mechanical cause worth
#: investigating.
DIAGNOSTIC_METRICS: Tuple[str, ...] = (
    "fill_rate", "rejection_rate", "median_slippage_bps",
    "median_submit_latency_ms", "unknown_state_rate", "fees_per_trade",
    "signals_per_day", "turnover",
)

#: Reported for context, never treated as evidence. A short live
#: period cannot distinguish skill from noise any better than a short
#: paper period could — the Phase 13 lesson, unchanged.
OUTCOME_METRICS: Tuple[str, ...] = (
    "total_return", "win_rate", "max_drawdown", "profit_factor",
)

#: Relative divergence past which a diagnostic metric is flagged.
DRIFT_THRESHOLD = 0.50


@dataclass
class ComparisonRow:
    metric: str
    backtest: Optional[float] = None
    paper: Optional[float] = None
    live: Optional[float] = None
    diagnostic: bool = True

    def divergence(self, left: str = "paper",
                   right: str = "live") -> Optional[float]:
        a = getattr(self, left)
        b = getattr(self, right)
        if a is None or b is None or a == 0:
            return None
        return finite_or_none((b - a) / abs(a))

    def has_drifted(self, left: str = "paper", right: str = "live") -> bool:
        if not self.diagnostic:
            return False
        drift = self.divergence(left, right)
        return drift is not None and abs(drift) > DRIFT_THRESHOLD


@dataclass
class EnvironmentComparison:
    """
    Backtest against paper against live (spec §61).

    `is_conclusive` is almost always False and says why. The field
    exists so the caveat travels with the numbers rather than living
    in a docstring nobody reads.
    """
    at: Optional[datetime] = None
    rows: List[ComparisonRow] = field(default_factory=list)
    live_trades: int = 0
    live_days: int = 0
    notes: List[str] = field(default_factory=list)

    def __post_init__(self):
        self.at = _require_utc(self.at, "at")

    @property
    def is_conclusive(self) -> bool:
        return self.live_trades >= 30 and self.live_days >= 60

    def drifted(self, left: str = "paper", right: str = "live") -> List[ComparisonRow]:
        return [r for r in self.rows if r.has_drifted(left, right)]

    def summary(self) -> Dict[str, Any]:
        return {
            "at": self.at.isoformat() if self.at else None,
            "live_trades": self.live_trades, "live_days": self.live_days,
            "conclusive": self.is_conclusive,
            "drifted": [r.metric for r in self.drifted()],
            "rows": [{"metric": r.metric, "backtest": r.backtest,
                      "paper": r.paper, "live": r.live,
                      "diagnostic": r.diagnostic} for r in self.rows],
            "notes": self.notes,
            "caveat": ("mechanical divergences are diagnostic; return "
                       "differences over a short period are not evidence "
                       "about a strategy"),
        }


def compare_environments(at: datetime,
                         backtest: Optional[Dict[str, Any]] = None,
                         paper: Optional[Dict[str, Any]] = None,
                         live: Optional[Dict[str, Any]] = None
                         ) -> EnvironmentComparison:
    """Build the three-way comparison from metric dictionaries."""
    backtest, paper, live = backtest or {}, paper or {}, live or {}
    comparison = EnvironmentComparison(
        at=at,
        live_trades=int(live.get("trades") or 0),
        live_days=int(live.get("days") or 0))

    for metric in DIAGNOSTIC_METRICS:
        comparison.rows.append(ComparisonRow(
            metric=metric, backtest=finite_or_none(backtest.get(metric)),
            paper=finite_or_none(paper.get(metric)),
            live=finite_or_none(live.get(metric)), diagnostic=True))

    for metric in OUTCOME_METRICS:
        comparison.rows.append(ComparisonRow(
            metric=metric, backtest=finite_or_none(backtest.get(metric)),
            paper=finite_or_none(paper.get(metric)),
            live=finite_or_none(live.get(metric)), diagnostic=False))

    if not comparison.is_conclusive:
        comparison.notes.append(
            f"{comparison.live_trades} live trade(s) over "
            f"{comparison.live_days} day(s) — below the 30 trades and 60 days "
            f"this comparison would need to mean anything")

    for row in comparison.drifted():
        comparison.notes.append(
            f"{row.metric} diverges between paper and live by "
            f"{row.divergence():.0%}")

    return comparison
