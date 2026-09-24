"""
tests/capture/test_capture_25_9g.py
-----------------------------------------
Phase 25.9G -- continuous capture: lifecycle, safety, integrity.

Every scenario drives the PRODUCTION runner through `step()` and
`_sleep()` exactly as `run()` does, against a mock venue and a replay
clock. Failure injection is done at the venue or the clock -- never by
patching the code under test.
"""

import json
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.capture import quality
from src.capture.features import bounded_builder, compute_and_persist, recompute_session
from src.capture.runner import (
    CaptureConfig, CaptureConfigError, CaptureRunner, CaptureSafetyViolation,
    CaptureState, LEASE_SCOPE,
)
from src.capture.schema import CAPTURE_WRITABLE
from src.capture.universe import (
    MappingStatus, RequestBudget, UniverseVersionConflict, load_definition,
    register_version,
)
from src.execution.adapters.submission_guard import BrokerSubmissionForbidden
from src.marketdata.intraday import LIVE_CAPTURE_SOURCE
from src.research.intraday_dataset import IntradayDatasetBuilder
from src.trading import leases
from src.trading.clock import ReplayClock
from tests.capture.harness import (
    CLOSE, OPEN, MovingVenue, TICKERS, capture_db, make_runner, run_until,
    write_universe,
)

UTC = timezone.utc
FAST = CaptureConfig(feature_every_minutes=100000)


def at(hour, minute=0, day=18, month=9):
    return datetime(2026, month, day, hour, minute, tzinfo=UTC)


def table_counts(conn):
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    return {n: conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0]
            for n in names}


