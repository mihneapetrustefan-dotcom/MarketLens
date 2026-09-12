"""
src/trading/session_runner.py
-------------------------------------------
The session runner (Phase 25.8).

WHAT THIS IS
----------------
An operating system for the existing trading architecture during a
market session. It decides WHEN things happen; it does not decide WHAT
they decide. Model inference, portfolio construction, risk logic and
execution mapping all stay exactly where they were -- this
orchestrates them.

It replaces manual dispatch. It does not replace governance: the IBKR
browser login, model promotion and paper-order authorisation all
remain human actions, and the runner reports when it is waiting on
one rather than pretending to be unattended.

WHAT IT REFUSES TO DO
-------------------------
Run on simulated time. `RunMode.requires_wall_clock` is checked at
construction, so a replay clock cannot drive a session against a live
venue. That is the Phase 25.8 headline defect, and it is closed
structurally rather than by convention.

Trade because it is running. The runner reaching the execution
boundary is not permission to send an order: the Phase 14/25 paper
gates still decide that, and `orders_enabled` defaults to False.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence

from src.domain.paper_models import HealthState
from src.marketdata.prices import operational_price, tradeable_prices
from src.trading.clock import Clock, ClockModeViolation, RunMode, clock_for
from src.trading.schedule import Schedule

#: A session that has produced no usable market data for this long is
#: not degraded, it is blind. Bounded so a runner cannot spend an
#: entire session reporting DEGRADED while acting on nothing.
BLIND_SESSION_LIMIT_SECONDS = 900.0

#: How long a runner's lease on a session is honoured without a
#: heartbeat. A crashed runner must not hold the session forever;
#: another may take over only after this.
DEFAULT_LEASE_SECONDS = 300.0


class SessionRefused(RuntimeError):
    """The session could not start, with a named reason."""


@dataclass
class StageOutcome:
    """What one scheduled stage did, and whether it can be trusted."""
    name: str
    ran: bool = False
    ok: bool = True
    detail: str = ""
    duration_seconds: float = 0.0

    @property
    def failed(self) -> bool:
        return self.ran and not self.ok


@dataclass
class SessionTick:
    """One heartbeat of the runner."""
    at: datetime
    stages: List[StageOutcome] = field(default_factory=list)
    health: HealthState = HealthState.HEALTHY
    blocks: List[str] = field(default_factory=list)
    cycle_id: str = ""
    orders_submitted: int = 0

    def stage(self, name: str) -> Optional[StageOutcome]:
        for outcome in self.stages:
            if outcome.name == name:
                return outcome
        return None

    @property
    def failures(self) -> List[str]:
        return [s.name for s in self.stages if s.failed]


@dataclass
class SessionState:
    """
    Everything needed to describe, restart or audit a session.

    `fingerprint` is the operational configuration this session ran
    under (§41). It is recorded once at start so a later forensic
    reader knows which schedule, policies and versions produced the
    rows, rather than inferring them from whatever the code says
    today.
    """
    session_id: str
    mode: RunMode
    started_at: datetime
    worker: str = ""
    closed_at: Optional[datetime] = None
    ticks: int = 0
    cycles_run: int = 0
    orders_submitted: int = 0
    last_market_data_at: Optional[datetime] = None
    last_health: HealthState = HealthState.HEALTHY
    fingerprint: Dict[str, Any] = field(default_factory=dict)
    blocks: List[str] = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return self.closed_at is None


def session_id_for(mode: RunMode, now: datetime, account: str = "") -> str:
    """Deterministic per mode, day and account, so a restart rejoins."""
    day = now.astimezone(timezone.utc).strftime("%Y-%m-%d")
    raw = f"{mode.value}|{day}|{account}"
    return f"sess-{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]}"


class SessionRunner:
    """
    Runs one market session, tick by tick, on real time.

    Deliberately NOT a daemon in the sense of owning its own process
    forever: `run_until_close` is a bounded loop that exits at session
    close or when told to stop. The runtime that keeps it alive across
    days is a deployment concern documented separately, not something
    this class pretends to solve.
    """

    def __init__(self, conn: sqlite3.Connection, loop, market_data,
                 mode: RunMode = RunMode.PAPER_SESSION,
                 clock: Optional[Clock] = None,
                 schedule: Optional[Schedule] = None,
                 worker: str = "",
                 orders_enabled: bool = False,
                 max_ticks: Optional[int] = None):
        # Checked FIRST. A runner that reached a live venue on a
        # simulated clock would produce real rows stamped at moments
        # that never happened.
        self.clock = clock_for(mode, clock)
        self.mode = mode
        self.conn = conn
        self.loop = loop
        self.market_data = market_data
        self.schedule = schedule or Schedule.default()
        self.worker = worker or "session-runner"
        #: Reaching the execution boundary is not permission to send.
        self.orders_enabled = bool(orders_enabled)
        self.max_ticks = max_ticks
        self.state: Optional[SessionState] = None
        self._stop = False
        self._blind_since: Optional[datetime] = None

    # ---------------- lifecycle ----------------

    def start(self, account: str = "") -> SessionState:
        """
        Open a session, refusing rather than starting half-ready.

        A connected gateway is NOT sufficient (§6). Usable market data
        is required, because a session that cannot price anything can
        observe nothing worth observing.
        """
        now = self.clock.now()
        if not self.market_data.session.any_open(
                self._universe(), now):
            raise SessionRefused(
                "no instrument is in session; there is nothing to run")

        health = self.market_data.health(now)
        if not health.connected:
            raise SessionRefused(
                "the broker session is not connected; a human may need to "
                "log into the Client Portal Gateway in a browser")

        state = SessionState(
            session_id=session_id_for(self.mode, now, account),
            mode=self.mode, started_at=now, worker=self.worker,
            fingerprint=self.fingerprint())
        self.state = state
        return state

    def fingerprint(self) -> Dict[str, Any]:
        """The operational configuration this session runs under (§41)."""
        from src.domain.market_data_models import OPERATIONAL_FRESHNESS
        from src.domain.trading_loop_models import LOOP_METHOD_VERSION
        return {
            "mode": self.mode.value,
            "loop_method_version": LOOP_METHOD_VERSION,
            "tick_seconds": self.schedule.tick_seconds,
            "cadences": {name: c.interval_seconds
                         for name, c in sorted(self.schedule.cadences.items())},
            "price_freshness": {
                "fresh": OPERATIONAL_FRESHNESS.fresh_seconds,
                "aging": OPERATIONAL_FRESHNESS.aging_seconds,
                "stale": OPERATIONAL_FRESHNESS.stale_seconds,
            },
            "orders_enabled": self.orders_enabled,
            "clock": type(self.clock).__name__,
            "worker": self.worker,
        }

    def stop(self) -> None:
        """Ask the runner to finish after the current tick."""
        self._stop = True

    def close(self) -> Optional[SessionState]:
        """Close the session cleanly. Idempotent."""
        if self.state is None or not self.state.is_open:
            return self.state
        self.state.closed_at = self.clock.now()
        return self.state

    # ---------------- the loop ----------------

    def run_until_close(self, account: str = "") -> SessionState:
        """
        Run from now until the session closes.

        Each iteration does its work, then waits until the NEXT grid
        boundary -- never `sleep(interval)` after the work, which
        drifts. A tick that overruns its boundary skips to the next
        future one rather than replaying a backlog of moments that
        have gone.
        """
        state = self.state or self.start(account)
        while not self._stop:
            started = self.clock.now()
            if not self._session_still_open(started):
                break

            tick = self.run_tick(started)
            state.ticks += 1
            state.last_health = tick.health
            state.orders_submitted += tick.orders_submitted
            if tick.cycle_id:
                state.cycles_run += 1

            if self.max_ticks is not None and state.ticks >= self.max_ticks:
                break

            finished = self.clock.now()
            self.clock.sleep_until(
                self.schedule.next_tick_after_work(started, finished))
        self.close()
        return state

    def run_tick(self, now: Optional[datetime] = None) -> SessionTick:
        """
        One heartbeat.

        Stage failures are CLASSIFIED, not fatal (§34). Market data
        failing blocks trading; a slow reconciliation blocks new
        exposure; neither ends the session, because a runner that
        exits on the first hiccup is worse than one that keeps
        observing and refuses to act.
        """
        now = now or self.clock.now()
        tick = SessionTick(at=now)

        market = self._run_stage(tick, "market_data", now, self._stage_market_data)
        if market.ran and not market.ok:
            tick.blocks.append("market data unavailable; trading blocked")

        usable = self._usable_prices(now)
        if not usable:
            tick.blocks.append("no instrument has a fresh operational price")
            self._blind_since = self._blind_since or now
        else:
            self._blind_since = None
            if self.state is not None:
                self.state.last_market_data_at = now

        for name, handler in (("features", self._stage_features),
                              ("signals", self._stage_signals),
                              ("portfolio", self._stage_portfolio),
                              ("risk", self._stage_risk)):
            outcome = self._run_stage(tick, name, now, handler)
            if outcome.ran and not outcome.ok:
                tick.blocks.append(f"{name} failed: {outcome.detail}")

        # The existing loop owns eligibility, intents, execution,
        # broker polling and reconciliation. It is invoked on the
        # reconciliation cadence, and it applies its own gates.
        if self.schedule.cadences["reconciliation"].is_due(now):
            outcome = self._run_stage(
                tick, "reconciliation", now, self._stage_loop_cycle)
            if outcome.ran and not outcome.ok:
                tick.blocks.append("loop cycle failed; new exposure blocked")

        tick.health = self._health_for(tick, now, bool(usable))
        if self.state is not None:
            self.state.blocks = list(tick.blocks)
        return tick

    # ---------------- stages ----------------

    def _run_stage(self, tick: SessionTick, name: str, now: datetime,
                   handler: Callable[[datetime], str]) -> StageOutcome:
        outcome = StageOutcome(name=name)
        cadence = self.schedule.cadences.get(name)
        if cadence is not None and not cadence.is_due(now):
            tick.stages.append(outcome)
            return outcome
        started = self.clock.now()
        outcome.ran = True
        try:
            outcome.detail = handler(now) or ""
        except Exception as error:                        # noqa: BLE001
            outcome.ok = False
            outcome.detail = f"{type(error).__name__}: {error}"
        outcome.duration_seconds = max(
            0.0, (self.clock.now() - started).total_seconds())
        self.schedule.mark(name, now)
        tick.stages.append(outcome)
        return outcome

    def _stage_market_data(self, now: datetime) -> str:
        cycle = self.market_data.run_cycle()
        return (f"{cycle.tradeable}/{cycle.requested} tradeable, "
                f"{cycle.bars_written} bar(s)")

    def _stage_features(self, now: datetime) -> str:
        """
        Intraday feature refresh (§14, §15).

        Phase 25.8 establishes the CADENCE and the boundary; the
        computation itself belongs to the feature layer and is not
        rebuilt here. What this proves is that completed bars reach a
        refresh on a schedule, which is the bridge the batch pipeline
        never had.
        """
        from src.marketdata.repository import MarketDataRepository
        repository = MarketDataRepository(self.conn)
        refreshed = 0
        for entry in self._universe():
            bars = repository.bars_for(entry.instrument_id, limit=5)
            if bars:
                refreshed += 1
        return f"{refreshed} instrument(s) have completed intraday bars"

    def _stage_signals(self, now: datetime) -> str:
        """
        Intraday signal evaluation (§16, §19).

        NO SIGNAL IS NOT A FAILURE. With no deployable model, or no
        signal clearing the confidence floor, the correct result is no
        trade and a running loop -- never a crash and never a lowered
        threshold.
        """
        try:
            deployable = self.loop.deployable_models()
        except Exception:                                 # noqa: BLE001
            deployable = None
        if not deployable:
            return "no deployable model; observing only"
        return "signal evaluation due"

    def _stage_portfolio(self, now: datetime) -> str:
        """Revalue against CURRENT operational prices only (§13, §21)."""
        prices = self._usable_prices(now)
        return f"revaluation input: {len(prices)} current price(s)"

    def _stage_risk(self, now: datetime) -> str:
        """
        Periodic risk (§22, §23).

        Runs on a timer as well as before every order, because
        exposure, concentration and drawdown all move when the market
        moves and no new signal is required for that. It reports
        state; it does not invent a defensive trade.
        """
        prices = self._usable_prices(now)
        if not prices:
            return "no fresh prices; price-dependent risk not evaluated"
        return f"price-dependent risk evaluable on {len(prices)} instrument(s)"

    def _stage_loop_cycle(self, now: datetime) -> str:
        """
        Advance the existing Phase 25 loop exactly once, on real time.

        `now` is the runner's wall clock. The loop derives its own
        anchor and idempotency keys from it, so a repeated tick at the
        same anchor is the same cycle rather than a second one.
        """
        result = self.loop.run_cycle(now=now, worker=self.worker)
        return (f"cycle {getattr(result, 'cycle_id', '')} "
                f"submitted {getattr(result, 'orders_submitted', 0)}")

    # ---------------- helpers ----------------

    def _universe(self) -> Sequence[Any]:
        from src.marketdata import universe as universe_module
        return universe_module.active(
            universe_module.resolve_universe(self.conn))

    def _usable_prices(self, now: datetime) -> Dict[str, float]:
        """
        Only prices that may back a decision.

        Reads the operational layer. There is no research-cache
        fallback here and none is reachable from here.
        """
        return tradeable_prices(
            self.conn, [e.instrument_id for e in self._universe()], now)

    def _session_still_open(self, now: datetime) -> bool:
        return self.market_data.session.any_open(self._universe(), now)

    def _health_for(self, tick: SessionTick, now: datetime,
                    has_prices: bool) -> HealthState:
        """
        The worst reading, never an average.

        A session blind for longer than the bound is FAILED rather
        than indefinitely DEGRADED: reporting degraded forever while
        acting on nothing is how an outage looks healthy.
        """
        if tick.failures:
            return HealthState.FAILED
        if not has_prices:
            if (self._blind_since is not None
                    and (now - self._blind_since).total_seconds()
                    > BLIND_SESSION_LIMIT_SECONDS):
                return HealthState.FAILED
            return HealthState.DEGRADED
        if tick.blocks:
            return HealthState.DEGRADED
        return HealthState.HEALTHY

    def describe(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        """Operational state for an observer (§38)."""
        now = now or self.clock.now()
        state = self.state
        return {
            "session": {
                "id": state.session_id if state else None,
                "mode": self.mode.value,
                "open": bool(state and state.is_open),
                "started_at": state.started_at.isoformat() if state else None,
                "ticks": state.ticks if state else 0,
                "cycles": state.cycles_run if state else 0,
                "health": state.last_health.value if state else None,
                "blocks": list(state.blocks) if state else [],
            },
            "clock": {
                "now": now.isoformat(),
                "wall_clock": self.clock.is_wall_clock,
            },
            "schedule": self.schedule.summary(now),
            "orders_enabled": self.orders_enabled,
        }
