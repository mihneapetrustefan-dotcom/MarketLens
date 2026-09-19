"""
src/capture/runner.py
-----------------------------
The capture runner: one process, one universe, every regular session.

LIFECYCLE (§8-§14)
----------------------

    STARTING ─► PREFLIGHT ─► WAITING_FOR_MARKET ─► ACTIVE_SESSION ─► CLOSING
                   │  ▲                                 │               │
                   ▼  │                                 ▼               ▼
            WAITING_FOR_AUTH ◄──────────────────── (auth lost)     POST_SESSION
                                                                        │
                                            IDLE ◄──────────────────────┘

The exchange calendar (`USEquityCalendar`, 25.9E) decides every
transition; nothing here knows an opening time. PREFLIGHT starts
`pre_open_minutes` before the open, so that authentication and contract
mapping are settled BEFORE the first minute rather than during it.

AUTHENTICATION BELONGS TO A HUMAN (§11)
-------------------------------------------
The Client Portal Gateway is logged in by a person, in a browser, with
their second factor. This runner never types, stores, forwards or
scripts a credential. It ASKS the gateway whether a session exists
(`IBKRGateway.connect`), and when none does it enters
WAITING_FOR_AUTH, says so in its heartbeat, retries the question every
`auth_retry_seconds`, and resumes on its own when the answer changes.
`capture_status` exits 1 while this lasts so a person can be told.

WHAT ONE ACTIVE TICK DOES (§23-§29)
---------------------------------------
Once a minute, two seconds after the boundary so the minute just ended
can be sealed:

  1. keepalive the gateway session                         1 request
  2. `MarketDataService.run_cycle` for the resolved members 1 request
     -- the canonical service: one batched snapshot, validation,
     `MinuteBarBuilder` (explicit gap rows, never an interpolated
     price), operational state.
  3. `archive_operational_bars` for this session's window: completed,
     non-gap minutes cross into the research cache, idempotently,
     each recorded in `capture_archive_log`.
  4. every `feature_every_minutes`, the 25.9F feature set at the last
     closed boundary, persisted.
  5. one `capture_ticks` row and a heartbeat.

HOST SLEEP IS DETECTED, NEVER FILLED (§45-§47)
--------------------------------------------------
Every step knows when it expected to wake. Waking more than
`host_gap_seconds` later means the process did not run -- a laptop
lid, a suspended VM, a debugger. That is recorded as a
HOST_SUSPEND_GAP event with its length, the session's quality accounts
for it, and the missing minutes stay missing: the bar builder's gap
rows are markers that are never archived.

ONE RUNNER (§56)
--------------------
A lease in `session_runner_leases` (scope `capture:ibkr`), owned by the
SUPERVISOR's identity. A child restarted by the same supervisor
re-acquires at once; a second supervisor is refused and exits 3.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from src.capture import features as capture_features
from src.capture import quality
from src.capture import universe as capture_universe
from src.capture.schema import initialize_capture_schema
from src.execution.adapters.submission_guard import broker_write_attempts
from src.marketdata import quotes as quote_acquisition
from src.marketdata import universe as md_universe
from src.marketdata.bars import MinuteBarBuilder, minute_floor
from src.marketdata.calendar import NEW_YORK, SessionWindow, USEquityCalendar
from src.marketdata.intraday import (
    ARCHIVE_VERSION, LIVE_CAPTURE_SOURCE, archive_operational_bars,
)
from src.marketdata.service import MarketDataService
from src.trading import leases

LOG = logging.getLogger("marketlens.capture")

LEASE_SCOPE = "capture:ibkr"

#: Exit codes shared by run_capture, the supervisor and capture_status.
EXIT_CLEAN = 0
EXIT_CRASH = 1
EXIT_CONFIG = 2
EXIT_DUPLICATE = 3
EXIT_SAFETY = 4


class CaptureState(str, Enum):
    STARTING = "STARTING"
    PREFLIGHT = "PREFLIGHT"
    WAITING_FOR_AUTH = "WAITING_FOR_AUTH"
    WAITING_FOR_MARKET = "WAITING_FOR_MARKET"
    ACTIVE_SESSION = "ACTIVE_SESSION"
    CLOSING = "CLOSING"
    POST_SESSION = "POST_SESSION"
    IDLE = "IDLE"
    STOPPED = "STOPPED"


class CaptureConfigError(RuntimeError):
    """The runner cannot capture as configured. Restarting will not help."""


class CaptureSafetyViolation(RuntimeError):
    """A venue write was attempted. The guard refused it; the process stops."""


@dataclass
class CaptureConfig:
    interval_seconds: float = 60.0
    tick_offset_seconds: float = 2.0
    pre_open_minutes: int = 20
    warmup_minutes: int = 2
    post_close_minutes: int = 10
    auth_retry_seconds: float = 60.0
    idle_poll_seconds: float = 300.0
    feature_every_minutes: int = 5
    mapping_retry_minutes: int = 15
    lease_ttl_seconds: float = 900.0
    host_gap_seconds: float = 180.0
    locked_retry_seconds: float = 5.0
    source_label: str = LIVE_CAPTURE_SOURCE
    bar_retention_days: int = 30


def _iso(moment: Optional[datetime]) -> Optional[str]:
    return moment.astimezone(timezone.utc).isoformat() if moment else None


def session_id_for(window: SessionWindow) -> str:
    return f"cap-{window.day.isoformat()}"


class CaptureRunner:

    def __init__(self, conn: sqlite3.Connection, gateway: Any,
                 definition: capture_universe.UniverseDefinition, *,
                 clock: Any, lease_owner: str,
                 config: Optional[CaptureConfig] = None,
                 calendar: Optional[USEquityCalendar] = None,
                 stop_requested: Callable[[], bool] = lambda: False,
                 instance_id: Optional[str] = None):
        if not getattr(gateway, "submission_forbidden", False) or not getattr(
                getattr(gateway, "transport", None), "submission_forbidden", False):
            # Structural, not advisory: an unguarded gateway is refused
            # before a single request is made.
            raise CaptureConfigError(
                "capture requires a capture_only() gateway: both the gateway "
                "and its transport must refuse venue writes")
        self.conn = conn
        self.gateway = gateway
        self.definition = definition
        self.clock = clock
        self.config = config or CaptureConfig()
        self.calendar = calendar or USEquityCalendar()
        self.stop_requested = stop_requested
        self.lease_owner = lease_owner
        self.instance_id = instance_id or f"cap-{uuid.uuid4().hex[:12]}"

        self.state = CaptureState.STARTING
        self.auth_state = "unknown"
        self.connected = False
        self.lease: Optional[leases.Lease] = None
        self.budget = capture_universe.RequestBudget(
            getattr(gateway.config, "max_requests_per_minute", 50) or 50)
        self.service = MarketDataService(conn, gateway,
                                         interval_seconds=self.config.interval_seconds)
        self.outcomes: Dict[str, capture_universe.MappingOutcome] = {}
        self.resolved: List[str] = []
        self.session_id: Optional[str] = None
        self.window: Optional[SessionWindow] = None
        self._expected_wake: Optional[datetime] = None
        self._last_features: Optional[datetime] = None
        self._last_mapping: Optional[datetime] = None
        self._warmed: Optional[str] = None
        self._mapped_once = False
        self._auth_announced = False
        self.last: Dict[str, Optional[datetime]] = {
            "quote": None, "bar": None, "archive": None, "feature": None}
        self.last_error = ""
        self.ticks = 0

    # ================================================================
    # Lifecycle
    # ================================================================

    def start(self) -> None:
        now = self.clock.now()
        initialize_capture_schema(self.conn)
        capture_universe.register_version(self.conn, self.definition, now)
        capture_universe.ensure_reference_rows(self.conn, self.definition)
        previous = (self.conn.execute(
            "SELECT owner, session_id, released_at FROM session_runner_leases "
            "WHERE scope = ?", (LEASE_SCOPE,)).fetchone()
            if self._table_exists("session_runner_leases") else None)
        # Raises LeaseRefused for a second supervisor: EXIT_DUPLICATE.
        self.lease = leases.acquire(self.conn, LEASE_SCOPE, self.lease_owner, now,
                                    session_id=self.instance_id,
                                    ttl_seconds=self.config.lease_ttl_seconds)
        self.conn.execute(
            "INSERT INTO capture_instances (instance_id, lease_owner, pid, host, "
            "started_at, state) VALUES (?,?,?,?,?,?)",
            (self.instance_id, self.lease_owner, os.getpid(),
             socket.gethostname(), _iso(now), self.state.value))
        self.conn.commit()
        self._event("STARTED", {"universe": self.definition.version,
                                "members": len(self.definition.members),
                                "took_over_from": self.lease.took_over_from})
        if previous and previous[2] is None and previous[1] != self.instance_id:
            # The previous process never released: it crashed, was killed
            # or is suspended. Recorded, so a restart is never mistaken for
            # a clean handover.
            self._event("LEASE_TAKEOVER", {"previous_owner": previous[0],
                                           "previous_instance": previous[1]})
        self._recover_unfinished(now)

    def _table_exists(self, name: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,)).fetchone() is not None

    def _recover_unfinished(self, now: datetime) -> None:
        """A session left open by a crash is sealed once it is over."""
        rows = self.conn.execute(
            "SELECT session_id, closes_at FROM capture_sessions "
            "WHERE status != 'finalized'").fetchall()
        for session_id, closes_at in rows:
            closes = datetime.fromisoformat(closes_at)
            if now >= closes + timedelta(minutes=self.config.post_close_minutes):
                summary = quality.finalize_session(self.conn, session_id, now)
                self._event("RECOVERED_FINALIZE", {"quality": summary["quality"]},
                            session_id)

    def run(self, max_steps: Optional[int] = None) -> str:
        """Step until stopped. Returns the exit reason."""
        self.start()
        reason = "stop requested"
        steps = 0
        try:
            while not self.stop_requested():
                delay = self.step()
                steps += 1
                if max_steps is not None and steps >= max_steps:
                    reason = "step limit"
                    break
                self._sleep(delay)
        except BaseException as error:
            reason = f"{type(error).__name__}: {error}"
            raise
        finally:
            self.shutdown(reason)
        return reason

    def _sleep(self, delay: float) -> None:
        target = self.clock.now() + timedelta(seconds=max(0.0, delay))
        self._expected_wake = target
        if getattr(self.clock, "is_wall_clock", False):
            # One-second slices, so a STOP file or a signal is honoured
            # within a second rather than after a five-minute idle wait.
            while not self.stop_requested():
                remaining = (target - self.clock.now()).total_seconds()
                if remaining <= 0:
                    break
                time.sleep(min(1.0, remaining))
        else:
            self.clock.sleep_until(target)

    def shutdown(self, reason: str) -> None:
        now = self.clock.now()
        try:
            if self.session_id and self.service.builder is not None:
                # Complete minutes are kept; the minute in progress is
                # written as incomplete and therefore never archived.
                bars = self.service.builder.flush(now, force_incomplete=True)
                if bars:
                    self.service.repository.write_bars(bars, now)
                self._archive(now)
        except Exception as error:                        # noqa: BLE001
            LOG.warning("shutdown flush failed: %s", error)
        self.state = CaptureState.STOPPED
        try:
            self._heartbeat(now)
            self.conn.execute(
                "UPDATE capture_instances SET ended_at = ?, exit_reason = ? "
                "WHERE instance_id = ?", (_iso(now), reason[:500], self.instance_id))
            self.conn.commit()
            self._event("STOPPED", {"reason": reason[:500]})
            if self.lease is not None:
                leases.release(self.conn, self.lease, now)
        except sqlite3.Error as error:
            LOG.warning("shutdown bookkeeping failed: %s", error)

    # ================================================================
    # One step
    # ================================================================

    def step(self) -> float:
        """Advance the lifecycle once. Returns seconds until the next step."""
        now = self.clock.now()
        try:
            self._detect_host_gap(now)
            self._keep_lease(now)
        except sqlite3.OperationalError as error:
            if "locked" not in str(error).lower():
                raise
            self.last_error = f"database locked: {error}"
            self.conn.rollback()
            return self.config.locked_retry_seconds
        self._check_safety()

        window = self.calendar.session(now.astimezone(NEW_YORK).date())
        pre_open = post_close = None
        if window.is_trading_day:
            pre_open = window.opens_at - timedelta(minutes=self.config.pre_open_minutes)
            post_close = window.closes_at + timedelta(minutes=self.config.post_close_minutes)

        try:
            if not window.is_trading_day or now < pre_open or now >= post_close:
                delay = self._off_hours(now)
            elif now < window.opens_at:
                delay = self._pre_open(now, window)
            elif now < window.closes_at:
                delay = self._active(now, window)
            else:
                delay = self._closing(now, window)
        except (CaptureConfigError, CaptureSafetyViolation, leases.LeaseRefused):
            raise
        except sqlite3.OperationalError as error:
            if "locked" not in str(error).lower():
                raise
            # Section 57: another process held the write lock past
            # busy_timeout. Nothing is written about it (that would need
            # the same lock); the next step retries and the archive, which
            # always covers the whole session window, catches up.
            self.last_error = f"database locked: {error}"
            LOG.warning("capture store locked; retrying in %.0fs",
                        self.config.locked_retry_seconds)
            try:
                self.conn.rollback()
            except sqlite3.Error:
                pass
            return self.config.locked_retry_seconds
        except sqlite3.Error:
            raise
        except Exception as error:                        # noqa: BLE001
            # A market-data failure degrades a tick; it must not end the
            # process that holds the session together.
            self.last_error = f"{type(error).__name__}: {error}"
            self._event("STEP_ERROR", {"error": self.last_error[:500]})
            LOG.exception("capture step failed")
            delay = self.config.interval_seconds
        self._check_safety()
        try:
            self._heartbeat(now)
        except sqlite3.OperationalError as error:
            if "locked" not in str(error).lower():
                raise
            self.conn.rollback()
        return delay

    # ---------------- phases ----------------

    def _off_hours(self, now: datetime) -> float:
        if self.session_id is not None:
            self._finalize(now)
        self._set_state(CaptureState.IDLE)
        upcoming = self._next_pre_open(now)
        if upcoming is None:
            return self.config.idle_poll_seconds
        return max(1.0, min(self.config.idle_poll_seconds,
                            (upcoming - now).total_seconds()))

    def _pre_open(self, now: datetime, window: SessionWindow) -> float:
        if self.state not in (CaptureState.WAITING_FOR_MARKET,
                              CaptureState.WAITING_FOR_AUTH):
            self._set_state(CaptureState.PREFLIGHT)
        if not self._ensure_auth(now):
            return self.config.auth_retry_seconds
        self._open_session(now, window)
        self._set_state(CaptureState.WAITING_FOR_MARKET)
        warm_at = window.opens_at - timedelta(minutes=self.config.warmup_minutes)
        if now >= warm_at and self._warmed != self.session_id:
            self._warm_up(now)
        until_open = (window.opens_at - now).total_seconds()
        if now < warm_at:
            until_open = min(until_open, (warm_at - now).total_seconds())
        return max(1.0, min(self.config.interval_seconds, until_open))

    def _active(self, now: datetime, window: SessionWindow) -> float:
        if not self._ensure_auth(now):
            if self.session_id is None:
                self._open_session(now, window, mapped=False)
            return self.config.auth_retry_seconds
        self._open_session(now, window)
        self._set_state(CaptureState.ACTIVE_SESSION)
        self._retry_mapping(now)
        if not self.resolved:
            # Preflight failed on mappings: no active capture (section 65).
            # The mapping retry above keeps asking on its own schedule.
            if self.last_error != "no resolved universe member":
                self.last_error = "no resolved universe member"
                self._event("CAPTURE_BLOCKED", {"reason": self.last_error},
                            self.session_id)
            return self.config.interval_seconds
        self._tick(now, window)
        return self._until_next_tick(now)

    def _closing(self, now: datetime, window: SessionWindow) -> float:
        if self.session_id is not None:
            self._set_state(CaptureState.CLOSING)
            self._finalize(now)
        self._set_state(CaptureState.POST_SESSION)
        after = window.closes_at + timedelta(minutes=self.config.post_close_minutes)
        return max(1.0, min(self.config.idle_poll_seconds,
                            (after - now).total_seconds()))

    # ---------------- authentication ----------------

    def _ensure_auth(self, now: datetime) -> bool:
        if self.connected:
            self.budget.spend(now)
            if self.gateway.heartbeat():
                return True
            self.connected = False
            self._event("AUTH_LOST", {"detail": "keepalive refused"})
        self.budget.spend(now, 2)
        state = self.gateway.connect()
        value = getattr(state, "value", str(state))
        self.auth_state = value
        if value == "connected":
            self.connected = True
            self._auth_announced = False
            self._event("AUTHENTICATED", {})
            return True
        if value == "disconnected" and not getattr(self.gateway.config, "enabled", True):
            raise CaptureConfigError(
                "IBKR is disabled in configuration; capture cannot connect")
        if not self._auth_announced:
            self._auth_announced = True
            self._event("WAITING_FOR_AUTH", {
                "gateway_state": value,
                "action": "log in to the IBKR Client Portal Gateway in a browser; "
                          "capture resumes by itself"})
            LOG.warning("IBKR gateway is not authenticated (%s). Log in to the "
                        "Client Portal Gateway in a browser; capture resumes "
                        "automatically.", value)
        self._set_state(CaptureState.WAITING_FOR_AUTH)
        return False

    # ---------------- sessions ----------------

    def _open_session(self, now: datetime, window: SessionWindow,
                      mapped: bool = True) -> None:
        session_id = session_id_for(window)
        if self.session_id == session_id and (self.outcomes or not mapped):
            return
        if self.session_id not in (None, session_id):
            self._finalize(now)
        existing = self.conn.execute(
            "SELECT status FROM capture_sessions WHERE session_id = ?",
            (session_id,)).fetchone()
        if existing is None:
            self.conn.execute(
                "INSERT INTO capture_sessions (session_id, session_date, "
                "session_type, opens_at, closes_at, universe_version, status, "
                "created_at) VALUES (?,?,?,?,?,?,'open',?)",
                (session_id, window.day.isoformat(),
                 "early_close" if window.reason.startswith("early") else "regular",
                 _iso(window.opens_at), _iso(window.closes_at),
                 self.definition.version, _iso(now)))
            self.conn.commit()
            self._event("SESSION_OPENED", {"reason": window.reason}, session_id)
        elif self.session_id is None:
            self._event("SESSION_RESUMED", {"instance": self.instance_id}, session_id)
        self.session_id = session_id
        self.window = window
        if self.service.builder is None or self.service.builder.session_id != session_id:
            self.service.builder = MinuteBarBuilder(session_id=session_id)
        if mapped:
            self._map(now)
        elif existing is None:
            # Unauthenticated at the open: the session still records who
            # was EXPECTED, so its coverage is judged against them.
            capture_universe.snapshot_membership(self.conn, session_id,
                                                 self.definition, {})

    def _map(self, now: datetime) -> None:
        from src.data_access.execution_repository import ExecutionRepository
        self.outcomes = capture_universe.map_universe(
            self.conn, self.gateway, ExecutionRepository(self.conn),
            self.definition, now, self.budget)
        self.resolved = capture_universe.resolved_ids(self.outcomes)
        self._last_mapping = now
        capture_universe.snapshot_membership(self.conn, self.session_id,
                                             self.definition, self.outcomes)
        summary = capture_universe.mapping_summary(self.outcomes)
        summary["requests"] = sum(1 for o in self.outcomes.values() if o.requested)
        checks = self.preflight(now)
        if summary["requests"] or not self._mapped_once:
            self._event("MAPPED", summary, self.session_id)
            self._event("PREFLIGHT", checks, self.session_id)
        self._mapped_once = True

    def preflight(self, now: datetime) -> Dict[str, Any]:
        """Section 65: every precondition for active capture, recorded."""
        entries = md_universe.resolve_universe(self.conn, instruments=self.resolved)
        fit = self.service.capacity(entries)
        checks = {
            "db_accessible": True,
            "schema_current": self._table_exists("capture_sessions"),
            "lease_held": self.lease is not None,
            "gateway_authenticated": self.connected,
            "universe_members": len(self.definition.members),
            "resolved_members": len(self.resolved),
            "request_budget_fits": bool(fit["fits"]),
            "requests_per_minute": fit["requests_per_minute"],
            "budget_per_minute": fit["budget_per_minute"],
            "capture_only_gateway": bool(getattr(self.gateway, "submission_forbidden",
                                                 False)),
            "capture_only_transport": bool(getattr(self.gateway.transport,
                                                   "submission_forbidden", False)),
            "ordering_enabled": bool(getattr(self.gateway.config,
                                             "ordering_enabled", False)),
        }
        checks["ok"] = bool(checks["schema_current"] and checks["lease_held"]
                            and checks["gateway_authenticated"]
                            and checks["resolved_members"] > 0
                            and checks["request_budget_fits"]
                            and checks["capture_only_gateway"]
                            and checks["capture_only_transport"]
                            and not checks["ordering_enabled"])
        if not checks["request_budget_fits"]:
            raise CaptureConfigError(
                f"universe needs {fit['requests_per_minute']} req/min against a "
                f"budget of {fit['budget_per_minute']}; the limit is not raised")
        return checks

    def _retry_mapping(self, now: datetime) -> None:
        # AMBIGUOUS waits for a human; only a retryable status is retried.
        retryable = (capture_universe.MappingStatus.FAILED,
                     capture_universe.MappingStatus.UNSUPPORTED)
        if not any(o.status in retryable for o in self.outcomes.values()):
            return
        if self._last_mapping and now - self._last_mapping < timedelta(
                minutes=self.config.mapping_retry_minutes):
            return
        self._map(now)

    def _warm_up(self, now: datetime) -> None:
        """
        One snapshot before the open, DISCARDED.

        IBKR answers a conid's first snapshot with no fields. Without this
        the first regular-hours minute of every instrument would be lost
        to subscription start-up. Nothing from it is written: a
        pre-market price is not a session bar.
        """
        entries = md_universe.active(md_universe.resolve_universe(
            self.conn, instruments=self.resolved))
        if entries:
            self.budget.spend(now)
            quote_acquisition.acquire(self.gateway.transport, entries, now)
        self._warmed = self.session_id
        self._event("WARMED_UP", {"instruments": len(entries)}, self.session_id)

    def _finalize(self, now: datetime) -> None:
        session_id = self.session_id
        if session_id is None:
            return
        if self.service.builder is not None:
            bars = self.service.builder.flush(now, force_incomplete=True)
            if bars:
                self.service.repository.write_bars(bars, now)
        self._archive(now)
        summary = quality.finalize_session(self.conn, session_id, now)
        pruned = quality.prune_archived(self.conn, now, self.config.bar_retention_days)
        self._event("SESSION_FINALIZED", {"quality": summary["quality"],
                                          "cross_sectional_minutes":
                                              summary["cross_sectional_minutes"],
                                          "bars_archived": summary["bars_archived"],
                                          "pruned": pruned}, session_id)
        LOG.info("session %s finalized: %s", session_id, summary["quality"])
        self.session_id = None
        self.window = None
        self.outcomes = {}
        self.resolved = []
        self.service.builder = None

    # ---------------- the tick ----------------

    def _tick(self, now: datetime, window: SessionWindow) -> None:
        started = time.monotonic()
        requested_at = self.clock.now()
        self.budget.spend(now)
        cycle = self.service.run_cycle(now=now, instruments=self.resolved)
        received_at = self.clock.now()
        if cycle.tradeable:
            self.last["quote"] = now
        if cycle.bars_written:
            self.last["bar"] = now
        health = cycle.health.value
        try:
            archived = self._archive(now)
        except sqlite3.DatabaseError as error:
            if "locked" in str(error).lower():
                raise
            # Section 98: the operational bars stay where they are; the next
            # tick archives the whole session window again, idempotently,
            # which is the bounded retry.
            self.conn.rollback()
            archived = -1
            health = "ARCHIVE_FAILED"
            self.last_error = f"archive: {error}"
            self._event("ARCHIVE_FAILED", {"error": str(error)[:300]}, self.session_id)
        feature_count = 0
        boundary = minute_floor(now)
        due = (self._last_features is None or boundary - self._last_features
               >= timedelta(minutes=self.config.feature_every_minutes))
        if due and boundary > window.opens_at and self.resolved:
            self._last_features = boundary
            try:
                result = capture_features.compute_and_persist(
                    self.conn, self.resolved, boundary, self.session_id, now,
                    calendar=self.calendar)
                feature_count = result["values"]
                self.last["feature"] = now
            except Exception as error:                    # noqa: BLE001
                # Sections 40 and 99: a feature failure costs features,
                # never bars. The cutoff is recorded so that
                # recompute_session repairs it later.
                self.conn.rollback()
                feature_count = -1
                health = "FEATURE_FAILED"
                self.last_error = f"features: {type(error).__name__}: {error}"
                self._event("FEATURE_FAILED", {"cutoff": _iso(boundary),
                                               "error": str(error)[:300]},
                            self.session_id)
        self.conn.execute(
            "INSERT OR REPLACE INTO capture_ticks (session_id, tick_at, "
            "instance_id, requested_at, received_at, target_minute, requested, "
            "tradeable, bars_written, archived, features, duration_seconds, "
            "health) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.session_id, _iso(now), self.instance_id, _iso(requested_at),
             _iso(received_at), _iso(boundary - timedelta(minutes=1)),
             cycle.requested, cycle.tradeable, cycle.bars_written, archived,
             feature_count, round(time.monotonic() - started, 3), health))
        self.conn.commit()
        self.ticks += 1

    def _archive(self, now: datetime) -> int:
        if self.session_id is None or self.window is None:
            return 0
        result = archive_operational_bars(
            self.conn, source_label=self.config.source_label,
            since=self.window.opens_at, until=min(now, self.window.closes_at),
            instruments=self.resolved or None)
        stamp = _iso(now)
        for instrument_id, bar_start in result["archived"]:
            self.conn.execute(
                "INSERT OR IGNORE INTO capture_archive_log (instrument_id, "
                "bar_start, session_id, instance_id, archive_version, "
                "archived_at) VALUES (?,?,?,?,?,?)",
                (instrument_id, bar_start, self.session_id, self.instance_id,
                 ARCHIVE_VERSION, stamp))
        self.conn.commit()
        if result["written"]:
            self.last["archive"] = now
        return int(result["written"])

    def _until_next_tick(self, now: datetime) -> float:
        target = minute_floor(now) + timedelta(
            seconds=self.config.interval_seconds + self.config.tick_offset_seconds)
        return max(1.0, (target - self.clock.now()).total_seconds())

    # ---------------- guards ----------------

    def _detect_host_gap(self, now: datetime) -> None:
        expected = self._expected_wake
        if expected is None:
            return
        late = (now - expected).total_seconds()
        if late > self.config.host_gap_seconds:
            detail = {"expected": _iso(expected), "woke": _iso(now),
                      "seconds": round(late, 1)}
            self._event("HOST_SUSPEND_GAP", detail, self.session_id)
            LOG.warning("host did not run for %.0fs (expected wake %s)",
                        late, _iso(expected))
            # A lost minute cannot be recovered; a stale builder would
            # otherwise emit hundreds of gap markers in one tick.
            if self.service.builder is not None:
                bars = self.service.builder.flush(now, force_incomplete=True)
                if bars:
                    self.service.repository.write_bars(bars, now)
            # The gateway session may have lapsed while we slept.
            self.connected = False

    def _keep_lease(self, now: datetime) -> None:
        if self.lease is None:
            return
        if leases.renew(self.conn, self.lease, now,
                        ttl_seconds=self.config.lease_ttl_seconds):
            holder_instance = self.conn.execute(
                "SELECT session_id FROM session_runner_leases WHERE scope = ?",
                (LEASE_SCOPE,)).fetchone()
            if holder_instance and holder_instance[0] != self.instance_id:
                # Same supervisor identity, newer process: this one is the
                # stale instance (woken from a suspend, or left behind).
                self.lease = None
                raise leases.LeaseRefused(
                    f"capture lease was taken over by instance "
                    f"{holder_instance[0]}; this instance stops")
            return
        # Expired while suspended, or taken. Same owner re-acquires;
        # a different live owner raises LeaseRefused.
        self.lease = leases.acquire(self.conn, LEASE_SCOPE, self.lease_owner, now,
                                    session_id=self.instance_id,
                                    ttl_seconds=self.config.lease_ttl_seconds)
        self._event("LEASE_REACQUIRED", {"took_over_from": self.lease.took_over_from})

    def _check_safety(self) -> None:
        attempts = broker_write_attempts(self.gateway)
        if attempts:
            self._event("BROKER_WRITE_REFUSED", {"attempts": attempts})
            raise CaptureSafetyViolation(
                f"venue write attempted and refused: {attempts}")

    # ---------------- bookkeeping ----------------

    def _next_pre_open(self, now: datetime) -> Optional[datetime]:
        day = now.astimezone(NEW_YORK).date()
        for _ in range(15):
            window = self.calendar.session(day)
            if window.is_trading_day:
                start = window.opens_at - timedelta(minutes=self.config.pre_open_minutes)
                if start > now:
                    return start
            day += timedelta(days=1)
        return None

    def _set_state(self, state: CaptureState) -> None:
        if state is not self.state:
            LOG.info("state %s -> %s", self.state.value, state.value)
            self.state = state

    def _event(self, kind: str, detail: Dict[str, Any],
               session_id: Optional[str] = None) -> None:
        self.conn.execute(
            "INSERT INTO capture_events (at, instance_id, session_id, kind, detail) "
            "VALUES (?,?,?,?,?)",
            (_iso(self.clock.now()), self.instance_id, session_id, kind,
             json.dumps(detail, sort_keys=True, default=str)))
        self.conn.commit()

    def _heartbeat(self, now: datetime) -> None:
        self.conn.execute(
            "UPDATE capture_instances SET state = ?, auth_state = ?, "
            "session_id = ?, heartbeat_at = ?, last_quote_at = ?, last_bar_at = ?, "
            "last_archive_at = ?, last_feature_at = ?, last_error = ?, "
            "broker_write_attempts = ? WHERE instance_id = ?",
            (self.state.value, self.auth_state, self.session_id, _iso(now),
             _iso(self.last["quote"]), _iso(self.last["bar"]),
             _iso(self.last["archive"]), _iso(self.last["feature"]),
             self.last_error[:500], len(broker_write_attempts(self.gateway)),
             self.instance_id))
        self.conn.commit()
