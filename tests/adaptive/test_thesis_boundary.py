"""
tests/adaptive/test_thesis_boundary.py
---------------------------------------------------
Phase 25.9I-E: the engine recommends, and can do nothing else.

Market-data requests, the execution boundary, persistence, lineage,
as-of safety and the flags. The recurring theme: a recommendation that
could reach a venue would not be a recommendation.
"""

import ast
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.adaptive.risk_budget import RiskConfig
from src.adaptive.thesis import PositionThesisEngine, ThesisPolicy
from src.adaptive.thesis_repository import AdaptiveThesisRepository
from src.adaptive.universe import MarketHistory
from src.data_access.adaptive_thesis_schema import (
    ADAPTIVE_THESIS_TABLES, initialize_adaptive_thesis_schema,
)
from src.domain.adaptive_models import MarketDataState, RealtimeActivationReason
from src.domain.thesis_models import (
    EvidenceDirection, ExpectedHorizon, InvalidationZone, PositionAction,
    PositionDirection, TargetZone, ThesisRevisionReason,
)
from tests.adaptive.test_dynamic_universe import build_market
from tests.adaptive.test_position_thesis import (
    DAY0, day, engine, evidence, thesis_of,
)

UTC = timezone.utc
ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
THESIS_MODULES = ("thesis.py", "thesis_repository.py")


class TestMarketDataRequests(unittest.TestCase):
    """§21–§22 — the engine asks for a state; it never activates one."""

    def test_an_exit_requires_hot_execution_grade_data(self):
        assessment = engine().reassess(
            thesis_of(), day(4),
            evidence=[evidence("ev", EvidenceDirection.NEGATIVE, 0.9,
                               invalidates=True)])
        self.assertIs(assessment.required_state, MarketDataState.HOT)
        self.assertTrue(assessment.freshness.requires_realtime_marker)
        self.assertFalse(assessment.freshness.permits_delayed)
        self.assertEqual(assessment.freshness.max_age_seconds, 60.0)

    def test_a_partial_exit_also_requires_hot(self):
        assessment = engine().reassess(
            thesis_of(), day(4), evidence=[evidence()], exit_fraction=0.4)
        self.assertIs(assessment.required_state, MarketDataState.HOT)
        self.assertIs(assessment.activation_reason,
                      RealtimeActivationReason.EXIT_CANDIDATE)

    def test_a_quiet_hold_needs_only_cold_data(self):
        assessment = engine().reassess(thesis_of(), day(2), price=101.0)
        self.assertIs(assessment.required_state, MarketDataState.COLD)
        self.assertTrue(assessment.freshness.permits_delayed)

    def test_target_proximity_requests_warm_not_hot(self):
        assessment = engine().reassess(thesis_of(), day(2), price=109.0)
        self.assertIs(assessment.required_state, MarketDataState.WARM)
        self.assertIs(assessment.activation_reason,
                      RealtimeActivationReason.TARGET_PROXIMITY)

    def test_no_subscription_is_activated_anywhere(self):
        base = os.path.join(ROOT, "src", "adaptive")
        for name in THESIS_MODULES:
            with open(os.path.join(base, name), encoding="utf-8") as handle:
                tree = ast.parse(handle.read())
            calls = {node.func.attr for node in ast.walk(tree)
                     if isinstance(node, ast.Call)
                     and isinstance(node.func, ast.Attribute)}
            for forbidden in ("subscribe", "market_snapshot",
                              "init_brokerage_session", "activate"):
                self.assertNotIn(forbidden, calls, f"{name} calls {forbidden}")


