"""
tests/capture/test_audit_25_9h.py
---------------------------------------
Phase 25.9H -- the first-session acceptance audit, and its negative
controls (section 76).

Every store here is a FIXTURE: a session captured through the production
runner against the mock venue. To exercise the REAL-evidence paths the
fixture's provenance is relabelled to the real transport -- the one thing
a mock cannot produce -- and then each test damages one property the
audit must catch.
"""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.capture import audit
from src.capture.runner import CaptureConfig
from src.trading.clock import ReplayClock
from tests.capture.harness import MovingVenue, TICKERS, capture_db, make_runner, run_until

UTC = timezone.utc
ROOT = os.path.join(os.path.dirname(__file__), "..", "..")


def at(hour, minute=0, day=18):
    return datetime(2026, 9, day, hour, minute, tzinfo=UTC)


def captured(start=at(12, 0), end=at(21, 0), venue_tickers=TICKERS, real=True,
             during=None, config=None):
    conn = capture_db()
    clock = ReplayClock(start)
    venue = MovingVenue(clock, venue_tickers)
    runner, _ = make_runner(conn, clock, tempfile.mkdtemp(), venue=venue,
                            config=config or CaptureConfig())
    runner.start()
    if during:
        during(runner, venue, clock)
    run_until(runner, clock, end)
    runner.shutdown("fixture done")
    if real:
        conn.execute("UPDATE capture_instances SET transport = ?", (audit.REAL_TRANSPORT,))
        conn.commit()
    return conn


def copy(conn):
    clone = sqlite3.connect(":memory:")
    conn.backup(clone)
    return clone


def status(result, name):
    return next(c["status"] for c in result["checks"] if c["check"] == name)


