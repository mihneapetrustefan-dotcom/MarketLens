"""
src/paper/health.py
------------------------
Pipeline health, heartbeats and latency (Phase 13, spec §34-§37, §70, §77).

WHY HEALTH IS A GATE, NOT A DASHBOARD WIDGET
------------------------------------------------
Spec §61 requires that when critical components fail, the default is to
STOP creating orders. That only works if health is something the
decision path consults, so `SystemHealth.allows_new_orders` is checked
before any order is created — not merely rendered somewhere.

The aggregate is the WORST component, never an average. A pipeline
whose model has failed is not "mostly healthy", and averaging is
exactly how a failing component gets hidden behind healthy ones.

HEARTBEATS DETECT SILENCE, WHICH ERRORS CANNOT
--------------------------------------------------
A component that raises is visible. A component that silently stops
running is not — and in a scheduled system that is the more likely
failure. Heartbeats turn absence into a positive signal: a stage that
has not reported within its expected interval is STALE, whether or not
anything ever threw.

LATENCY IS MEASURED HONESTLY
--------------------------------
These are wall-clock milliseconds inside one process. Spec §36 warns
against claiming precision the architecture cannot support, and this
one cannot support market-data latency at all: there is no streaming
feed to measure arrival against. What IS measurable is how long each
pipeline stage takes, and that is what these record.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterator, List, Optional, Sequence

from src.domain.paper_models import (
    AlertSeverity, ComponentHealth, DataFreshness, HealthState, LatencySample,
    PaperAlert, SystemHealth, finite_or_none,
)
from src.paper.clock import require_utc

#: Every stage that reports. Used for the health a session DISPLAYS.
PIPELINE_COMPONENTS = (
    "market_data",
    "freshness",
    "signals",
    "risk",
    "executor",
    "ledger",
    "persistence",
)

#: The subset that must be working to make a trading decision.
#:
#: Deliberately excludes `ledger` and `persistence`, which run AFTER
#: orders are placed within a tick. Gating on them would mean judging a
#: decision against stages that have not run yet — on the first tick
#: they have never reported at all, so every order would be blocked
#: forever. They are still reported, and a failure in either still
#: enters safe mode on the following tick.
DECISION_COMPONENTS = (
    "market_data",
    "freshness",
    "signals",
    "risk",
    "executor",
)

#: How long a component may go unheard from before it is STALE.
#:
#: This is NOT a universal constant — it has to exceed the session's
#: tick cadence, or every scheduled session declares itself stale
#: between ticks. A daily-bar session ticking once per session needs
#: days, not hours. Sessions set it from their configured cadence; this
#: default covers a daily cadence across a weekend.
DEFAULT_HEARTBEAT_TIMEOUT_SECONDS = 4 * 86_400.0


@dataclass
class HeartbeatMonitor:
    """
    Tracks when each component last reported.

    Deliberately separate from `SystemHealth`: health is a judgement at
    a moment, heartbeats are the history that judgement reads. Keeping
    them apart means a tick can record what happened without having to
    decide what it means.
    """
    timeout_seconds: float = DEFAULT_HEARTBEAT_TIMEOUT_SECONDS
    beats: Dict[str, datetime] = field(default_factory=dict)
    details: Dict[str, str] = field(default_factory=dict)

    def beat(self, component: str, at: datetime, detail: str = "") -> None:
        self.beats[component] = require_utc(at, "at")
        if detail:
            self.details[component] = detail

    def age_seconds(self, component: str, now: datetime) -> Optional[float]:
        last = self.beats.get(component)
        if last is None:
            return None
        return (require_utc(now, "now") - last).total_seconds()

    def is_stale(self, component: str, now: datetime) -> bool:
        age = self.age_seconds(component, now)
        return age is None or age > self.timeout_seconds

    def missing(self, now: datetime,
                expected: Iterator[str] = None) -> List[str]:
        """Components that have never reported, or not recently enough."""
        names = list(expected) if expected is not None else list(PIPELINE_COMPONENTS)
        return [name for name in names if self.is_stale(name, now)]


class LatencyTracker:
    """
    Measures how long each pipeline stage takes (spec §36, §77).

    Used as a context manager so a stage cannot be timed without the
    measurement being recorded, and cannot be recorded without actually
    having run.
    """

    def __init__(self):
        self.samples: List[LatencySample] = []

    @contextmanager
    def measure(self, stage: str, at: Optional[datetime] = None) -> Iterator[None]:
        started = time.perf_counter()
        try:
            yield
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            self.samples.append(LatencySample(
                stage=stage, milliseconds=round(elapsed_ms, 3), at=at))

    def record(self, stage: str, milliseconds: float,
               at: Optional[datetime] = None) -> None:
        self.samples.append(LatencySample(
            stage=stage, milliseconds=round(float(milliseconds), 3), at=at))

    def total_ms(self) -> float:
        return round(sum(s.milliseconds for s in self.samples), 3)

    def by_stage(self) -> Dict[str, float]:
        totals: Dict[str, float] = {}
        for sample in self.samples:
            totals[sample.stage] = round(
                totals.get(sample.stage, 0.0) + sample.milliseconds, 3)
        return totals

    def slowest(self) -> Optional[LatencySample]:
        return max(self.samples, key=lambda s: s.milliseconds, default=None)

    def reset(self) -> None:
        self.samples = []


class HealthMonitor:
    """Turns heartbeats, freshness and failures into a health verdict."""

    def __init__(self, heartbeats: Optional[HeartbeatMonitor] = None):
        self.heartbeats = heartbeats or HeartbeatMonitor()
        self.failures: Dict[str, str] = {}
        self.safe_mode = False
        self.safe_mode_reason = ""

    # ---------------- recording ----------------

    def beat(self, component: str, at: datetime, detail: str = "") -> None:
        self.heartbeats.beat(component, at, detail)
        self.failures.pop(component, None)

    def fail(self, component: str, reason: str) -> None:
        """
        Record a component failure.

        A failure persists until that component beats again, so a stage
        that broke and then silently stopped running stays failed
        rather than quietly ageing into merely stale.
        """
        self.failures[component] = reason

    def enter_safe_mode(self, reason: str) -> None:
        """Spec §61 — halt new orders, keep the portfolio observable."""
        self.safe_mode = True
        self.safe_mode_reason = reason

    def exit_safe_mode(self) -> None:
        self.safe_mode = False
        self.safe_mode_reason = ""

    # ---------------- judgement ----------------

    def evaluate(self, now: datetime,
                 freshness: Optional[DataFreshness] = None,
                 latencies: Optional[Dict[str, float]] = None,
                 components: Optional[Sequence[str]] = None) -> SystemHealth:
        """
        Judge health at `now`.

        `components` selects WHICH stages count. A session gates orders
        on `DECISION_COMPONENTS` and reports on `PIPELINE_COMPONENTS`,
        because stages that run after the decision cannot sensibly be
        required to have reported before it.
        """
        require_utc(now, "now")
        health = SystemHealth(at=now, safe_mode=self.safe_mode,
                              safe_mode_reason=self.safe_mode_reason)
        stage_latency = latencies or {}

        for component in (components or PIPELINE_COMPONENTS):
            if component in self.failures:
                health.record(component, HealthState.FAILED, now,
                              self.failures[component],
                              stage_latency.get(component))
                continue

            last = self.heartbeats.beats.get(component)
            if last is None:
                health.record(component, HealthState.STALE, now,
                              "has never reported", stage_latency.get(component))
                continue

            age = (now - last).total_seconds()
            if age > self.heartbeats.timeout_seconds:
                health.record(component, HealthState.STALE, now,
                              f"last reported {age / 3600:.1f}h ago",
                              stage_latency.get(component))
            else:
                health.record(component, HealthState.HEALTHY, now,
                              self.heartbeats.details.get(component, ""),
                              stage_latency.get(component))

        # Data freshness degrades the market_data component specifically,
        # rather than the whole pipeline: everything else may be working
        # perfectly while the cache is simply old.
        if freshness is not None:
            if freshness in (DataFreshness.INVALID, DataFreshness.UNAVAILABLE):
                health.record("market_data", HealthState.FAILED, now,
                              f"market data is {freshness.value}")
            elif freshness == DataFreshness.STALE:
                health.record("market_data", HealthState.STALE, now,
                              "market data is older than the freshness policy allows")
            elif freshness == DataFreshness.AGING:
                health.record("market_data", HealthState.DEGRADED, now,
                              "market data is ageing but still tradeable")

        return health

    # ---------------- alerts ----------------

    def alerts_for(self, health: SystemHealth, session_id: str,
                   now: datetime) -> List[PaperAlert]:
        """
        Raise one alert per unhealthy component (spec §37).

        Deliberately quiet when everything is fine: spec §37 warns
        against noisy alerts, and an alert that fires on every tick
        trains people to ignore all of them.
        """
        alerts: List[PaperAlert] = []
        for name, component in sorted(health.components.items()):
            if component.state == HealthState.HEALTHY:
                continue
            severity = (AlertSeverity.CRITICAL
                        if component.state == HealthState.FAILED
                        else AlertSeverity.WARNING)
            alerts.append(PaperAlert(
                alert_id=f"al-{session_id}-{name}-{int(now.timestamp())}",
                session_id=session_id, code=f"component_{component.state.value}",
                severity=severity,
                message=f"{name} is {component.state.value}",
                detail=component.detail, at=now))

        if health.safe_mode:
            alerts.append(PaperAlert(
                alert_id=f"al-{session_id}-safemode-{int(now.timestamp())}",
                session_id=session_id, code="safe_mode",
                severity=AlertSeverity.CRITICAL,
                message="session is in safe mode; no new orders will be created",
                detail=health.safe_mode_reason, at=now))
        return alerts