class TestExecutionBoundary(unittest.TestCase):
    """§39–§40 — nothing here can become an order."""

    def test_every_recommendation_is_non_executable(self):
        for price, ev in ((109.0, ()), (93.0, ()),
                          (None, (evidence(),))):
            assessment = engine().reassess(thesis_of(), day(4), price=price,
                                           evidence=ev)
            self.assertFalse(assessment.is_executable)

    def test_the_engine_imports_no_execution_trading_or_portfolio_module(self):
        base = os.path.join(ROOT, "src", "adaptive")
        for name in THESIS_MODULES:
            with open(os.path.join(base, name), encoding="utf-8") as handle:
                tree = ast.parse(handle.read())
            for node in ast.walk(tree):
                modules = []
                if isinstance(node, ast.Import):
                    modules = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""]
                for module in modules:
                    for forbidden in ("src.execution", "src.trading",
                                      "src.portfolio"):
                        self.assertFalse(module.startswith(forbidden),
                                         f"src/adaptive/{name} imports {module}")

    def test_no_order_intent_or_broker_call_exists_in_the_engine(self):
        base = os.path.join(ROOT, "src", "adaptive")
        for name in THESIS_MODULES:
            with open(os.path.join(base, name), encoding="utf-8") as handle:
                source = handle.read()
            for forbidden in ("OrderIntent(", "AllocationProposal(",
                              "place_order", "submit_order", "cancel_order",
                              "modify_order", "TradingModeStore"):
                self.assertNotIn(forbidden, source, f"{name} contains {forbidden}")

    def test_the_canonical_risk_gate_is_still_the_only_approver(self):
        from src.domain.portfolio_models import (
            OrderIntent, RiskDecision, RiskDecisionState,
        )
        rejected = RiskDecision(decision_id="rd", portfolio_id="p", as_of=DAY0,
                                state=RiskDecisionState.REJECTED)
        with self.assertRaises(ValueError):
            OrderIntent.require_approval(rejected)

    def test_the_cli_creates_no_orders_and_reports_non_executable(self):
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.path.abspath(ROOT)
        result = subprocess.run(
            [sys.executable, os.path.join(ROOT, "scripts", "adaptive_thesis.py"),
             "--scenario", "all", "--json"],
            capture_output=True, text=True, env=environment, timeout=600)
        self.assertEqual(result.returncode, 0, result.stderr[-800:])
        report = json.loads(result.stdout)
        self.assertFalse(report["flags"]["position_thesis_enabled"])
        self.assertFalse(report["flags"]["dynamic_exit_enabled"])
        for scenario in report["scenarios"]:
            self.assertFalse(scenario["assessment"]["is_executable"])


class TestFlags(unittest.TestCase):

    def test_both_flags_default_off(self):
        from src.adaptive import flags as adaptive_flags
        resolved = adaptive_flags.resolve({})
        self.assertFalse(resolved.position_thesis_enabled)
        self.assertFalse(resolved.dynamic_exit_enabled)

    def test_dynamic_exit_is_suppressed_without_the_thesis_flag(self):
        from src.adaptive import flags as adaptive_flags
        resolved = adaptive_flags.resolve(
            {adaptive_flags.FLAG_ENV["dynamic_exit_enabled"]: "true"})
        self.assertFalse(resolved.dynamic_exit_enabled)
        self.assertTrue(any("position_thesis_enabled" in s
                            for s in resolved.suppressed))

    def test_both_flags_on_still_enables_no_broker_write(self):
        from src.adaptive import flags as adaptive_flags
        resolved = adaptive_flags.resolve({
            adaptive_flags.FLAG_ENV["dynamic_exit_enabled"]: "true",
            adaptive_flags.FLAG_ENV["position_thesis_enabled"]: "true"})
        self.assertTrue(resolved.dynamic_exit_enabled)
        self.assertFalse(resolved.describe()["enables_broker_writes"])


class TestPersistence(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.conn = sqlite3.connect(os.path.join(self.dir, "store.db"))
        self.repository = AdaptiveThesisRepository(self.conn)
        self.engine = engine()
        self.thesis = thesis_of(self.engine)
        self.assessment = self.engine.reassess(
            self.thesis, day(4), evidence=[evidence()],
            proposed_target=TargetZone(117.0, 122.0),
            proposed_invalidation=InvalidationZone(101.0, 102.0),
            exit_fraction=0.4)

    def tearDown(self):
        self.conn.close()

    def test_initialization_creates_only_its_own_tables(self):
        fresh = sqlite3.connect(":memory:")
        initialize_adaptive_thesis_schema(fresh)
        tables = {r[0] for r in fresh.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, set(ADAPTIVE_THESIS_TABLES))
        fresh.close()

    def test_saving_twice_writes_one_record(self):
        self.assertTrue(self.repository.save_assessment(self.assessment))
        self.assertFalse(self.repository.save_assessment(self.assessment))
        counts = {table: self.conn.execute(
            "SELECT COUNT(*) FROM %s" % table).fetchone()[0]
            for table in ADAPTIVE_THESIS_TABLES}
        self.assertEqual(counts["adaptive_exit_recommendations"], 1)
        self.assertEqual(counts["adaptive_thesis_revisions"], 1)
        self.assertEqual(counts["adaptive_position_theses"], 2)   # before + after

    def test_a_revision_does_not_overwrite_its_predecessor(self):
        self.repository.save_assessment(self.assessment)
        stored = self.repository.thesis(self.thesis.thesis_id)
        self.assertEqual(stored["target"], (108.0, 112.0))
        self.assertEqual(stored["remaining_fraction"], 1.0)
        revised = self.repository.thesis(
            self.assessment.revised_thesis.thesis_id)
        self.assertEqual(revised["target"], (117.0, 122.0))
        self.assertAlmostEqual(revised["remaining_fraction"], 0.6)

    def test_history_is_readable_in_order(self):
        self.repository.save_assessment(self.assessment)
        history = self.repository.history(self.thesis.instrument_id)
        self.assertEqual([h["revision"] for h in history], [1, 2])
        self.assertEqual(history[1]["previous_thesis_id"],
                         self.thesis.thesis_id)

    def test_recommendations_are_stored_as_non_executable(self):
        self.repository.save_assessment(self.assessment)
        rows = self.repository.recommendations(self.thesis.thesis_id)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["is_executable"])
        self.assertEqual(rows[0]["action"], "partial_exit")

    def test_lineage_back_to_the_allocation_and_universe_is_stored(self):
        self.repository.save_thesis(self.thesis)
        row = self.conn.execute(
            "SELECT session_snapshot_id, admission_decision_id, "
            "allocation_plan_id FROM adaptive_position_theses WHERE thesis_id = ?",
            (self.thesis.thesis_id,)).fetchone()
        self.assertEqual(row, ("su-x", "adm-x", "alloc-x"))

    def test_the_repository_writes_only_adaptive_tables(self):
        with open(os.path.join(ROOT, "src", "adaptive", "thesis_repository.py"),
                  encoding="utf-8") as handle:
            source = handle.read()
        for statement in ("INSERT INTO", "INSERT OR IGNORE INTO", "UPDATE ",
                          "DELETE FROM"):
            index = 0
            while True:
                index = source.find(statement, index)
                if index == -1:
                    break
                fragment = source[index + len(statement):index + len(statement) + 40]
                self.assertTrue(fragment.strip().startswith("adaptive_"),
                                f"writes outside its tables: {fragment!r}")
                index += 1

    def test_production_databases_hold_no_thesis_tables(self):
        for db in ("data/marketlens.db", "data/capture/intraday_capture.db"):
            path = os.path.join(ROOT, db)
            if not os.path.exists(path):
                continue
            conn = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"),
                                   uri=True)
            try:
                names = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE name LIKE 'adaptive_%'")}
            finally:
                conn.close()
            self.assertEqual(names & set(ADAPTIVE_THESIS_TABLES), set())


