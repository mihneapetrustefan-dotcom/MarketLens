"""
tests/capture/test_capture_failures_25_9g.py
--------------------------------------------------
Phase 25.9G -- failure injection (sections 96-100) and independence
(sections 66-72).

Faults are injected where they happen in reality: at the venue (auth
lapses, the snapshot endpoint fails, contracts are missing, quotes go
stale), in the database (a lock held by another process, a trigger that
makes a write fail) and in the clock. The code under test is never
patched.
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.capture import quality
from src.capture.features import recompute_session
from src.capture.runner import CaptureConfig, LEASE_SCOPE
from src.capture.schema import initialize_capture_schema
from src.trading import leases
from src.trading.clock import ReplayClock
from tests.capture.harness import (
    MovingVenue, TICKERS, capture_db, make_runner, run_until,
)

UTC = timezone.utc
FAST = CaptureConfig(feature_every_minutes=100000)


def at(hour, minute=0, day=18):
    return datetime(2026, 9, day, hour, minute, tzinfo=UTC)


class _Case(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.conn = capture_db()

    def start(self, when, conn=None, **kwargs):
        clock = ReplayClock(when)
        kwargs.setdefault("config", FAST)
        runner, venue = make_runner(conn or self.conn, clock, self.dir, **kwargs)
        runner.start()
        return runner, venue, clock

    def events(self, kind, conn=None):
        return [json.loads(r[0]) for r in (conn or self.conn).execute(
            "SELECT detail FROM capture_events WHERE kind = ?", (kind,))]

    def research_minutes(self, start, end, conn=None):
        return (conn or self.conn).execute(
            "SELECT COUNT(*) FROM price_candle_cache WHERE timestamp >= ? "
            "AND timestamp < ?", (start.isoformat(), end.isoformat())).fetchone()[0]

    def summary(self, conn=None):
        row = (conn or self.conn).execute(
            "SELECT quality, summary_json FROM capture_sessions").fetchone()
        return row[0], json.loads(row[1])


class TestVenueFailures(_Case):

    def test_session_expiry_mid_day_waits_then_resumes(self):
        runner, venue, clock = self.start(at(13, 0))
        run_until(runner, clock, at(15, 0))
        venue.authenticated = False             # the gateway session lapses
        run_until(runner, clock, at(15, 30))
        self.assertEqual(runner.state.value, "WAITING_FOR_AUTH")
        self.assertEqual(len(self.events("AUTH_LOST")), 1)
        venue.authenticated = True
        run_until(runner, clock, at(21, 0))
        self.assertEqual(self.research_minutes(at(15, 2), at(15, 29)), 0,
                         "an outage is missing data, never invented data")
        markers = self.conn.execute(
            "SELECT COUNT(*) FROM market_data_bars WHERE is_gap = 1").fetchone()[0]
        self.assertGreater(markers, 0, "the outage is recorded as explicit gaps")
        grade, summary = self.summary()
        self.assertEqual(summary["reconnects"], 1)
        self.assertEqual(grade, "GOOD")         # 30 of 390 minutes lost: 92% kept
        self.assertEqual(summary["largest_gap_minutes"] >= 28, True)

    def test_quote_endpoint_failure_degrades_ticks_not_the_process(self):
        runner, venue, clock = self.start(at(13, 0))
        run_until(runner, clock, at(15, 0))
        venue.market_data_available = False
        run_until(runner, clock, at(15, 20))
        failed = self.conn.execute(
            "SELECT COUNT(*) FROM capture_ticks WHERE health = 'failed'").fetchone()[0]
        self.assertGreaterEqual(failed, 18)
        venue.market_data_available = True
        run_until(runner, clock, at(21, 0))
        self.assertEqual(self.research_minutes(at(15, 1), at(15, 19)), 0)
        self.assertEqual(self.events("STEP_ERROR"), [])
        grade, _ = self.summary()
        self.assertEqual(grade, "GOOD")

    def test_stale_quotes_make_no_new_minutes(self):
        runner, venue, clock = self.start(at(13, 0))
        run_until(runner, clock, at(15, 0))
        venue.stale_seconds = 600
        run_until(runner, clock, at(15, 10))
        venue.stale_seconds = 0
        run_until(runner, clock, at(21, 0))
        grade, summary = self.summary()
        self.assertGreater(summary["stale_observations"], 0)
        self.assertEqual(self.research_minutes(at(15, 1), at(15, 9)), 0)

    def test_half_the_universe_unmapped_still_captures_the_rest(self):
        clock = ReplayClock(at(13, 0))
        venue = MovingVenue(clock, TICKERS[:3])     # the venue knows only half
        runner, _ = make_runner(self.conn, clock, self.dir, venue=venue, config=FAST)
        runner.start()
        run_until(runner, clock, at(21, 0))
        grade, summary = self.summary()
        self.assertEqual(summary["resolved"], 3)
        self.assertEqual(len(summary["unresolved"]), 3)
        self.assertEqual(summary["cross_sectional_minutes"], 390)
        self.assertEqual(grade, "DEGRADED",
                         "half a universe is not multi-instrument readiness")

    def test_nothing_mapped_blocks_active_capture(self):
        clock = ReplayClock(at(13, 0))
        venue = MovingVenue(clock, ())
        runner, _ = make_runner(self.conn, clock, self.dir, venue=venue, config=FAST)
        runner.start()
        run_until(runner, clock, at(14, 0))
        self.assertEqual(venue.snapshot_calls, 0)
        self.assertEqual(len(self.events("CAPTURE_BLOCKED")), 1)
        preflight = self.events("PREFLIGHT")[0]
        self.assertFalse(preflight["ok"])
        self.assertEqual(preflight["resolved_members"], 0)


class TestStorageFailures(_Case):

    def test_archive_failure_keeps_operational_bars_and_catches_up(self):
        runner, venue, clock = self.start(at(13, 0))
        run_until(runner, clock, at(14, 0))
        self.conn.execute("CREATE TRIGGER inject_archive BEFORE INSERT ON "
                          "price_candle_cache BEGIN SELECT RAISE(ABORT, "
                          "'injected archive failure'); END")
        run_until(runner, clock, at(14, 30))
        self.assertGreater(len(self.events("ARCHIVE_FAILED")), 25)
        pending = self.conn.execute(
            "SELECT COUNT(*) FROM market_data_bars WHERE bar_start >= ? AND "
            "is_gap = 0", (at(14, 0).isoformat(),)).fetchone()[0]
        self.assertGreater(pending, 25 * len(TICKERS), "operational bars survive")
        self.conn.execute("DROP TRIGGER inject_archive")
        run_until(runner, clock, at(21, 0))
        grade, summary = self.summary()
        self.assertEqual(summary["bars_archived"], 390 * len(TICKERS),
                         "the bounded retry recovered every minute")
        self.assertGreater(summary["archive_failures"], 0)
        self.assertEqual(grade, "GOOD")

    def test_feature_failure_keeps_bars_and_recompute_repairs(self):
        config = CaptureConfig(feature_every_minutes=30)
        runner, venue, clock = self.start(at(13, 0), config=config)
        self.conn.execute("CREATE TRIGGER inject_features BEFORE INSERT ON "
                          "intraday_feature_values BEGIN SELECT RAISE(ABORT, "
                          "'injected feature failure'); END")
        run_until(runner, clock, at(16, 0))
        failed = self.events("FEATURE_FAILED")
        self.assertGreaterEqual(len(failed), 4)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM intraday_feature_values").fetchone()[0], 0)
        archived = self.research_minutes(at(13, 30), at(15, 59))
        self.assertEqual(archived, 149 * len(TICKERS), "bars are unaffected")
        self.conn.execute("DROP TRIGGER inject_features")
        repaired = recompute_session(self.conn, "cap-2026-09-18", clock.now())
        self.assertEqual(repaired["cutoffs"], len(failed))
        cutoffs = {r[0] for r in self.conn.execute(
            "SELECT DISTINCT cutoff FROM intraday_feature_values")}
        self.assertEqual(cutoffs, {f["cutoff"] for f in failed})

    def test_locked_database_is_waited_out(self):
        path = os.path.join(self.dir, "capture.db")
        conn = sqlite3.connect(path, timeout=0.2)
        conn.execute("PRAGMA journal_mode=WAL")
        initialize_capture_schema(conn)
        runner, venue, clock = self.start(at(13, 0), conn=conn)
        run_until(runner, clock, at(14, 0))
        blocker = sqlite3.connect(path)
        blocker.execute("BEGIN IMMEDIATE")     # another process holds the lock
        delays = [runner.step() for _ in range(3)]
        self.assertTrue(all(d == runner.config.locked_retry_seconds for d in delays))
        self.assertIn("locked", runner.last_error)
        # a reader is never blocked by the writer in WAL
        reader = sqlite3.connect(path)
        self.assertGreater(reader.execute(
            "SELECT COUNT(*) FROM price_candle_cache").fetchone()[0], 0)
        blocker.rollback()
        blocker.close()
        clock.advance(120)
        run_until(runner, clock, at(21, 0))
        grade, summary = self.summary(conn)
        self.assertIn(grade, ("GOOD", "PARTIAL"))
        self.assertGreaterEqual(summary["cross_sectional_minutes"], 385)


class TestLeaseTakeover(_Case):

    def test_superseded_instance_stops_acting(self):
        first, venue, clock = self.start(at(14, 0))
        run_until(first, clock, at(14, 10))
        second, _ = make_runner(self.conn, clock, self.dir, venue=venue, config=FAST)
        second.start()                          # same supervisor, newer process
        self.assertEqual(len(self.events("LEASE_TAKEOVER")), 1)
        with self.assertRaises(leases.LeaseRefused):
            first.step()
        run_until(second, clock, at(14, 20))    # the new one carries on

    def test_expired_lease_of_a_dead_supervisor_is_recoverable(self):
        first, venue, clock = self.start(at(14, 0))
        run_until(first, clock, at(14, 5))      # then the host dies: no release
        clock.advance(FAST.lease_ttl_seconds + 60)
        other, _ = make_runner(self.conn, clock, self.dir, venue=venue,
                               owner="supervisor-after-reboot", config=FAST)
        other.start()
        self.assertEqual(leases.holder(self.conn, LEASE_SCOPE, clock.now()),
                         "supervisor-after-reboot")
        takeover = self.events("LEASE_TAKEOVER")
        self.assertEqual(takeover[0]["previous_owner"], "supervisor-test")


class TestIndependence(unittest.TestCase):
    """Sections 66-72: capture needs no model, signal, risk or trading mode."""

    def test_capture_process_loads_no_decision_component(self):
        code = ("import sys; sys.path.insert(0, '.');"
                "import scripts.run_capture, src.capture.runner;"
                "bad = sorted(m for m in sys.modules if m.startswith(("
                "'src.models', 'src.signals', 'src.risk', 'src.trading.loop',"
                "'src.trading.session_runner', 'src.trading.stack',"
                "'src.execution.orchestrator', 'src.execution.service',"
                "'src.portfolio', 'src.research.models')));"
                "print(bad)")
        root = os.path.join(os.path.dirname(__file__), "..", "..")
        out = subprocess.run([sys.executable, "-c", code], cwd=root,
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "[]")

    def test_full_session_with_no_model_signal_or_trading_tables(self):
        conn = capture_db()
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ("trained_models", "model_promotions", "predictions", "signals"):
            self.assertNotIn(table, names, "no model/signal table exists to consult")
        clock = ReplayClock(at(13, 0))
        runner, venue = make_runner(conn, clock, tempfile.mkdtemp(), config=FAST)
        runner.start()
        run_until(runner, clock, at(21, 0))
        grade = conn.execute("SELECT quality FROM capture_sessions").fetchone()[0]
        self.assertEqual(grade, "GOOD")
        for table in ("execution_orders", "execution_fills", "order_intents",
                      "execution_events"):
            if table in names:
                self.assertEqual(conn.execute(
                    f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)
        self.assertFalse(runner.gateway.config.ordering_enabled)


class TestStoreSafety(unittest.TestCase):
    """Section 100: an invalid, unintended or corrupt store fails closed."""

    def run_capture(self, db):
        from scripts import run_capture
        d = tempfile.mkdtemp()
        return run_capture.main(["--db", db, "--log-dir", d,
                                 "--stop-file", os.path.join(d, "STOP")])

    def test_missing_directory_is_refused_not_created(self):
        d = tempfile.mkdtemp()
        target = os.path.join(d, "no", "such", "dir", "c.db")
        self.assertEqual(self.run_capture(target), 2)
        self.assertFalse(os.path.exists(os.path.dirname(target)))

    def test_research_database_is_refused(self):
        d = tempfile.mkdtemp()
        self.assertEqual(self.run_capture(os.path.join(d, "marketlens.db")), 2)
        self.assertFalse(os.path.exists(os.path.join(d, "marketlens.db")))

    def test_corrupt_store_is_refused(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "c.db")
        with open(path, "wb") as handle:
            handle.write(b"this is not a sqlite database" * 100)
        self.assertEqual(self.run_capture(path), 2)


class TestReports(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.conn = capture_db()
        cls.clock = ReplayClock(at(13, 0))
        cls.runner, _ = make_runner(cls.conn, cls.clock, tempfile.mkdtemp(),
                                    config=CaptureConfig(feature_every_minutes=60))
        cls.runner.start()
        run_until(cls.runner, cls.clock, at(21, 0))
        cls.summary = json.loads(cls.conn.execute(
            "SELECT summary_json FROM capture_sessions").fetchone()[0])

    def test_simultaneity_is_measured_at_the_same_minute(self):
        s = self.summary
        n = len(TICKERS)
        self.assertEqual((s["minutes_ge_1"], s["minutes_ge_3"], s["minutes_ge_5"]),
                         (390, 390, 390))
        self.assertEqual((s["median_simultaneous"], s["max_simultaneous"]), (n, n))
        self.assertEqual(s["largest_gap_minutes"], 0)
        self.assertIsNotNone(s["dispersion_1m_coverage"])
        self.assertEqual(s["order_write_attempts"], 0)
        self.assertEqual(s["quote_cycles"], 390)

    def test_provenance_of_a_random_minute(self):
        stamp = at(15, 17).isoformat()
        chain = quality.trace(self.conn, "us_and_intl-amzn", stamp)
        self.assertEqual(chain["contract"]["mapping"], "RESOLVED")
        self.assertTrue(chain["contract"]["conid"])
        self.assertTrue(chain["snapshot_ticks"])
        self.assertTrue(chain["operational_bar"]["complete"])
        self.assertEqual(chain["archive_record"]["archive_version"], "v1")
        self.assertEqual(chain["research_bar"]["source"], "ibkr_operational_archive")
        self.assertEqual(chain["research_bar"]["close"],
                         chain["operational_bar"]["close"])
        self.assertIsNotNone(chain["first_features_using_it"])

    def test_preflight_recorded_and_ordering_never_enabled(self):
        pre = [json.loads(r[0]) for r in self.conn.execute(
            "SELECT detail FROM capture_events WHERE kind = 'PREFLIGHT'")]
        self.assertTrue(pre and pre[0]["ok"])
        self.assertFalse(pre[0]["ordering_enabled"])
        self.assertTrue(pre[0]["capture_only_transport"])

    def test_status_condition_names_the_state(self):
        from scripts import capture_status
        base = {"reasons": [], "verdict_code": 0, "mappings": {"RESOLVED": 6}}
        cond = capture_status.condition
        self.assertEqual(cond(dict(base, instance={"state": "IDLE"})), "MARKET_CLOSED")
        self.assertEqual(cond(dict(base, instance={"state": "WAITING_FOR_AUTH"})),
                         "WAITING_FOR_AUTH")
        active = dict(base, instance={"state": "ACTIVE_SESSION"})
        self.assertEqual(cond(dict(active, last_tick={"health": "healthy"})),
                         "HEALTHY_CAPTURE")
        self.assertEqual(cond(dict(active, last_tick={"health": "degraded"})),
                         "DEGRADED_CAPTURE")
        self.assertEqual(cond(dict(active, mappings={"RESOLVED": 5, "FAILED": 1})),
                         "PARTIAL_CAPTURE")
        self.assertEqual(cond(dict(base, verdict_code=2, instance=None)),
                         "SYSTEM_ERROR")
        self.assertEqual(cond(dict(base, verdict_code=2, instance=None,
                                   supervisor={"state": "STOPPED"})),
                         "STOPPED_BY_OPERATOR")


if __name__ == "__main__":
    unittest.main()


class TestIdleKeepalive(_Case):
    """A morning login is kept alive until the pre-open (read-only)."""

    def test_morning_login_is_kept_alive_until_pre_open(self):
        runner, venue, clock = self.start(at(7, 30, day=21))       # Monday
        run_until(runner, clock, at(13, 5, day=21))
        self.assertEqual(runner.auth_state, "connected")
        # about one keepalive a minute across 5.5 hours
        self.assertGreater(venue.keepalive_calls, 300)
        self.assertEqual(venue.snapshot_calls, 0, "no market data before pre-open")
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM capture_sessions").fetchone()[0], 0)
        self.assertEqual((venue.place_calls, venue.cancel_calls), (0, 0))

    def test_session_carries_into_the_open_without_a_new_login(self):
        runner, venue, clock = self.start(at(7, 30, day=21))
        run_until(runner, clock, at(14, 0, day=21))
        self.assertEqual(runner.state.value, "ACTIVE_SESSION")
        self.assertEqual(len(self.events("AUTHENTICATED")), 1,
                         "the morning login is the only login")

    def test_nobody_logged_in_is_quiet_before_pre_open(self):
        runner, venue, clock = self.start(at(7, 30, day=21))
        venue.authenticated = False
        run_until(runner, clock, at(13, 5, day=21))
        self.assertEqual(self.events("WAITING_FOR_AUTH"), [])
        self.assertEqual(runner.state.value, "IDLE")
        self.assertLess(venue.auth_calls, 400, "polled every idle interval, not hammered")

    def test_lapse_is_recorded_and_a_new_login_is_picked_up(self):
        runner, venue, clock = self.start(at(7, 30, day=21))
        run_until(runner, clock, at(9, 0, day=21))
        venue.authenticated = False
        run_until(runner, clock, at(10, 0, day=21))
        self.assertEqual(len(self.events("IDLE_SESSION_LAPSED")), 1)
        venue.authenticated = True
        run_until(runner, clock, at(10, 10, day=21))
        self.assertEqual(runner.auth_state, "connected")

    def test_weekend_and_holiday_make_no_request(self):
        runner, venue, clock = self.start(at(10, 0, day=19))        # Saturday
        run_until(runner, clock, at(10, 0, day=20))
        self.assertEqual((venue.auth_calls, venue.keepalive_calls), (0, 0))


class TestRealEvidenceFields(_Case):
    """Phase 25.9H: what a real session must leave behind to be audited."""

    @classmethod
    def setUpClass(cls):
        cls.db = capture_db()
        cls.clock = ReplayClock(at(12, 0))
        cls.runner, cls.venue = make_runner(cls.db, cls.clock, tempfile.mkdtemp(),
                                            config=FAST)
        cls.runner.start()
        run_until(cls.runner, cls.clock, at(21, 0))

    def test_transport_is_recorded_per_instance(self):
        kinds = {r[0] for r in self.db.execute(
            "SELECT transport FROM capture_instances")}
        self.assertEqual(kinds, {"MovingVenue"})

    def test_realtime_marker_counted_every_tick(self):
        row = self.db.execute(
            "SELECT MIN(realtime), MAX(delayed), MAX(unknown_availability), "
            "COUNT(*) FROM capture_ticks").fetchone()
        self.assertEqual(row, (len(TICKERS), 0, 0, 390))

    def test_requests_per_minute_recorded_and_inside_budget(self):
        worst = self.db.execute(
            "SELECT MAX(requests_last_minute) FROM capture_ticks").fetchone()[0]
        self.assertGreaterEqual(worst, 2)
        self.assertLessEqual(worst, 10)

    def test_quote_samples_are_bounded(self):
        ticks = self.db.execute(
            "SELECT COUNT(DISTINCT tick_at) FROM capture_quote_samples").fetchone()[0]
        self.assertEqual(ticks, 5 + len(range(30, 390, 30)))
        rows = self.db.execute(
            "SELECT COUNT(*) FROM capture_quote_samples").fetchone()[0]
        self.assertEqual(rows, ticks * len(TICKERS))

    def test_host_kept_awake_only_for_the_session(self):
        self.assertEqual(self.venue.keep_awake_requests, [True, False])
        events = [json.loads(r[0]) for r in self.db.execute(
            "SELECT detail FROM capture_events WHERE kind = 'KEEP_AWAKE' "
            "ORDER BY event_id")]
        self.assertEqual([e["on"] for e in events], [True, False])

    def test_weekend_never_asks_to_stay_awake(self):
        runner, venue, clock = self.start(at(10, 0, day=19))
        run_until(runner, clock, at(10, 0, day=20))
        self.assertEqual(venue.keep_awake_requests, [])

    def test_existing_store_gains_the_new_columns(self):
        old = sqlite3.connect(":memory:")
        old.execute("CREATE TABLE capture_ticks (session_id TEXT, tick_at TEXT, "
                    "instance_id TEXT, PRIMARY KEY (session_id, tick_at))")
        old.execute("CREATE TABLE capture_instances (instance_id TEXT PRIMARY KEY, "
                    "lease_owner TEXT, started_at TEXT, state TEXT)")
        old.execute("INSERT INTO capture_ticks VALUES ('s', 't', 'i')")
        initialize_capture_schema(old)
        cols = {r[1] for r in old.execute("PRAGMA table_info(capture_ticks)")}
        self.assertTrue({"realtime", "delayed", "venue_lag_seconds"} <= cols)
        self.assertEqual(old.execute("SELECT COUNT(*) FROM capture_ticks").fetchone()[0], 1)