class TestVerdicts(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.full = captured()

    def test_full_session(self):
        result = audit.acceptance(self.full)
        self.assertEqual(result["verdict"], "FULL SESSION VERIFIED")
        self.assertEqual([c for c in result["checks"] if c["status"] == "FAIL"], [])
        rec = result["session"]
        self.assertEqual(rec["evidence"], "REAL")
        self.assertEqual(rec["captured_minutes"], 390)
        self.assertEqual(rec["resolved_instrument_minutes"], 390 * len(TICKERS))
        self.assertEqual(rec["coverage_of_resolved_instrument_minutes"], 1.0)
        self.assertEqual(result["features"]["missing_cutoffs"], [])
        self.assertEqual(result["gaps"], [])

    def test_zero_real_sessions(self):
        self.assertEqual(audit.acceptance(capture_db())["verdict"],
                         "NO REAL SESSION CAPTURED")

    def test_mock_session_is_not_real_evidence(self):
        conn = copy(self.full)
        conn.execute("UPDATE capture_instances SET transport = 'MovingVenue'")
        result = audit.acceptance(conn)
        self.assertEqual(result["verdict"], "NO REAL SESSION CAPTURED")
        self.assertEqual(status(result, "no mock session in the capture store"), "FAIL")

    def test_legacy_unattributed_session_is_not_real(self):
        conn = copy(self.full)
        conn.execute("UPDATE capture_instances SET transport = NULL")
        self.assertEqual(audit.session_evidence(conn, "cap-2026-09-18"), "UNKNOWN")
        self.assertEqual(audit.acceptance(conn)["verdict"], "NO REAL SESSION CAPTURED")

    def test_mock_mixed_into_a_real_store_fails(self):
        conn = copy(self.full)
        conn.execute("INSERT INTO capture_instances (instance_id, lease_owner, "
                     "started_at, state, transport) VALUES ('m', 'o', 'x', 'IDLE', "
                     "'MovingVenue')")
        conn.execute("INSERT INTO capture_sessions (session_id, session_date, "
                     "session_type, opens_at, closes_at, universe_version, status, "
                     "created_at) VALUES ('cap-2026-09-17', '2026-09-17', 'regular', "
                     "'2026-09-17T13:30:00+00:00', '2026-09-17T20:00:00+00:00', 'v', "
                     "'finalized', 'x')")
        conn.execute("INSERT INTO capture_ticks (session_id, tick_at, instance_id) "
                     "VALUES ('cap-2026-09-17', '2026-09-17T14:00:00+00:00', 'm')")
        result = audit.acceptance(conn)
        self.assertEqual(status(result, "no mock session in the capture store"), "FAIL")
        self.assertEqual(result["verdict"], "REAL DATA CAPTURED BUT SESSION INVALID")

    def test_session_in_progress_has_no_verdict(self):
        conn = captured(end=at(15, 0))
        self.assertEqual(audit.acceptance(conn)["verdict"],
                         "NO VERDICT YET: SESSION NOT FINALIZED")


class TestNegativeControls(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.base = captured(config=CaptureConfig(feature_every_minutes=5))

    def setUp(self):
        self.conn = copy(self.base)

    def test_duplicate_logical_bar_in_another_timestamp_format(self):
        row = self.conn.execute(
            "SELECT instrument_id, timestamp, open, high, low, close FROM "
            "price_candle_cache LIMIT 1").fetchone()
        self.conn.execute(
            "INSERT INTO price_candle_cache (instrument_id, interval, timestamp, open, "
            "high, low, close, source, fetched_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (row[0], "1m", row[1].replace("+00:00", "Z"), row[2], row[3], row[4],
             row[5], "ibkr_operational_archive", "x"))
        result = audit.acceptance(self.conn)
        self.assertEqual(result["archive"]["research_duplicates"], 1)
        self.assertEqual(status(result, "archive integrity"), "FAIL")

    def test_missing_archive(self):
        self.conn.execute("DELETE FROM price_candle_cache WHERE rowid IN "
                          "(SELECT rowid FROM price_candle_cache LIMIT 10)")
        result = audit.acceptance(self.conn)
        self.assertEqual(result["archive"]["complete_bars_not_archived"], 10)
        self.assertEqual(status(result, "archive integrity"), "FAIL")

    def test_gap_marker_in_the_research_archive(self):
        self.conn.execute("UPDATE market_data_bars SET is_gap = 1 WHERE rowid = "
                          "(SELECT MIN(rowid) FROM market_data_bars)")
        result = audit.acceptance(self.conn)
        self.assertEqual(result["archive"]["gap_or_incomplete_archived"], 1)
        self.assertEqual(status(result, "archive integrity"), "FAIL")

    def test_missing_features(self):
        cutoffs = [r[0] for r in self.conn.execute(
            "SELECT DISTINCT cutoff FROM intraday_feature_values ORDER BY cutoff")]
        cutoff = cutoffs[len(cutoffs) // 2]            # mid-session
        self.conn.execute("DELETE FROM intraday_feature_values WHERE cutoff = ?", (cutoff,))
        result = audit.acceptance(self.conn)
        self.assertEqual(len(result["features"]["missing_cutoffs"]), 1)
        self.assertEqual(status(result, "features at every due cutoff"), "FAIL")

    def test_broker_write_attempt(self):
        self.conn.execute("INSERT INTO capture_events (at, instance_id, kind, detail) "
                          "VALUES ('x', 'i', 'BROKER_WRITE_REFUSED', '{}')")
        result = audit.acceptance(self.conn)
        self.assertEqual(status(result, "broker write attempts = 0"), "FAIL")
        self.assertEqual(result["verdict"], "REAL DATA CAPTURED BUT SESSION INVALID")

    def test_unexpected_write_domain_row(self):
        self.conn.execute("CREATE TABLE signals (id TEXT)")      # a production domain
        self.conn.execute("INSERT INTO signals VALUES ('s1')")
        self.conn.execute("CREATE TABLE model_scores (x)")
        self.conn.execute("INSERT INTO model_scores VALUES (1)")
        result = audit.acceptance(self.conn)
        scope = result["checks"][1]["detail"]
        self.assertIn("model_scores", scope["unexpected_tables_with_rows"])
        self.assertEqual(scope["execution_domain_rows"]["signals"], 1)
        self.assertEqual(status(result, "write scope"), "FAIL")

    def test_wrong_currency_contract(self):
        self.conn.execute("UPDATE broker_instrument_mapping SET currency = 'EUR' "
                          "WHERE canonical_instrument_id = 'us_and_intl-amzn'")
        result = audit.acceptance(self.conn)
        self.assertEqual(status(result, "contract identity"), "FAIL")

    def test_future_venue_timestamp(self):
        self.conn.execute("UPDATE capture_quote_samples SET broker_at = "
                          "'2030-01-01T00:00:00+00:00' WHERE rowid = 1")
        result = audit.acceptance(self.conn)
        self.assertEqual(status(result, "no venue timestamp in the future"), "FAIL")

    def test_delayed_data_is_reported(self):
        self.conn.execute("UPDATE capture_ticks SET delayed = realtime, realtime = 0")
        result = audit.acceptance(self.conn)
        self.assertEqual(status(result, "realtime data"), "WARN")


class TestSessionShapes(unittest.TestCase):

    def test_partial_session(self):
        def late_login(runner, venue, clock):
            venue.authenticated = False
            run_until(runner, clock, at(15, 0))
            venue.authenticated = True
        result = audit.acceptance(captured(during=late_login))
        self.assertEqual(result["verdict"], "PARTIAL REAL SESSION VERIFIED")
        self.assertEqual(result["gaps"][0]["cause"], "AUTH")

    def test_large_gap_is_found_and_attributed(self):
        def sleep(runner, venue, clock):
            run_until(runner, clock, at(15, 0))
            clock.advance(45 * 60)
        result = audit.acceptance(captured(during=sleep))
        self.assertEqual(status(result, "no large gap"), "WARN")
        causes = {g["cause"] for g in result["gaps"]}
        self.assertEqual(causes, {"HOST SLEEP"})

    def test_unresolved_mappings(self):
        result = audit.acceptance(captured(venue_tickers=TICKERS[:3]))
        self.assertEqual(status(result, "universe fully mapped"), "WARN")
        self.assertEqual(result["session"]["resolved_universe"], 3)
        self.assertEqual(result["session"]["expected_universe"], len(TICKERS))
        # half the universe is DEGRADED by the frozen 25.9G rule
        self.assertEqual(result["verdict"], "REAL DATA CAPTURED BUT SESSION INVALID")


class TestReadOnlyCommand(unittest.TestCase):

    def run_cli(self, *args):
        return subprocess.run([sys.executable, os.path.join(ROOT, "scripts",
                                                            "capture_report.py"), *args],
                              capture_output=True, text=True, timeout=300)

    def test_cli_is_read_only_and_uses_exit_codes(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "store.db")
        disk = sqlite3.connect(path)
        captured(end=at(21, 0)).backup(disk)
        disk.close()
        digest = hashlib.sha256(open(path, "rb").read()).hexdigest()
        out = self.run_cli("--db", path, "--acceptance")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("VERDICT: FULL SESSION VERIFIED", out.stdout)
        self.assertEqual(hashlib.sha256(open(path, "rb").read()).hexdigest(), digest)
        as_json = json.loads(self.run_cli("--db", path, "--acceptance", "--json").stdout)
        self.assertEqual(as_json["verdict"], "FULL SESSION VERIFIED")

    def test_no_store_is_not_created(self):
        path = os.path.join(tempfile.mkdtemp(), "absent.db")
        self.assertEqual(self.run_cli("--db", path, "--acceptance").returncode, 2)
        self.assertFalse(os.path.exists(path))

    def test_empty_store_exit_2(self):
        path = os.path.join(tempfile.mkdtemp(), "empty.db")
        disk = sqlite3.connect(path)
        capture_db().backup(disk)
        disk.close()
        self.assertEqual(self.run_cli("--db", path, "--acceptance").returncode, 2)


if __name__ == "__main__":
    unittest.main()