class _Case(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.conn = capture_db()

    def runner(self, start, **kwargs):
        clock = ReplayClock(start)
        kwargs.setdefault("config", FAST)
        runner, venue = make_runner(self.conn, clock, self.dir, **kwargs)
        runner.start()
        return runner, venue, clock

    def session(self, session_id="cap-2026-09-18"):
        row = self.conn.execute(
            "SELECT status, quality, summary_json FROM capture_sessions "
            "WHERE session_id = ?", (session_id,)).fetchone()
        return row[0], row[1], json.loads(row[2] or "{}")

    def events(self, kind):
        return [json.loads(r[0]) for r in self.conn.execute(
            "SELECT detail FROM capture_events WHERE kind = ? ORDER BY event_id",
            (kind,))]


# ======================================================================
# A whole session, end to end
# ======================================================================

class TestFullSession(unittest.TestCase):
    """One real-length session, shared by the assertions below."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.conn = capture_db()
        cls.before = table_counts(cls.conn)
        cls.clock = ReplayClock(at(12, 0))
        cls.runner, cls.venue = make_runner(
            cls.conn, cls.clock, cls.dir,
            config=CaptureConfig(feature_every_minutes=30))
        cls.runner.start()
        run_until(cls.runner, cls.clock, at(21, 0))
        cls.runner.shutdown("test complete")
        status, cls.quality, cls.summary = cls.conn.execute(
            "SELECT status, quality, summary_json FROM capture_sessions").fetchone()
        cls.summary = json.loads(cls.summary)
        cls.status = status

    def test_session_is_finalized_good_with_every_minute(self):
        self.assertEqual(self.status, "finalized")
        self.assertEqual(self.quality, "GOOD")
        self.assertEqual(self.summary["expected_minutes"], 390)
        self.assertEqual(self.summary["cross_sectional_minutes"], 390)
        self.assertEqual(self.summary["bars_archived"], 390 * len(TICKERS))

    def test_archive_is_labelled_and_proven(self):
        sources = dict(self.conn.execute(
            "SELECT source, COUNT(*) FROM price_candle_cache GROUP BY source"))
        self.assertEqual(sources, {LIVE_CAPTURE_SOURCE: 390 * len(TICKERS)})
        logged = self.conn.execute(
            "SELECT COUNT(*) FROM capture_archive_log").fetchone()[0]
        self.assertEqual(logged, 390 * len(TICKERS))

    def test_no_bar_outside_regular_hours(self):
        outside = self.conn.execute(
            "SELECT COUNT(*) FROM price_candle_cache WHERE timestamp < ? "
            "OR timestamp >= ?", (OPEN.isoformat(), CLOSE.isoformat())).fetchone()[0]
        self.assertEqual(outside, 0)

    def test_one_snapshot_per_minute_plus_warm_up(self):
        self.assertEqual(self.venue.snapshot_calls, 390 + 1)
        ticks = self.conn.execute("SELECT COUNT(*) FROM capture_ticks").fetchone()[0]
        self.assertEqual(ticks, 390)

    def test_features_persisted_at_closed_boundaries(self):
        rows = self.conn.execute(
            "SELECT DISTINCT cutoff FROM intraday_feature_values").fetchall()
        self.assertGreater(len(rows), 10)
        for (cutoff,) in rows:
            stamp = datetime.fromisoformat(cutoff)
            self.assertEqual(stamp.second, 0)
            self.assertTrue(OPEN < stamp <= CLOSE)

    def test_no_venue_write_of_any_kind(self):
        self.assertEqual((self.venue.place_calls, self.venue.cancel_calls), (0, 0))
        self.assertEqual(self.conn.execute(
            "SELECT MAX(broker_write_attempts) FROM capture_instances").fetchone()[0], 0)

    def test_write_boundary(self):
        after = table_counts(self.conn)
        changed = {t for t in after if after[t] != self.before.get(t, 0)}
        self.assertTrue(changed, "capture wrote nothing at all")
        self.assertEqual(changed - CAPTURE_WRITABLE, set())

    def test_lease_released_and_instance_closed(self):
        self.assertIsNone(leases.holder(self.conn, LEASE_SCOPE, at(21, 1)))
        row = self.conn.execute(
            "SELECT ended_at, exit_reason, state FROM capture_instances").fetchone()
        self.assertIsNotNone(row[0])
        self.assertEqual(row[1], "test complete")
        self.assertEqual(row[2], "STOPPED")

    def test_idle_after_close(self):
        self.assertIs(self.runner.state, CaptureState.STOPPED)
        states = [r[0] for r in self.conn.execute(
            "SELECT kind FROM capture_events ORDER BY event_id")]
        self.assertLess(states.index("SESSION_OPENED"), states.index("SESSION_FINALIZED"))


# ======================================================================
# Calendar
# ======================================================================

class TestCalendar(_Case):

    def test_weekend_is_idle_and_silent(self):
        runner, venue, clock = self.runner(at(15, 0, day=19))      # Saturday
        run_until(runner, clock, at(15, 0, day=20))
        self.assertIs(runner.state, CaptureState.IDLE)
        self.assertEqual(venue.snapshot_calls, 0)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM capture_sessions").fetchone()[0], 0)

    def test_idle_sleep_is_bounded_so_heartbeat_stays_fresh(self):
        runner, venue, clock = self.runner(at(15, 0, day=19))
        delay = runner.step()
        self.assertLessEqual(delay, FAST.idle_poll_seconds)

    def test_holiday_opens_no_session(self):
        runner, venue, clock = self.runner(at(12, 0, day=26, month=11))  # Thanksgiving
        run_until(runner, clock, at(21, 0, day=26, month=11))
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM capture_sessions").fetchone()[0], 0)
        self.assertEqual(venue.snapshot_calls, 0)

    def test_early_close_ends_at_the_early_bell(self):
        runner, venue, clock = self.runner(at(13, 0, day=27, month=11))
        run_until(runner, clock, at(19, 0, day=27, month=11))
        status, grade, summary = self.session("cap-2026-11-27")
        self.assertEqual(status, "finalized")
        self.assertEqual(summary["expected_minutes"], 210)
        self.assertEqual(grade, "GOOD")
        late = self.conn.execute(
            "SELECT COUNT(*) FROM price_candle_cache WHERE timestamp >= ?",
            (at(18, 0, day=27, month=11).isoformat(),)).fetchone()[0]
        self.assertEqual(late, 0)

    def test_pre_open_prepares_before_the_first_minute(self):
        runner, venue, clock = self.runner(at(13, 0))
        run_until(runner, clock, at(13, 29))
        self.assertIs(runner.state, CaptureState.WAITING_FOR_MARKET)
        self.assertEqual(len(runner.resolved), len(TICKERS))
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM market_data_bars").fetchone()[0], 0,
            "a pre-market price must never become a bar")


# ======================================================================
# Authentication is a human's job
# ======================================================================

class TestAuthentication(_Case):

    def test_waits_for_auth_then_resumes_by_itself(self):
        runner, venue, clock = self.runner(at(13, 0))
        venue.authenticated = False
        run_until(runner, clock, at(15, 0))
        self.assertIs(runner.state, CaptureState.WAITING_FOR_AUTH)
        self.assertEqual(venue.snapshot_calls, 0)
        self.assertEqual(len(self.events("WAITING_FOR_AUTH")), 1,
                         "the wait is announced once, not every retry")
        venue.authenticated = True
        run_until(runner, clock, at(21, 0))
        status, grade, summary = self.session()
        self.assertEqual(status, "finalized")
        self.assertEqual(grade, "PARTIAL")      # 90 minutes were not observed
        self.assertLessEqual(summary["window_minutes"], 390 - 90)

    def test_never_authenticated_session_is_failed_not_invented(self):
        runner, venue, clock = self.runner(at(13, 0))
        venue.authenticated = False
        run_until(runner, clock, at(21, 0))
        status, grade, summary = self.session()
        self.assertEqual(grade, "FAILED")
        self.assertEqual(summary["members"], len(TICKERS))
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM price_candle_cache").fetchone()[0], 0)

    def test_disabled_ibkr_is_a_configuration_error(self):
        runner, venue, clock = self.runner(at(13, 45))
        runner.gateway.config.enabled = False
        with self.assertRaises(CaptureConfigError):
            runner.step()

    def test_no_credential_handling_anywhere_in_capture(self):
        root = os.path.join(os.path.dirname(__file__), "..", "..")
        files = [os.path.join(root, "src", "capture", f)
                 for f in os.listdir(os.path.join(root, "src", "capture"))
                 if f.endswith(".py")]
        files += [os.path.join(root, "scripts", f) for f in
                  ("run_capture.py", "capture_supervisor.py", "capture_status.py")]
        files.append(os.path.join(root, "deploy", "windows", "capture_task.ps1"))
        pattern = re.compile(r"(getpass|password\s*=|passwd|send_keys|"
                             r"-Password|ConvertTo-SecureString|/sso/Login)",
                             re.IGNORECASE)
        for path in files:
            with open(path, encoding="utf-8") as handle:
                self.assertIsNone(pattern.search(handle.read()), path)


# ======================================================================
# Host sleep and restarts
# ======================================================================

class TestHostSuspend(_Case):

    def test_gap_is_recorded_and_never_filled(self):
        runner, venue, clock = self.runner(at(13, 0))
        run_until(runner, clock, at(15, 0))
        clock.advance(45 * 60)                  # the laptop lid
        run_until(runner, clock, at(21, 0))
        gaps = self.events("HOST_SUSPEND_GAP")
        self.assertEqual(len(gaps), 1)
        self.assertGreater(gaps[0]["seconds"], 40 * 60)
        missing = self.conn.execute(
            "SELECT COUNT(*) FROM price_candle_cache WHERE timestamp >= ? "
            "AND timestamp < ?", (at(15, 2).isoformat(), at(15, 44).isoformat())
        ).fetchone()[0]
        self.assertEqual(missing, 0, "no synthetic bar may cover the gap")
        status, grade, summary = self.session()
        self.assertEqual(summary["host_suspend_gaps"], 1)
        self.assertEqual(grade, "PARTIAL")

    def test_gap_markers_stay_operational(self):
        runner, venue, clock = self.runner(at(13, 0))
        run_until(runner, clock, at(14, 0))
        clock.advance(10 * 60)
        run_until(runner, clock, at(14, 30))
        archived_gaps = self.conn.execute("""
            SELECT COUNT(*) FROM market_data_bars b JOIN price_candle_cache p
              ON p.instrument_id = b.instrument_id AND p.timestamp = b.bar_start
             WHERE b.is_gap = 1 OR b.is_complete = 0""").fetchone()[0]
        self.assertEqual(archived_gaps, 0)


class TestRestartAndLease(_Case):

    def test_supervised_restart_resumes_without_duplicates(self):
        runner, venue, clock = self.runner(at(13, 0))
        run_until(runner, clock, at(15, 0))
        runner.shutdown("crash rehearsal")
        second, _ = make_runner(self.conn, clock, self.dir, venue=venue,
                                config=FAST)
        second.start()                          # same supervisor identity
        run_until(second, clock, at(21, 0))
        self.assertEqual(len(self.events("SESSION_RESUMED")), 1)
        dupes = self.conn.execute(
            "SELECT COUNT(*) FROM (SELECT instrument_id, timestamp, COUNT(*) c "
            "FROM price_candle_cache GROUP BY 1, 2 HAVING c > 1)").fetchone()[0]
        self.assertEqual(dupes, 0)
        status, grade, summary = self.session()
        self.assertEqual(summary["processes"], 2)
        self.assertIn(grade, ("GOOD", "PARTIAL"))
        self.assertGreaterEqual(summary["cross_sectional_minutes"], 385)

    def test_crash_without_shutdown_is_taken_over_by_same_owner(self):
        runner, venue, clock = self.runner(at(14, 0))
        run_until(runner, clock, at(14, 10))
        # no shutdown: the lease is still live
        second, _ = make_runner(self.conn, clock, self.dir, venue=venue, config=FAST)
        second.start()
        self.assertEqual(leases.holder(self.conn, LEASE_SCOPE, clock.now()),
                         "supervisor-test")

    def test_second_supervisor_is_refused(self):
        runner, venue, clock = self.runner(at(14, 0))
        run_until(runner, clock, at(14, 5))
        other, _ = make_runner(self.conn, clock, self.dir, venue=venue,
                               owner="supervisor-other", config=FAST)
        with self.assertRaises(leases.LeaseRefused):
            other.start()

    def test_unfinished_session_is_sealed_on_next_start(self):
        runner, venue, clock = self.runner(at(14, 0))
        run_until(runner, clock, at(15, 30))    # 90 of 390 minutes, then a crash
        clock.set(at(13, 0, day=21))            # back on Monday
        second, _ = make_runner(self.conn, clock, self.dir, venue=venue, config=FAST)
        second.start()
        status, grade, summary = self.session()
        self.assertEqual(status, "finalized")
        self.assertEqual(grade, "PARTIAL")

    def test_archive_is_idempotent(self):
        runner, venue, clock = self.runner(at(14, 0))
        run_until(runner, clock, at(14, 30))
        before = self.conn.execute("SELECT COUNT(*) FROM price_candle_cache").fetchone()[0]
        self.assertEqual(runner._archive(clock.now()), 0)
        after = self.conn.execute("SELECT COUNT(*) FROM price_candle_cache").fetchone()[0]
        self.assertEqual(before, after)


# ======================================================================
# Universe and contracts
# ======================================================================

class TestUniverseAndMapping(_Case):

    def test_classification_covers_every_outcome(self):
        tickers = ("SPY", "AMZN", "AAPL", "ZZZZ")
        clock = ReplayClock(at(13, 0))
        venue = MovingVenue(clock, ("SPY", "AMZN", "AAPL"))   # AAPL doubled
        runner, _ = make_runner(self.conn, clock, self.dir, tickers=tickers,
                                venue=venue, config=FAST)
        runner.start()
        run_until(runner, clock, at(13, 20))
        status = {i: o.status for i, o in runner.outcomes.items()}
        self.assertEqual(status["benchmark-spy"], MappingStatus.RESOLVED)
        self.assertEqual(status["us_and_intl-aapl"], MappingStatus.AMBIGUOUS)
        self.assertEqual(status["us_and_intl-zzzz"], MappingStatus.UNSUPPORTED)
        members = dict(self.conn.execute(
            "SELECT instrument_id, mapping_status FROM capture_session_members"))
        self.assertEqual(members["us_and_intl-aapl"], "AMBIGUOUS")

    def test_ambiguous_is_not_retried(self):
        clock = ReplayClock(at(13, 0))
        venue = MovingVenue(clock, ("SPY", "AAPL"))
        runner, _ = make_runner(self.conn, clock, self.dir, tickers=("SPY", "AAPL"),
                                venue=venue, config=FAST)
        runner.start()
        run_until(runner, clock, at(16, 0))
        attempts = self.conn.execute(
            "SELECT attempts FROM capture_mappings WHERE instrument_id = "
            "'us_and_intl-aapl'").fetchone()[0]
        self.assertEqual(attempts, 1)

    def test_transient_failure_backs_off_then_resolves(self):
        clock = ReplayClock(at(13, 0))
        venue = MovingVenue(clock)
        venue.rate_limited = True
        runner, _ = make_runner(self.conn, clock, self.dir, venue=venue, config=FAST)
        runner.start()
        runner.connected = True                 # auth path is not the subject
        from src.data_access.execution_repository import ExecutionRepository
        from src.capture.universe import map_universe
        first = map_universe(self.conn, runner.gateway, ExecutionRepository(self.conn),
                             runner.definition, clock.now())
        self.assertTrue(all(o.status is MappingStatus.FAILED for o in first.values()))
        retry = self.conn.execute(
            "SELECT next_retry_at FROM capture_mappings LIMIT 1").fetchone()[0]
        self.assertGreater(datetime.fromisoformat(retry), clock.now())
        venue.rate_limited = False
        again = map_universe(self.conn, runner.gateway, ExecutionRepository(self.conn),
                             runner.definition, clock.now())
        self.assertTrue(all(o.status is MappingStatus.FAILED for o in again.values()),
                        "a retry before its time is not attempted")
        clock.advance(3600)
        later = map_universe(self.conn, runner.gateway, ExecutionRepository(self.conn),
                             runner.definition, clock.now())
        self.assertTrue(all(o.status is MappingStatus.RESOLVED for o in later.values()))

    def test_budget_reserves_room_for_market_data(self):
        budget = RequestBudget(per_minute=5, reserved=2)
        now = at(14, 0)
        allowed = 0
        for _ in range(10):
            if budget.allow_discretionary(now):
                budget.spend(now)
                allowed += 1
        self.assertEqual(allowed, 3)
        self.assertTrue(budget.allow_discretionary(now + timedelta(seconds=61)))

    def test_universe_version_is_immutable(self):
        path = write_universe(self.dir)
        definition = load_definition(path)
        self.assertTrue(register_version(self.conn, definition, at(12, 0)))
        self.assertFalse(register_version(self.conn, definition, at(12, 0)))
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(" ")
        with self.assertRaises(UniverseVersionConflict):
            register_version(self.conn, load_definition(path), at(12, 0))

    def test_committed_universe_is_valid_and_bounded(self):
        root = os.path.join(os.path.dirname(__file__), "..", "..")
        definition = load_definition(os.path.join(root, "config",
                                                  "capture_universe_v1.json"))
        self.assertEqual(definition.version, "v1")
        self.assertLessEqual(len(definition.members), 50)
        self.assertIn("benchmark-spy", definition.instrument_ids)
        allowed = {"instrument_id", "ticker", "sector_id", "asset_class",
                   "sec_type", "currency", "role", "daily_candles_at_build"}
        for item in definition.raw["instruments"]:
            self.assertEqual(set(item) - allowed, set(),
                             "a member may carry no outcome-derived field")

    def test_membership_is_frozen_after_finalize(self):
        runner, venue, clock = self.runner(at(13, 0))
        run_until(runner, clock, at(21, 0))
        before = self.conn.execute(
            "SELECT * FROM capture_session_members ORDER BY instrument_id").fetchall()
        from src.capture.universe import snapshot_membership
        snapshot_membership(self.conn, "cap-2026-09-18", runner.definition, {})
        after = self.conn.execute(
            "SELECT * FROM capture_session_members ORDER BY instrument_id").fetchall()
        self.assertEqual(before, after)


# ======================================================================
# Safety: capture can observe the market and cannot touch it
# ======================================================================

class TestSafety(_Case):

    def test_unguarded_gateway_is_refused(self):
        from src.execution.adapters.ibkr.gateway import IBKRGateway
        from src.execution.adapters.ibkr.config import paper_config
        from src.execution.instruments import InstrumentRegistry
        clock = ReplayClock(at(14, 0))
        raw = IBKRGateway(paper_config(), MovingVenue(clock), InstrumentRegistry())
        with self.assertRaises(CaptureConfigError):
            CaptureRunner(self.conn, raw, load_definition(write_universe(self.dir)),
                          clock=clock, lease_owner="x")

    def test_every_write_path_is_refused_and_stops_the_runner(self):
        runner, venue, clock = self.runner(at(14, 0))
        run_until(runner, clock, at(14, 5))
        for call in (lambda: runner.gateway.submit_order(None, clock.now()),
                     lambda: runner.gateway.cancel_order("1", clock.now()),
                     lambda: runner.gateway.modify_order("1"),
                     lambda: runner.gateway.transport.place_order("a", []),
                     lambda: runner.gateway.transport.cancel_order("a", "1"),
                     lambda: runner.gateway.transport.reply("r")):
            with self.assertRaises(BrokerSubmissionForbidden):
                call()
        self.assertEqual((venue.place_calls, venue.cancel_calls), (0, 0))
        with self.assertRaises(CaptureSafetyViolation):
            runner.step()
        self.assertEqual(len(self.events("BROKER_WRITE_REFUSED")), 1)

    def test_capture_imports_no_decision_component(self):
        root = os.path.join(os.path.dirname(__file__), "..", "..", "src", "capture")
        forbidden = re.compile(r"from src\.(trading\.(loop|session_runner|stack|"
                               r"portfolio|intake)|risk|execution\.orchestrator|"
                               r"execution\.service|signals|models)")
        for name in os.listdir(root):
            if name.endswith(".py"):
                with open(os.path.join(root, name), encoding="utf-8") as handle:
                    self.assertIsNone(forbidden.search(handle.read()), name)

    def test_ordering_is_forced_off(self):
        runner, venue, clock = self.runner(at(14, 0))
        self.assertFalse(runner.gateway.config.ordering_enabled)
        self.assertFalse(runner.gateway.config.can_submit_orders)


# ======================================================================
# Quality, maturity, retention
# ======================================================================

class TestQualityRules(unittest.TestCase):

    def test_classification_table(self):
        c = quality.classify
        self.assertEqual(c(390, 30, 31, 390, 390, 11000), "GOOD")
        self.assertEqual(c(390, 25, 31, 390, 390, 9000), "DEGRADED")   # too few mapped
        self.assertEqual(c(390, 30, 31, 200, 195, 5800), "PARTIAL")
        self.assertEqual(c(390, 30, 31, 390, 200, 8000), "DEGRADED")
        self.assertEqual(c(390, 30, 31, 20, 10, 200), "FAILED")
        self.assertEqual(c(390, 0, 31, 0, 0, 0), "FAILED")

    def _sessions(self, conn, dates, grade="GOOD", transport="ClientPortalTransport"):
        conn.execute("INSERT OR IGNORE INTO capture_instances (instance_id, "
                     "lease_owner, started_at, state, transport) VALUES "
                     "(?, 'o', 'x', 'IDLE', ?)", (f"i-{transport}", transport))
        for d in dates:
            conn.execute(
                "INSERT INTO capture_sessions (session_id, session_date, "
                "session_type, opens_at, closes_at, universe_version, status, "
                "quality, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (f"cap-{d}", d, "regular", "x", "x", "v1", "finalized", grade, "x"))
            conn.execute("INSERT INTO capture_ticks (session_id, tick_at, instance_id) "
                         "VALUES (?, ?, ?)", (f"cap-{d}", d, f"i-{transport}"))
        conn.commit()

    def test_mock_sessions_never_count_toward_maturity(self):
        conn = capture_db()
        self._sessions(conn, ["2026-10-01", "2026-10-02"], transport="MovingVenue")
        report = quality.maturity(conn, date(2026, 11, 1))
        self.assertEqual(report["qualifying_sessions"], 0)
        self.assertEqual(set(report["excluded_non_real_sessions"].values()), {"MOCK"})

    def test_maturity_counts_sessions_not_rows(self):
        conn = capture_db()
        self.assertEqual(quality.maturity(conn, date(2026, 9, 19))["band"], "INSUFFICIENT")
        start = date(2026, 9, 21)
        days = [(start + timedelta(days=i)).isoformat() for i in range(0, 60)
                if (start + timedelta(days=i)).weekday() < 5][:25]
        self._sessions(conn, days)
        self._sessions(conn, ["2026-12-01"], grade="DEGRADED")
        report = quality.maturity(conn, date(2026, 10, 30))
        self.assertEqual(report["qualifying_sessions"], 25)
        self.assertEqual(report["band"], "INSUFFICIENT",
                         "25 sessions in two months is below the 40-session floor")
        self.assertEqual(report["next_milestone"], 40)
        self.assertEqual(report["by_quality"]["DEGRADED"], 1)
        self.assertIn("planning targets", report["milestones_are"])

    def test_bands_follow_the_calendar_not_the_rows(self):
        conn = capture_db()
        day, dates = date(2026, 1, 5), []
        while len(dates) < 130:
            if day.weekday() < 5:
                dates.append(day.isoformat())
            day += timedelta(days=1)
        self._sessions(conn, dates[:45])
        self.assertEqual(quality.maturity(conn, date(2026, 12, 1))["band"], "MARGINAL")
        self._sessions(conn, dates[45:95])
        self.assertEqual(quality.maturity(conn, date(2026, 12, 1))["band"], "IMPROVING")
        self._sessions(conn, dates[95:125])
        self.assertEqual(quality.maturity(conn, date(2026, 12, 1))["band"], "READY")

    def test_a_full_month_of_perfect_sessions_is_still_insufficient(self):
        conn = capture_db()
        october = [d.isoformat() for d in (date(2026, 10, 1) + timedelta(days=i)
                                           for i in range(31)) if d.weekday() < 5]
        self._sessions(conn, october)
        report = quality.maturity(conn, date(2026, 11, 1))
        self.assertEqual(report["calendar_months"], 1)
        self.assertEqual(report["band"], "INSUFFICIENT")

    def test_prune_never_deletes_an_unarchived_bar(self):
        conn = capture_db()
        old = (datetime.now(UTC) - timedelta(days=40)).replace(second=0, microsecond=0)
        for i, (gap, archived) in enumerate(((0, True), (0, False), (1, False))):
            start = old + timedelta(minutes=i)
            conn.execute(
                "INSERT INTO market_data_bars (instrument_id, bar_start, bar_end, "
                "open, high, low, close, volume, observation_count, is_complete, "
                "is_gap, source, session_id, created_at) "
                "VALUES ('x',?,?,1,1,1,1,0,1,1,?,'s','s',?)",
                (start.isoformat(), (start + timedelta(minutes=1)).isoformat(),
                 gap, start.isoformat()))
            if archived:
                conn.execute("INSERT INTO price_candle_cache (instrument_id, interval, "
                             "timestamp, close, fetched_at) VALUES ('x','1m',?,1,?)",
                             (start.isoformat(), start.isoformat()))
        conn.commit()
        result = quality.prune_archived(conn, datetime.now(UTC), 30)
        self.assertEqual(result["bars"], 2)
        self.assertEqual(result["old_unarchived_kept"], 1)


class TestPersistedFeatures(_Case):

    def test_bounded_history_reproduces_full_history(self):
        runner, venue, clock = self.runner(at(13, 0, day=17))
        run_until(runner, clock, at(17, 0, day=18))
        ids = runner.definition.instrument_ids
        cutoff = at(16, 0)
        bounded = bounded_builder(self.conn, ids, cutoff)
        full = IntradayDatasetBuilder(self.conn)
        peers = [i for i in ids if i != "benchmark-spy"]
        for instrument_id in ids:
            a = bounded.observation(instrument_id, cutoff, peers=peers)
            b = full.observation(instrument_id, cutoff, peers=peers)
            self.assertEqual(a.features, b.features, instrument_id)
        self.assertIsNotNone(
            full.observation("us_and_intl-amzn", cutoff, peers=peers)
            .features.get("market.overnight_gap"),
            "the previous session's close must be inside the bounded window")

    def test_recompute_reproduces_live_values(self):
        runner, venue, clock = self.runner(at(13, 0))
        run_until(runner, clock, at(15, 0))
        compute_and_persist(self.conn, runner.resolved, at(14, 30),
                            "cap-2026-09-18", clock.now())
        live = self.conn.execute(
            "SELECT instrument_id, cutoff, feature_id, value FROM "
            "intraday_feature_values ORDER BY 1, 2, 3").fetchall()
        recompute_session(self.conn, "cap-2026-09-18", clock.now())
        again = self.conn.execute(
            "SELECT instrument_id, cutoff, feature_id, value FROM "
            "intraday_feature_values ORDER BY 1, 2, 3").fetchall()
        self.assertEqual(live, again)
        self.assertGreater(len(live), 0)


class TestColdContract(_Case):

    def test_silent_instrument_is_missing_not_invented(self):
        runner, venue, clock = self.runner(at(13, 0))
        run_until(runner, clock, at(13, 29))
        venue.silent.add("900001")              # AMZN says nothing
        run_until(runner, clock, at(14, 0))
        venue.silent.clear()
        run_until(runner, clock, at(21, 0))
        amzn = self.conn.execute(
            "SELECT COUNT(*) FROM price_candle_cache WHERE instrument_id = "
            "'us_and_intl-amzn' AND timestamp < ?", (at(13, 59).isoformat(),)
        ).fetchone()[0]
        self.assertEqual(amzn, 0)
        status, grade, summary = self.session()
        self.assertEqual(summary["member_coverage"]["us_and_intl-amzn"] < 1.0, True)
        # 5 of 6 present clears the 80% cross-sectional bar
        self.assertEqual(summary["cross_sectional_minutes"], 390)


if __name__ == "__main__":
    unittest.main()
