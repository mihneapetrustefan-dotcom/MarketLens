"""
tests/impact/test_label_readiness.py
-----------------------------------------------------------
Protected-window readiness and failure injection (Phase 25.9C, §31, §41).

Built on a synthetic database shaped like production, so every failure
can be injected precisely. Uses a temporary ledger throughout.

THE PROPERTY THAT MATTERS MOST: no false READY. A passed calendar date
with the data absent must be NOT READY (§32).
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import scripts.validate_d20_reversal as V
from scripts.check_d20_readiness import evaluate_readiness
from src.data_access.price_cache_schema import initialize_price_cache_schema
from src.domain.impact_models import DEFAULT_WINDOWS
from src.impact import label_readiness as R
from src.impact.anchoring import resolve_v2
from src.impact.engine import Candle
from src.research import protected_ledger as L

UTC = timezone.utc
NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)
ANCHOR_DATES = ["2026-08-17", "2026-08-18", "2026-08-19", "2026-08-20", "2026-08-21",
                "2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27"]
PER_DATE = 24     # 216 rows: MDE <= 0.20 needs at least 194


def weekdays(start, end):
    day, out = date.fromisoformat(start), []
    while day <= date.fromisoformat(end):
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def build(candle_end="2026-10-14", requested_at="2026-10-14T22:00:00+00:00",
          dates=ANCHOR_DATES, per_date=PER_DATE, labels=True, calculation="anchor-v2"):
    conn = sqlite3.connect(":memory:")
    initialize_price_cache_schema(conn)
    conn.executescript("""
        CREATE TABLE research_observations (observation_id TEXT, event_id TEXT,
            instrument_id TEXT, information_cutoff TEXT, quality_level TEXT);
        CREATE TABLE event_studies (event_id TEXT, instrument_id TEXT, benchmark_id TEXT,
            market_visibility_latest TEXT);
        CREATE TABLE research_labels (observation_id TEXT, name TEXT, value_json TEXT,
            measured_at TEXT, window_name TEXT, label_version TEXT, calculation TEXT,
            PRIMARY KEY (observation_id, name));
        CREATE TABLE research_features (observation_id TEXT, qualified_name TEXT, value_json TEXT);
        CREATE TABLE instruments (instrument_id TEXT, asset_class TEXT);
    """)
    for inst in ("us-x", "benchmark-spy"):
        for day in weekdays("2026-04-01", candle_end):
            conn.execute("INSERT INTO price_candle_cache (instrument_id, interval, timestamp, "
                         "open, high, low, close, adjusted_close, volume, source, fetched_at) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (inst, "1d", f"{day}T04:00:00+00:00", 1, 1, 1, 100.0, 100.0, 1, "t", "t"))
        conn.execute("INSERT INTO price_cache_requests VALUES (?,?,?,?,?,?)",
                     (inst, "1d", "2026-04-01T00:00:00+00:00", "2026-11-30T00:00:00+00:00",
                      100, requested_at))
    conn.execute("INSERT INTO instruments VALUES ('us-x','stock')")
    n = 0
    for d in dates:
        for k in range(per_date):
            oid, eid = f"obs-{d}-{k}", f"ev-{d}-{k}"
            conn.execute("INSERT INTO research_observations VALUES (?,?,?,?,?)",
                         (oid, eid, "us-x", f"{d}T15:00:00", "high"))
            conn.execute("INSERT INTO event_studies VALUES (?,?,?,?)",
                         (eid, "us-x", "benchmark-spy", f"{d}T15:00:00+00:00"))
            conn.execute("INSERT INTO research_features VALUES (?,?,?)",
                         (oid, R.FEATURE, "0.1"))
            if labels:
                for w in ("d5", "d20"):
                    conn.execute("INSERT INTO research_labels VALUES (?,?,?,?,?,?,?)",
                                 (oid, f"{w}.abnormal_return.anchor-v2", str(0.01 * k),
                                  None, w, "v2", calculation))
            n += 1
    conn.commit()
    return conn


class ReadinessCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.ledger = os.path.join(self.dir, "ledger.jsonl")
        L.register(V.EXPERIMENT_ID, V.fingerprint(),
                   {"max_quality_exclusion_share": 0.05}, self.ledger)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def check(self, conn, now=NOW):
        return evaluate_readiness(conn, now, self.ledger)


class TestCompleteIsReady(ReadinessCase):

    def test_everything_present_is_ready(self):
        report = self.check(build())
        self.assertTrue(report["ready"], report["reasons"])
        self.assertEqual(report["summary"]["resolvable"], len(ANCHOR_DATES) * PER_DATE)

    def test_the_report_exposes_no_performance_statistic(self):
        """§11: readiness never returns returns, correlations or spreads."""
        report = self.check(build())
        text = repr(report).lower()
        for forbidden in ("mean_ic", "correlation", "spread", "p_value", "hit_rate",
                          "expected_return", "verdict"):
            self.assertNotIn(forbidden, text)


class TestFailureInjection(ReadinessCase):

    def assertNotReady(self, report, fragment):
        self.assertFalse(report["ready"])
        self.assertTrue(any(fragment in r for r in report["reasons"]),
                        f"expected a reason containing {fragment!r}, got {report['reasons']}")

    def test_date_passed_but_data_absent_is_not_ready(self):
        """§27/§32: the calendar alone never makes READY."""
        conn = build(candle_end="2026-09-05", requested_at="2026-09-05T22:00:00+00:00")
        self.assertNotReady(self.check(conn), "pending")

    def test_stale_cache_is_distinguished_from_future(self):
        conn = build(candle_end="2026-09-05", requested_at="2026-09-05T22:00:00+00:00")
        states = R.summarize(R.assess(conn, V.PROTECTED_START, V.PROTECTED_END, NOW))["states"]
        self.assertIn(R.STALE_CACHE, states)
        self.assertNotIn(R.NOT_YET_OBSERVABLE, states)

    def test_before_the_horizon_it_is_not_yet_observable(self):
        conn = build(candle_end="2026-09-05", requested_at="2026-09-05T22:00:00+00:00")
        early = datetime(2026, 9, 6, tzinfo=UTC)
        states = R.summarize(R.assess(conn, V.PROTECTED_START, V.PROTECTED_END, early))["states"]
        self.assertIn(R.NOT_YET_OBSERVABLE, states)
        self.assertNotIn(R.EXPECTED_DATA_MISSING, states)

    def test_refreshed_past_the_date_with_no_price_is_expected_missing(self):
        conn = build()
        conn.execute("DELETE FROM price_candle_cache WHERE instrument_id='us-x' "
                     "AND timestamp >= '2026-09-01'")
        states = R.summarize(R.assess(conn, V.PROTECTED_START, V.PROTECTED_END, NOW))["states"]
        self.assertIn(R.EXPECTED_DATA_MISSING, states)
        self.assertNotReady(self.check(conn), "exclusion")

    def test_a_refresh_during_the_session_does_not_count_as_observed(self):
        """Measured: most caches end one day before their request date."""
        conn = build(candle_end="2026-08-27", requested_at="2026-08-28T18:54:00+00:00")
        items = R.assess(conn, "2026-08-21T00:00:00", "2026-08-21T23:59:59",
                         datetime(2026, 8, 29, tzinfo=UTC))
        self.assertNotIn(R.EXPECTED_DATA_MISSING, {i.state for i in items})

    def test_missing_benchmark_close_is_named(self):
        conn = build()
        conn.execute("DELETE FROM price_candle_cache WHERE instrument_id='benchmark-spy' "
                     "AND timestamp LIKE '2026-09%'")
        states = R.summarize(R.assess(conn, V.PROTECTED_START, V.PROTECTED_END, NOW))["states"]
        self.assertIn(R.MISSING_BENCHMARK_PRICE, states)

    def test_labels_not_built_is_pending(self):
        self.assertNotReady(self.check(build(labels=False)), "pending")

    def test_wrong_method_version_labels_do_not_count(self):
        """A v1 label is never silently reinterpreted as anchor-v2."""
        self.assertNotReady(self.check(build(calculation="anchor-v1")), "pending")

    def test_malformed_anchor_is_invalid(self):
        conn = build()
        conn.execute("UPDATE event_studies SET market_visibility_latest='not-a-time' "
                     "WHERE event_id LIKE 'ev-2026-08-17-%'")
        states = R.summarize(R.assess(conn, V.PROTECTED_START, V.PROTECTED_END, NOW))["states"]
        self.assertIn(R.INVALID_ANCHOR, states)
        self.assertFalse(self.check(conn)["ready"])

    def test_missing_feature_is_an_exclusion(self):
        conn = build()
        conn.execute("DELETE FROM research_features WHERE observation_id LIKE 'obs-2026-08-17-%'")
        self.assertNotReady(self.check(conn), "exclusion")

    def test_vintage_break_is_detected(self):
        conn = build()
        conn.execute("INSERT INTO price_cache_vintage_checks VALUES (?,?,?,?,?,?,?,?)",
                     ("us-x", "1d", "2026-10-01", "2026-08-20T04:00:00+00:00",
                      "2026-08-31T04:00:00+00:00", 8, 0.5, 0))
        states = R.summarize(R.assess(conn, V.PROTECTED_START, V.PROTECTED_END, NOW))["states"]
        self.assertIn(R.PRICE_VINTAGE_BREAK, states)

    def test_insufficient_coverage_is_not_ready(self):
        self.assertNotReady(self.check(build(dates=ANCHOR_DATES[:4], per_date=10)),
                            "resolvable")

    def test_a_consumed_test_is_not_ready(self):
        L.append(V.EXPERIMENT_ID, L.OPENING, {}, self.ledger)
        self.assertNotReady(self.check(build()), "CONSUMED")

    def test_an_unregistered_test_is_not_ready(self):
        other = os.path.join(self.dir, "empty.jsonl")
        self.assertFalse(evaluate_readiness(build(), NOW, other)["ready"])

    def test_a_changed_spec_fingerprint_is_not_ready(self):
        wrong = os.path.join(self.dir, "wrong.jsonl")
        L.register(V.EXPERIMENT_ID, "a-different-spec", {"max_quality_exclusion_share": 0.05}, wrong)
        report = evaluate_readiness(build(), NOW, wrong)
        self.assertNotReady(report, "fingerprint")

    def test_a_tampered_ledger_is_not_ready(self):
        with open(self.ledger, "a", encoding="utf-8") as handle:
            handle.write('{"test_id": "x", "state": "REGISTERED", "entry_hash": "forged"}\n')
        self.assertNotReady(self.check(build()), "ledger")

    def test_duplicate_candles_cannot_exist(self):
        conn = build()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO price_candle_cache (instrument_id, interval, timestamp, "
                         "close) VALUES ('us-x','1d','2026-08-17T04:00:00+00:00', 1)")


class TestDatesAndIdentity(ReadinessCase):

    def test_earliest_theoretical_date_is_derived_not_hardcoded(self):
        conn = build(candle_end="2026-09-05", requested_at="2026-09-05T22:00:00+00:00")
        items = R.assess(conn, V.PROTECTED_START, V.PROTECTED_END, NOW)
        earliest = R.earliest_theoretical_date(items)
        # Last anchor Thu 2026-08-27 15:00: session 0 = Fri 08-28; the 21st
        # weekday session is Fri 09-25 (weekday projection, lower bound).
        self.assertEqual(earliest, date(2026, 9, 26))

    def test_identical_data_has_identical_identity(self):
        a = build(); b = build()
        ia = R.dataset_identity(a, R.assess(a, V.PROTECTED_START, V.PROTECTED_END, NOW))
        ib = R.dataset_identity(b, R.assess(b, V.PROTECTED_START, V.PROTECTED_END, NOW))
        self.assertEqual(ia, ib)

    def test_changed_label_changes_identity(self):
        a = build(); b = build()
        b.execute("UPDATE research_labels SET value_json='9.99' WHERE rowid = 1")
        ia = R.dataset_identity(a, R.assess(a, V.PROTECTED_START, V.PROTECTED_END, NOW))
        ib = R.dataset_identity(b, R.assess(b, V.PROTECTED_START, V.PROTECTED_END, NOW))
        self.assertNotEqual(ia, ib)


class TestTradingCalendar(unittest.TestCase):
    """§21, on anchor-v2 itself: horizons follow real sessions, not arithmetic."""

    W = {w.name: w for w in DEFAULT_WINDOWS}

    def daily(self, days):
        return [Candle(timestamp=datetime.combine(d, datetime.min.time(), tzinfo=UTC)
                       + timedelta(hours=4), close=100.0 + i) for i, d in enumerate(days)]

    def test_a_holiday_absent_from_the_data_is_skipped(self):
        days = [d for d in weekdays("2026-08-24", "2026-09-30") if d != date(2026, 9, 7)]
        candles = self.daily(days)
        sessions = [c.timestamp for c in candles]
        r = resolve_v2(datetime(2026, 9, 4, 21, tzinfo=UTC), self.W["d1"], [], candles, sessions)
        # Session 0 after a Friday-evening event is Tue 09-08 (Labor Day is
        # absent from the data); d1 ends at session 1, Wed 09-09. That is
        # the engine's existing definition, unchanged by anchor-v2.
        self.assertEqual(r.after.timestamp.date(), date(2026, 9, 9))
        self.assertNotIn(date(2026, 9, 7), [s.date() for s in sessions])

    def test_year_boundary(self):
        days = weekdays("2026-12-20", "2027-01-20")
        candles = self.daily(days)
        sessions = [c.timestamp for c in candles]
        r = resolve_v2(datetime(2026, 12, 31, 21, tzinfo=UTC), self.W["d1"], [], candles, sessions)
        self.assertEqual(r.before.timestamp.date(), date(2026, 12, 30))
        self.assertEqual(r.after.timestamp.date().year, 2027)

    def test_crypto_projection_counts_weekends(self):
        anchor = datetime(2026, 9, 11, 12, tzinfo=UTC)                  # Friday
        self.assertEqual(R.project_session([], anchor, 1, crypto=True), date(2026, 9, 12))
        self.assertEqual(R.project_session([], anchor, 1, crypto=False), date(2026, 9, 14))


if __name__ == "__main__":
    unittest.main(verbosity=2)