class TestAsOfSafety(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = build_market(
            os.path.join(self.dir, "market.db"),
            {"us_and_intl-demo": {"daily_drift": 0.002},
             "benchmark-spy": {"volume": 50_000_000}},
            start=datetime(2026, 5, 1, tzinfo=UTC), days=200)

    def test_future_candles_cannot_change_a_thesis_assessment(self):
        as_of = datetime(2026, 8, 1, 14, 0, tzinfo=UTC)
        truncated = os.path.join(self.dir, "truncated.db")
        source = sqlite3.connect(self.db)
        source.execute("ATTACH DATABASE ? AS trimmed", (truncated,))
        source.execute("""CREATE TABLE trimmed.price_candle_cache AS
                          SELECT * FROM price_candle_cache WHERE timestamp <= ?""",
                       (as_of.isoformat(),))
        source.commit()
        source.close()

        def assess(db):
            with MarketHistory(db, as_of) as history:
                volatility = history.history("us_and_intl-demo").realized_volatility()
            eng = engine()
            thesis = thesis_of(eng)
            estimate = eng.risk_estimate(thesis, daily_volatility=volatility)
            assessment = eng.reassess(thesis, as_of, price=105.0)
            return estimate, assessment

        full_estimate, full_assessment = assess(self.db)
        cut_estimate, cut_assessment = assess(truncated)
        self.assertEqual(full_estimate.risk_per_dollar,
                         cut_estimate.risk_per_dollar)
        self.assertEqual(full_assessment.action, cut_assessment.action)
        self.assertEqual(full_assessment.recommendation.recommendation_id,
                         cut_assessment.recommendation.recommendation_id)

    def test_the_engine_reads_no_database_at_all(self):
        # Every input is an argument, so there is no query to bound.
        base = os.path.join(ROOT, "src", "adaptive")
        with open(os.path.join(base, "thesis.py"), encoding="utf-8") as handle:
            source = handle.read()
        for forbidden in ("sqlite3", "connect(", "SELECT "):
            self.assertNotIn(forbidden, source)

    def test_no_protected_experiment_artefact_is_referenced(self):
        base = os.path.join(ROOT, "src", "adaptive")
        for name in THESIS_MODULES:
            with open(os.path.join(base, name), encoding="utf-8") as handle:
                source = handle.read().lower()
            for forbidden in ("anchor_v2", "anchor-v2", "validate_d20",
                              "protected_ledger", "return_60d"):
                self.assertNotIn(forbidden, source, f"{name} mentions {forbidden}")

    def test_no_future_outcome_or_pnl_feeds_the_engine(self):
        with open(os.path.join(ROOT, "src", "adaptive", "thesis.py"),
                  encoding="utf-8") as handle:
            source = handle.read().lower()
        for forbidden in ("realized_pnl", "future_return", "outcome_return"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
